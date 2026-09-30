"""
Diagnostic ONLY. Isolates REGIONS-PER-CALL (holding tiles/region
roughly constant) as the variable, using the exact 5-region document
(65be453d) that originally produced the all-zero result when all 5
were batched together in one production-style call.

Conditions, 3 reruns each:
  1. 1 region/call  (5 separate calls per run)
  2. 3 regions/call (regions 1-3 together, region 4-5 together)
  3. 5 regions/call (all 5 together -- matches the original condition)

Completeness metric: same bearing-call-like regex count as the
tiles-per-call test, reported PER REGION so we can see where (if
anywhere) a cliff appears.
"""

import io
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

from PIL import Image
from google.genai import types

from app.services import vision

OUT = Path("scratch_diag/regions_per_call")
OUT.mkdir(parents=True, exist_ok=True)

REGION_FILES = [
    ("region1_QUANTUM_p2", "scratch_diag/zero_call_investigation/65be453d-f014-4024-84d6-60a2d0052aeb_region1_p2.png"),
    ("region2_LOT12_p3", "scratch_diag/zero_call_investigation/65be453d-f014-4024-84d6-60a2d0052aeb_region2_p3.png"),
    ("region3_PARCELA_p6", "scratch_diag/zero_call_investigation/65be453d-f014-4024-84d6-60a2d0052aeb_region3_p6.png"),
    ("region4_PARCELA_p7", "scratch_diag/zero_call_investigation/65be453d-f014-4024-84d6-60a2d0052aeb_region4_p7.png"),
    ("region5_PARCELA_p8", "scratch_diag/zero_call_investigation/65be453d-f014-4024-84d6-60a2d0052aeb_region5_p8.png"),
]

images = [Image.open(p).convert("RGB") for _, p in REGION_FILES]
tiles_per_region = [vision._tile_image(img) for img in images]
for (name, _), tiles in zip(REGION_FILES, tiles_per_region):
    print(f"{name}: {len(tiles)} tiles")

BEARING_RE = re.compile(r"[NSns]\s*\d{1,3}[°*ov°]\s*\d{1,2}", re.IGNORECASE)
N_RUNS = 3

client = vision._get_client()


def run_call(region_indices_and_tiles, run_label):
    """region_indices_and_tiles: list of (region_num_1based, tiles list)"""
    all_parts = []
    layout_lines = []
    for region_num, tiles in region_indices_and_tiles:
        layout_lines.append(f"Region {region_num}: {len(tiles)} piece(s)")
        for t in tiles:
            buf = io.BytesIO()
            t.save(buf, format="PNG")
            all_parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))

    prompt = vision._DOCUMENT_TILE_PROMPT_TEMPLATE.format(
        region_count=len(region_indices_and_tiles), region_layout="\n".join(layout_lines)
    )
    vision._wait_for_rate_limit()
    resp = client.models.generate_content(
        model=vision.settings.GEMINI_MODEL,
        contents=all_parts + [prompt],
        config=types.GenerateContentConfig(temperature=0),
    )
    text = resp.text.strip()
    (OUT / f"{run_label}.txt").write_text(text, encoding="utf-8")
    return text


def per_region_counts(text, region_nums):
    """Splits text by 'Region N' headers and counts bearing-like matches per region."""
    counts = {}
    for region_num in region_nums:
        # crude split: find this region's block up to the next "Region " mention
        pattern = re.compile(rf"Region {region_num}\b.*?(?=Region \d+\b|\Z)", re.DOTALL)
        matches = pattern.findall(text)
        block = "\n".join(matches)
        counts[region_num] = len(BEARING_RE.findall(block))
    return counts


results = {1: {}, 3: {}, 5: {}}  # regions_per_call -> region_num -> [counts per run]
for region_num in range(1, 6):
    results[1][region_num] = []
    results[3][region_num] = []
    results[5][region_num] = []

print(f"\n{'='*70}\nCONDITION: 1 region/call\n{'='*70}")
for run_i in range(1, N_RUNS + 1):
    for region_num, (name, _) in enumerate(REGION_FILES, start=1):
        tiles = tiles_per_region[region_num - 1]
        text = run_call([(region_num, tiles)], f"cond1_r{region_num}_run{run_i}")
        counts = per_region_counts(text, [region_num])
        results[1][region_num].append(counts[region_num])
        print(f"  run {run_i}, {name}: {counts[region_num]} matches")

print(f"\n{'='*70}\nCONDITION: 3 regions/call (1-3 together, 4-5 together)\n{'='*70}")
for run_i in range(1, N_RUNS + 1):
    text_a = run_call(
        [(1, tiles_per_region[0]), (2, tiles_per_region[1]), (3, tiles_per_region[2])],
        f"cond3_batchA_run{run_i}",
    )
    counts_a = per_region_counts(text_a, [1, 2, 3])
    text_b = run_call(
        [(4, tiles_per_region[3]), (5, tiles_per_region[4])],
        f"cond3_batchB_run{run_i}",
    )
    counts_b = per_region_counts(text_b, [4, 5])
    all_counts = {**counts_a, **counts_b}
    for region_num in range(1, 6):
        results[3][region_num].append(all_counts[region_num])
    print(f"  run {run_i}: {all_counts}")

print(f"\n{'='*70}\nCONDITION: 5 regions/call (matches original production condition)\n{'='*70}")
for run_i in range(1, N_RUNS + 1):
    text = run_call(
        [(i + 1, tiles_per_region[i]) for i in range(5)],
        f"cond5_run{run_i}",
    )
    counts = per_region_counts(text, [1, 2, 3, 4, 5])
    for region_num in range(1, 6):
        results[5][region_num].append(counts[region_num])
    print(f"  run {run_i}: {counts}")

print(f"\n{'='*70}\nSUMMARY (avg bearing-like matches per region, by regions/call)\n{'='*70}")
names = [n for n, _ in REGION_FILES]
print(f"{'region':<25} {'1/call':>10} {'3/call':>10} {'5/call':>10}")
for region_num in range(1, 6):
    avg1 = sum(results[1][region_num]) / len(results[1][region_num])
    avg3 = sum(results[3][region_num]) / len(results[3][region_num])
    avg5 = sum(results[5][region_num]) / len(results[5][region_num])
    print(f"{names[region_num-1]:<25} {avg1:>10.1f} {avg3:>10.1f} {avg5:>10.1f}")

with open("scratch_diag/regions_per_call_results.json", "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)
