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
from app.services import parcel_roster as pr  # noqa: E402
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


ROSTER = [  # what the roster call would read on NVZ page 9: both parcels, ids only where printed (none here)
    {"label": "PARCEL 1", "printed_id": None, "stated_area": "1.78 AC", "point": [0.62, 0.62]},
    {"label": "PARCEL 2", "printed_id": None, "stated_area": "2.78 AC", "point": [0.25, 0.62]},
]


def _partial_and_final(roster=True):
    """What the pipeline saves after the roster (partial), and returns at the end (final), on the real NVZ sheet."""
    final = _stored()
    final["processing"] = {"complete": True}
    entries = [{"page_number": p["page_number"], "regions": p["regions"]} for p in final["pages"]]
    pipe.build_sheets(entries)
    for page, entry in zip(final["pages"], entries):
        page["sheet"] = entry.get("sheet")
    sheet = next(p for p in final["pages"] if p["page_number"] == 9)["sheet"]
    sheet["role"], sheet["parcels"] = "target_parcel_map", []
    sheet["roster"] = {"status": "ready" if roster else "empty"}
    for item in ROSTER if roster else []:
        sheet["parcels"].append(pr.new_entity(9, sheet["parcels"], label=item["label"], region_index=sheet["main_region"],
                                              source="roster", stated_area=item["stated_area"], point=item["point"]))
    partial = copy.deepcopy(final)
    partial["processing"] = {"complete": False}
    partial["anchor_lat"] = partial["anchor_lon"] = partial["anchor"] = None
    for pg in partial["pages"]:
        for r in pg["regions"]:
            for k in ("parcels", "ocr_text", "ocr_confidence"):
                r.pop(k, None)
    pipe.join_roster_evidence([{"page_number": p["page_number"], "regions": p["regions"], "sheet": p.get("sheet")} for p in final["pages"]])
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


def _sheet(result):
    return next(p for p in result["pages"] if p["page_number"] == 9)["sheet"]


def _region(result):
    return next(p for p in result["pages"] if p["page_number"] == 9)["regions"][0]


def _entity(result, label):
    return next(e for e in _sheet(result)["parcels"] if e["label"] == label)


def _evidence(result, label):
    ref = _entity(result, label)["evidence_ref"]
    return _region(result)["parcels"][ref["parcel"]]


def _body(which, **extra):
    """The recorded outline of PARCEL 1 / PARCEL 2 (page 9, region 0), addressed by entity id."""
    b = json.loads((REPO / "scratch_diag" / ("confirm_p1.json" if which == 1 else "confirm_p2.json")).read_text())
    for k in ("local_vertices", "parcel_index"):
        b.pop(k, None)
    b.update(extra)
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
def test_two_parcels_on_one_sheet_each_keep_their_own_polygon(env):
    """The case that used to overwrite: outline Parcel 1, then Parcel 2, both before extraction is read."""
    partial, final = _partial_and_final()
    _write(env, partial)
    c = TestClient(_app())
    p1, p2 = (_entity(partial, "PARCEL 1")["id"], _entity(partial, "PARCEL 2")["id"])
    assert p1 != p2

    r1 = c.post(f"/documents/{NVZ}/confirm-boundary", json=_body(1, parcel_id=p1))
    assert r1.status_code == 200 and r1.json()["state"] == "waiting_for_document" and r1.json()["parcel"] is None
    r2 = c.post(f"/documents/{NVZ}/confirm-boundary", json=_body(2, parcel_id=p2))
    assert r2.json()["state"] == "waiting_for_document"

    stored = _read(env)
    v1, v2 = _entity(stored, "PARCEL 1")["confirmed_polygon"]["vertices"], _entity(stored, "PARCEL 2")["confirmed_polygon"]["vertices"]
    assert v1 == _body(1)["vertices"] and v2 == _body(2)["vertices"] and v1 != v2   # neither overwrote the other

    # a pipeline checkpoint saved meanwhile must not discard them either
    refreshed = copy.deepcopy(partial)
    pr.carry_over_user_data(stored, refreshed)
    assert _entity(refreshed, "PARCEL 1")["confirmed_polygon"]["vertices"] == v1

    # extraction finishes: the evidence join binds each polygon to ITS OWN parcel, independently
    asyncio.run(docs._finalize_and_bind(NVZ, copy.deepcopy(final)))
    done = _read(env)
    assert done["processing"]["complete"] is True
    for label, vertices in (("PARCEL 1", v1), ("PARCEL 2", v2)):
        parcel = _evidence(done, label)
        assert parcel["vision_geometry"]["parcel_label"] == label and parcel["human_confirmed"]
        assert parcel["confirmed_boundary_pixels"]["vertices"] == vertices
        assert parcel["calibration"]["status"] in ("cross_validated", "single_source", "unverified")   # verification ran, per parcel
        assert parcel["placement"]["status"] in ("surveyed_corner", "approximate")
        assert parcel["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]
    assert _evidence(done, "PARCEL 1")["boundary_geojson_wgs84"] != _evidence(done, "PARCEL 2")["boundary_geojson_wgs84"]


@needs_nvz
def test_confirming_after_the_pipeline_finished_binds_that_parcel_only(env):
    _, final = _partial_and_final()
    _write(env, final)
    other_before = json.dumps(_evidence(final, "PARCEL 2"), sort_keys=True)   # (already confirmed in the real stored data)
    c = TestClient(_app())
    r = c.post(f"/documents/{NVZ}/confirm-boundary", json=_body(1, parcel_id=_entity(final, "PARCEL 1")["id"]))
    body = r.json()
    assert body["state"] == "bound" and body["parcel"]["vision_geometry"]["parcel_label"] == "PARCEL 1"
    assert body["parcel"]["calibration"]["status"] == "pending"                      # instant save; verification follows
    done = _read(env)
    assert _evidence(done, "PARCEL 1")["calibration"]["status"] in ("cross_validated", "single_source", "unverified")
    assert json.dumps(_evidence(done, "PARCEL 2"), sort_keys=True) == other_before   # the other parcel is untouched
    assert _entity(done, "PARCEL 2")["confirmed_polygon"] is None


@needs_nvz
def test_roster_parcel_with_no_extracted_evidence_still_reaches_verification(env):
    """
    A roster parcel nothing matched (no resolved_boundary_calls at all --
    e.g. a hand-confirmed "remainder parcel") used to never attempt
    calibration/Gemini verification: _apply_confirmation's gate required
    body.local_vertices, which _seed_local_vertices can only build FROM
    resolved_boundary_calls, so a parcel with none always fell through to
    a hardcoded "cannot be placed" skip before verification ever ran. The
    confirmed polygon's own pixel vertices are sufficient on their own --
    this is the exact real-document regression for that fix: verification
    now runs on the real NVZ page/OCR fixture and produces a genuine
    scale/placement result, not the old skip message.
    """
    partial, final = _partial_and_final()
    _region(final)["parcels"] = []
    for e in _sheet(final)["parcels"]:
        e["evidence_ref"] = None
    _write(env, partial)
    c = TestClient(_app())
    c.post(f"/documents/{NVZ}/confirm-boundary", json=_body(1, parcel_id=_entity(partial, "PARCEL 1")["id"]))
    asyncio.run(docs._finalize_and_bind(NVZ, final))
    done = _read(env)
    parcel = _evidence(done, "PARCEL 1")
    assert parcel["created_from_confirmed_boundary"] and parcel["human_confirmed"]
    assert parcel["confirmed_boundary_pixels"]["vertices"] == _body(1)["vertices"]
    assert not parcel.get("resolved_boundary_calls")   # genuinely zero prior extraction -- the case under test
    notes = " ".join(parcel["calibration"]["notes"])
    assert "cannot be placed" not in notes   # the old bug: verification never even attempted
    assert parcel["calibration"]["status"] in ("cross_validated", "single_source", "unverified")
    assert parcel["boundary_source"] in ("manual_confirmed_calibrated", "manual_confirmed_uncalibrated")
    assert parcel["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]   # a real shape was actually placed
    assert _entity(done, "PARCEL 2")["evidence_ref"] is None and _entity(done, "PARCEL 2")["confirmed_polygon"] is None


@needs_nvz
def test_hand_named_parcel_is_marked_manual_never_gets_an_inferred_id_and_joins_by_label(env):
    partial, final = _partial_and_final(roster=False)     # the roster came back empty
    for e in _sheet(final)["parcels"]:
        e["evidence_ref"] = None
    _sheet(final)["parcels"] = []
    for parcel in _region(final)["parcels"]:
        parcel.pop("roster_id", None)
    _write(env, partial)
    c = TestClient(_app())
    r = c.post(f"/documents/{NVZ}/confirm-boundary", json=_body(1, label="  Parcel 1 "))
    ent = r.json()["entity"]
    assert ent["manual"] is True and ent["source"] == "manual" and ent["label"] == "Parcel 1"
    assert ent["printed_id"] is None and ent["id"] == "p9-m1"
    asyncio.run(docs._finalize_and_bind(NVZ, final))
    done = _read(env)
    mine = _entity(done, "Parcel 1")
    assert mine["manual"] is True and mine["printed_id"] is None and mine["evidence_ref"] is not None      # joined by label ...
    assert _evidence(done, "Parcel 1")["vision_geometry"]["parcel_label"] == "PARCEL 1"                   # ... to the extracted PARCEL 1
    assert _evidence(done, "Parcel 1")["human_confirmed"]
    # the extracted PARCEL 2 nobody named is its own (extraction) entity, still unconfirmed
    other = next(e for e in _sheet(done)["parcels"] if e["label"] == "PARCEL 2")
    assert other["source"] == "extraction" and other["confirmed_polygon"] is None


@needs_nvz
def test_confirm_requires_an_identity_and_a_real_parcel(env):
    partial, _ = _partial_and_final()
    _write(env, partial)
    c = TestClient(_app())
    assert c.post(f"/documents/{NVZ}/confirm-boundary", json=_body(1)).status_code == 400
    assert c.post(f"/documents/{NVZ}/confirm-boundary", json=_body(1, parcel_id="p9-nope")).status_code == 404
    assert c.post(f"/documents/{NVZ}/confirm-boundary", json=_body(1, parcel_id=_entity(partial, "PARCEL 1")["id"], vertices=[[0, 0], [1, 1]])).status_code == 400


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
    roster_calls = []

    def fake_roster(images):
        order.append("roster")
        roster_calls.append([im.size for im in images])
        return [[{"label": "Parcel A", "printed_id": None, "stated_area": "1.00 AC", "point": [0.5, 0.5]}] for _ in images]

    monkeypatch.setattr(pipe, "read_parcel_roster", fake_roster)

    async def fake_ocr(jobs):
        order.append("ocr")
        return [[OCRLine("Project Address: 0 Ironwood Road", 0.9, (5, 5, 90, 15))] for _ in jobs]

    async def fake_geocode(cands):
        order.append("geocode")
        return [], None

    def fake_extract(crops, model=None):
        order.append("vision")
        parcel = {"parcel_label": "PARCEL A", "role": "target", "stated_area_acres": "1.0", "boundary_calls": [],
                  "tie_point": None, "basis_of_bearings": None, "curve_calls": []}
        return [[parcel] if i == 0 else [] for i, _ in enumerate(crops)]

    monkeypatch.setattr(pipe, "geocode_anchor", fake_geocode)
    monkeypatch.setattr(pipe, "extract_parcel_geometries_batch", fake_extract)
    snapshots = []

    def checkpoint(partial):
        order.append("checkpoint")
        snapshots.append(copy.deepcopy(partial))

    result = asyncio.run(pipe.process_document("t1", ocr_dispatcher=fake_ocr, on_checkpoint=checkpoint))

    # triage -> checkpoint (sheets) -> roster -> checkpoint (roster) -> OCR -> geocode -> vision
    assert order[:4] == ["triage", "checkpoint", "roster", "checkpoint"]
    assert order.index("checkpoint") < order.index("roster") < order.index("ocr") < order.index("vision")
    assert order.count("triage") == 1 and order.count("roster") == 1
    # ONE image per SHEET (3 regions on 2 pages), each the whole page -- not a region crop
    assert len(sheet_calls) == 1 and len(sheet_calls[0]) == 2 and all(size[0] > 100 for size in sheet_calls[0])
    # the roster is read for the TARGET sheet only, from its main drawing cropped with the review canvas's padding
    assert roster_calls == [[(250, 170)]]
    first, second = snapshots
    assert first["processing"] == {"complete": False} and "parcels" not in first["pages"][0]["regions"][0]
    assert "parcels" not in first["pages"][0]["sheet"]                      # no roster yet at the first checkpoint
    p1, p2, p3 = second["pages"]
    assert p1["sheet"]["roster"] == {"status": "ready"} and p2["sheet"]["roster"] == {"status": "skipped"} and p3["sheet"] is None
    assert [(e["id"], e["label"], e["printed_id"], e["point"]) for e in p1["sheet"]["parcels"]] == [("p1-1", "Parcel A", None, [0.5, 0.5])]
    assert p2["sheet"]["main_region"] == 0 and p2["sheet"]["regions"] == [0] and p2["sheet"]["inset_regions"] == [1]  # inset folded in
    assert p2["sheet"]["role"] == "reference_survey" and [r.get("category") for r in p2["regions"]] == ["boundary_plat", "not_a_parcel_drawing"]
    # the evidence join: the extracted PARCEL A attached to the roster's Parcel A
    final = result["pages"][0]
    ent = final["sheet"]["parcels"][0]
    assert ent["evidence_ref"] == {"region": 0, "parcel": 0} and final["regions"][0]["parcels"][0]["roster_id"] == ent["id"]
    assert len(final["sheet"]["parcels"]) == 1
    stages = [h["stage"] for h in progress.get("t1")["history"]]
    assert stages.index("candidates_ready") < stages.index("roster") < stages.index("ocr") < stages.index("vision_extraction") < stages.index("done")
    assert result["processing"] == {"complete": True}


def test_a_failed_roster_leaves_the_sheet_a_candidate_and_the_hand_named_path_open(tmp_path, monkeypatch):
    monkeypatch.setattr(pipe, "read_parcel_roster", lambda images: (_ for _ in ()).throw(RuntimeError("no key")))
    img = tmp_path / "p.png"
    Image.new("RGB", (400, 300), "white").save(img)
    entries = [{"page_number": 1, "path": str(img), "regions": [{"class": "ParcelMap", "bbox": [10, 10, 300, 200]}]}]
    pipe.build_sheets(entries)
    asyncio.run(pipe.run_roster_stage(entries))
    assert entries[0]["sheet"]["parcels"] == [] and entries[0]["sheet"]["roster"] == {"status": "failed"}


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
