"""Drag-to-move: translate confirmed parcels by hand without touching their shape, and keep the move across re-verification."""
import copy
import json
import math
import sys
import types

sys.modules.setdefault("paddleocr", types.SimpleNamespace(PaddleOCR=object))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pyproj import Geod  # noqa: E402

from app.routes import documents as docs  # noqa: E402

GEOD = Geod(ellps="WGS84")
DOC = "move-doc"


def _feature(ring):
    return {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}}


def _parcel(label, ring, conf_id="c1"):
    return {
        "vision_geometry": {"parcel_label": label}, "human_confirmed": True,
        "confirmed_boundary_pixels": {"vertices": [[0, 0], [1, 0], [1, 1], [0, 1]], "id": conf_id},
        "boundary_geojson_wgs84": _feature(ring), "placement": {"status": "approximate", "notes": []},
    }


# Two parcels sharing the edge (-115.5530, 32.8105) -> (-115.5530, 32.8110)
A = [[-115.5545, 32.8105], [-115.5530, 32.8105], [-115.5530, 32.8110], [-115.5545, 32.8110], [-115.5545, 32.8105]]
B = [[-115.5530, 32.8105], [-115.5515, 32.8105], [-115.5515, 32.8110], [-115.5530, 32.8110], [-115.5530, 32.8105]]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    (tmp_path / DOC).mkdir()
    result = {"pages": [{"page_number": 3, "regions": [{"parcels": [_parcel("A", A), _parcel("B", B)]}]}]}
    (tmp_path / DOC / "result.json").write_text(json.dumps(result))
    monkeypatch.setattr(docs, "DOCUMENT_ROOT", tmp_path)
    from app.main import app
    return TestClient(app), tmp_path


def _stored(root):
    r = json.loads((root / DOC / "result.json").read_text())
    return r["pages"][0]["regions"][0]["parcels"]


def _ring(parcel):
    return parcel["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]


def _move(c, east, north, idx=(0, 1)):
    return c.post(f"/documents/{DOC}/move-parcels", json={
        "page_number": 3, "region_index": 0, "parcel_indexes": list(idx), "east_m": east, "north_m": north})


def test_every_vertex_moves_by_the_same_vector_and_the_shape_is_untouched(client):
    c, root = client
    assert _move(c, 30.0, -12.0).status_code == 200
    a, b = _stored(root)
    for parcel, old in ((a, A), (b, B)):
        ring = _ring(parcel)
        assert len(ring) == len(old) and ring[0] == ring[-1]
        for new, was in zip(ring, old):
            az, _, d = GEOD.inv(was[0], was[1], new[0], new[1])
            assert d == pytest.approx(math.hypot(30, 12), abs=0.01)
        # identical edge lengths: a pure translation
        for i in range(len(old) - 1):
            before = GEOD.inv(old[i][0], old[i][1], old[i + 1][0], old[i + 1][1])[2]
            after = GEOD.inv(ring[i][0], ring[i][1], ring[i + 1][0], ring[i + 1][1])[2]
            assert after == pytest.approx(before, abs=0.01)
        assert parcel["manual_position"] == {"confirmation_id": "c1", "east_m": 30.0, "north_m": -12.0}
        assert parcel["boundary_geojson_wgs84"]["properties"]["georeferenced"] == "manually_placed"
        assert any("moved by hand" in n for n in parcel["placement"]["notes"])


def test_moving_the_group_keeps_the_shared_edge_shared(client):
    c, root = client
    _move(c, 17.5, 9.0)
    a, b = _stored(root)
    # A's east edge vertices are B's west edge vertices
    for ia, ib in ((1, 0), (2, 3)):
        assert GEOD.inv(*_ring(a)[ia], *_ring(b)[ib])[2] < 0.01


def test_moves_accumulate_and_reset_restores_the_computed_position_exactly(client):
    c, root = client
    _move(c, 10.0, 0.0)
    _move(c, 0.0, 5.0)
    a, _ = _stored(root)
    assert a["manual_position"]["east_m"] == 10.0 and a["manual_position"]["north_m"] == 5.0
    assert a["boundary_geojson_wgs84_computed"]["geometry"]["coordinates"][0] == A      # the baseline never drifts
    r = c.post(f"/documents/{DOC}/reset-position", json={"page_number": 3, "region_index": 0, "parcel_indexes": [0, 1]})
    assert r.status_code == 200
    a, b = _stored(root)
    assert _ring(a) == A and _ring(b) == B
    assert "manual_position" not in a and "boundary_geojson_wgs84_computed" not in a
    assert not any("moved by hand" in n for n in a["placement"]["notes"])


def test_only_the_chosen_parcels_move(client):
    c, root = client
    _move(c, 40.0, 0.0, idx=(1,))
    a, b = _stored(root)
    assert _ring(a) == A and "manual_position" not in a
    assert _ring(b) != B and b["manual_position"]["east_m"] == 40.0


def test_a_move_survives_reverification_which_recomputes_the_position(client):
    c, root = client
    _move(c, 25.0, 10.0)
    parcel = _stored(root)[0]
    # re-verification writes a freshly computed ring (different from the old one), then re-applies the move
    new_computed = [[lon + 0.0003, lat + 0.0002] for lon, lat in A]
    parcel["boundary_geojson_wgs84"] = _feature(new_computed)
    docs._reapply_manual_position(parcel)
    assert parcel["boundary_geojson_wgs84_computed"]["geometry"]["coordinates"][0] == new_computed
    for got, base in zip(_ring(parcel), new_computed):
        assert GEOD.inv(base[0], base[1], got[0], got[1])[2] == pytest.approx(math.hypot(25, 10), abs=0.01)


def test_a_move_made_before_the_outline_was_re_confirmed_is_dropped():
    parcel = _parcel("A", A, conf_id="old")
    parcel["manual_position"] = {"confirmation_id": "old", "east_m": 30.0, "north_m": 0.0}
    parcel["boundary_geojson_wgs84_computed"] = _feature(A)
    parcel["confirmed_boundary_pixels"]["id"] = "new"                 # the user re-confirmed with new vertices
    fresh = [[lon + 0.001, lat] for lon, lat in A]
    parcel["boundary_geojson_wgs84"] = _feature(fresh)
    docs._reapply_manual_position(parcel)
    assert "manual_position" not in parcel and _ring(parcel) == fresh


def test_automatic_fits_leave_a_hand_placed_parcel_alone(client):
    c, root = client
    _move(c, 5.0, 5.0)
    result = json.loads((root / DOC / "result.json").read_text())
    region = result["pages"][0]["regions"][0]
    assert docs._control_fit_members(region) is None                     # treated as reliably placed
    before = copy.deepcopy(result)
    assert docs._unify_sheet_frame(result, 3) is False
    docs._fit_sheet_to_aliquot(result, 3)
    assert result == before


def test_unplaced_or_absurd_moves_are_refused(client):
    c, root = client
    result = json.loads((root / DOC / "result.json").read_text())
    result["pages"][0]["regions"][0]["parcels"][1].pop("boundary_geojson_wgs84")
    (root / DOC / "result.json").write_text(json.dumps(result))
    assert _move(c, 5.0, 5.0).status_code == 400                          # parcel B has no placed geometry
    assert _move(c, 5.0, 5.0, idx=(0,)).status_code == 200
    assert _move(c, 1e7, 0.0, idx=(0,)).status_code == 400
    assert c.post(f"/documents/{DOC}/move-parcels", json={
        "page_number": 3, "region_index": 0, "parcel_indexes": [9], "east_m": 1, "north_m": 1}).status_code == 404
