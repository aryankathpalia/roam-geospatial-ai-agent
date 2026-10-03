"""Group fit of a sheet's confirmed parcels onto the aliquot part its legal description names."""
import copy
import sys
import types

sys.modules.setdefault("paddleocr", types.SimpleNamespace(PaddleOCR=object))

import pytest  # noqa: E402
from pyproj import Geod  # noqa: E402

from app.routes.documents import _fit_sheet_to_aliquot  # noqa: E402

GEOD = Geod(ellps="WGS84")
# The real BLM-derived N 1/2 of S 1/2 of Sec 17, T22N R21E MDM (160.08 ac).
ALIQUOT = {
    "description": "NORTH 1/2 of SOUTH 1/2 of Section 17",
    "polygon": [[-119.6588596, 39.7747286], [-119.6401805, 39.7747644], [-119.6401692, 39.7711328], [-119.6588625, 39.7710712]],
    "acres": 160.08,
}


def _parcel(ring, acres):
    return {
        "human_confirmed": True,
        "boundary_geojson_wgs84": {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}},
        "spatial_validation": {"area_acres": acres},
        "placement": {"status": "approximate", "notes": []},
    }


# The live, ~1 km-west placement of the real Patnaude parcels before the fit.
REM = [[-119.670549, 39.775922], [-119.656559, 39.7758057], [-119.6566087, 39.7721741], [-119.670621, 39.7722376], [-119.670549, 39.775922]]
P1 = [[-119.656554, 39.7757854], [-119.651847, 39.7757496], [-119.651903, 39.7721366], [-119.6565908, 39.7721537], [-119.656554, 39.7757854]]


def _result(rem_acres=120.4, p1_acres=40.0):
    return {
        "anchor": {"aliquot": ALIQUOT},
        "pages": [{"page_number": 7, "regions": [{"parcels": [_parcel(REM, rem_acres), _parcel(P1, p1_acres)]}]}],
    }


def _nw(parcel):
    ring = parcel["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]
    return min(ring, key=lambda p: p[0] - p[1])


def test_fit_moves_the_real_patnaude_parcels_onto_the_described_land():
    r = _result()
    _fit_sheet_to_aliquot(r, 7)
    rem, p1 = r["pages"][0]["regions"][0]["parcels"]
    # Remainder's NW corner must now be the W 1/4 corner of Sec 17 -- the
    # monument the plat names -- within metres, not ~1 km.
    nw = _nw(rem)
    assert GEOD.inv(nw[0], nw[1], -119.6588596, 39.7747286)[2] < 30
    assert rem["placement"]["aliquot_fit"]["shift_m"] > 900
    # Shapes/shared edge untouched: P1's west corners still meet Remainder's east corners.
    rr, pr = rem["boundary_geojson_wgs84"]["geometry"]["coordinates"][0], p1["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]
    assert min(GEOD.inv(pr[0][0], pr[0][1], q[0], q[1])[2] for q in rr) < 5


def test_fit_is_idempotent():
    r = _result()
    _fit_sheet_to_aliquot(r, 7)
    once = copy.deepcopy(r)
    _fit_sheet_to_aliquot(r, 7)
    assert r == once


def test_no_fit_when_the_combined_area_does_not_match():
    r = _result(rem_acres=120.4, p1_acres=0.0)  # only one parcel's worth of area confirmed
    before = copy.deepcopy(r)
    _fit_sheet_to_aliquot(r, 7)
    assert r == before


def test_waits_while_a_parcel_is_still_pending():
    r = _result()
    r["pages"][0]["regions"][0]["parcels"][1]["placement"]["status"] = "pending"
    before = copy.deepcopy(r)
    _fit_sheet_to_aliquot(r, 7)
    assert r == before


def test_real_patnaude_fit_is_corroborated_by_area_and_extents():
    r = _result()
    _fit_sheet_to_aliquot(r, 7)
    fit = r["pages"][0]["regions"][0]["parcels"][0]["placement"]["aliquot_fit"]
    assert fit["area_error_pct"] < 1 and fit["extent_error_pct"] < 2
    assert fit["corroborated"] is True


def test_matching_area_but_wrong_extents_is_not_corroborated():
    # Same total area, but the group is a square instead of the long strip.
    side = 0.0182  # ~160 ac square at this latitude
    sq = [[-119.67, 39.775], [-119.67 + side * 1.3, 39.775], [-119.67 + side * 1.3, 39.775 - side], [-119.67, 39.775 - side], [-119.67, 39.775]]
    r = {"anchor": {"aliquot": ALIQUOT}, "pages": [{"page_number": 7, "regions": [{"parcels": [_parcel(sq, 160.4)]}]}]}
    _fit_sheet_to_aliquot(r, 7)
    fit = r["pages"][0]["regions"][0]["parcels"][0]["placement"]["aliquot_fit"]
    assert fit["corroborated"] is False


# ------------------------------------------------- common frame and containment
from app.routes.documents import _contain_in_aliquot, _unify_sheet_frame  # noqa: E402
import math  # noqa: E402


def _sheet_parcel(vertices, rotation, status, agreement, scale=1.2, label="P"):
    ring = [[-116.9 + 0.001 * i, 44.07 + 0.0005 * i] for i in range(len(vertices))]
    ring.append(ring[0])
    return {
        "vision_geometry": {"parcel_label": label},
        "human_confirmed": True,
        "confirmed_boundary_pixels": {"vertices": vertices, "id": "x"},
        "boundary_geojson_wgs84": {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}},
        "calibration": {"status": status, "rotation_deg": rotation, "scale_ft_per_px": scale, "scale_agreement_pct": agreement},
        "placement": {"status": "approximate", "notes": []},
    }


def _two_siblings():
    a = _sheet_parcel([[100, 100], [400, 100], [400, 300], [100, 300]], 1.7, "cross_validated", 0.4, label="A")
    b = _sheet_parcel([[100, 300], [400, 300], [400, 600], [100, 600]], None, "unverified", 0.9, label="B")
    return {"pages": [{"page_number": 1, "regions": [{"parcels": [a, b]}]}]}


def test_siblings_on_one_drawing_get_one_frame_and_their_shared_edge_coincides():
    r = _two_siblings()
    a, b = r["pages"][0]["regions"][0]["parcels"]
    before_a = copy.deepcopy(a["boundary_geojson_wgs84"])
    assert _unify_sheet_frame(r, 1) is True
    ra, rb = a["boundary_geojson_wgs84"]["geometry"]["coordinates"][0], b["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]
    assert len(ra) == 5 and len(rb) == 5 and ra[0] == ra[-1]                       # vertex counts/closure kept
    assert a["boundary_geojson_wgs84"]["geometry"]["coordinates"][0][0] == before_a["geometry"]["coordinates"][0][0]  # best parcel pinned
    # A's bottom edge (vertices 2,3) is B's top edge (vertices 0,1): same pixels -> same coordinates.
    assert GEOD.inv(ra[3][0], ra[3][1], rb[0][0], rb[0][1])[2] < 0.05 and GEOD.inv(ra[2][0], ra[2][1], rb[1][0], rb[1][1])[2] < 0.05
    # Rigid: B's edge lengths are pixels x the shared scale.
    assert GEOD.inv(rb[0][0], rb[0][1], rb[1][0], rb[1][1])[2] / 0.3048 == pytest.approx(300 * 1.2, rel=0.002)
    assert b["placement"]["common_frame"]["rotation_deg"] == 1.7 and b["placement"]["common_frame"]["from"] == "A"


def test_common_frame_is_idempotent_and_leaves_a_reliably_placed_sheet_alone():
    r = _two_siblings()
    _unify_sheet_frame(r, 1)
    once = copy.deepcopy(r)
    assert _unify_sheet_frame(r, 1) is False and r == once
    r2 = _two_siblings()
    r2["pages"][0]["regions"][0]["parcels"][0]["placement"]["status"] = "surveyed_corner"
    before = copy.deepcopy(r2)
    assert _unify_sheet_frame(r2, 1) is False and r2 == before


def _members(box):
    lon0, lat0, lon1, lat1 = box
    ring = [[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]
    parcel = {"placement": {"status": "approximate", "notes": []}}
    feat = {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}}
    parcel["boundary_geojson_wgs84"] = copy.deepcopy(feat)
    return [(parcel, feat)]


def test_parcels_that_stick_out_of_the_described_aliquot_are_moved_in_by_the_smallest_shift():
    aliq = {"description": "WEST 1/2 of NE 1/4 of Section 34", "acres": 80.0,
            "polygon": [[-116.9235, 44.0821], [-116.9184, 44.0821], [-116.9184, 44.0749], [-116.9235, 44.0749]]}
    members = _members((-116.9206, 44.0792, -116.9176, 44.0805))  # 0.0008 deg east of the line
    assert _contain_in_aliquot(members, aliq, 11.2) is True
    parcel = members[0][0]
    xs = [p[0] for p in parcel["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]]
    assert max(xs) == pytest.approx(-116.9184) and min(xs) > -116.9235   # touching the line, not past it
    assert parcel["boundary_geojson_wgs84"]["geometry"]["coordinates"][0][0][1] == 44.0792  # north-south untouched
    assert parcel["placement"]["aliquot_fit"]["mode"] == "containment" and parcel["placement"]["aliquot_fit"]["corroborated"] is False
    assert parcel["placement"]["status"] == "approximate" and any("constrained to lie within" in n for n in parcel["placement"]["notes"])


def test_already_inside_or_too_big_to_fit_is_left_alone():
    aliq = {"description": "x", "acres": 80.0, "polygon": [[-116.9235, 44.0821], [-116.9184, 44.0821], [-116.9184, 44.0749], [-116.9235, 44.0749]]}
    inside = _members((-116.921, 44.077, -116.920, 44.078))
    assert _contain_in_aliquot(inside, aliq, 1.0) is True and "aliquot_fit" not in inside[0][0]["placement"]
    assert inside[0][0]["boundary_geojson_wgs84"] == inside[0][1]
    too_big = _members((-116.93, 44.07, -116.91, 44.09))
    assert _contain_in_aliquot(too_big, aliq, 99.0) is False
