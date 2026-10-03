"""
Two-control-point placement fallback (app/services/control_points.py + the glue in
routes/documents.py): extraction with missing-component recovery, the vertex-correspondence solver
with its 180-degree tie-break and independent residual check, and the sheet-level fit.
Real data: Derry NH Map 7 Lot 48 / 48-3 (tests/fixtures/derry_lot48_control_points.json).
"""
import copy
import json
import math
import sys
import types
from pathlib import Path

sys.modules.setdefault("paddleocr", types.SimpleNamespace(PaddleOCR=object))

import pytest  # noqa: E402
from PIL import Image  # noqa: E402
from pyproj import Geod  # noqa: E402

from app.routes import documents as docs  # noqa: E402
from app.services import control_points as cp  # noqa: E402
from app.services.ocr import OCRLine  # noqa: E402

GEOD = Geod(ellps="WGS84")
FX = json.loads((Path(__file__).parent / "fixtures" / "derry_lot48_control_points.json").read_text())
CAND = FX["rotation_ambiguous_candidates_deg"]


def _tile_lines():
    return [OCRLine(t["text"], t["conf"], tuple(t["bbox"])) for t in FX["tile_ocr_lines"]]


def _points():
    return cp.extract_control_points(_tile_lines(), "ocr_recovered", positions_trusted=True)


def _all_vertices():
    return [tuple(v) for p in FX["parcels"] for v in p["vertices"]]


def _parcel(p, status="approximate", **placement_extra):
    # the stale pre-fit placement: a ring around the document anchor, as the crop-centre pin produced
    ring = [[FX["anchor_lon"] + 0.0005 * i, FX["anchor_lat"] + 0.0002 * i] for i in range(len(p["vertices"]))]
    ring.append(ring[0])
    return {
        "vision_geometry": {"parcel_label": p["label"]},
        "human_confirmed": True,
        "confirmed_boundary_pixels": {"vertices": p["vertices"], "id": "x"},
        "boundary_geojson_wgs84": {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}},
        "calibration": {
            "status": "unverified", "rotation_deg": None, "scale_ft_per_px": p["scale_ft_per_px"],
            "scale_agreement_pct": p["scale_agreement_pct"], "rotation_ambiguous_candidates_deg": CAND,
        },
        "placement": {"status": status, "notes": [], **placement_extra},
    }


def _result(points=True, **kw):
    r = {
        "anchor_lat": FX["anchor_lat"], "anchor_lon": FX["anchor_lon"],
        "anchor": {"precision": "surveyed", "source": "state-plane coordinate printed on the document"},
        "pages": [{"page_number": 13, "regions": [{"class": "ParcelMap", "bbox": [0, 0, 3059.2, 1836.8], "parcels": [_parcel(p, **kw) for p in FX["parcels"]]}]}],
    }
    if points:
        r["pages"][0]["control_points"] = {"points": [c.to_dict() for c in _points()], "tile_pass": True}
    return r


def _parcels(r):
    return r["pages"][0]["regions"][0]["parcels"]


def _ring(parcel):
    return parcel["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]


# ---------------------------------------------------------------- extraction

def test_extraction_reads_both_points_ignores_the_sideways_duplicate_and_keeps_evidence():
    pts = _points()
    assert [(p.northing, p.easting) for p in pts] == [(137042.75, 1101226.83), (136983.48, 1101681.09)]
    # The drill-hole label's tall sideways misreading (x~1830) must not become its position;
    # the real, horizontal one is at x~2435.
    assert pts[1].label_px[0] > 2300
    assert all(p.source == "ocr_recovered" and p.evidence[0].startswith("N ") for p in pts)


def test_missing_e_line_is_recovered_by_a_tiled_reread_and_original_ocr_is_kept():
    # First pass: the N line only (the real failure: the E line is dropped).
    first = [OCRLine("N:137,042.75", 0.98, (610, 615, 700, 630)), OCRLine("Z:289.78", 0.97, (610, 645, 670, 660))]
    first_pairs = cp.extract_control_points(first, "ocr", positions_trusted=False)
    assert first_pairs == [] and cp.labels_incomplete(first, first_pairs)

    calls = []

    def fake_ocr(img):  # tile at (600, 600), seen at 2x
        calls.append(img.size)
        if len(calls) == 5:
            return [
                OCRLine("N:137,042.75", 0.97, (20, 30, 200, 60)),
                OCRLine("E:1,101,226.83", 0.9, (20, 62, 200, 92)),
            ], True
        return [], True

    lines = cp.recover_control_points(Image.new("RGB", (1500, 900), "white"), fake_ocr)
    assert [(l.text, l.bbox) for l in lines] == [
        ("N:137,042.75", (610.0, 615.0, 700.0, 630.0)), ("E:1,101,226.83", (610.0, 631.0, 700.0, 646.0)),
    ]  # positions mapped back to true page pixels
    recovered = cp.extract_control_points(lines, "ocr_recovered", positions_trusted=True)
    assert len(recovered) == 1 and (recovered[0].northing, recovered[0].easting) == (137042.75, 1101226.83)
    assert first[0].text == "N:137,042.75"  # the first pass's evidence is untouched


def test_nothing_is_invented_when_the_e_value_is_still_unreadable():
    only_n = [OCRLine("N:137,042.75", 0.98, (610, 615, 700, 630))]
    assert cp.extract_control_points(only_n, "ocr_recovered") == []


def test_crs_is_identified_by_the_documents_own_anchor_not_a_state_name():
    assert cp.resolve_crs_from_anchor(_points(), FX["anchor_lat"], FX["anchor_lon"]) == 3437  # NH ftUS
    assert cp.resolve_crs_from_anchor(_points(), 10.0, 10.0) is None


# -------------------------------------------------------------------- solver

def test_lot_48_two_control_points_validate_with_the_observed_residual():
    sol = cp.solve_control_points(_points(), _all_vertices(), FX["parcels"][0]["scale_ft_per_px"], rotation_candidates_deg=CAND)
    assert sol.status == "validated"
    assert 2.0 <= sol.residual_ft <= 3.2 and sol.residual_ft < sol.tolerance_ft  # the observed ~2.6 ft
    assert sol.rotation_source == "printed_bearings"
    assert sol.separation_ft == pytest.approx(458.1, abs=0.2)


def test_180_degree_ambiguity_is_broken_by_label_positions_not_guessed():
    pts, verts, scale = _points(), _all_vertices(), FX["parcels"][0]["scale_ft_per_px"]
    sol = cp.solve_control_points(pts, verts, scale, rotation_candidates_deg=CAND)
    assert sol.details["candidate_correspondences"] == 2  # the reflected assignment fits the distances too
    # Correct: the iron pipe is the vertex next to its label (v0); the opposite end holds the drill hole.
    assert math.dist(sol.anchor_vertex_px, (1147, 1052)) < 40 and sol.rotation_deg == pytest.approx(335.9, abs=0.01)
    # Swap the labels' positions: the reflected correspondence now wins and the rotation flips 180 degrees.
    swapped = copy.deepcopy(pts)
    swapped[0].label_px, swapped[1].label_px = pts[1].label_px, pts[0].label_px
    flipped = cp.solve_control_points(swapped, verts, scale, rotation_candidates_deg=CAND)
    assert flipped.status == "validated" and flipped.rotation_deg == pytest.approx(155.9, abs=0.01)


def test_without_label_positions_the_tie_is_reported_ambiguous_not_resolved():
    pts = _points()
    for p in pts:
        p.label_px = None
    sol = cp.solve_control_points(pts, _all_vertices(), FX["parcels"][0]["scale_ft_per_px"], rotation_candidates_deg=CAND)
    assert sol.status == "ambiguous" and sol.rotation_deg is None


def test_labels_that_do_not_favour_one_correspondence_are_ambiguous():
    pts = _points()
    pts[0].label_px = pts[1].label_px = (1800.0, 1400.0)  # equidistant from both ends
    sol = cp.solve_control_points(pts, _all_vertices(), FX["parcels"][0]["scale_ft_per_px"], rotation_candidates_deg=CAND)
    assert sol.status == "ambiguous"


def test_wrong_correspondence_is_unverified():
    pts, verts = _points(), _all_vertices()
    scale = FX["parcels"][0]["scale_ft_per_px"]
    # (a) a control separation no pair of vertices matches
    far = copy.deepcopy(pts)
    far[1].easting += 400
    assert cp.solve_control_points(far, verts, scale, rotation_candidates_deg=CAND).status == "unverified"
    # (b) printed bearings that contradict the control vector (rotation far from both candidates)
    assert cp.solve_control_points(pts, verts, scale, rotation_candidates_deg=[100.0, 280.0]).status == "unverified"
    # (c) residual above tolerance: control B displaced so the distance is still within 2.5% but B misses
    off = copy.deepcopy(pts)
    off[1].northing += 9.0
    off[1].easting -= 1.0
    assert cp.solve_control_points(off, verts, scale, rotation_candidates_deg=CAND).status == "unverified"
    # (d) one control point, or no scale
    assert cp.solve_control_points(pts[:1], verts, scale).status == "unverified"
    assert cp.solve_control_points(pts, verts, None).status == "unverified"


def test_without_printed_bearings_rotation_comes_from_the_control_vector():
    sol = cp.solve_control_points(_points(), _all_vertices(), FX["parcels"][0]["scale_ft_per_px"])
    assert sol.status == "validated" and sol.rotation_source == "control_vector"
    assert sol.rotation_deg == pytest.approx(335.9, abs=2.5)


# ----------------------------------------------------------- sheet-level fit

def test_fit_places_every_parcel_keeps_topology_and_shares_the_common_corner():
    r = _result()
    before = [copy.deepcopy(_ring(p)) for p in _parcels(r)]
    assert docs._fit_sheet_to_control_points(r, 13) is True
    parcels = _parcels(r)
    for p, b, src in zip(parcels, before, FX["parcels"]):
        ring = _ring(p)
        n = len(src["vertices"])
        assert len(ring) == n + 1 and ring[0] == ring[-1]  # same vertex count, still closed
        # A rigid transform: every vertex-to-vertex distance equals pixels x calibrated scale.
        scale = p["placement"]["control_fit"]["scale_ft_per_px"]
        for i in range(n):
            j = (i + 1) % n
            want = math.dist(src["vertices"][i], src["vertices"][j]) * scale
            got = GEOD.inv(ring[i][0], ring[i][1], ring[j][0], ring[j][1])[2] / 0.3048
            assert got == pytest.approx(want, rel=0.003, abs=0.3)
        assert p["placement"]["status"] == "surveyed_corner" and p["placement"]["control_fit"]["validated"] is True
        assert p["placement"]["control_fit"]["residual_ft"] < 3.2
        assert p["confirmed_boundary_pixels"]["vertices"] == src["vertices"]  # the confirmed polygon is untouched
    # Adjacent lots drawn with a shared corner (iron pipe) coincide because both ride one transform.
    gap = min(GEOD.inv(a[0], a[1], b[0], b[1])[2] for a in _ring(parcels[0]) for b in _ring(parcels[1]))
    assert gap < 0.5
    # The drill hole's printed coordinate lands on a vertex of Lot 48-3.
    to_ll = __import__("pyproj").Transformer.from_crs(3437, 4326, always_xy=True)
    lon, lat = to_ll.transform(1101681.09, 136983.48)
    assert min(GEOD.inv(lon, lat, v[0], v[1])[2] for v in _ring(parcels[0])) < 1.5  # < ~5 ft


def test_fit_is_idempotent_and_survives_a_second_parcel_being_confirmed_later():
    r = _result()
    docs._fit_sheet_to_control_points(r, 13)
    once = copy.deepcopy(r)
    assert docs._fit_sheet_to_control_points(r, 13) is True
    assert r == once


def test_existing_trusted_placement_is_left_unchanged():
    for extra in (
        {"status": "surveyed_corner"},  # placement.py bound a vertex to a printed parcel corner
        {"status": "approximate", "aliquot_fit": {"corroborated": True}},  # corroborated aliquot fit
    ):
        r = _result()
        for p in _parcels(r):
            p["placement"].update(extra)
        before = copy.deepcopy(r)
        assert docs._fit_sheet_to_control_points(r, 13) is False
        assert r == before
    r = _result()
    _parcels(r)[0]["anchor_override"] = {"lat": 1.0, "lon": 2.0}  # hand-pinned
    before = copy.deepcopy(r)
    assert docs._fit_sheet_to_control_points(r, 13) is False and r == before


def test_not_applied_while_verification_is_still_pending_or_without_a_printed_state_plane_anchor():
    r = _result()
    _parcels(r)[1]["placement"]["status"] = "pending"
    before = copy.deepcopy(r)
    assert docs._fit_sheet_to_control_points(r, 13) is False and r == before
    r = _result()
    r["anchor"]["source"] = 'geocoded "0 Ironwood Road, Nevada"'
    before = copy.deepcopy(r)
    assert docs._fit_sheet_to_control_points(r, 13) is False and r == before


def test_unresolvable_sheet_stays_approximate_and_records_why():
    r = _result()
    for c in r["pages"][0]["control_points"]["points"]:
        c["label_px"] = None  # positions unavailable: the 180-degree tie cannot be broken
    before_rings = [copy.deepcopy(_ring(p)) for p in _parcels(r)]
    assert docs._fit_sheet_to_control_points(r, 13) is False
    for p, b in zip(_parcels(r), before_rings):
        assert _ring(p) == b and p["placement"]["status"] == "approximate"
        assert p["placement"]["control_fit"]["validated"] is False and p["placement"]["control_fit"]["status"] == "ambiguous"


def test_ensure_control_points_reads_recovers_caches_and_fits(tmp_path, monkeypatch):
    doc = "ctrl-doc"
    (tmp_path / doc / "pages").mkdir(parents=True)
    Image.new("RGB", (3400, 2200), "white").save(tmp_path / doc / "pages" / "page_013.png")
    r = _result(points=False)
    (tmp_path / doc / "result.json").write_text(json.dumps(r))
    monkeypatch.setattr(docs, "DOCUMENT_ROOT", tmp_path)
    # First pass: both N lines, no E lines (sideways boxes, positions not trusted).
    first = [OCRLine("N:137,042.75", 0.98, (1173, 752, 1192, 845)), OCRLine("N:136,983.48", 0.97, (3395, 917, 3412, 1010))]
    monkeypatch.setattr(docs, "run_parcelmap_ocr", lambda img: (first, True))
    monkeypatch.setattr(cp, "recover_control_points", lambda img, fn, only_tiles=None: _tile_lines())

    docs._ensure_control_points(doc, 13)
    saved = json.loads((tmp_path / doc / "result.json").read_text())
    page = saved["pages"][0]
    assert page["control_points"]["status"] == "done"
    assert [(c["northing"], c["easting"]) for c in page["control_points"]["points"]] == [
        (137042.75, 1101226.83), (136983.48, 1101681.09)]
    assert all(c["source"] == "ocr_recovered" for c in page["control_points"]["points"])  # the E values were not in the first pass
    assert all(p["placement"]["control_fit"]["validated"] for p in page["regions"][0]["parcels"])

    # Cached: a second call neither re-reads nor changes anything.
    monkeypatch.setattr(docs, "run_parcelmap_ocr", lambda img: (_ for _ in ()).throw(AssertionError("re-read")))
    docs._ensure_control_points(doc, 13)
    assert json.loads((tmp_path / doc / "result.json").read_text()) == saved


def test_ensure_control_points_does_not_read_a_sheet_whose_placement_is_already_reliable(tmp_path, monkeypatch):
    doc = "reliable-doc"
    (tmp_path / doc / "pages").mkdir(parents=True)
    Image.new("RGB", (100, 100), "white").save(tmp_path / doc / "pages" / "page_013.png")
    r = _result(points=False, status="surveyed_corner")
    (tmp_path / doc / "result.json").write_text(json.dumps(r))
    monkeypatch.setattr(docs, "DOCUMENT_ROOT", tmp_path)
    monkeypatch.setattr(docs, "run_parcelmap_ocr", lambda img: (_ for _ in ()).throw(AssertionError("OCR must not run")))
    docs._ensure_control_points(doc, 13)
    assert json.loads((tmp_path / doc / "result.json").read_text()) == r


def test_tiles_near_the_polygon_are_read_first_and_the_rest_of_the_page_only_if_needed(tmp_path, monkeypatch):
    doc = "tiles-doc"
    (tmp_path / doc / "pages").mkdir(parents=True)
    Image.new("RGB", (3400, 2200), "white").save(tmp_path / doc / "pages" / "page_013.png")
    (tmp_path / doc / "result.json").write_text(json.dumps(_result(points=False)))
    monkeypatch.setattr(docs, "DOCUMENT_ROOT", tmp_path)
    monkeypatch.setattr(docs, "run_parcelmap_ocr", lambda img: ([], True))
    reads = []

    def fake_recover(img, fn, only_tiles=None):
        reads.append(len(only_tiles))
        return [] if len(reads) == 1 else _tile_lines()  # the focused pass finds nothing; the widened one does

    monkeypatch.setattr(cp, "recover_control_points", fake_recover)
    docs._ensure_control_points(doc, 13)
    total = len(cp.tile_origins(3400, 2200))
    assert len(reads) == 2 and reads[0] < total and reads[0] + reads[1] == total  # focused first, then only the remainder
    saved = json.loads((tmp_path / doc / "result.json").read_text())
    assert saved["pages"][0]["control_points"]["status"] == "done"

    # When the focused pass already finds both pairs, the rest of the page is never read.
    (tmp_path / doc / "result.json").write_text(json.dumps(_result(points=False)))
    reads.clear()
    monkeypatch.setattr(cp, "recover_control_points", lambda img, fn, only_tiles=None: reads.append(len(only_tiles)) or _tile_lines())
    docs._ensure_control_points(doc, 13)
    assert len(reads) == 1


def test_reading_marker_blocks_a_concurrent_read_but_a_stale_one_does_not(tmp_path, monkeypatch):
    doc = "marker-doc"
    (tmp_path / doc / "pages").mkdir(parents=True)
    Image.new("RGB", (3400, 2200), "white").save(tmp_path / doc / "pages" / "page_013.png")
    monkeypatch.setattr(docs, "DOCUMENT_ROOT", tmp_path)
    monkeypatch.setattr(docs, "run_parcelmap_ocr", lambda img: ([], True))
    monkeypatch.setattr(cp, "recover_control_points", lambda img, fn, only_tiles=None: _tile_lines())

    r = _result(points=False)
    r["pages"][0]["control_points"] = {"status": "reading", "started_at": docs.time.time()}
    (tmp_path / doc / "result.json").write_text(json.dumps(r))
    docs._ensure_control_points(doc, 13)  # a read is in flight: do nothing
    assert json.loads((tmp_path / doc / "result.json").read_text())["pages"][0]["control_points"]["status"] == "reading"
    assert docs._control_read_fresh(r["pages"][0])

    r["pages"][0]["control_points"]["started_at"] = docs.time.time() - 3600  # died with its process
    assert not docs._control_read_fresh(r["pages"][0])
    (tmp_path / doc / "result.json").write_text(json.dumps(r))
    docs._ensure_control_points(doc, 13)
    assert json.loads((tmp_path / doc / "result.json").read_text())["pages"][0]["control_points"]["status"] == "done"


def test_failed_read_clears_the_marker_so_a_later_verification_can_retry(tmp_path, monkeypatch):
    doc = "fail-doc"
    (tmp_path / doc / "pages").mkdir(parents=True)
    Image.new("RGB", (3400, 2200), "white").save(tmp_path / doc / "pages" / "page_013.png")
    (tmp_path / doc / "result.json").write_text(json.dumps(_result(points=False)))
    monkeypatch.setattr(docs, "DOCUMENT_ROOT", tmp_path)
    monkeypatch.setattr(docs, "run_parcelmap_ocr", lambda img: (_ for _ in ()).throw(RuntimeError("ocr down")))
    docs._ensure_control_points(doc, 13)
    page = json.loads((tmp_path / doc / "result.json").read_text())["pages"][0]
    assert "control_points" not in page


def test_a_sibling_whose_own_calibration_failed_is_placed_with_the_sheets_scale():
    """Derry re-run: Lot 48's area-based and edge-based scales disagreed (5.5%), so it had no scale,
    no geometry and no shape checks -- and its mere presence used to block the whole sheet."""
    r = _result()
    sibling = _parcels(r)[1]
    sibling["calibration"]["scale_ft_per_px"] = None
    sibling.pop("boundary_geojson_wgs84")
    sibling["placement"] = {"status": "approximate", "notes": ["no scale evidence found -- outline saved but cannot be placed on the map"]}
    assert docs._control_fit_members(r["pages"][0]["regions"][0]) is not None  # not blocked
    assert docs._fit_sheet_to_control_points(r, 13) is True
    first, second = _parcels(r)
    n = len(FX["parcels"][1]["vertices"])
    assert len(_ring(second)) == n + 1 and _ring(second)[0] == _ring(second)[-1]
    assert second["placement"]["control_fit"]["validated"] and second["placement"]["control_fit"]["scale_borrowed_from_sheet"] is True
    assert first["placement"]["control_fit"]["scale_borrowed_from_sheet"] is False
    assert second["spatial_validation"]["area_acres"] > 0 and second["boundary_geojson"]["geometry"]["coordinates"][0]
    assert not any("cannot be placed" in note for note in second["placement"]["notes"])
    gap = min(GEOD.inv(a[0], a[1], b[0], b[1])[2] for a in _ring(first) for b in _ring(second))
    assert gap < 0.5  # still one framework: the shared corner coincides


def test_the_read_starts_even_when_a_sibling_has_no_placed_geometry(tmp_path, monkeypatch):
    doc = "sibling-doc"
    (tmp_path / doc / "pages").mkdir(parents=True)
    Image.new("RGB", (3400, 2200), "white").save(tmp_path / doc / "pages" / "page_013.png")
    r = _result(points=False)
    _parcels(r)[1].pop("boundary_geojson_wgs84")
    _parcels(r)[1]["calibration"]["scale_ft_per_px"] = None
    (tmp_path / doc / "result.json").write_text(json.dumps(r))
    monkeypatch.setattr(docs, "DOCUMENT_ROOT", tmp_path)
    monkeypatch.setattr(docs, "run_parcelmap_ocr", lambda img: ([], True))
    monkeypatch.setattr(cp, "recover_control_points", lambda img, fn, only_tiles=None: _tile_lines())
    docs._ensure_control_points(doc, 13)
    saved = json.loads((tmp_path / doc / "result.json").read_text())
    assert saved["pages"][0]["control_points"]["status"] == "done"
    assert all(p["placement"]["control_fit"]["validated"] for p in saved["pages"][0]["regions"][0]["parcels"])
