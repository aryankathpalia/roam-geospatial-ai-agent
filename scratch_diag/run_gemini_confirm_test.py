"""
DIAGNOSTIC ONLY. Resets a parcel to its pristine pre-confirm state, computes
local_vertices the same way the frontend does, POSTs to the real
/confirm-boundary endpoint, and prints the calibration result. Repeats N
times per parcel to sample Gemini run-to-run variance.
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, ".")
from app.services.geometry import walk_traverse, traverse_to_geojson, TraverseResult

API = "http://127.0.0.1:8000"


def reset_parcel(doc_id, page_number, label):
    path = f"data/documents/{doc_id}/result.json"
    r = json.load(open(path, encoding="utf-8"))
    for p in r["pages"]:
        if p["page_number"] != page_number:
            continue
        for reg in p.get("regions", []):
            if reg.get("class") != "ParcelMap":
                continue
            for par in reg.get("parcels") or []:
                if par.get("vision_geometry", {}).get("parcel_label") != label:
                    continue
                calls = par["resolved_boundary_calls"]
                pts = walk_traverse(calls).points[:-1]
                traverse = TraverseResult(points=pts, closure_error_ft=0.0, unparsed_calls=0)
                par["boundary_geojson"] = traverse_to_geojson(traverse)
                par.pop("boundary_geojson_wgs84", None)
                par.pop("confirmed_boundary_pixels", None)
                par.pop("calibration", None)
                par.pop("human_confirmed", None)
                par.pop("boundary_source", None)
                par.pop("georeference_error", None)
    json.dump(r, open(path, "w", encoding="utf-8"), indent=2)


def compute_local_transform(ring, target_w, target_h):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    w = maxx - minx or 1
    h = maxy - miny or 1
    pad = 0.85
    scale = min((target_w * pad) / w, (target_h * pad) / h)
    return dict(scale=scale, cx=target_w / 2, cy=target_h / 2, midX=(minx + maxx) / 2, midY=(miny + maxy) / 2)


def to_local(pixel_verts, t):
    return [[t["midX"] + (px - t["cx"]) / t["scale"], t["midY"] - (py - t["cy"]) / t["scale"]] for px, py in pixel_verts]


def get_current_ring(doc_id, page_number, label):
    path = f"data/documents/{doc_id}/result.json"
    r = json.load(open(path, encoding="utf-8"))
    for p in r["pages"]:
        if p["page_number"] != page_number:
            continue
        for reg in p.get("regions", []):
            if reg.get("class") != "ParcelMap":
                continue
            for par in reg.get("parcels") or []:
                if par.get("vision_geometry", {}).get("parcel_label") != label:
                    continue
                return par["boundary_geojson"]["geometry"]["coordinates"][0][:-1]
    raise RuntimeError("parcel not found")


def confirm(doc_id, base_payload, ring, crop_w, crop_h):
    t = compute_local_transform(ring, crop_w, crop_h)
    local_verts = to_local(base_payload["vertices"], t)
    payload = {**base_payload, "local_vertices": local_verts}
    req = urllib.request.Request(
        f"{API}/documents/{doc_id}/confirm-boundary",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def run_test(doc_id, page_number, label, payload_file, crop_w, crop_h, n_runs, out_prefix):
    base_payload = json.load(open(payload_file))
    for run in range(1, n_runs + 1):
        reset_parcel(doc_id, page_number, label)
        ring = get_current_ring(doc_id, page_number, label)
        try:
            resp = confirm(doc_id, base_payload, ring, crop_w, crop_h)
        except Exception as exc:
            print(f"=== {label} run {run}/{n_runs}: EXCEPTION {type(exc).__name__}: {exc} ===")
            continue
        par = resp["parcel"]
        cal = par.get("calibration") or {}
        out_path = f"scratch_diag/gemini_edge_experiment/{out_prefix}_run{run}.json"
        json.dump(cal, open(out_path, "w"), indent=2)
        print(f"=== {label} run {run}/{n_runs} ===")
        print(f"  status: {cal.get('status')}  scale: {cal.get('scale_ft_per_px')}  rotation: {cal.get('rotation_deg')}")
        print(f"  corroborating_edge_count: {cal.get('corroborating_edge_count')}")
        for c in cal.get("corroborations", []):
            print(f"    edge{c['edge_index']}: value={c['value']} source={c['source']} pct_err={c['pct_err']}")
        for note in cal.get("notes", []):
            print(f"  note: {note}")
        print()


if __name__ == "__main__":
    import sys as _sys
    which = _sys.argv[1] if len(_sys.argv) > 1 else "all"

    if which in ("lot48", "all"):
        run_test(
            "3ea4cc01-4d39-472b-9465-a105306d63dc", 13, "MAP 7 LOT 48",
            "scratch_diag/confirm_lot48.json", 3059.0, 1837.0, 5, "gem_lot48",
        )
    if which in ("lot48_3", "all"):
        run_test(
            "3ea4cc01-4d39-472b-9465-a105306d63dc", 13, "MAP 7 LOT 48-3",
            "scratch_diag/confirm_lot48_3.json", 3059.0, 1837.0, 5, "gem_lot48_3",
        )
    if which in ("easement", "all"):
        run_test(
            "1600dbf6-9174-4360-9cae-55886bc7ca7f", 6, "Planned Utility Easement",
            "scratch_diag/confirm_easement.json", 1460.0, 1922.0, 5, "gem_easement",
        )
    if which in ("nvz", "all"):
        run_test(
            "6d8534f5-f07a-460d-970f-4ccf5c40a3e4", 9, "PARCEL 1",
            "scratch_diag/confirm_p1.json", 2174.0, 1687.0, 1, "gem_nvz_p1",
        )
        run_test(
            "6d8534f5-f07a-460d-970f-4ccf5c40a3e4", 9, "PARCEL 2",
            "scratch_diag/confirm_p2.json", 2174.0, 1687.0, 1, "gem_nvz_p2",
        )
