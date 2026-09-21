"""Route schema: a path between two points, as returned by a routing engine."""

from typing import Any, Optional

from pydantic import BaseModel, Field

from app.schemas.location import Location


class Route(BaseModel):
    """A calculated route between an origin and a destination.

    `geometry` is left as a permissive structure (e.g. GeoJSON LineString or
    an encoded polyline string) since we don't want to lock in a routing
    provider's exact shape yet.
    """

    origin: Location
    destination: Location

    distance_meters: Optional[float] = Field(None, ge=0)
    duration_seconds: Optional[float] = Field(None, ge=0)
    geometry: Optional[Any] = Field(
        None, description="Route geometry as provided by the source, e.g. GeoJSON or an encoded polyline."
    )

    source: str = Field(..., description="Routing engine/system this route came from, e.g. 'osrm', 'google_directions'.")
    source_id: Optional[str] = None
