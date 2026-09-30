"""Mock-based tests: Gemini candidates must go through calibrate()'s normal checks."""
import sys
import types

from PIL import Image

# calibration only needs OCRLine; don't require the heavy PaddleOCR install to test it.
sys.modules.setdefault("paddleocr", types.SimpleNamespace(PaddleOCR=object))

from app.services import calibration  # noqa: E402
from app.services.gemini_edge_association import parse_response, render_polygon_overlay

# 1000x600 px rectangle; stated area implies 0.5 ft/px -> 500' x 300'.
POLY = [(100, 100), (1100, 100), (1100, 700), (100, 700)]
SQFT = 500 * 300


def _g(edge, value, bearing=None):
    return {"edge_index": edge, "value": value, "azimuth": bearing, "source": "gemini_association"}


def test_parse_response_drops_bad_items():
    out = parse_response({"edges": [
        {"edge_index": 0, "bearing_text": "N 90°00'00\" E", "distance_ft": 500},
        {"edge_index": 9, "distance_ft": 10},          # out of range
        {"edge_index": 1, "bearing_text": "N 1 E"},    # no distance
        {"edge_index": 2, "distance_ft": -4},          # invalid
    ]}, 4)
    assert [c["edge_index"] for c in out] == [0]
    assert out[0]["source"] == "gemini_association"


def test_overlay_draws_only_on_copy():
    img = Image.new("RGB", (400, 300), "white")
    out = render_polygon_overlay(img, [(50, 50), (300, 50), (300, 250)])
    assert out.size == img.size and out.getpixel((150, 50)) != (255, 255, 255)
    assert img.getpixel((150, 50)) == (255, 255, 255)


def test_gemini_candidates_corroborate_and_are_tagged():
    # north edge (0) and east edge (1): bearings consistent w/ rotation 0 (px angle 0 / 90)
    res = calibration.calibrate(POLY, [], SQFT, extra_candidates=[
        _g(0, 500, 0.0), _g(1, 300, 90.0),
    ])
    assert res.corroborating_edge_count == 2
    assert {c["source"] for c in res.corroborations} == {"gemini_association"}
    assert res.scale_from_edges and abs(res.scale_from_edges - 0.5) < 1e-6


def test_gemini_wrong_value_is_rejected_like_ocr():
    # 25% off the area-predicted 500' -> fails the 20% prefilter; must not corroborate
    res = calibration.calibrate(POLY, [], SQFT, extra_candidates=[_g(0, 650, 0.0)])
    assert res.corroborating_edge_count == 0 and res.status == "unverified"


def test_gemini_value_inside_prefilter_but_outside_3pct_not_corroborating():
    # 10% off: passes prefilter so it skews scale_from_edges, and then
    # scale-agreement (5%) must reject rather than accept it.
    res = calibration.calibrate(POLY, [], SQFT, extra_candidates=[_g(0, 550, 0.0)])
    assert res.status == "unverified" and res.rotation_deg is None
    assert any("disagree" in n for n in res.notes)


def test_no_extra_candidates_unchanged_behavior():
    res = calibration.calibrate(POLY, [], SQFT)
    assert res.status == "unverified" and res.corroborations == []


# --- quadrant-letter disambiguation (real MAP 7 LOT 48 geometry + real Gemini readings) ---
LOT48 = [
    (1704.616, 1063.383), (1658.866, 1163.697), (1145.091, 1053.975), (1145.815, 374.164),
    (1485.468, 353.150), (1519.217, 354.062), (1904.143, 981.626),
]
_S = 0.3102  # ft/px implied by the printed 105.55' on edge3
LOT48_SQFT = calibration._polygon_area(LOT48) * _S * _S
_E3 = 61.89833333  # N 61 53'54" E, rotation 335.44
_E6_AS_READ = 137.55333333  # S 42 26'48" E as Gemini read it; printed letter is W (222.4467)
_E1_AGREES, _E1_INTRUDER = 257.65888889, 337.13444444


def _lot48(e6=_E6_AS_READ, edge1=()):
    c = [_g(3, 105.55, _E3), _g(6, 66.91, e6)]
    c += [_g(1, v, az) for v, az in edge1]
    return calibration.calibrate(LOT48, [], LOT48_SQFT, extra_candidates=c)


def test_quadrant_flip_resolves_wrong_letter_then_needs_direction_reference():
    r = _lot48()
    fixed = [c for c in r.corroborations if c.get("quadrant_resolved")]
    assert [c["edge_index"] for c in fixed] == [6]
    assert abs(fixed[0]["azimuth"] - 222.4467) < 0.01 and fixed[0]["as_read_azimuth"] == _E6_AS_READ
    # rotation agreed (edges 3+6); only the 180deg direction is unresolvable without old_local_points
    assert r.corroborating_edge_count == 2
    assert any("quadrant disambiguation" in n for n in r.notes)
    assert any("180deg" in n for n in r.notes)


def test_quadrant_flip_reaches_cross_validated_with_direction_reference():
    # old placement consistent with rotation ~335 (old_local_points from the correct reprojection)
    page_pivot = (1500.0, 700.0)
    truth = calibration.reproject_page_px_to_local(LOT48, _S, 335.0, page_pivot, (0.0, 0.0))
    r = calibration.calibrate(
        LOT48, [], LOT48_SQFT, old_local_points=truth, page_pivot=page_pivot, local_pivot=(0.0, 0.0),
        extra_candidates=[_g(3, 105.55, _E3), _g(6, 66.91, _E6_AS_READ)],
    )
    assert r.status == "cross_validated" and abs(r.rotation_deg - 335.09) < 1.0


def test_conflicted_edge_excluded_not_resolved():
    r = _lot48(edge1=[(164.41, _E1_AGREES), (164.66, _E1_INTRUDER)])
    flagged = [c for c in r.corroborations if c["edge_index"] == 1]
    assert flagged and all(c.get("conflicted") for c in flagged)
    assert 1 not in [c["edge_index"] for c in r.corroborations if not c.get("conflicted")]
    assert r.corroborating_edge_count == 2
    assert any("edge1" in n and "excluded" in n for n in r.notes)


def test_unrelated_disagreeing_bearings_stay_unverified():
    # mirroring neither edge makes these agree -> no manufactured agreement
    r = _lot48(e6=20.0)
    assert r.status == "unverified" and not any(c.get("quadrant_resolved") for c in r.corroborations)


def test_ambiguous_flip_is_not_guessed():
    # two entries that would each agree if the OTHER were mirrored -> symmetric, must flag
    e = [{"edge": 0, "azimuth": 30.0, "angle_px": 0.0}, {"edge": 1, "azimuth": 330.0, "angle_px": 60.0}]
    _, notes = calibration._resolve_quadrants([dict(x, corr={}) for x in e])
    assert not any("accepted" in n for n in notes)
