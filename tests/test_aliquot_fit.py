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
