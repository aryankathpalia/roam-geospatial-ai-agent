"""Geocoding service for resolving place names into ROAM Locations."""

from typing import Any

import httpx

from app.core.config import settings
from app.schemas.location import Location


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