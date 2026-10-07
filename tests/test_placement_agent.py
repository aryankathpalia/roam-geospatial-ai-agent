"""The placement-review agent loop and its proposal endpoints, with a scripted model (no network)."""
import json

import pytest

from app.routes import documents as docs
from app.services import placement_agent as pa
from tests.test_agent_tools import _doc


def _call(name, **args):
    return {"id": f"c-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _script(monkeypatch, replies):
    """The model answers with `replies` in order; records the messages it was sent."""

    sent = []

    def fake_chat(client, messages, tools):
        sent.append([dict(m) for m in messages])
        return replies.pop(0), "fake-model"

    monkeypatch.setattr(pa, "_chat", fake_chat)
    return sent


def test_move_is_proposed_only_after_it_was_scored(monkeypatch):
    doc = _doc(neighbour_offset_m=-300.0)
    sent = _script(monkeypatch, [
        {"tool_calls": [_call("propose_move", page_number=3, east_m=0, north_m=-300, reason="r")]},  # not scored yet
        {"tool_calls": [_call("evaluate_position", page_number=3, east_m=0, north_m=-300)]},
        {"tool_calls": [_call("propose_move", page_number=3, east_m=0, north_m=-300, reason="neighbours 300 m south")]},
        {"content": "The sheet sits 300 m north of its neighbours; I proposed moving it."},
    ])
    out = pa.run(doc, "why is it wrong?", focus={"page_number": 3, "label": "PARCEL 1"})
    assert out["reply"].startswith("The sheet") and out["model"] == "fake-model"
    assert len(out["proposals"]) == 1
    prop = out["proposals"][0]
    assert prop["kind"] == "move" and prop["north_m"] == -300 and prop["status"] == "pending"
    assert prop["score"]["outline_hugging_a_neighbour_pct"] > 90
    # the first, unscored attempt came back to the model as an error
    first_tool_reply = next(m for m in sent[1] if m["role"] == "tool")
    assert "evaluate_position" in first_tool_reply["content"]
    assert "page 3, parcel PARCEL 1" in sent[0][-1]["content"]
    assert doc["pages"][0]["regions"][0]["parcels"][0].get("manual_position") is None  # nothing applied


def test_search_result_counts_as_scored_and_bad_arguments_are_survivable(monkeypatch):
    doc = _doc(neighbour_offset_m=-300.0)
    _script(monkeypatch, [
        {"tool_calls": [_call("search_position_near", page_number="3", east_m="0", north_m="-280")]},
        {"tool_calls": [{"id": "x", "function": {"name": "get_parcel_details", "arguments": "{not json"}}]},
        {"tool_calls": [_call("propose_move", page_number=3, east_m=0, north_m=-300, reason="fit")]},
        {"content": "done"},
    ])
    out = pa.run(doc, "place it")
    assert [p["kind"] for p in out["proposals"]] == ["move"]
    assert any("not valid JSON" in s["summary"] or "get_parcel_details" in s["summary"] for s in out["steps"])


def test_area_proposal_and_apply_discard_endpoints(monkeypatch):
    doc = _doc()
    doc["pages"][0]["regions"][0]["parcels"][0]["spatial_validation"]["area_sqft"] = 17222.0
    _script(monkeypatch, [
        {"tool_calls": [_call("propose_stated_area", page_number=3, label="PARCEL 1", stated_area="0.40 AC", reason="blurred")]},
        {"tool_calls": [_call("propose_stated_area", page_number=3, label="PARCEL 1", stated_area="unreadable", reason="x")]},
        {"content": "proposed"},
    ])
    store = {"r": doc}
    monkeypatch.setattr(docs, "_load_result", lambda _id: store["r"])
    monkeypatch.setattr(docs, "_save_result", lambda _id, r: store.update(r=r))
    out = docs.agent_chat("doc", docs.AgentChatRequest(message="area is wrong", page_number=3, label="PARCEL 1"))
    assert len(out["proposals"]) == 1 and store["r"]["agent_proposals"][0]["kind"] == "stated_area"
    pid = out["proposals"][0]["id"]
    applied = docs.apply_agent_proposal("doc", pid)["proposal"]
    entity = store["r"]["pages"][0]["sheet"]["parcels"][0]
    assert applied["status"] == "applied" and entity["stated_area"] == "0.40 AC" and entity["stated_area_edited"]
    with pytest.raises(docs.HTTPException):
        docs.apply_agent_proposal("doc", pid)  # not twice
    with pytest.raises(docs.HTTPException):
        docs.discard_agent_proposal("doc", "nope")


def test_applying_a_move_proposal_moves_the_sheet_by_hand(monkeypatch):
    doc = _doc()
    parcel = doc["pages"][0]["regions"][0]["parcels"][0]
    parcel["confirmed_boundary_pixels"] = {"id": "c1", "vertices": [[0, 0], [1, 0], [1, 1]]}
    doc["agent_proposals"] = [{"id": "p1", "status": "pending", "kind": "move", "page_number": 3, "east_m": 5.0, "north_m": -12.0}]
    store = {"r": doc}
    monkeypatch.setattr(docs, "_load_result", lambda _id: store["r"])
    monkeypatch.setattr(docs, "_save_result", lambda _id, r: store.update(r=r))
    before = parcel["boundary_geojson_wgs84"]["geometry"]["coordinates"][0][0][1]
    docs.apply_agent_proposal("doc", "p1")
    moved = store["r"]["pages"][0]["regions"][0]["parcels"][0]
    assert moved["manual_position"]["north_m"] == -12.0 and moved["manual_position"]["east_m"] == 5.0
    assert abs((moved["boundary_geojson_wgs84"]["geometry"]["coordinates"][0][0][1] - before) * 110_540 + 12) < 0.5
    assert store["r"]["agent_proposals"][0]["status"] == "applied"
