"""
Throwaway trial v2 (never committed): the full region-profile flow, end to end, with timings.

  region   Gemini names the country from the sheet; a rule check on printed signals confirms it.
           Gate: if the region is US, NOTHING new runs and ROAM's existing result stands untouched.
  anchor   per region, strongest key first:
             NZ  parcel appellations -> LINZ NZ Primary Parcels (official outline, area, title)
             GB  Ordnance Survey map reference (e.g. SK3390SW) -> British National Grid -> WGS 84
             any street + town geocoded worldwide (OpenStreetMap), then district/region as last resort
  verify   NZ: printed lot areas and record-title numbers against LINZ
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values
from google.genai import types
from PIL import Image
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).parent))
from trial import ROOT, SIGNALS, km, main_sheet, true_point  # noqa: E402
from app.services.geocoding import geocode_place  # noqa: E402
from app.services.vision import _generate_with_fallback, _get_client  # noqa: E402

OUT = Path(__file__).parent
LINZ = f"https://data.linz.govt.nz/services;key={dotenv_values(ROOT / '.env').get('LINZ_KEY', '').strip()}/wfs"
BNG = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)

INTERNATIONAL = {
    "Canada, Caledon draft plan": "c21abb6b-ee0c-404e-a257-2a8adcf5e9f5",
    "Canada, Ottawa draft plan": "89a3c8d8-2faa-4155-829a-638f3ef71d29",
    "New Zealand, DP 514130 (Auckland)": "785f7100-e54f-4e28-aaeb-4cf72a0f6621",
    "New Zealand, DP 564472 (Canterbury)": "abfa06e9-f91b-47ab-8667-a5ee8fb7059a",
    "UK, Sheffield title plan": "117ef5e3-c725-4a87-a0de-c558c296f791",
}
TRUTH = {"Canada, Caledon draft plan": "CA", "Canada, Ottawa draft plan": "CA", "New Zealand, DP 514130 (Auckland)": "NZ",
         "New Zealand, DP 564472 (Canterbury)": "NZ", "UK, Sheffield title plan": "GB"}
UK_REFERENCE = (53.4044, -1.4999)  # Hillsborough Park, Sheffield

PROMPT = """This is a land survey plan, plat or title plan. Return JSON:
{"country": ISO-3166 alpha-2, "country_evidence": [short quotes printed on the sheet],
 "datum_or_grid": str|null, "grid_reference": str|null (an Ordnance Survey map reference like 'SK3390SW', if printed),
 "plan_number": str|null (the survey plan's own number, e.g. 'DP 514130'),
 "appellations": [str] (every parcel appellation printed, e.g. 'Lot 2 DP 324915', 'Lot 1'),
 "lot_areas": [{"lot": str, "area_m2": number}] (areas printed for the new lots, converted to m²),
 "record_titles": [str] (record of title / certificate of title numbers printed, digits only),
 "place": {"road": str|null, "locality": str|null, "city": str|null, "district": str|null, "region": str|null, "postcode": str|null}}"""


def rule_country(read: dict) -> str | None:
    text = " ".join([*(read.get("country_evidence") or []), read.get("datum_or_grid") or "", read.get("grid_reference") or "",
                     read.get("plan_number") or "", *(read.get("appellations") or []),
                     *[v or "" for v in (read.get("place") or {}).values()]])
    score = {c: sum(bool(re.search(p, text, re.I)) for p in pats) for c, pats in SIGNALS.items()}
    score = {c: n for c, n in score.items() if n}
    return max(score, key=score.get) if score else None


def detect(img: Image.Image) -> tuple[dict, float]:
    t = time.time()
    r = _generate_with_fallback(_get_client(), contents=[img, PROMPT],
                                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0))
    return json.loads(r.text), time.time() - t


# ------------------------------------------------------------------ anchors

def linz(appellation: str) -> dict | None:
    r = httpx.get(LINZ, params={"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": "layer-50772",
                                "cql_filter": f"appellation='{appellation}'", "outputFormat": "json", "srsName": "EPSG:4326"}, timeout=60)
    feats = r.json().get("features", []) if r.status_code == 200 else []
    if not feats:
        return None
    f = feats[0]
    ring = f["geometry"]["coordinates"][0][0] if f["geometry"]["type"] == "MultiPolygon" else f["geometry"]["coordinates"][0]
    lat = sum(p[1] for p in ring) / len(ring); lon = sum(p[0] for p in ring) / len(ring)
    p = f["properties"]
    return {"appellation": p["appellation"], "area_m2": p.get("calc_area"), "titles": p.get("titles"), "lat": lat, "lon": lon}


def nz_anchor(read: dict) -> dict:
    """The new lots ('Lot 1' on DP 514130 -> 'Lot 1 DP 514130') first, then the parents printed in full."""

    plan = (read.get("plan_number") or "").upper().replace("  ", " ").strip()
    names = []
    for a in read.get("appellations") or []:
        a = re.sub(r"\s+", " ", a).strip()
        if re.fullmatch(r"(?i)lot \d+", a) and plan:
            names.append(f"{a.title()} {plan}")
        elif re.search(r"(?i)\bDP \d+", a):
            names.append(a)
    for la in read.get("lot_areas") or []:
        if plan and re.fullmatch(r"(?i)lot \d+", str(la.get("lot", "")).strip()):
            names.insert(0, f"{la['lot'].strip().title()} {plan}")
    found, checked = [], []
    for n in dict.fromkeys(names):
        if len(checked) >= 6:
            break
        hit = linz(n)
        checked.append(n)
        if hit:
            found.append(hit)
    if not found:
        return {"method": None, "checked": checked}
    areas = {}
    for la in read.get("lot_areas") or []:
        num = re.search(r"\d+", str(la.get("lot")))
        if num:
            areas[f"LOT {num.group()} {plan}".upper()] = la.get("area_m2")
    area_checks = [{"lot": f["appellation"], "printed_m2": areas.get(f["appellation"].upper()), "linz_m2": f["area_m2"]}
                   for f in found if areas.get(f["appellation"].upper())]
    titles = set(read.get("record_titles") or [])
    title_match = sorted({t for f in found for t in str(f.get("titles") or "").split(",") if t.strip() in titles})
    lat = sum(f["lat"] for f in found) / len(found); lon = sum(f["lon"] for f in found) / len(found)
    return {"method": "LINZ parcel appellation", "lat": lat, "lon": lon, "parcels_found": [f["appellation"] for f in found],
            "checked": checked, "area_checks": area_checks, "record_title_matches": title_match}


def os_grid(ref: str) -> tuple[float, float] | None:
    """'SK3390SW' -> centre of that 500 m square in WGS 84."""

    m = re.fullmatch(r"\s*([HJNOST][A-HJ-Z])\s*(\d{2})\s*(\d{2})\s*(NE|NW|SE|SW)?\s*", (ref or "").upper())
    if not m:
        return None
    letters = "ABCDEFGHJKLMNOPQRSTUVWXYZ"  # no I
    l1, l2 = letters.index(m[1][0]), letters.index(m[1][1])
    e100 = ((l1 - 2) % 5) * 5 + (l2 % 5)
    n100 = (19 - (l1 // 5) * 5) - (l2 // 5)
    e = e100 * 100000 + int(m[2]) * 1000
    n = n100 * 100000 + int(m[3]) * 1000
    if m[4]:
        e += 500 if "E" in m[4] else 0
        n += 500 if m[4].startswith("N") else 0
        e, n = e + 250, n + 250
    else:
        e, n = e + 500, n + 500
    lon, lat = BNG.transform(e, n)
    return lat, lon


async def geocode(read: dict, country: str) -> dict:
    names = {"CA": "Canada", "NZ": "New Zealand", "GB": "United Kingdom", "AU": "Australia"}
    p = read.get("place") or {}
    town = p.get("locality") or p.get("city")
    # A road name alone is ambiguous (many towns have a "Mount Pleasant Road"): only with a town.
    tries = [("street", [p.get("road"), town, p.get("region")]), ("street", [p.get("road"), town]),
             ("town", [town, p.get("region")]), ("district", [p.get("district"), p.get("region")]), ("region", [p.get("region")])]
    seen = set()
    for level, parts in tries:
        parts = [x for x in parts if x]
        if not parts or (level == "street" and not (p.get("road") and town)):
            continue
        q = ", ".join(dict.fromkeys(parts + [names.get(country, "")]))
        if q in seen:
            continue
        seen.add(q)
        await asyncio.sleep(1.1)
        try:
            hits = await geocode_place(q, limit=1)
        except Exception:  # noqa: BLE001
            hits = None
        if hits:
            return {"method": f"geocoded ({level})", "query": q, "lat": hits[0].latitude, "lon": hits[0].longitude}
    return {"method": None}


async def anchor(read: dict, country: str) -> dict:
    if country == "NZ":
        a = nz_anchor(read)
        if a.get("method"):
            return a
    if country == "GB" and read.get("grid_reference"):
        pt = os_grid(read["grid_reference"])
        if pt:
            return {"method": f"OS grid reference {read['grid_reference']}", "lat": pt[0], "lon": pt[1]}
    return await geocode(read, country)


# ------------------------------------------------------------------ run

async def main() -> None:
    rows = []
    for name, doc in INTERNATIONAL.items():
        img, page = main_sheet(doc)
        read, t_detect = detect(img)
        country = read.get("country") or rule_country(read)
        t = time.time()
        a = await anchor(read, country)
        t_anchor = time.time() - t
        if name.startswith("UK") and a.get("lat") is not None:
            a["km_from_true_location"] = km(UK_REFERENCE, (a["lat"], a["lon"]))
        rows.append({"document": name, "true": TRUTH[name], "detected": country, "rule": rule_country(read),
                     "anchor": a, "seconds": {"detect": round(t_detect, 1), "anchor": round(t_anchor, 1)}})
        print(json.dumps(rows[-1], ensure_ascii=False))

    # US regression: every sample must come out as US so the gate leaves it untouched
    samples = json.loads((ROOT / "data/samples.json").read_text(encoding="utf-8"))
    samples = samples if isinstance(samples, list) else samples.get("samples", samples)
    us = []
    for s in samples:
        img, page = main_sheet(s["id"])
        read, t_detect = detect(img)
        us.append({"sample": s["title"], "location": s["location"], "detected": read.get("country"), "rule": rule_country(read),
                   "evidence": read.get("country_evidence"), "seconds": round(t_detect, 1)})
        print(json.dumps(us[-1], ensure_ascii=False))
    (OUT / "results2.json").write_text(json.dumps({"international": rows, "us_regression": us}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
