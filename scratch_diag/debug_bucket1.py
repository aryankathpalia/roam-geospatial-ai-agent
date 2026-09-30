import glob
import json
import sys

sys.path.insert(0, ".")

from app.pipeline.document_pipeline import walk_region_parcels
from app.services.spatial_validation import parse_stated_area_acres

bucket1 = {
    ("27211ae5-0099-4b0d-8176-f1483897b766", 13),
    ("510ea60f-f577-420d-b1e0-f2d3e0352661", 41),
    ("798850fc-1be3-4bc6-9f53-1441c39cbd04", 28),
    ("8d75d3ed-d9e7-4bfb-874a-0b52063883eb", 10),
    ("8d75d3ed-d9e7-4bfb-874a-0b52063883eb", 11),
    ("8d75d3ed-d9e7-4bfb-874a-0b52063883eb", 39),
    ("8d75d3ed-d9e7-4bfb-874a-0b52063883eb", 103),
    ("db54d473-c7ab-4bec-bbf4-8ff9a1e3bd9b", 15),
}

f = sorted(glob.glob("scratch_diag/regression_baseline/*_full.json"))[-1]
d = json.load(open(f, encoding="utf-8"))
seen_regions = 0
for doc, dd in d.items():
    for run in dd["full_runs"][:1]:
        if "result" not in run:
            continue
        for page in run["result"]["pages"]:
            key = (doc, page["page_number"])
            if key not in bucket1:
                continue
            for r in page["regions"]:
                if "parcels" not in r:
                    continue
                seen_regions += 1
                print(f"=== {doc[:8]} p{page['page_number']} ({len(r['parcels'])} parcels) ===")
                for p in r["parcels"]:
                    vg = p["vision_geometry"]
                    calls = vg.get("boundary_calls") or []
                    stated = parse_stated_area_acres(vg.get("stated_area_acres"))
                    print(
                        f"  {vg.get('parcel_label')!r}: {len(calls)} raw calls, "
                        f"stated_sqft={stated}"
                    )
                geoms = [pp["vision_geometry"] for pp in r["parcels"]]
                new = walk_region_parcels(geoms, r.get("ocr_text") or "")
                for np in new:
                    resolved = np.get("resolved_boundary_calls")
                    sv = np.get("spatial_validation") or {}
                    print(
                        f"    resolved calls={len(resolved) if resolved else 0} "
                        f"valid={sv.get('valid')} precision={sv.get('precision_ratio')}"
                    )
print("regions checked:", seen_regions)
