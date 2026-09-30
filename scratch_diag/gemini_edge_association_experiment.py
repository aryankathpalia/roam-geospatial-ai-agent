"""
DIAGNOSTIC ONLY -- not production code, not imported by anything.

Tests whether a vision model (Gemini) can reliably associate printed
survey bearing/distance calls with specific edges of an already-confirmed
polygon, as an alternative to the current OCR-everything-then-proximity-
match approach in app/services/calibration.py.

Uses the SAME MAP 7 LOT 48 image and the SAME confirmed polygon vertices
already used earlier this session (scratch_diag/confirm_lot48.json /
confirm_lot48_3.json). Draws the confirmed polygon(s) directly onto the
region crop with numbered vertices so edge indices are unambiguous
between Gemini's answer and this script, then asks Gemini, per edge,
for the bearing/distance it associates, its evidence, confidence, and
an explicit self-report of whether it read the text or inferred it.

Two experiments:
  A. Single polygon (Lot 48 only, blue) -- 5 independent runs.
  B. Two polygons (Lot 48 blue, Lot 48-3 red) -- 5 independent runs --
     specifically to test whether Gemini assigns the shared-edge call
     322.74' to the correct owning parcel when given the choice.

No calibration thresholds, no OCR logic, no production code touched.
"""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw, ImageFont
from google import genai
from google.genai import types

from app.core.config import settings

DOC_ID = "3ea4cc01-4d39-472b-9465-a105306d63dc"
PAGE_PATH = Path(f"data/documents/{DOC_ID}/pages/page_013.png")
OUT_DIR = Path("scratch_diag/gemini_edge_experiment")
OUT_DIR.mkdir(exist_ok=True)

# Current padded ParcelMap crop for this region (same math confirm_boundary
# uses), vertices already converted into this crop's pixel frame in the
# earlier session (scratch_diag/confirm_lot48.json / confirm_lot48_3.json).
CROP_ORIGIN = (0.0, 0.0)
CROP_SIZE = (3059, 1837)

LOT48_VERTICES = [
    [1704.6162442957523, 1063.3833596877982], [1658.8656411201432, 1163.697320575635],
    [1145.0908881072703, 1053.9745067557385], [1145.8146373665345, 374.16419036110847],
    [1485.4675666648727, 353.1497864268346], [1519.2172936614857, 354.0619069822799],
    [1904.143151624319, 981.6263662347021],
]
LOT48_3_VERTICES = [
    [1142.0462871539373, 1052.199399617587], [1665.7975467124709, 1166.1935007745053],
    [1702.7683615097599, 1063.496074698282], [1903.0260362722258, 978.257127479637],
    [2401.7751732755296, 1832.1425989554443], [1124.2992597284692, 1829.997845784317],
]

KNOWN_CALLS = ["105.55", "11.63", "208.82", "322.74", "164.41", "32.24", "66.91"]


def make_crop() -> Image.Image:
    with Image.open(PAGE_PATH) as page:
        bx, by = CROP_ORIGIN
        cw, ch = CROP_SIZE
        return page.convert("RGB").crop((int(bx), int(by), int(bx + cw), int(by + ch)))


def draw_polygon(draw: ImageDraw.ImageDraw, verts, color, label_prefix=""):
    n = len(verts)
    pts = [(v[0], v[1]) for v in verts]
    for i in range(n):
        a = pts[i]
        b = pts[(i + 1) % n]
        draw.line([a, b], fill=color, width=6)
    for i, (x, y) in enumerate(pts):
        r = 14
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color, outline="white", width=2)
        draw.text((x + 16, y - 10), f"{label_prefix}{i}", fill=color)


def build_single_polygon_image() -> Path:
    crop = make_crop()
    draw = ImageDraw.Draw(crop)
    draw_polygon(draw, LOT48_VERTICES, "blue")
    out_path = OUT_DIR / "lot48_single.png"
    crop.save(out_path)
    return out_path


def build_two_polygon_image() -> Path:
    crop = make_crop()
    draw = ImageDraw.Draw(crop)
    draw_polygon(draw, LOT48_VERTICES, "blue", label_prefix="B")
    draw_polygon(draw, LOT48_3_VERTICES, "red", label_prefix="R")
    out_path = OUT_DIR / "lot48_and_lot48_3.png"
    crop.save(out_path)
    return out_path


SINGLE_SCHEMA = {
    "type": "object",
    "properties": {
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "edge_index": {"type": "integer"},
                    "bearing": {"type": "string"},
                    "distance": {"type": "string"},
                    "evidence_location": {"type": "string"},
                    "confidence": {"type": "number"},
                    "read_or_inferred": {"type": "string", "enum": ["read", "inferred", "uncertain"]},
                    "self_report_reasoning": {"type": "string"},
                },
                "required": [
                    "edge_index", "bearing", "distance", "evidence_location",
                    "confidence", "read_or_inferred", "self_report_reasoning",
                ],
            },
        }
    },
    "required": ["edges"],
}

TWO_POLY_SCHEMA = {
    "type": "object",
    "properties": {
        "shared_edge_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "distance_value": {"type": "string"},
                    "bearing": {"type": "string"},
                    "assigned_to": {"type": "string", "enum": ["blue (Lot 48)", "red (Lot 48-3)", "uncertain"]},
                    "evidence_location": {"type": "string"},
                    "confidence": {"type": "number"},
                    "reasoning": {"type": "string"},
                },
                "required": ["distance_value", "bearing", "assigned_to", "evidence_location", "confidence", "reasoning"],
            },
        }
    },
    "required": ["shared_edge_calls"],
}

SINGLE_PROMPT = """This is a cropped page from a real recorded property survey plat. A BLUE polygon has been drawn on top of the original drawing, tracing the confirmed boundary of a parcel called "Lot 48". Each vertex of the blue polygon is marked with a small numbered dot (0, 1, 2, ...). "Edge i" means the blue line segment connecting vertex i to vertex (i+1), wrapping around (the last vertex connects back to vertex 0).

The polygon has {n} edges (0 through {n_minus_1}).

For EACH edge, look at the printed survey annotations near that edge in the ORIGINAL drawing underneath the blue overlay, and tell me:
- Which printed bearing (e.g. "N 61°53'54" E") and distance (e.g. "105.55'") you believe corresponds to that specific edge, based on the printed text's position and orientation relative to that edge.
- Where exactly you see that evidence (describe its position on the page, e.g. "just above the midpoint of this edge", "to the right of vertex 3").
- Your confidence (0.0 to 1.0) that this bearing/distance genuinely belongs to THIS edge specifically (not just that it exists somewhere on the page).
- Whether you are directly READING printed characters you can see, or INFERRING/estimating the value because you can't actually read text there (e.g. from the polygon's approximate shape, other edges' calls, or the stated acreage) -- answer "read", "inferred", or "uncertain" and explain your reasoning for that self-assessment specifically.

Be honest about uncertainty. If you cannot find any legible printed text near an edge, say so explicitly rather than guessing a plausible-sounding number -- report read_or_inferred as "inferred" or "uncertain" in that case, and set confidence low.

Return one entry per edge (0 through {n_minus_1})."""

TWO_POLY_PROMPT = """This is a cropped page from a real recorded property survey plat. TWO polygons are drawn on top of the original drawing: a BLUE polygon (vertices labeled B0, B1, ...) tracing "Lot 48", and a RED polygon (vertices labeled R0, R1, ...) tracing the adjacent "Lot 48-3". These two lots share at least one physical boundary line.

Somewhere near where these two polygons meet or run close together, there are printed distance/bearing calls that could plausibly belong to either lot's own boundary. I'm specifically interested in the printed distance "322.74'" (and its associated bearing, likely near "S 59°41'15" E" or similar) -- but also flag any OTHER call you see near the shared area that could be ambiguous between the two lots.

For each such ambiguous/shared-area call you find, tell me:
- The exact bearing and distance text.
- Which polygon it actually belongs to: the BLUE polygon (Lot 48) or the RED polygon (Lot 48-3) -- base this on which polygon's own edge the printed line visually traces/runs along, NOT just which polygon's vertex dot happens to be closer to the text.
- Where you see this evidence on the page.
- Your confidence (0.0 to 1.0).
- Your reasoning for the assignment -- specifically, describe what visual evidence (which edge, which direction the line runs) led you to pick one polygon over the other."""


def call_gemini(client, image_path: Path, prompt: str, schema: dict):
    img = Image.open(image_path)
    buffer = io.BytesIO()
    img.convert("RGB").save(buffer, format="PNG")
    part = types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/png")
    response = client.models.generate_content(
        model=settings.GEMINI_MODEL,
        contents=[part, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )
    return response.text


def main():
    client = genai.Client(api_key=settings.GEMINI_API_KEY)

    single_path = build_single_polygon_image()
    two_path = build_two_polygon_image()
    print(f"Built {single_path} and {two_path}")

    n = len(LOT48_VERTICES)
    single_prompt = SINGLE_PROMPT.format(n=n, n_minus_1=n - 1)

    for run in range(1, 6):
        print(f"--- Single-polygon run {run}/5 ---")
        try:
            text = call_gemini(client, single_path, single_prompt, SINGLE_SCHEMA)
        except Exception as exc:  # noqa: BLE001
            text = json.dumps({"error": str(exc)})
        out = OUT_DIR / f"single_run_{run}.json"
        out.write_text(text, encoding="utf-8")
        print(f"  saved {out}")

    for run in range(1, 6):
        print(f"--- Two-polygon run {run}/5 ---")
        try:
            text = call_gemini(client, two_path, TWO_POLY_PROMPT, TWO_POLY_SCHEMA)
        except Exception as exc:  # noqa: BLE001
            text = json.dumps({"error": str(exc)})
        out = OUT_DIR / f"twopoly_run_{run}.json"
        out.write_text(text, encoding="utf-8")
        print(f"  saved {out}")

    print("Done.")


if __name__ == "__main__":
    main()
