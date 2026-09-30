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
