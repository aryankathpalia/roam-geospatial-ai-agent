"""
Reconstructs an actual boundary polygon from a survey traverse --
bearing/distance calls extracted by vision.py -- using standard
surveying math: walk each bearing/distance as a vector from the
previous corner, and check whether the traverse closes (returns to
its starting point within a small tolerance). Closure error is a real,
standard land-surveying QA signal for free: professional survey plats
are expected to close to a few hundredths of a foot, so a large
closure error is a direct, honest measure of how trustworthy the
extracted calls are -- not something we have to invent a confidence
score for separately.

This produces a LOCAL polygon (feet, arbitrary origin) -- not yet
georeferenced to real-world lat/lon. Projecting it using the tie_point
+ basis_of_bearings (via pyproj, already a dependency) into actual
WGS84 coordinates is a deliberately separate next step: identifying
the right state-plane zone and doing that conversion correctly is its
own piece of work, not something to bolt on here.
"""

import math
import re
from dataclasses import dataclass

_BEARING_RE = re.compile(
    # Degree symbol shows up as *, o, v, or the real ° depending on how
    # the model renders it in JSON -- confirmed empirically ("N0v48'45"E"
    # in real Gemini output, not a typo in our prompt). Also confirmed:
    # TYPOGRAPHIC/curly quote marks for the minute and second symbols --
    # U+2019 RIGHT SINGLE QUOTATION MARK ('), U+2018 LEFT SINGLE
    # QUOTATION MARK ('), U+201D RIGHT DOUBLE QUOTATION MARK ("), and
    # U+201C LEFT DOUBLE QUOTATION MARK (") -- instead of the straight
    # ' " or true prime ′ ″ marks (e.g. "S89°33'38"W" with curly marks,
    # confirmed by inspecting the raw JSON codepoints directly, not
    # assumed from a terminal print -- a console/redirect encoding
    # issue elsewhere in this codebase had displayed these as the
    # Unicode replacement character U+FFFD, which is NOT what Gemini
    # actually returns and is NOT matched here). Without the curly
    # variants, those calls silently failed to parse and were dropped
    # as unparsed (confirmed: a real parcel's 4 calls, 3 in this
    # format, parsed as only 1).
    r"([NSns])\s*(\d+(?:\.\d+)?)\s*[°*ov°]?\s*"
    r"(?:(\d+(?:\.\d+)?)\s*[\'′‘’]?\s*)?"
    r"(?:(\d+(?:\.\d+)?)\s*[\"″“”]?\s*)?"
    r"([EWew])"
)
_DISTANCE_RE = re.compile(r"(\d+(?:\.\d+)?)")


@dataclass
class TraverseResult:
    points: list[tuple[float, float]]  # local (x, y) in feet, origin at (0, 0)
    closure_error_ft: float
    unparsed_calls: int


def parse_bearing(bearing: str) -> float | None:
    """
    Converts a quadrant bearing (e.g. "N89*11'15\"E") to an azimuth in
    degrees, measured clockwise from north (0-360). Returns None if it
    doesn't look like a bearing at all.
    """

    match = _BEARING_RE.search(bearing)
    if not match:
        return None

    ns, deg, minutes, seconds, ew = match.groups()
    angle = float(deg) + float(minutes or 0) / 60 + float(seconds or 0) / 3600

    ns, ew = ns.upper(), ew.upper()
    if ns == "N" and ew == "E":
        return angle
    if ns == "S" and ew == "E":
        return 180 - angle
    if ns == "S" and ew == "W":
        return 180 + angle
    if ns == "N" and ew == "W":
        return 360 - angle
    return None


def parse_distance(distance: str) -> float | None:
    """Extracts a numeric distance in feet from a string like "903.15'"."""

    match = _DISTANCE_RE.search(distance)
    return float(match.group(1)) if match else None


def walk_traverse(boundary_calls: list[dict]) -> TraverseResult:
    """
    Walks a sequence of {bearing, distance} calls as vectors from an
    arbitrary origin, producing the polygon's corner points in order.
    Calls that don't parse are skipped (counted in unparsed_calls)
    rather than aborting the whole traverse.
    """

    x, y = 0.0, 0.0
    points = [(x, y)]
    unparsed = 0

    for call in boundary_calls:
        bearing_str = str(call.get("bearing") or "")
        distance_str = str(call.get("distance") or "")

        # Gemini sometimes returns a bare dimension number (a curve
        # table length, an interior measurement) as a "boundary call"
        # with bearing explicitly "null" -- that's not a traverse leg,
        # it's a call the model itself flagged as incomplete. Skip
        # rather than counting it as noise to blame on our parser.
        if bearing_str.strip().lower() == "null" or distance_str.strip().lower() == "null":
            continue

        azimuth = parse_bearing(bearing_str)
        distance = parse_distance(distance_str)

        if azimuth is None or distance is None:
            unparsed += 1
            continue

        radians = math.radians(azimuth)
        x += distance * math.sin(radians)
        y += distance * math.cos(radians)
        points.append((x, y))

    closure_error = math.hypot(points[-1][0] - points[0][0], points[-1][1] - points[0][1])

    return TraverseResult(
        points=points,
        closure_error_ft=round(closure_error, 2),
        unparsed_calls=unparsed,
    )


def _axis_key(bearing: str) -> float | None:
    azimuth = parse_bearing(bearing)
    return None if azimuth is None else round(azimuth % 180, 1)


def _shoelace_area(points: list[tuple[float, float]]) -> float:
    corners = points[:-1] if len(points) > 1 else points
    n = len(corners)
    if n < 3:
        return 0.0
    s = sum(
        corners[i][0] * corners[(i + 1) % n][1] - corners[(i + 1) % n][0] * corners[i][1]
        for i in range(n)
    )
    return abs(s) / 2


_AREA_TOLERANCE = 0.15
_CLOSED_RATIO = 0.01


def resolve_ambiguous_calls(
    boundary_calls: list[dict],
    ambiguous_alternates: list[dict],
    stated_area_sqft: float | None = None,
    sibling_stated_sqfts: list[float] | None = None,
) -> list[dict]:
    """
    Picks between competing distance readings for boundary lines.
    Alternates match by AXIS (bearing mod 180), so one alternate can
    replace both opposite legs of a line -- needed because a shared
    line's individual segment lengths are often printed as bare
    distances that inherit the combined line's bearing (see vision.py
    rule 8), and a rectangle walks that axis twice.

    When this parcel's own stated acreage is known, combinations are
    ranked by it -- independent evidence closure alone can't provide,
    since the combined tract rectangle closes perfectly too. A
    combination whose area matches THIS parcel plus one or more
    sibling parcels' stated areas is disqualified outright: that's the
    signature of the combined tract dimension being used. Without a
    stated acreage, falls back to lowest closure error.
    """

    if not ambiguous_alternates:
        return boundary_calls

    alts_by_axis: dict[float, list[str]] = {}
    for alt in ambiguous_alternates:
        key = _axis_key(str(alt.get("bearing") or ""))
        dist = alt.get("distance")
        if key is not None and dist:
            alts_by_axis.setdefault(key, []).append(dist)

    candidates_per_position: list[list[dict]] = []
    ambiguous_positions = 0
    for call in boundary_calls:
        key = _axis_key(str(call.get("bearing") or ""))
        dists = alts_by_axis.get(key) if key is not None else None
        if dists and ambiguous_positions < 6:
            options = [call] + [
                {**call, "distance": d} for d in dists if d != call.get("distance")
            ]
            candidates_per_position.append(options)
            ambiguous_positions += 1
        else:
            candidates_per_position.append([call])

    if ambiguous_positions == 0:
        return boundary_calls

    disqualifying_sums: list[float] = []
    if stated_area_sqft and sibling_stated_sqfts:
        total = stated_area_sqft + sum(sibling_stated_sqfts)
        disqualifying_sums.append(total)
        disqualifying_sums.extend(stated_area_sqft + s for s in sibling_stated_sqfts)

    def rank(combo: list[dict]) -> tuple:
        traverse = walk_traverse(combo)
        perimeter = sum(
            math.hypot(b[0] - a[0], b[1] - a[1])
            for a, b in zip(traverse.points, traverse.points[1:])
        ) or 1.0
        closed = traverse.closure_error_ft <= _CLOSED_RATIO * perimeter
        if not stated_area_sqft:
            return (0, traverse.closure_error_ft)
        area = _shoelace_area(traverse.points)
        own_diff = abs(area - stated_area_sqft) / stated_area_sqft
        is_sum_match = any(abs(area - s) / s <= _AREA_TOLERANCE for s in disqualifying_sums)
        return (
            0 if closed else 1,
            1 if is_sum_match else 0,
            0 if own_diff <= _AREA_TOLERANCE else 1,
            own_diff,
            traverse.closure_error_ft,
        )

    return min(_iter_combinations(candidates_per_position), key=rank)


def drop_conflicting_axis_duplicates(boundary_calls: list[dict]) -> list[dict]:
    """
    Catches a second, distinct duplicate-reading failure mode that
    resolve_ambiguous_calls can't: two calls on the SAME axis (bearings
    pointing in opposite/anti-parallel directions, e.g. N0*48'45"E and
    S0*48'45"W) whose distances differ by more than rounding -- a real,
    observed case (2637.36' vs 2417.36' on the same N-S axis, both in
    one parcel's boundary_calls, neither flagged by vision as an
    alternate of the other). This is NOT the same shape as
    resolve_ambiguous_calls' problem: vision never linked these two as
    readings of one line, so there's no explicit alternates list to
    consult -- this is inferred purely from the geometry of the
    finished call list.

    A genuinely valid parcel routinely has two opposite-direction calls
    on the same axis with NEARLY EQUAL distance (a rectangle's two
    N-S sides) -- that's normal and must be left alone. Only a
    meaningful difference (>2%) between same-axis, opposite-direction
    distances is treated as a candidate duplicate/misread, since real
    matching sides agree far more closely than that.

    For each such conflicting pair, tries dropping either call (never
    both at once, and never more than 2 pairs, to keep this bounded)
    and keeps whichever variant -- original, drop-first, or drop-second
    -- yields the lowest closure error, same closure-based evidence
    resolve_ambiguous_calls uses. Never drops a call that improves
    closure by only a marginal amount (<10%), since that's within the
    noise of a legitimately imperfect real survey traverse and dropping
    real data on a weak signal would be guessing, not evidence.
    """

    axis_groups: dict[float, list[int]] = {}
    for i, call in enumerate(boundary_calls):
        azimuth = parse_bearing(str(call.get("bearing") or ""))
        if azimuth is None:
            continue
        axis_key = round(azimuth % 180, 1)
        axis_groups.setdefault(axis_key, []).append(i)

    conflicting_pairs: list[tuple[int, int]] = []
    for indices in axis_groups.values():
        for a, b in zip(indices, indices[1:]):
            dist_a = parse_distance(str(boundary_calls[a].get("distance") or ""))
            dist_b = parse_distance(str(boundary_calls[b].get("distance") or ""))
            if dist_a is None or dist_b is None:
                continue
            if abs(dist_a - dist_b) / max(dist_a, dist_b) > 0.02:
                conflicting_pairs.append((a, b))

    if not conflicting_pairs:
        return boundary_calls

    conflicting_pairs = conflicting_pairs[:2]
    baseline_closure = walk_traverse(boundary_calls).closure_error_ft

    best_calls = boundary_calls
    best_closure = baseline_closure
    for drop_choices in _iter_combinations([[None, a, b] for a, b in conflicting_pairs]):
        dropped_indices = {c for c in drop_choices if c is not None}
        if not dropped_indices:
            continue
        trial = [c for i, c in enumerate(boundary_calls) if i not in dropped_indices]
        if len(trial) < 3:
            continue
        closure = walk_traverse(trial).closure_error_ft
        if closure < best_closure:
            best_closure = closure
            best_calls = trial

    if best_calls is not boundary_calls and baseline_closure > 0:
        improvement = (baseline_closure - best_closure) / baseline_closure
        if improvement < 0.10:
            return boundary_calls

    return best_calls


def _iter_combinations(candidates_per_position: list[list[dict]]):
    if not candidates_per_position:
        yield []
        return
    head, *rest = candidates_per_position
    for tail in _iter_combinations(rest):
        for choice in head:
            yield [choice, *tail]


_OUTLIER_MIN_CALLS = 4  # need >=3 left after removing one candidate
_OUTLIER_MIN_IMPROVEMENT = 0.5  # candidate must cut closure error by >=50%


def find_likely_outlier_call(calls: list[dict]) -> dict | None:
    """
    Weak closure is the single most common issue across real documents
    (confirmed this session across 8 test documents), and the generic
    "precision ratio below threshold" message never says WHICH call to
    check. This tries removing each call one at a time and re-walking
    the rest, looking for ONE removal that explains most of the
    closure error -- the same kind of geometric evidence
    resolve_ambiguous_calls already uses (does removing/changing this
    specific thing make the shape close better?), just applied as a
    diagnostic instead of a correction.

    Returns None (say nothing specific) unless one candidate's removal
    cuts closure error by at least _OUTLIER_MIN_IMPROVEMENT -- a
    generic "somewhat helps" isn't worth pointing a user at a specific
    call over, and multiple call removal is deliberately not
    attempted: an evidence-based diagnostic backed by removing ONE
    value is DEFENSIBLE (as a checkable, "does this change help"
    fact); guessing that TWO OR MORE distinct values are wrong at once
    crosses into speculation this function isn't built to make. Never
    mutates or drops anything -- the caller decides what to do with
    the pointer, this only identifies it.
    """

    if len(calls) < _OUTLIER_MIN_CALLS:
        return None

    baseline = walk_traverse(calls).closure_error_ft
    if baseline <= 0:
        return None

    best_idx, best_closure = None, baseline
    for i in range(len(calls)):
        trial = calls[:i] + calls[i + 1 :]
        closure = walk_traverse(trial).closure_error_ft
        if closure < best_closure:
            best_idx, best_closure = i, closure

    if best_idx is None:
        return None

    improvement = (baseline - best_closure) / baseline
    if improvement < _OUTLIER_MIN_IMPROVEMENT:
        return None

    return {
        "call": calls[best_idx],
        "call_index": best_idx,
        "baseline_closure_ft": round(baseline, 2),
        "closure_without_ft": round(best_closure, 2),
        "improvement": round(improvement, 3),
    }


def find_self_intersecting_segment_pair(
    corners: list[tuple[float, float]], calls: list[dict]
) -> tuple[int, int] | None:
    """
    Names WHICH two edges cross, instead of just "self-intersects" --
    checks every pair of non-adjacent edges (adjacent edges share a
    vertex, which isn't a crossing) for a real intersection using
    shapely, the same library already used for the validity check
    itself. Returns the pair of CALL indices (0-indexed into `calls`,
    same order used to build `corners`) whose edges cross, or None if
    no single pair explains it (e.g. three-way overlap, or corners
    don't line up 1:1 with calls). Purely diagnostic -- never changes
    the traverse.
    """

    from shapely.geometry import LineString

    n = len(corners)
    if n < 4 or len(calls) != n:
        return None

    edges = [LineString([corners[i], corners[(i + 1) % n]]) for i in range(n)]

    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue  # adjacent (wraps around), shares a vertex
            if edges[i].intersects(edges[j]):
                return (i, j)
    return None


def traverse_to_geojson(result: TraverseResult) -> dict:
    """
    Local-coordinate GeoJSON Polygon (NOT yet georeferenced -- see
    module docstring). Closes the ring explicitly, per the GeoJSON
    spec, regardless of how well the traverse itself closed.
    """

    ring = list(result.points)
    if ring[0] != ring[-1]:
        ring.append(ring[0])

    return {
        "type": "Feature",
        "properties": {
            "closure_error_ft": result.closure_error_ft,
            "unparsed_calls": result.unparsed_calls,
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[round(px, 2), round(py, 2)] for px, py in ring]],
        },
    }
