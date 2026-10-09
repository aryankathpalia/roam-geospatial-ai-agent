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
from typing import Any, Iterator

import httpx

from app.core.config import settings
from app.services import agent_tools

logger = logging.getLogger(__name__)

_URL = "https://openrouter.ai/api/v1/chat/completions"
_TIMEOUT_S = 120.0
_MAX_STEPS = 14
_MAX_TOOL_RESULT_CHARS = 6000
_MAX_HISTORY_TURNS = 12
_MATCH_M = 3.0  # a proposed move this close to a checked one is that move
_STRONG_HUGGING_PCT = 50.0  # as the pipeline's own county fit: half the outline against a neighbour ...
_MAX_OVERLAP_PCT = 3.0  # ... and almost no overlap with one
# Free models come and go and rate-limit hard: tried in order on 429 / 5xx / an unusable reply.
_FALLBACK_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
]

SYSTEM_PROMPT = """You are ROAM's placement reviewer: a careful land-survey analyst. ROAM reads scanned plats and deeds, \
the user confirms each parcel's outline on the drawing, and ROAM places those outlines on a satellite map. Your job \
is to find out whether a sheet's parcels sit in the right place, explain why not, and propose a fix.
Write plain, simple sentences. Never use the long dash character; use commas, colons or full stops instead.

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
- With county records (APNs found): work out roughly where the sheet belongs (a correctly converted printed \
coordinate, where the APN neighbours are), then search_position_near from that rough move; propose its \
best_total_move when its rotation is 0.
- With no county records or printed coordinates (most places outside Washoe County, NV), or to double-check: call \
find_position_from_imagery ONCE (pass the user's own words as user_note). It reads the plat for the roads/canals \
bordering the parcels, lines the outlines up with them on the satellite imagery, confirms, and returns the best \
total move with its uncertainty. Propose that best_total_move. If it does not settle, say what it saw instead. \
Do not repeat it or look_at_imagery to chase a few metres: single readings vary by several metres.
- If the user states the move themselves ("15 m west"), look at it if you can; propose it with \
from_user_instruction=true when it cannot be checked.
- Do not stop at "not enough evidence" while find_position_from_imagery is untried.
- County records can be stale: if the plat's APN neighbours are scattered or the best county fit is not \
corroborated, do not trust them; use the imagery.
- Parcels the user moved by hand (moved_by_hand in the overview) are where the user decided they belong: say so, \
and propose moving them only on strong evidence that they are wrong.
- Proposing IS asking: the user approves or discards each proposal in the interface, so when the evidence supports \
a fix, propose it in this turn instead of asking permission. You cannot change anything yourself.
- Refer to things by their meaning (e.g. "the printed area"), not by internal field names.
- Answer in short, plain language for a non-technical user: what is wrong, why, what you propose. No JSON.
"""

_PROPOSAL_TOOLS = [
    {"type": "function", "function": {
        "name": "propose_move",
        "description": "Propose moving ALL confirmed parcels of a sheet together by east_m / north_m metres (the TOTAL "
                       "move from where they are now). The move must have been checked: a look_at_imagery at it that "
                       "lines up, or evaluate_position / search_position_near. The user approves or discards it.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["page_number", "east_m", "north_m", "reason"],
                       "properties": {
                           "page_number": {"type": "integer"}, "east_m": {"type": "number"}, "north_m": {"type": "number"},
                           "reason": {"type": "string", "description": "one or two sentences of evidence"},
                           "from_user_instruction": {"type": "boolean",
                                                     "description": "true only when the user stated this move themselves"}}},
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


_STEP_LABELS = {
    "get_document_overview": "Reading how the sheet was placed",
    "get_location_evidence": "Reading the location evidence printed on the plat",
    "convert_state_plane": "Converting a printed survey coordinate",
    "county_parcels_near": "Checking the county's parcel records at a spot",
    "lookup_county_apns": "Looking up the plat's APNs in the county records",
    "evaluate_position": "Testing a position against the neighbouring parcels",
    "search_position_near": "Searching for the best fit among the county parcels",
    "look_at_imagery": "Comparing the plat with the satellite imagery",
    "find_position_from_imagery": "Lining the parcels up with the roads and canals on the imagery",
    "get_parcel_details": "Reading a parcel's measurements",
    "search_document_text": "Searching the document text",
    "propose_move": "Preparing the proposed move",
    "propose_stated_area": "Preparing the proposed area correction",
}


def _direction(e: float, n: float) -> str:
    parts = [f"{abs(n):.0f} m {'north' if n >= 0 else 'south'}" if abs(n) >= 0.5 else "",
             f"{abs(e):.0f} m {'east' if e >= 0 else 'west'}" if abs(e) >= 0.5 else ""]
    return ", ".join(p for p in parts if p) or "no move"


def _summary(name: str, args: dict, out: dict) -> str:
    """One plain line describing what a tool call found, for the user's step list."""

    if "error" in out:
        return f"Could not complete: {out['error']}"
    if name == "look_at_imagery":
        feats = ", ".join(f"{f['name'].title()} ({f['side']})" for f in out.get("features_located", []))
        at = out["viewed_at_move"]
        where = "at the current position" if not (at["east_m"] or at["north_m"]) else f"moved {_direction(at['east_m'], at['north_m'])}"
        if not feats:
            return f"Looked at the imagery {where}: could not find the features the plat draws around the parcels"
        if out.get("lines_up"):
            return f"Looked at the imagery {where}: the outlines line up with {feats}"
        extra = out["suggested_extra_move_m"]
        return f"Looked at the imagery {where}: found {feats}. The outlines need to move {_direction(extra['east_m'], extra['north_m'])}"
    if name == "find_position_from_imagery":
        if not out.get("settled"):
            return f"Compared the plat with the imagery {len(out.get('looks') or [])} times, but the readings did not settle"
        best = out["best_total_move"]
        return (f"Lined the parcels up with {', '.join(out['lines_up_with'])} on the imagery: they belong "
                f"{_direction(best['east_m'], best['north_m'])} from here (±{out['uncertainty_m']:.0f} m, {out['looks']} looks)")
    if name == "evaluate_position":
        return (f"Tested a move {_direction(args.get('east_m', 0), args.get('north_m', 0))}: "
                f"{out['outline_hugging_a_neighbour_pct']}% of the outline against a neighbouring parcel, "
                f"{out['overlap_with_neighbours_pct']}% overlap")
    if name == "search_position_near":
        best = out["best_total_move"]
        return (f"Best fit among the county parcels: {_direction(best['east_m'], best['north_m'])} "
                f"({'corroborated' if out['corroborated'] else 'not corroborated'})")
    if name == "lookup_county_apns":
        return f"Looked up {len(out['apns'])} APNs in the county records: {out['found']} found"
    if name == "county_parcels_near":
        return f"Found {out['count']} county parcels within {out['radius_m']:.0f} m of a point"
    if name == "convert_state_plane":
        return f"Converted N {args.get('northing')} E {args.get('easting')}" + (" as ground coordinates" if args.get("ground_to_grid") else "")
    if name == "search_document_text":
        return f"Searched the document text for “{args.get('pattern')}”: {len(out['hits'])} hits"
    if name == "get_location_evidence":
        n = len(out.get("printed_coordinates") or [])
        return (f"Plat evidence: {len(out.get('apns') or [])} APNs, {n} printed coordinate{'s' if n != 1 else ''}"
                + (f", address {out['address']}" if out.get("address") else ""))
    return {"get_document_overview": "Read how the sheet was placed",
            "get_parcel_details": f"Read {args.get('label')}'s measurements"}.get(name, name)


def _checked_move(checks: list[dict], page: int, e: float, n: float) -> dict | None:
    """The latest check of exactly this move: a county-parcel score, or an imagery look that lines up."""

    return next((c for c in reversed(checks) if c["page"] == page and abs(c["east_m"] - e) <= _MATCH_M
                 and abs(c["north_m"] - n) <= _MATCH_M), None)


def _strong_county_fit(check: dict) -> bool:
    """A county-parcel score good enough to move a sheet on: corroborated, or snug against its neighbours."""

    return bool(check.get("corroborated")) or (
        (check.get("hugging_pct") or 0) >= _STRONG_HUGGING_PCT and (check.get("overlap_pct") or 0) <= _MAX_OVERLAP_PCT
    )


def _propose(result: dict, name: str, args: dict, checks: list[dict]) -> dict:
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
        check = _checked_move(checks, page, e, n)
        if check and check["kind"] == "county_parcels" and not _strong_county_fit(check):
            return {"error": f"the county parcels back this move only weakly ({check['hugging_pct']}% of the outline "
                             f"against a neighbour, not corroborated) -- the plat's APNs may have been renumbered. "
                             "Do not propose it; check the position with find_position_from_imagery instead."}
        if check is None and not args.get("from_user_instruction"):
            known = [{"east_m": c["east_m"], "north_m": c["north_m"]} for c in checks if c["page"] == page]
            return {"error": "this move was not checked. " + (f"Checked moves you can propose: {known}" if known else
                    "Check it first with find_position_from_imagery, look_at_imagery or evaluate_position.")}
        proposal.update({"kind": "move", "east_m": round(e, 2), "north_m": round(n, 2),
                         "check": check, "from_user_instruction": bool(args.get("from_user_instruction")) and check is None})
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


def _record_check(name: str, out: dict, result: dict, checks: list[dict]) -> None:
    """Remembers the moves a tool verified, so a proposal of one of them needs no second check."""

    if "error" in out:
        return
    if name == "evaluate_position":
        checks.append({"page": out["page"], "east_m": out["east_m"], "north_m": out["north_m"], "kind": "county_parcels",
                       "hugging_pct": out["outline_hugging_a_neighbour_pct"], "overlap_pct": out["overlap_with_neighbours_pct"]})
    elif name == "search_position_near":
        best = out["best_total_move"]
        if not best.get("rotation_deg"):
            checks.append({"page": out["page"], "east_m": best["east_m"], "north_m": best["north_m"], "kind": "county_parcels",
                           "hugging_pct": round((out.get("outline_hugging_share") or 0) * 100, 1),
                           "overlap_pct": round((out.get("overlap_with_neighbours") or 0) * 100, 1),
                           "corroborated": out.get("corroborated")})
    elif name == "find_position_from_imagery" and out.get("settled"):
        best = out["best_total_move"]
        checks.append({"page": out["page"], "east_m": best["east_m"], "north_m": best["north_m"], "kind": "imagery",
                       "features": out["lines_up_with"], "uncertainty_m": out["uncertainty_m"]})
    elif name == "look_at_imagery" and out.get("lines_up"):
        at = out["viewed_at_move"]
        checks.append({"page": out["page"], "east_m": at["east_m"], "north_m": at["north_m"], "kind": "imagery",
                       "features": [f"{f['name']} ({f['side']}, {abs(f['move_to_meet_it_m'])} m)" for f in out["features_located"]]})


def run_events(result: dict, user_message: str, history: list[dict] | None = None,
               focus: dict | None = None) -> Iterator[dict]:
    """
    One user turn as a stream of events, for a live step list:
      {"type": "thinking"}                                  -- the model is deciding its next step
      {"type": "step_start", "id", "label"}                 -- a check started
      {"type": "step_done", "id", "summary", "ok", "image"?} -- it finished (image: JPEG base64 of a look)
      {"type": "done", "reply", "proposals", "model", "steps"}
      {"type": "error", "message"}
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
    checks: list[dict] = []
    model = None
    try:
        with httpx.Client(headers={"User-Agent": "ROAM/1.0"}) as client:
            for _ in range(_MAX_STEPS):
                yield {"type": "thinking"}
                msg, model = _chat(client, messages, tools)
                calls = msg.get("tool_calls") or []
                if not calls:
                    yield {"type": "done", "reply": (msg.get("content") or "").strip(), "proposals": proposals,
                           "model": model, "steps": steps}
                    return
                messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
                for call in calls:
                    fn = call.get("function") or {}
                    name = fn.get("name", "")
                    step_id = f"s{len(steps) + 1}"
                    yield {"type": "step_start", "id": step_id, "label": _STEP_LABELS.get(name, name)}
                    try:
                        args = json.loads(fn.get("arguments") or "{}") or {}
                    except json.JSONDecodeError:
                        args, out = {}, {"error": "arguments were not valid JSON"}
                    else:
                        if name in ("propose_move", "propose_stated_area"):
                            out = _propose(result, name, args, checks)
                            if "error" not in out:
                                proposals.append(out)
                                out = {"ok": True, "proposal_id": out["id"], "note": "shown to the user for approval"}
                        else:
                            out = agent_tools.run_tool(result, name, args)
                            _record_check(name, out, result, checks)
                    image = out.pop("_image_jpeg_b64", None)
                    summary = ("Proposed a fix for you to review" if out.get("ok") else _summary(name, args, out))
                    step = {"id": step_id, "tool": name, "summary": summary, "ok": "error" not in out}
                    steps.append(step)
                    yield {"type": "step_done", **step, **({"image": image} if image else {})}
                    messages.append({"role": "tool", "tool_call_id": call.get("id", name), "name": name,
                                     "content": json.dumps(out, default=str)[:_MAX_TOOL_RESULT_CHARS]})
            messages.append({"role": "user", "content": "Stop investigating and answer now with what you found."})
            yield {"type": "thinking"}
            msg, model = _chat(client, messages, [])
            yield {"type": "done", "reply": (msg.get("content") or "").strip(), "proposals": proposals,
                   "model": model, "steps": steps}
    except AgentError as exc:
        if proposals:  # the work is done; only the write-up failed -- keep the proposal
            yield {"type": "done", "reply": "The AI model is busy, so there is no written summary -- the checks above "
                   "and the proposed fix below are complete.", "proposals": proposals, "model": model, "steps": steps}
        else:
            yield {"type": "error", "message": str(exc)}


def run(result: dict, user_message: str, history: list[dict] | None = None, focus: dict | None = None) -> dict:
    """run_events collected into one answer: {reply, steps, proposals, model}. Raises AgentError on failure."""

    for event in run_events(result, user_message, history, focus):
        if event["type"] == "done":
            return {k: event[k] for k in ("reply", "steps", "proposals", "model")}
        if event["type"] == "error":
            raise AgentError(event["message"])
    raise AgentError("the agent stopped without an answer")
