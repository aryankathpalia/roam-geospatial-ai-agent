"""
Vision escalation for ParcelMap crops -- reads a survey drawing and
extracts the structured data needed to reconstruct real geometry:
the boundary traverse (bearing/distance calls), any ground-coordinate
tie point, and the basis of bearings (coordinate system/zone).

Two-stage design, and NOT the obvious one-shot "image in, JSON out"
call -- that was tested directly against real ParcelMap crops and
consistently failed with 503 "high demand" errors. Isolated the real
cause empirically (see this session's investigation): it's not
schema-related, not prompt-length-related, and not rate-limiting --
it's specifically LARGE/DENSE images combined with a fine-detail
extraction task. A small, simple crop (a seal) with the same kind of
extraction prompt succeeded immediately; a one-sentence description of
the SAME dense parcel map succeeded immediately; but asking Gemini to
carefully read bearings/distances off the full dense drawing failed
100% of the time (4/4 retries, 3 different models, with and without
response_schema). Reading fine detail off a large image evidently
routes through a more capacity-constrained path than a quick
description or a small image does.

Fix, mirroring the same tiling approach that fixed OCR:
  1. Split the ParcelMap crop into tiles (small images -> the path
     that works) and ask each tile, in plain language (no schema --
     also part of what worked), what bearing/distance/coordinate/label
     text is visible on it.
  2. Merge the tiles' raw findings and send them, as plain TEXT (no
     image at all), to a second call that DOES use response_schema to
     produce the final structured JSON -- confirmed reliable, since
     the failure mode above never triggers on text-only input.
"""

import io
import json
import threading
import time
from collections import deque

from google import genai
from google.genai import types
from PIL import Image

from app.core.config import settings

# Gemini's free tier caps gemini-3.5-flash-lite at 15 requests/MINUTE
# (confirmed via real 429s from this account). A single ParcelMap
# region's tiled extraction alone makes 5-9 calls (one per tile, plus
# one to structure the result) -- a document with a few ParcelMap
# regions blows through 15/min even processed one region at a time,
# not just when run concurrently. This is a process-wide sliding-
# window limiter shared by every Gemini call in this module, capped
# below the real limit (12, not 15) for safety margin.
_RATE_LIMIT_PER_MINUTE = 12
_call_times: deque[float] = deque()
_rate_limit_lock = threading.Lock()


def _wait_for_rate_limit() -> None:
    with _rate_limit_lock:
        now = time.monotonic()
        while _call_times and now - _call_times[0] > 60:
            _call_times.popleft()

        if len(_call_times) >= _RATE_LIMIT_PER_MINUTE:
            sleep_for = 60 - (now - _call_times[0]) + 0.5
        else:
            sleep_for = 0

        if sleep_for <= 0:
            _call_times.append(now)
            return

    time.sleep(sleep_for)
    with _rate_limit_lock:
        _call_times.append(time.monotonic())

def _get_client() -> genai.Client:
    if not settings.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Get a free key at "
            "https://aistudio.google.com/apikey and set it in .env."
        )
    return genai.Client(api_key=settings.GEMINI_API_KEY)


def _tile_image(image: Image.Image, tile_size: int = 700) -> list[Image.Image]:
    """Grid-split an image into ~tile_size x ~tile_size pieces."""

    width, height = image.size
    cols = max(1, round(width / tile_size))
    rows = max(1, round(height / tile_size))

    tiles = []
    tile_w, tile_h = width / cols, height / rows
    for row in range(rows):
        for col in range(cols):
            x0, y0 = int(col * tile_w), int(row * tile_h)
            x1 = int(width) if col == cols - 1 else int((col + 1) * tile_w)
            y1 = int(height) if row == rows - 1 else int((row + 1) * tile_h)
            tiles.append(image.crop((x0, y0, x1, y1)))

    return tiles


def extract_parcel_geometry(image: Image.Image) -> dict:
    """
    Tiles a ParcelMap crop, reads each tile, then structures the
    combined findings into JSON. Raises on failure -- callers should
    catch and degrade gracefully (this is an enhancement on top of
    OCR text, not a hard requirement).
    """

    results = extract_parcel_geometries_batch([image])
    return results[0]


_DOCUMENT_TILE_PROMPT_TEMPLATE = """\
These are pieces of {region_count} different land survey / parcel map
drawings from one document, grouped by region (Region 1, Region 2,
...) and piece within that region. For EACH piece, list, exactly as
written, anything on it that is one of:
- a boundary bearing and distance call (e.g. N89*11'15"E 903.15')
- a ground/state-plane coordinate (e.g. "N 14926910.28 E 2251599.70")
- a basis-of-bearings / datum / coordinate-zone statement
- a parcel/lot label or acreage

Label each piece's findings clearly with its region and piece number
(e.g. "Region 1, Piece 2:"). Say "nothing relevant" for a piece with
none. Do not mix findings from different regions together.

{region_layout}
"""

_BATCH_STRUCTURE_PROMPT = """\
Below are raw notes read off pieces of {region_count} different survey
drawings (Region 1 through Region {region_count}), from one document.
For EACH region, consolidate its own notes into its boundary traverse
and related data. Use null for anything not present. Do not invent or
guess values, and do not mix data between regions.

Return one result per region, in order, each tagged with its
region_index (1-based, matching the region numbers below).

NOTES:
{notes}
"""

_BATCH_RESPONSE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "region_index": {"type": "integer"},
            "boundary_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "bearing": {"type": "string"},
                        "distance": {"type": "string"},
                    },
                    "required": ["bearing", "distance"],
                },
            },
            "tie_point": {
                "type": "object",
                "nullable": True,
                "properties": {
                    "northing": {"type": "string"},
                    "easting": {"type": "string"},
                    "anchors_corner": {"type": "string"},
                },
            },
            "basis_of_bearings": {"type": "string", "nullable": True},
            "parcel_label": {"type": "string", "nullable": True},
        },
        "required": ["region_index", "boundary_calls"],
    },
}

_EMPTY_RESULT = {
    "boundary_calls": [],
    "tie_point": None,
    "basis_of_bearings": None,
    "parcel_label": None,
}


def extract_parcel_geometries_batch(images: list[Image.Image]) -> list[dict]:
    """
    Extracts structured geometry for MULTIPLE ParcelMap regions in just
    2 Gemini calls total for the whole document, not 2 calls per
    region -- combines every region's tiles into one labeled multi-part
    call, then one structuring call that returns an array (one result
    per region). This is what actually matters given Gemini's
    free-tier 15 requests/minute cap: a document with several ParcelMap
    regions previously needed ~2 calls PER region; now it needs 2
    total, regardless of region count.

    Returns results in the same order as `images`. Raises on failure --
    callers should catch and degrade gracefully.
    """

    if not images:
        return []

    client = _get_client()

    all_parts: list[types.Part] = []
    region_layout_lines = []
    for region_idx, image in enumerate(images, start=1):
        tiles = _tile_image(image)
        region_layout_lines.append(f"Region {region_idx}: {len(tiles)} piece(s)")
        for tile in tiles:
            buffer = io.BytesIO()
            tile.convert("RGB").save(buffer, format="PNG")
            all_parts.append(
                types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/png")
            )

    prompt = _DOCUMENT_TILE_PROMPT_TEMPLATE.format(
        region_count=len(images),
        region_layout="\n".join(region_layout_lines),
    )

    _wait_for_rate_limit()
    read_response = client.models.generate_content(
        model=settings.GEMINI_MODEL,
        contents=all_parts + [prompt],
    )
    notes = read_response.text.strip()

    if not notes:
        return [dict(_EMPTY_RESULT) for _ in images]

    _wait_for_rate_limit()
    structure_response = client.models.generate_content(
        model=settings.GEMINI_MODEL,
        contents=[
            _BATCH_STRUCTURE_PROMPT.format(region_count=len(images), notes=notes)
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_BATCH_RESPONSE_SCHEMA,
        ),
    )
    parsed = json.loads(structure_response.text)

    by_index = {item["region_index"]: item for item in parsed}
    results = []
    for region_idx in range(1, len(images) + 1):
        item = by_index.get(region_idx)
        results.append(
            {
                "boundary_calls": item.get("boundary_calls", []) if item else [],
                "tie_point": item.get("tie_point") if item else None,
                "basis_of_bearings": item.get("basis_of_bearings") if item else None,
                "parcel_label": item.get("parcel_label") if item else None,
            }
        )
    return results
