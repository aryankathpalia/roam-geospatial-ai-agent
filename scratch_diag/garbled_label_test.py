"""Diagnostic: check whether the atomic-label fix stops the garbled label on 83ab22d0."""
import sys
sys.path.insert(0, ".")
from pathlib import Path
from PIL import Image
from app.services.pdf_inspector import inspect_pdf
from app.services.pdf_renderer import render_page
from app.services.layout_detector_onnx import detect_page_layout
from app.services.region_cropper import extract_region_crops
from app.services.vision import extract_parcel_geometries_batch

doc_id = "83ab22d0-b950-4f5d-ad33-5f0fd6105dbe"
SCRATCH = Path("scratch_diag/generalization")
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

for run in range(1, 4):
    print(f"\n--- run {run} ---")
    regions = extract_parcel_geometries_batch(crops)
    for r_idx, parcels in enumerate(regions, start=1):
        for g in parcels:
            print(f"  label={g.get('parcel_label')!r} calls={len(g.get('boundary_calls') or [])}")
