"""
The final deliverable REPORT for a processed document: one structured model that the viewer shows, the
user edits, and every export (PDF, GeoJSON, Shapefile, KML, CSV, package ZIP) is rendered from.

What GIS / cadastral data clients expect from a parcel-digitisation deliverable (county parcel RFPs, state
parcel standards such as RIGIS and Vermont VCGI, NSSDA / ISO 19115 metadata practice):
- the parcel polygons themselves, in a stated coordinate system (a state-plane zone for US work) AND WGS84,
  as Shapefile / file geodatabase and an open format (GeoJSON / KML);
- an attribute table with the parcel identifier, the source document it came from, how it was created
  (automation method) and when -- per-feature lineage, not just a layer-level note;
- the record (deed / plat) COGO: each course's bearing and distance, curve data, closure / misclosure and
  precision ratio;
- QA/QC: the checks run, their results and what a reviewer changed;
- metadata: CRS / datum / units, positional-accuracy statement, lineage, processing steps;
- figures: a location map (where on the ground), the parcels over imagery, and the source sheet.

Field names in the attribute table follow those standards' conventions (ParcelID, SrcID, SrcType, SrcDate,
AutoMeth, UpdDate ...) so the layer drops into a client's parcel model with little remapping.
"""

from __future__ import annotations

import copy
import datetime as dt
from typing import Any

from pyproj import CRS, Transformer
from pyproj.database import query_crs_info
from shapely.geometry import Polygon

from app.services import parcel_roster

REPORT_VERSION = "1.0"

# How a confirmed outline's position was established, from strongest to weakest -- the label a client sees.
PLACEMENT_METHODS = {
    "manual": "Placed by reviewer",
    "control": "Fitted to printed survey control points",
    "apn": "Fitted to county parcel records (APN)",
    "aliquot": "Fitted to PLSS aliquot part (BLM)",
    "surveyed_corner": "Bound to a printed parcel-corner coordinate",
    "apn_approx": "Oriented by county parcel records (position approximate)",
    "anchor": "Document-level anchor only (approximate)",
}

ANCHOR_LABELS = {
    "surveyed": "Printed survey coordinates",
    "plss_single_source": "PLSS monument (uncorroborated)",
    "apn_parcel": "County parcel record (APN)",
    "street": "Street address (approximate)",
    "city": "City / county only (coarse)",
}


def _placement_method(parcel: dict) -> tuple[str, bool]:
    """(method key, location confirmed?) -- the same rules the workspace verdict uses."""

    pl = parcel.get("placement") or {}
    if parcel.get("anchor_override") or parcel.get("manual_position"):
        return "manual", True
    if (pl.get("control_fit") or {}).get("validated"):
        return "control", True
    if (pl.get("apn_fit") or {}).get("corroborated"):
        return "apn", True
    if (pl.get("aliquot_fit") or {}).get("corroborated"):
        return "aliquot", True
    if pl.get("status") == "surveyed_corner":
        return "surveyed_corner", True
    if pl.get("apn_fit"):
        return "apn_approx", False
    return "anchor", False


def _utm_crs(lon: float, lat: float) -> CRS:
    zone = int((lon + 180) // 6) + 1
    return CRS.from_epsg((32600 if lat >= 0 else 32700) + zone)


def _state_plane_crs(lon: float, lat: float, state: str | None) -> CRS | None:
    """The NAD83 state-plane zone (US survey feet when the state uses them) whose area of use contains the
    point; None outside the US or when no zone matches."""

    best = None
    for info in query_crs_info(auth_name="EPSG", pj_types=["PROJECTED_CRS"]):
        name = info.name
        if not name.startswith("NAD83 /") or not ("(ftUS)" in name or "(ft)" in name):
            continue
        if state and state.lower() not in name.lower():
            continue
        a = info.area_of_use
        if a and a.west <= lon <= a.east and a.south <= lat <= a.north:
            # the smallest matching area is the zone (a state-wide CRS also matches)
            size = (a.east - a.west) * (a.north - a.south)
            if best is None or size < best[0]:
                best = (size, info.code)
    return CRS.from_epsg(int(best[1])) if best else None


def _ring_area_perimeter(ring: list, crs: CRS) -> tuple[float, float]:
    t = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    pts = [t.transform(x, y) for x, y in ring]
    poly = Polygon(pts)
    unit = crs.axis_info[0].unit_conversion_factor if crs.axis_info else 1.0  # metres per CRS unit
    return poly.area * unit * unit / 0.09290304, poly.length * unit / 0.3048  # sq ft, ft


def _calls(parcel: dict) -> list[dict]:
    vg = parcel.get("vision_geometry") or {}
    out = []
    for i, c in enumerate(vg.get("boundary_calls") or [], 1):
        if isinstance(c, dict):
            out.append({"course": i, "type": "line", "bearing": c.get("bearing"), "distance": c.get("distance")})
    for c in vg.get("curve_calls") or []:
        if isinstance(c, dict):
            out.append({
                "course": len(out) + 1, "type": "curve", "bearing": c.get("chord_bearing"),
                "distance": c.get("arc_length") or c.get("chord_length"),
                "radius": c.get("radius"), "delta": c.get("delta") or c.get("central_angle"),
            })
    return out


def _entity_for(result: dict, parcel: dict) -> dict | None:
    rid = parcel.get("roster_id")
    for page in result.get("pages", []):
        for e in (page.get("sheet") or {}).get("parcels", []):
            if e.get("id") == rid:
                return e
    return None


def _qa(parcel: dict, confirmed_location: bool) -> list[dict]:
    sv = parcel.get("spatial_validation") or {}
    cal = parcel.get("calibration") or {}
    checks = [
        {"check": "Boundary confirmed by reviewer on the source drawing", "ok": bool(parcel.get("human_confirmed"))},
        {"check": "Outline closes and does not self-intersect", "ok": not sv.get("self_intersects", False)},
    ]
    if sv.get("stated_area_sqft"):
        checks.append({
            "check": f"Area matches the document's stated area (diff {sv.get('area_diff_pct', 0):.1f}%)",
            "ok": bool(sv.get("area_matches_stated")),
        })
    checks.append({
        "check": "Scale and rotation calibrated against printed dimensions"
        + (" (sheet scale from stated areas)" if cal.get("sheet_scale") else ""),
        "ok": cal.get("status") in ("cross_validated", "single_source") or bool(cal.get("sheet_scale")),
    })
    checks.append({"check": "Location independently corroborated", "ok": confirmed_location})
    return checks


def build_report(result: dict, document_id: str) -> dict:
    """The report model, with the user's saved edits (result["report_edits"]) applied."""

    anchor = result.get("anchor") or {}
    evidence = result.get("location_evidence") or {}
    site = result.get("apn_site") or {}
    inspection = result.get("inspection") or {}
    meta = inspection.get("metadata") or {}

    parcels: list[dict] = []
    all_pts: list[tuple[float, float]] = []
    for page in result.get("pages", []):
        for region in page.get("regions", []):
            for p in region.get("parcels") or []:
                geo = (p.get("boundary_geojson_wgs84") or {}).get("geometry") or {}
                ring = (geo.get("coordinates") or [[]])[0]
                if not p.get("human_confirmed") or len(ring) < 4:
                    continue
                all_pts += [tuple(pt) for pt in ring]
                parcels.append({"_parcel": p, "_page": page["page_number"], "_ring": ring})

    if parcels:
        lon0 = sum(p[0] for p in all_pts) / len(all_pts)
        lat0 = sum(p[1] for p in all_pts) / len(all_pts)
    else:
        lon0, lat0 = result.get("anchor_lon") or 0.0, result.get("anchor_lat") or 0.0
    utm = _utm_crs(lon0, lat0)
    sp = _state_plane_crs(lon0, lat0, (evidence.get("state") or "").title() or None)
    to_utm = Transformer.from_crs("EPSG:4326", utm, always_xy=True)
    to_sp = Transformer.from_crs("EPSG:4326", sp, always_xy=True) if sp else None

    owner_by_apn = {n.get("apn"): n for n in site.get("neighbours") or []}
    if site.get("target"):
        owner_by_apn[site["target"]["apn"]] = site["target"]

    out_parcels = []
    for i, item in enumerate(parcels, 1):
        p, ring = item["_parcel"], item["_ring"]
        entity = _entity_for(result, p) or {}
        sv = p.get("spatial_validation") or {}
        method, confirmed = _placement_method(p)
        area_sqft, perim_ft = _ring_area_perimeter(ring, utm)
        label = entity.get("label") or (p.get("vision_geometry") or {}).get("parcel_label") or f"Parcel {i}"
        # the drawing's own printed figure (verified) before one derived from a rounded acreage
        stated_sqft = entity.get("stated_area_sqft") or sv.get("stated_area_sqft")
        if not stated_sqft and entity.get("stated_area"):
            acres = parcel_roster.parse_acres(entity["stated_area"])
            stated_sqft = acres * 43560 if acres else None
        vertices = []
        for k, (lon, lat) in enumerate(ring[:-1], 1):
            e_u, n_u = to_utm.transform(lon, lat)
            v = {"point": k, "lat": round(lat, 8), "lon": round(lon, 8), "utm_e": round(e_u, 3), "utm_n": round(n_u, 3)}
            if to_sp:
                e_s, n_s = to_sp.transform(lon, lat)
                v.update({"sp_e": round(e_s, 3), "sp_n": round(n_s, 3)})
            vertices.append(v)
        closure = (p.get("boundary_geojson_wgs84") or {}).get("properties", {}).get("closure_error_ft")
        out_parcels.append({
            "id": entity.get("id") or p.get("roster_id") or f"P{i}",
            "label": label,
            "apn": entity.get("printed_id"),
            "parent_apn": site.get("target_apn") if site.get("target") else None,
            "parent_owner": (site.get("target") or {}).get("owner"),
            "page": item["_page"],
            "stated_area_sqft": round(stated_sqft, 1) if stated_sqft else None,
            "stated_area_text": entity.get("stated_area"),
            "area_sqft": round(area_sqft, 1),
            "area_acres": round(area_sqft / 43560, 4),
            "area_diff_pct": round(abs(area_sqft - stated_sqft) / stated_sqft * 100, 2) if stated_sqft else None,
            "perimeter_ft": round(perim_ft, 2),
            "closure_error_ft": closure,
            "precision_ratio": sv.get("precision_ratio"),
            "calls": _calls(p),
            "vertices": vertices,
            "ring": ring,
            "placement_method": method,
            "placement_label": PLACEMENT_METHODS[method],
            "location_confirmed": confirmed,
            "calibration": (p.get("calibration") or {}).get("status"),
            "placement_notes": list((p.get("placement") or {}).get("notes") or []),
            "qa": _qa(p, confirmed),
            "review_status": "pending",
            "notes": "",
        })

    report = {
        "version": REPORT_VERSION,
        "document_id": document_id,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "project": {
            "title": meta.get("title") or inspection.get("filename") or "Parcel georeferencing report",
            "client": "",
            "project_ref": "",
            "prepared_by": "ROAM",
            "source_document": meta.get("title") or inspection.get("filename"),
            "source_pages": inspection.get("page_count"),
            "source_type": "Parcel map / plat",
            "county": evidence.get("county"),
            "state": evidence.get("state"),
        },
        "location": {
            "anchor_method": ANCHOR_LABELS.get(anchor.get("precision"), anchor.get("precision")),
            "anchor_source": anchor.get("source"),
            "centre": {"lat": round(lat0, 7), "lon": round(lon0, 7)},
            "plss": evidence.get("plss"),
            "county_parcel_service": site.get("source"),
            "parent_apn": site.get("target_apn"),
            "neighbours": [
                {"apn": n.get("apn"), "owner": n.get("owner"), "address": n.get("address")}
                for n in site.get("neighbours") or []
            ],
        },
        "crs": {
            "geographic": "WGS 84 (EPSG:4326)",
            "utm": f"{utm.name} (EPSG:{utm.to_epsg()})",
            "state_plane": f"{sp.name} (EPSG:{sp.to_epsg()})" if sp else None,
            "units": "US survey feet (state plane), metres (UTM), square feet / acres (areas)",
        },
        "summary": {
            "parcel_count": len(out_parcels),
            "confirmed_locations": sum(1 for p in out_parcels if p["location_confirmed"]),
            "total_area_acres": round(sum(p["area_acres"] for p in out_parcels), 4),
            "total_stated_acres": round(sum((p["stated_area_sqft"] or 0) for p in out_parcels) / 43560, 4) or None,
        },
        "accuracy_statement": (
            "Parcel shapes are digitised from the source drawing and scaled/oriented from its printed "
            "dimensions; areas are compared with the stated areas. Positions marked 'confirmed' agree with "
            "independent data (county parcel records, printed survey control or BLM PLSS corners); others are "
            "approximate and should be field- or record-verified before use for boundary determination. "
            "This is a compiled record map, not a boundary survey."
        ),
        "lineage": [
            "Source PDF rendered per page; layout detection and OCR of text regions.",
            "Parcel-map sheets identified; parcel labels, stated areas and location evidence read by a vision model, "
            "each number verified against the OCR text.",
            "Reviewer confirmed each parcel outline on the source drawing.",
            "Scale and rotation calibrated from printed dimensions and stated areas.",
            "Placement from the document anchor, refined against county parcel records / printed control where available.",
        ],
        "parcels": out_parcels,
        "edit_log": [],
    }
    return apply_edits(report, result.get("report_edits") or {})


# Fields a reviewer may change: project header fields and per-parcel identity / review fields. Geometry
# is NOT edited here -- it is changed by re-confirming the outline, which re-runs the checks.
EDITABLE_PROJECT = {"title", "client", "project_ref", "prepared_by", "source_type", "county", "state"}
EDITABLE_PARCEL = {"label", "apn", "review_status", "notes"}
REVIEW_STATUSES = {"pending", "approved", "rejected", "needs_field_check"}


def apply_edits(report: dict, edits: dict) -> dict:
    report = copy.deepcopy(report)
    for k, v in (edits.get("project") or {}).items():
        if k in EDITABLE_PROJECT:
            report["project"][k] = v
    for p in report["parcels"]:
        for k, v in ((edits.get("parcels") or {}).get(p["id"]) or {}).items():
            if k in EDITABLE_PARCEL:
                p[k] = v
    report["edit_log"] = list(edits.get("log") or [])
    report["summary"]["approved"] = sum(1 for p in report["parcels"] if p["review_status"] == "approved")
    return report


def merge_edits(existing: dict, incoming: dict, editor: str | None = None) -> dict:
    """Validates and merges a viewer's edits into result["report_edits"], logging each change."""

    out = copy.deepcopy(existing or {})
    out.setdefault("project", {})
    out.setdefault("parcels", {})
    out.setdefault("log", [])
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for k, v in (incoming.get("project") or {}).items():
        if k in EDITABLE_PROJECT and isinstance(v, (str, type(None))) and out["project"].get(k) != v:
            out["project"][k] = v
            out["log"].append({"at": now, "by": editor, "field": f"project.{k}", "value": v})
    for pid, fields in (incoming.get("parcels") or {}).items():
        for k, v in (fields or {}).items():
            if k not in EDITABLE_PARCEL or not isinstance(v, (str, type(None))):
                continue
            if k == "review_status" and v not in REVIEW_STATUSES:
                continue
            slot = out["parcels"].setdefault(pid, {})
            if slot.get(k) != v:
                slot[k] = v
                out["log"].append({"at": now, "by": editor, "field": f"{pid}.{k}", "value": v})
    return out


def attribute_row(report: dict, p: dict) -> dict[str, Any]:
    """One parcel's attribute record, in parcel-standard field names (<= 10 chars for the shapefile)."""

    proj = report["project"]
    return {
        "ParcelID": p["id"],
        "Label": p["label"],
        "APN": p.get("apn") or "",
        "ParentAPN": p.get("parent_apn") or "",
        "County": proj.get("county") or "",
        "State": proj.get("state") or "",
        "StatedSF": p.get("stated_area_sqft"),
        "CalcSF": p["area_sqft"],
        "CalcAcres": p["area_acres"],
        "AreaDiffPc": p.get("area_diff_pct"),
        "PerimFt": p["perimeter_ft"],
        "ClosureFt": p.get("closure_error_ft"),
        "SrcID": (proj.get("source_document") or "")[:254],
        "SrcPage": p["page"],
        "SrcType": proj.get("source_type") or "",
        "AutoMeth": "ROAM digitised + reviewer confirmed",
        "PlaceMeth": p["placement_label"],
        "LocConf": "confirmed" if p["location_confirmed"] else "approximate",
        "Review": p.get("review_status") or "pending",
        "UpdDate": report["generated_at"][:10],
        "Notes": (p.get("notes") or "")[:254],
    }


def bbox(report: dict) -> tuple[float, float, float, float] | None:
    pts = [pt for p in report["parcels"] for pt in p["ring"]]
    if not pts:
        return None
    return min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)

