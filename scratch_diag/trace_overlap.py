"""
Diagnostic ONLY -- does not modify app/services/vision.py. Tests
whether adding tile overlap fixes the confirmed 352.40'/550.75'
boundary-split, and re-measures 1077.91's BATCHED presence rate with
overlap added, to separate "some of the 20% batched-miss rate was
actually a tiling artifact" from "it's purely a batching effect."
"""

import io
import json
import sys

sys.path.insert(0, ".")

from PIL import Image
from google.genai import types

from app.services import vision

OVERLAP_PX = 50


def tile_image_with_overlap(image: Image.Image, tile_size: int = 700, overlap: int = OVERLAP_PX):
    """Same grid as vision._tile_image, but each tile is padded by
    `overlap` px on every internal edge (not the crop's own outer
    edges), so a label sitting on a seam appears whole on both
    neighboring tiles instead of being split."""
    width, height = image.size
    cols = max(1, round(width / tile_size))
    rows = max(1, round(height / tile_size))
    tile_w, tile_h = width / cols, height / rows

    tiles = []
    bboxes = []
    for row in range(rows):
        for col in range(cols):
            x0 = int(col * tile_w)
            y0 = int(row * tile_h)
            x1 = width if col == cols - 1 else int((col + 1) * tile_w)
            y1 = height if row == rows - 1 else int((row + 1) * tile_h)

            ox0 = max(0, x0 - overlap)
            oy0 = max(0, y0 - overlap)
            ox1 = min(width, x1 + overlap)
            oy1 = min(height, y1 + overlap)

            tiles.append(image.crop((ox0, oy0, ox1, oy1)))
            bboxes.append((ox0, oy0, ox1, oy1))
    return tiles, bboxes


img = Image.open("scratch_diag/crop_p9.png").convert("RGB")
tiles, bboxes = tile_image_with_overlap(img)
print(f"Overlap={OVERLAP_PX}px. Tile bboxes: {bboxes}\n")
for i, t in enumerate(tiles, start=1):
    t.save(f"scratch_diag/trace_tiles/overlap_tile_{i}.png")

client = vision._get_client()
read_prompt = vision._DOCUMENT_TILE_PROMPT_TEMPLATE.format(
    region_count=1, region_layout=f"Region 1: {len(tiles)} piece(s)"
)

parts = []
for t in tiles:
    buf = io.BytesIO()
    t.save(buf, format="PNG")
    parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))

TARGETS = ["352.40", "550.75", "1077.9"]
N_RUNS = 5

results = []
for run_idx in range(1, N_RUNS + 1):
    vision._wait_for_rate_limit()
    resp = client.models.generate_content(
        model=vision.settings.GEMINI_MODEL,
        contents=parts + [read_prompt],
        config=types.GenerateContentConfig(temperature=0),
    )
    text = resp.text.strip()
    with open(f"scratch_diag/trace_tiles/overlap_batched_run{run_idx}.txt", "w", encoding="utf-8") as f:
        f.write(text)
    hits = {v: (v in text) for v in TARGETS}
    results.append(hits)
    print(f"run {run_idx}: {hits}")

print("\n=== SUMMARY (batched, WITH overlap) ===")
for v in TARGETS:
    count = sum(1 for r in results if r[v])
    print(f"  {v}: {count}/{N_RUNS}")
