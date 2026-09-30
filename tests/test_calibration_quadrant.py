"""Quadrant-letter disambiguation fallback in calibrate() (real LOT 48 geometry)."""
import math
import random
import sys
import types

sys.modules.setdefault("paddleocr", types.SimpleNamespace(PaddleOCR=object))

from app.services import calibration  # noqa: E402
from app.services.calibration import _edge_geom, _resolve_quadrant_ambiguity  # noqa: E402

# MAP 7 LOT 48 confirmed polygon (page px, crop-relative; only shape/angles matter)
LOT48 = [(0, 0), (-95, -300)]
V = None


def _lot48_poly():
    # angles/lengths of the confirmed 7-edge polygon, rebuilt from edge geometry
    edges = [(110.3, 204.52), (525.4, 282.06), (679.8, 0.06), (340.3, 86.46),
             (33.8, 91.55), (736.2, 148.48), (215.6, 247.72)]
    pts = [(0.0, 0.0)]
    for L, a in edges[:-1]:
        x, y = pts[-1]
        pts.append((x + L * math.sin(math.radians(a)), y - L * math.cos(math.radians(a))))
    return pts


def _cand(edge, value, az):
    return {"edge_index": edge, "value": value, "azimuth": az, "source": "gemini_association"}


def _sqft(poly, scale):
    return calibration._polygon_area(poly) * scale * scale


def test_flipped_quadrant_resolved_against_other_edge():
    poly = _lot48_poly()
    scale = 0.3096
    sq = _sqft(poly, scale)
    e3 = _edge_geom(poly, 3)[0] * scale
    e6 = _edge_geom(poly, 6)[0] * scale
    # edge3 read correctly; edge6 numeric core right, quadrant letter wrong (E instead of W)
    res = calibration.calibrate(poly, [], sq, extra_candidates=[
        _cand(3, e3, 61.898), _cand(6, e6, 137.553),
    ])
    assert res.corroborating_edge_count == 2
    flagged = [c for c in res.corroborations if c.get("quadrant_resolved")]
    assert [c["edge_index"] for c in flagged] == [6]
    assert abs(flagged[0]["azimuth"] - 222.447) < 0.01 and abs(flagged[0]["azimuth_as_read"] - 137.553) < 0.01
    # no prior placement -> stops at the 180deg ambiguity, but rotation was reconciled
    assert any("180deg" in n for n in res.notes)


def test_single_bearing_never_flipped():
    poly = _lot48_poly()
    scale = 0.3096
    e6 = _edge_geom(poly, 6)[0] * scale
    res = calibration.calibrate(poly, [], _sqft(poly, scale), extra_candidates=[_cand(6, e6, 137.553)])
    assert not any(c.get("quadrant_resolved") for c in res.corroborations)


def test_conflicting_stray_bearing_blocks_resolution():
    poly = _lot48_poly()
    scale = 0.3096
    sq = _sqft(poly, scale)
    e3, e6, e1 = (_edge_geom(poly, i)[0] * scale for i in (3, 6, 1))
    res = calibration.calibrate(poly, [], sq, extra_candidates=[
        _cand(3, e3, 61.898), _cand(6, e6, 137.553), _cand(1, e1, 337.134),
    ])
    assert res.status == "unverified" and res.rotation_deg is None
    assert not any(c.get("quadrant_resolved") for c in res.corroborations)
    assert any("not applied" in n for n in res.notes)


def test_unrelated_bearings_stay_unresolved():
    items = [{"edge": 0, "read": 10.0, "alt": 170.0}, {"edge": 1, "read": 100.0, "alt": 80.0}]
    # 10/170 vs 100/80: 170 vs 100 no, 10 vs 80 no ... nothing within tolerance
    assert _resolve_quadrant_ambiguity(items)[0] is None


def test_false_agreement_rate_bounded():
    # Random unrelated 2-bearing pairs: how often does the fallback manufacture agreement
    # where the plain as-read check does not?
    rnd = random.Random(1)
    n, extra = 20000, 0
    for _ in range(n):
        az_a, az_b = rnd.uniform(0, 360), rnd.uniform(0, 360)
        th_a, th_b = rnd.uniform(0, 360), rnd.uniform(0, 360)  # each edge's own pixel angle
        a, b = (az_a - th_a) % 180, (az_b - th_b) % 180
        items = [{"edge": 0, "read": a, "alt": (-az_a - th_a) % 180},
                 {"edge": 1, "read": b, "alt": (-az_b - th_b) % 180}]
        if calibration._circular_diff(a, b, 180) <= calibration._ROTATION_AGREEMENT_TOLERANCE_DEG:
            continue
        if _resolve_quadrant_ambiguity(items)[0] is not None:
            extra += 1
    print("extra false agreement rate:", extra / n)
    assert extra / n < 0.055  # no looser than the as-read check (~5.6%)
