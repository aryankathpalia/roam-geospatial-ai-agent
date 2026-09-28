"""Geocoding service for resolving place names into ROAM Locations."""

import asyncio
import logging
from typing import Any

import httpx

from app.core.config import settings
from app.schemas.location import Location

logger = logging.getLogger(__name__)


class GeocodingError(Exception):
    """Raised when the geocoding provider cannot be reached or returns an invalid response."""


async def geocode_place(query: str, limit: int = 5) -> list[Location]:
    """Resolve a place/query using Nominatim.

    Returns multiple candidates because a geographic name may be ambiguous.
    """

    query = query.strip()

    if not query:
        raise ValueError("Geocoding query cannot be empty.")

    if limit < 1 or limit > 10:
        raise ValueError("Geocoding limit must be between 1 and 10.")

    url = f"{settings.NOMINATIM_BASE_URL.rstrip('/')}/search"

    params = {
        "q": query,
        "format": "jsonv2",
        "limit": limit,
        "addressdetails": 1,
    }

    headers = {
        "User-Agent": settings.NOMINATIM_USER_AGENT,
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                url,
                params=params,
                headers=headers,
            )
            response.raise_for_status()

    except httpx.HTTPError as exc:
        raise GeocodingError(f"Nominatim request failed: {exc}") from exc

    try:
        results: list[dict[str, Any]] = response.json()
    except ValueError as exc:
        raise GeocodingError("Nominatim returned invalid JSON.") from exc

    locations: list[Location] = []

    for result in results:
        try:
            latitude = float(result["lat"])
            longitude = float(result["lon"])
        except (KeyError, TypeError, ValueError):
            # Do not create a Location without valid coordinates.
            continue

        address = result.get("address") or {}

        locations.append(
            Location(
                name=result.get("display_name"),
                latitude=latitude,
                longitude=longitude,
                address=result.get("display_name"),
                city=(
                    address.get("city")
                    or address.get("town")
                    or address.get("municipality")
                    or address.get("village")
                ),
                region=(
                    address.get("state")
                    or address.get("state_district")
                    or address.get("region")
                ),
                country=address.get("country"),
                source="nominatim",
                source_id=(
                    str(result["place_id"])
                    if result.get("place_id") is not None
                    else None
                ),
                provider_metadata={
                "osm_type": result.get("osm_type"),
                "osm_id": result.get("osm_id"),
                "category": result.get("category"),
                "type": result.get("type"),
            }
            )
        )

    return locations


def _coarsen(query: str) -> list[str]:
    """
    Progressively drops the leading comma-separated segment of a query
    ("700 Center Ave, Payette, ID 83661" -> "Payette, ID 83661"). Used
    as a last-resort retry when a candidate's full text fails to
    geocode -- a typo or OCR garble almost always lands in the
    street-level detail (the first segment), not in the city/state/
    zip that follows, so dropping it sidesteps the bad text entirely
    rather than needing to know what was wrong with it.

    Stops at 2 remaining segments, deliberately never coarsening down
    to a single bare fragment ("83661" alone, or a stray number with
    no place-name context) -- confirmed as a real regression: an
    ultra-short, context-free query matches SOMETHING in Nominatim's
    global database almost every time, confidently returning a wrong-
    country result (a scale-ratio number matched a Czech Republic
    postal code; another document's garbled OCR matched an address in
    Japan) instead of correctly finding nothing.
    """

    segments = [s.strip() for s in query.split(",") if s.strip()]
    return [", ".join(segments[i:]) for i in range(1, len(segments) - 1)]


async def geocode_anchor(
    candidates: list[str], limit: int = 1
) -> tuple[list[Location], str | None]:
    """
    Tries each candidate query in order (best first), and for the
    first one only, falls back to progressively coarser versions of it
    if it fails to geocode -- before moving on to the next distinct
    candidate. Returns (results, the query that matched) as soon as
    anything geocodes; ([], None) if nothing does. The matched query is
    returned so the caller can judge how precise the anchor is (a
    street address vs. just a city or ZIP). A GeocodingError on one candidate (network/provider issue)
    is logged and treated the same as a zero-result response, so one
    bad candidate can't block trying the rest.

    This exists because a single best-scored candidate failing to
    geocode previously meant the whole document got NO anchor at all,
    even when an equally real, just coarser, candidate existed right
    next to it in the same OCR text (confirmed on a real document: the
    winning address line had one OCR-garbled word and failed outright,
    while the plat's own "City, County, State" line, sitting in the
    same text block, geocoded cleanly).

    Not filtered by country -- this pipeline isn't US-only. The actual
    defense against a wrong-country false match (confirmed to happen:
    a scale-ratio number matched a Czech postal code, garbled OCR
    matched an address in Japan) is upstream, in
    georeference.MIN_CANDIDATE_SCORE: a candidate only reaches this
    function at all if it had real corroborating signal (an explicit
    address label, or a ZIP/postal code together with a state/county
    hint), not just one weak, globally-ambiguous match like a bare
    5-digit number. See that constant's docstring for why a country
    allowlist was tried first and rejected.
    """

    if not candidates:
        return [], None

    queries = [candidates[0], *_coarsen(candidates[0]), *candidates[1:]]

    for attempt, query in enumerate(queries):
        if attempt > 0:
            # Nominatim's usage policy caps public requests at 1/second;
            # a single geocode_place call already respected that
            # implicitly by only ever firing once per document. The
            # cascade can now fire several times for one document, so
            # this makes that explicit rather than risking a 429 (or
            # an IP block for sustained bulk use) partway through.
            await asyncio.sleep(1.0)
        try:
            results = await geocode_place(query, limit=limit)
        except GeocodingError as exc:
            logger.warning("Anchor geocode failed for %r: %s", query, exc)
            continue
        if results:
            return results, query

    return [], None