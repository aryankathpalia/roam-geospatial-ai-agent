"""
DIAGNOSTIC ONLY. Adversarial false-positive check on REAL polygons: candidates whose
DISTANCES genuinely match real edges (so they pass corroboration) but whose bearings are
random/unrelated. Counts how often calibrate() reaches an accepted rotation
(a) with the quadrant fallback disabled (baseline) and (b) enabled.
"""
import json, random, sys, types
sys.modules.setdefault("paddleocr", types.SimpleNamespace(PaddleOCR=object))
sys.path.insert(0, ".")
from app.services import calibration as c

CASES = {  # name: (doc, page, label, payload)
    "lot48": ("3ea4cc01-4d39-472b-9465-a105306d63dc", 13, "MAP 7 LOT 48", "confirm_lot48.json"),
    "lot48_3": ("3ea4cc01-4d39-472b-9465-a105306d63dc", 13, "MAP 7 LOT 48-3", "confirm_lot48_3.json"),
    "easement": ("1600dbf6-9174-4360-9cae-55886bc7ca7f", 6, "Planned Utility Easement", "confirm_easement.json"),
    "nvz_p1": ("6d8534f5-f07a-460d-970f-4ccf5c40a3e4", 9, "PARCEL 1", "confirm_p1.json"),
    "nvz_p2": ("6d8534f5-f07a-460d-970f-4ccf5c40a3e4", 9, "PARCEL 2", "confirm_p2.json"),
}
real_resolve = c._resolve_quadrant_ambiguity
rnd = random.Random(7)
N = 3000
for name, (doc, page, label, payload) in CASES.items():
    r = json.load(open(f"data/documents/{doc}/result.json"))
    par = next(p for pg in r["pages"] if pg["page_number"] == page for reg in pg.get("regions", [])
               if reg.get("class") == "ParcelMap" for p in reg.get("parcels") or []
               if p.get("vision_geometry", {}).get("parcel_label") == label)
    acres = float(par["vision_geometry"]["stated_area_acres"])
    poly = [tuple(v) for v in json.load(open("scratch_diag/" + payload))["vertices"]]
    n = len(poly)
    sq = acres * 43560
    scale = (sq / c._polygon_area(poly)) ** 0.5
    out = {}
    for mode in ("baseline", "fallback"):
        c._resolve_quadrant_ambiguity = (lambda items: (None, "disabled")) if mode == "baseline" else real_resolve
        rnd.seed(11); ok = 0; tried = 0
        for _ in range(N):
            k = rnd.choice([2, 3]) if n >= 3 else 2
            edges = rnd.sample(range(n), min(k, n))
            cands = [{"edge_index": e, "value": c._edge_geom(poly, e)[0] * scale * rnd.uniform(0.99, 1.01),
                      "azimuth": rnd.uniform(0, 360), "source": "gemini_association"} for e in edges]
            res = c.calibrate(poly, [], sq, extra_candidates=cands)
            tried += 1
            ok += res.rotation_deg is not None or any("180deg" in x and "not guessing" in x for x in res.notes)
        out[mode] = ok / tried
    print(f"{name:9s} edges={n}  false 'rotation accepted' on random bearings:  baseline {out['baseline']*100:5.2f}%  ->  with fallback {out['fallback']*100:5.2f}%")
