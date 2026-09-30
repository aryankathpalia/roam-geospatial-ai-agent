"""
Diagnostic ONLY -- does not touch production code. Isolates the source
of run-to-run instability in the tile-read stage: same crop, same
combined-tile prompt (matching production's actual call shape, not the
per-tile isolation used in trace_debug.py), temperature=0, called 5x.

For each of 8 known values, records which run(s)/tile(s)/bboxes it
appeared in, plus whatever response metadata the Gemini SDK exposes,
to distinguish:
  1. tile vision extraction noise (same bbox, inconsistent read)
  2. prompt classification drift (read consistently, bucketed differently)
  3. tile coverage/resolution (value near a tile boundary)
  4. API-layer variability indistinguishable from #1 without external data
  5. structuring-stage inconsistency (not tested here -- tile-read only)
"""

import io
import json
import re
import sys

sys.path.insert(0, ".")

from PIL import Image
from google.genai import types

from app.services import vision

TARGET_VALUES = ["220.00", "352.40", "550.75", "1077.91", "2637.36", "325.30", "329.80", "336.41"]
N_RUNS = 5

img = Image.open("scratch_diag/crop_p9.png").convert("RGB")
width, height = img.size

tile_size = 700
cols = max(1, round(width / tile_size))
rows = max(1, round(height / tile_size))
tile_w, tile_h = width / cols, height / rows

tile_bboxes = []
for row in range(rows):
    for col in range(cols):
        x0, y0 = int(col * tile_w), int(row * tile_h)
        x1 = width if col == cols - 1 else int((col + 1) * tile_w)
        y1 = height if row == rows - 1 else int((row + 1) * tile_h)
        tile_bboxes.append((x0, y0, x1, y1))

tiles = [img.crop(b) for b in tile_bboxes]
print(f"Crop: {width}x{height}, {len(tiles)} tiles, bboxes: {tile_bboxes}\n")

client = vision._get_client()
read_prompt = vision._DOCUMENT_TILE_PROMPT_TEMPLATE.format(
    region_count=1, region_layout=f"Region 1: {len(tiles)} piece(s)"
)

parts = []
for tile_img in tiles:
    buf = io.BytesIO()
    tile_img.save(buf, format="PNG")
    parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))

run_texts = []
run_metadata = []

for run_idx in range(1, N_RUNS + 1):
    vision._wait_for_rate_limit()
    resp = client.models.generate_content(
        model=vision.settings.GEMINI_MODEL,
        contents=parts + [read_prompt],
        config=types.GenerateContentConfig(temperature=0),
    )
    text = resp.text.strip()
    run_texts.append(text)

    dump = resp.model_dump(exclude={"parsed"}) if hasattr(resp, "model_dump") else {}
    meta = {
        "model_version": dump.get("model_version"),
        "response_id": dump.get("response_id"),
        "usage_metadata": dump.get("usage_metadata"),
        "candidates_finish_reason": [
            c.get("finish_reason") for c in (dump.get("candidates") or [])
        ],
        "candidates_avg_logprobs": [
            c.get("avg_logprobs") for c in (dump.get("candidates") or [])
        ],
    }
    run_metadata.append(meta)

    with open(f"scratch_diag/trace_tiles/variance_run{run_idx}.txt", "w", encoding="utf-8") as f:
        f.write(text)
    with open(f"scratch_diag/trace_tiles/variance_run{run_idx}_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, default=str)

    print(f"=== RUN {run_idx} metadata ===")
    print(json.dumps(meta, indent=2, default=str))
    print()

# --- split each run's text into per-piece sections so we can attribute
# each occurrence to a tile/piece number ---
PIECE_RE = re.compile(r"Piece\s+(\d+)\s*:", re.IGNORECASE)


def split_by_piece(text: str) -> dict[int, str]:
    matches = list(PIECE_RE.finditer(text))
    sections = {}
    for i, m in enumerate(matches):
        piece_num = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.setdefault(piece_num, "")
        sections[piece_num] += text[start:end]
    return sections


run_sections = [split_by_piece(t) for t in run_texts]

print("=== PER-VALUE TRACE ACROSS 5 RUNS ===\n")
for v in TARGET_VALUES:
    print(f"--- {v} ---")
    seen_any = False
    for run_idx, sections in enumerate(run_sections, start=1):
        hits = [piece for piece, text in sections.items() if v in text]
        if hits:
            seen_any = True
            for piece in hits:
                bbox = tile_bboxes[piece - 1] if 1 <= piece <= len(tile_bboxes) else None
                print(f"  run {run_idx}: tile/piece {piece}  bbox={bbox}")
        else:
            print(f"  run {run_idx}: NOT FOUND")
    if not seen_any:
        print("  (never found in any run)")
    print()

print("=== RAW RUN TEXTS SAVED to scratch_diag/trace_tiles/variance_run{1..5}.txt ===")
