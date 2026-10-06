import numpy as np
from shapely.geometry import Polygon

from app.services.region_fill import fill_region


def _sheet():
    """A white sheet with two lots side by side (solid 3px lines), some text inside lot A, and a break in
    the outer line of lot B."""
    g = np.full((400, 600), 255, np.uint8)
    g[100:103, 100:500] = 0  # top
    g[300:303, 100:500] = 0  # bottom
    g[100:303, 100:103] = 0  # left
    g[100:303, 300:303] = 0  # shared line
    g[100:303, 497:500] = 0  # right
    g[180:190, 150:240] = 0  # a label inside lot A
    g[200:204, 497:500] = 255  # a 4px break in lot B's right line
    return g


def test_one_click_fills_the_lot_it_lands_in():
    out = fill_region(_sheet(), 200, 250, gap=5)
    poly = Polygon(out["vertices"])
    lot_a = Polygon([(101, 101), (301, 101), (301, 301), (101, 301)])  # on the line centres
    assert poly.intersection(lot_a).area / poly.union(lot_a).area > 0.95
    assert len(out["vertices"]) <= 8  # simplified to its corners, the label inside is not a hole


def test_gap_bridges_a_broken_line_and_a_leak_is_refused():
    g = _sheet()
    bridged = fill_region(g, 400, 250, gap=7)
    assert "vertices" in bridged and bridged["fraction"] < 0.2
    leaked = fill_region(g, 400, 250, gap=1)  # the break lets the fill run out over the whole sheet
    assert "error" in leaked


def test_clicks_on_a_line_or_outside_are_handled():
    g = _sheet()
    assert "vertices" in fill_region(g, 301, 200, gap=5)  # on the shared line: nudged into a lot
    assert "error" in fill_region(g, 900, 200)
