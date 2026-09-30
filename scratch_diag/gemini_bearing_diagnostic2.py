"""
DIAGNOSTIC ONLY -- not production code.

Follow-up to gemini_edge_association_experiment.py. Two parts:

A. "Full sheet context" test. This sheet (MAP 7 LOTS 48 & 48-3) only has
   TWO parcels with an actual drawn/dimensioned boundary in our data --
   a third referenced property ("MAP 7 LOT 47") is a plain adjoiner
   label with no traced polygon, so "full sheet" == the same two
   polygons already used in the prior 2-polygon ownership test. What's
   NEW here: asking for Lot 48's COMPLETE edge-by-edge bearing list
   (not just the one shared ambiguous call) with BOTH polygons visible,
   to see whether having Lot 48-3 in view stabilizes bearings broadly
   or only fixed the one edge it was specifically asked about before.

B. Targeted OCR-only verification. For the 3 calls whose BEARING was
   unstable across the 5 single-polygon runs (208.82/true 209.92,
   322.74, 32.24), crop tightly around just that label (using the pixel
   location already identified by direct visual inspection this
   session) and run PaddleOCR alone -- no Gemini -- to see whether the
   bearing is actually legible in isolation.

No production code, thresholds, or calibration touched.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import io
from PIL import Image, ImageDraw
from google import genai
from google.genai import types

from app.core.config import settings
from app.services.ocr import run_parcelmap_ocr

DOC_ID = "3ea4cc01-4d39-472b-9465-a105306d63dc"
PAGE_PATH = Path(f"data/documents/{DOC_ID}/pages/page_013.png")
OUT_DIR = Path("scratch_diag/gemini_edge_experiment")
OUT_DIR.mkdir(exist_ok=True)

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


def make_crop() -> Image.Image:
    with Image.open(PAGE_PATH) as page:
        cw, ch = CROP_SIZE
        return page.convert("RGB").crop((0, 0, cw, ch))


def draw_polygon(draw, verts, color, label_prefix=""):
    n = len(verts)
    pts = [(v[0], v[1]) for v in verts]
    for i in range(n):
        draw.line([pts[i], pts[(i + 1) % n]], fill=color, width=6)
    for i, (x, y) in enumerate(pts):
        r = 14
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color, outline="white", width=2)
        draw.text((x + 16, y - 10), f"{label_prefix}{i}", fill=color)


FULL_EDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "lot48_edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "edge_index": {"type": "integer"},
                    "bearing": {"type": "string"},
                    "distance": {"type": "string"},
                    "confidence": {"type": "number"},
                    "read_or_inferred": {"type": "string", "enum": ["read", "inferred", "uncertain"]},
                },
                "required": ["edge_index", "bearing", "distance", "confidence", "read_or_inferred"],
            },
        }
    },
    "required": ["lot48_edges"],
}

FULL_EDGE_PROMPT = """This is a cropped page from a real recorded property survey plat. TWO polygons are drawn on it: a BLUE polygon (vertices B0-B6) tracing "Lot 48", and a RED polygon (vertices R0-R5) tracing the adjacent "Lot 48-3". They share at least one boundary line.

I want the COMPLETE bearing and distance for EVERY edge of the BLUE polygon (Lot 48) only -- edges B0-B1, B1-B2, B2-B3, B3-B4, B4-B5, B5-B6, B6-B0 (7 edges total). Use the RED polygon only as context to help you correctly attribute each printed call to the right lot where lines are close together or shared.

For each of Lot 48's 7 edges, give the bearing, distance, your confidence (0-1), and whether you are reading printed text directly or inferring/estimating it."""


def call_gemini_json(client, image: Image.Image, prompt: str, schema: dict) -> str:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    part = types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/png")
    response = client.models.generate_content(
        model=settings.GEMINI_MODEL,
        contents=[part, prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema),
    )
    return response.text


def part_a():
    crop = make_crop()
    draw = ImageDraw.Draw(crop)
    draw_polygon(draw, LOT48_VERTICES, "blue", "B")
    draw_polygon(draw, LOT48_3_VERTICES, "red", "R")
    img_path = OUT_DIR / "full_context_two_poly.png"
    crop.save(img_path)

    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    for run in range(1, 6):
        print(f"--- Part A run {run}/5 ---")
        try:
            text = call_gemini_json(client, crop, FULL_EDGE_PROMPT, FULL_EDGE_SCHEMA)
        except Exception as exc:  # noqa: BLE001
            text = json.dumps({"error": str(exc)})
        out = OUT_DIR / f"fullctx_run_{run}.json"
        out.write_text(text, encoding="utf-8")
        print(f"  saved {out}")


# Tight crops around each unstable-bearing label, located and VISUALLY
# VERIFIED by direct inspection this session (page-absolute pixel coords,
# generous margin around the text).
TARGET_CROPS = {
    "208.82_true_209.92": (1040, 540, 1220, 780),    # "N 24'11'47" W  209.92'" label
    "322.74": (1650, 700, 1950, 900),                # "S 59'41'15" E  322.74'" label
    "32.24": (1650, 1060, 1820, 1200),               # "S 0?'23'08" E  32.24'" label
    "184.66_true_164.66_bonus": (1030, 1080, 1150, 1420),  # Lot 48-3's own call -- bonus check,
    # my own eye also read this as 164.66' (not the 184.66' our pipeline stored) --
    # independent OCR read here is useful cross-validation, not required by the task.
}


def part_b():
    with Image.open(PAGE_PATH) as page:
        page = page.convert("RGB")
        for name, box in TARGET_CROPS.items():
            crop = page.crop(box)
            up = crop.resize((crop.width * 3, crop.height * 3), Image.LANCZOS)
            crop_path = OUT_DIR / f"tight_{name}.png"
            up.save(crop_path)
            print(f"--- OCR-only tight crop: {name} ({crop_path}) ---")
            lines, _ = run_parcelmap_ocr(up)
            if not lines:
                print("  NO TEXT DETECTED AT ALL")
            for line in lines:
                safe = line.text.encode("ascii", "replace").decode()
                print(f"  {safe!r}  bbox={line.bbox}")


if __name__ == "__main__":
    print("=== PART A: full-context (both polygons) complete Lot 48 edge list, 5 runs ===")
    part_a()
    print()
    print("=== PART B: targeted PaddleOCR-only bearing verification ===")
    part_b()
