"""
The placement-review agent: an LLM (via OpenRouter's OpenAI-compatible API) that investigates why a sheet's
location is unconfirmed or wrong, using the read-only tools in agent_tools.py, and answers in plain language.

It never changes a document. It can PROPOSE a fix -- move a sheet's confirmed parcels, or correct a misread
printed area -- which is stored as pending on the result and applied only when the user approves it
(routes/documents.py). A proposed move must first be scored with evaluate_position, and the score is kept
with the proposal so the user sees the evidence behind it.

Stateless across turns: the caller passes the visible conversation (user/assistant text) back each turn.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import httpx

from app.core.config import settings
from app.services import agent_tools

logger = logging.getLogger(__name__)

_URL = "https://openrouter.ai/api/v1/chat/completions"
_TIMEOUT_S = 120.0
_MAX_STEPS = 14
_MAX_TOOL_RESULT_CHARS = 6000
_MAX_HISTORY_TURNS = 12
# Free models come and go and rate-limit hard: tried in order on 429 / 5xx / an unusable reply.
_FALLBACK_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
]

SYSTEM_PROMPT = """You are ROAM's placement reviewer: a careful land-survey analyst. ROAM reads scanned plats and deeds, \
the user confirms each parcel's outline on the drawing, and ROAM places those outlines on a satellite map. Your job \
is to find out whether a sheet's parcels sit in the right place, explain why not, and propose a fix.

How ROAM places a sheet:
- Shapes come from the user's confirmed outline; size from the printed areas and edge lengths (calibration).
- Position starts from a document anchor: a printed state-plane coordinate, a PLSS corner, an address, or the \
county parcels whose APNs the plat prints. Then fits refine it: the county-parcel fit (slides the parcels into the \
gap between the APN neighbours), and the control-point fit (binds printed coordinates to corners).

Failure modes seen before (check for them, do not assume them):
- Ground vs grid coordinates: a plat printing GROUND coordinates with a combined factor must be scaled by \
1/factor before conversion; skipping that misplaces a Washoe sheet by ~900 m.
- APNs renumbered since the plat was drawn: the county's current parcels for those APNs are scattered or missing, \
so the county fit has nothing valid to fit to.
- A misread printed area (blurred label): the outline is right but the area check fails.
- A wrong rotation from one misread bearing.

Rules:
- Investigate with tools before concluding; start with get_document_overview. Cite the numbers you found.
- Never invent coordinates, APNs or measurements. If the evidence is insufficient, say what is missing.
- To place a sheet: work out roughly where it belongs from the evidence (e.g. a correctly converted printed \
coordinate, or where the plat's APN neighbours are), then call search_position_near from that rough move to find \
the exact spot. Propose its best_total_move (when its rotation is 0) with propose_move, or a move you scored with \
evaluate_position. Low overlap and high hugging is a good fit; say whether it is corroborated.
- Proposing IS asking: the user approves or discards each proposal in the interface, so when the evidence supports \
a fix, propose it in this turn instead of asking permission. You cannot change anything yourself.
- Refer to things by their meaning (e.g. "the printed area"), not by internal field names.
- Answer in short, plain language for a non-technical user: what is wrong, why, what you propose. No JSON.
"""

_PROPOSAL_TOOLS = [
    {"type": "function", "function": {
        "name": "propose_move",
        "description": "Propose moving ALL confirmed parcels of a sheet together by east_m / north_m metres. Must be "
                       "scored with evaluate_position first. The user approves or discards it.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["page_number", "east_m", "north_m", "reason"],
                       "properties": {
                           "page_number": {"type": "integer"}, "east_m": {"type": "number"}, "north_m": {"type": "number"},
                           "reason": {"type": "string", "description": "one or two sentences of evidence"}}},
    }},
    {"type": "function", "function": {
        "name": "propose_stated_area",
        "description": "Propose correcting a parcel's printed area where it was misread (e.g. '1.55 AC').",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["page_number", "label", "stated_area", "reason"],
                       "properties": {
                           "page_number": {"type": "integer"}, "label": {"type": "string"},
                           "stated_area": {"type": "string"}, "reason": {"type": "string"}}},
    }},
]


class AgentError(RuntimeError):
    pass


def _models() -> list[str]:
    first = settings.OPENROUTER_MODEL.strip()
    return list(dict.fromkeys(([first] if first and first != "openrouter/free" else []) + _FALLBACK_MODELS))


def _chat(client: httpx.Client, messages: list[dict], tools: list[dict]) -> tuple[dict, str]:
    """One completion; falls through the model list on rate limits, server errors and empty replies."""

    if not settings.OPENROUTER_API_KEY:
        raise AgentError("OPENROUTER_API_KEY is not set in .env")
    last = "no model answered"
    for model in _models():
        try:
            body: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.2}
            if tools:
                body.update(tools=tools, tool_choice="auto")
            r = client.post(_URL, json=body, headers={
                "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                "HTTP-Referer": "http://localhost:5173", "X-Title": "ROAM placement reviewer",
            }, timeout=_TIMEOUT_S)
        except httpx.HTTPError as exc:
            last = f"{model}: {exc}"
            continue
        if r.status_code in (429, 500, 502, 503, 504) or r.status_code == 404:
            last = f"{model}: HTTP {r.status_code}"
            logger.warning("agent model %s unavailable (%s)", model, r.status_code)
            continue
        if r.status_code >= 400:
            raise AgentError(f"{model}: HTTP {r.status_code} {r.text[:300]}")
        data = r.json()
        msg = ((data.get("choices") or [{}])[0]).get("message") or {}
        if not msg.get("content") and not msg.get("tool_calls"):
            last = f"{model}: empty reply"
            continue
        return msg, model
    raise AgentError(f"every model failed ({last})")


def _summary(name: str, args: dict, out: dict) -> str:
    """A one-line description of a tool call for the user's 'what I checked' list."""

    if "error" in out:
        return f"{name}: {out['error']}"
    if name == "evaluate_position":
        return (f"Tested a move of {args.get('east_m', 0):+.0f} m east, {args.get('north_m', 0):+.0f} m north: "
                f"{out['outline_hugging_a_neighbour_pct']}% of the outline against a neighbour, "
                f"{out['overlap_with_neighbours_pct']}% overlap")
    if name == "search_position_near":
        best = out["best_total_move"]
        return (f"Searched for the best fit among the county parcels: {best['east_m']:+.0f} m east, "
                f"{best['north_m']:+.0f} m north ({'corroborated' if out['corroborated'] else 'not corroborated'})")
    if name == "lookup_county_apns":
        return f"Looked up {len(out['apns'])} APNs in the county records ({out['found']} found)"
    if name == "county_parcels_near":
        return f"Listed {out['count']} county parcels within {out['radius_m']:.0f} m of a point"
    if name == "convert_state_plane":
        return f"Converted N {args.get('northing')} E {args.get('easting')}" + (" as ground coordinates" if args.get("ground_to_grid") else "")
    if name == "search_document_text":
        return f"Searched the document text for “{args.get('pattern')}” ({len(out['hits'])} hits)"
    return {"get_document_overview": "Read the document's placement state",
            "get_location_evidence": f"Read the printed location evidence on page {args.get('page_number')}",
            "get_parcel_details": f"Read {args.get('label')}'s details"}.get(name, name)


def _propose(result: dict, name: str, args: dict, evaluated: list[dict]) -> dict:
    """Validates a proposal against the document and returns it (status pending), or an {'error'}."""

    page = args.get("page_number")
    if not any(p["page_number"] == page and agent_tools._confirmed(p) for p in result.get("pages", [])):
        return {"error": f"page {page} has no confirmed, placed parcels"}
    proposal: dict[str, Any] = {"id": uuid.uuid4().hex[:12], "status": "pending", "created_at": time.time(),
                                "page_number": page, "reason": str(args.get("reason") or "")[:600]}
    if name == "propose_move":
        e, n = float(args["east_m"]), float(args["north_m"])
        if not (abs(e) < 20_000 and abs(n) < 20_000):
            return {"error": "move too large"}
        score = next((s for s in reversed(evaluated) if s.get("page") == page
                      and abs(s.get("east_m", 0) - e) < 1 and abs(s.get("north_m", 0) - n) < 1
                      and not s.get("rotation_deg")), None)
        if score is None:
            return {"error": "score this exact move with evaluate_position (rotation 0) before proposing it"}
        baseline = next((s for s in evaluated if s.get("page") == page and not s.get("east_m") and not s.get("north_m")), None)
        proposal.update({"kind": "move", "east_m": round(e, 2), "north_m": round(n, 2), "score": score, "baseline": baseline})
    else:
        label = str(args.get("label") or "")
        try:
            agent_tools.get_parcel_details(result, page, label)
        except ValueError as exc:
            return {"error": str(exc)}
        from app.services import parcel_roster

        if not parcel_roster.parse_acres(args.get("stated_area")):
            return {"error": "stated_area not understood -- e.g. '1.55 AC' or '67,400 SQ. FT.'"}
        proposal.update({"kind": "stated_area", "label": label, "stated_area": str(args["stated_area"])})
    return proposal


def run(result: dict, user_message: str, history: list[dict] | None = None, focus: dict | None = None) -> dict:
    """
    One user turn. `history`: earlier visible turns [{role: user|assistant, content}]; `focus`: optional
    {page_number, label} of the card the user opened the chat from. Returns {reply, steps, proposals, model}.
    The result dict is read, never modified.
    """

    context = ""
    if focus:
        context = f"\n\n(The user opened this chat from page {focus.get('page_number')}" + (
            f", parcel {focus['label']}" if focus.get("label") else "") + ".)"
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in (history or [])[-_MAX_HISTORY_TURNS:]:
        if turn.get("role") in ("user", "assistant") and isinstance(turn.get("content"), str):
            messages.append({"role": turn["role"], "content": turn["content"][:4000]})
    messages.append({"role": "user", "content": user_message.strip()[:4000] + context})

    tools = agent_tools.tool_specs() + _PROPOSAL_TOOLS
    steps: list[dict] = []
    proposals: list[dict] = []
    evaluated: list[dict] = []
    model = None
    with httpx.Client(headers={"User-Agent": "ROAM/1.0"}) as client:
        for _ in range(_MAX_STEPS):
            msg, model = _chat(client, messages, tools)
            calls = msg.get("tool_calls") or []
            if not calls:
                return {"reply": (msg.get("content") or "").strip(), "steps": steps, "proposals": proposals, "model": model}
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}") or {}
                except json.JSONDecodeError:
                    args = {}
                    out: dict = {"error": "arguments were not valid JSON"}
                else:
                    if name in ("propose_move", "propose_stated_area"):
                        out = _propose(result, name, args, evaluated)
                        if "error" not in out:
                            proposals.append(out)
                            out = {"ok": True, "proposal_id": out["id"], "note": "shown to the user for approval"}
                    else:
                        out = agent_tools.run_tool(result, name, args)
                        if name == "evaluate_position" and "error" not in out:
                            evaluated.append(out)
                        if name == "search_position_near" and "error" not in out:
                            best = out["best_total_move"]
                            # the search's answer counts as scored: proposing exactly it needs no extra call
                            score = agent_tools.run_tool(result, "evaluate_position", {
                                "page_number": out["page"], "east_m": best["east_m"], "north_m": best["north_m"]})
                            if "error" not in score:
                                evaluated.append(score)
                steps.append({"tool": name, "args": args, "summary": _summary(name, args, out) if "ok" not in out else
                              f"Proposed: {name.replace('propose_', '').replace('_', ' ')}"})
                text = json.dumps(out, default=str)
                messages.append({"role": "tool", "tool_call_id": call.get("id", name), "name": name,
                                 "content": text[:_MAX_TOOL_RESULT_CHARS]})
        # out of steps: ask for the answer without tools
        messages.append({"role": "user", "content": "Stop investigating and answer now with what you found."})
        msg, model = _chat(client, messages, [])
    return {"reply": (msg.get("content") or "").strip(), "steps": steps, "proposals": proposals, "model": model}
