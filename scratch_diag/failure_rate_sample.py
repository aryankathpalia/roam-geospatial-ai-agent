"""
Diagnostic ONLY. Measures the real post-fix (overlap + tie-line
classification, both now in app/services/vision.py) closure/area
failure rate across a SAMPLE of real documents -- not just the one
NVZ Parcel 1 crop this whole investigation has focused on. This number
determines whether targeted retry (only re-running failed regions
unbatched/small-batched) is cheap or still blows the confirmed 15
req/min free-tier limit.

Does NOT modify production code, does NOT implement retry logic --
measurement only, per explicit instruction.
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, ".")

from PIL import Image

from app.services.pdf_inspector import inspect_pdf
from app.services.pdf_renderer import render_page
from app.services.layout_detector_onnx import detect_page_layout
from app.services.region_cropper import extract_region_crops
from app.services.vision import extract_parcel_geometries_batch
from app.services.geometry import (
    resolve_ambiguous_calls,
    drop_conflicting_axis_duplicates,
    walk_traverse,
)
from app.services.spatial_validation import validate_traverse, check_combined_tract_dimension

# A handful of real, small-to-medium documents from data/documents/ --
# different documents from the one this whole investigation has used,
# to check whether the fixes generalize or were overfit to one sheet.
SAMPLE_DOC_IDS = [
    "42f617a1-1d02-4f26-bf05-2a17b23f8fda",  # 2 pages
    "a1136318-03ef-438d-9f02-8fcdcb5dca30",  # 5 pages
    "2f896c95-b0f3-4a49-a447-4b3ca6129c27",  # 8 pages
    "65be453d-f014-4024-84d6-60a2d0052aeb",  # 8 pages
    "83ab22d0-b950-4f5d-ad33-5f0fd6105dbe",  # 12 pages
]

DOC_ROOT = Path("data/documents")
SCRATCH = Path("scratch_diag/failure_rate")
SCRATCH.mkdir(parents=True, exist_ok=True)

all_results = []

for doc_id in SAMPLE_DOC_IDS:
    pdf_path = DOC_ROOT / doc_id / "original.pdf"
    if not pdf_path.exists():
        print(f"[skip] {doc_id}: no original.pdf")
        continue

    inspection = inspect_pdf(str(pdf_path))
    page_count = inspection["page_count"]
    print(f"\n=== {doc_id} ({page_count} pages) ===")

    parcelmap_crops = []  # (page_num, PIL.Image)
    for page_num in range(1, page_count + 1):
        out_path = SCRATCH / f"{doc_id}_p{page_num:03d}.png"
        render_page(pdf_path=str(pdf_path), page_number=page_num, output_path=str(out_path), dpi=200)
        detections = detect_page_layout(str(out_path))
        crops = extract_region_crops(str(out_path), detections, save_dir=None, save_only_needs_review=False)
        page_img = Image.open(out_path).convert("RGB")
        for crop in crops:
            if crop.roam_class == "ParcelMap":
                x, y, w, h = crop.bbox
                region_img = page_img.crop((x, y, x + w, y + h))
                parcelmap_crops.append((page_num, region_img))

    print(f"  ParcelMap regions found: {len(parcelmap_crops)}")
    if not parcelmap_crops:
        continue

    images = [img for _, img in parcelmap_crops]
    try:
        regions_parcels = extract_parcel_geometries_batch(images)
    except Exception as e:
        print(f"  [vision error] {e}")
        continue

    for (page_num, _), parcels in zip(parcelmap_crops, regions_parcels):
        for geometry in parcels:
            calls = geometry.get("boundary_calls") or []
            if calls and geometry.get("ambiguous_alternates"):
                calls = resolve_ambiguous_calls(calls, geometry["ambiguous_alternates"])
            if calls:
                calls = drop_conflicting_axis_duplicates(calls)

            record = {
                "doc_id": doc_id,
                "page": page_num,
                "parcel_label": geometry.get("parcel_label"),
                "num_calls": len(calls),
            }
            if calls:
                traverse = walk_traverse(calls)
                v = validate_traverse(traverse, "", stated_area_acres=geometry.get("stated_area_acres"))
                record["valid"] = v["valid"]
                record["precision_ratio"] = v["precision_ratio"]
                record["issues"] = v["issues"]
            else:
                record["valid"] = False
                record["issues"] = ["no boundary calls survived"]

            all_results.append(record)
            print(f"  page {page_num} {record['parcel_label']}: valid={record['valid']} "
                  f"calls={record['num_calls']} issues={record.get('issues')}")

with open("scratch_diag/failure_rate_results.json", "w", encoding="utf-8") as f:
    json.dump(all_results, f, indent=2, default=str)

total = len(all_results)
failed = sum(1 for r in all_results if not r["valid"])
print(f"\n=== SUMMARY ===")
print(f"Total parcels extracted across sample: {total}")
print(f"Failed validation (Needs Review): {failed}")
if total:
    print(f"Failure rate: {failed/total:.0%}")
