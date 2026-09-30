"""Diagnostic: Track B end-to-end on NVZ page 9, several reruns."""

import sys

sys.path.insert(0, ".")

from PIL import Image

from app.services.vision import extract_parcel_geometries_batch
from app.services.geometry import (
    resolve_ambiguous_calls,
    drop_conflicting_axis_duplicates,
    walk_traverse,
    _shoelace_area,
)
from app.services.spatial_validation import parse_stated_area_acres

img = Image.open("scratch_diag/crop_p9.png")
N = 5
for run in range(1, N + 1):
    for attempt in range(3):
        try:
            parcels = extract_parcel_geometries_batch([img])[0]
            break
        except Exception as e:
            print(f"  (retry: {e})")
    else:
        print(f"run {run}: failed")
        continue
    stated = [parse_stated_area_acres(p.get("stated_area_acres")) for p in parcels]
    print(f"\n=== run {run} ===")
    for i, p in enumerate(parcels):
        calls = p.get("boundary_calls") or []
        alts = p.get("ambiguous_alternates") or []
        if calls and alts:
            calls = resolve_ambiguous_calls(
                calls, alts, stated[i], [s for j, s in enumerate(stated) if j != i and s]
            )
        if calls:
            calls = drop_conflicting_axis_duplicates(calls)
        t = walk_traverse(calls) if calls else None
        area = round(_shoelace_area(t.points) / 43560, 3) if t else None
        print(f"  {p.get('parcel_label')} stated={p.get('stated_area_acres')} "
              f"alts={[a['distance'] for a in alts]} "
              f"final={[c['distance'] for c in calls]} area={area} "
              f"closure={t.closure_error_ft if t else None}")
