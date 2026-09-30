"""
Gemini-assisted edge association for calibration.

OCR proximity matching (calibration.py) fails on dense multi-lot sheets:
the printed bearing/distance for an edge often sits too far from it, or
beside a neighbor's edge. This module asks Gemini to read the printed
text for each edge of ONE confirmed polygon.

Deliberately narrow (broader framings tested worse, twice -- see
KNOWN_ISSUES.md): exactly ONE image per call, exactly ONE polygon drawn
on it (numbered edges), one target parcel, one question. Never draw
neighbors or ask it to read the whole sheet.

Output feeds calibration.calibrate() as extra candidates. They go through
the SAME scale/tolerance/rotation checks as OCR candidates; Gemini's
reading is never trusted on its own.
"""

from __future__ import annotations

import io
import json
import logging
import math

from PIL import Image, ImageDraw

from app.services.geometry import parse_bearing

logger = logging.getLogger(__name__)

SOURCE = "gemini_association"

_PROMPT = """This image is a crop of a land survey plat. One parcel is outlined in BLUE. \
Its edges are labeled E0, E1, E2, ... in red, each label placed at the edge's midpoint; \
edge Ei runs from the blue vertex marked V{i} to the next vertex.

For each labeled edge of the blue parcel ONLY, read the bearing and/or distance that is \
printed on the plat alongside that edge (the text that describes that specific boundary line).

Rules:
- Only consider the blue parcel. Ignore every other parcel, lot, easement and neighbor, \
even if its text is closer to the blue line.
- Transcribe exactly what is printed. Never infer, compute, or guess a value. If an edge \
has no legible printed bearing/distance, omit it.
- Ignore text marked as superseded or to be removed, and parenthetical record/reference values.
- bearing_text is the bearing as printed (e.g. N 45°30'10" E); distance_ft is the \
distance in feet as a number. Either may be null if only the other is printed.
- Return the JSON object only."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "edge_index": {"type": "integer"},
                    "bearing_text": {"type": "string", "nullable": True},
                    "distance_ft": {"type": "number", "nullable": True},
                },
                "required": ["edge_index"],
            },
        }
    },
    "required": ["edges"],
}


def render_polygon_overlay(crop: Image.Image, vertices: list[tuple[float, float]]) -> Image.Image:
    """`vertices` are in `crop` pixel coordinates. One blue polygon, numbered edges."""
    img = crop.convert("RGB").copy()
    draw = ImageDraw.Draw(img)
    n = len(vertices)
    width = max(2, round(max(img.size) / 600))
    for i in range(n):
        draw.line([vertices[i], vertices[(i + 1) % n]], fill=(0, 70, 255), width=width)
    r = width * 2
    for i, (x, y) in enumerate(vertices):
        draw.ellipse([x - r, y - r, x + r, y + r], outline=(0, 70, 255), width=width)
    for i in range(n):
        (ax, ay), (bx, by) = vertices[i], vertices[(i + 1) % n]
        draw.text(((ax + bx) / 2 + r, (ay + by) / 2 - r * 3), f"E{i}", fill=(220, 0, 0))
    return img


def parse_response(payload: dict, n_edges: int) -> list[dict]:
    """Gemini JSON -> calibration candidates: {edge_index, value, azimuth, source, raw}."""
    out = []
    for item in payload.get("edges", []):
        idx = item.get("edge_index")
        if not isinstance(idx, int) or not 0 <= idx < n_edges:
            continue
        dist = item.get("distance_ft")
        bearing_text = item.get("bearing_text")
        if isinstance(bearing_text, str):
            # Live Gemini (JSON mode) intermittently emits U+0000 where the
            # degree sign belongs (seen 1 call in 18: 'N 61\x0053\'54" E'),
            # which parse_bearing rejects, silently dropping every bearing in
            # that response. Map exactly that character, only here -- the
            # shared parser stays as strict as before.
            bearing_text = bearing_text.replace("\x00", "\u00b0")
        az = parse_bearing(bearing_text) if bearing_text else None
        if not isinstance(dist, (int, float)) or isinstance(dist, bool) or not math.isfinite(dist) or dist <= 0:
            dist = None
        if dist is None:
            continue  # calibration's corroboration is distance-based
        out.append({
            "edge_index": idx, "value": float(dist), "azimuth": az, "source": SOURCE,
            "raw": {"bearing_text": bearing_text, "distance_ft": dist},
        })
    return out


def associate_edges(
    page_img: Image.Image,
    crop_box: tuple[int, int, int, int],
    polygon_crop_px: list[tuple[float, float]],
) -> list[dict]:
    """One Gemini call: crop `crop_box` of the page, draw the single polygon, read per-edge text."""
    # Imported lazily so calibration/tests don't need the Gemini SDK.
    from google.genai import types

    from app.services import vision

    overlay = render_polygon_overlay(page_img.crop(crop_box), polygon_crop_px)
    buf = io.BytesIO()
    overlay.save(buf, format="PNG")
    client = vision._get_client()
    vision._wait_for_rate_limit()
    response = vision._generate_with_fallback(
        client,
        contents=[types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"), _PROMPT],
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=_SCHEMA, temperature=0,
        ),
    )
    return parse_response(json.loads(response.text), len(polygon_crop_px))
