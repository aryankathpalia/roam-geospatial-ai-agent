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


PAYETTE_TEXT = (
    "A parcel of land being a portion of the W1/2 of the NE1/4 of Section 34, Township 9 North, Range 5 West of the\n"
    "Boise Meridian, Payette County, Idaho. COMMENCING at the NE1/16 corner of said Section 34, marked by an aluminum cap,\n"
    "from which the CE1/16 corner of said Section 34 bears South 00 24'53\" West, 1305.74 feet.\n"
    "BASIS OF BEARING is between the NE1/16 corner and the CE1/16 corner of Section 34, T. 9 N., R. 5 W., B.M.\n"
)


def test_trs_long_form_with_of_the_lead_in_and_the_abbreviated_heading_both_parse():
    long_form = plss._find_trs(PAYETTE_TEXT.split("Idaho.")[0])
    assert long_form.meridian_name == "Boise Meridian" and (long_form.township_no, long_form.range_no) == (9, 5)
    assert (long_form.township_dir, long_form.range_dir) == ("N", "W")
    for heading in ("SECTION34. T.9 N.. R.5W.. B.M.", "Section 34, T. 9 N., R. 5 W., B.M., Idaho"):
        abbr = plss._find_trs(heading)
        assert abbr is not None and abbr.meridian_name == "Boise Meridian" and (abbr.township_no, abbr.range_no) == (9, 5)


def test_sixteenth_corners_resolve_and_the_pair_is_corroborated(fake_blm):
    anchor = plss.resolve_plss_anchor(_pages(PAYETTE_TEXT.replace("Section 34", "Section 17")), "Idaho")
    assert anchor is not None and anchor.corroborated is True  # two independently named monuments
    # NE1/16 = centre of the NE 1/4: (0.75, 0.75) of the 0.01-degree test section; CE1/16 at (0.75, 0.5).
    assert (anchor.lon, anchor.lat) == pytest.approx((0.0075, 0.0075), abs=1e-9)
    assert anchor.description == "NE1/16 corner of Section 17" and len(anchor.notes) == 2


def test_the_two_named_monuments_are_a_quarter_section_apart_like_the_printed_distance():
    # Printed 1,305.74 ft for a ~1,320 ft quarter side: the geometry the resolver assumes for NE1/16 -> CE1/16.
    ne, ce = plss._SIXTEENTH["NE"], plss._SIXTEENTH["CE"]
    assert (ne[0] - ce[0], ne[1] - ce[1]) == (0.0, 0.25)


def test_abbreviated_aliquot_is_read_as_the_west_half_of_the_ne_quarter_and_flagged_as_a_portion(fake_blm):
    plss._ring_cache.clear()
    text = "being a portion of the W1/2 of the NE1/4 of Section 17, Township 9 North"
    aliquot = plss._resolve_aliquot("FAKE1", text)
    assert aliquot["description"] == "WEST 1/2 of NE 1/4 of Section 17" and aliquot["portion_of"] is True
    lons = [p[0] for p in aliquot["polygon"]]
    lats = [p[1] for p in aliquot["polygon"]]
    # section 17 = (0..S) x (0..S): the NE quarter is (S/2..S)^2, its west half u in (S/2 .. 3S/4)
    assert (min(lons), max(lons)) == pytest.approx((0.005, 0.0075)) and (min(lats), max(lats)) == pytest.approx((0.005, 0.01))
    assert plss._resolve_aliquot("FAKE1", "THE SOUTH 2 OF SECTION 17")["portion_of"] is False


def test_find_trs_accepts_ampersand_meridian_abbreviation():
    for text in ("Section 21, T13S, R12E, G&SRM, Pima County", "T13S, R12E, G & S.R.M."):
        trs = plss._find_trs(text)
        assert (trs.township_no, trs.township_dir, trs.range_no, trs.range_dir) == (13, "S", 12, "E")
        assert trs.meridian_name == "Gila-Salt River Meridian"


def test_spelled_out_corner_monuments_and_base_and_meridian():
    text = (
        "Section 21, Township 13 South, Range 12 East of the Gila and Salt River Base and Meridian. "
        "Beginning at the North One Quarter corner of said Section 21, from which the Northeast "
        "corner of said Section 21, bears North 89 East"
    )
    assert plss._find_trs(text).meridian_name == "Gila and Salt River Meridian"
    assert [m.group(1).upper() for m in plss._ONE_SECTION_QUARTER_RE.finditer(text)] == ["NORTH"]
    assert [m.group(1).upper() for m in plss._FULL_CORNER_RE.finditer(text)] == ["NORTHEAST"]
    # "Northeast Quarter of the Northeast Quarter of Section 21" names no corner monument
    assert not list(plss._ONE_SECTION_QUARTER_RE.finditer(
        "the Northeast Quarter of the Northeast Quarter of Section 21"))
