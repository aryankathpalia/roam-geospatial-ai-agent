"""
Resolves a PLSS (Public Land Survey System) section-corner monument
description -- e.g. "FOUND ... BRASS CAP BEING THE 1/4 CORNER OF SECTIONS
18 AND 17 ... TOWNSHIP 22 NORTH, RANGE 21 EAST, M.D.M." -- printed on a US
survey plat into a real lat/lon, via BLM's public PLSS CadNSDI REST
service (gis.blm.gov/arcgis/rest/services/Cadastral/BLM_Natl_PLSS_CadNSDI).

SCOPE, DELIBERATELY NARROW:

- Only the ~30 US states inside the Public Land Survey System. The
  original 13 colonies, Texas, Hawaii, and a few others use metes-and-
  bounds or other survey systems with no township/range/section grid at
  all -- there is nothing for this module to resolve there, and it
  returns None rather than ever being asked to guess.
- Only REGULAR (non-fractional) sections, whose BLM polygon is square
  enough that its 4 bounding-box extremes ARE its 4 real corners. A
  section next to a meridian, baseline, or an old land grant can be
  irregular; _section_corners refuses (returns None) whenever the
  polygon's actual corners don't land within tolerance of its bbox
  extremes, rather than confidently returning a wrong corner.
- Only resolves a corner the document itself names (a quarter corner of
  one or two named sections, or a named NE/NW/SE/SW section corner).
  Never invents or guesses which corner is "probably" meant.

Two-step query, both against the specific attribute filters BLM's service
actually supports (confirmed empirically against the live service: layer 1
("PLSS Township") accepts an arbitrary WHERE clause; layer 2 ("PLSS
Section") only accepts PLSSID + FRSTDIVNO -- any other attribute filter on
it returns a 400):

  1. Layer 1: STATEABBR + PRINMER (meridian name) + township/range number
     + direction -> that township's canonical PLSSID (also confirms the
     township genuinely exists in BLM's data, rather than trusting the
     document's own OCR'd numbers blindly).
  2. Layer 2: PLSSID + FRSTDIVNO (section number) -> that section's
     polygon, in WGS84.

A "quarter corner" (the usual monument named on a division/display map --
it sits on a section LINE, not at one of a section's own 4 corners) is
derived geometrically from the resolved polygon(s), not read off some
extra ring vertex BLM's polygon happens to include -- see
_quarter_corner / _shared_edge_midpoint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx
from pyproj import Geod

_GEOD = Geod(ellps="WGS84")
_BASE = "https://gis.blm.gov/arcgis/rest/services/Cadastral/BLM_Natl_PLSS_CadNSDI/MapServer"
_TOWNSHIP_LAYER = f"{_BASE}/1/query"
_SECTION_LAYER = f"{_BASE}/2/query"
_HTTP_TIMEOUT = 10.0

# A section's real corners, read off BLM's own polygon, must land within
# this of its bounding-box extremes to be trusted as a REGULAR section.
# ~0.0008 deg is ~80m -- generous enough for a normal 1-mile section
# (whose actual corners sit far closer to the bbox than that), but tight
# enough to reject a genuinely fractional/irregular one.
_REGULAR_SECTION_TOLERANCE_DEG = 0.0008

# Two independently-resolved monuments on the same sheet must land within
# this of each other to count as mutually corroborating (rather than two
# "successful" lookups that happen to be unrelated) -- a generous
# township-scale bound (~10 miles), not a tight precision check.
_CORROBORATION_MAX_SEPARATION_M = 16_000.0

_STATE_ABBR = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "FLORIDA": "FL", "GEORGIA": "GA",
    "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA",
    "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
    "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV", "WISCONSIN": "WI",
    "WYOMING": "WY",
}

# Common US principal meridian abbreviations as printed on plats -> BLM's
# own PRINMER name for it. Not exhaustive (there are ~35 nationally); an
# abbreviation not in this table returns None rather than a guess.
_MERIDIAN_ABBR = {
    "MDM": "Mount Diablo Meridian",
    "WM": "Willamette Meridian",
    "BM": "Boise Meridian",
    "SLM": "Salt Lake Meridian",
    "SLBM": "Salt Lake Meridian",
    "UM": "Uintah Meridian",
    "NMM": "New Mexico Principal Meridian",
    "NMPM": "New Mexico Principal Meridian",
    "GSRM": "Gila-Salt River Meridian",
    "SBM": "San Bernardino Meridian",
    "SBBM": "San Bernardino Meridian",
    "HM": "Humboldt Meridian",
    "MDBM": "Mount Diablo Meridian",
    "6PM": "Sixth Principal Meridian",
    "FIFTH PM": "Fifth Principal Meridian",
    "5PM": "Fifth Principal Meridian",
    "4PM": "Fourth Principal Meridian",
    "3PM": "Third Principal Meridian",
    "2PM": "Second Principal Meridian",
    "1PM": "First Principal Meridian",
    "IM": "Indian Meridian",
    "CM": "Cimarron Meridian",
    "LM": "Louisiana Meridian",
    "PMM": "Principal Meridian Montana",
    "MPM": "Principal Meridian Montana",
    "BHM": "Black Hills Meridian",
}

#  OCR on real documents emits the full-width comma "，" in place of a
#  plain "," surprisingly often (confirmed on the Patnaude packet) --
#  treated as an ordinary separator everywhere a plain comma would be.
_SEP = r"[,，\s]*"
_TRM_RE = re.compile(
    r"TOWNSHIP\s+(\d{1,3})\s*(NORTH|SOUTH)" + _SEP + r"RANGE\s+(\d{1,3})\s*(EAST|WEST)" + _SEP +
    r"([A-Z][A-Z.\s]{0,45}MERIDIAN|[A-Z]{1,5}\.?[A-Z]\.?[A-Z]?\.?)",
    re.IGNORECASE,
)
# "T. 9 N., R. 5 W., B.M." -- the abbreviated form most plat headings use (OCR often turns the
# periods into doubled ones or drops the spaces: "T.9 N.. R.5W.. B.M.").
_SEPD = r"[.,，\s]*"  # also periods: OCR doubles them ("N.. R.5W.. B.M.")
_TRM_ABBR_RE = re.compile(
    r"\bT\.?\s*(\d{1,3})\s*([NS])\b" + _SEPD + r"R\.?\s*(\d{1,3})\s*([EW])\b" + _SEPD +
    r"((?:[A-Z]\.?\s?&?\s?){1,5})(?![A-Za-z])",
    re.IGNORECASE,
)
# Sixteenth-section corners as plats name them. Measured on the Payette ROS: the printed distance
# from the "NE1/16 corner" to the "CE1/16 corner" of Section 34 is 1,305.74 ft, a quarter of the
# section side -- so NE1/16 is the centre of the NE 1/4 and CE1/16 the midpoint between the section
# centre and the E 1/4 corner. (u east fraction, v north fraction of the section.)
_SIXTEENTH = {
    "NE": (0.75, 0.75), "NW": (0.25, 0.75), "SE": (0.75, 0.25), "SW": (0.25, 0.25),
    "CN": (0.5, 0.75), "CE": (0.75, 0.5), "CS": (0.5, 0.25), "CW": (0.25, 0.5),
}
_SIXTEENTH_RE = re.compile(
    r"\b(NE|NW|SE|SW|CN|CE|CS|CW)\s*1/16\s*CORNER\s+(?:OF\s+)?(?:SAID\s+|THE\s+)?SECTION\s*(\d{1,2})\b", re.IGNORECASE
)
_TWO_SECTION_QUARTER_RE = re.compile(
    r"1/4\s*CORNER\s+OF\s+SECTIONS?\s+(\d{1,2})\s+AND\s+(\d{1,2})", re.IGNORECASE
)
_ONE_SECTION_QUARTER_RE = re.compile(
    r"\b(NORTH|SOUTH|EAST|WEST|N|S|E|W)\s*(?:1/4|ONE[- ]?QUARTER|QUARTER)\s*CORNER\s+"
    r"(?:OF\s+)?(?:SAID\s+|THE\s+)?SECTION\s+(\d{1,2})\b", re.IGNORECASE
)
_FULL_CORNER_RE = re.compile(
    r"\b(NORTHEAST|NORTHWEST|SOUTHEAST|SOUTHWEST|NE|NW|SE|SW)\s+CORNER\s+"
    r"(?:OF\s+)?(?:SAID\s+|THE\s+)?SECTION\s+(\d{1,2})\b", re.IGNORECASE
)
_FULL_CORNER_CODE = {"NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW"}


def _normalize_meridian(raw: str) -> str | None:
    if "MERIDIAN" in raw.upper():
        # "...Range 5 West of the Boise Meridian": the lead-in is not part of the name.
        cleaned = re.sub(r"^\s*(?:OF\s+THE\s+|OF\s+|THE\s+)", "", raw.strip(), flags=re.IGNORECASE)
        # "Gila and Salt River Base and Meridian" -> BLM's "Gila and Salt River Meridian"
        cleaned = re.sub(r"\bBASE\s+(?:AND|&)\s+", "", cleaned, flags=re.IGNORECASE)
        words = [w.capitalize() for w in cleaned.split()]
        return " ".join(w.lower() if w in ("And", "Of") else w for w in words)
    key = re.sub(r"[.\s&]", "", raw).upper()
    return _MERIDIAN_ABBR.get(key)


@dataclass
class _TRS:
    township_no: int
    township_dir: str
    range_no: int
    range_dir: str
    meridian_name: str


def _find_trs(text: str) -> _TRS | None:
    for rx in (_TRM_RE, _TRM_ABBR_RE):
        for m in rx.finditer(text):
            meridian = _normalize_meridian(m.group(5))
            if meridian is not None:
                return _trs_from(m, meridian)
    return None


def _trs_from(m, meridian: str) -> _TRS:
    return _TRS(
        township_no=int(m.group(1)), township_dir=m.group(2)[0].upper(),
        range_no=int(m.group(3)), range_dir=m.group(4)[0].upper(),
        meridian_name=meridian,
    )


def _query_township(state_abbr: str, trs: _TRS, meridian: str | None) -> list[dict]:
    where = (
        f"STATEABBR='{state_abbr}' AND "
        + (f"PRINMER='{meridian}' AND " if meridian else "")
        + f"TWNSHPNO='{trs.township_no:03d}' AND TWNSHPDIR='{trs.township_dir}' AND "
        f"RANGENO='{trs.range_no:03d}' AND RANGEDIR='{trs.range_dir}'"
    )
    resp = httpx.get(
        _TOWNSHIP_LAYER,
        params={"where": where, "outFields": "PLSSID", "returnGeometry": "false", "f": "json"},
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("features") or []


def _resolve_township_plssid(state_abbr: str, trs: _TRS) -> str | None:
    feats = _query_township(state_abbr, trs, trs.meridian_name)
    if not feats:
        # BLM spells meridians its own way ("Gila-Salt River Meridian"); the printed name may
        # differ. A state/township/range with exactly ONE match is unambiguous without it.
        feats = _query_township(state_abbr, trs, None)
    if len(feats) != 1:
        return None
    return feats[0]["attributes"]["PLSSID"]


_ring_cache: dict[tuple[str, int], list[tuple[float, float]] | None] = {}


def _fetch_section_ring(plssid: str, section_no: int) -> list[tuple[float, float]] | None:
    cache_key = (plssid, section_no)
    if cache_key in _ring_cache:
        return _ring_cache[cache_key]
    where = f"PLSSID='{plssid}' AND FRSTDIVNO='{section_no:02d}'"
    resp = httpx.get(
        _SECTION_LAYER,
        params={
            "where": where, "outFields": "FRSTDIVNO", "returnGeometry": "true",
            "outSR": "4326", "f": "json",
        },
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    feats = resp.json().get("features") or []
    ring = None
    if len(feats) == 1:
        rings = feats[0].get("geometry", {}).get("rings") or []
        if rings:
            ring = [(p[0], p[1]) for p in rings[0]]
    _ring_cache[cache_key] = ring
    return ring


def _section_corners(ring: list[tuple[float, float]]) -> dict[str, tuple[float, float]] | None:
    """
    The 4 real corners of a REGULAR section, read off its own polygon --
    refuses (returns None) if the section looks fractional/irregular (its
    nearest-to-bbox-extreme points aren't actually close to those
    extremes), rather than returning a confident-looking wrong corner.
    """

    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    minlon, maxlon, minlat, maxlat = min(lons), max(lons), min(lats), max(lats)

    def nearest(lon: float, lat: float) -> tuple[float, float]:
        return min(ring, key=lambda p: (p[0] - lon) ** 2 + (p[1] - lat) ** 2)

    corners = {
        "NW": nearest(minlon, maxlat), "NE": nearest(maxlon, maxlat),
        "SW": nearest(minlon, minlat), "SE": nearest(maxlon, minlat),
    }
    for key, (clon, clat) in corners.items():
        want_lon = minlon if "W" in key else maxlon
        want_lat = maxlat if key[0] == "N" else minlat
        if abs(clon - want_lon) > _REGULAR_SECTION_TOLERANCE_DEG or abs(clat - want_lat) > _REGULAR_SECTION_TOLERANCE_DEG:
            return None
    return corners


_EDGE_CORNERS = {"N": ("NW", "NE"), "S": ("SW", "SE"), "E": ("NE", "SE"), "W": ("NW", "SW")}


def _midpoint(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


def _close(a: tuple[float, float], b: tuple[float, float], tol: float) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _resolve_one_section_quarter(plssid: str, section_no: int, direction: str) -> tuple[float, float] | None:
    ring = _fetch_section_ring(plssid, section_no)
    if ring is None:
        return None
    corners = _section_corners(ring)
    if corners is None:
        return None
    a, b = _EDGE_CORNERS[direction]
    return _midpoint(corners[a], corners[b])


def _resolve_sixteenth(plssid: str, section_no: int, name: str) -> tuple[float, float] | None:
    ring = _fetch_section_ring(plssid, section_no)
    corners = _section_corners(ring) if ring else None
    if corners is None:
        return None
    u, v = _SIXTEENTH[name]
    sw, se, nw, ne = corners["SW"], corners["SE"], corners["NW"], corners["NE"]
    return (
        sw[0] * (1 - u) * (1 - v) + se[0] * u * (1 - v) + nw[0] * (1 - u) * v + ne[0] * u * v,
        sw[1] * (1 - u) * (1 - v) + se[1] * u * (1 - v) + nw[1] * (1 - u) * v + ne[1] * u * v,
    )


def _resolve_full_corner(plssid: str, section_no: int, direction: str) -> tuple[float, float] | None:
    ring = _fetch_section_ring(plssid, section_no)
    if ring is None:
        return None
    corners = _section_corners(ring)
    if corners is None:
        return None
    return corners[direction]


def _resolve_two_section_quarter(
    plssid: str, section_a: int, section_b: int, tol: float = _REGULAR_SECTION_TOLERANCE_DEG
) -> tuple[float, float] | None:
    ring_a, ring_b = _fetch_section_ring(plssid, section_a), _fetch_section_ring(plssid, section_b)
    if ring_a is None or ring_b is None:
        return None
    corners_a, corners_b = _section_corners(ring_a), _section_corners(ring_b)
    if corners_a is None or corners_b is None:
        return None
    pairs = (("NW", "NE"), ("SW", "SE"), ("NE", "SE"), ("NW", "SW"))
    for pa in pairs:
        for pb in pairs:
            a1, a2 = corners_a[pa[0]], corners_a[pa[1]]
            b1, b2 = corners_b[pb[0]], corners_b[pb[1]]
            if _close(a1, b1, tol) and _close(a2, b2, tol):
                return _midpoint(_midpoint(a1, b1), _midpoint(a2, b2))
            if _close(a1, b2, tol) and _close(a2, b1, tol):
                return _midpoint(_midpoint(a1, b2), _midpoint(a2, b1))
    return None  # the two named sections don't actually share an edge in BLM's data


# Aliquot part of a section as plats print it: "NORTH 1/2 OF THE SOUTH 1/2
# OF SECTION 17", "NE 1/4 OF SECTION 9". OCR often drops the "1/" of a half
# or quarter symbol ("NORTH 2 OF THE SOUTH 2", confirmed on the Patnaude
# packet), so a bare 2 / 4 is accepted in the same position.
_ALIQUOT_PART = (
    r"(?:\b(?:NORTH|SOUTH|EAST|WEST)\s*(?:1/2|\u00bd|2)"
    r"|\b[NSEW]\s*(?:1/2|\u00bd)"
    r"|\b(?:NE|NW|SE|SW)\s*(?:1/4|\u00bc|4))"
)
_ALIQUOT_RE = re.compile(
    r"((?:" + _ALIQUOT_PART + r"\s+OF\s+(?:THE\s+)?)+)SECTION\s+(\d{1,2})\b", re.IGNORECASE
)
_PORTION_RE = re.compile(r"PORTION\s+OF|PART\s+OF|LYING\s+(?:IN|WITHIN)|SITUATE[D]?\s+IN|LOCATED\s+IN|BEING\s+IN", re.IGNORECASE)
_ALIQUOT_ABBR = {"N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST"}
_ALIQUOT_PART_RE = re.compile(r"(NORTH|SOUTH|EAST|WEST|NE|NW|SE|SW|N|S|E|W)\s*(?:1/2|\u00bd|2|1/4|\u00bc|4)", re.IGNORECASE)


def _apply_aliquot(box: list[float], part: str) -> None:
    u0, u1, v0, v1 = box
    um, vm = (u0 + u1) / 2, (v0 + v1) / 2
    p = part.upper()
    if p in ("NORTH", "NE", "NW"):
        box[2] = vm
    if p in ("SOUTH", "SE", "SW"):
        box[3] = vm
    if p in ("EAST", "NE", "SE"):
        box[0] = um
    if p in ("WEST", "NW", "SW"):
        box[1] = um


def _bilinear(corners: dict[str, tuple[float, float]], u: float, v: float) -> tuple[float, float]:
    sw, se, nw, ne = corners["SW"], corners["SE"], corners["NW"], corners["NE"]
    return (
        sw[0] * (1 - u) * (1 - v) + se[0] * u * (1 - v) + nw[0] * (1 - u) * v + ne[0] * u * v,
        sw[1] * (1 - u) * (1 - v) + se[1] * u * (1 - v) + nw[1] * (1 - u) * v + ne[1] * u * v,
    )


def _resolve_aliquot(plssid: str, text: str) -> dict | None:
    """
    The aliquot part the plat's legal description names (e.g. N 1/2 of S 1/2
    of Section 17), as a WGS84 polygon from the section's own BLM corners,
    with its area. Applied innermost-first, as written right to left.
    """

    m = _ALIQUOT_RE.search(text)
    if not m:
        return None
    parts = [_ALIQUOT_ABBR.get(t.upper(), t.upper()) for t in _ALIQUOT_PART_RE.findall(m.group(1))]
    section_no = int(m.group(2))
    ring = _fetch_section_ring(plssid, section_no)
    corners = _section_corners(ring) if ring else None
    if not parts or corners is None:
        return None
    box = [0.0, 1.0, 0.0, 1.0]  # u0, u1 (west->east), v0, v1 (south->north)
    for part in reversed(parts):
        _apply_aliquot(box, part)
    u0, u1, v0, v1 = box
    polygon = [_bilinear(corners, u, v) for u, v in ((u0, v1), (u1, v1), (u1, v0), (u0, v0))]
    area_m2, _ = _GEOD.polygon_area_perimeter([p[0] for p in polygon], [p[1] for p in polygon])
    words = " of ".join(f"{p.upper()} {'1/4' if len(p) == 2 else '1/2'}" for p in parts)
    lead = text[max(0, m.start() - 60):m.start()]
    return {
        # "...being a PORTION OF the W1/2 of the NE1/4 of Section 34": the parcels are only part of it.
        "portion_of": bool(_PORTION_RE.search(lead)),
        "description": f"{words} of Section {section_no}",
        "polygon": [list(p) for p in polygon],
        "acres": abs(area_m2) / 4046.8564224,
    }


@dataclass
class PLSSAnchor:
    lat: float
    lon: float
    description: str
    # Full confidence only once a SECOND, independently-named monument on
    # the same sheet also resolves and lands near the first -- the same
    # single-source/cross-validated discipline calibration.py already
    # applies to bearing/distance evidence. A lone resolved corner is real
    # (BLM's own data, not a guess) but single-source until something else
    # on the sheet ties back to it.
    corroborated: bool
    notes: list[str] = field(default_factory=list)
    # The aliquot part the legal description names, if any -- see
    # _resolve_aliquot. Used to fit the sheet's confirmed parcels, as a
    # group, onto the land the description says they occupy.
    aliquot: dict | None = None


def resolve_plss_anchor(pages_result: list[dict], state_name: str) -> PLSSAnchor | None:
    """
    Scans this document's own OCR'd text for a PLSS township/range/
    meridian and any section-corner monument(s) named relative to it, and
    resolves the FIRST one found against BLM's PLSS CadNSDI service.
    `state_name` is the state the document's coarse (geocoded) anchor
    already landed in -- the same role it plays in
    georeference.find_surveyed_coordinates, reused here rather than
    re-deriving the state from free text.

    Returns None whenever anything along the way doesn't resolve cleanly
    (no PLSS state match, no T/R/S found, the township doesn't exist in
    BLM's data, the named section(s) don't exist or are irregular, or
    named sections turn out not to actually share an edge) -- this module
    never falls back to a guess of its own.
    """

    state_abbr = _STATE_ABBR.get(state_name.strip().upper())
    if state_abbr is None:
        return None

    text = "\n".join(r.get("ocr_text") or "" for p in pages_result for r in p.get("regions", []))
    trs = _find_trs(text)
    if trs is None:
        return None

    try:
        plssid = _resolve_township_plssid(state_abbr, trs)
    except (httpx.HTTPError, KeyError, ValueError):
        return None
    if plssid is None:
        return None

    resolved: list[tuple[str, tuple[float, float]]] = []
    try:
        for m in _TWO_SECTION_QUARTER_RE.finditer(text):
            a, b = int(m.group(1)), int(m.group(2))
            point = _resolve_two_section_quarter(plssid, a, b)
            if point:
                resolved.append((f"1/4 corner of Sections {a} and {b}", point))
        seen_q: set[tuple[str, int]] = set()
        for m in _ONE_SECTION_QUARTER_RE.finditer(text):
            d, s = m.group(1).upper()[0], int(m.group(2))
            if (d, s) in seen_q:
                continue  # a deed names the same monument on several pages
            seen_q.add((d, s))
            point = _resolve_one_section_quarter(plssid, s, d)
            if point:
                resolved.append((f"{d}1/4 corner of Section {s}", point))
        seen_16 = set()
        for m in _SIXTEENTH_RE.finditer(text):
            name, sec = m.group(1).upper(), int(m.group(2))
            if (name, sec) in seen_16:
                continue  # a plat names the same monument several times
            seen_16.add((name, sec))
            point = _resolve_sixteenth(plssid, sec, name)
            if point:
                resolved.append((f"{name}1/16 corner of Section {sec}", point))
        seen_c: set[tuple[str, int]] = set()
        for m in _FULL_CORNER_RE.finditer(text):
            d, s = m.group(1).upper(), int(m.group(2))
            d = _FULL_CORNER_CODE.get(d, d)
            if (d, s) in seen_c:
                continue
            seen_c.add((d, s))
            point = _resolve_full_corner(plssid, s, d)
            if point:
                resolved.append((f"{d} corner of Section {s}", point))
    except (httpx.HTTPError, KeyError, ValueError):
        pass  # whatever resolved before the failure is still real evidence

    if not resolved:
        return None

    primary_desc, (lon, lat) = resolved[0]
    corroborated = False
    if len(resolved) >= 2:
        distances_m = [_GEOD.inv(lon, lat, p[0], p[1])[2] for _, p in resolved[1:]]
        corroborated = all(d <= _CORROBORATION_MAX_SEPARATION_M for d in distances_m)

    try:
        aliquot = _resolve_aliquot(plssid, text)
    except (httpx.HTTPError, KeyError, ValueError):
        aliquot = None

    return PLSSAnchor(
        lat=lat, lon=lon, description=primary_desc, corroborated=corroborated,
        notes=[f"BLM PLSS CadNSDI: {desc}" for desc, _ in resolved],
        aliquot=aliquot,
    )
