"""Evidence semantics: only independently read bearings establish cross_validated rotation."""
import sys
import types

sys.modules.setdefault("paddleocr", types.SimpleNamespace(PaddleOCR=object))

from app.services import calibration  # noqa: E402
from app.services.calibration import _edge_geom  # noqa: E402
from app.services.gemini_edge_association import parse_response  # noqa: E402
from tests.test_calibration_quadrant import _cand, _lot48_poly, _sqft  # noqa: E402

SCALE = 0.3096
POLY = _lot48_poly()
SQFT = _sqft(POLY, SCALE)
# Page rotation implied by edge3's correctly read bearing N61deg53'54"E.
ROT = (61.898 - _edge_geom(POLY, 3)[1]) % 360


def _true(edge):
    """Candidate for `edge` whose distance and AS-READ bearing are both correct."""
    length_px, angle_px = _edge_geom(POLY, edge)
    return _cand(edge, length_px * SCALE, (angle_px + ROT) % 360)


def _misread(edge):
    """Correct numeric core, one quadrant letter wrong (E<->W), like real LOT 48 edge6."""
    c = _true(edge)
    return {**c, "azimuth": (360 - c["azimuth"]) % 360}


def _distance_only(edge):
    return {**_true(edge), "azimuth": None}


def _calibrate(cands):
    # Prior placement at the true rotation, as the real endpoint supplies, so the
    # 180deg walk-direction ambiguity is resolved and rotation_deg is returned.
    old = calibration.reproject_page_px_to_local(POLY, SCALE, ROT, (0, 0), (0, 0))
    return calibration.calibrate(POLY, [], SQFT, old_local_points=old, page_pivot=(0, 0),
                                 local_pivot=(0, 0), extra_candidates=cands)


def _rot_ok(res):
    return res.rotation_deg is not None and calibration._circular_diff(res.rotation_deg, ROT) < 0.5


def test_two_independent_bearings_cross_validated():
    res = _calibrate([_true(3), _true(1)])
    assert res.status == "cross_validated" and _rot_ok(res)
    assert res.independent_bearing_edges == [1, 3] and res.quadrant_resolved_edges == []


def test_quadrant_resolved_plus_one_independent_is_single_source():
    res = _calibrate([_true(3), _misread(6)])
    assert _rot_ok(res)
    assert res.quadrant_resolved_edges == [6] and res.independent_bearing_edges == [3]
    # two edges corroborate scale, but only one bearing was independently read
    assert res.corroborating_edge_count == 2 and res.status == "single_source"


def test_quadrant_resolved_plus_two_independent_cross_validated():
    res = _calibrate([_true(3), _true(1), _misread(6)])
    assert res.status == "cross_validated" and _rot_ok(res)
    assert res.independent_bearing_edges == [1, 3] and res.quadrant_resolved_edges == [6]


def test_distance_only_edge_does_not_establish_rotation():
    res = _calibrate([_true(3), _distance_only(2)])
    assert res.corroborating_edge_count == 2 and _rot_ok(res)
    assert res.independent_bearing_edges == [3] and res.status == "single_source"


def test_conflicting_reading_on_one_edge_blocks_everything():
    # real LOT 48 run1: edge1 also carries a neighbor parcel's call whose distance
    # happens to match (N22deg51'56"W) -- conflicting evidence is never resolved away
    conflict = _cand(1, _edge_geom(POLY, 1)[0] * SCALE * 1.01, 337.134)
    res = _calibrate([_true(3), _true(1), _misread(6), conflict])
    assert res.status == "unverified" and res.rotation_deg is None
    assert res.quadrant_resolved_edges == [] and res.independent_bearing_edges == []


def test_gemini_nul_degree_sign_parsed():
    out = parse_response({"edges": [
        {"edge_index": 0, "bearing_text": "N 61\x0053'54\" E", "distance_ft": 105.55},
    ]}, 7)
    assert abs(out[0]["azimuth"] - 61.8983) < 1e-3


def test_other_control_characters_still_rejected():
    out = parse_response({"edges": [
        {"edge_index": 0, "bearing_text": "N 61\x0153'54\" E", "distance_ft": 105.55},
        {"edge_index": 1, "bearing_text": "N 61\x00\x0053'54\" E", "distance_ft": 50.0},
    ]}, 7)
    assert [c["azimuth"] for c in out] == [None, None]
