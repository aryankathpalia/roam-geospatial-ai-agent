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
    assert prop["check"]["kind"] == "county_parcels" and prop["check"]["hugging_pct"] > 90
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


def test_imagery_look_that_lines_up_backs_a_move_and_steps_stream(monkeypatch):
    from app.services import agent_tools

    doc = _doc()

    def fake_look(result, page_number, east_m=0.0, north_m=0.0, user_note=None):
        lined = abs(east_m + 70) < 1 and abs(north_m - 80) < 1
        return {"page": page_number, "viewed_at_move": {"east_m": east_m, "north_m": north_m},
                "features_located": [{"side": "east", "name": "CROSS ROAD", "boundary_on": "centreline",
                                      "move_to_meet_it_m": 0.7 if lined else -70.0}],
                "lines_up": lined, "suggested_extra_move_m": {"east_m": 0 if lined else -70.0, "north_m": 0 if lined else 80.0},
                "suggested_total_move_m": {"east_m": -70.0, "north_m": 80.0}, "_image_jpeg_b64": "aGk="}

    monkeypatch.setitem(agent_tools.TOOLS, "look_at_imagery", (fake_look, *agent_tools.TOOLS["look_at_imagery"][1:]))
    _script(monkeypatch, [
        {"tool_calls": [_call("look_at_imagery", page_number=3, user_note="it is off")]},
        {"tool_calls": [_call("propose_move", page_number=3, east_m=-70, north_m=80, reason="r")]},  # not yet confirmed
        {"tool_calls": [_call("look_at_imagery", page_number=3, east_m=-70, north_m=80)]},
        {"tool_calls": [_call("propose_move", page_number=3, east_m=-70, north_m=80, reason="road on the east")]},
        {"content": "Moved it next to Cross Road."},
    ])
    events = list(pa.run_events(doc, "fix it"))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "thinking" and kinds[-1] == "done" and kinds.count("step_start") == kinds.count("step_done") == 4
    looks = [e for e in events if e["type"] == "step_done" and e["tool"] == "look_at_imagery"]
    assert looks[0]["image"] == "aGk=" and "need to move 80 m north, 70 m west" in looks[0]["summary"]
    assert "line up with Cross Road (east)" in looks[1]["summary"]
    done = events[-1]
    assert len(done["proposals"]) == 1 and done["proposals"][0]["check"]["kind"] == "imagery"
    assert "image" not in done["steps"][0]  # images go to the live step only, not into the saved answer


def test_weak_county_fit_cannot_back_a_move(monkeypatch):
    doc = _doc(neighbour_offset_m=-300.0)
    _script(monkeypatch, [
        {"tool_calls": [_call("evaluate_position", page_number=3, east_m=0, north_m=-250)]},  # off target: weak
        {"tool_calls": [_call("propose_move", page_number=3, east_m=0, north_m=-250, reason="r")]},
        {"content": "no fix"},
    ])
    out = pa.run(doc, "fix it")
    assert out["proposals"] == [] and "only weakly" in out["steps"][1]["summary"]
