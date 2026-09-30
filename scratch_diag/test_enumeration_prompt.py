"""
Diagnostic ONLY -- does not modify app/services/vision.py. Tests a
revised structuring prompt that forces explicit parcel-label
enumeration BEFORE extraction (a new top-level `parcel_labels_found`
field in the response schema), against:
  1. 65be453d p6 with INTACT labels (the clean merge, not the
     corrupted one from the earlier test)
  2. 65be453d p7 (already had intact labels)
  3. NVZ's existing 2-parcel notes (production's known-good case --
     checking for regression)
Each run 5x, temperature=0, to see how often the full correct parcel
count is produced.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from google.genai import types

from app.services import vision

client = vision._get_client()

# Revised prompt: same as production's _BATCH_STRUCTURE_PROMPT but with
# an explicit forced-enumeration step inserted before extraction.
TEST_STRUCTURE_PROMPT = """\
Below are raw notes read off pieces of {region_count} different survey
drawings ({region_list}), from one document.

STEP 1 -- MANDATORY, BEFORE ANYTHING ELSE: For each region, scan its
notes and list EVERY DISTINCT REAL-WORLD parcel you find, in the
`parcel_labels_found` field, grouped by region_index. A label counts
even if it only has a legal description and no boundary calls yet --
list it anyway. Do this scan BEFORE you extract any boundary calls.
Do not skip a label because it looks minor or you're not sure it has
its own boundary data -- list it, then decide in Step 2 whether it
has enough to also get a `parcels` entry.

CRITICAL -- DEDUPLICATE BEFORE LISTING: the SAME physical parcel is
often mentioned more than once across different tile notes, in
DIFFERENT formatting -- different capitalization, quoting, or
punctuation around the exact same identity (e.g. "PARCEL B",
"Parcel 'B'", "Parcel B", "PARCEL 'B'" are ALL THE SAME PARCEL, not
four different ones). Before adding a label to `parcel_labels_found`,
check whether you've already listed a label that plausibly refers to
the same real-world parcel -- same letter/number/name, regardless of
case, quotes, or apostrophes -- and if so, do NOT add it again. Judge
by the underlying identity (the letter or number after "PARCEL"/
"LOT"), not the surface text. When in doubt whether two mentions are
the same parcel or genuinely two different ones, prefer treating them
as the SAME parcel (undercounting a truly-distinct parcel is rarer
and less harmful here than inventing duplicates of one real parcel).

STEP 2: For each region, produce EXACTLY ONE ENTRY in `parcels` for
EVERY LABEL YOU LISTED IN STEP 1 for that region -- not fewer. If you
listed 3 labels for a region, `parcels` MUST contain 3 entries for
that region_index, even if one of them ends up with an empty
boundary_calls array because you couldn't find its dimensions. Do not
silently merge two labels into one entry, and do not omit a listed
label from `parcels` for any reason -- if it has no usable boundary
calls, include it anyway with an empty boundary_calls list.

IMPORTANT -- a single region can show MORE THAN ONE parcel. Survey
exhibits routinely draw two or more adjacent parcels on one sheet (a
"Parcel Map Exhibit" creating "PARCEL 1" and "PARCEL 2" side by side is
common), and a region's bounding box wraps the whole sheet, not one
parcel.
1. Two entries from the same region share the same region_index but
   have different parcel_label values and, critically, DISJOINT
   boundary_calls -- a call belongs to exactly one parcel's traverse,
   never both, even if two parcels share a common edge (assign a
   shared edge's call to whichever parcel's label it's written closest
   to / associated with in the notes, not to both).
2. Only extract parcels that THIS drawing is itself defining the
   boundary of. Do NOT extract a "parcel" whose label only ever
   appears as a NEIGHBOR/CONTEXT citation naming an adjacent property.
3. If this parcel's own stated acreage is given anywhere in the notes,
   put it in stated_area_acres. Use null if not given.
4. When a shared edge shows more than one plausible dimension for it,
   use the SHORTER one that stays within THIS parcel's own corners.
5. If you find MORE THAN ONE candidate distance for what looks like
   the same physical line, do NOT pick one and discard the rest.
   Include every distinct candidate in `ambiguous_alternates` on that
   parcel (put your best single guess in boundary_calls, the rest in
   ambiguous_alternates).

For boundary_calls specifically: only include an entry if it has BOTH
a bearing AND a distance stated together as a single call on THAT
parcel's own OUTER boundary line. Do NOT include a bare dimension
number, a value in PARENTHESES (prior-record citation), a road-
frontage/quitclaim measurement, or a tie line to an outside monument/
control point/section corner. Leave a call out entirely rather than
guessing which parcel it belongs to.

Put each parcel's boundary_calls in true walking sequence around ITS
OWN perimeter, inferred from positional cues in the notes.

Tag every entry with its region_index matching the region numbers
above.

NOTES:
{notes}
"""

TEST_SCHEMA = {
    "type": "object",
    "properties": {
        "parcel_labels_found": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "region_index": {"type": "integer"},
                    "label": {"type": "string"},
                },
                "required": ["region_index", "label"],
            },
        },
        "parcels": vision._BATCH_RESPONSE_SCHEMA,
    },
    "required": ["parcel_labels_found", "parcels"],
}

TESTS = [
    ("65be453d p6 (INTACT labels)", "scratch_diag/zero_call_investigation/p6_intact_labels_notes.txt", 3, 10),
    ("65be453d p7", "scratch_diag/zero_call_investigation/unbatch_test_65be453d_PARCEL_A_p7_notes.txt", 3, 10),
    ("NVZ (production known-good)", "scratch_diag/trace_tiles/variance_run1.txt", 2, 5),
]
results_summary = {}

for label, notes_path, expected_count, n_runs in TESTS:
    notes = Path(notes_path).read_text(encoding="utf-8")
    print(f"\n{'='*70}\n{label} (expected parcel count: {expected_count}, {n_runs} runs)\n{'='*70}")

    correct_count_runs = 0
    total_calls_across_runs = 0
    for run_i in range(1, n_runs + 1):
        prompt = TEST_STRUCTURE_PROMPT.format(region_count=1, region_list="Region 1", notes=notes)
        vision._wait_for_rate_limit()
        resp = client.models.generate_content(
            model=vision.settings.GEMINI_MODEL,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=TEST_SCHEMA,
                temperature=0,
            ),
        )
        parsed = json.loads(resp.text)
        labels_found = parsed.get("parcel_labels_found", [])
        parcels = parsed.get("parcels", [])
        num_calls_per = [len(p.get("boundary_calls", [])) for p in parcels]
        is_correct = len(parcels) == expected_count

        if is_correct:
            correct_count_runs += 1
        total_calls_across_runs += sum(num_calls_per)

        print(f"  run {run_i}: labels_found={len(labels_found)} {[l['label'] for l in labels_found]} "
              f"| parcels_returned={len(parcels)} | calls_per_parcel={num_calls_per} "
              f"| CORRECT_COUNT={is_correct}")

    results_summary[label] = f"{correct_count_runs}/{n_runs} correct count | total calls across all runs: {total_calls_across_runs}"

print(f"\n{'='*70}\nSUMMARY\n{'='*70}")
for label, result in results_summary.items():
    print(f"  {label}: {result} runs got the correct parcel count")
