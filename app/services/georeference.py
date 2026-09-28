"""
Projects a LOCAL traverse polygon (feet, arbitrary origin -- see
geometry.py) onto real WGS84 lat/lon.

The hard part is not the math, it's finding an ANCHOR point to project
from. Three sources are tried, in order of precision:

1. A "Lat X Long Y" coordinate printed directly on the drawing
   (find_explicit_coordinates) -- exact, no zone ambiguity, no
   geocoding at all.
2. A state-plane or ground corner coordinate (e.g. "N 14926910.28
   E 2251599.70") printed on the drawing (find_surveyed_coordinates).
   Correctly converting this requires knowing which of ~120 US
   state-plane zones it belongs to -- not reliably inferable from the
   coordinate alone, and guessing wrong would silently place a parcel
   hundreds of miles from where it actually is. Solved by using a
   coarser anchor (below) to identify the STATE, trying every zone
   registered for it, and keeping only a conversion that lands near
   that coarser anchor -- a wrong zone guess is rejected outright, so
   this can only make the anchor more precise, never wrong in a new
   way.
3. A geocodable address or "<place>, County, State" found in the
   document's own OCR text (Text/ScannedPrintout regions scanned for
   the most address-like line), geocoded via Nominatim
   (app/services/geocoding.py). This is also what supplies the
   coarser anchor step 2 validates against.

Whichever anchor is found, the traverse is walked from it using true
geodesic math (pyproj.Geod), exact regardless of anchor position (no
small-distance approximation error) -- the same forward-geodesic
operation a real GIS uses for "walk a survey traverse from a known
point."

A geocoded address (source 3) is only an APPROXIMATION: the anchor is
an address centroid, not a surveyed tie point, so the resulting
polygon's ABSOLUTE position is only as good as the geocode (typically
street-level, tens of meters) while its SHAPE and SIZE are exact (the
bearings/distances are walked precisely). Sources 1 and 2 are surveyed
-- see ANCHOR_SURVEYED below, and the "Location" check in the review UI,
which is only green when the anchor is one of these two.
"""

import math
import re

from pyproj import Geod

from app.services.geometry import TraverseResult

_GEOD = Geod(ellps="WGS84")
_FEET_TO_METERS = 0.3048

# Some documents print literal surveyed/GPS coordinates directly on the
# drawing (confirmed on a real document: a utility easement exhibit
# labeling each pole location as "Lat 39.72330 Long -90.79963"). When
# present, these are strictly better than geocoding any address text --
# exact, no ambiguity about which building/office/city matched, no
# dependency on an external geocoder at all -- so they're checked for
# FIRST, before any address-based candidate is even considered.
_LATLONG_RE = re.compile(
    r"\bLat\.?\s*(-?\d{1,2}\.\d{3,8})\s*,?\s*Long\.?\s*(-?\d{1,3}\.\d{3,8})",
    re.IGNORECASE,
)
# Loose bounding box for the continental US + Alaska/Hawaii -- a sanity
# filter, not a real projection check, so an OCR misread digit doesn't
# produce a coordinate pair that LOOKS valid but is actually nonsense
# (e.g. swapped lat/lon, or a decimal point shifted by a digit).
_US_LAT_RANGE = (17.0, 72.0)
_US_LON_RANGE = (-180.0, -65.0)


# How exactly the anchor pins the parcel down. Only coordinates printed
# on the document itself are "surveyed": a geocoded street address
# lands on the building/address point, not the parcel's actual point of
# beginning, which is visibly tens of metres off on imagery.
ANCHOR_SURVEYED = "surveyed"
ANCHOR_STREET = "street"
ANCHOR_CITY = "city"
_STREET_NUMBER_RE = re.compile(r"^\s*\d+[A-Za-z]?\s+\S")


def classify_anchor_query(query: str) -> str:
    return ANCHOR_STREET if _STREET_NUMBER_RE.match(query) else ANCHOR_CITY


def find_explicit_coordinates(pages_result: list[dict]) -> tuple[float, float] | None:
    """
    Scans every OCR'd region for literal "Lat X Long Y" coordinate
    labels and returns their centroid, or None if none are found.
    Multiple points (e.g. a corridor of utility pole locations) are
    averaged rather than just taking the first -- a reasonable
    representative anchor for the whole parcel/corridor, not tied to
    wherever the OCR happened to encounter the first label.
    """

    found: list[tuple[float, float]] = []
    for page in pages_result:
        for region in page["regions"]:
            text = region.get("ocr_text") or ""
            for match in _LATLONG_RE.finditer(text):
                lat, lon = float(match.group(1)), float(match.group(2))
                if _US_LAT_RANGE[0] <= lat <= _US_LAT_RANGE[1] and _US_LON_RANGE[0] <= lon <= _US_LON_RANGE[1]:
                    found.append((lat, lon))

    if not found:
        return None
    return (
        sum(lat for lat, _ in found) / len(found),
        sum(lon for _, lon in found) / len(found),
    )


# A surveyed state-plane corner coordinate ("N: 137,042.75  E: 1,101,226.83"
# or "N 14926910.28 E 2251599.70") is far more precise than any geocoded
# address, but converting it correctly requires knowing which of ~120 US
# state-plane zones it belongs to -- the exact "real, unsolved problem"
# this module's own docstring used to name. It's solvable, though, once
# a coarse anchor already exists (from geocoding or an explicit lat/long
# elsewhere in the document): that anchor tells us the STATE, and the
# only zone worth trying is the one whose converted point actually lands
# near where we already know the parcel is. A wrong zone guess produces
# a point far from the coarse anchor and is rejected outright, so this
# can only make the anchor MORE precise, never wrong in a new way.
#
# N and E are matched SEPARATELY, not as one "N... E..." pattern -- OCR
# on a real document put unrelated text between them (the two labels
# read out of visual order), so requiring them adjacent found nothing.
# Matched by position (1st N pairs with 1st E, etc.) instead.
_N_COORD_RE = re.compile(r"\bN[:：]\s*([\d.,]+)")
_E_COORD_RE = re.compile(r"\bE[:：]\s*([\d.,]+)")
_STATE_PLANE_VALIDATION_MILES = 60.0


def _parse_survey_number(raw: str) -> float | None:
    """
    OCR sometimes misreads one of a number's thousands-separating commas
    as a period ("1,101,226.83" -> "1,101.226.83", confirmed on a real
    document). Treats every "." or "," as a thousands separator EXCEPT
    the last one, which is the true decimal point only if followed by
    1-2 digits (the normal case for a coordinate given in feet).
    """

    match = re.match(r"^([\d.,]*?)[.,](\d{1,2})$", raw)
    if not match:
        digits = re.sub(r"[.,]", "", raw)
        return float(digits) if digits else None
    whole = re.sub(r"[.,]", "", match.group(1))
    return float(f"{whole}.{match.group(2)}")


def find_surveyed_coordinates(
    pages_result: list[dict], state_name: str, near_lat: float, near_lon: float
) -> tuple[float, float] | None:
    """
    Looks for a printed "N: ... E: ..." state-plane corner coordinate,
    tries every NAD83 state-plane CRS registered for `state_name` (most
    states have 2-8; a handful, like New Hampshire, have exactly one),
    and returns the FIRST converted point that lands within
    _STATE_PLANE_VALIDATION_MILES of (near_lat, near_lon) -- the coarse
    anchor already found by geocoding or explicit lat/long. Returns
    None if no coordinate is found, or none of the state's zones
    converts anywhere near the coarse anchor (never guesses a zone with
    nothing to validate it against).
    """

    from pyproj import Transformer
    from pyproj.database import query_crs_info

    text = "\n".join(r.get("ocr_text") or "" for p in pages_result for r in p["regions"])
    northings = [_parse_survey_number(m.group(1)) for m in _N_COORD_RE.finditer(text)]
    eastings = [_parse_survey_number(m.group(1)) for m in _E_COORD_RE.finditer(text)]
    pairs = [
        (n, e) for n, e in zip(northings, eastings) if n is not None and e is not None
    ]
    if not pairs:
        return None

    candidates = [
        (crs.code, crs.name)
        for crs in query_crs_info(auth_name="EPSG", pj_types=None)
        if "NAD83" in crs.name and state_name in crs.name
    ]
    # Newer/US-feet realizations first -- the common case for a recent
    # US survey plat -- but every candidate is validated the same way
    # regardless of order, so a wrong guess here just gets skipped.
    candidates.sort(key=lambda pair: "ftUS" not in pair[1])

    for northing, easting in pairs:
        for code, _name in candidates:
            try:
                transformer = Transformer.from_crs(f"EPSG:{code}", "EPSG:4326", always_xy=True)
                lon, lat = transformer.transform(easting, northing)
            except Exception:  # noqa: BLE001 -- a bad/inapplicable CRS, try the next
                continue
            _, _, distance_m = _GEOD.inv(lon, lat, near_lon, near_lat)
            if distance_m / 1609.34 <= _STATE_PLANE_VALIDATION_MILES:
                return lat, lon

    return None


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
# US land-record documents routinely phrase a place as "City of X" /
# "Town of X" / "Village of X" -- confirmed empirically that Nominatim
# returns zero results for "City of Payette, Idaho" but resolves
# "Payette, Payette County, Idaho" correctly once this municipal
# prefix is stripped, the same kind of label-vs-place-name mismatch
# _ADDRESS_LABEL_RE already handles for "Project Address:".
_MUNICIPAL_PREFIX_RE = re.compile(
    # \s* (not \s+) after "of" -- OCR sometimes glues the following
    # word on with no space at all ("CITY OFPAYETTE"), confirmed on
    # this exact document.
    r"\b(?:city|town|village)\s+of\s*",
    re.IGNORECASE,
)
# Lines that are almost always a FIRM's or agency's own mailing/
# letterhead address, not the parcel's -- downweighted rather than
# excluded outright, since they're still real fallback signal if
# nothing better exists in the document. "surveying" added alongside
# "surveyor" -- a firm name like "Sawtooth Land Surveying" uses the
# noun form, not "surveyor".
_OFFICE_HINT_RE = re.compile(
    r"\b(?:surveyor|surveying|engineer|p\.?l\.?s\.?|inc\.?|llc|"
    r"planning\s+and\s+building|treasurer|development\s+application)\b",
    re.IGNORECASE,
)

# A candidate needs real corroborating signal, not just one weak match,
# before it's trusted enough to spend a geocode attempt on. Originally
# any positive score (score > 0) qualified, which let a BARE ZIP-shaped
# number -- with no state, county, or address-label context at all --
# through on its own. That's exactly the kind of short, place-name-free
# text that matches something in Nominatim's global database almost
# every time it's tried, confidently returning a wrong-country result
# instead of correctly finding nothing (confirmed: a scale ratio like
# "1:37435" matched a Czech Republic postal code; unrelated garbled OCR
# matched an address in Japan). Raising the bar to require an explicit
# address label alone (5), OR a ZIP together with a state/county hint
# (2+1=3), fixes this without any country-specific logic -- it works
# the same everywhere in the world, unlike an earlier version of this
# fix that tried allowlisting the US and was rightly rejected: this
# pipeline isn't US-only.
MIN_CANDIDATE_SCORE = 3


def _clean_candidate(line: str) -> str:
    # Strip a leading field label ("Project Address:") and any
    # "City/Town/Village of" municipal prefix before handing the line
    # to the geocoder -- neither is part of a real place name Nominatim
    # can match against (see the two regexes' docstrings above).
    cleaned = _ADDRESS_LABEL_RE.sub("", line).lstrip(": ").strip()
    return _MUNICIPAL_PREFIX_RE.sub("", cleaned).strip()


def find_anchor_candidates(pages_result: list[dict], limit: int = 3) -> list[str]:
    """
    Same scoring as find_anchor_query, but returns up to `limit`
    distinct candidate queries, best first, instead of committing to
    just one. Exists so a caller can retry geocoding against the next
    candidate (or a coarsened version of the best one -- see
    geocode_anchor in geocoding.py) when the top-scored line fails to
    geocode, e.g. because of an OCR typo the scoring has no way to
    detect. Without this, one bad candidate meant the whole document
    got no anchor at all, even when a real, geocodable address existed
    elsewhere -- confirmed on a real document (798850fc): the winning
    line failed to geocode over a single garbled word ("Avefue" for
    "Avenue"), and the document's location fell back to nothing rather
    than to the equally-real, coarser candidate sitting right next to
    it in the same OCR block.
    """

    scored: list[tuple[int, str]] = []

    for page in pages_result:
        for region in page["regions"]:
            if region["class"] not in _TEXT_BEARING_CLASSES:
                continue
            text = region.get("ocr_text") or ""
            for raw_line in text.splitlines():
                raw_line = raw_line.strip()
                if len(raw_line) < 6:
                    continue

                # Letterhead is often OCR'd as one physical line with
                # "~" or "|" joining several distinct fields ("City of
                # Payette ~ 700 Center Ave ~ Payette, ID 83661 ~
                # 208-642-6024") -- scoring the whole joined line hides
                # a clean address inside noisier neighbors (a typo'd
                # street name, a phone number). Scoring each field
                # separately too lets a clean "Payette, ID 83661"
                # segment win on its own, even when the full line it
                # came from is contaminated. The whole line is still
                # scored as well, so this only ADDS candidates, never
                # removes the original behavior.
                candidate_lines = [raw_line]
                if "~" in raw_line or "|" in raw_line:
                    for part in re.split(r"[~|]", raw_line):
                        part = part.strip()
                        if len(part) >= 6:
                            candidate_lines.append(part)

                for line in candidate_lines:
                    score = 0
                    if _ADDRESS_LABEL_RE.search(line):
                        score += 5
                    if _ZIP_RE.search(line):
                        score += 2
                    if _STATE_HINT_RE.search(line):
                        score += 1
                    if _OFFICE_HINT_RE.search(line):
                        score -= 3
                    if score >= MIN_CANDIDATE_SCORE:
                        scored.append((score, line))

    scored.sort(key=lambda pair: pair[0], reverse=True)

    candidates: list[str] = []
    seen: set[str] = set()
    for _score, line in scored:
        cleaned = _clean_candidate(line)
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        candidates.append(cleaned)
        if len(candidates) >= limit:
            break

    return candidates


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
