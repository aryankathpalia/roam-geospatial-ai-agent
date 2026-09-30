"""Test enumeration-citation broadening ONLY (rule 1 untouched): 798850fc p28
(must stay fixed: ADJUSTED LOT H phantom label gone) and 2f896c95 p7 (must NOT
regress -- this is the page that broke last time under the bundled fix)."""
import sys

sys.path.insert(0, ".")

import json

from PIL import Image

from app.pipeline.document_pipeline import walk_region_parcels
from app.services.vision import extract_parcel_geometries_batch

TARGETS = {
    "798850fc_p28": ("798850fc-1be3-4bc6-9f53-1441c39cbd04", 28, [133, 288, 1502, 1207]),
    "2f896c95_p7": ("2f896c95-b0f3-4a49-a447-4b3ca6129c27", 7, None),
}

labs = json.loads(open("tests/regression/region_labels.json", encoding="utf-8").read())["regions"]
for r in labs:
    if r["doc"].startswith("2f896c95") and r["page"] == 7:
        TARGETS["2f896c95_p7"] = ("2f896c95-b0f3-4a49-a447-4b3ca6129c27", 7, r["bbox"])

crops = []
keys = []
for name, (doc, page, bbox) in TARGETS.items():
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
    labels = [g.get("parcel_label") for g in geometries]
    print(f"  {len(geometries)} parcel(s) found: {labels}")
    if key == "798850fc_p28":
        phantom = any("ADJUSTED LOT H" in (lbl or "") for lbl in labels)
        print(f"  PHANTOM 'ADJUSTED LOT H' present: {phantom}")
    parcels = walk_region_parcels(geometries)
    for p in parcels:
        label = p["vision_geometry"].get("parcel_label")
        resolved = p.get("resolved_boundary_calls") or []
        sv = p.get("spatial_validation")
        if sv:
            print(
                f"  {label}: {len(resolved)} calls, precision 1:{sv['precision_ratio']}, "
                f"valid={sv['valid']}, area_acres={sv['area_acres']}"
            )
        else:
            print(f"  {label}: no traverse ({p.get('extraction_note')})")
