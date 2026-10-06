"""
Assessor parcel numbers (APNs) printed on a document, looked up in the county's own public parcel layer.

A parcel map prints the APN of the land it divides AND of every neighbour ("A.P.N. 085-570-29, RENO
INVESTMENT GROUP"). Those are exact, independent evidence of WHERE the drawing sits: the county's parcel
polygons are on the ground already. This module only gathers that evidence (resolve_apn_site); fitting the
confirmed outlines between the neighbours is placement_by_apn below, a pure function over GeoJSON-like
coordinates so it is testable without the network.

Counties publish parcels through their own ArcGIS services with their own field names, so lookups go
through a small registry (_SERVICES). A county that is not registered simply yields nothing -- never a
guess. Results are kept only if they land near the document's coarse anchor, and only the cluster of
parcels that are near each other (an APN from a referenced deed elsewhere in the county is dropped).
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

# "A.P.N. 085-500-08", "APN: 085-570-29", "APN 085 500 08". Three groups of digits: 3-3-2 or 3-3-3 (and
# 3-2-3 / 3-4-2 as some counties write them). The label is required, so a bearing or a phone number never
# matches.
_APN_RE = re.compile(
    r"\bA\.?\s?P\.?\s?N\.?\s*(?:NO\.?|#|:|=)?\s*(\d{3})[-\s.](\d{2,4})[-\s.](\d{2,3})\b", re.IGNORECASE
)

_HTTP_TIMEOUT = 20.0
_MAX_LOOKUPS = 25
# A parcel the document names must lie this close to the coarse anchor to count at all ...
_NEAR_ANCHOR_M = 30_000.0
# ... and this close to the cluster's median, so a deed referenced from across the county is dropped.
_CLUSTER_M = 600.0


@dataclass
class _Service:
    name: str
    state: str  # full state name, as the geocoder reports it
    url: str  # ArcGIS layer /query endpoint
    field: str  # attribute holding the dashed APN
    owner_field: str | None = None
    address_field: str | None = None

    def where(self, apn: str) -> str:
        return f"{self.field}='{apn}'"


_SERVICES = [
    _Service(
        name="Washoe County, NV assessor parcels",
        state="Nevada",
        url="https://wcgisweb.washoecounty.us/arcgis/rest/services/OpenData/OpenData/FeatureServer/0/query",
        field="PIN",  # "085-570-29" (the APN field itself is numeric)
        owner_field="LASTNAME",
        address_field="FullAddress",
    ),
]


@dataclass
class ApnParcel:
    apn: str
    ring: list[tuple[float, float]]  # (lon, lat), WGS84
    owner: str | None = None
    address: str | None = None

    @property
    def centroid(self) -> tuple[float, float]:
        pts = self.ring[:-1] if len(self.ring) > 1 and self.ring[0] == self.ring[-1] else self.ring
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


@dataclass
class ApnSite:
    source: str
    target_apn: str | None  # the APN the document names most often (its own land)
    target: ApnParcel | None  # that parcel's county polygon, when it still exists
    neighbours: list[ApnParcel] = field(default_factory=list)

    def to_dict(self) -> dict:
        def p(x: ApnParcel) -> dict:
            return {"apn": x.apn, "owner": x.owner, "address": x.address, "ring": [list(pt) for pt in x.ring]}

        return {
            "source": self.source,
            "target_apn": self.target_apn,
            "target": p(self.target) if self.target else None,
            "neighbours": [p(n) for n in self.neighbours],
        }


def extract_apns(text: str) -> list[str]:
    """Every labelled APN in the text, normalised to dashes, most-mentioned first."""

    return [apn for apn, _ in _apn_counts(text)]


def _apn_counts(text: str) -> list[tuple[str, int]]:

    counts: dict[str, int] = {}
    order: list[str] = []
    for m in _APN_RE.finditer(text or ""):
        apn = "-".join(m.groups())
        if apn not in counts:
            order.append(apn)
        counts[apn] = counts.get(apn, 0) + 1
    return sorted(((a, counts[a]) for a in order), key=lambda ac: -ac[1])  # stable: ties keep document order


def target_apn(text: str) -> str | None:
    """The document's OWN parcel: the APN it names clearly more often than any other (application forms
    and title blocks repeat it). A plat that prints each APN once names only neighbours as far as text
    can tell, so there is no target -- never the first one by position."""

    counts = _apn_counts(text)
    if not counts or (len(counts) > 1 and counts[0][1] <= counts[1][1]):
        return None
    return counts[0][0]


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat = math.radians((a[1] + b[1]) / 2)
    return math.hypot((a[0] - b[0]) * 111_320 * math.cos(lat), (a[1] - b[1]) * 110_540)


def _lookup(client: httpx.Client, svc: _Service, apn: str) -> ApnParcel | None:
    fields = ",".join(f for f in (svc.field, svc.owner_field, svc.address_field) if f)
    r = client.get(svc.url, params={
        "where": svc.where(apn), "outFields": fields, "returnGeometry": "true", "outSR": "4326", "f": "json",
    }, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    feats = r.json().get("features") or []
    if len(feats) != 1:
        return None  # unknown (e.g. retired by this very split) or ambiguous
    rings = (feats[0].get("geometry") or {}).get("rings") or []
    if not rings:
        return None
    ring = max(rings, key=len)
    attrs = feats[0].get("attributes") or {}
    owner = attrs.get(svc.owner_field) if svc.owner_field else None
    address = attrs.get(svc.address_field) if svc.address_field else None
    return ApnParcel(
        apn=apn, ring=[(float(x), float(y)) for x, y in ring],
        owner=(owner or "").strip() or None, address=(address or "").strip() or None,
    )


def resolve_apn_site(text: str, state_name: str | None, near_lat: float, near_lon: float) -> ApnSite | None:
    """
    Looks up the document's APNs in the registered parcel layer(s) for its state. None when no service
    covers the state, no APN is printed, or nothing found lands near the coarse anchor.
    """

    apns = extract_apns(text)[:_MAX_LOOKUPS]
    services = [s for s in _SERVICES if state_name and s.state.lower() == state_name.strip().lower()]
    if not apns or not services:
        return None

    for svc in services:
        found: list[ApnParcel] = []
        try:
            with httpx.Client(headers={"User-Agent": "ROAM/1.0"}) as client:
                for apn in apns:
                    parcel = _lookup(client, svc, apn)
                    if parcel and _metres(parcel.centroid, (near_lon, near_lat)) <= _NEAR_ANCHOR_M:
                        found.append(parcel)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("APN lookup via %s failed: %s", svc.name, exc)
        if not found:
            continue
        lons = sorted(p.centroid[0] for p in found)
        lats = sorted(p.centroid[1] for p in found)
        median = (lons[len(lons) // 2], lats[len(lats) // 2])
        found = [p for p in found if _metres(p.centroid, median) <= _CLUSTER_M]
        own = target_apn(text)
        target = next((p for p in found if p.apn == own), None)
        return ApnSite(
            source=svc.name, target_apn=own, target=target,
            neighbours=[p for p in found if p is not target],
        )
    return None


# ---------------------------------------------------------------------------------------------------
# Fitting confirmed outlines to the county parcels (pure)
# ---------------------------------------------------------------------------------------------------

# A placement agrees with the county parcels when the outlines overlap the neighbours by less than this
# share of their own area AND the group's edges sit this close (on average) to a neighbour's edge.
_FIT_MAX_OVERLAP = 0.03
_HUG_M = 3.0  # an outline edge within this of a neighbour's edge "hugs" it
_FIT_MIN_HUGGING = 0.5  # at least half the group's outline must hug a neighbour (the rest may face roads)
_GAP_CAP_M = 10.0
# 10% overlap costs as much as 5 m of average gap: strong enough to keep outlines off neighbours, soft
# enough that a coarse-grid point a few metres from the true position still wins.
_OVERLAP_WEIGHT = 50.0
_COARSE_STEP_M = 5.0
_DISTINCT_M = 15.0  # a rival position at least this far from the best ...
_UNIQUE_MARGIN = 1.5  # ... must score this much worse, or the neighbours do not pin the group down
_SEARCH_M = 250.0
_TARGET_AREA_MIN, _TARGET_AREA_MAX = 0.8, 1.25
_TARGET_MIN_IOU = 0.85  # the confirmed outlines and the county polygon cover the same land


def _area_m2(rings: list) -> float:
    total = 0.0
    for ring in rings:
        lat0 = math.radians(sum(p[1] for p in ring) / len(ring))
        pts = [(x * 111_320 * math.cos(lat0), y * 110_540) for x, y in ring]
        total += abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))) / 2
    return total


def placement_by_apn(group_rings: list[list[tuple[float, float]]], site: dict) -> dict | None:
    """
    The translation (east_m, north_m) that best seats the confirmed outlines (WGS84 rings, already
    placed approximately) on the county parcels in `site` (ApnSite.to_dict()):

    - if the target parcel's own polygon exists: move the group's centre onto it;
    - otherwise: search for the position where the group overlaps no neighbour and its edges hug the
      neighbours' edges (adjoining parcels share their boundaries).

    Returns {"east_m", "north_m", "mode", "overlap", "gap_m", "corroborated"} or None when there is
    nothing to fit to. Shapes and their relative positions are never changed -- translation only.
    """

    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    neighbours = [n["ring"] for n in site.get("neighbours") or [] if len(n.get("ring") or []) >= 4]
    target = (site.get("target") or {}).get("ring")
    if not group_rings or (not neighbours and not target):
        return None
    if target and len(target) >= 4:
        # The target's polygon only stands for the confirmed outlines when it is the same land: a parcel
        # being split is the outlines' union, so the areas must agree. Otherwise it is one more neighbour.
        ratio = _area_m2([target]) and _area_m2(group_rings) / _area_m2([target])
        if not ratio or not (_TARGET_AREA_MIN <= ratio <= _TARGET_AREA_MAX):
            neighbours.append(target)
            target = None

    all_pts = [pt for ring in group_rings for pt in ring]
    lon0 = sum(p[0] for p in all_pts) / len(all_pts)
    lat0 = sum(p[1] for p in all_pts) / len(all_pts)
    kx = 111_320 * math.cos(math.radians(lat0))
    ky = 110_540

    def local(ring: list) -> Polygon:
        return Polygon([((x - lon0) * kx, (y - lat0) * ky) for x, y in ring]).buffer(0)

    group = unary_union([local(r) for r in group_rings])
    if group.is_empty or group.area <= 0:
        return None

    if target and len(target) >= 4:
        from shapely.affinity import rotate as _rot

        t = local(target)
        pivot_t = (group.centroid.x, group.centroid.y)
        # Centre the group on the target parcel at each candidate rotation (the calibrated one, the
        # 180-degree walk ambiguity, and the turns aligning their edge grids); keep the best overlap.
        def at(theta: float) -> tuple[float, float, float, float]:
            g = _rot(group, -theta, origin=pivot_t) if theta else group
            dx, dy = t.centroid.x - g.centroid.x, t.centroid.y - g.centroid.y
            moved = _translate(g, dx, dy)
            return moved.intersection(t).area / moved.union(t).area, theta, dx, dy

        # every 2 degrees, then refined -- overlap is cheap, and an irregular tract has no edge grid to align
        best_t = max((at(float(a)) for a in range(-180, 180, 2)), key=lambda r: r[0])
        for step in (0.5, 0.1):
            best_t = max((at(best_t[1] + k * step) for k in range(-4, 5)), key=lambda r: r[0])
        keep = at(0.0)  # the drawing's own calibrated orientation wins a near-tie
        if keep[0] >= best_t[0] - 0.005:
            best_t = keep
        iou, theta, dx, dy = best_t
        theta = _angle_diff(theta, 0.0)
        area_ratio = group.area / t.area if t.area else 0.0
        return {
            "east_m": dx, "north_m": dy, "rotation_deg": round(theta, 2),
            "pivot": [lon0 + pivot_t[0] / kx, lat0 + pivot_t[1] / ky],
            "mode": "target_parcel", "overlap": round(iou, 3), "gap_m": None, "area_ratio": round(area_ratio, 3),
            "corroborated": iou >= _TARGET_MIN_IOU and _TARGET_AREA_MIN <= area_ratio <= _TARGET_AREA_MAX,
        }

    import numpy as np
    import shapely
    from shapely.affinity import rotate

    nb = unary_union([local(r) for r in neighbours])
    boundary = nb.boundary
    shapely.prepare(boundary)
    pivot = (group.centroid.x, group.centroid.y)

    def search(geom):
        samples = np.array(_boundary_samples(geom, 2.0))

        def score(dx: float, dy: float) -> tuple[float, float, float]:
            moved = _translate(geom, dx, dy)
            overlap = moved.intersection(nb).area / geom.area
            d = shapely.distance(boundary, shapely.points(samples + (dx, dy)))
            # capped: an edge facing a road has no neighbour to hug, and must not drag the fit across it
            gap = float(np.minimum(d, _GAP_CAP_M).mean())
            return overlap * _OVERLAP_WEIGHT + gap, overlap, gap

        best = (math.inf, 0.0, 0.0, 0.0, 0.0)  # (score, dx, dy, overlap, gap)
        coarse: dict[tuple[float, float], float] = {}
        for step, radius, centre in ((_COARSE_STEP_M, _SEARCH_M, (0.0, 0.0)), (1.0, 6.0, None), (0.25, 1.0, None)):
            cx, cy = centre if centre is not None else (best[1], best[2])
            n = int(radius / step)
            for i in range(-n, n + 1):
                for j in range(-n, n + 1):
                    dx, dy = cx + i * step, cy + j * step
                    sc, ov, gap = score(dx, dy)
                    if centre is not None:
                        coarse[(dx, dy)] = sc
                    if sc < best[0]:
                        best = (sc, dx, dy, ov, gap)
        d = shapely.distance(boundary, shapely.points(samples + (best[1], best[2])))
        return best, coarse, float((d <= _HUG_M).mean())

    # Rotation: the placement's own (the drawing's calibrated orientation), plus the turns that line the
    # group's dominant edge direction up with the neighbours' -- a wrongly calibrated rotation (one misread
    # bearing) is corrected by the land around it, and the 180-degree walk-direction ambiguity too.
    rotations = _rotation_candidates(group, nb)
    results = []
    for theta in rotations:
        geom = rotate(group, -theta, origin=pivot) if theta else group  # shapely: +angle is counter-clockwise
        best, coarse, hugging = search(geom)
        results.append((theta, best, coarse, hugging))
    winner = min(results, key=lambda r: r[1][0])
    keep = results[0]  # rotation 0: the drawing's own calibrated orientation wins a near-tie
    if winner is not keep and keep[1][0] - winner[1][0] < _ROTATION_TIE:
        winner = keep
    theta, best, coarse, hugging = winner
    _, dx, dy, overlap, gap = best

    def placed(r):
        g = rotate(group, -r[0], origin=pivot) if r[0] else group
        return _translate(g, r[1][1], r[1][2])

    footprint = placed(winner)
    # The best rival -- another position, or another rotation that puts the outline somewhere else (a
    # symmetric group turned 180 deg lands on the same footprint, which is no rival): if it scores almost
    # as well, the neighbours do not pin the group down and the fit is not corroborated.
    rivals = [sc for (ddx, ddy), sc in coarse.items() if math.hypot(ddx - dx, ddy - dy) >= _DISTINCT_M]
    rivals += [
        r[1][0] for r in results
        if r is not winner and placed(r).symmetric_difference(footprint).area / group.area >= _DISTINCT_FOOTPRINT
    ]
    unique = min(rivals, default=math.inf) - best[0] >= _UNIQUE_MARGIN
    return {
        "east_m": dx, "north_m": dy, "rotation_deg": round(theta, 2),
        "pivot": [lon0 + pivot[0] / kx, lat0 + pivot[1] / ky],
        "mode": "between_neighbours", "overlap": round(overlap, 3),
        "gap_m": round(gap, 2), "hugging": round(hugging, 2), "unique": unique, "area_ratio": None,
        "corroborated": overlap <= _FIT_MAX_OVERLAP and hugging >= _FIT_MIN_HUGGING and unique and len(neighbours) >= 3,
    }


_ROTATION_TIE = 0.5
_DISTINCT_FOOTPRINT = 0.1


def _angle_diff(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


def _dominant_direction(geom) -> float | None:
    """Length-weighted dominant edge direction modulo 90 degrees (a rectilinear layout's grid), in degrees."""

    from shapely.geometry import MultiPolygon

    sx = sy = 0.0
    polys = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    for poly in polys:
        c = list(poly.exterior.coords)
        for (x0, y0), (x1, y1) in zip(c, c[1:]):
            length = math.hypot(x1 - x0, y1 - y0)
            ang = math.atan2(y1 - y0, x1 - x0) * 4  # x4 folds the four grid directions together
            sx += length * math.cos(ang)
            sy += length * math.sin(ang)
    if sx == 0 and sy == 0:
        return None
    return math.degrees(math.atan2(sy, sx)) / 4


def _rotation_candidates(group, neighbours) -> list[float]:
    """0 (keep the calibrated orientation), 180 (the walk-direction ambiguity) and the turns that align the
    group's dominant edge direction with the neighbours' (each of the four grid directions)."""

    out = [0.0, 180.0]
    dg, dn = _dominant_direction(group), _dominant_direction(neighbours)
    if dg is not None and dn is not None:
        # dominant directions are counter-clockwise from east; a clockwise turn of theta aligns them
        base = -_angle_diff(dn, dg) % 90.0
        for k in range(4):
            out.append(_angle_diff(base + 90.0 * k, 0.0))
    uniq: list[float] = []
    for t in out:
        if all(abs(_angle_diff(t, u)) >= 1.0 for u in uniq):
            uniq.append(t)
    return uniq


def _translate(geom, dx: float, dy: float):
    from shapely.affinity import translate

    return translate(geom, dx, dy)


def _pt(x: float, y: float):
    from shapely.geometry import Point

    return Point(x, y)


def _boundary_samples(geom, spacing_m: float) -> list[tuple[float, float]]:
    """Points every `spacing_m` along the OUTER boundary of the group (its shared inner lines excluded)."""

    from shapely.geometry import MultiPolygon

    polys = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    out: list[tuple[float, float]] = []
    for poly in polys:
        line = poly.exterior
        n = max(8, int(line.length / spacing_m))
        out += [(p.x, p.y) for p in (line.interpolate(k / n, normalized=True) for k in range(n))]
    return out
