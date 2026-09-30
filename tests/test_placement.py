"""Absolute placement: anchor identity, vertex binding, CRS, on the real NVZ page 9 layout."""
import json
import math
import sys
import types
from pathlib import Path

sys.modules.setdefault("paddleocr", types.SimpleNamespace(PaddleOCR=object))

from pyproj import Transformer  # noqa: E402

from app.services import placement as pl  # noqa: E402
from app.services.calibration import reproject_page_px_to_local  # noqa: E402
from app.services.georeference import _E_COORD_RE, _N_COORD_RE  # noqa: E402
from app.services.ocr import OCRLine  # noqa: E402

LINES = [OCRLine(d["text"], 1.0, tuple(d["bbox"]))
         for d in json.loads((Path(__file__).parent / "fixtures" / "nvz_page9_ocr_lines.json").read_text())]
# Exactly what the NVZ sheet prints about its coordinate system.
SHEET = ("THE BASIS OF BEARINGS FOR THIS SURVEY IS NEVADA STATE PLANE, WEST ZONE\n"
         "NAD83(94) BASED ON REAL TIME KINEMATIC (RTK) GPS OBSERVATIONS\n"
         "COORDINATES AND DISTANCES HEREON ARE AT GROUND LEVEL BASED ON A\n"
         "COMBINED GRID TO GROUND FACTOR OF 1.000197939.")
F = 1.000197939
# Confirmed polygons (page px) and live-calibrated scale/rotation.
P1 = [(1243.34, 807.25), (1824.3, 816.4), (1827.95, 438.16), (1248.1, 429.35)]
P1_CAL = (0.59438, 0.1469)
P2 = [(472.36, 774.66), (474.88, 439.92), (1318.96, 450.73), (1310.85, 787.28)]
P2_CAL = (0.6548, 359.907)
P1_NE = (14926897.47, 2252502.76)  # printed "PER THIS MAP"
P2_NW = (14926910.28, 2251599.70)  # printed "PER THIS MAP"


def _place(poly, cal, lines=LINES, sheet=SHEET):
    return pl.place(poly, reproject_page_px_to_local(poly, cal[0], cal[1], (0, 0), (0, 0)), lines, sheet)


def _ground_ne(lonlat):
    e, n = Transformer.from_crs(4326, 3423, always_xy=True).transform(*lonlat)
    return n * F, e * F


def test_colonless_coordinates_match_but_bearings_and_distances_do_not():
    assert _N_COORD_RE.findall("N 14926910.28") == ["14926910.28"]
    assert _E_COORD_RE.findall("E2251599.70") == ["2251599.70"]
    for text in ("N 22 51'56\" W", "N89°11'15\"W", "E 903.15"):
        assert not _N_COORD_RE.findall(text) and not _E_COORD_RE.findall(text)


def test_monuments_are_never_parcel_corners():
    kinds = {(p.northing, p.easting): p.kind for p in pl.extract_coordinate_pairs(LINES)}
    # Washoe County control points (section corner, 1/4 corner), measured AND record values
    for ne in [(14928989.71, 2253610.44), (14928989.62, 2253610.74),
               (14926352.62, 2253573.04), (14926352.55, 2253573.13)]:
        assert kinds[ne] == "monument"
    assert kinds[P1_NE] == "parcel_corner" and kinds[P2_NW] == "parcel_corner"


def test_crs_resolved_only_from_what_the_sheet_states():
    crs, _ = pl.resolve_crs(SHEET)
    assert crs["epsg"] == 3423 and crs["grid_to_ground_factor"] == F and crs["ground"]
    assert pl.resolve_crs(SHEET.replace("WEST ZONE", ""))[0] is None
    no_factor = SHEET.replace("COMBINED GRID TO GROUND FACTOR OF 1.000197939.", "")
    assert pl.resolve_crs(no_factor)[0] is None  # ground stated, factor missing -> refuse


def test_nvz_parcel1_bound_to_its_printed_ne_corner():
    res = _place(P1, P1_CAL)
    assert res.status == pl.PLACEMENT_SURVEYED_CORNER and res.details["anchor_vertex"] == 2
    ring = res.geojson["geometry"]["coordinates"][0]
    n, e = _ground_ne(ring[2])
    assert abs(n - P1_NE[0]) < 0.05 and abs(e - P1_NE[1]) < 0.05
    # Independent check: P1's NW corner lies 550.75 ft (Parcel 2's width) along the
    # printed line from Parcel 2's printed NW corner to P1's printed NE corner.
    L = math.dist(P2_NW, P1_NE)
    pred = tuple(P2_NW[k] + (P1_NE[k] - P2_NW[k]) * 550.75 / L for k in (0, 1))
    assert math.dist(_ground_ne(ring[3]), pred) < 10.0


def test_nvz_parcels_placed_from_different_corners_share_their_boundary():
    r1, r2 = _place(P1, P1_CAL), _place(P2, P2_CAL)
    assert r1.status == r2.status == pl.PLACEMENT_SURVEYED_CORNER
    assert r2.details["anchor_vertex"] == 1
    g1 = [_ground_ne(p) for p in r1.geojson["geometry"]["coordinates"][0]]
    g2 = [_ground_ne(p) for p in r2.geojson["geometry"]["coordinates"][0]]
    assert math.dist(g1[3], g2[2]) < 10.0  # P1 NW == P2 NE
    assert math.dist(g1[0], g2[3]) < 10.0  # P1 SW == P2 SE


def test_monuments_alone_never_place_a_parcel():
    lines = [ln for ln in LINES if "PER THIS MAP" not in ln.text.upper()]
    res = _place(P1, P1_CAL, lines=lines)
    assert res.status == pl.PLACEMENT_APPROXIMATE and res.geojson is None


def test_unstated_crs_never_places_a_parcel():
    res = _place(P1, P1_CAL, sheet="NO COORDINATE SYSTEM NOTE")
    assert res.status == pl.PLACEMENT_APPROXIMATE and res.geojson is None


def test_disagreeing_second_corner_blocks_placement():
    # A second "PER THIS MAP" coordinate beside P1's SE corner (v1), 60 ft off its true spot.
    x, y = P1[1]
    fake = [OCRLine("GROUND COORDINATES", 1, (x + 10, y + 5, x + 90, y + 15)),
            OCRLine("N14926612.00", 1, (x + 10, y + 20, x + 90, y + 30)),
            OCRLine("E2252500.00", 1, (x + 10, y + 32, x + 90, y + 42)),
            OCRLine("PER THIS MAP", 1, (x + 10, y + 44, x + 90, y + 54))]
    res = _place(P1, P1_CAL, lines=LINES + fake)
    assert res.status == pl.PLACEMENT_APPROXIMATE and any("disagree" in n for n in res.notes)
