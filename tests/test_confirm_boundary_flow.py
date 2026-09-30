"""confirm-boundary saves instantly; verification and placement follow in the background."""
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
