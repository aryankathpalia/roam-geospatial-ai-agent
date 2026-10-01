"""Boundary confirmation as an early input: draw after triage, join when the pipeline finishes."""
import asyncio
import copy
import json
import shutil
import sys
import types
from pathlib import Path
from types import SimpleNamespace

sys.modules.setdefault("paddleocr", types.SimpleNamespace(PaddleOCR=object))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.pipeline import document_pipeline as pipe  # noqa: E402
from app.routes import documents as docs  # noqa: E402
from app.services import progress  # noqa: E402
from app.services.ocr import OCRLine  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
NVZ = "6d8534f5-f07a-460d-970f-4ccf5c40a3e4"
SRC = REPO / "data" / "documents" / NVZ
FIXTURES = Path(__file__).parent / "fixtures"
needs_nvz = pytest.mark.skipif(not (SRC / "pages" / "page_009.png").exists(), reason="NVZ test document not present")


def _app():
    from app.main import app
    return app


def _stored():
    return json.loads((SRC / "result.json").read_text())


def _partial_and_final():
    """What the pipeline would have saved right after triage, and what it returns at the end."""
    final = _stored()
    final["processing"] = {"complete": True}
    partial = copy.deepcopy(final)
    partial["processing"] = {"complete": False}
    partial["anchor_lat"] = partial["anchor_lon"] = partial["anchor"] = None
    for pg in partial["pages"]:
        for r in pg["regions"]:
            for k in ("parcels", "ocr_text", "ocr_confidence"):
                r.pop(k, None)
    return partial, final


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "documents"
    (root / NVZ / "pages").mkdir(parents=True)
    shutil.copy(SRC / "pages" / "page_009.png", root / NVZ / "pages" / "page_009.png")
    monkeypatch.setattr(docs, "DOCUMENT_ROOT", root)
    lines = [OCRLine(d["text"], 1.0, tuple(d["bbox"])) for d in json.loads((FIXTURES / "nvz_page9_ocr_lines.json").read_text())]
    monkeypatch.setattr(docs, "run_parcelmap_ocr", lambda img: (lines, None))
    monkeypatch.setattr(docs.settings, "GEMINI_API_KEY", "")
    return root


def _write(root, result):
    (root / NVZ / "result.json").write_text(json.dumps(result))


def _read(root):
    return json.loads((root / NVZ / "result.json").read_text())


def _region(result):
    pg = next(p for p in result["pages"] if p["page_number"] == 9)
    return next(r for r in pg["regions"] if r.get("class") == "ParcelMap")


def _body(parcel_index=None):
    b = json.loads((REPO / "scratch_diag" / "confirm_p1.json").read_text())
    b.pop("local_vertices", None)  # drawn before the parcel's seed ring existed in the browser
    b["parcel_index"] = parcel_index
    return b


@needs_nvz
def test_get_reports_processing_until_the_pipeline_is_done(env):
    partial, final = _partial_and_final()
    _write(env, partial)
    c = TestClient(_app())
    assert c.get(f"/documents/{NVZ}").json()["status"] == "processing"
    _write(env, final)
    assert c.get(f"/documents/{NVZ}").json()["status"] == "processed"


@needs_nvz
def test_drawing_before_parcels_are_read_is_saved_on_the_region_and_survives_the_join(env):
    partial, final = _partial_and_final()
    _write(env, partial)
    c = TestClient(_app())
    r = c.post(f"/documents/{NVZ}/confirm-boundary", json=_body())
    assert r.status_code == 200 and r.json()["state"] == "waiting_for_document" and r.json()["parcel"] is None
    saved = _region(_read(env))["confirmed_polygon"]
    assert len(saved["vertices"]) == 4 and saved["id"]

    # pipeline finishes: the region now holds TWO parcels (PARCEL 2, PARCEL 1) -> the user must choose
    asyncio.run(docs._finalize_and_bind(NVZ, copy.deepcopy(final)))
    after = _read(env)
    region = _region(after)
    assert after["processing"]["complete"] is True and len(region["parcels"]) == 2
    assert region["confirmed_polygon"]["needs_parcel"] is True  # carried over, not bound, not lost
    assert not any(p.get("human_confirmed") and p["vision_geometry"]["parcel_label"] == "PARCEL 1"
                   and (p.get("confirmed_boundary_pixels") or {}).get("id") == saved["id"] for p in region["parcels"])

    # the user picks the parcel: the usual confirm (parcel_index set), region drawing is superseded
    idx = next(i for i, p in enumerate(region["parcels"]) if p["vision_geometry"]["parcel_label"] == "PARCEL 1")
    r = c.post(f"/documents/{NVZ}/confirm-boundary", json=_body(idx))
    assert r.json()["state"] == "bound"
    done = _read(env)
    parcel = _region(done)["parcels"][idx]
    assert "confirmed_polygon" not in _region(done) and parcel["human_confirmed"]
    assert parcel["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]
    assert parcel["calibration"]["status"] != "pending" and parcel["placement"]["status"] in ("surveyed_corner", "approximate")


@needs_nvz
def test_single_parcel_region_binds_automatically_when_the_pipeline_finishes(env):
    partial, final = _partial_and_final()
    region_f = _region(final)
    region_f["parcels"] = [p for p in region_f["parcels"] if p["vision_geometry"]["parcel_label"] == "PARCEL 1"]
    _write(env, partial)
    c = TestClient(_app())
    c.post(f"/documents/{NVZ}/confirm-boundary", json=_body())
    asyncio.run(docs._finalize_and_bind(NVZ, final))
    region = _region(_read(env))
    assert "confirmed_polygon" not in region
    parcel = region["parcels"][0]
    assert parcel["human_confirmed"] and parcel["calibration"]["status"] in ("cross_validated", "single_source", "unverified")
    assert parcel["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]
    assert parcel["placement"]["status"] in ("surveyed_corner", "approximate")  # verification ran, nothing left "pending"


@needs_nvz
def test_drawing_after_the_pipeline_finished_binds_immediately(env):
    _, final = _partial_and_final()
    region_f = _region(final)
    region_f["parcels"] = [p for p in region_f["parcels"] if p["vision_geometry"]["parcel_label"] == "PARCEL 1"]
    _write(env, final)
    r = TestClient(_app()).post(f"/documents/{NVZ}/confirm-boundary", json=_body())
    assert r.json()["state"] == "bound" and r.json()["parcel"]["human_confirmed"]
    assert r.json()["parcel"]["calibration"]["status"] == "pending"  # instant save; verification follows
    final_parcel = _region(_read(env))["parcels"][0]
    assert final_parcel["calibration"]["status"] in ("cross_validated", "single_source", "unverified")


@needs_nvz
def test_region_with_no_extracted_parcels_still_keeps_the_users_outline(env):
    partial, final = _partial_and_final()
    _region(final)["parcels"] = []
    _write(env, partial)
    c = TestClient(_app())
    c.post(f"/documents/{NVZ}/confirm-boundary", json=_body())
    asyncio.run(docs._finalize_and_bind(NVZ, final))
    parcel = _region(_read(env))["parcels"][0]
    assert parcel["human_confirmed"] and parcel["created_from_confirmed_boundary"]
    assert parcel["confirmed_boundary_pixels"]["vertices"] and "cannot be placed" in " ".join(parcel["calibration"]["notes"])


# ---------------------------------------------------------------- pipeline ordering

def test_checkpoint_and_candidates_come_before_ocr_geocoding_and_vision(tmp_path, monkeypatch):
    root = tmp_path / "documents"
    doc = root / "t1"
    doc.mkdir(parents=True)
    pdf = __import__("pymupdf").open()
    for _ in range(3):
        pdf.new_page(width=300, height=200).insert_text((20, 40), "sheet")
    pdf.save(doc / "original.pdf")

    order = []
    monkeypatch.setattr(pipe, "DOCUMENT_ROOT", root)
    monkeypatch.setattr(pipe, "detect_page_layout", lambda path: [])

    def fake_crops(path, det, save_dir, save_only_needs_review):
        n = int(path[-7:-4])
        box = lambda b, c="ParcelMap": SimpleNamespace(roam_class=c, confidence=0.9, bbox=b, needs_review=False, saved_path=None)  # noqa: E731
        return {1: [box((10, 10, 200, 120))],
                2: [box((10, 10, 220, 150)), box((240, 10, 40, 30))],   # main map + a small inset on the SAME sheet
                3: [box((10, 10, 200, 100), "Text")]}[n]

    monkeypatch.setattr(pipe, "extract_region_crops", fake_crops)
    sheet_calls = []

    def fake_sheets(images):
        order.append("triage")
        sheet_calls.append([im.size for im in images])
        return [{"role": "target_parcel_map", "reason": "Titled for the owner."},
                {"role": "reference_survey", "reason": "Older recorded survey cited as a reference."}][: len(images)]

    monkeypatch.setattr(pipe, "classify_sheets", fake_sheets)
    monkeypatch.setattr(pipe, "classify_regions", lambda crops: pytest.fail("region-level triage must not run again"))

    async def fake_ocr(jobs):
        order.append("ocr")
        return [[OCRLine("Project Address: 0 Ironwood Road", 0.9, (5, 5, 90, 15))] for _ in jobs]

    async def fake_geocode(cands):
        order.append("geocode")
        return [], None

    def fake_extract(crops, model=None):
        order.append("vision")
        return [[] for _ in crops]

    monkeypatch.setattr(pipe, "geocode_anchor", fake_geocode)
    monkeypatch.setattr(pipe, "extract_parcel_geometries_batch", fake_extract)
    snapshots = []

    def checkpoint(partial):
        order.append("checkpoint")
        snapshots.append(copy.deepcopy(partial))

    result = asyncio.run(pipe.process_document("t1", ocr_dispatcher=fake_ocr, on_checkpoint=checkpoint))

    assert order[:2] == ["triage", "checkpoint"] and order.index("checkpoint") < order.index("ocr") < order.index("vision")
    assert order.count("triage") == 1  # one batched sheet call, not re-run inside the vision stage
    # ONE image per SHEET (3 regions on 2 pages), each the whole page -- not a region crop
    assert len(sheet_calls) == 1 and len(sheet_calls[0]) == 2 and all(size[0] > 100 for size in sheet_calls[0])
    early = snapshots[0]
    assert early["processing"] == {"complete": False} and "parcels" not in early["pages"][0]["regions"][0]
    p1, p2, p3 = early["pages"]
    assert p1["sheet"] == {"main_region": 0, "regions": [0], "inset_regions": [], "role": "target_parcel_map", "reason": "Titled for the owner."}
    assert p2["sheet"]["main_region"] == 0 and p2["sheet"]["regions"] == [0] and p2["sheet"]["inset_regions"] == [1]  # folded into the sheet
    assert p2["sheet"]["role"] == "reference_survey" and p3["sheet"] is None  # a text page is not a sheet
    assert [r.get("category") for r in p2["regions"]] == ["boundary_plat", "not_a_parcel_drawing"]
    assert result["pages"][1]["sheet"]["inset_regions"] == [1]  # the final result keeps the structure
    stages = [h["stage"] for h in progress.get("t1")["history"]]
    assert stages.index("candidates_ready") < stages.index("ocr") < stages.index("vision_extraction") < stages.index("done")
    assert result["processing"] == {"complete": True}


def test_sheet_without_a_role_is_still_a_candidate_when_triage_fails(tmp_path, monkeypatch):
    entries = [{"page_number": 1, "path": None, "regions": [{"class": "ParcelMap", "bbox": [0, 0, 100, 100]}]}]
    pipe.build_sheets(entries)
    assert entries[0]["sheet"]["role"] is None and entries[0]["sheet"]["main_region"] == 0


def test_comparable_regions_on_one_page_are_one_sheet_with_both_drawings():
    entries = [{"page_number": 7, "regions": [
        {"class": "ParcelMap", "bbox": [0, 0, 900, 700]}, {"class": "ParcelMap", "bbox": [0, 800, 800, 600]},
        {"class": "ParcelMap", "bbox": [950, 0, 100, 80]}, {"class": "Text", "bbox": [0, 0, 5000, 5000]}]}]
    pipe.build_sheets(entries)
    assert entries[0]["sheet"] == {"main_region": 0, "regions": [0, 1], "inset_regions": [2], "role": None, "reason": None}
