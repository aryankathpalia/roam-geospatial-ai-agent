"""confirm-boundary saves instantly; verification and placement follow in the background."""
import copy
import json
import shutil
import sys
import types
from pathlib import Path

sys.modules.setdefault("paddleocr", types.SimpleNamespace(PaddleOCR=object))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.routes import documents as docs  # noqa: E402
from app.services.ocr import OCRLine  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
NVZ = "6d8534f5-f07a-460d-970f-4ccf5c40a3e4"
SRC = REPO / "data" / "documents" / NVZ
FIXTURES = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.skipif(not (SRC / "pages" / "page_009.png").exists(), reason="NVZ test document not present")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    root = tmp_path / "documents"
    (root / NVZ / "pages").mkdir(parents=True)
    shutil.copy(SRC / "result.json", root / NVZ / "result.json")
    shutil.copy(SRC / "pages" / "page_009.png", root / NVZ / "pages" / "page_009.png")
    monkeypatch.setattr(docs, "DOCUMENT_ROOT", root)
    # Real OCR lines recorded from this page; no Gemini key, so OCR-only verification.
    lines = [OCRLine(d["text"], 1.0, tuple(d["bbox"])) for d in json.loads((FIXTURES / "nvz_page9_ocr_lines.json").read_text())]
    monkeypatch.setattr(docs, "run_parcelmap_ocr", lambda img: (lines, None))
    monkeypatch.setattr(docs.settings, "GEMINI_API_KEY", "")
    return TestClient(docs.router_app if hasattr(docs, "router_app") else _app()), root


def _app():
    from app.main import app
    return app


def _payload():
    body = json.loads((REPO / "scratch_diag" / "confirm_p1.json").read_text())
    # local_vertices: any 4 points; the provisional position only needs them present
    return body


def _parcel(root):
    r = json.loads((root / NVZ / "result.json").read_text())
    pg = next(p for p in r["pages"] if p["page_number"] == 9)
    return next(p for reg in pg["regions"] if reg.get("class") == "ParcelMap" for p in reg["parcels"]
                if p["vision_geometry"]["parcel_label"] == "PARCEL 1")


def test_save_returns_pending_then_verification_fills_in(client):
    c, root = client
    resp = c.post(f"/documents/{NVZ}/confirm-boundary", json=_payload())
    assert resp.status_code == 200
    body = resp.json()["parcel"]
    # what the user's screen gets: the polygon is saved and already on the map, nothing judged yet
    assert body["human_confirmed"] and body["confirmed_boundary_pixels"]["id"]
    assert body["calibration"]["status"] == "pending" and body["placement"]["status"] == "pending"
    assert body["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]
    # TestClient runs background tasks before returning, so the stored result is now final
    final = _parcel(root)
    assert final["calibration"]["status"] in ("cross_validated", "single_source", "unverified")
    assert final["placement"]["status"] in ("surveyed_corner", "approximate")
    assert final["boundary_source"].startswith("manual_confirmed")


def test_stale_verification_does_not_overwrite_a_newer_confirmation(client):
    c, root = client
    c.post(f"/documents/{NVZ}/confirm-boundary", json=_payload())
    stale_id = "not-the-current-confirmation"
    before = json.dumps(_parcel(root), sort_keys=True)
    body = docs.ConfirmBoundaryRequest(**_payload())
    docs._verify_confirmation(NVZ, body, stale_id)
    assert json.dumps(_parcel(root), sort_keys=True) == before


def test_wait_true_verifies_inline(client):
    c, root = client
    resp = c.post(f"/documents/{NVZ}/confirm-boundary?wait=true", json=_payload())
    assert resp.json()["parcel"]["calibration"]["status"] != "pending"


def test_confirmed_parcel_area_uses_the_closing_edge(client):
    # Regression: an open ring made validate_traverse measure HALF the area
    # (0.891 ac vs 1.78 ac stated), so every confirmed parcel read "needs review".
    c, root = client
    c.post(f"/documents/{NVZ}/confirm-boundary?wait=true", json=_payload())
    sv = _parcel(root)["spatial_validation"]
    assert abs(sv["area_acres"] - 1.78) < 0.03 and not sv["self_intersects"]
    assert sv["area_matches_stated"] is True


def test_empty_resolved_calls_still_reaches_gemini_verification(client, monkeypatch):
    """
    Regression: _apply_confirmation's gate used to require body.local_vertices,
    which _seed_local_vertices can only build FROM parcel["resolved_boundary_calls"]
    -- so a parcel with zero extracted survey calls (a hand-confirmed "remainder
    parcel", the exact case Gemini edge-association exists to help) never reached
    _derive_confirmed_geometry, and therefore never called Gemini, at all. The
    confirmed polygon's own pixel vertices must be sufficient on their own.
    """
    c, root = client
    r = json.loads((root / NVZ / "result.json").read_text())
    pg = next(p for p in r["pages"] if p["page_number"] == 9)
    parcel = next(p for reg in pg["regions"] if reg.get("class") == "ParcelMap" for p in reg["parcels"]
                  if p["vision_geometry"]["parcel_label"] == "PARCEL 1")
    parcel["resolved_boundary_calls"] = []   # genuinely zero prior extraction
    (root / NVZ / "result.json").write_text(json.dumps(r))

    calls = []

    def fake_associate_edges(page_img, crop_box, polygon_crop_px):
        calls.append((crop_box, polygon_crop_px))
        return []  # no edges found -- still a valid, non-fatal result (requirement 8)

    monkeypatch.setattr(docs.gemini_edge_association, "associate_edges", fake_associate_edges)
    monkeypatch.setattr(docs.settings, "GEMINI_API_KEY", "dummy-test-key")
    monkeypatch.setattr(docs.settings, "CALIBRATION_GEMINI_ASSOCIATION", True)

    payload = _payload()
    payload.pop("local_vertices", None)   # exactly what the frontend sends for a never-seeded parcel
    resp = c.post(f"/documents/{NVZ}/confirm-boundary?wait=true", json=payload)
    assert resp.status_code == 200

    assert len(calls) == 1   # Gemini was actually invoked, not skipped
    crop_box, polygon_crop_px = calls[0]
    assert len(polygon_crop_px) >= 3 and polygon_crop_px == [tuple(v) for v in payload["vertices"]]

    final = _parcel(root)
    assert "cannot be placed" not in " ".join(final["calibration"]["notes"])
    assert final["calibration"]["status"] in ("cross_validated", "single_source", "unverified")   # not silently failed
    assert final["boundary_source"] in ("manual_confirmed_calibrated", "manual_confirmed_uncalibrated")
    assert final["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]   # a real shape was actually placed


def test_update_anchor_preserves_the_full_confirmed_polygon(client):
    """
    Regression for the production bug: a manually confirmed polygon's
    full vertex set (not just the independently-corroborated ones) was
    silently destroyed by any anchor change, because /recompute
    unconditionally rebuilds boundary_geojson/_wgs84 from
    resolved_boundary_calls -- a field the confirmed-polygon flow never
    even writes to. /update-anchor must re-georeference the EXISTING
    confirmed shape instead: same vertex count, same topology, same
    calibration/evidence, only the map position moves.
    """
    c, root = client
    c.post(f"/documents/{NVZ}/confirm-boundary?wait=true", json=_payload())
    before = _parcel(root)
    assert before["confirmed_boundary_pixels"]["vertices"]  # sanity: really confirmed
    # Evidence need not cover every vertex for this to matter -- assert
    # whatever the real corroboration found, not a fabricated count.
    before_evidence = before["calibration"]["independent_bearing_edges"]
    before_local_ring = before["boundary_geojson"]["geometry"]["coordinates"][0]
    before_wgs84_ring = before["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]
    assert len(before_local_ring) == 5  # 4 vertices + closing repeat

    new_lat, new_lon = 39.80, -119.80   # clearly different from NVZ's real anchor (~39.70, -119.90)
    resp = c.post(f"/documents/{NVZ}/update-anchor", json={
        "page_number": 9, "region_index": 0, "parcel_index": 1,
        "anchor_lat": new_lat, "anchor_lon": new_lon,
    })
    assert resp.status_code == 200
    after = _parcel(root)

    after_wgs84_ring = after["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]
    assert len(after_wgs84_ring) == len(before_wgs84_ring) == 5   # vertex count unchanged
    assert after["confirmed_boundary_pixels"] == before["confirmed_boundary_pixels"]   # untouched
    assert after["boundary_geojson"]["geometry"]["coordinates"][0] == before_local_ring  # topology unchanged (local ring is anchor-independent)
    assert after["calibration"] == before["calibration"]   # evidence/corroboration status untouched
    assert after["calibration"]["independent_bearing_edges"] == before_evidence  # no new measurement invented
    assert after["boundary_source"] == before["boundary_source"]
    assert after["placement"] == before["placement"]
    assert after["boundary_geojson_wgs84"] != before["boundary_geojson_wgs84"]   # position DID change
    assert after["anchor_override"] == {"lat": new_lat, "lon": new_lon}


def test_update_anchor_rejects_a_parcel_with_no_confirmed_polygon(client):
    """The opposite case: a parcel whose geometry source of truth IS
    resolved_boundary_calls must never be silently routed through the
    confirmed-polygon path -- a clear error, not a silent downgrade."""
    c, root = client
    r = json.loads((root / NVZ / "result.json").read_text())
    pg = next(p for p in r["pages"] if p["page_number"] == 9)
    parcel = next(p for reg in pg["regions"] if reg.get("class") == "ParcelMap" for p in reg["parcels"]
                  if p["vision_geometry"]["parcel_label"] == "PARCEL 1")
    parcel.pop("confirmed_boundary_pixels", None)
    parcel["human_confirmed"] = False
    (root / NVZ / "result.json").write_text(json.dumps(r))

    resp = c.post(f"/documents/{NVZ}/update-anchor", json={
        "page_number": 9, "region_index": 0, "parcel_index": 1,
        "anchor_lat": 39.70, "anchor_lon": -119.90,
    })
    assert resp.status_code == 400
    assert "use /recompute instead" in resp.json()["detail"]


PATNAUDE = "edda5a9b-aa1b-415c-b6ca-f2aa8f29ea8f"
PATNAUDE_SRC = REPO / "data" / "documents" / PATNAUDE
pytestmark_patnaude = pytest.mark.skipif(
    not (PATNAUDE_SRC / "pages" / "page_007.png").exists(), reason="Patnaude test document not present"
)


@pytest.fixture()
def patnaude_client(tmp_path, monkeypatch):
    root = tmp_path / "documents"
    (root / PATNAUDE / "pages").mkdir(parents=True)
    shutil.copy(PATNAUDE_SRC / "result.json", root / PATNAUDE / "result.json")
    shutil.copy(PATNAUDE_SRC / "pages" / "page_007.png", root / PATNAUDE / "pages" / "page_007.png")
    monkeypatch.setattr(docs, "DOCUMENT_ROOT", root)
    monkeypatch.setattr(docs, "run_parcelmap_ocr", lambda img: ([], None))  # no OCR evidence either way
    monkeypatch.setattr(docs.settings, "GEMINI_API_KEY", "")

    # Give the real "Remainder Parcel" extraction entry (region 3, parcel 3 --
    # zero resolved_boundary_calls, exactly the Patnaude bug's shape) a
    # same-region SIBLING with no calls of its own either -- standing in for
    # "Parcel 1", which real extraction never attributed any calls to at all.
    r = json.loads((root / PATNAUDE / "result.json").read_text())
    pg7 = next(p for p in r["pages"] if p["page_number"] == 7)
    region3 = pg7["regions"][3]
    sibling = copy.deepcopy(region3["parcels"][3])  # REMAINDER PARCEL's own entry
    sibling["vision_geometry"]["parcel_label"] = "PARCEL 1"
    sibling["vision_geometry"]["stated_area_acres"] = "40.00"
    region3["parcels"].append(sibling)  # now index 4
    (root / PATNAUDE / "result.json").write_text(json.dumps(r))
    return TestClient(_app()), root


def _patnaude_parcel(root, label):
    r = json.loads((root / PATNAUDE / "result.json").read_text())
    pg7 = next(p for p in r["pages"] if p["page_number"] == 7)
    return next(p for p in pg7["regions"][3]["parcels"] if p["vision_geometry"]["parcel_label"] == label)


def _square(cx, cy, half=60.0):
    return [[cx - half, cy - half], [cx + half, cy - half], [cx + half, cy + half], [cx - half, cy + half]]


def test_sibling_parcels_sharing_a_sheet_are_not_collapsed_onto_the_same_anchor(patnaude_client):
    """
    Regression for the real Patnaude bug: "Remainder Parcel" and "Parcel 1"
    share one ParcelMap region/sheet and neither has any extracted boundary
    calls of its own, so each independently fell back to pinning its OWN
    confirmed-pixel bbox center at the document's single anchor point --
    collapsing both parcels' centers onto the exact same real-world spot and
    discarding their true relative position on the sheet (reported as the
    two shapes landing far apart/jumbled instead of correctly adjacent).

    Confirming two such siblings at two clearly different pixel locations on
    the SAME region must leave them at two correspondingly different,
    non-coincident real-world positions -- not collapsed onto one point.
    """
    c, root = patnaude_client
    # Two small, well-separated squares within region 3's crop: ~3000px
    # apart horizontally, nowhere near each other or the crop center.
    remainder_vertices = _square(400, 400)
    parcel1_vertices = _square(3400, 400)

    r1 = c.post(f"/documents/{PATNAUDE}/confirm-boundary?wait=true", json={
        "page_number": 7, "region_index": 3, "parcel_index": 3,
        "vertices": remainder_vertices, "crop_width": 4500.0, "crop_height": 4300.0,
    })
    assert r1.status_code == 200
    r2 = c.post(f"/documents/{PATNAUDE}/confirm-boundary?wait=true", json={
        "page_number": 7, "region_index": 3, "parcel_index": 4,
        "vertices": parcel1_vertices, "crop_width": 4500.0, "crop_height": 4300.0,
    })
    assert r2.status_code == 200

    remainder = _patnaude_parcel(root, "REMAINDER PARCEL")
    parcel1 = _patnaude_parcel(root, "PARCEL 1")
    rem_ring = remainder["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]
    p1_ring = parcel1["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]

    def centroid(ring):
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return sum(xs) / len(xs), sum(ys) / len(ys)

    rem_lon, rem_lat = centroid(rem_ring)
    p1_lon, p1_lat = centroid(p1_ring)

    from pyproj import Geod
    _, _, distance_m = Geod(ellps="WGS84").inv(rem_lon, rem_lat, p1_lon, p1_lat)

    # The bug collapsed both centers onto the identical anchor point --
    # distance ~0. The pixel offset here (3000px) at whatever scale
    # calibration found is certainly more than 50m in any realistic
    # feet-per-pixel range for a survey plat.
    assert distance_m > 50.0


def test_delete_parcel_entity_removes_an_unconfirmed_spurious_candidate(patnaude_client):
    c, root = patnaude_client
    r = json.loads((root / PATNAUDE / "result.json").read_text())
    pg7 = next(p for p in r["pages"] if p["page_number"] == 7)
    before_ids = {e["id"] for e in pg7["sheet"]["parcels"]}
    target = next(e for e in pg7["sheet"]["parcels"] if e["id"] == "p7-x1")  # a flagged extraction fragment
    assert not target.get("confirmed_polygon")

    resp = c.delete(f"/documents/{PATNAUDE}/pages/7/parcels/p7-x1")
    assert resp.status_code == 200 and resp.json()["deleted"] == "p7-x1"

    after = json.loads((root / PATNAUDE / "result.json").read_text())
    pg7_after = next(p for p in after["pages"] if p["page_number"] == 7)
    after_ids = {e["id"] for e in pg7_after["sheet"]["parcels"]}
    assert after_ids == before_ids - {"p7-x1"}


def test_delete_parcel_entity_refuses_a_confirmed_parcel(patnaude_client):
    c, root = patnaude_client
    resp = c.delete(f"/documents/{PATNAUDE}/pages/7/parcels/p7-1")  # REMAINDER PARCEL, already confirmed
    assert resp.status_code == 400
    assert "already been confirmed" in resp.json()["detail"]
    after = json.loads((root / PATNAUDE / "result.json").read_text())
    pg7 = next(p for p in after["pages"] if p["page_number"] == 7)
    assert any(e["id"] == "p7-1" for e in pg7["sheet"]["parcels"])


def test_delete_parcel_entity_404s_for_an_unknown_id(patnaude_client):
    c, root = patnaude_client
    resp = c.delete(f"/documents/{PATNAUDE}/pages/7/parcels/not-a-real-id")
    assert resp.status_code == 404


def test_unverified_rotation_still_uses_the_corroborated_scale(client, monkeypatch):
    # Patnaude packet: rotation could not be verified, but the scale was corroborated by
    # 3 printed distances. The parcel must be sized by that scale (1.78 ac here), not by
    # the seed's display scale (a 40-acre parcel was drawn as 3.95 acres).
    c, root = client
    real = docs.calibration_service.calibrate

    def unverified_rotation(*a, **k):
        res = real(*a, **k)
        res.status, res.rotation_deg = "unverified", None
        return res

    monkeypatch.setattr(docs.calibration_service, "calibrate", unverified_rotation)
    c.post(f"/documents/{NVZ}/confirm-boundary?wait=true", json=_payload())
    p = _parcel(root)
    assert p["calibration"]["status"] == "unverified" and p["boundary_source"] == "manual_confirmed_uncalibrated"
    assert p["placement"]["status"] == "approximate"
    assert abs(p["spatial_validation"]["area_acres"] - 1.78) < 0.05
    assert any("north-up" in n for n in p["calibration"]["notes"])


def test_deleting_a_parcel_also_removes_it_from_the_extracted_list_and_it_is_never_recreated(patnaude_client):
    from app.services import parcel_roster

    c, root = patnaude_client
    assert c.delete(f"/documents/{PATNAUDE}/pages/7/parcels/p7-x1").status_code == 200
    r = json.loads((root / PATNAUDE / "result.json").read_text())
    pg7 = next(p for p in r["pages"] if p["page_number"] == 7)
    assert pg7["regions"][3]["parcels"][0]["deleted"] is True          # what the workspace lists
    parcel_roster.join_evidence(7, pg7["sheet"], pg7["regions"])      # the next evidence join (finalize / reprocess)
    assert not any(e.get("evidence_ref") == {"region": 3, "parcel": 0} for e in pg7["sheet"]["parcels"])
    assert "p7-x1" not in {e["id"] for e in pg7["sheet"]["parcels"]}
