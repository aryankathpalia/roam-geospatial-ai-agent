"""
Throwaway trial (not part of ROAM, never committed): can a region-profile step make ROAM work
outside the US?  Tests, per document:
  1. region detection   Gemini names the country + the printed evidence; a rule check confirms it
  2. approximate place  the printed place details are geocoded worldwide (OpenStreetMap Nominatim)
  3. bearings           boundary calls read as printed; both quadrant (N 45°30' E) and
                        whole-circle (123°45'20") styles parsed; the walk is checked for closure
Uses ROAM's existing results (which sheet is the parcel map) and its Gemini + geocoding helpers.
Runs on the local CPU; no Modal.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sys
from pathlib import Path

from google.genai import types
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.services.geocoding import geocode_place  # noqa: E402
from app.services.vision import _generate_with_fallback, _get_client  # noqa: E402

OUT = Path(__file__).parent

DOCS = {
    "Canada, Caledon draft plan": ("c21abb6b-ee0c-404e-a257-2a8adcf5e9f5", "CA"),
    "Canada, Ottawa draft plan": ("89a3c8d8-2faa-4155-829a-638f3ef71d29", "CA"),
    "New Zealand, DP 514130 (Auckland)": ("785f7100-e54f-4e28-aaeb-4cf72a0f6621", "NZ"),
    "New Zealand, DP 564472 (Canterbury)": ("abfa06e9-f91b-47ab-8667-a5ee8fb7059a", "NZ"),
    "UK, Sheffield title plan": ("117ef5e3-c725-4a87-a0de-c558c296f791", "GB"),
    "US, Washoe four lots (control)": ("d29364ff-8a19-4328-a23f-895eab8ff6b4", "US"),
    "US, Washoe four parcels (control)": ("b4c6b8bf-e44a-459a-a8bd-0f631164fcb7", "US"),
}

PROMPT = """This is a land survey plan, plat or title plan. Return JSON:
{"country": ISO-3166 alpha-2 code,
 "country_evidence": [short quotes printed on the sheet that show the country],
 "datum_or_grid": str|null (exactly as printed, e.g. "NAD83", "NZGD2000", "OSGB36"),
 "parcel_reference": str|null (e.g. "Lot 2 DP 60057", "Part 1 of Lot 18, Concession 9", "APN 017-150-35", a title number),
 "place": {"road": str|null, "locality": str|null (suburb/town/township), "city": str|null,
           "region": str|null (state/province/county/district), "postcode": str|null},
 "lot": {"label": str|null,
         "calls": [{"bearing": str exactly as printed, "distance_m": number}]  (the boundary of ONE lot whose
                   full set of bearings and distances is printed, in order around the lot; [] if none),
         "stated_area_m2": number|null}}"""

# Printed signals that confirm (or contradict) the model's country.
SIGNALS = {
    "US": [r"\bAPN\b", r"\bNAD ?83\b", r"\bstate plane\b", r"\bcounty recorder\b", r"\bT\d+[NS]\b.*\bR\d+[EW]\b", r"\bPLSS\b"],
    "CA": [r"\bconcession\b", r"\bontario\b", r"\bregistered plan\b", r"\bMTM\b", r"\bCSRS\b", r"\b[A-Z]\d[A-Z] ?\d[A-Z]\d\b"],
    "NZ": [r"\bDP ?\d{4,6}\b", r"\bNZGD", r"\bNZTM\b", r"\bLINZ\b", r"land information new zealand", r"\bnew zealand\b"],
    "GB": [r"HM Land Registry", r"\btitle number\b", r"\bOSGB", r"\bordnance survey\b", r"\b[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}\b"],
    "AU": [r"\bGDA ?(94|2020)\b", r"\bMGA\b", r"\bSP ?\d{5,6}\b", r"\bLRS\b"],
}


def main_sheet(doc_id: str) -> tuple[Image.Image, int]:
    d = json.loads((ROOT / "data/documents" / doc_id / "result.json").read_text(encoding="utf-8"))
    pages = d["pages"]
    target = [p for p in pages if (p.get("sheet") or {}).get("role") == "target_parcel_map"]
    page = (target or [p for p in pages if any(r["class"] == "ParcelMap" for r in p["regions"])] or pages)[0]
    img = Image.open(ROOT / "data/documents" / doc_id / "pages" / f"page_{page['page_number']:03d}.png").convert("RGB")
    img.thumbnail((3000, 3000))
    return img, page["page_number"]


def read_sheet(img: Image.Image) -> dict:
    resp = _generate_with_fallback(_get_client(), contents=[img, PROMPT],
                                   config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0))
    return json.loads(resp.text)


def rule_check(read: dict) -> dict:
    text = " ".join([*(read.get("country_evidence") or []), read.get("datum_or_grid") or "",
                     read.get("parcel_reference") or "", *[v or "" for v in (read.get("place") or {}).values()]])
    hits = {c: [p for p in pats if re.search(p, text, re.I)] for c, pats in SIGNALS.items()}
    score = {c: len(v) for c, v in hits.items() if v}
    rule = max(score, key=score.get) if score else None
    return {"rule_country": rule, "signals": {c: len(v) for c, v in hits.items() if v}}


# ------------------------------------------------------------------ bearings

def bearing_to_azimuth(b: str) -> float | None:
    """Quadrant 'N 45°30'15" E' or whole-circle '123°45'20"' -> azimuth in degrees."""

    s = b.replace("º", "°").replace("’", "'").replace("”", '"').strip()
    m = re.match(r"^\s*([NS])\s*(\d+(?:\.\d+)?)\s*°?\s*(?:(\d+(?:\.\d+)?)\s*')?\s*(?:(\d+(?:\.\d+)?)\s*\")?\s*([EW])\s*$", s, re.I)
    if m:
        a = float(m[2]) + float(m[3] or 0) / 60 + float(m[4] or 0) / 3600
        ns, ew = m[1].upper(), m[5].upper()
        return {("N", "E"): a, ("S", "E"): 180 - a, ("S", "W"): 180 + a, ("N", "W"): 360 - a}[(ns, ew)]
    m = re.match(r"^\s*(\d{1,3}(?:\.\d+)?)\s*°\s*(?:(\d+(?:\.\d+)?)\s*')?\s*(?:(\d+(?:\.\d+)?)\s*\")?\s*$", s)
    if m:
        return (float(m[1]) + float(m[2] or 0) / 60 + float(m[3] or 0) / 3600) % 360
    m = re.match(r"^\s*(\d{1,3})[\s°](\d{2})['\s](\d{2})\"?\s*$", s)  # "123 45 20"
    if m:
        return (int(m[1]) + int(m[2]) / 60 + int(m[3]) / 3600) % 360
    return None


def walk(calls: list[dict]) -> dict | None:
    pts, x, y, styles, bad = [(0.0, 0.0)], 0.0, 0.0, set(), 0
    for c in calls:
        az = bearing_to_azimuth(str(c.get("bearing", "")))
        d = c.get("distance_m")
        if az is None or not isinstance(d, (int, float)):
            bad += 1
            continue
        styles.add("quadrant" if re.match(r"^\s*[NS]", str(c["bearing"]), re.I) else "whole-circle")
        x += d * math.sin(math.radians(az)); y += d * math.cos(math.radians(az))
        pts.append((x, y))
    if len(pts) < 4:
        return None
    perim = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
    misclose = math.dist(pts[0], pts[-1])
    area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))) / 2
    return {"calls": len(calls), "unparsed": bad, "styles": sorted(styles), "perimeter_m": round(perim, 2),
            "misclosure_m": round(misclose, 3), "precision": f"1:{int(perim / misclose)}" if misclose > 1e-6 else "exact",
            "area_m2": round(area, 1)}


# ------------------------------------------------------------------ geocoding

# Hillsborough Park, Sheffield (the park this title plan covers), from OpenStreetMap
REFERENCE = {"UK, Sheffield title plan": (53.4044, -1.4999)}

COUNTRY_NAMES = {"US": "United States", "CA": "Canada", "NZ": "New Zealand", "GB": "United Kingdom", "AU": "Australia", "IN": "India"}


async def approximate_place(read: dict, country: str) -> dict:
    """Most specific query first; OpenStreetMap's usage policy asks for at most 1 request a second."""

    p = read.get("place") or {}
    town = p.get("locality") or p.get("city")
    tries = [("road", [p.get("road"), town]), ("road", [p.get("road"), p.get("city")]),
             ("road", [p.get("road"), p.get("region")]), ("town", [p.get("locality"), p.get("region")]),
             ("town", [p.get("city")]), ("region", [p.get("region")])]
    seen = set()
    for level, parts in tries:
        parts = [x for x in parts if x]
        if not parts or (level == "road" and not p.get("road")):
            continue
        q = ", ".join(dict.fromkeys(parts + [COUNTRY_NAMES.get(country, "")]))
        if q in seen:
            continue
        seen.add(q)
        for attempt in range(3):
            await asyncio.sleep(1.2)
            try:
                hits = await geocode_place(q, limit=1)
                break
            except Exception:  # noqa: BLE001
                hits = None
        if hits:
            h = hits[0]
            return {"query": q, "level": level, "lat": h.latitude if hasattr(h, "latitude") else h.lat,
                    "lon": h.longitude if hasattr(h, "longitude") else h.lon, "name": h.name}
    return {"query": None, "level": None}


def true_point(doc_id: str) -> tuple[float, float] | None:
    """Reference location: a US control's verified placement."""

    d = json.loads((ROOT / "data/documents" / doc_id / "result.json").read_text(encoding="utf-8"))
    pts = [pt for pg in d["pages"] for r in pg["regions"] for pa in r.get("parcels") or []
           if pa.get("human_confirmed") and pa.get("boundary_geojson_wgs84")
           for pt in pa["boundary_geojson_wgs84"]["geometry"]["coordinates"][0]]
    if not pts:
        return None
    return sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts)


def km(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return round(6371 * 2 * math.asin(math.sqrt(h)), 2)


async def main() -> None:
    results = []
    for name, (doc_id, truth) in DOCS.items():
        img, page = main_sheet(doc_id)
        read = read_sheet(img)
        rc = rule_check(read)
        country = read.get("country") or rc["rule_country"]
        place = await approximate_place(read, country)
        ref = true_point(doc_id) if truth == "US" else REFERENCE.get(name)
        if ref and place.get("lat") is not None:
            place["km_from_true_location"] = km(ref, (float(place["lat"]), float(place["lon"])))
        results.append({"document": name, "page": page, "true_country": truth, "model_country": read.get("country"),
                        **rc, "evidence": read.get("country_evidence"), "datum": read.get("datum_or_grid"),
                        "parcel_reference": read.get("parcel_reference"), "place": read.get("place"),
                        "geocoded": place, "lot": (read.get("lot") or {}).get("label"),
                        "stated_area_m2": (read.get("lot") or {}).get("stated_area_m2"),
                        "walk": walk((read.get("lot") or {}).get("calls") or [])})
        print(json.dumps(results[-1], ensure_ascii=False))
    (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
