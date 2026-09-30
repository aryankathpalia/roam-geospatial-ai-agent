import sys, json
sys.path.insert(0, ".")
from PIL import Image
from app.services.vision import extract_parcel_geometries_batch
from app.services.geometry import walk_traverse, traverse_to_geojson, resolve_ambiguous_calls, drop_conflicting_axis_duplicates
from app.services.spatial_validation import validate_traverse, check_combined_tract_dimension

img = Image.open("scratch_diag/crop_p9.png")
regions_parcels = extract_parcel_geometries_batch([img])
parcels_raw = regions_parcels[0]

results = []
for geometry in parcels_raw:
    print(f"\n{geometry.get('parcel_label')} stated_acres: {geometry.get('stated_area_acres')}")
    for c in geometry.get("boundary_calls", []):
        print(f"  {c['bearing']} {c['distance']}")
    if geometry.get("ambiguous_alternates"):
        print(f"  ambiguous_alternates: {geometry['ambiguous_alternates']}")
    calls = geometry.get("boundary_calls") or []
    if calls and geometry.get("ambiguous_alternates"):
        resolved = resolve_ambiguous_calls(calls, geometry["ambiguous_alternates"])
        if resolved != calls:
            print(f"  RESOLVED to: {resolved}")
        calls = resolved
    if calls:
        deduped = drop_conflicting_axis_duplicates(calls)
        if deduped != calls:
            print(f"  AXIS-DEDUPED to: {deduped}")
        calls = deduped
    entry = {"vision_geometry": geometry}
    if calls:
        traverse = walk_traverse(calls)
        entry["spatial_validation"] = validate_traverse(
            traverse, "", stated_area_acres=geometry.get("stated_area_acres")
        )
    results.append(entry)

combined = check_combined_tract_dimension([
    {
        "parcel_label": r["vision_geometry"].get("parcel_label"),
        "area_sqft": r.get("spatial_validation", {}).get("area_sqft"),
        "stated_area_sqft": r.get("spatial_validation", {}).get("stated_area_sqft"),
    }
    for r in results
])

print("\n=== VALIDATION ===")
for r, w in zip(results, combined):
    label = r["vision_geometry"].get("parcel_label")
    sv = r.get("spatial_validation", {})
    print(f"\n{label}: valid={sv.get('valid')} precision=1:{sv.get('precision_ratio')} closure_issues={sv.get('issues')}")
    print(f"  area_acres={sv.get('area_acres')} stated_sqft={sv.get('stated_area_sqft')}")
    if w:
        print(f"  COMBINED-TRACT WARNING: {w}")

with open("scratch_diag/regression_result.json", "w") as f:
    json.dump(results, f, indent=2, default=str)
