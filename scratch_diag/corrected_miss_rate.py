"""
Diagnostic ONLY. Redoes the region3_p6 / region5_p8 8x miss-rate
measurement using the CORRECT metric: complete calls surviving
structuring + resolve_ambiguous_calls + drop_conflicting_axis_duplicates
(both bearing AND distance present), not raw regex bearing-fragment
matches -- which the previous end-to-end test showed overstated
completeness substantially.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

from google.genai import types

from app.services import vision
from app.services.geometry import resolve_ambiguous_calls, drop_conflicting_axis_duplicates

RUN_DIR = Path("scratch_diag/regions_per_call")
client = vision._get_client()


def extract_region_block(full_text: str, region_num: int) -> str:
    pattern = re.compile(rf"Region {region_num}\b.*?(?=Region \d+\b|\Z)", re.DOTALL)
    return "\n".join(pattern.findall(full_text))


def count_real_calls(notes_text: str) -> int:
    if not notes_text.strip():
        return 0
    prompt = vision._BATCH_STRUCTURE_PROMPT.format(
        region_count=1, region_list="Region 1", notes=notes_text
    )
    vision._wait_for_rate_limit()
    resp = client.models.generate_content(
        model=vision.settings.GEMINI_MODEL,
        contents=[prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=vision._BATCH_RESPONSE_SCHEMA,
            temperature=0,
        ),
    )
    parsed = json.loads(resp.text)
    parcels = parsed.get("parcels", [])
    if not parcels:
        return 0
    geometry = parcels[0]
    calls = geometry.get("boundary_calls") or []
    alternates = geometry.get("ambiguous_alternates") or []
    resolved = calls
    if calls and alternates:
        resolved = resolve_ambiguous_calls(calls, alternates)
    if resolved:
        resolved = drop_conflicting_axis_duplicates(resolved)
    return len(resolved)


results = {3: [], 5: []}

for region_num in [3, 5]:
    print(f"\n{'='*70}\nregion {region_num}\n{'='*70}")
    for run_i in range(1, 9):
        full_text = (RUN_DIR / f"merge_test_run{run_i}.txt").read_text(encoding="utf-8")
        block = extract_region_block(full_text, region_num)
        count = count_real_calls(block)
        results[region_num].append(count)
        print(f"  run {run_i}: {count} real (post-structuring) calls")

print(f"\n{'='*70}\nCORRECTED SUMMARY\n{'='*70}")
for region_num, counts in results.items():
    zero_runs = sum(1 for c in counts if c == 0)
    print(f"\nregion {region_num}:")
    print(f"  per-run counts: {counts}")
    print(f"  zero-match runs: {zero_runs}/8 ({zero_runs/8:.0%})")

with open("scratch_diag/corrected_miss_rate_results.json", "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)
