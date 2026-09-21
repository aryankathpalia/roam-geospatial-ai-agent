"""Tool definitions and execution for the ROAM agent."""

import inspect
from dataclasses import dataclass
from typing import Any, Callable

from app.agent.schemas import CalculateAreaInput, GeocodePlaceInput
from app.services.geocoding import geocode_place
from app.services.spatial_analysis import calculate_area_km2
@dataclass
class Tool:
    """A function exposed to the ROAM agent."""

    name: str
    description: str
    function: Callable[..., Any]
    input_schema: type


def calculate_area(
    geometry: dict[str, Any],
) -> float:
    """Calculate the area of a GeoJSON geometry."""

    from app.schemas.geospatial_feature import GeospatialFeature

    boundary = GeospatialFeature(
        name="agent_boundary",
        feature_type="boundary",
        geometry=geometry,
        properties={},
        source="agent",
    )

    return calculate_area_km2(boundary)


async def geocode_place_tool(
    query: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Find possible locations matching a place name."""

    locations = await geocode_place(
        query=query,
        limit=limit,
    )

    return [
        location.model_dump()
        for location in locations
    ]

geocode_place_tool_definition = Tool(
    name="geocode_place",
    description=(
        "Find possible geographic locations matching a place name "
        "or location query."
    ),
    function=geocode_place_tool,
    input_schema=GeocodePlaceInput,
)

calculate_area_tool = Tool(
    name="calculate_area",
    description=(
        "Calculate the area of a geographic boundary "
        "in square kilometres."
    ),
    function=calculate_area,
    input_schema=CalculateAreaInput,
)


TOOLS: dict[str, Tool] = {
    calculate_area_tool.name: calculate_area_tool,
    geocode_place_tool_definition.name: geocode_place_tool_definition,
}

async def execute_tool(
    name: str,
    arguments: dict[str, Any],
) -> Any:
    """Find a tool by name, validate its input, and execute it."""

    tool = TOOLS.get(name)

    if tool is None:
        raise ValueError(f"Unknown tool: {name}")

    validated_arguments = tool.input_schema.model_validate(arguments)

    result = tool.function(**validated_arguments.model_dump())

    if inspect.isawaitable(result):
        result = await result

    return result


def get_tool_schema(tool: Tool) -> dict[str, Any]:
    """Convert a ROAM tool into an LLM-compatible function schema."""

    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema.model_json_schema(),
        },
    }