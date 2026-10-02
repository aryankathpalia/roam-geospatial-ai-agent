"""
PLSS section-corner resolver: parsing, geometry, and the single-source/
corroborated confidence gate -- see app/services/plss.py's docstring for
why each refusal (unrecognized state, no T/R/S, irregular section) exists.
"""
import json
from types import SimpleNamespace

import pytest

from app.services import plss

PATNAUDE_TEXT = (
    "FOUND 2.5\" DIAMETER GENERAL LAND OFFICE BRASS CAP BEING THE 1/4 CORNER OF "
    "SECTIONS 18 AND 17, DATE STAMPED \"1911\".\n"
    "BEING A PORTION OF THE NORTH 2 OF THE SOUTH 2 OF SECTION 17\n"
    "TOWNSHIP 22 NORTH， RANGE 21 EAST， M.D.M,\n"
    "OF WASHOE COUNTY， NEVADA.\n"
)


def _pages(text: str) -> list[dict]:
    return [{"regions": [{"ocr_text": text}]}]


def test_find_trs_handles_real_ocr_commas_and_abbreviated_meridian():
    trs = plss._find_trs(PATNAUDE_TEXT)
    assert trs is not None
    assert (trs.township_no, trs.township_dir) == (22, "N")
    assert (trs.range_no, trs.range_dir) == (21, "E")
    assert trs.meridian_name == "Mount Diablo Meridian"


def test_unrecognized_state_name_refuses_rather_than_guesses():
    assert plss.resolve_plss_anchor(_pages(PATNAUDE_TEXT), "Atlantis") is None


def test_no_township_range_section_text_refuses():
    assert plss.resolve_plss_anchor(_pages("just some unrelated OCR text"), "Nevada") is None


# --- a fake, deterministic BLM service for everything below ---

# Real PLSS sections are ~1 mile (~0.0145 deg) on a side -- these use a
# comparable 0.01 deg scale so the corroboration distance gate (a
# township-scale ~16km bound) behaves realistically against them.
_S = 0.01
SQUARE_17 = [[0.0, 0.0], [_S, 0.0], [_S, _S], [0.0, _S], [0.0, 0.0]]
# Section 18 sits immediately WEST of section 17 -- shared edge is
# section 17's west edge (x=0), from y=0 to y=_S.
SQUARE_18 = [[-_S, 0.0], [0.0, 0.0], [0.0, _S], [-_S, _S], [-_S, 0.0]]
# An IRREGULAR section (e.g. adjoining a meridian/baseline): its actual
# corners don't land near its bounding-box extremes.
IRREGULAR_19 = [[0.0, -_S], [1.3 * _S, -_S], [0.9 * _S, -0.1 * _S], [0.1 * _S, -0.05 * _S], [0.0, -_S]]


def _fake_response(payload: dict):
    resp = SimpleNamespace()
    resp.raise_for_status = lambda: None
    resp.json = lambda: payload
    return resp


def _township_payload(plssid: str = "FAKE1"):
    return {"features": [{"attributes": {"PLSSID": plssid}}]}


def _section_payload(ring: list[list[float]] | None):
    if ring is None:
        return {"features": []}
    return {"features": [{"geometry": {"rings": [ring]}}]}


@pytest.fixture()
def fake_blm(monkeypatch):
    plss._ring_cache.clear()
    calls = {"township": 0, "section": []}

    def fake_get(url, params=None, timeout=None):
        if url == plss._TOWNSHIP_LAYER:
            calls["township"] += 1
            return _fake_response(_township_payload())
        assert url == plss._SECTION_LAYER
        where = params["where"]
        calls["section"].append(where)
        if "FRSTDIVNO='17'" in where:
            return _fake_response(_section_payload(SQUARE_17))
        if "FRSTDIVNO='18'" in where:
            return _fake_response(_section_payload(SQUARE_18))
        if "FRSTDIVNO='19'" in where:
            return _fake_response(_section_payload(IRREGULAR_19))
        return _fake_response(_section_payload(None))

    monkeypatch.setattr(plss.httpx, "get", fake_get)
    return calls


def test_resolves_the_real_patnaude_two_section_quarter_corner(fake_blm):
    anchor = plss.resolve_plss_anchor(_pages(PATNAUDE_TEXT), "Nevada")
    assert anchor is not None
    # Midpoint of the shared edge (x=0, y in [0,_S]) -> (0, _S/2).
    assert anchor.lon == pytest.approx(0.0, abs=1e-9)
    assert anchor.lat == pytest.approx(0.005, abs=1e-9)
    assert "Sections 18 and 17" in anchor.description
    assert anchor.corroborated is False  # only one monument named on this sheet


def test_single_section_quarter_corner_direction(fake_blm):
    # W1/4 corner of section 17 -- midpoint of its WEST edge (x=0, y in [0,_S]).
    text = "TOWNSHIP 22 NORTH, RANGE 21 EAST, M.D.M. W1/4 CORNER OF SECTION 17"
    anchor = plss.resolve_plss_anchor(_pages(text), "Nevada")
    assert anchor is not None
    assert anchor.lon == pytest.approx(0.0, abs=1e-9)
    assert anchor.lat == pytest.approx(0.005, abs=1e-9)


def test_two_independently_named_monuments_corroborate_each_other(monkeypatch):
    plss._ring_cache.clear()

    def fake_get(url, params=None, timeout=None):
        if url == plss._TOWNSHIP_LAYER:
            return _fake_response(_township_payload())
        where = params["where"]
        if "FRSTDIVNO='17'" in where:
            return _fake_response(_section_payload(SQUARE_17))
        if "FRSTDIVNO='18'" in where:
            return _fake_response(_section_payload(SQUARE_18))
        return _fake_response(_section_payload(None))

    monkeypatch.setattr(plss.httpx, "get", fake_get)

    text = (
        "1/4 CORNER OF SECTIONS 18 AND 17\n"
        "NE CORNER OF SECTION 17\n"  # a second, independently-named monument on the same sheet
        "TOWNSHIP 22 NORTH， RANGE 21 EAST， M.D.M\n"
    )
    anchor = plss.resolve_plss_anchor(_pages(text), "Nevada")
    assert anchor is not None
    assert anchor.corroborated is True
    assert len(anchor.notes) == 2


def test_far_apart_monuments_do_not_falsely_corroborate(monkeypatch):
    plss._ring_cache.clear()
    far_square = [[50.0, 50.0], [51.0, 50.0], [51.0, 51.0], [50.0, 51.0], [50.0, 50.0]]

    def fake_get(url, params=None, timeout=None):
        if url == plss._TOWNSHIP_LAYER:
            return _fake_response(_township_payload())
        where = params["where"]
        if "FRSTDIVNO='17'" in where:
            return _fake_response(_section_payload(SQUARE_17))
        if "FRSTDIVNO='18'" in where:
            return _fake_response(_section_payload(SQUARE_18))
        if "FRSTDIVNO='20'" in where:
            return _fake_response(_section_payload(far_square))
        return _fake_response(_section_payload(None))

    monkeypatch.setattr(plss.httpx, "get", fake_get)
    text = (
        "1/4 CORNER OF SECTIONS 18 AND 17\n"
        "NE CORNER OF SECTION 20\n"  # resolves, but nowhere near the first
        "TOWNSHIP 22 NORTH， RANGE 21 EAST， M.D.M\n"
    )
    anchor = plss.resolve_plss_anchor(_pages(text), "Nevada")
    assert anchor is not None
    assert anchor.corroborated is False


def test_irregular_section_refuses_rather_than_guessing_a_corner(monkeypatch):
    plss._ring_cache.clear()

    def fake_get(url, params=None, timeout=None):
        if url == plss._TOWNSHIP_LAYER:
            return _fake_response(_township_payload())
        where = params["where"]
        if "FRSTDIVNO='19'" in where:
            return _fake_response(_section_payload(IRREGULAR_19))
        return _fake_response(_section_payload(None))

    monkeypatch.setattr(plss.httpx, "get", fake_get)
    text = "NE CORNER OF SECTION 19 TOWNSHIP 22 NORTH， RANGE 21 EAST， M.D.M\n"
    assert plss.resolve_plss_anchor(_pages(text), "Nevada") is None


def test_township_not_found_in_blm_data_refuses(monkeypatch):
    plss._ring_cache.clear()

    def fake_get(url, params=None, timeout=None):
        if url == plss._TOWNSHIP_LAYER:
            return _fake_response({"features": []})  # BLM has no such township
        return _fake_response(_section_payload(None))

    monkeypatch.setattr(plss.httpx, "get", fake_get)
    assert plss.resolve_plss_anchor(_pages(PATNAUDE_TEXT), "Nevada") is None


def test_aliquot_north_half_of_south_half_with_ocr_dropped_fraction(fake_blm):
    # Real OCR on the Patnaude packet reads "1/2" as a bare "2".
    aliquot = plss._resolve_aliquot("FAKE1", "BEING A PORTION OF THE NORTH 2 OF THE SOUTH 2 OF SECTION 17")
    assert aliquot is not None
    lons = [p[0] for p in aliquot["polygon"]]
    lats = [p[1] for p in aliquot["polygon"]]
    # Section 17 spans y 0.._S; N 1/2 of S 1/2 is the strip y = _S/4 .. _S/2, full width.
    assert min(lons) == pytest.approx(0.0) and max(lons) == pytest.approx(_S)
    assert min(lats) == pytest.approx(_S / 4) and max(lats) == pytest.approx(_S / 2)
    assert "NORTH 1/2 of SOUTH 1/2 of Section 17" == aliquot["description"]


def test_no_aliquot_phrase_returns_none(fake_blm):
    assert plss._resolve_aliquot("FAKE1", "A DIVISION OF PARCEL 17-2-1-4") is None
