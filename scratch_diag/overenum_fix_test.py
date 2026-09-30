"""Diagnostic: test the Step-1-exclusion fix on 42f617a1 and a1136318."""
import sys
sys.path.insert(0, ".")
from pathlib import Path
from PIL import Image
from app.services.pdf_inspector import inspect_pdf
from app.services.pdf_renderer import render_page
from app.services.layout_detector_onnx import detect_page_layout
from app.services.region_cropper import extract_region_crops
from app.services.vision import extract_parcel_geometries_batch

SCRATCH = Path("scratch_diag/generalization")

for doc_id, expect in [
    ("42f617a1-1d02-4f26-bf05-2a17b23f8fda", "LOT 11 should be gone"),
    ("a1136318-03ef-438d-9f02-8fcdcb5dca30", "~2 labels (Lot 1, Lot 2)"),
]:
    pdf = Path("data/documents") / doc_id / "original.pdf"
    pages = inspect_pdf(str(pdf))["page_count"]
    crops = []
    for n in range(1, pages + 1):
        out = SCRATCH / f"{doc_id}_p{n:03d}.png"
        if not out.exists():
            render_page(pdf_path=str(pdf), page_number=n, output_path=str(out), dpi=200)
        dets = detect_page_layout(str(out))
        page_img = Image.open(out).convert("RGB")
        for c in extract_region_crops(str(out), dets, save_dir=None, save_only_needs_review=False):
            if c.roam_class == "ParcelMap":
                x, y, w, h = c.bbox
                crops.append(page_img.crop((x, y, x + w, y + h)))

    print(f"\n{'='*70}\n{doc_id}  (expect: {expect})\n{'='*70}")
    regions = extract_parcel_geometries_batch(crops)
    for r_idx, parcels in enumerate(regions, start=1):
        for g in parcels:
            print(f"  region {r_idx}: label={g.get('parcel_label')!r} calls={len(g.get('boundary_calls') or [])} stated={g.get('stated_area_acres')}")
