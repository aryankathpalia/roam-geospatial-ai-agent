"""
Read-only tools for the placement-review agent.

Each tool takes a document's result dict (plus its own arguments) and returns a small JSON-serialisable
dict: what a surveyor would look up to decide whether a sheet is placed correctly -- the printed location
evidence and how it was used, the county parcels at a spot, how well the confirmed outlines fit them at a
hypothetical position, and the document's own text. Nothing here changes a result; changes are proposed
separately and applied only on the user's approval.

`TOOLS` maps each tool name to (function, JSON schema of its arguments) in the OpenAI tool-calling format;
`run_tool` dispatches one call and turns any failure into an {"error": ...} result the model can read.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Callable

import httpx

from app.services import apn as apn_service
from app.services import location_evidence

_M_PER_DEG_LAT = 110_540.0
_MAX_NEAR_RADIUS_M = 400.0
_MAX_NEAR_PARCELS = 40
_MAX_TEXT_HITS = 12


# ---------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------


def _kx(lat: float) -> float:
    return 111_320.0 * math.cos(math.radians(lat))


def _offset_m(frm: tuple[float, float], to: tuple[float, float]) -> dict:
    """(lon, lat) -> (lon, lat) as metres east / north and distance."""

    lat = (frm[1] + to[1]) / 2
    e, n = (to[0] - frm[0]) * _kx(lat), (to[1] - frm[1]) * _M_PER_DEG_LAT
    return {"east_m": round(e, 1), "north_m": round(n, 1), "distance_m": round(math.hypot(e, n), 1)}


def _ring_of(parcel: dict) -> list | None:
    geo = parcel.get("boundary_geojson_wgs84") or {}
    coords = (geo.get("geometry") or {}).get("coordinates") or []
    return coords[0] if coords and coords[0] else None


def _centroid(ring: list) -> tuple[float, float]:
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _area_acres(ring: list) -> float:
    return round(apn_service._area_m2([ring]) * 10.7639 / 43560, 3)


def _page(result: dict, page_number: int) -> dict:
    page = next((p for p in result.get("pages", []) if p["page_number"] == page_number), None)
    if page is None:
        raise ValueError(f"page {page_number} not found")
    return page


def _confirmed(page: dict) -> list[dict]:
    return [
        p for r in page.get("regions", []) for p in r.get("parcels") or []
        if p.get("human_confirmed") and _ring_of(p)
    ]


def _label(parcel: dict) -> str | None:
    return (parcel.get("vision_geometry") or {}).get("parcel_label")


def _group_centre(page: dict) -> tuple[float, float] | None:
    pts = [pt for p in _confirmed(page) for pt in _ring_of(p)]
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _doc_text(result: dict, page_number: int | None = None) -> str:
    return "\n".join(
        r.get("ocr_text") or "" for p in result.get("pages", [])
        if page_number is None or p["page_number"] == page_number for r in p.get("regions", [])
    )


def _sheet_pages(result: dict) -> list[dict]:
    return [p for p in result.get("pages", []) if _confirmed(p)]


# ---------------------------------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------------------------------


def get_document_overview(result: dict) -> dict:
    """The document anchor and, per sheet with confirmed parcels, each parcel's placement state."""

    sheets = []
    for page in _sheet_pages(result):
        parcels = []
        for p in _confirmed(page):
            cal = p.get("calibration") or {}
            sv = p.get("spatial_validation") or {}
            pl = p.get("placement") or {}
            ring = _ring_of(p)
            apn_fit = pl.get("apn_fit") or {}
            parcels.append({
                "label": _label(p),
                "centroid_lat_lon": [round(_centroid(ring)[1], 6), round(_centroid(ring)[0], 6)],
                "area_acres_on_map": _area_acres(ring),
                "stated_area_sqft": sv.get("stated_area_sqft"),
                "area_matches_stated": sv.get("area_matches_stated"),
                "calibration": {
                    "status": cal.get("status"), "rotation_deg": cal.get("rotation_deg"),
                    "scale_ft_per_px": cal.get("scale_ft_per_px"), "scale_agreement_pct": cal.get("scale_agreement_pct"),
                },
                "placement_status": pl.get("status"),
                "placed_by": (ring and (p["boundary_geojson_wgs84"].get("properties") or {}).get("georeferenced")),
                "county_fit": {k: apn_fit.get(k) for k in ("mode", "corroborated", "improves", "shift_m", "rotation_deg")} if apn_fit else None,
                "control_point_fit": (pl.get("control_fit") or {}).get("validated"),
                "moved_by_hand": p.get("manual_position"),
                "notes": pl.get("notes", [])[:4],
            })
        sheets.append({"page": page["page_number"], "role": (page.get("sheet") or {}).get("role"), "parcels": parcels})
    return {
        "anchor": {
            "lat": result.get("anchor_lat"), "lon": result.get("anchor_lon"),
            **{k: (result.get("anchor") or {}).get(k) for k in ("precision", "source")},
        },
        "sheets": sheets,
    }


def get_location_evidence(result: dict, page_number: int) -> dict:
    """What the sheet's drawing says about its location, checked against the OCR text as the pipeline does
    -- including what the check DROPPED (an item the model read but OCR could not confirm is ignored by the
    pipeline, e.g. a ground-to-grid factor whose digits OCR garbled)."""

    page = _page(result, page_number)
    raw = (page.get("sheet") or {}).get("location_evidence")
    if not raw:
        return {"page": page_number, "evidence": None, "note": "no location evidence was read on this sheet"}
    checked = location_evidence.verify(raw, _doc_text(result))
    return {
        "page": page_number,
        "apns": checked["apns"],
        "printed_coordinates": checked["coordinates"],
        "coordinate_system": checked["coordinate_system"],
        "ground_to_grid_multiplier_used": location_evidence.ground_to_grid(checked),
        "plss": checked.get("plss"),
        "address": checked.get("address"), "city": checked.get("city"), "county": checked.get("county"),
        "state": checked.get("state"),
        "dropped_by_ocr_check": checked.get("dropped", []),
        "as_read_before_check": {"coordinate_system": raw.get("coordinate_system")},
    }


def convert_state_plane(result: dict, northing: float, easting: float, state: str,
                        ground_to_grid: float | None = None) -> dict:
    """A printed state-plane coordinate in each NAD83 zone of `state`, optionally scaled ground -> grid first,
    with each conversion's offset from the current anchor and from the confirmed parcels."""

    from pyproj import Transformer
    from pyproj.database import query_crs_info

    f = ground_to_grid or 1.0
    n, e = northing * f, easting * f
    anchor = (result.get("anchor_lon"), result.get("anchor_lat"))
    centre = next((_group_centre(p) for p in _sheet_pages(result)), None)
    out = []
    for info in query_crs_info(auth_name="EPSG", pj_types=None):
        if "NAD83" not in info.name or state.lower() not in info.name.lower() or "ft" not in info.name:
            continue
        if any(tag in info.name for tag in ("HARN", "CORS", "NSRS", "(2011)", "CSRS", "+")):  # "+": with heights
            continue
        try:
            lon, lat = Transformer.from_crs(f"EPSG:{info.code}", "EPSG:4326", always_xy=True).transform(e, n)
        except Exception:  # noqa: BLE001
            continue
        if not (math.isfinite(lon) and math.isfinite(lat)):
            continue
        row = {"zone": info.name, "epsg": int(info.code), "lat": round(lat, 6), "lon": round(lon, 6)}
        if anchor[0] is not None:
            row["from_anchor"] = _offset_m(anchor, (lon, lat))
        if centre:
            row["from_parcels"] = _offset_m(centre, (lon, lat))
        out.append(row)
    out.sort(key=lambda r: (r.get("from_parcels") or r.get("from_anchor") or {}).get("distance_m", 1e12))
    return {"input": {"northing": northing, "easting": easting, "ground_to_grid": ground_to_grid}, "conversions": out[:4]}


def _county_service(state: str | None):
    services = [s for s in apn_service._SERVICES if not state or s.state.lower() == state.lower()]
    if not services:
        raise ValueError(f"no county parcel service is registered for {state!r}")
    return services[0]


def _feature_to_parcel(svc, feat: dict) -> dict | None:
    rings = (feat.get("geometry") or {}).get("rings") or []
    if not rings:
        return None
    ring = [(float(x), float(y)) for x, y in max(rings, key=len)]
    attrs = feat.get("attributes") or {}
    return {
        "apn": attrs.get(svc.field), "owner": (attrs.get(svc.owner_field) or "").strip() or None,
        "address": (attrs.get(svc.address_field) or "").strip() or None, "ring": ring,
    }


def county_parcels_near(result: dict, lat: float, lon: float, radius_m: float = 150.0, state: str | None = "Nevada") -> dict:
    """The county's parcels within `radius_m` of a point: APN, owner, address, area and offset from the point.
    Lets the agent see what the land around a candidate position actually is."""

    radius_m = max(10.0, min(float(radius_m), _MAX_NEAR_RADIUS_M))
    svc = _county_service(state)
    with httpx.Client(headers={"User-Agent": "ROAM/1.0"}) as client:
        r = client.get(svc.url, params={
            "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects", "distance": radius_m, "units": "esriSRUnit_Meter",
            "outFields": ",".join(f for f in (svc.field, svc.owner_field, svc.address_field) if f),
            "returnGeometry": "true", "outSR": "4326", "f": "json",
        }, timeout=apn_service._HTTP_TIMEOUT)
        r.raise_for_status()
        feats = r.json().get("features") or []
    parcels = []
    for feat in feats:
        p = _feature_to_parcel(svc, feat)
        if not p:
            continue
        c = _centroid(p["ring"])
        parcels.append({
            "apn": p["apn"], "owner": p["owner"], "address": p["address"],
            "area_acres": _area_acres(p["ring"]), "from_point": _offset_m((lon, lat), c),
        })
    parcels.sort(key=lambda p: p["from_point"]["distance_m"])
    return {"source": svc.name, "point": [lat, lon], "radius_m": radius_m, "count": len(parcels),
            "parcels": parcels[:_MAX_NEAR_PARCELS]}


def lookup_county_apns(result: dict, page_number: int, apns: list[str] | None = None) -> dict:
    """Where the county's CURRENT records put the APNs the plat prints (or `apns`), each with the side the
    plat draws it on and its offset from the confirmed parcels -- and whether they form one neighbourhood.
    APNs renumbered since the plat was drawn show up as missing or scattered."""

    page = _page(result, page_number)
    evidence = (page.get("sheet") or {}).get("location_evidence") or {}
    printed = {a["apn"]: a for a in evidence.get("apns", [])}
    wanted = list(dict.fromkeys(apns or list(printed)))[:apn_service._MAX_LOOKUPS]
    svc = _county_service(evidence.get("state") or "Nevada")
    centre = _group_centre(page)
    rows, found_centres = [], []
    with httpx.Client(headers={"User-Agent": "ROAM/1.0"}) as client:
        for a in wanted:
            parcel = None
            for variant in apn_service.apn_variants(a):
                parcel = apn_service._lookup(client, svc, variant)
                if parcel:
                    break
            row = {"apn": a, "plat_role": (printed.get(a) or {}).get("role"), "plat_side": (printed.get(a) or {}).get("side")}
            if parcel is None:
                row["found"] = False
            else:
                c = parcel.centroid
                found_centres.append(c)
                row.update({"found": True, "owner": parcel.owner, "address": parcel.address,
                            "area_acres": _area_acres(parcel.ring)})
                if centre:
                    row["from_parcels"] = _offset_m(centre, c)
            rows.append(row)
    spread = None
    if len(found_centres) >= 2:
        spread = round(max(_offset_m(a, b)["distance_m"] for a in found_centres for b in found_centres), 1)
    return {"source": svc.name, "apns": rows, "found": len(found_centres), "max_spread_between_found_m": spread,
            "hint": "neighbours of one site normally lie within a few hundred metres of each other and of the parcels"}


def evaluate_position(result: dict, page_number: int, east_m: float = 0.0, north_m: float = 0.0,
                      rotation_deg: float = 0.0) -> dict:
    """How the sheet's confirmed outlines would sit among the document's county neighbour parcels if moved by
    (east_m, north_m) and turned `rotation_deg` clockwise about their centre: overlap with neighbours, share
    of the outline hugging a neighbour's edge, mean gap. Lower overlap and higher hugging = better fit."""

    from shapely.affinity import rotate, translate
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    import numpy as np
    import shapely

    site = result.get("apn_site") or {}
    neighbours = [n["ring"] for n in site.get("neighbours") or [] if len(n.get("ring") or []) >= 4]
    if site.get("target") and len(site["target"].get("ring") or []) >= 4:
        neighbours.append(site["target"]["ring"])
    if not neighbours:
        return {"error": "the document has no county parcels to compare with (no APNs found in the county records)"}
    rings = [_ring_of(p) for p in _confirmed(_page(result, page_number))]
    if not rings:
        return {"error": f"page {page_number} has no confirmed, placed parcels"}
    pts = [pt for r in rings for pt in r]
    lon0, lat0 = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    kx = _kx(lat0)

    def local(ring):
        return Polygon([((x - lon0) * kx, (y - lat0) * _M_PER_DEG_LAT) for x, y in ring]).buffer(0)

    group = unary_union([local(r) for r in rings])
    nb = unary_union([local(r) for r in neighbours])
    moved = translate(rotate(group, -rotation_deg, origin="centroid"), east_m, north_m)
    samples = np.array(apn_service._boundary_samples(moved, 2.0))
    d = shapely.distance(nb.boundary, shapely.points(samples))
    return {
        "page": page_number, "east_m": east_m, "north_m": north_m, "rotation_deg": rotation_deg,
        "overlap_with_neighbours_pct": round(moved.intersection(nb).area / group.area * 100, 1),
        "outline_hugging_a_neighbour_pct": round(float((d <= apn_service._HUG_M).mean()) * 100, 1),
        "mean_gap_to_neighbour_edge_m": round(float(np.minimum(d, apn_service._GAP_CAP_M).mean()), 2),
        "nearest_neighbour_edge_m": round(float(d.min()), 1),
        "neighbour_count": len(neighbours),
    }


def search_position_near(result: dict, page_number: int, east_m: float = 0.0, north_m: float = 0.0) -> dict:
    """Runs ROAM's county-parcel fit with the sheet's confirmed parcels first moved by (east_m, north_m): it
    searches 250 m around that guess (and the rotations that align the outlines with the neighbours) for the
    position where they sit in the gap between the county neighbour parcels. Returns the TOTAL move from the
    current position and how well it fits."""

    site = result.get("apn_site") or {}
    if not site.get("neighbours") and not site.get("target"):
        return {"error": "the document has no county parcels to fit to (no APNs found in the county records)"}
    page = _page(result, page_number)
    rings = []
    for p in _confirmed(page):
        ring = _ring_of(p)
        lat = _centroid(ring)[1]
        rings.append([(x + east_m / _kx(lat), y + north_m / _M_PER_DEG_LAT) for x, y in ring])
    if not rings:
        return {"error": f"page {page_number} has no confirmed, placed parcels"}
    fit = apn_service.placement_by_apn(rings, site)
    if not fit:
        return {"error": "the county fit found nothing to fit to"}
    total_e, total_n = east_m + fit["east_m"], north_m + fit["north_m"]
    return {
        "page": page_number, "started_from": {"east_m": east_m, "north_m": north_m},
        "best_total_move": {"east_m": round(total_e, 1), "north_m": round(total_n, 1),
                            "rotation_deg": fit.get("rotation_deg")},
        "mode": fit["mode"], "overlap_with_neighbours": fit.get("overlap"),
        "outline_hugging_share": fit.get("hugging"), "mean_gap_m": fit.get("gap_m"),
        "unique": fit.get("unique"), "corroborated": fit.get("corroborated"),
        "at_search_edge": abs(fit["east_m"]) >= apn_service._SEARCH_M - 1 or abs(fit["north_m"]) >= apn_service._SEARCH_M - 1,
        "hint": "corroborated = fits snugly AND no rival position fits as well; at_search_edge = start nearer and retry",
    }


_IMAGERY_PX = 1024
_IMAGERY_FILL = 0.45  # the outlines fill under half the view: the roads and fields around them must show

_IMAGERY_PROMPT = """You get two images.

IMAGE 1 is the survey plat these parcels come from{plat_note}. Read what borders the parcels -- roads, canals, \
ditches, railways, named tracts -- and on WHICH SIDE of the parcel group (north/east/south/west; use the plat's \
north arrow).

IMAGE 2 is a north-up satellite image. The coloured outlines are those parcels as placed so far: shapes and sizes are \
right, the position may be off -- possibly by a lot, so look at the WHOLE image, not just under the outlines.

For each side of the group that the plat shows bordered by a feature you can find in image 2, give that feature's \
line in image 2: two points [y, x] on it, normalised 0-1000 (top-left 0,0), spanning the stretch beside where the \
parcels belong. Give the line the parcel boundary runs along: the road CENTRELINE when the plat runs the boundary \
down the road (a section/tract line, or the road drawn on both sides of the line), otherwise the edge of the feature \
nearest the parcels. Only features you can actually see; do not guess. Skip sides with nothing identifiable.

The parcels are most likely within about {reach} m of where they are drawn: take the matching features NEAREST the outlines, and check each one is the KIND of feature the plat names -- a "Court" is a short street ending in a cul-de-sac, a "Lateral" or "Ditch" is a canal, a "Way"/"Road"/"Avenue" is a through road. A road of the right direction but the wrong kind, or far away, is not a match.
{user_note}"""

_IMAGERY_SCHEMA = {
    "type": "object",
    "properties": {
        "plat_borders": {"type": "string", "description": "e.g. 'east: CROSS ROAD; south: DATE LATERAL No. 4'"},
        "features": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "side": {"type": "string", "enum": ["north", "east", "south", "west"]},
                    "name": {"type": "string"},
                    "line": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
                    "boundary_on": {"type": "string", "enum": ["centreline", "near_edge"]},
                },
                "required": ["side", "name", "line", "boundary_on"],
            },
        },
        "observations": {"type": "string"},
    },
    "required": ["plat_borders", "features", "observations"],
}
_PLAT_PX = 1600
_LINE_TOL_DEG = 25  # a "north/south" feature line must run within this of east-west (and vice versa)
_AGREE_M = 2.5  # two features on opposite sides implying moves this close agree


def _plat_image(result: dict, page: dict) -> bytes | None:
    """The plat region the sheet's parcels were confirmed on, as PNG (None when the page image is missing)."""

    import io
    from pathlib import Path

    from PIL import Image

    doc = result.get("document_id")
    path = Path("data/documents") / doc / "pages" / f"page_{page['page_number']:03d}.png" if doc else None
    if not path or not path.exists():
        return None
    region = next((r for r in page.get("regions", []) if any(p.get("human_confirmed") for p in r.get("parcels") or [])), None)
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as im:
        im = im.convert("L")
        if region and region.get("bbox"):
            x, y, w, h = region["bbox"]
            im = im.crop((int(x), int(y), int(x + w), int(y + h)))
        im.thumbnail((_PLAT_PX, _PLAT_PX))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
    return buf.getvalue()


def _feature_offsets(features: list[dict], box: tuple[float, float, float, float], size: int) -> list[dict]:
    """For each located feature line, the pixel move that puts the group's matching side on it. `box`: the
    outlines' pixel extent (left, top, right, bottom). Lines running the wrong way for their side are dropped."""

    left, top, right, bottom = box
    out = []
    for f in features:
        line = f.get("line") or []
        if len(line) < 2 or any(len(pt) != 2 for pt in line[:2]):
            continue
        (y1, x1), (y2, x2) = ((v * size / 1000 for v in pt) for pt in line[:2])
        (y1, x1), (y2, x2) = (tuple(pt) for pt in ((y1, x1), (y2, x2)))
        angle = abs(math.degrees(math.atan2(y2 - y1, x2 - x1))) % 180  # 0 = horizontal, 90 = vertical
        side = f.get("side")
        if side in ("east", "west"):
            if abs(angle - 90) > _LINE_TOL_DEG:
                continue
            ymid = (top + bottom) / 2  # where the line crosses the group's middle height
            x_at = x1 if abs(y2 - y1) < 1e-6 else x1 + (x2 - x1) * (ymid - y1) / (y2 - y1)
            out.append({**f, "dx_px": x_at - (right if side == "east" else left), "dy_px": None})
        elif side in ("north", "south"):
            if min(angle, 180 - angle) > _LINE_TOL_DEG:
                continue
            xmid = (left + right) / 2
            y_at = y1 if abs(x2 - x1) < 1e-6 else y1 + (y2 - y1) * (xmid - x1) / (x2 - x1)
            out.append({**f, "dx_px": None, "dy_px": y_at - (bottom if side == "south" else top)})
    return out


def look_at_imagery(result: dict, page_number: int, east_m: float = 0.0, north_m: float = 0.0,
                    user_note: str | None = None, max_move_m: float = 200.0) -> dict:
    """Shows a vision model the plat and the satellite imagery with the sheet's confirmed outlines (moved by
    east_m / north_m). The model only LOCATES the features the plat draws around the parcels (a road on the
    east, a canal on the south...); the move that puts the group's sides on them is then MEASURED here from
    the known outline position and the image scale -- vision models find things well but estimate distances
    badly. Look again at the suggested move to confirm it lines up before proposing it."""

    import base64
    import io

    from google.genai import types
    from PIL import Image, ImageDraw

    from app.services import static_map, vision

    page = _page(result, page_number)
    parcels = _confirmed(page)
    if not parcels:
        raise ValueError(f"page {page_number} has no confirmed, placed parcels")
    rings = []
    for p in parcels:
        ring = _ring_of(p)
        lat = _centroid(ring)[1]
        rings.append([(x + east_m / _kx(lat), y + north_m / _M_PER_DEG_LAT) for x, y in ring])
    size = _IMAGERY_PX
    png = static_map.render(rings, None, width=size, height=size, fill=_IMAGERY_FILL, max_zoom=20)
    pts = [pt for r in rings for pt in r]
    bounds = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
    z = max(2, static_map._zoom_for(bounds, size, size, _IMAGERY_FILL, 20))
    cx, cy = static_map._px((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2, z)
    ox, oy = cx - size / 2, cy - size / 2
    pix = [static_map._px(x, y, z) for x, y in pts]
    box = (min(p[0] for p in pix) - ox, min(p[1] for p in pix) - oy, max(p[0] for p in pix) - ox, max(p[1] for p in pix) - oy)
    mpp = 156543.03392 * math.cos(math.radians((bounds[1] + bounds[3]) / 2)) / 2**z

    plat = _plat_image(result, page)
    labels = ", ".join(filter(None, (_label(p) for p in parcels)))
    prompt = _IMAGERY_PROMPT.format(
        reach=int(max_move_m),
        plat_note=f" (parcels: {labels})" if plat else " -- NOT AVAILABLE: judge the borders from image 2 alone",
        user_note=f"\nThe user reviewing this says: \"{user_note.strip()[:400]}\"" if user_note else "",
    )
    parts = ([types.Part.from_bytes(data=plat, mime_type="image/png")] if plat else []) + [
        types.Part.from_bytes(data=png, mime_type="image/png"), prompt]
    response = vision._generate_with_fallback(
        vision._get_client(), contents=parts,
        config=types.GenerateContentConfig(temperature=0.0, response_mime_type="application/json",
                                           response_schema=_IMAGERY_SCHEMA),
    )
    try:
        seen = json.loads(response.text)
    except (TypeError, ValueError):
        raise ValueError("the vision model did not return a readable answer")

    found = _feature_offsets(seen.get("features") or [], box, size)
    dxs = [f["dx_px"] for f in found if f["dx_px"] is not None]
    dys = [f["dy_px"] for f in found if f["dy_px"] is not None]
    # two features on opposite sides must agree (east road and west fence imply the same move), else unsure
    disagree = [axis for axis, v in (("east-west", dxs), ("north-south", dys)) if len(v) > 1 and (max(v) - min(v)) * mpp > _AGREE_M]
    dx = sum(dxs) / len(dxs) if dxs else 0.0
    dy = sum(dys) / len(dys) if dys else 0.0
    shift_e, shift_n = dx * mpp, -dy * mpp

    img = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(img)
    for f in found:  # the features it matched, for the user to check
        (y1, x1), (y2, x2) = f["line"][:2]
        draw.line([(x1 * size / 1000, y1 * size / 1000), (x2 * size / 1000, y2 * size / 1000)], fill=(255, 225, 0), width=5)
    img.thumbnail((640, 640))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=78)
    lines_up = bool(found) and abs(shift_e) <= 3 and abs(shift_n) <= 3
    return {
        "page": page_number, "viewed_at_move": {"east_m": east_m, "north_m": north_m},
        "plat_shows_around_parcels": seen.get("plat_borders"), "observations": seen.get("observations"),
        "features_located": [
            {"side": f["side"], "name": f["name"], "boundary_on": f["boundary_on"],
             "move_to_meet_it_m": round((f["dx_px"] if f["dx_px"] is not None else -f["dy_px"]) * mpp, 1)}
            for f in found
        ],
        "pinned_axes": {"east_west": bool(dxs), "north_south": bool(dys)},
        "features_disagree_on": disagree,
        "lines_up": lines_up,
        "suggested_extra_move_m": {"east_m": round(shift_e, 1), "north_m": round(shift_n, 1)},
        "suggested_total_move_m": {"east_m": round(east_m + shift_e, 1), "north_m": round(north_m + shift_n, 1)},
        "hint": ("no bordering feature located -- nothing to measure against" if not found else
                 "look again at suggested_total_move_m: lines_up there (within 3 m) confirms it; an axis with no "
                 "located feature is not pinned"),
        "_image_jpeg_b64": base64.b64encode(buf.getvalue()).decode(),  # for the user's step list, not the model
    }


_CLOSE_Z = 19  # ~0.23 m/px at Washoe's latitude: a few pixels of error is under a metre
_CLOSE_PX = 640
_CLOSE_SETTLED_M = 1.5

_CLOSE_PROMPT = """IMAGE 1 is the survey plat. IMAGE 2 is a north-up satellite CLOSE-UP, {mpp:.2f} metres per pixel. The \
red lines are the parcel outlines as currently placed; the {side} side of the parcel group runs through the middle \
of this close-up.

The plat shows {name} along the parcels' {side} side, with the boundary on its {on}. Find {name} in IMAGE 2 -- it \
must be the KIND of feature the plat names (a "Court" ends in a cul-de-sac, a "Lateral"/"Ditch" is a canal) -- and \
give the line where the parcel boundary should run: two points [y, x] normalised 0-1000 (top-left 0,0) on that \
line, spanning this image. If {name} is not visible in this close-up, set found to false."""

_CLOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "line": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
        "note": {"type": "string"},
    },
    "required": ["found", "note"],
}


def _close_up(rings: list, lon: float, lat: float) -> tuple[bytes, float, Any]:
    """A north-up satellite close-up centred on (lon, lat) with the outlines drawn: (PNG, metres per pixel,
    a function mapping (lon, lat) to its pixel position)."""

    import io

    from PIL import Image, ImageDraw

    from app.services import static_map

    z, size = _CLOSE_Z, _CLOSE_PX
    cx, cy = static_map._px(lon, lat, z)
    left, top = cx - size / 2, cy - size / 2
    canvas = Image.new("RGBA", (size, size), (40, 40, 40, 255))
    T = static_map._TILE
    with httpx.Client(headers={"User-Agent": "ROAM/1.0"}) as client:
        for tx in range(int(left // T), int((left + size) // T) + 1):
            for ty in range(int(top // T), int((top + size) // T) + 1):
                img = static_map._tile(client, static_map._TILE_URL, z, tx % 2**z, ty)
                if img is not None:
                    canvas.alpha_composite(img, (int(tx * T - left), int(ty * T - top)))

    def to_xy(lo: float, la: float) -> tuple[float, float]:
        x, y = static_map._px(lo, la, z)
        return x - left, y - top

    d = ImageDraw.Draw(canvas)
    for ring in rings:
        xy = [to_xy(*p) for p in ring]
        d.line(xy + [xy[0]], fill=(255, 30, 60, 255), width=3)
    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    return buf.getvalue(), 156543.03392 * math.cos(math.radians(lat)) / 2**z, to_xy


def _refine_side(rings: list, feature: dict, plat: bytes | None) -> dict:
    """One close-up of the group's `feature['side']` edge: how far (m, east/north positive) that side must move
    to sit on the named feature, or found=False."""

    from google.genai import types

    from app.services import vision

    pts = [p for r in rings for p in r]
    w, s_, e, n = min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)
    side = feature["side"]
    lon = {"east": e, "west": w}.get(side, (w + e) / 2)
    lat = {"north": n, "south": s_}.get(side, (s_ + n) / 2)
    png, mpp, to_xy = _close_up(rings, lon, lat)
    prompt = _CLOSE_PROMPT.format(mpp=mpp, side=side, name=feature["name"],
                                  on="centreline" if feature.get("boundary_on") == "centreline" else "near edge")
    parts = ([types.Part.from_bytes(data=plat, mime_type="image/png")] if plat else []) + [
        types.Part.from_bytes(data=png, mime_type="image/png"), prompt]
    resp = vision._generate_with_fallback(
        vision._get_client(), contents=parts,
        config=types.GenerateContentConfig(temperature=0.0, response_mime_type="application/json",
                                           response_schema=_CLOSE_SCHEMA),
    )
    try:
        seen = json.loads(resp.text)
    except (TypeError, ValueError):
        return {"side": side, "name": feature["name"], "found": False, "note": "unreadable answer"}
    line = seen.get("line") or []
    if not seen.get("found") or len(line) < 2 or any(len(p) != 2 for p in line[:2]):
        return {"side": side, "name": feature["name"], "found": False, "note": seen.get("note")}
    (y1, x1), (y2, x2) = ((v * _CLOSE_PX / 1000 for v in p) for p in line[:2])
    (y1, x1), (y2, x2) = (tuple(p) for p in ((y1, x1), (y2, x2)))
    ex, ey = to_xy(lon, lat)  # where the group's side is drawn
    angle = abs(math.degrees(math.atan2(y2 - y1, x2 - x1))) % 180
    if side in ("east", "west"):
        if abs(angle - 90) > _LINE_TOL_DEG:
            return {"side": side, "name": feature["name"], "found": False, "note": "line runs the wrong way"}
        x_at = x1 if abs(y2 - y1) < 1e-6 else x1 + (x2 - x1) * (ey - y1) / (y2 - y1)
        return {"side": side, "name": feature["name"], "found": True, "east_m": (x_at - ex) * mpp, "north_m": None}
    if min(angle, 180 - angle) > _LINE_TOL_DEG:
        return {"side": side, "name": feature["name"], "found": False, "note": "line runs the wrong way"}
    y_at = y1 if abs(x2 - x1) < 1e-6 else y1 + (y2 - y1) * (ex - x1) / (x2 - x1)
    return {"side": side, "name": feature["name"], "found": True, "east_m": None, "north_m": -(y_at - ey) * mpp}


def _refine_close(result: dict, page: dict, features: list[dict], east_m: float, north_m: float) -> dict:
    """Close-up refinement from a move: measure each named side, move by the average per axis, and measure
    again to confirm. Returns the refined total move and its last residual."""

    plat = _plat_image(result, page)
    e, n = east_m, north_m
    history = []
    for _ in range(3):
        rings = []
        for p in _confirmed(page):
            ring = _ring_of(p)
            lat = _centroid(ring)[1]
            rings.append([(x + e / _kx(lat), y + n / _M_PER_DEG_LAT) for x, y in ring])
        sides = [_refine_side(rings, f, plat) for f in features]
        de = [s["east_m"] for s in sides if s["found"] and s["east_m"] is not None]
        dn = [s["north_m"] for s in sides if s["found"] and s["north_m"] is not None]
        step_e = sum(de) / len(de) if de else 0.0
        step_n = sum(dn) / len(dn) if dn else 0.0
        history.append({"at": {"east_m": round(e, 1), "north_m": round(n, 1)},
                        "sides": [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in s.items()} for s in sides]})
        if not de and not dn:
            return {"refined": False, "history": history, "note": "the close-ups did not show the named features"}
        if math.hypot(step_e, step_n) <= _CLOSE_SETTLED_M:
            return {"refined": True, "east_m": e, "north_m": n, "residual_m": round(math.hypot(step_e, step_n), 1),
                    "pinned": {"east_west": bool(de), "north_south": bool(dn)}, "history": history}
        e, n = e + step_e, n + step_n
    return {"refined": False, "history": history, "note": "the close-up readings did not settle"}


_REFINE_LOOKS = 4
_DEFAULT_REACH_M = 200.0  # how far from the starting position the imagery may move the parcels
_SETTLED_M = 4.0  # a confirming look asking for less than this is "lined up" (vision noise is a few metres)


def find_position_from_imagery(result: dict, page_number: int, east_m: float = 0.0, north_m: float = 0.0,
                               user_note: str | None = None, max_move_m: float = _DEFAULT_REACH_M) -> dict:
    """look_at_imagery repeated until it settles: look, move by what it measured, look again to confirm, up to
    four looks. A single reading wanders by several metres, so the answer is the average of the positions
    the confirming looks accept, with their spread as the uncertainty. Returns the TOTAL move from now."""

    looks, images, accepted = [], [], []
    e, n = east_m, north_m
    for _ in range(_REFINE_LOOKS):
        out = look_at_imagery(result, page_number, e, n, user_note, max_move_m)
        if out.get("_image_jpeg_b64"):
            images.append(out.pop("_image_jpeg_b64"))
        extra = out["suggested_extra_move_m"]
        looks.append({"at": {"east_m": round(e, 1), "north_m": round(n, 1)}, "features": out["features_located"],
                      "needs_further": extra})
        if not out["features_located"]:
            break
        if math.hypot(e + extra["east_m"] - east_m, n + extra["north_m"] - north_m) > max_move_m:
            break  # matched roads beyond the reach: probably the wrong ones -- the close-ups start from here instead
        residual = math.hypot(extra["east_m"], extra["north_m"])
        if residual <= _SETTLED_M:
            accepted.append((e + extra["east_m"], n + extra["north_m"]))  # this look's own best estimate
            if len(accepted) >= 2:
                break
        e, n = e + extra["east_m"], n + extra["north_m"]
    feats = next((lk["features"] for lk in looks if lk["features"]), [])
    if not feats:
        return {"page": page_number, "settled": False, "looks": looks,
                "plat_shows_around_parcels": out.get("plat_shows_around_parcels"),
                "observations": out.get("observations"),
                "hint": "could not find the features the plat draws around the parcels: report that instead of proposing",
                "_image_jpeg_b64": images[-1] if images else None}
    if not accepted:
        return {"page": page_number, "settled": False, "looks": looks,
                "plat_shows_around_parcels": out.get("plat_shows_around_parcels"),
                "observations": out.get("observations"),
                "hint": "the imagery readings did not settle (they vary by ~10 m between looks): report what was seen "
                        "-- a few metres is below what the imagery can measure -- instead of proposing a move",
                "_image_jpeg_b64": images[-1] if images else None}
    me = sum(a[0] for a in accepted) / len(accepted)
    mn = sum(a[1] for a in accepted) / len(accepted)
    spread = max(math.hypot(a[0] - me, a[1] - mn) for a in accepted)
    common = {
        "page": page_number, "lines_up_with": [f"{f['name']} ({f['side']})" for f in feats],
        "plat_shows_around_parcels": out.get("plat_shows_around_parcels"), "looks": len(looks),
        "_image_jpeg_b64": images[-1] if images else None,
    }
    # The wide view is only good to ~10 m: measure each bordering side again in a close-up (~0.23 m/px).
    page = _page(result, page_number)
    close = _refine_close(result, page, feats, me, mn)
    if close.get("refined"):
        return {**common, "settled": True, "method": "close-ups of the bordering roads/canals",
                "best_total_move": {"east_m": round(close["east_m"], 1), "north_m": round(close["north_m"], 1)},
                "uncertainty_m": max(1.0, close["residual_m"]), "pinned_axes": close["pinned"],
                "hint": "propose best_total_move"}
    # The close-ups did not settle (the features may touch only part of a side): the agreeing wide looks stand,
    # with their wider uncertainty.
    return {**common, "settled": True, "method": "wide satellite view (close-ups did not settle)",
            "best_total_move": {"east_m": round(me, 1), "north_m": round(mn, 1)},
            "uncertainty_m": round(max(spread, _SETTLED_M), 1),
            "hint": "propose best_total_move, saying it is accurate to about the uncertainty"}


def get_parcel_details(result: dict, page_number: int, label: str) -> dict:
    """One confirmed parcel: its outline on the map (size, extent), printed vs computed area, calibration
    evidence and the pipeline's notes."""

    page = _page(result, page_number)
    parcel = next((p for p in _confirmed(page) if (_label(p) or "").strip().lower() == label.strip().lower()), None)
    if parcel is None:
        raise ValueError(f"no confirmed parcel {label!r} on page {page_number}; have {[_label(p) for p in _confirmed(page)]}")
    ring = _ring_of(parcel)
    lat0 = _centroid(ring)[1]
    xs = [p[0] * _kx(lat0) for p in ring]
    ys = [p[1] * _M_PER_DEG_LAT for p in ring]
    cal = parcel.get("calibration") or {}
    sv = parcel.get("spatial_validation") or {}
    entity = next((e for e in (page.get("sheet") or {}).get("parcels", []) if e.get("id") == parcel.get("roster_id")), {})
    return {
        "label": _label(parcel), "page": page_number,
        "corners": len(ring) - 1 if ring[0] == ring[-1] else len(ring),
        "extent_m": [round(max(xs) - min(xs), 1), round(max(ys) - min(ys), 1)],
        "area_acres_on_map": _area_acres(ring),
        "stated_area_as_printed": entity.get("stated_area"), "stated_area_corrected_by_user": entity.get("stated_area_edited"),
        "area_diff_pct": sv.get("area_diff_pct"), "perimeter_ft": sv.get("perimeter_ft"),
        "calibration": {k: cal.get(k) for k in (
            "status", "scale_ft_per_px", "scale_from_area", "scale_from_edges", "scale_agreement_pct",
            "corroborating_edge_count", "rotation_deg", "rotation_ambiguous_candidates_deg")},
        "calibration_notes": cal.get("notes", [])[:6],
        "placement_notes": (parcel.get("placement") or {}).get("notes", [])[:6],
        "common_frame": (parcel.get("placement") or {}).get("common_frame"),
    }


def search_document_text(result: dict, pattern: str, page_number: int | None = None) -> dict:
    """Case-insensitive search of the document's OCR text (a plain phrase, or a regular expression), with
    context around each hit. OCR is noisy: O/0, I/1 and dropped letters are common, so search short stems."""

    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error:
        rx = re.compile(re.escape(pattern), re.IGNORECASE)
    hits = []
    for page in result.get("pages", []):
        if page_number is not None and page["page_number"] != page_number:
            continue
        for idx, region in enumerate(page.get("regions", [])):
            text = region.get("ocr_text") or ""
            for m in rx.finditer(text):
                s, e = max(0, m.start() - 120), min(len(text), m.end() + 120)
                hits.append({"page": page["page_number"], "region": idx, "class": region.get("class"),
                             "context": re.sub(r"\s+", " ", text[s:e])})
                if len(hits) >= _MAX_TEXT_HITS:
                    return {"pattern": pattern, "hits": hits, "truncated": True}
    return {"pattern": pattern, "hits": hits, "truncated": False}


# ---------------------------------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------------------------------


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_PAGE = {"type": "integer", "description": "page number of the sheet"}

TOOLS: dict[str, tuple[Callable[..., dict], str, dict]] = {
    "get_document_overview": (
        get_document_overview,
        "Anchor and every confirmed parcel's placement state (calibration, county fit, notes). Start here.",
        _schema({}, []),
    ),
    "get_location_evidence": (
        get_location_evidence,
        "Printed location evidence of a sheet (APNs with sides, coordinates, coordinate system, ground factor, "
        "PLSS) as the pipeline uses it, and what the OCR check dropped.",
        _schema({"page_number": _PAGE}, ["page_number"]),
    ),
    "convert_state_plane": (
        convert_state_plane,
        "Convert a printed state-plane northing/easting (ft) to lat/lon in each zone of a state, optionally "
        "multiplying by a ground-to-grid factor first; shows the offset from the anchor and the parcels.",
        _schema({
            "northing": {"type": "number"}, "easting": {"type": "number"},
            "state": {"type": "string", "description": "e.g. Nevada"},
            "ground_to_grid": {"type": ["number", "null"], "description": "1/combined factor for ground coordinates"},
        }, ["northing", "easting", "state"]),
    ),
    "county_parcels_near": (
        county_parcels_near,
        "County parcels within a radius (m, max 400) of a lat/lon: APN, owner, address, area, offset.",
        _schema({
            "lat": {"type": "number"}, "lon": {"type": "number"},
            "radius_m": {"type": "number", "description": "default 150"},
        }, ["lat", "lon"]),
    ),
    "lookup_county_apns": (
        lookup_county_apns,
        "Where the county's current records put the APNs a sheet prints (or given APNs), with the plat's side "
        "for each and the offset from the parcels; reveals renumbered or scattered APNs.",
        _schema({"page_number": _PAGE, "apns": {"type": "array", "items": {"type": "string"}}}, ["page_number"]),
    ),
    "evaluate_position": (
        evaluate_position,
        "Score a hypothetical move (east_m, north_m) and clockwise turn of a sheet's confirmed parcels against "
        "the county neighbour parcels: overlap %, hugging %, mean gap.",
        _schema({
            "page_number": _PAGE, "east_m": {"type": "number"}, "north_m": {"type": "number"},
            "rotation_deg": {"type": "number"},
        }, ["page_number"]),
    ),
    "search_position_near": (
        search_position_near,
        "Run the county-parcel fit starting from a rough move (east_m, north_m) of a sheet's parcels; searches "
        "250 m around it and returns the best TOTAL move, its fit quality and whether it is corroborated. Use it "
        "to find the exact spot once you know roughly where the parcels belong.",
        _schema({"page_number": _PAGE, "east_m": {"type": "number"}, "north_m": {"type": "number"}}, ["page_number"]),
    ),
    "look_at_imagery": (
        look_at_imagery,
        "LOOK at the satellite imagery with the sheet's outlines drawn on it (optionally moved by east_m/north_m): "
        "a vision model says whether the boundaries follow visible roads, fences and field edges, and suggests the "
        "move that would line them up. Works anywhere, with no county records needed. Pass the user's own "
        "description of the problem as user_note. Look again at the suggested total move to confirm it.",
        _schema({
            "page_number": _PAGE, "east_m": {"type": "number"}, "north_m": {"type": "number"},
            "user_note": {"type": ["string", "null"]},
            "max_move_m": {"type": "number", "description": "how far from the start the parcels may be (default 200 m); "
                           "widen only when evidence says the sheet is far off"},
        }, ["page_number"]),
    ),
    "find_position_from_imagery": (
        find_position_from_imagery,
        "PLACE a sheet from the satellite imagery: repeatedly looks at the plat and the imagery, moves the "
        "outlines onto the roads/canals the plat draws around them and confirms, until the readings settle. "
        "Returns the best TOTAL move and its uncertainty. Use this to fix a position; pass the user's words as "
        "user_note, and a rough starting move if you have one.",
        _schema({
            "page_number": _PAGE, "east_m": {"type": "number"}, "north_m": {"type": "number"},
            "user_note": {"type": ["string", "null"]},
            "max_move_m": {"type": "number", "description": "how far from the start the parcels may be (default 200 m); "
                           "widen only when evidence says the sheet is far off"},
        }, ["page_number"]),
    ),
    "get_parcel_details": (
        get_parcel_details,
        "One confirmed parcel: map extent and area, printed vs computed area, calibration evidence and notes.",
        _schema({"page_number": _PAGE, "label": {"type": "string"}}, ["page_number", "label"]),
    ),
    "search_document_text": (
        search_document_text,
        "Search the document's OCR text (phrase or regex) with context; OCR is noisy, search short stems.",
        _schema({"pattern": {"type": "string"}, "page_number": {"type": ["integer", "null"]}}, ["pattern"]),
    ),
}


def tool_specs() -> list[dict]:
    """The tools in the OpenAI / OpenRouter tool-calling format."""

    return [
        {"type": "function", "function": {"name": name, "description": desc, "parameters": params}}
        for name, (_, desc, params) in TOOLS.items()
    ]


def run_tool(result: dict, name: str, args: dict[str, Any] | None) -> dict:
    """One tool call; unknown tools, bad arguments and failures come back as {"error": ...}."""

    entry = TOOLS.get(name)
    if entry is None:
        return {"error": f"unknown tool {name!r}; available: {sorted(TOOLS)}"}
    fn, _, schema = entry
    args = dict(args or {})
    for key, spec in schema.get("properties", {}).items():  # models often send numbers as strings
        types = spec.get("type") if isinstance(spec.get("type"), list) else [spec.get("type")]
        value = args.get(key)
        if isinstance(value, str) and ("number" in types or "integer" in types):
            try:
                args[key] = int(value) if "integer" in types and "number" not in types else float(value.replace(",", ""))
            except ValueError:
                return {"error": f"{key} must be a number, got {value!r}"}
    try:
        return fn(result, **args)
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    except (ValueError, KeyError) as exc:
        return {"error": str(exc)}
    except httpx.HTTPError as exc:
        return {"error": f"county parcel service unavailable: {exc}"}
    except Exception as exc:  # noqa: BLE001 -- a failed look (e.g. the vision model overloaded) must not end the review
        return {"error": f"{name} failed: {type(exc).__name__}: {str(exc)[:200]}"}
