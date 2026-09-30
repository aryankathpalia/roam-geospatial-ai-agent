"""
Diagnostic ONLY. Runs today's production extraction (tie-line
exclusion, tile overlap, enumeration+dedup, Track B acreage resolution,
503 fallback) on the same 5 real documents as this morning's
failure-rate baseline (18/18 parcels failed), mirroring
document_pipeline.py's per-parcel post-processing exactly. Detection and
cropping match production; OCR is skipped (paddleocr is Modal-only), so
stated acreage comes only from vision's own extraction.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from PIL import Image

from app.services.pdf_inspector import inspect_pdf
from app.services.pdf_renderer import render_page
from app.services.layout_detector_onnx import detect_page_layout
from app.services.region_cropper import extract_region_crops
from app.services.vision import extract_parcel_geometries_batch
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

DOCS = [
    "27211ae5-0099-4b0d-8176-f1483897b766",
    "3affa529-5b4f-42d7-b336-596315433572",
    "db54d473-c7ab-4bec-bbf4-8ff9a1e3bd9b",
    "42f617a1-1d02-4f26-bf05-2a17b23f8fda",
    "a1136318-03ef-438d-9f02-8fcdcb5dca30",
    "2f896c95-b0f3-4a49-a447-4b3ca6129c27",
    "65be453d-f014-4024-84d6-60a2d0052aeb",
    "83ab22d0-b950-4f5d-ad33-5f0fd6105dbe",
]
SCRATCH = Path("scratch_diag/generalization")
SCRATCH.mkdir(parents=True, exist_ok=True)

all_records = []
for doc_id in DOCS:
    pdf = Path("data/documents") / doc_id / "original.pdf"
    pages = inspect_pdf(str(pdf))["page_count"]
    crops = []
    for n in range(1, pages + 1):
        out = SCRATCH / f"{doc_id}_p{n:03d}.png"
        render_page(pdf_path=str(pdf), page_number=n, output_path=str(out), dpi=200)
        dets = detect_page_layout(str(out))
        page_img = Image.open(out).convert("RGB")
        for c in extract_region_crops(str(out), dets, save_dir=None, save_only_needs_review=False):
            if c.roam_class == "ParcelMap":
                x, y, w, h = c.bbox
                crops.append((n, page_img.crop((x, y, x + w, y + h))))
    print(f"\n{'=' * 70}\n{doc_id} ({pages} pages): {len(crops)} ParcelMap region(s)", flush=True)
    if not crops:
        continue

    try:
        regions = extract_parcel_geometries_batch([im for _, im in crops])
    except Exception as e:
        print(f"  VISION ERROR: {type(e).__name__}: {e}", flush=True)
        all_records.append({"doc": doc_id, "vision_error": str(e)})
        continue

    for r_idx, ((page, _), parcels) in enumerate(zip(crops, regions), start=1):
        stated = [parse_stated_area_acres(g.get("stated_area_acres")) for g in parcels]
        results = []
        for i, g in enumerate(parcels):
            rec = {
                "doc": doc_id, "page": page, "region": r_idx,
                "label": g.get("parcel_label"),
                "stated_ac": g.get("stated_area_acres"),
                "raw_calls": len(g.get("boundary_calls") or []),
                "alternates": [a.get("distance") for a in g.get("ambiguous_alternates") or []],
            }
            calls = g.get("boundary_calls") or []
            if calls and g.get("ambiguous_alternates"):
                calls = resolve_ambiguous_calls(
                    calls, g["ambiguous_alternates"],
                    stated_area_sqft=stated[i],
                    sibling_stated_sqfts=[s for j, s in enumerate(stated) if j != i and s],
                )
            if calls:
                calls = drop_conflicting_axis_duplicates(calls)
            rec["used_calls"] = [f"{c.get('bearing')} {c.get('distance')}" for c in calls]
            if calls:
                t = walk_traverse(calls)
                v = validate_traverse(t, "", stated_area_acres=g.get("stated_area_acres"))
                rec.update(closure_ft=t.closure_error_ft, precision=v["precision_ratio"],
                           area_ac=v["area_acres"], area_match=v["area_matches_stated"],
                           self_intersects=v["self_intersects"], valid=v["valid"],
                           issues=v["issues"], _v=v)
            else:
                rec.update(valid=None, issues=["no attributable boundary calls (extraction_note)"])
            results.append(rec)

        warns = check_combined_tract_dimension([
            {"parcel_label": r["label"],
             "area_sqft": (r.get("_v") or {}).get("area_sqft"),
             "stated_area_sqft": (r.get("_v") or {}).get("stated_area_sqft")}
            for r in results
        ])
        for r, w in zip(results, warns):
            if w and r.get("_v"):
                r["issues"].append(w)
                r["valid"] = False
            r.pop("_v", None)
            all_records.append(r)
            print(f"  p{page} R{r_idx} {r['label']}: raw_calls={r['raw_calls']} used={len(r['used_calls'])} "
                  f"alts={r['alternates']} closure={r.get('closure_ft')} prec=1:{r.get('precision')} "
                  f"area={r.get('area_ac')} stated={r['stated_ac']} valid={r['valid']}", flush=True)
            print(f"      calls: {r['used_calls']}", flush=True)
            for iss in r["issues"]:
                print(f"      - {iss[:170]}", flush=True)

(Path("scratch_diag") / "generalization_results.json").write_text(
    json.dumps(all_records, indent=2, default=str), encoding="utf-8"
)
parcels = [r for r in all_records if "label" in r]
print(f"\nTOTAL parcels={len(parcels)} valid={sum(1 for r in parcels if r['valid'])} "
      f"needs_review={sum(1 for r in parcels if r['valid'] is False)} "
      f"no_calls={sum(1 for r in parcels if r['valid'] is None)}")
