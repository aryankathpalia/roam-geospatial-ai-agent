import asyncio

from app.agent.tools import (
    calculate_area_tool,
    execute_tool,
    geocode_place_tool_definition,
    get_tool_schema,
)


sector_17_geometry = {
    "type": "Polygon",
    "coordinates": [[
        [76.7909706, 30.7404678],
        [76.7856494, 30.7466288],
        [76.7754147, 30.7402531],
        [76.7742476, 30.7397398],
        [76.7797155, 30.7334212],
        [76.7909706, 30.7404678],
    ]],
}


async def main():

    # Test 1: synchronous tool
    result = await execute_tool(
        name="calculate_area",
        arguments={
            "geometry": sector_17_geometry,
        },
    )

    print("===== CALCULATE AREA =====")
    print("Result:", result)


    # Test 2: asynchronous tool
    locations = await execute_tool(
        name="geocode_place",
        arguments={
            "query": "Sector 17 Chandigarh",
            "limit": 5,
        },
    )

    print("\n===== GEOCODE PLACE =====")
    print("Results:")

    for location in locations:
        print(location)


    # Test 3: LLM-compatible tool schemas
    print("\n===== CALCULATE AREA SCHEMA =====")
    print(get_tool_schema(calculate_area_tool))

    print("\n===== GEOCODE PLACE SCHEMA =====")
    print(get_tool_schema(geocode_place_tool_definition))


asyncio.run(main())