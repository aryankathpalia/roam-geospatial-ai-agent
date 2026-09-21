"""GeospatialFeature: a generic geographic feature not covered by POI/Route.

Useful for things like boundaries, areas, or arbitrary GeoJSON-shaped
geographic data that doesn't fit the POI or Route contracts (e.g. a
neighborhood polygon, a park boundary).
"""

from typing import Any, Optional

from pydantic import BaseModel, Field


class GeospatialFeature(BaseModel):
    """A generic, loosely-typed geographic feature with provenance."""

    name: Optional[str] = None
    feature_type: Optional[str] = Field(None, description="e.g. 'boundary', 'area', 'neighborhood', 'polygon'.")
    geometry: Any = Field(..., description="Geometry payload, e.g. a GeoJSON geometry object.")
    properties: dict[str, Any] = Field(default_factory=dict, description="Arbitrary source-provided properties.")

    source: str
    source_id: Optional[str] = None
