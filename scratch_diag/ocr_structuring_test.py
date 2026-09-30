"""
Diagnostic ONLY. Tests whether feeding OCR text (deterministic,
position-grounded) into the EXISTING structuring prompt reconstructs
correct boundary calls -- the actual open question before wiring OCR
into the extraction pipeline. Does not modify production code.

Notes format mirrors what the tile-read stage currently produces (a
flat list of detected text, reading-order sorted) so the SAME
_BATCH_STRUCTURE_PROMPT can be reused unchanged.
"""

import json
import sys

sys.path.insert(0, ".")

from google.genai import types
from PIL import Image

from app.services import vision
from app.services.ocr import run_parcelmap_ocr
from app.services.geometry import (
    drop_conflicting_axis_duplicates,
    resolve_ambiguous_calls,
    walk_traverse,
)
from app.services.spatial_validation import (
    check_combined_tract_dimension,
    parse_stated_area_acres,
    validate_traverse,
)

client = vision._get_client()


def ocr_lines_to_notes(lines) -> str:
    # Reading order: top-to-bottom, then left-to-right within a row band.
    rows: list[list] = []
    for line in sorted(lines, key=lambda l: l.bbox[1]):
        cy = (line.bbox[1] + line.bbox[3]) / 2
        placed = False
        for row in rows:
            row_cy = (row[0].bbox[1] + row[0].bbox[3]) / 2
            if abs(cy - row_cy) < (line.bbox[3] - line.bbox[1]):
                row.append(line)
                placed = True
                break
        if not placed:
            rows.append([line])
    ordered = [l for row in rows for l in sorted(row, key=lambda l: l.bbox[0])]
    return "\n".join(f"- {l.text}" for l in ordered)


def run_ocr_structuring(crop_path: str, label: str):
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    img = Image.open(crop_path)
    lines, reliable = run_parcelmap_ocr(img)
    notes = ocr_lines_to_notes(lines)
    print(f"OCR: {len(lines)} lines, reliable={reliable}")
    notes_region = f"Region 1, Piece 1:\n{notes}"

    prompt = vision._BATCH_STRUCTURE_PROMPT.format(
        region_count=1, region_list="Region 1", notes=notes_region
    )
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
    print(f"parcel_labels_found: {[l['label'] for l in parsed.get('parcel_labels_found', [])]}")

    stated = [parse_stated_area_acres(g.get("stated_area_acres")) for g in parcels]
    results = []
    for i, g in enumerate(parcels):
        calls = g.get("boundary_calls") or []
        if calls and g.get("ambiguous_alternates"):
            calls = resolve_ambiguous_calls(
                calls, g["ambiguous_alternates"],
                stated_area_sqft=stated[i],
                sibling_stated_sqfts=[s for j, s in enumerate(stated) if j != i and s],
            )
        if calls:
            calls = drop_conflicting_axis_duplicates(calls)
        rec = {"label": g.get("parcel_label"), "stated": g.get("stated_area_acres"),
               "calls": [f"{c['bearing']} {c['distance']}" for c in calls]}
        if calls:
            t = walk_traverse(calls)
            v = validate_traverse(t, "", stated_area_acres=g.get("stated_area_acres"))
            rec.update(closure=t.closure_error_ft, precision=v["precision_ratio"],
                       area=v["area_acres"], match=v["area_matches_stated"], valid=v["valid"],
                       issues=v["issues"], _v=v)
        else:
            rec.update(valid=None, issues=["no calls"])
        results.append(rec)

    warns = check_combined_tract_dimension([
        {"parcel_label": r["label"], "area_sqft": (r.get("_v") or {}).get("area_sqft"),
         "stated_area_sqft": (r.get("_v") or {}).get("stated_area_sqft")}
        for r in results
    ])
    for r, w in zip(results, warns):
        if w and r.get("_v"):
            r["issues"].append(w)
            r["valid"] = False
        r.pop("_v", None)
        print(f"  {r['label']}: calls={len(r['calls'])} closure={r.get('closure')} "
              f"prec=1:{r.get('precision')} area={r.get('area')} stated={r['stated']} "
              f"match={r.get('match')} valid={r['valid']}")
        for c in r["calls"]:
            print(f"     {c}")
        for iss in r["issues"]:
            print(f"     - {iss[:150]}")
    return results


run_ocr_structuring("scratch_diag/crop_p9.png", "NVZ (regression check -- vision already gets this right)")
run_ocr_structuring("scratch_diag/ocr_test_a1136318_p5_r2.png", "a1136318 (split-pair case)")
run_ocr_structuring("scratch_diag/ocr_test_db54d473_p15_r1.png", "db54d473 (large-crop case)")
