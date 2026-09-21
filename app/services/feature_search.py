"""Retrieve geospatial features from OpenStreetMap."""

from typing import Any

import httpx
from shapely.geometry import shape

from app.core.config import settings
from app.schemas import query
from app.schemas.geospatial_feature import GeospatialFeature


class FeatureSearchError(Exception):
    """Raised when geospatial feature retrieval fails."""


async def get_features_in_area(
    boundary: GeospatialFeature,
    feature_type: str,
) -> list[GeospatialFeature]:
    """Retrieve OSM features of a given type inside a GeoJSON boundary."""

    if not boundary.geometry:
        raise ValueError("Boundary geometry cannot be empty.")

    if not feature_type.strip():
        raise ValueError("Feature type cannot be empty.")

    polygon = shape(boundary.geometry)

    if polygon.is_empty or not polygon.is_valid:
        raise ValueError("Boundary geometry must be a valid polygon.")

    # GeoJSON uses (longitude, latitude).
    # Overpass's polygon filter expects:
    # latitude longitude latitude longitude ...
    coordinates = list(polygon.exterior.coords)

    polygon_string = " ".join(
        f"{lat} {lon}"
        for lon, lat in coordinates
    )

    query = f"""
    [out:json][timeout:25];

    (
    node["amenity"="{feature_type}"](poly:"{polygon_string}");
    way["amenity"="{feature_type}"](poly:"{polygon_string}");
    );

    out center;
    """

    headers = {
        "User-Agent": settings.NOMINATIM_USER_AGENT,
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                settings.OVERPASS_URL,
                data={"data": query},
                headers=headers,
            )
            response.raise_for_status()

    except httpx.HTTPError as exc:
        raise FeatureSearchError(
            f"Overpass feature request failed: {exc}"
        ) from exc

    try:
        data: dict[str, Any] = response.json()
    except ValueError as exc:
        raise FeatureSearchError(
            "Overpass returned invalid JSON."
        ) from exc

    features: list[GeospatialFeature] = []

    for element in data.get("elements", []):
        geometry = _extract_geometry(element)

        if geometry is None:
            continue

        tags = element.get("tags", {})

        features.append(
            GeospatialFeature(
                name=tags.get("name"),
                feature_type=feature_type,
                geometry=geometry,
                properties=tags,
                source="openstreetmap",
                source_id=f"{element.get('type')}/{element.get('id')}",
            )
        )

    return features


def _extract_geometry(element: dict[str, Any]) -> dict[str, Any] | None:
    """Convert an Overpass element into a GeoJSON geometry."""

    element_type = element.get("type")

    if element_type == "node":
        lat = element.get("lat")
        lon = element.get("lon")

        if lat is None or lon is None:
            return None

        return {
            "type": "Point",
            "coordinates": [float(lon), float(lat)],
        }

    center = element.get("center")

    if center:
        lat = center.get("lat")
        lon = center.get("lon")

        if lat is not None and lon is not None:
            return {
                "type": "Point",
                "coordinates": [float(lon), float(lat)],
            }

    return None