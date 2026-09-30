"""
Absolute placement of a human-confirmed, calibrated parcel polygon.

Calibration (calibration.py) verifies SCALE and ROTATION only. Where the
polygon sits on Earth is a separate question, and until this module the
answer was never verified: the document-level anchor is a single point
(the first printed state-plane pair, a geocode, ...) that was assumed to
be local (0, 0) of the parcel. Confirmed on NVZ page 9: that first pair
is a Washoe County section-corner CONTROL MONUMENT, not a parcel corner,
and the confirmed Parcel 1 landed ~2,600 ft from its own printed NE
corner while being drawn as placeable.

This module places a polygon ONLY from evidence that pins a specific
vertex to a specific printed coordinate in a stated coordinate system:

1. Identity -- every printed N/E pair is classified by the text printed
   around it. Monuments (control points, section/quarter corners, brass
   caps, benchmarks) are never bound to a parcel vertex; only coordinates
   the sheet marks as its own ("PER THIS MAP") are candidates.
2. Binding -- a candidate binds to the confirmed vertex its label sits
   next to, only when that vertex is unambiguously the nearest.
3. CRS -- zone/state, datum and ground-vs-grid (with the combined factor)
   must all be stated on the sheet. Anything missing -> not placed.

Anything short of that returns status "approximate" and the caller must
not present the polygon as correctly located.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from app.services.geometry import TraverseResult
from app.services.georeference import (
    _E_COORD_RE,
    _N_COORD_RE,
    _parse_survey_number,
    georeference_traverse_from_ground_corner,
)
from app.services.ocr import OCRLine

PLACEMENT_SURVEYED_CORNER = "surveyed_corner"
PLACEMENT_APPROXIMATE = "approximate"

# Text around a coordinate that marks it as a monument / control, never a
# parcel corner of THIS map.
_MONUMENT_RE = re.compile(
    r"CONTROL\s*POINT|BRASS\s*CAP|USGLO|\bBLM\b|SECTION\s*CORNER|1/4\s*CORNER|QUARTER\s*CORNER|"
    r"BENCH\s*MARK|\bB\.?M\.?\b|MONUMENT|TRIANGULATION|\bNGS\b|\bPID\b",
    re.I,
)
# Text that marks a coordinate as this map's own computed/set corner.
_OWN_CORNER_RE = re.compile(r"PER\s*THIS\s*MAP", re.I)
_ZONE_RE = re.compile(r"([A-Z][A-Z ]*?)\s+STATE\s+PLANE,?\s+([A-Z]+)\s+ZONE", re.I)
_FACTOR_RE = re.compile(r"(?:GRID\s*TO\s*GROUND|COMBINED)[A-Z ]*?FACTOR\s*(?:OF|=|:)?\s*(\d\.\d{4,})", re.I)
_GROUND_RE = re.compile(r"GROUND\s+(?:LEVEL|COORDINATES|DISTANCES)", re.I)

# Binding: a label must sit within this fraction of the polygon's shortest
# edge of its vertex, and the next-nearest vertex must be at least this
# many times farther away.
_BIND_MAX_FRAC_OF_SHORTEST_EDGE = 0.5
_BIND_MIN_SEPARATION_RATIO = 2.0
_CHECK_TOLERANCE_FRAC = 0.02


@dataclass
class CoordinatePair:
    northing: float
    easting: float
    label_center: tuple[float, float]  # page px, center of the N line
    context: str
    kind: str  # "monument" | "parcel_corner" | "unknown"


@dataclass
class PlacementResult:
    status: str
    geojson: dict | None = None
    notes: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


def _center(bbox) -> tuple[float, float]:
    x1, y1, x2, y2 = bbox
    return (x1 + x2) / 2, (y1 + y2) / 2


def extract_coordinate_pairs(lines: list[OCRLine]) -> list[CoordinatePair]:
    """Every printed N/E pair, paired by layout (E directly below N) and classified by nearby text."""

    def full(regex, text):
        m = regex.search(text)
        return m if m and not text[: m.start()].strip() and not text[m.end():].strip() else None

    n_lines = [(l, full(_N_COORD_RE, l.text.strip())) for l in lines]
    e_lines = [(l, full(_E_COORD_RE, l.text.strip())) for l in lines]
    e_lines = [(l, m) for l, m in e_lines if m]
    pairs = []
    for nl, nm in n_lines:
        if not nm:
            continue
        nx, ny = _center(nl.bbox)
        h = max(1.0, nl.bbox[3] - nl.bbox[1])
        below = [(l, m) for l, m in e_lines if 0 < _center(l.bbox)[1] - ny <= 2.5 * h
                 and abs(_center(l.bbox)[0] - nx) <= 8 * h]
        if not below:
            continue
        el, em = min(below, key=lambda lm: _center(lm[0].bbox)[1] - ny)
        northing, easting = _parse_survey_number(nm.group(1)), _parse_survey_number(em.group(1))
        if northing is None or easting is None:
            continue
        # Context: the text block stacked around the label (headers above,
        # a "PER THIS MAP" note beside/below), same column.
        ctx = [l.text for l in lines if l is not nl and l is not el
               and -6 * h <= _center(l.bbox)[1] - ny <= 5 * h and abs(_center(l.bbox)[0] - nx) <= 12 * h]
        context = " | ".join(ctx)
        kind = "monument" if _MONUMENT_RE.search(context) else "parcel_corner" if _OWN_CORNER_RE.search(context) else "unknown"
        pairs.append(CoordinatePair(northing, easting, (nx, ny), context, kind))
    # A "(RECORD)" value printed under a monument's "(MEASURED)" one sits
    # outside its header's window but is the same point: a pair within a
    # couple of feet of a monument is that monument.
    for p in pairs:
        if p.kind != "monument" and any(
            q.kind == "monument" and math.dist((p.easting, p.northing), (q.easting, q.northing)) <= 2.0 for q in pairs
        ):
            p.kind = "monument"
    return pairs


def resolve_crs(sheet_text: str) -> tuple[dict | None, str]:
    """Zone, datum and ground/grid from what the sheet itself states. (None, why) if any piece is missing."""

    from pyproj.database import query_crs_info

    zm = _ZONE_RE.search(sheet_text)
    if not zm:
        return None, "sheet does not state a state-plane zone"
    state = zm.group(1).strip().split()[-1].title()
    zone = zm.group(2).title()
    if not re.search(r"NAD\s*83", sheet_text, re.I):
        return None, "sheet does not state the NAD83 datum"
    name = f"NAD83 / {state} {zone} (ftUS)"
    codes = [c.code for c in query_crs_info(auth_name="EPSG", pj_types=None) if c.name == name]
    if len(codes) != 1:
        return None, f"no unique EPSG CRS named '{name}'"
    is_ground = bool(_GROUND_RE.search(sheet_text))
    fm = _FACTOR_RE.search(sheet_text)
    if is_ground and not fm:
        return None, "sheet says values are at ground level but states no grid-to-ground factor"
    factor = float(fm.group(1)) if (is_ground and fm) else 1.0
    return {
        "epsg": int(codes[0]), "crs_name": name, "grid_to_ground_factor": factor, "ground": is_ground,
        # Stated on the sheet: zone, NAD83, ground + factor. NOT stated, assumed:
        # US survey feet (the ftUS realization of the zone) and that ground
        # coordinates are grid coordinates scaled by the factor about the
        # CRS origin (divide by factor to get grid).
        "assumptions": ["units are US survey feet", "ground = grid x factor, scaled about the CRS origin",
                        "NAD83 realization differences (<~1 m) ignored"],
    }, ""


def bind_vertex(pair: CoordinatePair, polygon_page_px: list[tuple[float, float]]) -> tuple[int | None, str]:
    n = len(polygon_page_px)
    shortest = min(math.dist(polygon_page_px[i], polygon_page_px[(i + 1) % n]) for i in range(n))
    dists = sorted((math.dist(pair.label_center, v), i) for i, v in enumerate(polygon_page_px))
    (d0, i0), (d1, _) = dists[0], dists[1]
    if d0 > _BIND_MAX_FRAC_OF_SHORTEST_EDGE * shortest:
        return None, f"label is {d0:.0f}px from the nearest vertex (> {_BIND_MAX_FRAC_OF_SHORTEST_EDGE} x shortest edge {shortest:.0f}px)"
    if d1 < _BIND_MIN_SEPARATION_RATIO * d0:
        return None, f"label is not unambiguously nearest one vertex ({d0:.0f}px vs {d1:.0f}px)"
    return i0, ""


def place(
    polygon_page_px: list[tuple[float, float]],
    local_points_ft: list[tuple[float, float]],
    ocr_lines: list[OCRLine],
    sheet_text: str,
) -> PlacementResult:
    """
    `local_points_ft` is the calibrated polygon in ground feet (x east,
    y north), vertex-for-vertex with `polygon_page_px`; only its shape
    and orientation are used -- translation comes from the bound corner.
    """

    notes: list[str] = []
    pairs = extract_coordinate_pairs(ocr_lines)
    details = {"coordinate_pairs": [
        {"northing": p.northing, "easting": p.easting, "kind": p.kind, "label_center_px": p.label_center} for p in pairs
    ]}
    if not pairs:
        return PlacementResult(PLACEMENT_APPROXIMATE, notes=["no printed N/E coordinate pairs found near any text layout"], details=details)
    for p in pairs:
        if p.kind == "monument":
            notes.append(f"N {p.northing} E {p.easting} is a monument/control point -- never bound to a parcel vertex")

    crs, why = resolve_crs(sheet_text)
    if crs is None:
        return PlacementResult(PLACEMENT_APPROXIMATE, notes=notes + [f"coordinate system unresolved: {why}"], details=details)
    details["crs"] = crs

    bound = []
    for p in (q for q in pairs if q.kind == "parcel_corner"):
        idx, why = bind_vertex(p, polygon_page_px)
        if idx is None:
            notes.append(f"parcel-corner N {p.northing} E {p.easting} not bound: {why}")
        else:
            bound.append((idx, p))
    if not bound:
        return PlacementResult(PLACEMENT_APPROXIMATE, notes=notes + ["no parcel-corner coordinate could be bound to a confirmed vertex"], details=details)
    if len({i for i, _ in bound}) != len(bound):
        return PlacementResult(PLACEMENT_APPROXIMATE, notes=notes + ["two coordinates bind to the same vertex -- not guessing"], details=details)

    idx, p = bound[0]
    traverse = TraverseResult(points=list(local_points_ft) + [local_points_ft[0]], closure_error_ft=0.0, unparsed_calls=0)
    geo = georeference_traverse_from_ground_corner(
        traverse, idx, p.northing, p.easting, crs["epsg"], crs["grid_to_ground_factor"])

    # Every further bound corner is an independent check of the placement.
    ax, ay = local_points_ft[idx]
    residuals = []
    for j, q in bound[1:]:
        bx, by = local_points_ft[j]
        predicted = (p.easting + (bx - ax), p.northing + (by - ay))
        residual = math.dist(predicted, (q.easting, q.northing))
        # The confirmed shape is hand-placed (~2% edge error seen on NVZ),
        # so allow that much over the span between the two corners.
        allowed = max(3.0, _CHECK_TOLERANCE_FRAC * math.dist((ax, ay), (bx, by)))
        if residual > allowed:
            return PlacementResult(PLACEMENT_APPROXIMATE, notes=notes + [
                f"bound corners disagree: vertex {j} is {residual:.1f} ft from its printed coordinate "
                f"(> {allowed:.1f} ft) when vertex {idx} is placed on its own -- not placing"], details=details)
        residuals.append(round(residual, 2))
    details.update({"anchor_vertex": idx, "anchor_ground_ne_ft": [p.northing, p.easting],
                    "additional_bound_corners": len(bound) - 1, "check_residuals_ft": residuals})
    notes.append(
        f"vertex {idx} bound to printed parcel corner N {p.northing} E {p.easting} ({crs['crs_name']}, "
        f"ground/grid factor {crs['grid_to_ground_factor']})"
        + (f"; {len(residuals)} further bound corner(s), residuals {residuals} ft" if residuals else
           "; no second bound corner on this parcel to cross-check")
    )
    return PlacementResult(PLACEMENT_SURVEYED_CORNER, geojson=geo, notes=notes, details=details)
