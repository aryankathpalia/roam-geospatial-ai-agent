"""Schemas used by ROAM agent tools."""

from typing import Any

from pydantic import BaseModel


class CalculateAreaInput(BaseModel):
    """Input required to calculate the area of a GeoJSON geometry."""

    geometry: dict[str, Any]


class GeocodePlaceInput(BaseModel):
    """Input required to geocode a place."""

    query: str
    limit: int = 5