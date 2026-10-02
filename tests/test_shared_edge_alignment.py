"""
Shared-edge alignment: sibling parcels on the same sheet that describe a
literal common boundary line must be placed so that line actually
coincides, not independently re-centered on the document's single anchor
point. Regression for a real bug on the Patnaude packet.
"""
import math

import pytest

from app.pipeline.document_pipeline import walk_region_parcels
from app.services.geometry import align_to_shared_edge, find_shared_edge, walk_traverse

ANCHOR_LAT, ANCHOR_LON = 39.774729, -119.658860

# The real two sibling parcels' real extracted calls from a live Patnaude
# run: "Remainder Parcel" (3 calls, doesn't close on its own) and
# "Parcel 1" (4 calls, closes well) -- both naming the SAME shared line
# from their own side ("S89 24'39"E 1318.25'" vs its near-reverse).
REMAINDER_CALLS = [
    {"bearing": "N89 25' 02\"W", "distance": "3946.91'"},
    {"bearing": "N0 57' 09\"E", "distance": "1333.30'"},
    {"bearing": "N89 33' 38\"W", "distance": "3950.22'"},
]
PARCEL_1_CALLS = [
    {"bearing": "S89 24' 39\"E", "distance": "1318.25'"},
    {"bearing": "S0 48' 45\"W", "distance": "1320.11'"},
    {"bearing": "N89 33' 00\"W", "distance": "1318.27'"},
    {"bearing": "N0 48' 45\"E", "distance": "1323.32'"},
]


def test_find_shared_edge_matches_the_real_reversed_printed_line():
    remainder_points = walk_traverse(REMAINDER_CALLS).points
    parcel1_points = walk_traverse(PARCEL_1_CALLS).points
    match = find_shared_edge(remainder_points, parcel1_points)
    assert match is not None


def test_align_to_shared_edge_only_translates_never_reshapes():
    points_a = walk_traverse(REMAINDER_CALLS).points
    points_b = walk_traverse(PARCEL_1_CALLS).points
    seg_a, seg_b = find_shared_edge(points_a, points_b)

    def seg_len(points, i):
        return math.hypot(points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1])

    before_len_b = seg_len(points_b, seg_b)
    aligned = align_to_shared_edge(points_a, points_b, seg_a, seg_b)
    after_len_b = math.hypot(aligned[seg_b + 1][0] - aligned[seg_b][0], aligned[seg_b + 1][1] - aligned[seg_b][1])
    assert after_len_b == pytest.approx(before_len_b, abs=1e-6)  # shape untouched

    # The matched segments now coincide (within the printed distances' own
    # small mismatch, and REMAINDER's own 3-call traverse not perfectly
    # closing) -- translation only, no rotation/scale drift, and a world
    # apart from the un-aligned ~3,900ft gap a naive independent-anchor
    # placement produces.
    a1, a2 = points_a[seg_a], points_a[seg_a + 1]
    assert math.hypot(aligned[seg_b + 1][0] - a1[0], aligned[seg_b + 1][1] - a1[1]) < 10.0
    assert math.hypot(aligned[seg_b][0] - a2[0], aligned[seg_b][1] - a2[1]) < 10.0


def test_walk_region_parcels_places_real_siblings_touching_not_gapped():
    parcels = [
        {"parcel_label": "REMAINDER PARCEL", "stated_area_acres": "120.4", "boundary_calls": REMAINDER_CALLS},
        {"parcel_label": "PARCEL 1", "stated_area_acres": "40.00", "boundary_calls": PARCEL_1_CALLS},
    ]
    results = walk_region_parcels(parcels, anchor_lat=ANCHOR_LAT, anchor_lon=ANCHOR_LON)
    remainder, parcel1 = results
    assert "boundary_geojson_wgs84" in remainder and "boundary_geojson_wgs84" in parcel1
    assert any("aligned to share a common boundary" in n for n in parcel1.get("assembly_notes", []))

    from pyproj import Geod
    geod = Geod(ellps="WGS84")

    def ring_vertices(parcel):
        return parcel["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]

    rem_ring, p1_ring = ring_vertices(remainder), ring_vertices(parcel1)

    def min_vertex_distance_m(ring_a, ring_b):
        best = float("inf")
        for lon_a, lat_a in ring_a:
            for lon_b, lat_b in ring_b:
                _, _, d = geod.inv(lon_a, lat_a, lon_b, lat_b)
                best = min(best, d)
        return best

    # The shared edge's endpoints must now coincide (within a few metres
    # -- the printed calls' own small reading mismatch, and REMAINDER's
    # own traverse not perfectly closing), not sit hundreds/thousands of
    # feet apart the way two independently-anchored sibling shapes would
    # (confirmed: the real bug's un-aligned gap was on the order of
    # kilometres -- see the PLSS/anchor investigation's before/after).
    assert min_vertex_distance_m(rem_ring, p1_ring) < 10.0


def test_walk_region_parcels_untouched_when_no_shared_edge_exists():
    parcels = [
        {"parcel_label": "LOT A", "stated_area_acres": "5.0", "boundary_calls": [
            {"bearing": "N0 00' 00\"E", "distance": "300'"}, {"bearing": "S90 00' 00\"E", "distance": "300'"},
            {"bearing": "S0 00' 00\"W", "distance": "300'"}, {"bearing": "N90 00' 00\"W", "distance": "300'"},
        ]},
        {"parcel_label": "LOT B", "stated_area_acres": "7.0", "boundary_calls": [
            {"bearing": "N0 00' 00\"E", "distance": "500'"}, {"bearing": "S90 00' 00\"E", "distance": "500'"},
            {"bearing": "S0 00' 00\"W", "distance": "500'"}, {"bearing": "N90 00' 00\"W", "distance": "500'"},
        ]},
    ]
    results = walk_region_parcels(parcels, anchor_lat=ANCHOR_LAT, anchor_lon=ANCHOR_LON)
    for r in results:
        assert not any("aligned to share a common boundary" in n for n in r.get("assembly_notes", []))
