"""DIAGNOSTIC ONLY. Replays saved real-Gemini candidates through the real confirm endpoint."""
import glob
import json
import os
import sys

sys.path.insert(0, "scratch_diag")
import run_gemini_confirm_test as t  # noqa: E402

D = "scratch_diag/gemini_edge_experiment/"
CASES = {
    "lot48": ("3ea4cc01-4d39-472b-9465-a105306d63dc", 13, "MAP 7 LOT 48", "confirm_lot48.json", 3059.0, 1837.0),
    "lot48_3": ("3ea4cc01-4d39-472b-9465-a105306d63dc", 13, "MAP 7 LOT 48-3", "confirm_lot48_3.json", 3059.0, 1837.0),
    "easement": ("1600dbf6-9174-4360-9cae-55886bc7ca7f", 6, "Planned Utility Easement", "confirm_easement.json", 1460.0, 1922.0),
    "nvz_p1": ("6d8534f5-f07a-460d-970f-4ccf5c40a3e4", 9, "PARCEL 1", "confirm_p1.json", 2174.0, 1687.0),
    "nvz_p2": ("6d8534f5-f07a-460d-970f-4ccf5c40a3e4", 9, "PARCEL 2", "confirm_p2.json", 2174.0, 1687.0),
}

for name in sys.argv[1:]:
    doc, page, label, payload, cw, ch = CASES[name]
    base = json.load(open("scratch_diag/" + payload))
    for f in sorted(glob.glob(f"{D}gem_{name}_run[0-9].json")):
        saved = json.load(open(f))
        cands = [
            {"edge_index": c["edge_index"], "value": c["value"], "azimuth": c["azimuth"], "source": c["source"]}
            for c in saved["corroborations"] if c["source"] == "gemini_association"
        ]
        json.dump(cands, open("/tmp/claude-0/replay_candidates.json", "w"))
        t.reset_parcel(doc, page, label)
        ring = t.get_current_ring(doc, page, label)
        cal = t.confirm(doc, base, ring, cw, ch)["parcel"].get("calibration") or {}
        run = f[-6]
        json.dump(cal, open(f"scratch_diag/quadrant_replay/{name}_run{run}.json", "w"), indent=2)
        print(f"=== {name} run{run}: was {saved['status']} ({saved['corroborating_edge_count']}) -> NOW {cal.get('status')} "
              f"rot={cal.get('rotation_deg')} edges={cal.get('corroborating_edge_count')}")
        for c in cal.get("corroborations", []):
            print(f"    edge{c['edge_index']} {c['source'][:3]} v={c['value']} az={c['azimuth']} err={c['pct_err']}"
                  + (f"  ** QUADRANT RESOLVED (as read {c['azimuth_as_read']:.2f})" if c.get("quadrant_resolved") else ""))
        for n in cal.get("notes", []):
            if "quadrant" in n or "disagree" in n or "180" in n:
                print("    note:", n[:230])
