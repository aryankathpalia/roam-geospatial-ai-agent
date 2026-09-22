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
    # in real Gemini output, not a typo in our prompt).
    r"([NSns])\s*(\d+(?:\.\d+)?)\s*[°*ov°]?\s*"
    r"(?:(\d+(?:\.\d+)?)\s*[\'′]?\s*)?"
    r"(?:(\d+(?:\.\d+)?)\s*[\"″]?\s*)?"
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
