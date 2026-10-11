"""
ROAM for Indian land records: a standalone prototype, not part of the main pipeline.

An Indian record names its parcel instead of drawing it to scale: state, district, tehsil, revenue
circle, village and khasra number, with an area in hectares and the neighbouring khasras. The
geometry lives in the state's village map (Bhu-Naksha). So the flow is:

  1. read    Gemini reads the scanned record (Hindi) into structured fields
  2. resolve the Hindi place names are matched to Bhu-Naksha's district/tehsil/circle/village codes
  3. lookup  the record of rights and the plot's outline come from the public Bhu-Naksha services
  4. verify  area (document vs record vs map), neighbours, side lengths
  5. place   the outline is converted to WGS 84 and drawn on satellite imagery

Implemented for Chhattisgarh (bhunaksha.cg.nic.in, state 22). Run from the repository root:
    roam-env/Scripts/python -X utf8 experiments/india_bhunaksha/roam_india.py <record.pdf> [...]
"""

from __future__ import annotations

import difflib
import io
import json
import math
import re
import sys
import unicodedata
from pathlib import Path

import cv2
import httpx
import numpy as np
import pymupdf
from google.genai import types
from PIL import Image
from pyproj import Transformer
from shapely.geometry import Point, Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.services import static_map  # noqa: E402
from app.services.vision import _generate_with_fallback, _get_client  # noqa: E402

BASE = "https://bhunaksha.cg.nic.in/"
STATE = "22"
# Chhattisgarh's village maps are stored in UTM zone 44N (checked against imagery in the report)
TO_WGS84 = Transformer.from_crs("EPSG:32644", "EPSG:4326", always_xy=True)
OUT = Path(__file__).parent / "out"

http = httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, timeout=120)

READ_PROMPT = """This is a scanned Indian land record (a khasra map / plot report). Read it and return JSON:
{"state": str|null, "district": str|null, "tehsil": str|null, "revenue_circle": str|null,
 "village": str|null, "patwari_halka": str|null, "khasra_no": str|null,
 "area_hectare": number|null, "map_scale": str|null,
 "neighbour_khasras": [str]  (ONLY the khasra numbers of plots that share an edge with the highlighted
                             plot; leave out plots that are nearby but do not touch it),
 "edge_lengths_m": [number]  (side lengths printed along the highlighted plot's edges, if any)}
Keep place names exactly as printed (Devanagari). Do not include personal names."""


# ---------------------------------------------------------------- 1. read

def read_document(pdf: Path) -> tuple[dict, Image.Image]:
    page = pymupdf.open(pdf)[0]
    img = Image.open(io.BytesIO(page.get_pixmap(dpi=170).tobytes("png"))).convert("RGB")
    resp = _generate_with_fallback(
        _get_client(),
        contents=[img, READ_PROMPT],
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
    )
    return json.loads(resp.text), img


# ---------------------------------------------------------------- 2. resolve

def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFC", s or "")
    s = re.sub(r"^\d+\s*", "", s)  # "5702028 अधचरा" -> "अधचरा"
    return re.sub(r"[\s\-\.,:()]", "", s)


def _pick(options: list[dict], name: str | None) -> tuple[dict | None, float]:
    target = _norm(name)
    if not target:
        return None, 0.0
    best, score = None, 0.0
    for o in options:
        cand = _norm(o["value"])
        s = 1.0 if cand == target else difflib.SequenceMatcher(None, cand, target).ratio()
        if s > score:
            best, score = o, s
    return (best, score) if score >= 0.75 else (None, score)


def resolve_codes(doc: dict) -> dict:
    """Walk district -> tehsil -> revenue circle -> village, matching each printed name."""

    wanted = [doc.get("district"), doc.get("tehsil"), doc.get("revenue_circle"), doc.get("village")]
    chosen: list[dict] = []
    trail = []
    for level in range(4):
        codes = "".join(o["code"] + "," for o in chosen)
        lists = http.post(BASE + "rest/Levels/ListsAfterLevel",
                          data={"state": STATE, "level": str(level), "codes": codes, "hasmap": "true"}).json()
        opt, score = _pick(lists[0], wanted[level])
        if opt is None and len(lists[0]) == 1:  # a single choice (e.g. one circle) needs no match
            opt, score = lists[0][0], 1.0
        if opt is None:
            raise LookupError(f"level {level + 1}: no match for {wanted[level]!r}")
        chosen.append(opt)
        trail.append({"printed": wanted[level], "matched": opt["value"], "code": opt["code"], "score": round(score, 2)})
    return {"levels": "".join(o["code"] + "," for o in chosen), "trail": trail}


# ---------------------------------------------------------------- 3. lookup

def plot_record(levels: str, khasra: str) -> dict:
    r = http.get(BASE + "ScalarDatahandler", params={"OP": "5", "state": STATE, "levels": levels, "plotno": khasra}).json()
    if r.get("has_data") != "Y":
        raise LookupError(f"khasra {khasra} not found in this village")
    m = re.search(r"क्षेत्रफल\s*:\s*([\d.]+)\s*हेक्टेयर", r.get("info") or "")
    return {"id": r["ID"], "gis_code": r["gisCode"], "bbox": (r["xmin"], r["ymin"], r["xmax"], r["ymax"]),
            "record_area_ha": float(m.group(1)) if m else None}


def plot_outline(rec: dict, size: int = 900) -> Polygon:
    """The plot's outline in UTM metres, traced from Bhu-Naksha's 'selected plot' map image."""

    xmin, ymin, xmax, ymax = rec["bbox"]
    pad = max(xmax - xmin, ymax - ymin) * 0.08
    x0, y0, x1, y1 = xmin - pad, ymin - pad, xmax + pad, ymax + pad
    side = max(x1 - x0, y1 - y0)
    x1, y1 = x0 + side, y0 + side
    img = http.get(BASE + "WMS", params={
        "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap", "FORMAT": "image/png", "TRANSPARENT": "true",
        "LAYERS": "PLOT_LIST", "STYLES": "PLOT_SELECTION", "state": STATE, "gis_code": rec["gis_code"],
        "plot_id": rec["id"], "BBOX": f"{x0},{y0},{x1},{y1}", "WIDTH": str(size), "HEIGHT": str(size)}).content
    alpha = np.array(Image.open(io.BytesIO(img)).convert("RGBA"))[..., 3]
    mask = (alpha > 0).astype(np.uint8)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    c = max(contours, key=cv2.contourArea)
    c = cv2.approxPolyDP(c, 1.0, True)[:, 0, :]
    px = side / size
    return Polygon([(x0 + (u + 0.5) * px, y1 - (v + 0.5) * px) for u, v in c]).buffer(0)


def neighbours(rec: dict, outline: Polygon, step_m: float = 6.0, offset_m: float = 4.0) -> set[str]:
    """Khasras just outside the outline, sampled along its boundary."""

    found: set[str] = set()
    ring = outline.exterior
    centre = outline.centroid
    for d in np.arange(0, ring.length, step_m):
        p = ring.interpolate(d)
        vx, vy = p.x - centre.x, p.y - centre.y
        n = math.hypot(vx, vy) or 1
        q = Point(p.x + vx / n * offset_m, p.y + vy / n * offset_m)
        if outline.contains(q):
            continue
        try:
            r = http.post(BASE + "rest/MapInfo/getPlotAtXY",
                          data={"state": STATE, "giscode": rec["gis_code"], "x": q.x, "y": q.y}).json()
        except Exception:
            continue
        no = str(r.get("plotNo") or r.get("kide") or "").strip()
        if no:
            found.add(no)
    return found


# ---------------------------------------------------------------- 4/5. verify and place

def run(pdf: Path) -> dict:
    OUT.mkdir(exist_ok=True)
    doc, _ = read_document(pdf)
    res = resolve_codes(doc)
    rec = plot_record(res["levels"], str(doc["khasra_no"]))
    outline = plot_outline(rec)
    map_area_ha = outline.area / 10_000
    nbrs = neighbours(rec, outline)
    printed = {str(n).strip() for n in doc.get("neighbour_khasras") or []}
    perim = outline.exterior.length
    edges = doc.get("edge_lengths_m") or []

    ring = [TO_WGS84.transform(x, y) for x, y in outline.exterior.coords]
    lon, lat = TO_WGS84.transform(outline.centroid.x, outline.centroid.y)
    # Only about half of India's village maps are geo-referenced; the rest are stored in local
    # survey coordinates with an arbitrary origin, which land far outside India when read as UTM.
    georeferenced = 68 <= lon <= 98 and 6 <= lat <= 37
    fig = None
    if georeferenced:
        png = static_map.render([ring], [f"Khasra {doc['khasra_no']}"], width=1000, height=700, fill=0.7, max_zoom=18)  # z19 imagery is often missing in rural India
        fig = OUT / f"{pdf.stem}_placed.jpg"
        Image.open(io.BytesIO(png)).convert("RGB").save(fig, quality=88)

    def pct(a, b):
        return None if not a or not b else round(100 * (a - b) / b, 1)

    report = {
        "document": pdf.name,
        "read": {k: v for k, v in doc.items()},
        "resolved": res["trail"],
        "gis_code": rec["gis_code"],
        "areas_ha": {"document": doc.get("area_hectare"), "record_of_rights": rec["record_area_ha"], "village_map": round(map_area_ha, 4)},
        "area_checks_pct": {"document_vs_record": pct(doc.get("area_hectare"), rec["record_area_ha"]),
                            "map_vs_record": pct(map_area_ha, rec["record_area_ha"])},
        "neighbours": {"printed_on_document": sorted(printed), "from_village_map": sorted(nbrs),
                       "agree": sorted(printed & nbrs), "only_on_document": sorted(printed - nbrs)},
        "perimeter_m": {"village_map": round(perim, 1), "sum_of_printed_edges": round(sum(edges), 1) if edges else None},
        "placed": ({"centroid_wgs84": [round(lon, 6), round(lat, 6)], "figure": fig.name} if georeferenced
                   else {"centroid_wgs84": None, "reason": "village map is not geo-referenced (local survey coordinates)"}),
    }
    checks = report["area_checks_pct"]
    doc_matches_record = checks["document_vs_record"] is not None and abs(checks["document_vs_record"]) < 1
    map_matches_record = checks["map_vs_record"] is not None and abs(checks["map_vs_record"]) <= 15
    nbrs_match = bool(printed) and len(printed & nbrs) >= 0.6 * len(printed)
    if not georeferenced:
        report["status"] = "identified, not placed: village map is not geo-referenced"
    elif checks["map_vs_record"] is not None and abs(checks["map_vs_record"]) > 50:
        # the village map and the record of rights describe different land (an unrecorded
        # subdivision, a stale record): exactly what a person has to look at
        report["status"] = "needs review: map and record disagree"
    elif doc_matches_record and map_matches_record and nbrs_match:
        report["status"] = "verified"
    else:
        report["status"] = "approximate"
    (OUT / f"{pdf.stem}_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        try:
            r = run(Path(arg))
            print(json.dumps({k: r[k] for k in ("document", "resolved", "areas_ha", "area_checks_pct", "neighbours", "perimeter_m", "placed", "status")}, ensure_ascii=False, indent=1))
        except Exception as exc:
            print(json.dumps({"document": Path(arg).name, "error": repr(exc)}, ensure_ascii=False))
