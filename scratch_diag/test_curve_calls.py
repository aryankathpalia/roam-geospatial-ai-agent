"""Targeted test: does curve-call support help exactly the 4 pages
flagged as curve-boundary subdivision plats (8d75d3ed p10/p11/p39/p103)?
Runs extraction fresh (live Gemini) on just these 4 crops, 2x each."""

import json
import sys

sys.path.insert(0, ".")

from PIL import Image

from app.pipeline.document_pipeline import walk_region_parcels
from app.services.vision import extract_parcel_geometries_batch

DOC = "8d75d3ed-d9e7-4bfb-874a-0b52063883eb"
PAGES = {
    10: [182, 398, 1380, 926],
    11: [187, 433, 1398, 831],
    39: [150, 228, 1595, 1042],
    103: [233, 180, 6502, 2865],
}

for run in range(2):
    print(f"=== RUN {run} ===")
    crops = []
    for page, (x, y, w, h) in PAGES.items():
        im = Image.open(f"data/documents/{DOC}/pages/page_{page:03d}.png").convert("RGB")
        crops.append(im.crop((x, y, x + w, y + h)))

    results = extract_parcel_geometries_batch(crops)
    for page, geometries in zip(PAGES, results):
        if isinstance(geometries, Exception):
            print(f"page {page}: vision error: {geometries}")
            continue
        n_curve = sum(len(g.get("curve_calls") or []) for g in geometries)
        n_line = sum(len(g.get("boundary_calls") or []) for g in geometries)
        print(f"page {page}: {len(geometries)} parcel(s), {n_line} line calls, {n_curve} curve calls")
        parcels = walk_region_parcels(geometries)
        for p in parcels:
            sv = p.get("spatial_validation")
            label = p["vision_geometry"].get("parcel_label")
            resolved = p.get("resolved_boundary_calls") or []
            n_resolved_curve = sum(1 for c in resolved if c.get("call_type") == "curve")
            if sv:
                print(
                    f"  {label}: {len(resolved)} calls ({n_resolved_curve} curve), "
                    f"precision 1:{sv['precision_ratio']}, valid={sv['valid']}, "
                    f"area_acres={sv['area_acres']}"
                )
            else:
                print(f"  {label}: no traverse ({p.get('extraction_note')})")
