"""
Calibrated reprojection for a human-confirmed parcel boundary.

Replaces the naive "invert whatever arbitrary transform seeded the
shape on screen" reprojection with one that tries to independently
verify scale and rotation against the document's own printed evidence
(stated acreage, printed bearing/distance calls near the confirmed
edges), using the SAME association method validated in the
predict-and-verify diagnostic (proximity + parallel-orientation
matching, OCR text via the real parcelmap OCR pass).

Why this exists: the old approach reprojects every confirmed vertex --
whether the user dragged it or not -- through the scale/rotation the
ORIGINAL vision-extracted geometry happened to imply. When vision's
original read was already good (confirmed on NVZ, which was already
`valid: true` before any human correction), that's harmless. When
vision's original read was bad (confirmed on MAP 7 LOT 48, seed
described as not resembling the boundary at all), the on-screen shape
can look pixel-perfect after dragging while still reprojecting to the
wrong real-world scale/rotation, silently, because nothing re-derives
calibration from what the user actually validated.

This module never silently picks a value it can't corroborate. See
CalibrationResult.status.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

from app.services.geometry import parse_bearing, parse_distance, _BEARING_RE
from app.services.ocr import OCRLine

_EDGE_ASSOC_MAX_DIST_PX = 110
_EDGE_ASSOC_MAX_ORIENT_DIFF_DEG = 15
_EDGE_MATCH_TOLERANCE_PCT = 3.0
_SCALE_AGREEMENT_TOLERANCE_PCT = 5.0
_ROTATION_AGREEMENT_TOLERANCE_DEG = 5.0
# Loose sanity prefilter used only to pick, among several candidates
# proximity-matched to the same edge, which one is even plausibly that
# edge's own dimension -- not itself a pass/fail tolerance (that's
# _EDGE_MATCH_TOLERANCE_PCT / _SCALE_AGREEMENT_TOLERANCE_PCT below).
_EDGE_PREFILTER_TOLERANCE_PCT = 20.0


@dataclass
class CalibrationResult:
    status: str  # "cross_validated" | "single_source" | "unverified"
    scale_ft_per_px: float | None
    rotation_deg: float | None  # degrees to ADD to a pixel-space edge angle to get its real azimuth
    scale_from_area: float | None
    scale_from_edges: float | None
    scale_agreement_pct: float | None
    corroborating_edge_count: int
    notes: list[str] = field(default_factory=list)


def _polygon_area(poly: list[tuple[float, float]]) -> float:
    n = len(poly)
    s = 0.0
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _edge_geom(poly: list[tuple[float, float]], i: int) -> tuple[float, float]:
    n = len(poly)
    ax, ay = poly[i]
    bx, by = poly[(i + 1) % n]
    length_px = math.hypot(bx - ax, by - ay)
    angle_deg = math.degrees(math.atan2(bx - ax, -(by - ay))) % 360
    return length_px, angle_deg


def _point_segment_distance(p, a, b) -> float:
    p, a, b = np.array(p, float), np.array(a, float), np.array(b, float)
    ab = b - a
    t = np.dot(p - a, ab) / (np.dot(ab, ab) + 1e-9)
    t = max(0.0, min(1.0, t))
    proj = a + t * ab
    return float(np.linalg.norm(p - proj))


def _nearest_edge(center, poly: list[tuple[float, float]]) -> tuple[int, float]:
    best_idx, best_dist = -1, float("inf")
    for i in range(len(poly)):
        d = _point_segment_distance(center, poly[i], poly[(i + 1) % len(poly)])
        if d < best_dist:
            best_dist, best_idx = d, i
    return best_idx, best_dist


def _circular_diff(a: float, b: float, period: float = 360) -> float:
    d = abs(a - b) % period
    return min(d, period - d)


def _text_orientation_diff(bbox, edge_angle_deg: float) -> float:
    x1, y1, x2, y2 = bbox
    w, h = abs(x2 - x1), abs(y2 - y1)
    presumed = 0.0 if w >= h else 90.0
    return _circular_diff(presumed, edge_angle_deg % 90, period=90)


def _collect_candidate_numbers(lines: list[OCRLine]) -> list[dict]:
    """
    Every printed number on the page that could plausibly be a boundary
    call -- a full bearing+distance line, or a standalone distance
    label. Same strict-parser criteria as the predict-and-verify
    diagnostic: parse_bearing's structured regex for bearings (low
    false-positive rate), and "the whole OCR line is nothing but a
    number" for standalone distances (rules out numbers embedded in
    prose/notes).
    """

    out = []
    for line in lines:
        text = line.text.strip()
        az = parse_bearing(text)
        if az is not None:
            m = _BEARING_RE.search(text)
            remainder = text[: m.start()] + text[m.end():] if m else text
            dist = parse_distance(remainder)
            out.append({"line": line, "value": dist, "azimuth": az})
            continue
        bare = text.rstrip("'′\"")
        try:
            float(bare)
        except ValueError:
            continue
        dist = parse_distance(text)
        if dist is not None:
            out.append({"line": line, "value": dist, "azimuth": None})
    return out


def reproject_page_px_to_local(
    polygon_page_px: list[tuple[float, float]],
    scale_ft_per_px: float,
    rotation_deg: float,
    page_pivot: tuple[float, float],
    local_pivot: tuple[float, float],
) -> list[tuple[float, float]]:
    """
    Maps the page-pixel polygon into local feet using the given
    (scale, rotation), pivoting around (page_pivot -> local_pivot) --
    the EXACT SAME pivot the original (uncalibrated) seed transform
    used: the crop's own center in pixels, and the original
    vision-extracted ring's bounding-box center in local feet.

    This must NOT be the shape's own centroid. The original transform
    was `local = ring_bbox_center + (pixel - crop_center) / old_scale`
    (see boundary-review's computeLocalTransform) -- an affine pivoted
    at ring_bbox_center, an essentially ARBITRARY point relative to the
    anchor (local (0,0)), not at the shape's centroid and not at the
    anchor itself. Re-pivoting around the shape's own centroid instead
    introduces a spurious translation (confirmed empirically: ~150-300ft
    position error on NVZ despite correct scale and near-zero rotation)
    because the centroid generally sits nowhere near ring_bbox_center.
    Reusing the SAME pivot the proven-correct original transform used
    is what keeps translation "exactly as approximate as it already
    was" -- the actual design intent -- instead of silently shifting it.
    """

    page_pivot_x, page_pivot_y = page_pivot
    local_pivot_x, local_pivot_y = local_pivot

    points = []
    for px, py in polygon_page_px:
        dx_px, dy_px = px - page_pivot_x, py - page_pivot_y
        pixel_len = math.hypot(dx_px, dy_px)
        pixel_az = math.degrees(math.atan2(dx_px, -dy_px)) % 360
        real_az = (pixel_az + rotation_deg) % 360
        real_len_ft = pixel_len * scale_ft_per_px
        dx_ft = real_len_ft * math.sin(math.radians(real_az))
        dy_ft = real_len_ft * math.cos(math.radians(real_az))
        points.append((local_pivot_x + dx_ft, local_pivot_y + dy_ft))
    return points


def _sum_sq_error(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> float:
    return sum((ax - bx) ** 2 + (ay - by) ** 2 for (ax, ay), (bx, by) in zip(a, b))


def calibrate(
    polygon_page_px: list[tuple[float, float]],
    ocr_lines: list[OCRLine],
    stated_sqft: float | None,
    old_local_points: list[tuple[float, float]] | None = None,
    page_pivot: tuple[float, float] | None = None,
    local_pivot: tuple[float, float] | None = None,
) -> CalibrationResult:
    notes: list[str] = []
    n_edges = len(polygon_page_px)

    if not stated_sqft:
        return CalibrationResult(
            status="unverified", scale_ft_per_px=None, rotation_deg=None,
            scale_from_area=None, scale_from_edges=None, scale_agreement_pct=None,
            corroborating_edge_count=0,
            notes=["no stated acreage available for this parcel -- cannot compute an area-based scale at all"],
        )

    pixel_area = _polygon_area(polygon_page_px)
    scale_from_area = math.sqrt(stated_sqft / pixel_area) if pixel_area else None

    candidates = _collect_candidate_numbers(ocr_lines)

    # Associate by proximity + parallel-orientation ONLY (no magnitude
    # check yet) -- this is what makes scale_from_edges an INDEPENDENT
    # estimate rather than one that's circularly guaranteed to agree
    # with scale_from_area.
    by_edge: dict[int, list[dict]] = {}
    for cand in candidates:
        if not cand["value"]:
            continue
        x1, y1, x2, y2 = cand["line"].bbox
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        edge_idx, dist_px = _nearest_edge((cx, cy), polygon_page_px)
        if dist_px > _EDGE_ASSOC_MAX_DIST_PX:
            continue
        length_px, angle_px = _edge_geom(polygon_page_px, edge_idx)
        if _text_orientation_diff(cand["line"].bbox, angle_px) > _EDGE_ASSOC_MAX_ORIENT_DIFF_DEG:
            continue
        by_edge.setdefault(edge_idx, []).append({**cand, "dist_px": dist_px, "length_px": length_px, "angle_px": angle_px})

    # Independent per-edge scale estimate. Real plats routinely have
    # MORE THAN ONE printed number proximity-matched to the same edge --
    # a phantom combined-tract distance sharing the same physical line
    # (confirmed on NVZ: edge3's true 550.75' sits right next to the
    # combined tract's 903.15'), or a parenthetical prior-deed reference
    # bearing (confirmed on NVZ edge0: "(N0*02'31"W 250.00") (R4)" sits
    # right next to the true 220.00'). Requiring "exactly one candidate"
    # on an edge to trust it means every edge with a co-located
    # phantom/reference number gets silently excluded, and only truly
    # unrelated stray numbers (legend figures, tax IDs) that happen to
    # be sole occupants of an edge get through -- backwards from what's
    # actually trustworthy.
    #
    # Instead: use scale_from_area as a loose (20%) sanity filter among
    # an edge's candidates to reject numbers that clearly aren't that
    # edge's own dimension (10x-off legend figures, etc.), then take
    # WHICHEVER remaining candidate best matches the area-predicted
    # length for that edge. This is still an independent check (a
    # phantom/reference value is rejected because it disagrees with the
    # true edge's own pixel length, not because of who reported it) --
    # it just resolves multi-candidate edges by best fit instead of
    # discarding them.
    best_per_edge: dict[int, dict] = {}
    if scale_from_area:
        for edge_idx, cands in by_edge.items():
            length_px, _ = _edge_geom(polygon_page_px, edge_idx)
            predicted_ft = length_px * scale_from_area
            best, best_err = None, None
            for c in cands:
                if not predicted_ft:
                    continue
                pct_err = abs(c["value"] - predicted_ft) / predicted_ft * 100
                if pct_err <= _EDGE_PREFILTER_TOLERANCE_PCT and (best_err is None or pct_err < best_err):
                    best, best_err = c, pct_err
            if best is not None:
                best_per_edge[edge_idx] = best

    implied_scales = [c["value"] / c["length_px"] for c in best_per_edge.values() if c["length_px"]]

    scale_from_edges = None
    if implied_scales:
        scale_from_edges = float(np.median(implied_scales))

    scale_agreement_pct = None
    if scale_from_area and scale_from_edges:
        scale_agreement_pct = abs(scale_from_area - scale_from_edges) / scale_from_area * 100

    if scale_from_edges is not None and scale_agreement_pct is not None and scale_agreement_pct > _SCALE_AGREEMENT_TOLERANCE_PCT:
        notes.append(
            f"area-based scale ({scale_from_area:.4f} ft/px) and edge-corroborated scale "
            f"({scale_from_edges:.4f} ft/px) disagree by {scale_agreement_pct:.1f}% (>{_SCALE_AGREEMENT_TOLERANCE_PCT}% tolerance) "
            "-- not picking one silently."
        )
        return CalibrationResult(
            status="unverified", scale_ft_per_px=None, rotation_deg=None,
            scale_from_area=scale_from_area, scale_from_edges=scale_from_edges,
            scale_agreement_pct=scale_agreement_pct, corroborating_edge_count=len(best_per_edge),
            notes=notes,
        )

    # Scale accepted (either corroborated within tolerance, or no
    # independent edge estimate exists at all -- area-based scale used
    # alone, which is why it can only reach "single_source" below, not
    # "cross_validated").
    chosen_scale = scale_from_area

    # Now find edges that corroborate at the CHOSEN scale within the
    # tighter 3% tolerance (the actual predict-and-verify-style check),
    # and among THOSE, the ones with a printed bearing solve rotation.
    corroborating_edges = []
    rotation_candidates = []
    for edge_idx, cands in by_edge.items():
        length_px, angle_px = _edge_geom(polygon_page_px, edge_idx)
        predicted_ft = length_px * chosen_scale
        for c in cands:
            pct_err = abs(c["value"] - predicted_ft) / predicted_ft * 100 if predicted_ft else 999
            if pct_err <= _EDGE_MATCH_TOLERANCE_PCT:
                corroborating_edges.append(edge_idx)
                if c["azimuth"] is not None:
                    rotation_candidates.append((c["azimuth"] - angle_px) % 360)

    corroborating_edges = sorted(set(corroborating_edges))
    rotation_deg = None
    if rotation_candidates:
        # A printed bearing describes ONE specific walk direction along
        # a physical line -- but the confirmed polygon's edge (v_i ->
        # v_i+1) might traverse that same line in either direction, a
        # 180deg ambiguity with no way to resolve it from a single
        # matched edge alone. Fold every candidate mod 180 first so a
        # bearing and its reverse are treated as the SAME evidence for
        # the line's orientation (avoids reporting false disagreement
        # between two edges that are actually consistent, just matched
        # in opposite walk directions), find consensus on that folded
        # axis, then disambiguate the resulting 180deg-apart pair of
        # absolute candidates using old_local_points as an independent
        # reference below.
        folded = [r % 180 for r in rotation_candidates]
        vecs = [(math.cos(math.radians(2 * r)), math.sin(math.radians(2 * r))) for r in folded]
        mean_vec = (sum(v[0] for v in vecs) / len(vecs), sum(v[1] for v in vecs) / len(vecs))
        consensus_folded = (math.degrees(math.atan2(mean_vec[1], mean_vec[0])) / 2) % 180
        spread = max(_circular_diff(r, consensus_folded, period=180) for r in folded)
        if spread <= _ROTATION_AGREEMENT_TOLERANCE_DEG:
            candidate_a, candidate_b = consensus_folded, (consensus_folded + 180) % 360
            if (
                old_local_points and len(old_local_points) == len(polygon_page_px)
                and page_pivot is not None and local_pivot is not None
            ):
                proj_a = reproject_page_px_to_local(polygon_page_px, chosen_scale, candidate_a, page_pivot, local_pivot)
                proj_b = reproject_page_px_to_local(polygon_page_px, chosen_scale, candidate_b, page_pivot, local_pivot)
                err_a = _sum_sq_error(proj_a, old_local_points)
                err_b = _sum_sq_error(proj_b, old_local_points)
                rotation_deg = candidate_a if err_a <= err_b else candidate_b
                notes.append(
                    f"resolved 180deg direction ambiguity using the previous (uncalibrated) placement as a "
                    f"reference (candidate A err={err_a:.0f}, candidate B err={err_b:.0f})"
                )
            else:
                # No reference to disambiguate with -- can't safely pick
                # one of two 180deg-apart placements, since that choice
                # determines which side of the anchor the whole parcel
                # ends up on. Flag rather than guess.
                notes.append(
                    "rotation solved up to a 180deg direction ambiguity (a printed bearing can describe "
                    "either walk direction along a line) and no prior placement was available to "
                    "disambiguate -- not guessing."
                )
        else:
            notes.append(
                f"{len(rotation_candidates)} corroborating edges have bearings but disagree on line "
                f"orientation by up to {spread:.1f}deg (>{_ROTATION_AGREEMENT_TOLERANCE_DEG}deg) -- not averaging them."
            )

    if rotation_deg is None:
        notes.append("no corroborating edge has a printed bearing that agrees closely enough -- rotation unverified.")
        return CalibrationResult(
            status="unverified", scale_ft_per_px=chosen_scale, rotation_deg=None,
            scale_from_area=scale_from_area, scale_from_edges=scale_from_edges,
            scale_agreement_pct=scale_agreement_pct, corroborating_edge_count=len(corroborating_edges),
            notes=notes,
        )

    status = "cross_validated" if len(corroborating_edges) >= 2 else "single_source"
    notes.append(f"{len(corroborating_edges)} corroborating edge(s): {corroborating_edges}")
    return CalibrationResult(
        status=status, scale_ft_per_px=chosen_scale, rotation_deg=rotation_deg,
        scale_from_area=scale_from_area, scale_from_edges=scale_from_edges,
        scale_agreement_pct=scale_agreement_pct, corroborating_edge_count=len(corroborating_edges),
        notes=notes,
    )
