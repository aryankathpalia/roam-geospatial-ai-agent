"""POI (Point of Interest) schema: a rich, source-grounded place profile."""

from typing import Optional

from pydantic import BaseModel, Field


class POIMetadata(BaseModel):
    """Optional enrichment fields that only some sources provide.

    Kept as a nested, fully-optional block rather than flattened onto POI so
    it's obvious at a glance which fields are "core" vs "may not exist for
    this source".
    """

    cuisine: Optional[str] = None
    opening_hours: Optional[str] = None
    wheelchair_access: Optional[str] = None
    website: Optional[str] = None
    phone: Optional[str] = None
    rating: Optional[float] = Field(None, ge=0, le=5)
    review_count: Optional[int] = Field(None, ge=0)


class POI(BaseModel):
    """A point of interest, e.g. a restaurant, park, or landmark.

    Do not assume every source provides every field — only `id`, `name`,
    coordinates, and provenance (`source`) are required.
    """

    id: Optional[str] = Field(None, description="ROAM-internal identifier, if assigned.")
    name: str
    category: Optional[str] = Field(None, description="e.g. 'restaurant', 'museum', 'park'.")

    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)

    address: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None

    tags: list[str] = Field(default_factory=list)
    description: Optional[str] = None

    metadata: Optional[POIMetadata] = None

    source: str = Field(..., description="System this POI was retrieved from, e.g. 'osm', 'google_places'.")
    source_id: Optional[str] = Field(None, description="Identifier of this record within the source system.")
