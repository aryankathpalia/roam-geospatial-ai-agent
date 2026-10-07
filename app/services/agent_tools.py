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
