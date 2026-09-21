"""Observation: a record of what an external tool/API returned.

This is the wrapper that lets the agent (later) reason about "what did I
learn and from where" without losing provenance or timing.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field


class Observation(BaseModel):
    """A single piece of information retrieved by a tool call.

    `data` holds the structured payload (e.g. a POI, Route, or raw dict),
    left generic so Observation can wrap any of our other schemas without
    a union of every possible type.
    """

    tool: str = Field(..., description="Name of the tool/source that produced this observation, e.g. 'nominatim_geocode'.")
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    data: Any = Field(..., description="Structured payload returned by the tool, e.g. a POI, Route, or list thereof.")

    latitude: Optional[float] = Field(None, ge=-90, le=90, description="Relevant coordinate, if applicable.")
    longitude: Optional[float] = Field(None, ge=-180, le=180, description="Relevant coordinate, if applicable.")

    source: Optional[str] = Field(None, description="Underlying data source, if different from `tool`.")
    source_id: Optional[str] = None
