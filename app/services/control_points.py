"""
Two-control-point placement: a fallback for when the existing trusted placement
(placement.py's vertex-bound printed parcel corner, or a corroborated fit)
could not place a sheet's confirmed parcels.

A plat that prints two surveyed state-plane coordinates (an iron pipe, a drill
hole, a monument...) pins position AND rotation with no assumption:

1. extract_control_points -- every printed N/E pair, with the physical point it
   labels and where the label sits on the page. If a label's N or E line was
   dropped by the first OCR pass, recover_control_points re-reads the whole page
   in 2x tiles; the original OCR evidence is never replaced, only added to.
2. solve_control_points -- finds which confirmed-polygon vertex is each control
   point: the pixel separation of two vertices, at the calibrated scale, must
   equal the ground separation of the two coordinates. That distance is
   symmetric (it cannot tell A->v0,B->v4 from A->v4,B->v0), so each control
   point's LABEL POSITION breaks the tie: the correct hypothesis has each label
   next to its own vertex. Rotation comes from the calibration's printed-bearing
   candidates when it has them, else from the control vector. The solution is
   then checked independently: anchored at A with that rotation, the predicted
   position of B must land within tolerance of B's printed coordinate.

Anything not unique, or whose residual is too large, is returned unplaced.
Nothing is ever guessed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from app.services.georeference import _E_COORD_RE, _N_COORD_RE
from app.services.placement import extract_coordinate_pairs

# Pixel separation of two vertices (at the calibrated scale) vs the ground
# separation of two control points.
DISTANCE_MATCH_TOL = 0.025
# Prediction of control point B after solving from A: max(RESIDUAL_FLOOR_FT, frac * |AB|).
RESIDUAL_TOL_FRAC = 0.02
RESIDUAL_FLOOR_FT = 3.0
# A printed-bearing rotation candidate must agree with the control vector this closely.
ROTATION_AGREE_DEG = 5.0
# Competing correspondences are separated by label proximity only if the best
# one's label-to-vertex distances are this many times smaller than the next's.
LABEL_SEPARATION_RATIO = 2.0

_POINT_RE = re.compile(
    r"IRON\s*PIPE|IRON\s*ROD|REBAR|DRILL\s*HOLE|MONUMENT|BRASS\s*CAP|ALUMINUM\s*CAP|"
    r"CONCRETE\s*BOUND|STONE\s*BOUND|\bBOUND\b|SPIKE|\bNAIL\b|\bPIN\b|\bPK\b|MAG\s*NAIL|BENCH\s*MARK",
    re.I,
)


@dataclass
class ControlPoint:
    northing: float
    easting: float
    description: str | None
    label_px: tuple[float, float] | None  # page px (true page frame), centre of the N line
    source: str  # "ocr" (first pass) | "ocr_recovered" (re-read tiles)
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "northing": self.northing, "easting": self.easting, "description": self.description,
            "label_px": list(self.label_px) if self.label_px else None, "source": self.source,
            "evidence": self.evidence,
        }

    @staticmethod
    def from_dict(d: dict) -> "ControlPoint":
        return ControlPoint(
            d["northing"], d["easting"], d.get("description"),
            tuple(d["label_px"]) if d.get("label_px") else None, d.get("source", "ocr"), d.get("evidence") or [],
        )


def _full(regex, text: str):
    m = regex.search(text)
    return m if m and not text[: m.start()].strip() and not text[m.end():].strip() else None


def labels_incomplete(lines, pairs) -> bool:
    """True when the OCR saw an N or E coordinate line that did not end up in a complete pair."""
    n = sum(1 for l in lines if _full(_N_COORD_RE, l.text.strip()))
    e = sum(1 for l in lines if _full(_E_COORD_RE, l.text.strip()))
    return n != len(pairs) or e != len(pairs)


def extract_control_points(lines, source: str = "ocr", positions_trusted: bool = True) -> list[ControlPoint]:
    # A coordinate label is horizontal text; a TALL box with coordinate text is a tile edge
    # misreading the same label sideways (seen on Derry: a duplicate of the drill-hole label
    # ~600 px from the real one), and would put the label at a false position.
    lines = [
        l for l in lines
        if not ((_full(_N_COORD_RE, l.text.strip()) or _full(_E_COORD_RE, l.text.strip()))
                and (l.bbox[3] - l.bbox[1]) > (l.bbox[2] - l.bbox[0]))
    ] if positions_trusted else lines
    out: list[ControlPoint] = []
    for p in extract_coordinate_pairs(lines):
        if any(math.hypot(p.northing - q.northing, p.easting - q.easting) <= 0.5 for q in out):
            continue  # same point read twice (tile overlap, duplicate reading)
        desc = _POINT_RE.search(p.context or "")
        out.append(ControlPoint(
            p.northing, p.easting, re.sub(r"\s+", " ", desc.group()).upper() if desc else None,
            p.label_center if positions_trusted else None, source,
            [f"N {p.northing} E {p.easting}", *([p.context[:160]] if p.context else [])],
        ))
    return out


def tile_origins(width: int, height: int, tile: int = 700, overlap: int = 100) -> list[tuple[int, int]]:
    step = tile - overlap
    return [(x, y) for y in range(0, height, step) for x in range(0, width, step)]


def tiles_near(origins, points, radius: float, tile: int = 700) -> list[tuple[int, int]]:
    """The tiles whose area comes within `radius` px of any of `points` (page px)."""
    return [
        (x, y) for x, y in origins
        if any(x - radius <= px <= x + tile + radius and y - radius <= py <= y + tile + radius for px, py in points)
    ]


def recover_control_points(page_image, ocr_fn, tile: int = 700, overlap: int = 100, upscale: int = 2, only_tiles=None):
    """
    Re-reads the page in overlapping tiles at `upscale`x -- small coordinate labels (where
    the first pass drops a line) read reliably at that size. `only_tiles` limits the pass to
    those tile origins (labels sit next to the corners they name, so the tiles around the
    confirmed polygon come first; the caller widens to the whole page only if that finds
    too little). Returns OCR lines with bboxes in TRUE page pixels (the first pass's frame
    can be rotated or offset on a page it orientation-corrected, so its positions are not
    used for binding). The first pass's lines are untouched by this.
    """

    from PIL import Image

    from app.services.ocr import OCRLine

    w, h = page_image.size
    lines = []
    for x, y in (only_tiles if only_tiles is not None else tile_origins(w, h, tile, overlap)):
        crop = page_image.crop((x, y, min(w, x + tile), min(h, y + tile)))
        if crop.width < 50 or crop.height < 50:
            continue
        big = crop.resize((crop.width * upscale, crop.height * upscale), Image.LANCZOS)
        tile_lines, _ = ocr_fn(big)
        for l in tile_lines:
            x1, y1, x2, y2 = l.bbox
            lines.append(OCRLine(l.text, l.confidence, (x1 / upscale + x, y1 / upscale + y, x2 / upscale + x, y2 / upscale + y)))
    return lines


def resolve_crs_from_anchor(points: list[ControlPoint], anchor_lat: float, anchor_lon: float, max_err_m: float = 5.0) -> int | None:
    """
    The NAD83 state-plane CRS under which one of the printed pairs converts onto the
    document's existing anchor (itself derived from a printed pair). Matching the
    anchor identifies the zone with no assumption about which state it is.
    """

    from pyproj import Geod, Transformer
    from pyproj.database import query_crs_info

    geod = Geod(ellps="WGS84")
    best: tuple[float, int] | None = None
    for info in query_crs_info(auth_name="EPSG", pj_types=None):
        if "NAD83" not in info.name or "ft" not in info.name or "/" not in info.name:
            continue
        bounds = getattr(info.area_of_use, "bounds", None)
        if bounds and not (bounds[0] - 0.5 <= anchor_lon <= bounds[2] + 0.5 and bounds[1] - 0.5 <= anchor_lat <= bounds[3] + 0.5):
            continue  # a zone that does not cover the anchor cannot be the one the sheet uses
        try:
            t = Transformer.from_crs(f"EPSG:{info.code}", "EPSG:4326", always_xy=True)
        except Exception:  # noqa: BLE001
            continue
        for p in points:
            try:
                lon, lat = t.transform(p.easting, p.northing)
            except Exception:  # noqa: BLE001
                continue
            if not (math.isfinite(lon) and math.isfinite(lat)):
                continue
            err = geod.inv(lon, lat, anchor_lon, anchor_lat)[2]
            if err <= max_err_m and (best is None or err < best[0]):
                best = (err, int(info.code))
    return best[1] if best else None


@dataclass
class ControlSolution:
    status: str  # "validated" | "ambiguous" | "unverified"
    reason: str
    # --- set when validated ---
    anchor_index: int | None = None        # which control point is the origin
    anchor_vertex_px: tuple[float, float] | None = None
    anchor_en: tuple[float, float] | None = None  # (easting, northing) ft
    rotation_deg: float | None = None
    scale_ft_per_px: float | None = None
    other_index: int | None = None
    residual_ft: float | None = None
    tolerance_ft: float | None = None
    separation_ft: float | None = None
    rotation_source: str | None = None     # "printed_bearings" | "control_vector"
    details: dict = field(default_factory=dict)


def _cdiff(a: float, b: float) -> float:
    d = abs((a - b) % 360)
    return min(d, 360 - d)


def _place(px, anchor_px, anchor_en, rotation_deg, scale):
    dx, dy = (px[0] - anchor_px[0]) * scale, -(px[1] - anchor_px[1]) * scale
    az = (math.degrees(math.atan2(dx, dy)) + rotation_deg) % 360
    d = math.hypot(dx, dy)
    return anchor_en[0] + d * math.sin(math.radians(az)), anchor_en[1] + d * math.cos(math.radians(az))


def solve_control_points(
    points: list[ControlPoint],
    vertices_px: list[tuple[float, float]],
    scale_ft_per_px: float | None,
    rotation_candidates_deg: list[float] | None = None,
    known_rotation_deg: float | None = None,
) -> ControlSolution:
    if len(points) < 2:
        return ControlSolution("unverified", f"{len(points)} complete control point(s) -- two are needed")
    if not scale_ft_per_px or len(vertices_px) < 3:
        return ControlSolution("unverified", "no calibrated scale to compare vertex separations against")
    cands = [known_rotation_deg] if known_rotation_deg is not None else list(rotation_candidates_deg or [])

    # (a, b, vi, vj, rot, residual, tol, rot_source)
    hyps = []
    for a, pa in enumerate(points):
        for b, pb in enumerate(points):
            if a == b:
                continue
            dE, dN = pb.easting - pa.easting, pb.northing - pa.northing
            D = math.hypot(dE, dN)
            if D <= 0:
                continue
            az_ab = math.degrees(math.atan2(dE, dN)) % 360
            tol = max(RESIDUAL_FLOOR_FT, RESIDUAL_TOL_FRAC * D)
            for i, vi in enumerate(vertices_px):
                for j, vj in enumerate(vertices_px):
                    if i == j:
                        continue
                    d_ft = math.hypot(vj[0] - vi[0], vj[1] - vi[1]) * scale_ft_per_px
                    if abs(d_ft - D) > DISTANCE_MATCH_TOL * D:
                        continue
                    pix_az = math.degrees(math.atan2(vj[0] - vi[0], -(vj[1] - vi[1]))) % 360
                    rot_c = (az_ab - pix_az) % 360
                    if cands:
                        near = min(cands, key=lambda c: _cdiff(c, rot_c))
                        if _cdiff(near, rot_c) > ROTATION_AGREE_DEG:
                            continue  # the printed bearings contradict this correspondence
                        rot, rsrc = near, "printed_bearings"
                    else:
                        rot, rsrc = rot_c, "control_vector"
                    pred = _place(vj, vi, (pa.easting, pa.northing), rot, scale_ft_per_px)
                    residual = math.hypot(pred[0] - pb.easting, pred[1] - pb.northing)
                    if residual <= tol:
                        hyps.append((a, b, i, j, rot, residual, tol, rsrc, D))
    if not hyps:
        return ControlSolution("unverified", "no pair of confirmed vertices matches the control points' separation and rotation within tolerance")

    # One drawn corner is often outlined twice (shared by adjacent lots, a few px apart):
    # hypotheses that agree on control assignment, rotation and vertex positions are one solution.
    tol_px = RESIDUAL_TOL_FRAC * hyps[0][8] / scale_ft_per_px
    def assignment(h):  # {control point index: vertex pixel position}
        return {h[0]: vertices_px[h[2]], h[1]: vertices_px[h[3]]}

    clusters: list[list] = []
    for h in hyps:
        ah = assignment(h)
        for c in clusters:
            ar = assignment(c[0])
            # The (A,B) and (B,A) orderings of one correspondence are a single solution.
            if ar.keys() == ah.keys() and _cdiff(c[0][4], h[4]) <= 3.0 and all(math.dist(ar[k], ah[k]) <= tol_px for k in ah):
                c.append(h)
                break
        else:
            clusters.append([h])

    chosen = None
    if len(clusters) == 1:
        chosen = clusters[0]
    else:
        scored = []
        for c in clusters:
            h = min(c, key=lambda t: t[5])
            la, lb = points[h[0]].label_px, points[h[1]].label_px
            if la is None or lb is None:
                scored = []
                break
            scored.append((math.dist(la, vertices_px[h[2]]) + math.dist(lb, vertices_px[h[3]]), c))
        if not scored:
            return ControlSolution(
                "ambiguous", f"{len(clusters)} different vertex correspondences fit the control points equally "
                "and the labels' page positions are unavailable to tell them apart")
        scored.sort(key=lambda t: t[0])
        if scored[1][0] < LABEL_SEPARATION_RATIO * max(scored[0][0], 1.0):
            return ControlSolution(
                "ambiguous", f"{len(clusters)} vertex correspondences fit and the labels' positions do not clearly "
                f"favour one ({scored[0][0]:.0f}px vs {scored[1][0]:.0f}px of label-to-vertex distance)")
        chosen = scored[0][1]

    a, b, i, j, rot, residual, tol, rsrc, D = min(chosen, key=lambda t: t[5])
    return ControlSolution(
        "validated", "unique correspondence; control point B predicted from A within tolerance",
        anchor_index=a, anchor_vertex_px=tuple(vertices_px[i]), anchor_en=(points[a].easting, points[a].northing),
        rotation_deg=rot, scale_ft_per_px=scale_ft_per_px, other_index=b, residual_ft=round(residual, 2),
        tolerance_ft=round(tol, 2), separation_ft=round(D, 2), rotation_source=rsrc,
        details={"candidate_correspondences": len(clusters), "anchor_vertex": i, "other_vertex": j,
                 "rotation_candidates_deg": cands},
    )


def place_vertices(sol: ControlSolution, vertices_px: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Grid (easting, northing) in feet for every vertex."""
    return [_place(v, sol.anchor_vertex_px, sol.anchor_en, sol.rotation_deg, sol.scale_ft_per_px) for v in vertices_px]
