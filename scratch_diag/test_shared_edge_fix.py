"""Test the shared-edge prompt fix: db54d473 p15, 27211ae5 p13, 798850fc p28
(the 3 original failures) plus 2f896c95 p6/p7/p8 (a real, previously-partially-
passing multi-parcel document, as a Pattern-A-style regression check since
NVZ's own source PDF isn't available in this environment)."""
import sys

sys.path.insert(0, ".")

from PIL import Image

from app.pipeline.document_pipeline import walk_region_parcels
from app.services.vision import extract_parcel_geometries_batch

TARGETS = {
    "db54d473_p15": ("db54d473-c7ab-4bec-bbf4-8ff9a1e3bd9b", 15, [654, 187, 4637, 3615]),
    "27211ae5_p13": ("27211ae5-0099-4b0d-8176-f1483897b766", 13, [125, 117, 2717, 1592]),
    "798850fc_p28": ("798850fc-1be3-4bc6-9f53-1441c39cbd04", 28, [133, 288, 1502, 1207]),
    "2f896c95_p6": ("2f896c95-b0f3-4a49-a447-4b3ca6129c27", 6, [55, 89, 2065, 1344]),
    "2f896c95_p7": ("2f896c95-b0f3-4a49-a447-4b3ca6129c27", 7, None),  # bbox filled below if found
    "2f896c95_p8": ("2f896c95-b0f3-4a49-a447-4b3ca6129c27", 8, [878, 268, 5330, 3403]),
}

import json

labs = json.loads(open("tests/regression/region_labels.json", encoding="utf-8").read())["regions"]
for r in labs:
    if r["doc"].startswith("2f896c95") and r["page"] == 7:
        TARGETS["2f896c95_p7"] = ("2f896c95-b0f3-4a49-a447-4b3ca6129c27", 7, r["bbox"])

crops = []
keys = []
for name, (doc, page, bbox) in TARGETS.items():
    if bbox is None:
        print(f"SKIP {name}: no bbox found")
        continue
    x, y, w, h = bbox
    x, y = max(x, 0), max(y, 0)
    im = Image.open(f"data/documents/{doc}/pages/page_{page:03d}.png").convert("RGB")
    crops.append(im.crop((x, y, x + w, y + h)))
    keys.append(name)

print(f"running {len(crops)} crops through extraction...")
results = extract_parcel_geometries_batch(crops)

for key, geometries in zip(keys, results):
    print(f"\n=== {key} ===")
    if isinstance(geometries, Exception):
        print("  vision error:", geometries)
        continue
    print(f"  {len(geometries)} parcel(s) found: {[g.get('parcel_label') for g in geometries]}")
    parcels = walk_region_parcels(geometries)
    for p in parcels:
        label = p["vision_geometry"].get("parcel_label")
        resolved = p.get("resolved_boundary_calls") or []
        sv = p.get("spatial_validation")
        notes = p.get("assembly_notes")
        if sv:
            print(
                f"  {label}: {len(resolved)} calls, precision 1:{sv['precision_ratio']}, "
                f"valid={sv['valid']}, area_acres={sv['area_acres']}"
            )
        else:
            print(f"  {label}: no traverse ({p.get('extraction_note')})")
        if notes:
            for n in notes:
                print(f"    note: {n[:150]}")
