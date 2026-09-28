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
   The stated figure is preferably the parcel-specific one vision
   extracted (associated with THIS parcel's own label, not just the
   first acreage number anywhere in the region), since a region with
   several parcels has several different stated areas and grabbing
   the wrong one defeats the check.

4. Combined-tract dimension detection (check_combined_tract_dimension)
   -- a real, confirmed failure mode: two adjacent parcels sharing one
   drawn property line can each have their own individual segment
   length labeled, alongside a longer combined dimension for the two
   segments together (a real NVZ LLC exhibit: Parcel 2's own edge is
   550.75ft, a sibling Parcel 1's is 352.40ft, and the sheet also
   shows 903.15ft for the two combined -- 550.75 + 352.40 = 903.15
   exactly). A parcel that accidentally walks using the combined
   dimension instead of its own can still close perfectly (the
   combined rectangle is a real, valid shape in the drawing -- just
   the wrong one), so closure alone can't catch it. This check uses
   acreage as an INDEPENDENT signal: if a parcel's walked area is
   suspiciously close to the SUM of its own stated area plus one or
   more sibling parcels' stated areas in the same region, that's
   direct evidence a combined dimension was used. It never silently
   substitutes the correct value -- it surfaces the competing evidence
   and leaves the parcel flagged for review.
"""

import re

from shapely.geometry import Polygon

from app.services.geometry import (
    TraverseResult,
    find_likely_outlier_call,
    find_self_intersecting_segment_pair,
)

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


def _signed_shoelace_area(points: list[tuple[float, float]]) -> float:
    """
    Signed polygon area (positive or negative depending on winding
    direction), in the exact (x, y) convention walk_traverse itself
    uses. Unlike shapely's Polygon.area (always non-negative), the sign
    here is meaningful -- needed so a curve's signed segment-area
    correction (see geometry.py's curve_diagnostics) gets added with
    the right sign regardless of which way this particular traverse
    happens to be wound.
    """

    total = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total / 2


def parse_stated_area_acres(value: str | None) -> float | None:
    """
    Parses vision's per-parcel stated_area_acres field (a bare number
    string like "2.78", no unit) into square feet. Returns None for
    missing/unparseable input -- callers fall back to scanning OCR
    text instead of guessing.
    """

    if not value:
        return None
    try:
        return float(value.strip()) * _SQFT_PER_ACRE
    except ValueError:
        return None


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


# "CONTAINING X.XX Acres" (or "more or less") is standard, near-
# universal boilerplate closing a US legal description with a
# parcel's OWN recorded acreage -- a much stronger signal than a bare
# area figure anywhere in the text, and confirmed more reliable than
# vision's own per-parcel stated_area_acres field on a real document:
# vision extracted a wrong, much larger acreage for a 1.03-acre
# easement (evidently confused by a nearby "the parent tract... is
# recorded in Book 834 at page 104" reference sentence in the same
# paragraph), while this pattern correctly isolated "CONTAINING 1.03
# Acres more or less" -- the parcel's own explicit statement.
_CONTAINING_ACREAGE_RE = re.compile(
    r"\bCONTAINING\s+(\d[\d,]*\.?\d*)\s*(acres?|ac\.?|sq\.?\s?ft\.?|sf)\b",
    re.IGNORECASE,
)


def find_containing_acreage_sqft(text: str) -> float | None:
    """
    Returns the acreage stated via a "CONTAINING X Acres" phrase, but
    ONLY when exactly one such phrase appears in the given text --
    same caution as find_stated_area_sqft's docstring: a region
    covering multiple parcels can have multiple different "CONTAINING"
    statements, one per parcel, and blindly taking the first would
    silently check one parcel against a sibling's acreage. Returns
    None for zero or 2+ matches, not a guess.
    """

    matches = list(_CONTAINING_ACREAGE_RE.finditer(text))
    if len(matches) != 1:
        return None

    value = float(matches[0].group(1).replace(",", ""))
    unit = matches[0].group(2).lower()
    return value * _SQFT_PER_ACRE if unit.startswith("ac") else value


def validate_traverse(
    traverse: TraverseResult,
    region_ocr_text: str = "",
    stated_area_acres: str | None = None,
    calls: list[dict] | None = None,
) -> dict:
    """
    Returns a validation report for a walked traverse. `valid` is the
    overall verdict; `issues` lists the specific, human-readable
    reasons -- surfaced rather than collapsed into a single boolean,
    since a caller (or a future map UI) may want to show which check
    failed, not just that something did.

    `stated_area_acres` is vision's own per-parcel acreage extraction
    (associated with this specific parcel's label) and is preferred
    over scanning `region_ocr_text` for the first acreage figure --
    a region with multiple parcels has multiple different stated
    areas, and blindly taking the first one in OCR text can silently
    check a parcel against a SIBLING's area instead of its own.

    `calls` (the original bearing/distance list, same order used to
    build `traverse`) is optional and only used to name a likely
    outlier call in the weak-closure message when one stands out (see
    geometry.find_likely_outlier_call) -- omit it and the message
    stays generic, same as before.
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
            message = (
                f"Weak closure: precision ratio 1:{precision_ratio:.0f} is below the "
                f"1:{_MIN_ACCEPTABLE_PRECISION_RATIO} conservative minimum -- the "
                "extracted boundary calls are likely incomplete or include noise."
            )
            outlier = find_likely_outlier_call(calls) if calls else None
            if outlier:
                call = outlier["call"]
                message += (
                    f" The call {call.get('bearing')} {call.get('distance')} looks "
                    "like the likely culprit -- removing just that one call alone "
                    f"would cut closure error from {outlier['baseline_closure_ft']:,.0f} ft "
                    f"to {outlier['closure_without_ft']:,.0f} ft "
                    f"({outlier['improvement']:.0%} better). Worth checking that value "
                    "against the source document; not auto-corrected."
                )
            issues.append(message)

    # walk_traverse's last point is where the traverse ENDS UP after
    # its final call. For a well-closed traverse that's a near-
    # duplicate of points[0], not a distinct corner -- feeding it to
    # shapely as-is produces a degenerate zero-length closing segment
    # that gets flagged as a false self-intersection (confirmed on a
    # real, genuinely valid rectangle). But it is NOT safe to drop
    # unconditionally "regardless of how well the traverse closed", as
    # this used to claim: confirmed on a real curve-ending traverse
    # tested against multiple candidate tangent bearings -- when the
    # closure error was large (the traverse genuinely didn't return
    # near its start), area_sqft came out byte-identical across every
    # candidate, because the one point that actually differed between
    # them (the final call's real endpoint) was being discarded before
    # shapely ever saw it, and shapely silently drew its own straight
    # closing line instead of using the real final call. Only drop the
    # last point when it's ACTUALLY near-duplicate of the first.
    closure_error_ft = traverse.closure_error_ft
    near_duplicate_close = closure_error_ft <= max(1.0, 0.01 * perimeter_ft)
    corners = points[:-1] if len(points) > 1 and near_duplicate_close else points

    # A traverse needs at least 3 distinct corners to form a polygon at
    # all -- fewer than that isn't a self-intersection question, it's
    # just not a shape yet.
    self_intersects = False
    area_sqft: float | None = None
    if len(corners) >= 3:
        polygon = Polygon(corners)
        self_intersects = not polygon.is_valid
        if self_intersects:
            message = (
                "Traverse self-intersects -- the walked boundary crosses itself, "
                "which isn't a valid parcel shape regardless of closure error."
            )
            pair = find_self_intersecting_segment_pair(corners, calls) if calls else None
            if pair:
                a, b = calls[pair[0]], calls[pair[1]]
                message += (
                    f" The edges from call {pair[0] + 1} ({a.get('bearing')} "
                    f"{a.get('distance')}) and call {pair[1] + 1} ({b.get('bearing')} "
                    f"{b.get('distance')}) are the ones crossing -- check those two "
                    "against the source document; not auto-corrected."
                )
            issues.append(message)

        # polygon.area (shapely) is the CHORD-polygon area -- correct for
        # straight-only traverses, but for any curve it silently treats
        # the curve as its chord, missing the circular-segment sliver
        # between chord and true arc (confirmed analytically and
        # numerically before this was added: for a real 15°13'50",
        # R=435' curve, that sliver is ~295 sqft, not a rounding-level
        # effect). walk_traverse's curve_diagnostics carries a signed
        # correction per curve (see its "signed_segment_area_sqft" for
        # the sign derivation); applied here against the polygon's own
        # signed shoelace area (not shapely's always-positive .area, so
        # the correction's sign is meaningful) before taking the final
        # absolute value.
        area_sqft = round(abs(polygon.area), 2)
        if traverse.curve_diagnostics:
            signed_area = _signed_shoelace_area(corners)
            signed_area += sum(
                c["signed_segment_area_sqft"] for c in traverse.curve_diagnostics
            )
            area_sqft = round(abs(signed_area), 2)
    else:
        issues.append(
            f"Only {len(corners)} boundary call(s) parsed -- too few to form a "
            "closed shape (need at least 3)."
        )

    stated_area_sqft = parse_stated_area_acres(stated_area_acres) or find_stated_area_sqft(
        region_ocr_text
    )
    area_match: bool | None = None
    area_diff_pct: float | None = None
    if area_sqft is not None and stated_area_sqft:
        relative_diff = abs(area_sqft - stated_area_sqft) / stated_area_sqft
        area_diff_pct = round(relative_diff * 100, 2)
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
        "area_diff_pct": area_diff_pct,
        "curve_diagnostics": traverse.curve_diagnostics,
    }


def check_combined_tract_dimension(parcels: list[dict]) -> list[str | None]:
    """
    Cross-parcel check across every parcel walked from the SAME
    region: flags a parcel whose walked area is suspiciously close to
    the SUM of stated areas across two or more parcels in the group --
    direct, independent evidence (acreage, not geometry) that a
    combined/gross tract dimension was used instead of this parcel's
    own individual segment. See module docstring point 4 for why
    closure alone can't catch this (the combined rectangle is a real,
    valid shape in the drawing -- just the wrong one).

    `parcels` is a list of {"parcel_label", "area_sqft",
    "stated_area_sqft"}, one entry per parcel in the region, any
    order. Returns a same-length list of either None or a human-
    readable warning for that index. Never mutates input, and never
    guesses which dimension is correct or auto-corrects anything --
    it only surfaces the competing evidence for a human (or the
    caller's own issues list) to act on.
    """

    stated_values = [p["stated_area_sqft"] for p in parcels if p.get("stated_area_sqft")]
    total_stated = sum(stated_values)

    warnings: list[str | None] = [None] * len(parcels)
    if len(stated_values) < 2 or total_stated <= 0:
        # Need at least 2 sibling parcels with a known stated area to
        # have a meaningful sum to compare against.
        return warnings

    for i, p in enumerate(parcels):
        area = p.get("area_sqft")
        if not area:
            continue
        relative_diff = abs(area - total_stated) / total_stated
        if relative_diff <= _AREA_MISMATCH_TOLERANCE:
            label = p.get("parcel_label") or "This parcel"
            warnings[i] = (
                f"{label}'s walked area ({area:,.0f} sqft) is close to the COMBINED "
                f"stated area of all {len(stated_values)} parcels in this region "
                f"({total_stated:,.0f} sqft) -- a strong sign one of the extracted "
                "dimensions is the shared/combined tract width, not this parcel's "
                "own individual boundary segment. Needs manual review; not "
                "auto-corrected."
            )
    return warnings
