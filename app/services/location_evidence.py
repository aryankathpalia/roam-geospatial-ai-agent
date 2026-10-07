"""
Location evidence read by the vision model, VERIFIED against the document's own OCR text before use.

The sheet roster call (vision.read_parcel_roster) also returns, per sheet, what the drawing says about WHERE
it is: APNs and whose they are (subject / neighbour), printed coordinate pairs and what each marks (a named
monument or a corner of the parcel), the coordinate system (zone, units, grid vs ground, combined factor),
the PLSS description and the street address. A model reads wording that regexes keep missing ("SCALE FACT0R
OF", "G&SRM", "Base and Meridian", "S.F."), and it can tell a neighbour's APN from the subject's, which text
position cannot.

It can also misread or invent a number. So nothing here is trusted on the model's word:
- an APN or a coordinate is kept only if its digits are printed in the OCR text;
- a combined factor only if that number is printed;
- PLSS / address / county / state only if their key tokens are printed -- otherwise they are kept as
  "vision_only" and used solely where nothing better exists (e.g. to name the state for a lookup).
The deterministic readers (georeference / plss / apn regexes) still run; this only ADDS candidates and
role labels, each of which is checked again downstream against independent data (BLM, county parcels,
the geocoder).
"""

from __future__ import annotations

import re
from typing import Any

_SIDES = {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}
_ROLES = {"subject", "neighbour", "referenced"}
_KINDS = {"monument", "parcel_corner", "point_of_beginning", "other"}


def _s(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    v = " ".join(v.split())
    return v if v and v.upper() not in {"NULL", "NONE", "N/A", "UNKNOWN"} else None


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def sanitize_location(raw: Any) -> dict:
    """Model output -> a clean dict with the fixed keys (bad entries dropped, nothing invented)."""

    raw = raw if isinstance(raw, dict) else {}
    apns, seen = [], set()
    for a in raw.get("apns") or []:
        if not isinstance(a, dict):
            continue
        groups = re.findall(r"\d+", str(a.get("apn") or ""))
        if not groups or sum(len(g) for g in groups) < 6:
            continue
        apn = "-".join(groups)
        if apn in seen:
            continue
        seen.add(apn)
        side = str(a.get("side") or "").upper().strip()
        apns.append({
            "apn": apn, "role": a.get("role") if a.get("role") in _ROLES else "referenced",
            "side": side if side in _SIDES else None,
        })
    coords = []
    for c in raw.get("coordinates") or []:
        if not isinstance(c, dict):
            continue
        n, e = _num(c.get("northing")), _num(c.get("easting"))
        if n is None or e is None or n < 10_000 or e < 10_000:
            continue
        coords.append({
            "northing": n, "easting": e, "kind": c.get("kind") if c.get("kind") in _KINDS else "other",
            "label": _s(c.get("label")),
        })
    cs = raw.get("coordinate_system") if isinstance(raw.get("coordinate_system"), dict) else {}
    factor = _num(cs.get("combined_factor"))
    system = {
        "zone": _s(cs.get("zone")), "datum": _s(cs.get("datum")),
        "units": cs.get("units") if cs.get("units") in ("us_survey_feet", "international_feet", "meters") else None,
        "coordinates_are": cs.get("coordinates_are") if cs.get("coordinates_are") in ("grid", "ground") else None,
        "combined_factor": factor if factor and 0.99 < factor < 1.01 and factor != 1.0 else None,
    }
    pl = raw.get("plss") if isinstance(raw.get("plss"), dict) else {}
    t, r = pl.get("township"), pl.get("range")
    td, rd = str(pl.get("township_dir") or "").upper()[:1], str(pl.get("range_dir") or "").upper()[:1]
    plss = None
    if isinstance(t, int) and isinstance(r, int) and td in ("N", "S") and rd in ("E", "W"):
        plss = {
            "township": t, "township_dir": td, "range": r, "range_dir": rd, "meridian": _s(pl.get("meridian")),
            "sections": [x for x in pl.get("sections") or [] if isinstance(x, int) and 1 <= x <= 36],
            "aliquot": _s(pl.get("aliquot")),
        }
    return {
        "apns": apns, "coordinates": coords, "coordinate_system": system, "plss": plss,
        "address": _s(raw.get("address")), "city": _s(raw.get("city")),
        "county": _s(raw.get("county")), "state": _s(raw.get("state")),
    }


def _normalise_ocr(text: str) -> str:
    # OCR reads 0 as O and 1 as l/I inside numbers; full-width punctuation as ordinary
    return text.translate(str.maketrans({"：": ":", "，": ","}))


def _printed_number(value: float, text: str) -> bool:
    """`value`'s digits are printed in the text (thousands separators and O/0 confusion tolerated)."""

    whole = str(int(abs(value)))
    digits = re.sub(r"(?<=\d)[,\s](?=\d{3}\b)", "", text)
    digits = re.sub(r"(?<=\d)[Oo](?=\d)|(?<=\d)[Oo]\b|\b[Oo](?=\d)", "0", digits)
    return re.search(rf"(?<!\d){whole}(?!\d)", digits) is not None or re.search(
        rf"(?<!\d){whole}\.\d", digits
    ) is not None


def _printed_factor(factor: float, text: str) -> bool:
    """A factor like 1.000197939 is printed (OCR may read its zeros as O)."""

    digits = f"{factor:.9f}".rstrip("0")
    fixed, previous = text, None
    while fixed != previous:  # repeated: "1.OOO19" has O's whose neighbours are only O's until the first pass
        previous, fixed = fixed, re.sub(r"(?<=[\d.])[Oo]|[Oo](?=[\d.])", "0", fixed)
    if re.search(rf"(?<!\d){re.escape(digits)}", fixed):
        return True
    # OCR garbles the factor's tail ("1.0001973WASSTOCOVT" for 1.000197938 -- a digit dropped): a printed number
    # agreeing with it through the first three significant decimals is the same factor (to ~1e-6; the value
    # used is the full one the evidence pass read).
    def lead(s: str) -> str | None:
        whole, _, frac = s.partition(".")
        zeros = len(frac) - len(frac.lstrip("0"))
        return f"{whole}.{frac[:zeros + 3]}" if len(frac) - zeros >= 3 else None

    want = lead(digits)
    return want is not None and any(lead(m.group(1)) == want for m in re.finditer(r"(?<!\d)(\d\.\d+)", fixed))


def _printed_apn(apn: str, text: str) -> bool:
    groups = apn.split("-")
    return re.search(r"(?<!\d)" + r"[\s\-.]?".join(groups) + r"(?!\d)", text) is not None


def _printed_words(phrase: str | None, text: str) -> bool:
    if not phrase:
        return False
    words = [w for w in re.findall(r"[A-Za-z0-9]+", phrase) if len(w) > 1 and w.upper() not in {"COUNTY", "THE", "OF"}]
    return bool(words) and all(re.search(rf"(?i)\b{re.escape(w)}\b", text) for w in words[:3])


def verify(evidence: dict, ocr_text: str) -> dict:
    """
    The evidence with every number checked against the OCR text: unprinted APNs / coordinates / factors
    are dropped (listed under "dropped"); PLSS and place names that the text does not show are kept but
    marked "vision_only" (the OCR misses rotated title blocks that vision reads fine).
    """

    text = _normalise_ocr(ocr_text or "")
    dropped: list[str] = []
    apns = []
    for a in evidence.get("apns", []):
        if _printed_apn(a["apn"], text):
            apns.append(a)
        else:
            dropped.append(f"APN {a['apn']}")
    coords = []
    for c in evidence.get("coordinates", []):
        if _printed_number(c["northing"], text) and _printed_number(c["easting"], text):
            coords.append(c)
        else:
            dropped.append(f"coordinate N {c['northing']} E {c['easting']}")
    system = dict(evidence.get("coordinate_system") or {})
    f = system.get("combined_factor")
    if f is not None and not _printed_factor(f, text):
        dropped.append(f"combined factor {f}")
        system["combined_factor"] = None
    plss = evidence.get("plss")
    if plss:
        plss = {**plss, "vision_only": not (
            _printed_number(plss["township"], text) and _printed_number(plss["range"], text)
            and re.search(r"(?i)\b(T(?:OWNSHIP)?|R(?:ANGE)?)\b|\bT\.?\s*\d|\bR\.?\s*\d", text)
        )}
    place = {}
    for key in ("address", "city", "county", "state"):
        value = evidence.get(key)
        if value:
            place[key] = value
            place[f"{key}_vision_only"] = not _printed_words(value, text)
    return {
        "apns": apns, "coordinates": coords, "coordinate_system": system, "plss": plss,
        **{k: place.get(k) for k in ("address", "city", "county", "state")},
        "vision_only": [k for k in ("address", "city", "county", "state") if place.get(f"{k}_vision_only")],
        "dropped": dropped,
    }


def merge(per_sheet: list[dict | None]) -> dict | None:
    """One document-level view: APNs and coordinates from every sheet (a subject role wins over others),
    the first sheet's value for the rest."""

    sheets = [e for e in per_sheet if e]
    if not sheets:
        return None
    apns: dict[str, dict] = {}
    for e in sheets:
        for a in e.get("apns", []):
            if a["apn"] not in apns or a["role"] == "subject":
                apns[a["apn"]] = a
    coords, seen = [], set()
    for e in sheets:
        for c in e.get("coordinates", []):
            key = (round(c["northing"], 2), round(c["easting"], 2))
            if key not in seen:
                seen.add(key)
                coords.append(c)

    def first(key):
        return next((e.get(key) for e in sheets if e.get(key)), None)

    system: dict = {}
    for e in sheets:
        for k, v in (e.get("coordinate_system") or {}).items():
            if v is not None and system.get(k) is None:
                system[k] = v
    return {
        "apns": list(apns.values()), "coordinates": coords, "coordinate_system": system,
        "plss": first("plss"), "address": first("address"), "city": first("city"),
        "county": first("county"), "state": first("state"),
        "vision_only": sorted({k for e in sheets for k in e.get("vision_only", [])}),
        "dropped": [d for e in sheets for d in e.get("dropped", [])],
    }


def subject_apns(evidence: dict | None) -> list[str]:
    return [a["apn"] for a in (evidence or {}).get("apns", []) if a["role"] == "subject"]


def parcel_coordinate_pairs(evidence: dict | None) -> list[tuple[float, float]]:
    """Printed pairs that mark the SUBJECT parcel (a corner or the point of beginning) -- not monuments."""

    return [
        (c["northing"], c["easting"]) for c in (evidence or {}).get("coordinates", [])
        if c["kind"] in ("parcel_corner", "point_of_beginning")
    ]


def ground_to_grid(evidence: dict | None) -> float | None:
    """grid = ground x this, when the sheet says its coordinates are GROUND and prints the factor."""

    cs = (evidence or {}).get("coordinate_system") or {}
    if cs.get("coordinates_are") == "ground" and cs.get("combined_factor"):
        return 1.0 / cs["combined_factor"]
    return None


def geocode_queries(evidence: dict | None) -> list[str]:
    """Address-first queries from the evidence ("470 Foothill Road, Reno, Nevada"), then the county."""

    e = evidence or {}
    out = []
    county = e.get("county")
    if county and not re.search(r"(?i)\bcounty\b", county):
        county = f"{county} County"
    # the city, else the county: "470 Foothill Road, Nevada" alone matches any Foothill Road in the state
    tail = ", ".join(x for x in (e.get("city") or county, e.get("state")) if x)
    if e.get("address") and "address" not in e.get("vision_only", []):
        out.append(f"{e['address']}, {tail}" if tail else e["address"])
    if e.get("county") and e.get("state"):
        county = e["county"] if re.search(r"(?i)\bcounty\b", e["county"]) else f"{e['county']} County"
        out.append(f"{county}, {e['state']}")
    return out


def plss_text(evidence: dict | None) -> str | None:
    """The PLSS description in the canonical wording plss.py parses, or None."""

    p = (evidence or {}).get("plss")
    if not p or not p.get("meridian"):
        return None
    dirs = {"N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST"}
    sections = " ".join(f"SECTION {s}," for s in p.get("sections") or [])
    meridian = p["meridian"]
    if not re.search(r"(?i)meridian", meridian):
        # an abbreviation ("G&SRM", "M.D.M."): plss.py reads those in the short T/R form
        return f"{sections} T{p['township']}{p['township_dir']}, R{p['range']}{p['range_dir']}, {meridian}".strip()
    return (
        f"{(p.get('aliquot') + ' OF ') if p.get('aliquot') else ''}{sections} "
        f"TOWNSHIP {p['township']} {dirs[p['township_dir']]}, RANGE {p['range']} {dirs[p['range_dir']]}, {meridian}"
    ).strip()
