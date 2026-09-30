"""
Diagnostic ONLY. Measures tile-read completeness at batch sizes
1, 2, 3 tiles/call (6 tiles/call data already exists from earlier
production-batched tests) across 4 real low-completeness regions.

Completeness proxy: count of bearing-call-like matches (a NSEW letter
followed by a degree-ish number) found in that run's combined raw
text -- a generic, automatable stand-in for "how much real boundary
content survived," since these 4 regions don't share one fixed
target-value list the way the NVZ 1077.91 test did.

Reduced from 5 to 2 reruns per (region, batch_size) cell to keep this
tractable given 4 regions x 3 batch sizes x multiple calls/run --
flagged explicitly, not silently.
"""

import io
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from PIL import Image
from google.genai import types

from app.services import vision

OUT = Path("scratch_diag/batch_curve")
OUT.mkdir(parents=True, exist_ok=True)

TARGETS = [
    ("65be453d_p6", "scratch_diag/zero_call_investigation/65be453d-f014-4024-84d6-60a2d0052aeb_region3_p6.png"),
    ("65be453d_p7", "scratch_diag/zero_call_investigation/65be453d-f014-4024-84d6-60a2d0052aeb_region4_p7.png"),
    ("65be453d_p8", "scratch_diag/zero_call_investigation/65be453d-f014-4024-84d6-60a2d0052aeb_region5_p8.png"),
    ("2f896c95_p6", "scratch_diag/zero_call_investigation/2f896c95_region1_p6.png"),
    ("42f617a1_LOT16B2_region", "scratch_diag/zero_call_investigation/42f617a1-1d02-4f26-bf05-2a17b23f8fda_region2_p1.png"),
]

BATCH_SIZES = [1, 2, 3]
N_RUNS = 2

BEARING_RE = re.compile(r"[NSns]\s*\d{1,3}[°*ov°]\s*\d{1,2}", re.IGNORECASE)

client = vision._get_client()

results = {}  # region -> batch_size -> [count_per_run]

for region_name, crop_path in TARGETS:
    img = Image.open(crop_path).convert("RGB")
    tiles = vision._tile_image(img)
    print(f"\n{'='*70}\n{region_name}: {len(tiles)} tiles total\n{'='*70}")
    results[region_name] = {}

    for batch_size in BATCH_SIZES:
        chunks = [tiles[i:i + batch_size] for i in range(0, len(tiles), batch_size)]
        run_counts = []

        for run_i in range(1, N_RUNS + 1):
            combined_text = ""
            for chunk_idx, chunk in enumerate(chunks, start=1):
                parts = []
                for t in chunk:
                    buf = io.BytesIO()
                    t.save(buf, format="PNG")
                    parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))
                prompt = vision._DOCUMENT_TILE_PROMPT_TEMPLATE.format(
                    region_count=1, region_layout=f"Region 1: {len(chunk)} piece(s)"
                )
                vision._wait_for_rate_limit()
                try:
                    resp = client.models.generate_content(
                        model=vision.settings.GEMINI_MODEL,
                        contents=parts + [prompt],
                        config=types.GenerateContentConfig(temperature=0),
                    )
                    combined_text += resp.text.strip() + "\n"
                except Exception as e:
                    print(f"    [error on chunk {chunk_idx}]: {e}")
                    time.sleep(5)

            bearing_count = len(BEARING_RE.findall(combined_text))
            run_counts.append(bearing_count)
            out_file = OUT / f"{region_name}_batch{batch_size}_run{run_i}.txt"
            out_file.write_text(combined_text, encoding="utf-8")
            print(f"  batch_size={batch_size} run={run_i}: {len(chunks)} calls, "
                  f"bearing-like matches found = {bearing_count}")

        results[region_name][batch_size] = run_counts

print(f"\n{'='*70}\nSUMMARY (bearing-call-like match count per run)\n{'='*70}")
for region_name, by_batch in results.items():
    print(f"\n{region_name}:")
    for batch_size, counts in by_batch.items():
        avg = sum(counts) / len(counts) if counts else 0
        print(f"  batch_size={batch_size}: runs={counts}  avg={avg:.1f}")

with open("scratch_diag/batch_curve_results.json", "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)
