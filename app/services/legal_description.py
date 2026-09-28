"""
Deterministic parser for written metes-and-bounds legal descriptions
("...thence South 00 Degrees 37 Minutes 56 Seconds West a distance of
669.60 feet..."). Deeds and survey plats very often print the full
boundary as prose, and OCR reads that prose far more reliably than a
vision model reads a dimensioned drawing -- the same document can
produce different vision readings run to run, while this text parses
identically every time. So a description that parses and closes is
treated as the authoritative boundary for its parcel.
"""

import re

from app.services.geometry import parse_bearing, parse_distance, walk_traverse
from app.services.spatial_validation import compute_perimeter_ft

_WORD_BEARING = (
    r"(North|South)\s+(\d{1,3})\s*Degrees?\s*(\d{1,2})\s*Minutes?"
    r"(?:\s*(\d{1,2}(?:\.\d+)?)\s*Seconds?)?\s+(East|West)"
)
_SYMBOL_BEARING = (
    r"\b([NS])\s*(\d{1,3})\s*[°º*]\s*(\d{1,2})\s*['’′]\s*"
    r"(?:(\d{1,2}(?:\.\d+)?)\s*[\"”″]\s*)?([EW])\b"
)
_BEARING_RE = re.compile(f"{_WORD_BEARING}|{_SYMBOL_BEARING}", re.IGNORECASE)
_DISTANCE_RE = re.compile(
    r"(?:distance\s+of\s+)?(\d[\d,]*\.\d+|\d[\d,]*)\s*(?:feet|foot|ft\.?)", re.IGNORECASE
)
_POB_RE = re.compile(r"POINT\s+OF\s+BEGINNING", re.IGNORECASE)
_CONTAINING_RE = re.compile(
    r"CONTAINING\s+(\d[\d,]*\.?\d*)\s*(acres?|ac\.?)", re.IGNORECASE
)
_CURVE_RE = re.compile(r"\bcurve\b|\barc\b|\bradius\b", re.IGNORECASE)

_MIN_PRECISION = 2_000
_MIN_CALLS = 3


def _bearing_text(match: re.Match) -> str:
    g = match.groups()
    if g[0]:
        ns, deg, mins, secs, ew = g[0][0], g[1], g[2], g[3] or "0", g[4][0]
    else:
        ns, deg, mins, secs, ew = g[5], g[6], g[7], g[8] or "0", g[9]
    return f"{ns.upper()} {int(deg):02d}°{int(mins):02d}'{secs}\" {ew.upper()}"


def _calls_between(text: str, start: int, end: int) -> list[dict]:
    segment = text[start:end]
    bearings = list(_BEARING_RE.finditer(segment))
    calls = []
    for i, bearing in enumerate(bearings):
        stop = bearings[i + 1].start() if i + 1 < len(bearings) else len(segment)
        distance = _DISTANCE_RE.search(segment, bearing.end(), stop)
        if not distance:
            return []
        calls.append(
            {"bearing": _bearing_text(bearing), "distance": f"{distance.group(1).replace(',', '')}'"}
        )
    return calls


def _precision(calls: list[dict]) -> float:
    traverse = walk_traverse(calls)
    if traverse.unparsed_calls:
        return 0.0
    if traverse.closure_error_ft <= 0:
        return float("inf")
    return compute_perimeter_ft(traverse.points) / traverse.closure_error_ft


def parse_legal_descriptions(text: str) -> list[dict]:
    """
    Returns every closing boundary found in `text`, as
    {"boundary_calls", "stated_area_acres"}. Calls before the first
    "POINT OF BEGINNING" are a tie from a reference corner (the
    "COMMENCING at..." leg) and are not part of the boundary; the
    boundary runs from there to the next "POINT OF BEGINNING" (the
    return) or a "CONTAINING" clause. Descriptions with curves are
    skipped rather than half-parsed, and anything that doesn't close
    to survey precision is dropped -- this only ever returns a
    boundary it can verify.
    """

    results = []
    pobs = list(_POB_RE.finditer(text))
    for i, pob in enumerate(pobs):
        start = pob.end()
        end_candidates = [m.start() for m in pobs[i + 1 :]]
        containing = _CONTAINING_RE.search(text, start)
        if containing:
            end_candidates.append(containing.start())
        end = min(end_candidates) if end_candidates else len(text)
        if _CURVE_RE.search(text, start, end):
            continue
        calls = _calls_between(text, start, end)
        if len(calls) < _MIN_CALLS:
            continue
        if any(parse_bearing(c["bearing"]) is None or parse_distance(c["distance"]) is None for c in calls):
            continue
        if _precision(calls) < _MIN_PRECISION:
            continue
        # The acreage clause belongs to this boundary only if it follows
        # the closing "...to the POINT OF BEGINNING" almost immediately.
        acres = None
        if containing and containing.start() - end < 80:
            acres = containing.group(1).replace(",", "")
        results.append({"boundary_calls": calls, "stated_area_acres": acres})
    return _dedupe(results)


def _dedupe(results: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for r in results:
        key = tuple((c["bearing"], c["distance"]) for c in r["boundary_calls"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique
