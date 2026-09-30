"""
Diagnostic ONLY. Tests whether (b) resolver under-splitting is
downstream of (a) batching-instability, or a separate bug: takes 3 of
the "(a) primary, went to zero" regions (65be453d PARCEL A, pages
6/7/8 -- confirmed saved crops from the earlier investigation), reads
each tile UNBATCHED (one tile per call, same method as the 1077.91
isolation test), runs each tile 2x to check consistency, then feeds
the resulting complete/consistent notes into the SAME structuring
prompt used in production to see if it now correctly reconstructs the
real parcel.

Does not modify production code or build any retry logic.
"""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from PIL import Image
from google.genai import types

from app.services import vision

OUT = Path("scratch_diag/zero_call_investigation")
TARGETS = [
    ("65be453d PARCEL A p6", OUT / "65be453d-f014-4024-84d6-60a2d0052aeb_region3_p6.png"),
    ("65be453d PARCEL A p7", OUT / "65be453d-f014-4024-84d6-60a2d0052aeb_region4_p7.png"),
    ("65be453d PARCEL A p8", OUT / "65be453d-f014-4024-84d6-60a2d0052aeb_region5_p8.png"),
]

client = vision._get_client()
N_REPEATS = 2

for label, crop_path in TARGETS:
    print(f"\n{'='*70}\n{label}  ({crop_path})\n{'='*70}")
    img = Image.open(crop_path).convert("RGB")
    tiles = vision._tile_image(img)
    print(f"{len(tiles)} tiles (with production overlap)")

    per_tile_prompt = vision._DOCUMENT_TILE_PROMPT_TEMPLATE.format(
        region_count=1, region_layout="Region 1: 1 piece(s)"
    )

    tile_run_texts = []  # [tile_idx] -> [run1_text, run2_text]
    for t_idx, tile in enumerate(tiles, start=1):
        buf = io.BytesIO()
        tile.save(buf, format="PNG")
        tile_bytes = buf.getvalue()

        runs = []
        for run_i in range(1, N_REPEATS + 1):
            vision._wait_for_rate_limit()
            part = types.Part.from_bytes(data=tile_bytes, mime_type="image/png")
            resp = client.models.generate_content(
                model=vision.settings.GEMINI_MODEL,
                contents=[part, per_tile_prompt],
                config=types.GenerateContentConfig(temperature=0),
            )
            text = resp.text.strip()
            runs.append(text)
        tile_run_texts.append(runs)

        same = runs[0] == runs[1]
        print(f"\n  --- tile {t_idx} (run1 == run2: {same}) ---")
        print(f"  run1: {runs[0][:400]}")
        if not same:
            print(f"  run2: {runs[1][:400]}")

    # Use run 1 of each tile as the "complete" merged notes (both runs
    # inspected above for consistency -- pick the longer/more complete
    # of the two per tile if they differ).
    merged_lines = []
    for t_idx, runs in enumerate(tile_run_texts, start=1):
        best = max(runs, key=len)
        merged_lines.append(f"Region 1, Piece {t_idx}:\n{best}")
    merged_notes = "\n\n".join(merged_lines)

    notes_path = OUT / f"unbatch_test_{label.replace(' ', '_')}_notes.txt"
    notes_path.write_text(merged_notes, encoding="utf-8")

    structure_prompt = vision._BATCH_STRUCTURE_PROMPT.format(
        region_count=1, region_list="Region 1", notes=merged_notes
    )
    vision._wait_for_rate_limit()
    struct_resp = client.models.generate_content(
        model=vision.settings.GEMINI_MODEL,
        contents=[structure_prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=vision._BATCH_RESPONSE_SCHEMA,
            temperature=0,
        ),
    )
    parsed = json.loads(struct_resp.text)
    print(f"\n  === STRUCTURING OUTPUT (given clean unbatched input) ===")
    print(json.dumps(parsed, indent=2))
