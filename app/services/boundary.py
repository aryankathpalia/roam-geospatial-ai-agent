"""OSM boundary retrieval and geometry normalization."""

from typing import Any

import httpx
from shapely.geometry import Polygon, mapping

from app.core.config import settings
from app.schemas.geospatial_feature import GeospatialFeature


class BoundaryError(Exception):
    """Raised when a geographic boundary cannot be retrieved or constructed."""


async def get_osm_boundary(
    osm_type: str,
    osm_id: int,
) -> GeospatialFeature:
    """Retrieve an OSM relation boundary and convert it to GeoJSON geometry.

    This first implementation handles polygonal OSM relations composed of
    outer ways.
    """

    if osm_type != "relation":
        raise ValueError(
            "Boundary retrieval currently requires an OSM relation."
        )

    query = f"""
    [out:json];
    relation({osm_id});
    out geom;
    """

    headers = {
        "User-Agent": settings.NOMINATIM_USER_AGENT,
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                settings.OVERPASS_URL,
                data={"data": query},
                headers=headers,
            )
            response.raise_for_status()

    except httpx.HTTPError as exc:
        raise BoundaryError(
            f"Overpass request failed: {exc}"
        ) from exc

    try:
        data: dict[str, Any] = response.json()
    except ValueError as exc:
        raise BoundaryError(
            "Overpass returned invalid JSON."
        ) from exc

    elements = data.get("elements", [])

    if not elements:
        raise BoundaryError(
            f"OSM relation {osm_id} was not found."
        )

    relation = elements[0]

    outer_ways = [
        member
        for member in relation.get("members", [])
        if member.get("type") == "way"
        and member.get("role") == "outer"
    ]

    if not outer_ways:
        raise BoundaryError(
            f"OSM relation {osm_id} contains no outer boundary ways."
        )

    coordinates = _assemble_outer_ring(outer_ways)

    if len(coordinates) < 4:
        raise BoundaryError(
            f"OSM relation {osm_id} does not contain enough points "
            "to form a polygon."
        )

    polygon = Polygon(coordinates)

    if polygon.is_empty or not polygon.is_valid:
        raise BoundaryError(
            f"Could not construct a valid polygon for OSM relation {osm_id}."
        )

    geometry = mapping(polygon)

    tags = relation.get("tags", {})

    return GeospatialFeature(
        name=tags.get("name"),
        feature_type="boundary",
        geometry=geometry,
        properties=tags,
        source="openstreetmap",
        source_id=f"relation/{osm_id}",
    )


def _assemble_outer_ring(
    ways: list[dict[str, Any]],
) -> list[tuple[float, float]]:
    """Join OSM outer ways into one closed coordinate ring.

    OSM geometry points are latitude/longitude, while Shapely expects
    coordinates in x/y order, so we convert them to (longitude, latitude).
    """

    segments: list[list[tuple[float, float]]] = []

    for way in ways:
        geometry = way.get("geometry", [])

        points = [
            (
                float(point["lon"]),
                float(point["lat"]),
            )
            for point in geometry
            if "lat" in point and "lon" in point
        ]

        if len(points) >= 2:
            segments.append(points)

    if not segments:
        return []

    ring = segments.pop(0)

    while segments:
        connected = False

        for index, segment in enumerate(segments):
            if ring[-1] == segment[0]:
                ring.extend(segment[1:])
                segments.pop(index)
                connected = True
                break

            if ring[-1] == segment[-1]:
                segment.reverse()
                ring.extend(segment[1:])
                segments.pop(index)
                connected = True
                break

            if ring[0] == segment[-1]:
                ring = segment[:-1] + ring
                segments.pop(index)
                connected = True
                break

            if ring[0] == segment[0]:
                segment.reverse()
                ring = segment[:-1] + ring
                segments.pop(index)
                connected = True
                break

        if not connected:
            raise BoundaryError(
                "Could not connect all outer boundary ways into one ring."
            )

    if ring[0] != ring[-1]:
        ring.append(ring[0])

    return ring