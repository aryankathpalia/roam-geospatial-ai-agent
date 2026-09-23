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
import math
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


# A SEPARATE, second real limit discovered live on a 127-page/56-
# ParcelMap-region document: gemini-3.5-flash-lite's free tier also
# caps INPUT TOKENS at 250,000/minute (distinct from the 15
# requests/minute cap above -- confirmed via a real 429:
# "GenerateContentInputTokensPerModelPerMinute-FreeTier ... quotaValue:
# 250000"). Sending every region's tiles in ONE combined call (the fix
# that solved the requests/minute problem) doesn't help here -- a
# single oversized call blows the TOKEN budget outright, and it's a
# per-minute budget shared across calls, not a per-call cap, so
# multiple smaller calls in the same minute can still add up to a 429.
#
# Gemini's documented image tokenization: an image is tiled into
# 768x768 blocks, each costing 258 tokens (ceil(w/768) * ceil(h/768) *
# 258). Our tiles are ~700x700 (see _tile_image), so in practice this
# is almost always exactly 258 tokens/tile -- but the real formula is
# used anyway rather than a flat constant, so this keeps working
# correctly if _tile_image's tile_size ever changes.
_TOKENS_PER_IMAGE_TILE_BLOCK = 258
_IMAGE_TILE_BLOCK_PX = 768
# ~4 characters/token is the standard rough estimate for English text
# (Gemini doesn't expose an official ratio for arbitrary prompt text).
_CHARS_PER_TOKEN_ESTIMATE = 4
# Budget each call to well under the real 250,000/minute limit --
# both because multiple calls can land in the same rolling minute
# (this budget is also what the token-aware rate limiter below paces
# against) and because our own token estimate is an approximation, not
# a guarantee of Gemini's actual count.
_TOKEN_BUDGET_PER_MINUTE = 180_000

_token_usage: deque[tuple[float, int]] = deque()
_token_budget_lock = threading.Lock()


def _estimate_image_tokens(image: Image.Image) -> int:
    width, height = image.size
    tiles_x = math.ceil(width / _IMAGE_TILE_BLOCK_PX)
    tiles_y = math.ceil(height / _IMAGE_TILE_BLOCK_PX)
    return tiles_x * tiles_y * _TOKENS_PER_IMAGE_TILE_BLOCK


def _estimate_text_tokens(text: str) -> int:
    return math.ceil(len(text) / _CHARS_PER_TOKEN_ESTIMATE)


def _wait_for_token_budget(tokens_needed: int) -> None:
    """
    Sliding-window limiter on ESTIMATED input tokens, mirroring
    _wait_for_rate_limit's request-count version but for the separate
    per-minute token cap. A single call is allowed to exceed the
    budget on its own (there's no way to split one region's tiles
    across calls in the current design) -- it just has to wait for the
    window to clear first, same as any other call.
    """

    with _token_budget_lock:
        now = time.monotonic()
        while _token_usage and now - _token_usage[0][0] > 60:
            _token_usage.popleft()

        used = sum(tokens for _, tokens in _token_usage)
        if used > 0 and used + tokens_needed > _TOKEN_BUDGET_PER_MINUTE:
            sleep_for = 60 - (now - _token_usage[0][0]) + 0.5
        else:
            sleep_for = 0

        if sleep_for <= 0:
            _token_usage.append((now, tokens_needed))
            return

    time.sleep(sleep_for)
    with _token_budget_lock:
        _token_usage.append((time.monotonic(), tokens_needed))


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


_DOCUMENT_TILE_PROMPT_TEMPLATE = """\
These are pieces of {region_count} land survey / parcel map drawings
from one document, grouped by region number and piece within that
region (see layout below -- region numbers are this document's real
region numbers and may not start at 1 or be contiguous, when this is
only some of the document's regions). For EACH piece, list, exactly as
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
drawings ({region_list}), from one document.

IMPORTANT -- a single region can show MORE THAN ONE parcel. Survey
exhibits routinely draw two or more adjacent parcels on one sheet (a
"Parcel Map Exhibit" creating "PARCEL 1" and "PARCEL 2" side by side is
common), and a region's bounding box wraps the whole sheet, not one
parcel. Before extracting anything:
1. For EACH region, scan its notes for every distinct parcel label
   (e.g. "PARCEL 1", "PARCEL 2", "LOT 3") -- a "RESULTANT PARCEL AREAS"
   or similar summary table listing multiple labels with their own
   acreage is a strong signal of exactly how many parcels that region
   contains.
2. Return ONE ENTRY PER PARCEL FOUND, not one entry per region. Two
   entries from the same region share the same region_index but have
   different parcel_label values and, critically, DISJOINT
   boundary_calls -- a call belongs to exactly one parcel's traverse,
   never both, even if two parcels share a common edge (assign a
   shared edge's call to whichever parcel's label it's written closest
   to / associated with in the notes, not to both).
3. If a region's notes only ever mention one parcel label (or none),
   return exactly one entry for it, same as before.
4. Only extract parcels that THIS drawing is itself defining the
   boundary of -- typically the parcel(s) listed in that region's own
   "RESULTANT PARCEL AREAS" table (or similar), or whose full traverse
   is walked in the notes. Do NOT extract a "parcel" whose label only
   ever appears as a NEIGHBOR/CONTEXT citation, e.g. an adjoining
   owner's name plus a parcel label and APN written near the outer
   edge for reference ("ROBERT L. CARSEY JR., PARCEL 1B RS 6231 (R3),
   APN: 086-260-21") -- that names an adjacent property, not one this
   document is establishing, and has no traverse of its own here.

For boundary_calls specifically: only include an entry if it has BOTH
a bearing AND a distance stated together as a single call on THAT
parcel's own OUTER boundary line. Do NOT include:
- a bare dimension number, curve table length, or interior measurement
  (e.g. a building or setback dimension)
- a bearing or distance shown in PARENTHESES -- survey plats use
  parentheses for reference/record citations (a prior deed's stated
  bearing, a tie to a section corner), not the as-surveyed boundary
  call. Only use the un-parenthesized value.
- a measurement labeled as road frontage, right-of-way, or a
  quitclaim/dedication area -- those describe the road, not a parcel's
  boundary.
- a bearing/distance that TIES the parcel to an outside reference
  point -- a found section/quarter-section corner, a survey control
  point, or another monument -- rather than walking corner-to-corner
  around the parcel itself. This is a distinct exclusion from the
  parentheses rule above: a tie value is often NOT itself in
  parentheses (only its alternate record citations are), so check
  what it CONNECTS, not just its punctuation -- if the notes place it
  near "TIE", a control-point/monument label, or coordinates for a
  point outside the parcel's own corners, it's a tie, not a boundary
  call.
- a call that visibly belongs to a DIFFERENT parcel's own perimeter
  (e.g. the far side of an adjacent parcel, or a call the notes
  associate with a different parcel label than the one you're
  currently building).
Leave a call out entirely rather than pairing it with a null bearing,
and leave it out entirely rather than guessing which parcel it belongs
to.

The notes were read tile-by-tile, which is NOT the order the calls
appear walking around each parcel -- you must reorder them yourself,
per parcel. Put each parcel's boundary_calls in true walking sequence
around ITS OWN perimeter (consistently clockwise or counter-clockwise,
starting anywhere), using positional cues in the notes (corner labels,
"top"/"bottom"/"east side" mentions, which piece each call came from)
to infer the correct sequence. A correctly ordered traverse returns
close to its starting point after the last call -- if your ordering
doesn't, re-check both the order AND whether every call truly belongs
to this parcel before answering.

Tag every entry with its region_index matching that region's actual
number above (NOT a 1-based position in this list -- use the real
region numbers).

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


def _chunk_regions_by_token_budget(
    images: list[Image.Image],
) -> list[list[tuple[int, list[Image.Image]]]]:
    """
    Groups (1-based global region_index, tiles) into chunks that each
    stay under _TOKEN_BUDGET_PER_MINUTE of estimated image tokens.

    This is what makes the batching DYNAMIC instead of hardcoded: a
    document with a handful of ParcelMap regions (a few thousand
    tokens) gets ONE chunk -- same as before, still just 2 Gemini calls
    total. A document with 56 regions (a real one that blew the
    250,000 tokens/minute free-tier quota when sent as a single call --
    see the module docstring above) gets however many chunks its
    actual image data needs, computed from Gemini's own documented
    tiling formula rather than a guessed region count cutoff.

    Regions are packed greedily in order, so a chunk's region_index
    values are always a contiguous run (e.g. [4, 5, 6]) -- relied on
    only for readability of the resulting prompts, not required for
    correctness.
    """

    chunks: list[list[tuple[int, list[Image.Image]]]] = []
    current: list[tuple[int, list[Image.Image]]] = []
    current_tokens = 0

    for region_idx, image in enumerate(images, start=1):
        tiles = _tile_image(image)
        region_tokens = sum(_estimate_image_tokens(t) for t in tiles)

        if current and current_tokens + region_tokens > _TOKEN_BUDGET_PER_MINUTE:
            chunks.append(current)
            current = []
            current_tokens = 0

        current.append((region_idx, tiles))
        current_tokens += region_tokens

    if current:
        chunks.append(current)

    return chunks


def extract_parcel_geometries_batch(images: list[Image.Image]) -> list[list[dict]]:
    """
    Extracts structured geometry for MULTIPLE ParcelMap regions,
    chunked dynamically by estimated Gemini input-token cost (see
    _chunk_regions_by_token_budget) rather than always sent as one
    call -- a document with a few regions still needs just 2 Gemini
    calls total (one to read all tiles, one to structure the result),
    same as the original all-in-one-call design; a document with many
    regions automatically gets split into as many read+structure call
    pairs as its actual token budget requires.

    Returns ONE LIST PER input region/image (same order as `images`),
    and each region's list holds one dict PER PARCEL found in it --
    not one dict per region. A region's ParcelMap bounding box wraps
    the whole drawing, not necessarily one parcel: a real "Parcel Map
    Exhibit" sheet showing two adjacent parcels ("PARCEL 1"/"PARCEL 2"
    side by side, a common real-world layout) is one region but two
    parcels, and assuming otherwise was confirmed (via a live diagnostic
    against a real such document, reproducible even with a single
    region processed in complete isolation -- not a batching artifact)
    to make the model interleave both parcels' boundary calls into one
    nonsensical traverse. Most regions still yield a single-item list.

    Raises on failure -- callers should catch and degrade gracefully.
    """

    if not images:
        return []

    client = _get_client()
    chunks = _chunk_regions_by_token_budget(images)

    by_index: dict[int, list[dict]] = {}

    for chunk in chunks:
        all_parts: list[types.Part] = []
        region_layout_lines = []
        image_tokens = 0
        for region_idx, tiles in chunk:
            region_layout_lines.append(f"Region {region_idx}: {len(tiles)} piece(s)")
            for tile in tiles:
                image_tokens += _estimate_image_tokens(tile)
                buffer = io.BytesIO()
                tile.convert("RGB").save(buffer, format="PNG")
                all_parts.append(
                    types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/png")
                )

        read_prompt = _DOCUMENT_TILE_PROMPT_TEMPLATE.format(
            region_count=len(chunk),
            region_layout="\n".join(region_layout_lines),
        )

        _wait_for_rate_limit()
        _wait_for_token_budget(image_tokens + _estimate_text_tokens(read_prompt))
        read_response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=all_parts + [read_prompt],
            # Transcription, not creative generation -- same
            # determinism rationale as the structuring call below.
            config=types.GenerateContentConfig(temperature=0),
        )
        notes = read_response.text.strip()

        if not notes:
            continue

        region_indices = [region_idx for region_idx, _ in chunk]
        structure_prompt = _BATCH_STRUCTURE_PROMPT.format(
            region_count=len(chunk),
            region_list=", ".join(f"Region {i}" for i in region_indices),
            notes=notes,
        )

        _wait_for_rate_limit()
        _wait_for_token_budget(_estimate_text_tokens(structure_prompt))
        structure_response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[structure_prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_BATCH_RESPONSE_SCHEMA,
                # This step is structured data extraction from fixed
                # notes, not creative generation -- temperature=0
                # minimizes run-to-run variance (confirmed via a real
                # diagnostic: identical input crops produced visibly
                # different parcel/call attributions across repeated
                # default-temperature runs).
                temperature=0,
            ),
        )
        parsed = json.loads(structure_response.text)
        for item in parsed:
            by_index.setdefault(item["region_index"], []).append(item)

    results = []
    for region_idx in range(1, len(images) + 1):
        parcels = by_index.get(region_idx, [])
        results.append(
            [
                {
                    "boundary_calls": item.get("boundary_calls", []),
                    "tie_point": item.get("tie_point"),
                    "basis_of_bearings": item.get("basis_of_bearings"),
                    "parcel_label": item.get("parcel_label"),
                }
                for item in parcels
            ]
        )
    return results
