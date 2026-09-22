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

# A US ZIP code (with optional +4) is the single strongest signal that a
# line is a real postal address rather than survey jargon that happens
# to contain a place-sounding word.
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
# "<word>, XX" or "<word> County, State" -- a state abbreviation or the
# literal word County near a comma is the next-strongest signal.
_STATE_HINT_RE = re.compile(r",\s*[A-Z]{2}\b|\bCounty\b", re.IGNORECASE)


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
                if _ZIP_RE.search(line):
                    score += 2
                if _STATE_HINT_RE.search(line):
                    score += 1
                if score > best_score:
                    best_score = score
                    best_line = line

    return best_line


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
