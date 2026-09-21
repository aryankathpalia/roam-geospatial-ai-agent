"""Location schema: a resolved point in the world, generally the output of geocoding."""

from typing import Any, Optional

from pydantic import BaseModel, Field


class Location(BaseModel):
    """A named geographic point resolved from a source (e.g. a geocoder).

    Location is intentionally lightweight compared to POI — it represents
    "where is X", not a rich place profile. Fields are optional wherever a
    source may not provide them; we never invent missing values.
    """

    name: Optional[str] = Field(None, description="Human-readable name of the location, if known.")
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    address: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = Field(None, description="State/province/region.")
    country: Optional[str] = None

    source: str = Field(..., description="System that resolved this location, e.g. 'nominatim'.")
    source_id: Optional[str] = Field(None, description="Identifier of this record within the source system.")

    provider_metadata: dict[str, Any] = Field(
    default_factory=dict,
    description="Provider-specific identifiers or metadata retained for downstream geospatial operations.",
)
