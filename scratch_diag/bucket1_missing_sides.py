import glob
import json
import sys
from collections import defaultdict

sys.path.insert(0, ".")

from app.services.geometry import parse_bearing, parse_distance

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

# label -> max parsed call count seen across every run, and one example call list
best = defaultdict(lambda: {"max_calls": 0, "example": None})

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
                    for p in r.get("parcels", []):
                        vg = p["vision_geometry"]
                        label = vg.get("parcel_label")
                        calls = vg.get("boundary_calls") or []
                        n = sum(
                            1
                            for c in calls
                            if parse_bearing(str(c.get("bearing") or "")) is not None
                            and parse_distance(str(c.get("distance") or "")) is not None
                        )
                        entry = best[(key, label)]
                        if n > entry["max_calls"]:
                            entry["max_calls"] = n
                            entry["example"] = calls

for (key, label), info in sorted(best.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
    print(f"{key[0][:8]} p{key[1]} | {label!r}: max_calls_ever_captured={info['max_calls']}")
