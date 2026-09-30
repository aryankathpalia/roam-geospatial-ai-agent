"""
Diagnostic ONLY. Reruns the 5-regions/call condition (65be453d, all 5
ParcelMap regions from one document, one call) 8x, capturing the
actual distinct bearing-call-like strings per region per run (not
just counts), to compute:
  1. per-run miss/hit pattern per region (all-or-nothing vs partial)
  2. miss rate per region
  3. whether the UNION of distinct values across runs recovers more
     than any single best run -- the actual question for merge vs
     retry-and-pick-best.
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

# Captures the full bearing+distance string, not just the bearing
# prefix, so we can dedupe/union real distinct values.
CALL_RE = re.compile(
    r"[NSns]\s*\d{1,3}[°*ov°]\s*\d{1,2}['′]?\s*\d{0,2}[\"″]?\s*[EWew][^\n,;]{0,30}",
)

N_RUNS = 8
client = vision._get_client()


def run_call(run_label):
    all_parts = []
    layout_lines = []
    for i, ((name, _), tiles) in enumerate(zip(REGION_FILES, tiles_per_region), start=1):
        layout_lines.append(f"Region {i}: {len(tiles)} piece(s)")
        for t in tiles:
            buf = io.BytesIO()
            t.save(buf, format="PNG")
            all_parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))

    prompt = vision._DOCUMENT_TILE_PROMPT_TEMPLATE.format(
        region_count=5, region_layout="\n".join(layout_lines)
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


def per_region_values(text, region_nums):
    values = {}
    for region_num in region_nums:
        pattern = re.compile(rf"Region {region_num}\b.*?(?=Region \d+\b|\Z)", re.DOTALL)
        block = "\n".join(pattern.findall(text))
        found = [m.strip() for m in CALL_RE.findall(block)]
        values[region_num] = found
    return values


per_run_values = {3: [], 4: [], 5: []}  # region_num -> list of (run_idx, [values])

for run_i in range(1, N_RUNS + 1):
    text = run_call(f"merge_test_run{run_i}")
    values = per_region_values(text, [3, 4, 5])
    for region_num in [3, 4, 5]:
        per_run_values[region_num].append(values[region_num])
    print(f"run {run_i}: " + " | ".join(f"R{r}={len(values[r])}" for r in [3, 4, 5]))

print(f"\n{'='*70}\nPER-RUN DETAIL\n{'='*70}")
for region_num in [3, 4, 5]:
    print(f"\n--- region {region_num} ---")
    for run_i, vals in enumerate(per_run_values[region_num], start=1):
        print(f"  run {run_i} ({len(vals)} values): {vals}")

print(f"\n{'='*70}\nMISS RATE + UNION ANALYSIS\n{'='*70}")
summary = {}
for region_num in [3, 4, 5]:
    counts = [len(v) for v in per_run_values[region_num]]
    zero_runs = sum(1 for c in counts if c == 0)
    below2_runs = sum(1 for c in counts if c < 2)
    best_single_run = max(counts) if counts else 0

    # crude normalization for union: strip whitespace/case for dedup
    def norm(s):
        return re.sub(r"\s+", "", s).upper()

    union_set = set()
    for vals in per_run_values[region_num]:
        for v in vals:
            union_set.add(norm(v))

    print(f"\nregion {region_num}:")
    print(f"  per-run counts: {counts}")
    print(f"  zero-match runs: {zero_runs}/{N_RUNS} ({zero_runs/N_RUNS:.0%})")
    print(f"  below-2-matches runs: {below2_runs}/{N_RUNS} ({below2_runs/N_RUNS:.0%})")
    print(f"  best single run: {best_single_run} values")
    print(f"  union across all {N_RUNS} runs (deduped): {len(union_set)} distinct values")
    print(f"  union > best single run: {len(union_set) > best_single_run}")

    summary[region_num] = {
        "counts": counts,
        "zero_rate": zero_runs / N_RUNS,
        "below2_rate": below2_runs / N_RUNS,
        "best_single_run": best_single_run,
        "union_count": len(union_set),
        "union_beats_best_single": len(union_set) > best_single_run,
    }

with open("scratch_diag/merge_test_results.json", "w", encoding="utf-8") as f:
    json.dump(summary, f, indent=2)
