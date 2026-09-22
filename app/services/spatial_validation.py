"""
Validates a walked traverse (see geometry.py) beyond raw closure error,
using the same standard land-surveying signals a human reviewer would
check, plus one cross-check against the document's own OCR text:

1. Relative precision (perimeter / closure_error, expressed "1:N") --
   the actual metric surveyors use, not a flat closure_error_ft number.
   200ft of closure error means something very different on a 900ft
   parcel boundary than on a 90,000ft one; raw closure_error_ft alone
   (what geometry.py surfaces) can't tell those apart, but the ratio
   can. There's no single legal threshold (it varies by jurisdiction
   and survey class), so this flags a generic, conservative cutoff
   rather than claiming regulatory compliance.

2. Self-intersection -- a traverse that crosses its own boundary is
   geometrically invalid regardless of how well it closes (shapely,
   already a dependency, does the real polygon-validity check here
   rather than a hand-rolled segment-intersection test).

3. Stated-area cross-check -- survey documents often print the
   parcel's area directly (e.g. "51984 sf", confirmed present in a
   real test document's OCR text), independent of the boundary calls
   themselves. Comparing the WALKED polygon's shoelace-formula area
   against that stated figure is a real, free consistency check: if
   they disagree by a lot, either the extracted calls or the stated
   area is wrong -- worth surfacing either way, not guessing which.
"""

import re

from shapely.geometry import Polygon

from app.services.geometry import TraverseResult

_SQFT_PER_ACRE = 43_560.0

# Matches a number followed by an area unit, in either order relative
# to the number (documents vary): "51984 sf", "51,984 SF", "1.19
# acres", "1.19 AC". Comma-thousands-separators are stripped before
# parsing.
_AREA_RE = re.compile(
    r"(\d[\d,]*\.?\d*)\s*(sq\.?\s?ft\.?|sf|square\s+feet|acres?|ac\.?)\b",
    re.IGNORECASE,
)

# A generic, conservative cutoff -- real minimum-standard ratios vary
# by jurisdiction and survey class (rural boundary surveys and urban
# control surveys are held to very different standards), so this is
# deliberately loose: it flags traverses whose closure is bad enough
# that almost no real standard would accept them, not a claim of
# meeting any specific one.
_MIN_ACCEPTABLE_PRECISION_RATIO = 2_000

# How far the walked polygon's area is allowed to differ from a
# stated area on the document before flagging a mismatch. Loose on
# purpose: the stated figure might describe a slightly different
# boundary (e.g. including/excluding a road dedication) even when the
# traverse itself is correct.
_AREA_MISMATCH_TOLERANCE = 0.15


def compute_perimeter_ft(points: list[tuple[float, float]]) -> float:
    return sum(
        ((points[i + 1][0] - points[i][0]) ** 2 + (points[i + 1][1] - points[i][1]) ** 2)
        ** 0.5
        for i in range(len(points) - 1)
    )


def find_stated_area_sqft(text: str) -> float | None:
    """
    Scans OCR text for the first area figure (in acres or square feet,
    normalized to square feet). Returns None if nothing matches --
    callers must treat that as "no stated area to check against", not
    zero.
    """

    match = _AREA_RE.search(text)
    if not match:
        return None

    value = float(match.group(1).replace(",", ""))
    unit = match.group(2).lower()

    if unit.startswith("ac"):
        return value * _SQFT_PER_ACRE
    return value


def validate_traverse(traverse: TraverseResult, region_ocr_text: str = "") -> dict:
    """
    Returns a validation report for a walked traverse. `valid` is the
    overall verdict; `issues` lists the specific, human-readable
    reasons -- surfaced rather than collapsed into a single boolean,
    since a caller (or a future map UI) may want to show which check
    failed, not just that something did.
    """

    issues: list[str] = []
    points = traverse.points

    perimeter_ft = round(compute_perimeter_ft(points), 2)

    if traverse.closure_error_ft <= 0:
        precision_ratio: float | None = None  # perfect closure -- no ratio to report
    elif perimeter_ft <= 0:
        precision_ratio = None
    else:
        precision_ratio = round(perimeter_ft / traverse.closure_error_ft, 1)
        if precision_ratio < _MIN_ACCEPTABLE_PRECISION_RATIO:
            issues.append(
                f"Weak closure: precision ratio 1:{precision_ratio:.0f} is below the "
                f"1:{_MIN_ACCEPTABLE_PRECISION_RATIO} conservative minimum -- the "
                "extracted boundary calls are likely incomplete or include noise."
            )

    # walk_traverse's last point is where the traverse ENDS UP after
    # its final call, which -- for a properly closed N-sided
    # traverse -- lands back on (or very near) the start point. That
    # makes it a near-duplicate of points[0], not a distinct corner:
    # feeding it to shapely as-is produces a degenerate zero-length
    # closing segment that gets flagged as a false self-intersection
    # (confirmed on a real, genuinely valid rectangle -- shapely's
    # own explain_validity() named it "Ring Self-intersection" at
    # exactly that near-duplicate vertex). Dropping it and letting
    # shapely close the ring itself (corner N back to corner 0) is
    # correct regardless of how well the traverse actually closed.
    corners = points[:-1] if len(points) > 1 else points

    # A traverse needs at least 3 distinct corners to form a polygon at
    # all -- fewer than that isn't a self-intersection question, it's
    # just not a shape yet.
    self_intersects = False
    area_sqft: float | None = None
    if len(corners) >= 3:
        polygon = Polygon(corners)
        self_intersects = not polygon.is_valid
        if self_intersects:
            issues.append(
                "Traverse self-intersects -- the walked boundary crosses itself, "
                "which isn't a valid parcel shape regardless of closure error."
            )
        area_sqft = round(abs(polygon.area), 2)
    else:
        issues.append(
            f"Only {len(corners)} boundary call(s) parsed -- too few to form a "
            "closed shape (need at least 3)."
        )

    stated_area_sqft = find_stated_area_sqft(region_ocr_text)
    area_match: bool | None = None
    if area_sqft is not None and stated_area_sqft:
        relative_diff = abs(area_sqft - stated_area_sqft) / stated_area_sqft
        area_match = relative_diff <= _AREA_MISMATCH_TOLERANCE
        if not area_match:
            issues.append(
                f"Walked area ({area_sqft:,.0f} sqft) differs from the document's "
                f"stated area ({stated_area_sqft:,.0f} sqft) by "
                f"{relative_diff:.0%} -- beyond the "
                f"{_AREA_MISMATCH_TOLERANCE:.0%} tolerance."
            )

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "perimeter_ft": perimeter_ft,
        "precision_ratio": precision_ratio,
        "self_intersects": self_intersects,
        "area_sqft": area_sqft,
        "area_acres": round(area_sqft / _SQFT_PER_ACRE, 3) if area_sqft is not None else None,
        "stated_area_sqft": stated_area_sqft,
        "area_matches_stated": area_match,
    }
