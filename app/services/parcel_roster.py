"""
Parcel roster: the parcels on a map SHEET as first-class, sheet-owned entities.

    sheet (page, role)  -- the map; the shared drawing canvas
      parcels[]         -- what the user actually confirms
        id, label, printed_id, stated_area, point, source, manual
        confirmed_polygon      -- this parcel's OWN outline (never shared)
        evidence_ref           -- {region, parcel}: the extracted survey evidence joined to it

The roster is read EARLY (one small Gemini call per target sheet, vision.read_parcel_roster)
so the user can pick and outline "Parcel 1" or "Remainder Parcel" while OCR and the tiled
extraction are still running. When extraction finishes it is an EVIDENCE JOIN: extracted
parcels (calls, stated area) are matched to roster parcels and attached to them; the user's
polygons are never replaced. Extracted parcels with no roster match become entities of their
own; roster parcels with no evidence keep their outline.

Identity rule: `printed_id` is text printed on THIS sheet next to the parcel, or None. Nothing
here derives an id from a title, another page or a referenced survey, and ids the sheet's OCR
text does not contain are dropped (verify_printed_ids).

Pure functions over plain dicts -- no I/O -- so the join is directly testable.
"""

from __future__ import annotations

import re
from typing import Any

# Words that do not distinguish one parcel's label from another's.
_GENERIC = {"PARCEL", "LOT", "THE", "OF", "NO", "NUMBER", "AC", "ACRES"}
_NULLISH = {"", "NULL", "NONE", "N/A", "NA", "-", "UNKNOWN", "NOT PRINTED"}
MAX_PARCELS_PER_SHEET = 12


def label_tokens(text: Any) -> list[str]:
    """Upper-case tokens; a hyphenated id like 17-2-1-4 is ONE token, so "PARCEL 1" can never
    match inside "REMAINDER PARCEL 17-2-1-4"."""
    return re.findall(r"[A-Z0-9]+(?:-[A-Z0-9]+)*", str(text or "").upper())


def parse_acres(text: Any) -> float | None:
    """'120.4 AC.±' / '[40.00 AC.±]' / '1.78' -> acres; square feet are converted."""
    if text is None:
        return None
    t = str(text).upper().replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)", t)
    if not m:
        return None
    value = float(m.group(1))
    if re.search(r"\b(SF|SQ\.?\s*FT|SQUARE\s+F)", t):
        value /= 43560.0
    return value


def clean_printed_id(value: Any) -> str | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    return None if text.upper() in _NULLISH else text


def clean_point(x: Any, y: Any) -> list[float] | None:
    try:
        fx, fy = float(x), float(y)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
        return None
    return [round(fx, 4), round(fy, 4)]


def sanitize_roster(items: Any) -> list[dict]:
    """Model output -> [{label, printed_id, stated_area, point}], one per distinct label."""
    out: list[dict] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        key = " ".join(label_tokens(label))
        if not key or key in seen:
            continue
        seen.add(key)
        area = item.get("stated_area")
        out.append(
            {
                "label": label.strip(),
                "printed_id": clean_printed_id(item.get("printed_id")),
                "stated_area": area.strip() if isinstance(area, str) and area.strip() else None,
                "point": clean_point(item.get("point_x"), item.get("point_y")),
            }
        )
        if len(out) >= MAX_PARCELS_PER_SHEET:
            break
    return out


def new_entity(
    page_number: int,
    existing: list[dict],
    *,
    label: str,
    region_index: int,
    source: str,
    printed_id: str | None = None,
    stated_area: str | None = None,
    point: list[float] | None = None,
    manual: bool = False,
) -> dict:
    prefix = {"roster": "", "extraction": "x", "manual": "m"}[source]
    taken = {e["id"] for e in existing}
    n = 1
    while f"p{page_number}-{prefix}{n}" in taken:
        n += 1
    return {
        "id": f"p{page_number}-{prefix}{n}",
        "label": label,
        "printed_id": printed_id,
        "stated_area": stated_area,
        "point": point,
        "region_index": region_index,
        "source": source,
        # True when the user named it: identity was NOT detected automatically.
        "manual": manual,
        "confirmed_polygon": None,
        "evidence_ref": None,
    }


def verify_printed_ids(entities: list[dict], sheet_text: str) -> None:
    """Drop a printed_id the sheet's own text does not contain (a model slip, or an id borrowed
    from a title or another page). No text -> nothing to check against, nothing dropped."""
    haystack = re.sub(r"[^A-Z0-9]", "", (sheet_text or "").upper())
    if not haystack:
        return
    norm = lambda t: re.sub(r"[^A-Z0-9]", "", str(t).upper())  # noqa: E731
    for e in entities:
        pid = e.get("printed_id")
        if pid and norm(pid) not in haystack:
            e["printed_id_dropped"] = pid
            e["printed_id"] = None
    # An id printed beside one parcel cannot also identify another: keep it on the parcel whose
    # own label carries it, else on the first, and drop it from the rest.
    owners: dict[str, list[dict]] = {}
    for e in entities:
        if e.get("printed_id"):
            owners.setdefault(norm(e["printed_id"]), []).append(e)
    for pid, group in owners.items():
        if len(group) > 1:
            keep = next((e for e in group if pid in norm(e.get("label"))), group[0])
            for e in group:
                if e is not keep:
                    e["printed_id_dropped"] = e["printed_id"]
                    e["printed_id"] = None


# ------------------------------------------------------------------ evidence join

def _score(entity: dict, cand: dict) -> int | None:
    """How well an extracted parcel {label, acres} matches a roster entity. None = no match."""
    e_tokens, c_tokens = label_tokens(entity.get("label")), label_tokens(cand.get("label"))
    e_key, c_key = " ".join(e_tokens), " ".join(c_tokens)
    e_core = {t for t in e_tokens if t not in _GENERIC}
    c_core = {t for t in c_tokens if t not in _GENERIC}
    label = 0
    if e_key and e_key == c_key:
        label = 4
    elif e_core and c_core and (e_core <= c_core or c_core <= e_core):
        label = 3
    else:
        pid = re.sub(r"[^A-Z0-9]", "", str(entity.get("printed_id") or "").upper())
        if pid and pid in re.sub(r"[^A-Z0-9]", "", str(cand.get("label") or "").upper()):
            label = 3
    a, b = parse_acres(entity.get("stated_area")), cand.get("acres")
    area = 0
    if a and b:
        ratio = abs(a - b) / max(a, b)
        area = 2 if ratio <= 0.03 else (-3 if ratio > 0.10 else 0)
    total = label + area
    if label == 0 and area <= 0:
        return None
    return total if total >= 2 else None


def match_evidence(entities: list[dict], candidates: list[dict]) -> dict[int, int]:
    """Greedy best-score assignment, each side used once: {entity index: candidate index}."""
    pairs = []
    for i, e in enumerate(entities):
        for j, c in enumerate(candidates):
            s = _score(e, c)
            if s is not None:
                pairs.append((s, i, j))
    pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
    out: dict[int, int] = {}
    used: set[int] = set()
    for _, i, j in pairs:
        if i in out or j in used:
            continue
        out[i] = j
        used.add(j)
    return out


def join_evidence(page_number: int, sheet: dict, regions: list[dict]) -> None:
    """
    Attach the pipeline's extracted parcels to the sheet's roster entities, in place:
    entity["evidence_ref"] = {region, parcel}; parcel["roster_id"] = entity id. Extracted
    parcels nothing matched become `extraction` entities (parcels read off an inset region, or
    flagged as a duplicate, carry an excluded_reason); unmatched roster entities stay as they are.
    """

    entities: list[dict] = sheet.setdefault("parcels", [])
    cands: list[dict] = []
    for ri in list(sheet.get("regions", [])) + list(sheet.get("inset_regions", [])):
        for pi, parcel in enumerate(regions[ri].get("parcels") or []):
            if parcel.get("roster_id") or parcel.get("deleted"):
                continue  # already linked (joining twice is safe), or removed by the user: never resurrect it
            vg = parcel.get("vision_geometry") or {}
            cands.append(
                {
                    "region": ri,
                    "parcel": pi,
                    "label": vg.get("parcel_label") or "",
                    "acres": parse_acres(vg.get("stated_area_acres")),
                    "inset": ri in sheet.get("inset_regions", []),
                    "ref": parcel,
                }
            )
    # Roster/manual entities that have no evidence yet are the ones to match.
    free = [e for e in entities if not e.get("evidence_ref")]
    matched = match_evidence(free, cands)
    taken: set[int] = set()
    for ei, cj in matched.items():
        e, c = free[ei], cands[cj]
        e["evidence_ref"] = {"region": c["region"], "parcel": c["parcel"]}
        e["region_index"] = c["region"]
        c["ref"]["roster_id"] = e["id"]
        taken.add(cj)
    for j, c in enumerate(cands):
        if j in taken:
            continue
        label = c["label"] or f"Parcel {len(entities) + 1}"
        e = new_entity(page_number, entities, label=label, region_index=c["region"], source="extraction")
        e["stated_area"] = (c["ref"].get("vision_geometry") or {}).get("stated_area_acres")
        e["evidence_ref"] = {"region": c["region"], "parcel": c["parcel"]}
        n_calls = len((c["ref"].get("vision_geometry") or {}).get("boundary_calls") or [])
        if c["inset"]:
            e["excluded_reason"] = "Read from a small inset of this sheet (likely a vicinity or detail map), not its main drawing."
        elif c["ref"].get("likely_duplicate_region"):
            e["excluded_reason"] = c["ref"].get("duplicate_note") or "Likely a duplicate of a parcel drawn elsewhere."
        elif n_calls < 3:
            # Fewer than 3 calls can't even close a polygon (same bar
            # assemble_traverse itself requires, geometry.py) -- a label
            # that surfaced with only a stray edge or two is far more
            # likely a scattered fragment or an adjoiner/reference
            # citation on a dense multi-parcel sheet than a real,
            # independently confirmable parcel. Confirmed on a real
            # document (Patnaude packet, page 7): three such fragments
            # (1, 1, and 0 calls) appeared as full, unflagged, equally-
            # weighted confirmable entities alongside the two real
            # target parcels the roster had already correctly found --
            # one of them ("17-2-1-4", 145.40 ac, 1 call) was actually a
            # garbled duplicate of the real REMAINDER PARCEL (120.4 ac,
            # matched separately via the roster). Flagged, not dropped:
            # the label and stated area are still real signal a reviewer
            # may want to check against the source.
            e["excluded_reason"] = (
                f"Only {n_calls} boundary call(s) were extracted for this label -- not enough to form a "
                "shape. Likely a scattered fragment or an adjoiner/reference citation on this sheet, not "
                "an independently confirmable parcel; check it against the source if it looks real."
            )
        c["ref"]["roster_id"] = e["id"]
        entities.append(e)


def carry_over_user_data(stored: dict | None, new: dict) -> None:
    """
    The pipeline owns pages/regions/parcels and the roster it read; the USER owns each
    entity's confirmed_polygon and any parcel they named by hand. Copy those from the stored
    result onto a newly built one (a pipeline checkpoint or its final result), by entity id,
    so a save from the background pipeline can never discard what the user did meanwhile.
    """

    if not stored:
        return
    new_pages = {p["page_number"]: p for p in new.get("pages", [])}
    for st_page in stored.get("pages", []):
        st_sheet = st_page.get("sheet")
        nw_page = new_pages.get(st_page["page_number"])
        if not st_sheet or not nw_page or not nw_page.get("sheet"):
            continue
        nw_sheet = nw_page["sheet"]
        entities = nw_sheet.setdefault("parcels", [])
        by_id = {e["id"]: e for e in entities}
        for e in st_sheet.get("parcels") or []:
            if e["id"] in by_id:
                if e.get("confirmed_polygon"):
                    by_id[e["id"]]["confirmed_polygon"] = e["confirmed_polygon"]
            elif e.get("source") == "manual":
                entities.append(e)
