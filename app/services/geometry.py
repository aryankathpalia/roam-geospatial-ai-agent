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

import itertools
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


# Same DMS shape as a bearing's angle, but with no N/S/E/W letters --
# a curve's delta angle is printed as e.g. "41*18'14"" on its own.
_DELTA_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[°*ov°]?\s*"
    r"(?:(\d+(?:\.\d+)?)\s*[\'′‘’]?\s*)?"
    r"(?:(\d+(?:\.\d+)?)\s*[\"″“”]?\s*)?"
)


def parse_delta(delta: str) -> float | None:
    """Converts a curve's delta angle (e.g. "41*18'14\"") to decimal
    degrees. Returns None if it doesn't look like an angle at all."""

    match = _DELTA_RE.search(delta)
    if not match or not match.group(1):
        return None
    deg, minutes, seconds = match.groups()
    return float(deg) + float(minutes or 0) / 60 + float(seconds or 0) / 3600


def merge_curve_calls(boundary_calls: list[dict], curve_calls: list[dict]) -> list[dict]:
    """
    Interleaves vision's separately-reported curve_calls (delta/
    radius/arc_length, each tagged with the 0-indexed straight call it
    walks after -- -1 meaning "before the first call") back into
    boundary_calls' walking order, producing ONE ordered list
    walk_traverse can walk straight through. A no-op when there are no
    curves, so every existing caller (resolve_ambiguous_calls,
    drop_conflicting_axis_duplicates, assemble_traverse -- none of
    which understand curve calls) keeps working unchanged on
    straight-line-only traverses.
    """

    if not curve_calls:
        return boundary_calls

    by_index: dict[int, list[dict]] = {}
    for curve in curve_calls:
        index = curve.get("after_call_index")
        if index is None:
            continue
        by_index.setdefault(index, []).append({**curve, "call_type": "curve"})

    merged = list(by_index.get(-1, []))
    for i, call in enumerate(boundary_calls):
        merged.append(call)
        merged.extend(by_index.get(i, []))
    return merged


def _curve_chord(
    tangent_in_azimuth: float, delta_deg: float, radius: float, turn: str | None
) -> tuple[float, float, float]:
    """
    Standard circular-curve relationship: a curve's chord bearing sits
    exactly half its delta angle off the tangent-in direction, turned
    toward whichever side the curve bows to (R = clockwise/right, L =
    counter-clockwise/left); the tangent-out direction (this curve's
    exit bearing, and the next call's tangent-in) is the full delta off
    tangent-in, same side. Plats don't always print which side a curve
    turns -- defaults to "R" when not given by vision, an explicit,
    documented default rather than a guess dressed up as certainty; a
    curve that turns the wrong way here shows up honestly as a weak
    closure, same as any other extraction miss.
    """

    sign = -1.0 if (turn or "R").upper() == "L" else 1.0
    chord_azimuth = (tangent_in_azimuth + sign * delta_deg / 2) % 360
    chord_distance = 2 * radius * math.sin(math.radians(delta_deg / 2))
    tangent_out_azimuth = (tangent_in_azimuth + sign * delta_deg) % 360
    return chord_azimuth, chord_distance, tangent_out_azimuth


def walk_traverse(boundary_calls: list[dict]) -> TraverseResult:
    """
    Walks a sequence of calls as vectors from an arbitrary origin,
    producing the polygon's corner points in order. Each call is
    either a straight line ({bearing, distance}) or, if merge_curve_
    calls has tagged it call_type "curve" ({delta, radius, arc_length,
    turn}), a circular curve walked chord-to-chord (see _curve_chord).
    Calls that don't parse are skipped (counted in unparsed_calls)
    rather than aborting the whole traverse; a curve as the very FIRST
    call is also skipped and counted, since there's no preceding
    tangent direction to compute its chord from.
    """

    x, y = 0.0, 0.0
    points = [(x, y)]
    unparsed = 0
    tangent_azimuth: float | None = None

    for call in boundary_calls:
        if call.get("call_type") == "curve":
            delta = parse_delta(str(call.get("delta") or ""))
            radius = parse_distance(str(call.get("radius") or ""))
            if delta is None or radius is None or radius <= 0 or tangent_azimuth is None:
                unparsed += 1
                continue
            azimuth, distance, tangent_azimuth = _curve_chord(
                tangent_azimuth, delta, radius, call.get("turn")
            )
        else:
            bearing_str = str(call.get("bearing") or "")
            distance_str = str(call.get("distance") or "")

            # Gemini sometimes returns a bare dimension number (an
            # interior measurement) as a "boundary call" with bearing
            # explicitly "null" -- that's not a traverse leg, it's a
            # call the model itself flagged as incomplete. Skip rather
            # than counting it as noise to blame on our parser.
            if bearing_str.strip().lower() == "null" or distance_str.strip().lower() == "null":
                continue

            azimuth = parse_bearing(bearing_str)
            distance = parse_distance(distance_str)

            if azimuth is None or distance is None:
                unparsed += 1
                continue
            tangent_azimuth = azimuth

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


_BORROW_MIN_PRECISION = 2_000  # same bar as validate_traverse's "valid" cutoff


def borrow_sibling_call(
    calls: list[dict],
    own_stated_sqft: float | None,
    siblings: list[tuple[str | None, list[dict]]],
    disqualifying_areas: list[float],
) -> tuple[list[dict], str | None]:
    """
    Generalizes Track B (resolve_ambiguous_calls) past the 2-parcel case
    it was built for. Track B only resolves AMBIGUITY -- it picks between
    candidate readings vision already offered for one of THIS parcel's
    own calls. It has no mechanism for a side that's missing outright,
    which is the common case on an N-lot subdivision plat: a shared line
    between two lots is often labeled once, next to whichever lot's
    label the drafter put it closest to, and vision's own extraction
    rule (deliberately, to avoid cross-attributing calls) never assigns
    it to the OTHER lot at all -- so that lot's boundary_calls simply
    never contains it, not even as an alternate.

    This tries, ONE AT A TIME, appending a call from a SIBLING parcel's
    own already-resolved boundary (same region, extracted in the same
    vision call) to `calls`, keeping the borrow only if the resulting
    traverse both closes (>= _BORROW_MIN_PRECISION) AND its area matches
    THIS parcel's own stated acreage -- the same acreage-match discipline
    Track B already uses to reject a combined-tract mistake, extended
    here to also reject a borrow whose area matches a SIBLING's stated
    acreage instead (a different tell: it means the borrowed line traced
    the sibling's shape, not this parcel's own). `siblings` must already
    be ordered nearest-first by the caller (adjacency in extraction
    order is the only proxy available without real coordinates -- see
    this session's scoping note on why that's a real, accepted limit).

    Limited to exactly ONE borrowed call -- stacking multiple borrows
    compounds the false-positive risk for a first version. Returns the
    original `calls` unchanged (and None) if nothing validates.
    """

    if len(calls) < 2 or not own_stated_sqft:
        return calls, None

    baseline = walk_traverse(calls)
    baseline_perimeter = sum(
        math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(baseline.points, baseline.points[1:])
    ) or 1.0
    if baseline.closure_error_ft <= 0 or baseline_perimeter / baseline.closure_error_ft >= _BORROW_MIN_PRECISION:
        return calls, None  # already closes; nothing to borrow for

    for label, sibling_calls in siblings:
        for base_candidate in sibling_calls:
            bearing_str = str(base_candidate.get("bearing") or "")
            if parse_bearing(bearing_str) is None:
                continue
            if parse_distance(str(base_candidate.get("distance") or "")) is None:
                continue

            # Same "which end was this line labeled from" ambiguity
            # assemble_traverse already resolves for a parcel's OWN
            # calls -- a borrowed line is just as likely to need
            # walking in the opposite direction from how the sibling
            # printed it, since it's still just one physical line with
            # two possible walking directions.
            candidates = [base_candidate, {**base_candidate, "bearing": _reverse_bearing(bearing_str)}]

            for candidate in candidates:
                trial = calls + [candidate]
                traverse = walk_traverse(trial)
                perimeter = sum(
                    math.hypot(b[0] - a[0], b[1] - a[1])
                    for a, b in zip(traverse.points, traverse.points[1:])
                ) or 1.0
                if traverse.closure_error_ft <= 0:
                    precision = float("inf")
                else:
                    precision = perimeter / traverse.closure_error_ft
                if precision < _BORROW_MIN_PRECISION:
                    continue

                area = _shoelace_area(traverse.points)
                own_diff = abs(area - own_stated_sqft) / own_stated_sqft
                if own_diff > _AREA_TOLERANCE:
                    continue
                if any(abs(area - s) / s <= _AREA_TOLERANCE for s in disqualifying_areas if s > 0):
                    continue

                note = (
                    f"Borrowed the call {candidate.get('bearing')} {candidate.get('distance')} "
                    f"from {label or 'a neighboring parcel'} in this region to close the "
                    f"traverse -- this parcel's own extraction never included it, but the "
                    f"resulting area matches this parcel's own stated acreage "
                    f"({own_stated_sqft:,.0f} sqft, {own_diff:.0%} off) and does not match "
                    f"any sibling parcel's acreage. Worth checking against the source "
                    f"document; not auto-corrected beyond this one borrowed value."
                )
                return trial, note

    return calls, None


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


_ASSEMBLY_MIN_PRECISION = 2_000
_ASSEMBLY_MAX_CALLS = 16
_ASSEMBLY_MAX_DROPS = 2


def _reverse_bearing(bearing: str) -> str:
    match = _BEARING_RE.search(bearing)
    ns, deg, minutes, seconds, ew = match.groups()
    ns = "S" if ns.upper() == "N" else "N"
    ew = "W" if ew.upper() == "E" else "E"
    text = f"{ns}{deg}°"
    if minutes is not None:
        text += f"{minutes}'"
    if seconds is not None:
        text += f'{seconds}"'
    return text + ew


def _is_simple_polygon(vectors) -> bool:
    from shapely.geometry import Polygon

    points = [(0.0, 0.0)]
    for dx, dy in vectors[:-1]:
        points.append((points[-1][0] + dx, points[-1][1] + dy))
    return len(points) >= 3 and Polygon(points).is_valid


def _polygon_area(vectors) -> float:
    points = [(0.0, 0.0)]
    for dx, dy in vectors:
        points.append((points[-1][0] + dx, points[-1][1] + dy))
    return _shoelace_area(points)


def assemble_traverse(
    calls: list[dict], stated_area_sqft: float | None = None
) -> tuple[list[dict], list[str]]:
    """
    Plats label each line's bearing from whichever end the drafter
    chose, and vision lists lines in reading order, not walking order
    -- so a fully correct set of extracted lines can still "fail" to
    close (confirmed on the regression corpus: a 40-acre parcel with
    all four sides read correctly closed at 1:7 as listed, 1:601,203
    once two directions were reversed). This finds the direction
    assignment that closes the traverse and, only if the given order
    then crosses itself, falls back to walking the lines in bearing
    order (the convex arrangement).

    Reversing a line's direction doesn't change the line, so it's
    applied whenever it reaches the closure bar -- tested against
    random lines from unrelated parcels, direction flips alone never
    produced a false closure. Dropping lines is far easier to fool, so
    up to two drops are allowed only when the parcel's own stated
    acreage independently agrees with the result. Returns the
    (possibly unchanged) calls plus human-readable notes on every
    change made; never changes a distance or bearing angle.
    """

    import numpy as np

    parsed = []
    for call in calls:
        azimuth = parse_bearing(str(call.get("bearing") or ""))
        distance = parse_distance(str(call.get("distance") or ""))
        if azimuth is not None and distance:
            r = math.radians(azimuth)
            parsed.append((call, (distance * math.sin(r), distance * math.cos(r))))
    n = len(parsed)
    if n < 3 or n > _ASSEMBLY_MAX_CALLS or n != len(calls):
        return calls, []

    vectors = np.array([v for _, v in parsed])
    lengths = np.hypot(vectors[:, 0], vectors[:, 1])
    baseline_closure = float(np.hypot(*vectors.sum(axis=0)))
    if baseline_closure == 0 or lengths.sum() / baseline_closure >= _ASSEMBLY_MIN_PRECISION:
        return calls, []

    max_drops = _ASSEMBLY_MAX_DROPS if stated_area_sqft else 0
    best = None
    for k in range(max_drops + 1):
        for dropped in itertools.combinations(range(n), k):
            keep = [i for i in range(n) if i not in dropped]
            if len(keep) < 3:
                continue
            sub = vectors[keep]
            signs = np.array(list(itertools.product([1, -1], repeat=len(keep) - 1)))
            signs = np.hstack([np.ones((len(signs), 1)), signs])
            totals = signs @ sub
            closures = np.hypot(totals[:, 0], totals[:, 1])
            j = int(closures.argmin())
            precision = lengths[keep].sum() / max(float(closures[j]), 1e-9)
            if precision < _ASSEMBLY_MIN_PRECISION:
                continue
            signed = sub * signs[j][:, None]
            order = list(range(len(keep)))
            if not _is_simple_polygon(signed):
                order = sorted(order, key=lambda i: math.atan2(signed[i][0], signed[i][1]) % (2 * math.pi))
                if not _is_simple_polygon(signed[order]):
                    continue
            area = _polygon_area(signed[order])
            if k and abs(area - stated_area_sqft) / stated_area_sqft > _AREA_TOLERANCE:
                continue
            if best is None or precision > best[0]:
                best = (precision, keep, signs[j], order, dropped)
        if best:
            break

    if best is None:
        return calls, []

    _, keep, sign_row, order, dropped = best
    assembled = []
    reversed_count = 0
    for pos in order:
        call = parsed[keep[pos]][0]
        if sign_row[pos] < 0:
            call = {**call, "bearing": _reverse_bearing(str(call["bearing"]))}
            reversed_count += 1
        assembled.append(call)

    notes = []
    if reversed_count:
        notes.append(
            f"Walked {reversed_count} line(s) in the opposite direction to how the "
            "bearing is printed (same line, other end) so the traverse closes."
        )
    if order != sorted(order):
        notes.append("Reordered lines into walking order; the extracted order crossed itself.")
    for i in dropped:
        c = parsed[i][0]
        notes.append(
            f"Left out {c.get('bearing')} {c.get('distance')}: the remaining lines close "
            "and match the stated acreage without it -- check it against the source."
        )
    return assembled, notes


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
