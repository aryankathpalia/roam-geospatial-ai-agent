"""
Projects a LOCAL traverse polygon (feet, arbitrary origin -- see
geometry.py) onto real WGS84 lat/lon.

The hard part is not the math, it's finding an ANCHOR point to project
from. A real survey plat's tie_point is a state-plane or ground
coordinate (e.g. "N 14926910.28 E 2251599.70"), and correctly
georeferencing that requires knowing which of ~120 US state-plane zones
it belongs to -- not reliably inferable from a scanned document alone,
and guessing wrong would silently place a parcel hundreds of miles from
where it actually is. That's a real, unsolved problem, not something to
paper over.

Instead: this document's own OCR text almost always contains a
geocodable address or "<place>, County, State" somewhere (a cover page,
a legal description, a stamp) -- Text/ScannedPrintout regions across
the whole document are searched for the most address-like line, which
is geocoded via the EXISTING Nominatim geocoder (app/services/
geocoding.py) to get a real-world anchor point. The traverse is then
walked from that anchor using true geodesic math (pyproj.Geod), which
is exact regardless of anchor position (no small-distance approximation
error) -- this is the same forward-geodesic operation a real GIS uses
for "walk a survey traverse from a known point."

This is deliberately an APPROXIMATION relative to a true state-plane
projection: the anchor is a geocoded address centroid, not a surveyed
tie point, so the resulting polygon's ABSOLUTE position is only as
good as the geocode (typically street-level, tens of meters) while its
SHAPE and SIZE are exact (the bearings/distances are walked precisely).
Good enough to place a parcel on a map and see its correct shape/
orientation/scale; not good enough for legal boundary determination.
"""

import math
import re

from pyproj import Geod

from app.services.geometry import TraverseResult

_GEOD = Geod(ellps="WGS84")
_FEET_TO_METERS = 0.3048

# Same set as app.pipeline.page_ocr.OCR_ELIGIBLE_CLASSES -- duplicated
# rather than imported so this module doesn't pull in the OCR engine
# (paddleocr) just to read a region-class constant.
_TEXT_BEARING_CLASSES = {"Text", "Table", "Seal", "ScannedPrintout", "ParcelMap"}

# A US ZIP code (with optional +4) is a signal that a line is a real
# postal address rather than survey jargon that happens to contain a
# place-sounding word.
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
# "<word>, XX" or "<word> County, State" -- a state abbreviation or the
# literal word County near a comma is a weaker signal on its own.
_STATE_HINT_RE = re.compile(r",\s*[A-Z]{2}\b|\bCounty\b", re.IGNORECASE)
# An explicit "Project/Site/Property/Subject Address:" label is the
# STRONGEST signal -- it directly names the parcel's own location,
# not a form-preparer's or surveying firm's office address. Land-
# record application forms consistently use one of these labels
# (confirmed on a real document: "Project Address: 10235 Placerville
# Road" was present and unambiguous, but scored the same as a firm's
# Reno office ZIP line before this field-label check existed, and the
# office address won on tie-break order).
_ADDRESS_LABEL_RE = re.compile(
    r"\b(?:project|site|property|subject)\s+address\b", re.IGNORECASE
)
# Lines that are almost always a FIRM's or agency's own mailing/
# letterhead address, not the parcel's -- downweighted rather than
# excluded outright, since they're still real fallback signal if
# nothing better exists in the document.
_OFFICE_HINT_RE = re.compile(
    r"\b(?:surveyor|engineer|p\.?l\.?s\.?|inc\.?|llc|"
    r"planning\s+and\s+building|treasurer|development\s+application)\b",
    re.IGNORECASE,
)


def find_anchor_query(pages_result: list[dict]) -> str | None:
    """
    Scans every OCR'd Text/ScannedPrintout region across the document
    for the single most address-like line, to hand to the geocoder.
    Returns None if nothing plausible is found -- callers must degrade
    gracefully (no anchor means no georeferencing, not a guessed one).
    """

    best_line: str | None = None
    best_score = 0

    for page in pages_result:
        for region in page["regions"]:
            if region["class"] not in _TEXT_BEARING_CLASSES:
                continue
            text = region.get("ocr_text") or ""
            for line in text.splitlines():
                line = line.strip()
                if len(line) < 6:
                    continue

                score = 0
                if _ADDRESS_LABEL_RE.search(line):
                    score += 5
                if _ZIP_RE.search(line):
                    score += 2
                if _STATE_HINT_RE.search(line):
                    score += 1
                if _OFFICE_HINT_RE.search(line):
                    score -= 3
                if score > best_score:
                    best_score = score
                    best_line = line

    if best_line is None:
        return None

    # Strip a leading field label ("Project Address:") before handing
    # the line to the geocoder -- confirmed empirically that Nominatim
    # returns zero results for "Project Address: 10235 Placerville
    # Road" but resolves "10235 Placerville Road" correctly, since the
    # label isn't part of any real place name it can match against.
    return _ADDRESS_LABEL_RE.sub("", best_line).lstrip(": ").strip()


def georeference_traverse_to_geojson(
    traverse: TraverseResult, anchor_lat: float, anchor_lon: float
) -> dict:
    """
    Projects the traverse's LOCAL (x, y) feet-from-origin points onto
    real WGS84 coordinates, walking each one from (anchor_lat,
    anchor_lon) as a true geodesic (exact forward-azimuth/distance
    solve, not a flat-earth approximation) -- x is east-offset feet,
    y is north-offset feet, matching geometry.py's walk_traverse
    convention.
    """

    ring_lonlat: list[tuple[float, float]] = []
    for x, y in traverse.points:
        distance_m = math.hypot(x, y) * _FEET_TO_METERS
        if distance_m == 0:
            ring_lonlat.append((anchor_lon, anchor_lat))
            continue

        azimuth_deg = math.degrees(math.atan2(x, y))  # clockwise from north
        lon, lat, _back_azimuth = _GEOD.fwd(anchor_lon, anchor_lat, azimuth_deg, distance_m)
        ring_lonlat.append((lon, lat))

    if ring_lonlat[0] != ring_lonlat[-1]:
        ring_lonlat.append(ring_lonlat[0])

    return {
        "type": "Feature",
        "properties": {
            "closure_error_ft": traverse.closure_error_ft,
            "unparsed_calls": traverse.unparsed_calls,
            "anchor_lat": anchor_lat,
            "anchor_lon": anchor_lon,
            "georeferenced": "approximate",
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[round(lon, 7), round(lat, 7)] for lon, lat in ring_lonlat]],
        },
    }


# ---------------------------------------------------------------------
# Surveyed ground-coordinate placement -- NARROW SCOPE, READ BEFORE REUSE
#
# This is NOT general coordinate-system support. It only works when the
# caller already knows, from the document itself, (a) a surveyed ground
# coordinate at a specific corner of the parcel, (b) the exact EPSG code
# of the state-plane zone the sheet states in its basis of bearings, and
# (c) the sheet's stated grid-to-ground combined factor. Nothing here
# infers a zone, validates that the coordinate belongs to that zone, or
# handles datums/units beyond what the caller passes in. It was written
# for one confirmed case (WTPM26-0004 page 9, NAD83(94) Nevada West,
# factor 1.000197939) and should not be wired into the general pipeline
# without that detection/validation work being done first -- guessing a
# zone wrong silently puts a parcel in the wrong place (see module
# docstring). It also assumes the traverse's bearings are grid bearings
# in that same zone (true when the sheet's basis of bearings IS the
# state-plane zone), so no convergence-angle rotation is applied.
# ---------------------------------------------------------------------

_CORNER_SCORES = {
    "NW": lambda x, y: y - x,
    "NE": lambda x, y: y + x,
    "SW": lambda x, y: -y - x,
    "SE": lambda x, y: -y + x,
}


def georeference_traverse_from_ground_corner(
    traverse: TraverseResult,
    corner: str,
    ground_northing_ft: float,
    ground_easting_ft: float,
    epsg: int,
    grid_to_ground_factor: float,
) -> dict:
    """
    Places a walked traverse so its `corner` ("NW"/"NE"/"SW"/"SE" --
    the vertex extreme in that direction) sits at a surveyed ground
    coordinate, then converts every vertex to WGS84. Ground coordinates
    (and the traverse's ground distances) are scaled to grid by dividing
    by grid_to_ground_factor before projecting. See the scope note above.
    """

    from pyproj import Transformer

    corners = traverse.points[:-1] if len(traverse.points) > 1 else traverse.points
    score = _CORNER_SCORES[corner.upper()]
    ax, ay = max(corners, key=lambda p: score(p[0], p[1]))

    to_wgs84 = Transformer.from_crs(epsg, 4326, always_xy=True)
    ring_lonlat = []
    for x, y in traverse.points:
        grid_e = (ground_easting_ft + (x - ax)) / grid_to_ground_factor
        grid_n = (ground_northing_ft + (y - ay)) / grid_to_ground_factor
        lon, lat = to_wgs84.transform(grid_e, grid_n)
        ring_lonlat.append((lon, lat))
    if ring_lonlat[0] != ring_lonlat[-1]:
        ring_lonlat.append(ring_lonlat[0])

    return {
        "type": "Feature",
        "properties": {
            "closure_error_ft": traverse.closure_error_ft,
            "unparsed_calls": traverse.unparsed_calls,
            "georeferenced": "surveyed_ground_coordinate",
            "anchor_corner": corner.upper(),
            "anchor_ground_ne_ft": [ground_northing_ft, ground_easting_ft],
            "epsg": epsg,
            "grid_to_ground_factor": grid_to_ground_factor,
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[round(lon, 8), round(lat, 8)] for lon, lat in ring_lonlat]],
        },
    }
