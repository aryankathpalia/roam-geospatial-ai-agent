"""REGION -> SHEET -> PARCEL(S) over the real stored documents (real layout output, real parcels)."""
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

sys.modules.setdefault("paddleocr", types.SimpleNamespace(PaddleOCR=object))

import pytest  # noqa: E402

from app.pipeline import document_pipeline as pipe  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DOCS = sorted((REPO / "data" / "documents").glob("*/result.json"))
pytestmark = pytest.mark.skipif(not DOCS or not shutil.which("node"), reason="needs stored documents and node")


def _probe(path):
    out = subprocess.run(["node", str(Path(__file__).parent / "sheet_model_probe.mts"), str(path)],
                         capture_output=True, text=True, check=True, cwd=REPO)
    return json.loads(out.stdout)


@pytest.mark.parametrize("path", DOCS, ids=[p.parent.name[:8] for p in DOCS])
def test_one_candidate_per_sheet_and_parcels_nest_under_it(path):
    result = json.loads(path.read_text())
    probe = _probe(path)
    pages_with_maps = {p["page_number"] for p in result["pages"] if any(r["class"] == "ParcelMap" for r in p["regions"])}

    # ONE group per page that holds a parcel map -- never one per region or per parcel
    pages_in_groups = [g["page"] for g in probe["groups"]]
    assert len(pages_in_groups) == len(set(pages_in_groups))
    parcel_pages = {p["page_number"] for p in result["pages"] for r in p["regions"] if r.get("parcels")}
    assert parcel_pages <= set(pages_in_groups) <= pages_with_maps

    # every parcel of a sheet's drawing regions is nested under that sheet's single group
    for page in result["pages"]:
        group = next((g for g in probe["groups"] if g["page"] == page["page_number"]), None)
        sheet = probe["sheets"].get(str(page["page_number"]))
        if group is None:
            continue
        nested = [i["label"] for i in group["items"] if not i["isRegion"]]
        expected = [pc["vision_geometry"].get("parcel_label") for ri in sheet["regions"]
                    for pc in page["regions"][ri].get("parcels") or [] if not pc.get("likely_duplicate_region")]
        assert nested == [e or n for e, n in zip(expected, nested)] and len(nested) == len(expected)
        # a sheet with parcels has no placeholder map item next to them
        assert all(not i["isRegion"] for i in group["items"]) if nested else len(group["items"]) == 1


@pytest.mark.parametrize("path", DOCS, ids=[p.parent.name[:8] for p in DOCS])
def test_frontend_and_backend_build_the_same_sheets(path):
    result = json.loads(path.read_text())
    entries = [{"page_number": p["page_number"], "regions": [dict(r) for r in p["regions"]]} for p in result["pages"]]
    pipe.build_sheets(entries)
    probe = _probe(path)
    for e in entries:
        ts = probe["sheets"].get(str(e["page_number"]))
        if e.get("sheet") is None:
            assert ts is None or not any(r["class"] == "ParcelMap" for r in e["regions"])
            continue
        assert ts["mainRegion"] == e["sheet"]["main_region"]
        assert ts["regions"] == e["sheet"]["regions"] and ts["insetRegions"] == e["sheet"]["inset_regions"]


def test_a_sheet_with_two_parcels_is_one_candidate_in_the_real_documents():
    by_id = {p.parent.name: p for p in DOCS}
    # MAP 7: LOT 48 + LOT 48-3 are two parcels on ONE sheet -> one primary group with both nested
    map7 = _probe(by_id["3ea4cc01-4d39-472b-9465-a105306d63dc"])
    primary = [g for g in map7["groups"] if g["primary"]]
    assert [g["page"] for g in primary] == [13] and [i["label"] for i in primary[0]["items"]] == ["MAP 7 LOT 48", "MAP 7 LOT 48-3"]
    # the other ParcelMap-class region in this document is its own (non-primary) sheet, listed, not hidden
    assert [g["page"] for g in map7["groups"] if not g["primary"]] == [2]
    # Ada County: PARCEL 1 + PARCEL 2 on one sheet
    ada = _probe(by_id["6cbca296-7d7e-4457-a66b-37f04a7c2628"])
    assert [(g["page"], [i["label"] for i in g["items"]]) for g in ada["groups"]] == [(28, ["PARCEL 1", "PARCEL 2"])]
    # Easement: page 7 has TWO comparable regions with 5 parcels between them -> still ONE sheet
    easement = _probe(by_id["1600dbf6-9174-4360-9cae-55886bc7ca7f"])
    p7 = [g for g in easement["groups"] if g["page"] == 7]
    assert len(p7) == 1 and len(p7[0]["items"]) == 5


def test_nvz_true_parcel_sheets_are_never_demoted_by_citing_text():
    # Regression: an OCR "cited as a reference" heuristic marked NVZ pages 9 and 11 (the real plat,
    # whose own notes say "as shown on RS 5122") as referenced surveys, leaving no primary sheet.
    nvz = _probe({p.parent.name: p for p in DOCS}["6d8534f5-f07a-460d-970f-4ccf5c40a3e4"])
    sheets = {g["page"]: g for g in nvz["groups"]}
    assert sheets[9]["primary"] and sheets[11]["primary"] and sheets[9]["badge"] is None
    assert [i["label"] for i in sheets[9]["items"]] == ["PARCEL 2", "PARCEL 1"]
    assert nvz["primaryParcels"] == 4
