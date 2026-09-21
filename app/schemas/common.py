"""Shared primitives used across ROAM geospatial schemas."""

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class SourceInfo(BaseModel):
    """Provenance for any piece of retrieved geographic data.

    Every object ROAM retrieves from an external tool/API must be able to
    say where it came from. We never fabricate geographic information, so
    this is mandatory on retrieved objects (not on user-authored input).
    """

    source: str = Field(..., description="Name of the originating system, e.g. 'nominatim', 'osm', 'google_places'.")
    source_id: Optional[str] = Field(None, description="Identifier of this record within the source system.")
    retrieved_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when this data was retrieved from the source.",
    )


class Coordinates(BaseModel):
    """A single lat/lon pair, kept separate so it can be reused/validated consistently."""

    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
