import math

from app.services import apn

LAT0, LON0 = 39.6, -119.76
KX = 111_320 * math.cos(math.radians(LAT0))
KY = 110_540


def _ring(x0, y0, w, h):
    """A rectangle in metres from (LON0, LAT0), as a closed WGS84 ring."""
    pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h), (x0, y0)]
    return [(LON0 + x / KX, LAT0 + y / KY) for x, y in pts]


def test_extract_apns_normalises_and_orders_by_mentions():
    text = "A.P.N. 085-570-29 RENO INVESTMENT\nAPN: 085 500 08\nAPN 085-500-08 (subject)\nN 89 38'51\" W 300.93'"
    assert apn.extract_apns(text) == ["085-500-08", "085-570-29"]


def test_target_apn_needs_a_clear_plurality():
    assert apn.target_apn("A.P.N. 085-570-29  A.P.N. 085-570-50  A.P.N. 085-500-48") is None  # a plat: neighbours only
    assert apn.target_apn("APN: 085-500-08 ... APN 085-500-08 ... A.P.N. 085-570-29") == "085-500-08"


def test_group_is_seated_in_the_gap_between_its_neighbours():
    # the site is the 90 x 50 m gap; neighbours on three sides, a road (no parcel) on the west
    site = {"source": "test", "target_apn": None, "target": None, "neighbours": [
        {"apn": "N1", "ring": _ring(0, 50, 30, 30)}, {"apn": "N2", "ring": _ring(30, 50, 30, 30)},
        {"apn": "N3", "ring": _ring(60, 50, 30, 30)},
        {"apn": "S1", "ring": _ring(0, -30, 30, 30)}, {"apn": "S2", "ring": _ring(30, -30, 30, 30)},
        {"apn": "S3", "ring": _ring(60, -30, 30, 30)},
        {"apn": "E1", "ring": _ring(90, 0, 30, 50)},
    ]}
    # three lots placed 40 m west and 25 m north of where they belong
    lots = [_ring(-40 + 30 * k, 25, 30, 50) for k in range(3)]
    fit = apn.placement_by_apn(lots, site)
    assert abs(fit["east_m"] - 40) <= 1.0 and abs(fit["north_m"] + 25) <= 1.0, fit
    assert fit["mode"] == "between_neighbours" and fit["corroborated"], fit


def test_identical_row_of_lots_is_not_corroborated():
    # neighbours only north and south in a long uniform row: the group could slide east-west
    site = {"source": "test", "target_apn": None, "target": None, "neighbours": [
        {"apn": f"N{k}", "ring": _ring(30 * k, 50, 30, 30)} for k in range(-8, 9)
    ] + [{"apn": f"S{k}", "ring": _ring(30 * k, -30, 30, 30)} for k in range(-8, 9)]}
    lots = [_ring(5, 10, 30, 50)]
    fit = apn.placement_by_apn(lots, site)
    assert not fit["corroborated"], fit


def test_target_polygon_is_used_only_when_its_area_matches():
    lots = [_ring(10, 10, 30, 50), _ring(40, 10, 30, 50)]
    same = {"neighbours": [], "target": {"apn": "T", "ring": _ring(100, 0, 60, 50)}}
    fit = apn.placement_by_apn(lots, same)
    assert fit["mode"] == "target_parcel" and fit["corroborated"]
    assert abs(fit["east_m"] - 90) < 1 and abs(fit["north_m"] + 10) < 1
    much_bigger = {"neighbours": [], "target": {"apn": "T", "ring": _ring(100, 0, 300, 300)}}
    assert apn.placement_by_apn(lots, much_bigger)["mode"] == "between_neighbours"


def test_no_service_for_the_state_means_no_lookup():
    assert apn.resolve_apn_site("A.P.N. 085-570-29", "Ohio", 40.0, -83.0) is None
    assert apn.resolve_apn_site("no parcel numbers here", "Nevada", 39.6, -119.76) is None
