import glob
import json
import os
import sys

sys.path.insert(0, ".")

from app.pipeline.document_pipeline import walk_region_parcels

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

before_closed = before_valid = before_n = 0
after_closed = after_valid = after_n = 0
borrows = []

for f in glob.glob("scratch_diag/regression_baseline/*_full.json"):
    d = json.load(open(f, encoding="utf-8"))
    for doc, dd in d.items():
        for run in dd["full_runs"]:
            if "result" not in run:
                continue
            for page in run["result"]["pages"]:
                key = (doc, page["page_number"])
                if key not in bucket1:
                    continue
                for r in page["regions"]:
                    if "parcels" not in r:
                        continue
                    for p in r["parcels"]:
                        before_n += 1
                        sv = p.get("spatial_validation") or {}
                        if sv.get("valid"):
                            before_valid += 1
                        if sv.get("precision_ratio") and sv["precision_ratio"] >= 500:
                            before_closed += 1
                    geoms = [pp["vision_geometry"] for pp in r["parcels"]]
                    new = walk_region_parcels(geoms, r.get("ocr_text") or "")
                    for new_p in new:
                        after_n += 1
                        sv = new_p.get("spatial_validation") or {}
                        if sv.get("valid"):
                            after_valid += 1
                        if sv.get("precision_ratio") and sv["precision_ratio"] >= 500:
                            after_closed += 1
                        for note in new_p.get("assembly_notes") or []:
                            if "Borrowed" in note:
                                borrows.append(
                                    (
                                        os.path.basename(f),
                                        doc[:8],
                                        page["page_number"],
                                        run["run"],
                                        new_p["vision_geometry"].get("parcel_label"),
                                        sv.get("valid"),
                                        note[:160],
                                    )
                                )

print("BEFORE: n_parcels", before_n, "closed(1:500+)", before_closed, "valid", before_valid)
print("AFTER:  n_parcels", after_n, "closed(1:500+)", after_closed, "valid", after_valid)
print()
print("borrow attempts that produced a note:", len(borrows))
for b in borrows:
    print(b)
