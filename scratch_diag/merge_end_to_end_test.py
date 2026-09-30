"""
Diagnostic ONLY. For region 3 (p6) and region 5 (p8) -- the two
regions with a dead (zero-match) run in the 8x test -- concatenates
raw tile-read notes from a zero-run + 2 non-zero runs, feeds the
COMBINED notes through the real, unmodified production structuring
prompt + resolve_ambiguous_calls + drop_conflicting_axis_duplicates,
and compares against (a) the best single run alone and (b) the
zero/worst run alone, on the metric that actually matters: walked
area vs stated acreage, not just call count or closure.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

from google.genai import types

from app.services import vision
from app.services.geometry import (
    resolve_ambiguous_calls,
    drop_conflicting_axis_duplicates,
    walk_traverse,
)
from app.services.spatial_validation import validate_traverse

RUN_DIR = Path("scratch_diag/regions_per_call")
client = vision._get_client()


def extract_region_block(full_text: str, region_num: int) -> str:
    pattern = re.compile(rf"Region {region_num}\b.*?(?=Region \d+\b|\Z)", re.DOTALL)
    return "\n".join(pattern.findall(full_text))


def structure_and_resolve(notes_text: str, label: str) -> dict:
    if not notes_text.strip():
        return {"label": label, "empty_input": True}

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
        return {"label": label, "no_parcels": True, "raw": parsed}

    # Use the first/only parcel entry (single-region test)
    geometry = parcels[0]
    calls = geometry.get("boundary_calls") or []
    alternates = geometry.get("ambiguous_alternates") or []

    resolved = calls
    picked_alt = False
    if calls and alternates:
        resolved = resolve_ambiguous_calls(calls, alternates)
        picked_alt = resolved != calls
    if resolved:
        resolved = drop_conflicting_axis_duplicates(resolved)

    result = {
        "label": label,
        "parcel_label": geometry.get("parcel_label"),
        "stated_area_acres": geometry.get("stated_area_acres"),
        "raw_calls": calls,
        "ambiguous_alternates": alternates,
        "resolved_calls": resolved,
        "picked_alternate_over_original": picked_alt,
    }
    if resolved:
        traverse = walk_traverse(resolved)
        validation = validate_traverse(
            traverse, "", stated_area_acres=geometry.get("stated_area_acres")
        )
        result["validation"] = validation
    return result


TARGETS = [
    {
        "region_label": "region3_p6",
        "region_num": 3,
        "zero_run": 8,
        "nonzero_runs": [1, 3],  # best (13) + a mid one (6)
        "best_single_run": 1,
    },
    {
        "region_label": "region5_p8",
        "region_num": 5,
        "zero_run": 5,
        "nonzero_runs": [7, 1],  # 6 values + 5 values
        "best_single_run": 7,
    },
]

all_results = {}

for target in TARGETS:
    region_label = target["region_label"]
    region_num = target["region_num"]
    print(f"\n{'='*70}\n{region_label}\n{'='*70}")

    run_texts = {}
    for run_i in [target["zero_run"]] + target["nonzero_runs"]:
        full_text = (RUN_DIR / f"merge_test_run{run_i}.txt").read_text(encoding="utf-8")
        run_texts[run_i] = extract_region_block(full_text, region_num)

    # (a) best single run alone
    best_run = target["best_single_run"]
    best_notes = f"Region 1, Piece 1:\n{run_texts[best_run]}"
    result_best = structure_and_resolve(best_notes, f"{region_label}_best_single_run{best_run}")

    # (b) zero/worst run alone
    zero_run = target["zero_run"]
    zero_notes = f"Region 1, Piece 1:\n{run_texts[zero_run]}"
    result_zero = structure_and_resolve(zero_notes, f"{region_label}_zero_run{zero_run}")

    # merged: zero run + 2 nonzero runs, concatenated as separate pieces
    merged_lines = []
    for i, run_i in enumerate([zero_run] + target["nonzero_runs"], start=1):
        merged_lines.append(f"Region 1, Piece {i}:\n{run_texts[run_i]}")
    merged_notes = "\n\n".join(merged_lines)
    result_merged = structure_and_resolve(merged_notes, f"{region_label}_merged")

    for r in [result_best, result_zero, result_merged]:
        print(f"\n--- {r['label']} ---")
        print(json.dumps(r, indent=2, default=str))

    all_results[region_label] = {
        "best_single": result_best,
        "zero_run": result_zero,
        "merged": result_merged,
    }

with open("scratch_diag/merge_end_to_end_results.json", "w", encoding="utf-8") as f:
    json.dump(all_results, f, indent=2, default=str)

print(f"\n{'='*70}\nSUMMARY TABLE\n{'='*70}")
for region_label, res in all_results.items():
    print(f"\n{region_label}:")
    for key in ["best_single", "zero_run", "merged"]:
        r = res[key]
        v = r.get("validation", {})
        print(f"  {key}: calls_used={len(r.get('resolved_calls', []) or [])} "
              f"stated_acres={r.get('stated_area_acres')} "
              f"walked_area_acres={v.get('area_acres')} "
              f"area_matches_stated={v.get('area_matches_stated')} "
              f"precision=1:{v.get('precision_ratio')} "
              f"valid={v.get('valid')} "
              f"picked_alt_over_original={r.get('picked_alternate_over_original')}")
