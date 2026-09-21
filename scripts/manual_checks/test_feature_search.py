import asyncio

from app.schemas.geospatial_feature import GeospatialFeature
from app.services.feature_search import get_features_in_area
from app.services.spatial_analysis import (
    calculate_area_km2,
    count_features_in_area,
)


async def main():
    boundary = GeospatialFeature(
        name="Sector 17",
        feature_type="boundary",
        geometry={
            "type": "Polygon",
            "coordinates": [[
                [76.7909706, 30.7404678],
                [76.7856494, 30.7466288],
                [76.7754147, 30.7402531],
                [76.7742476, 30.7397398],
                [76.7797155, 30.7334212],
                [76.7909706, 30.7404678],
            ]],
        },
        properties={
            "name": "Sector 17",
            "boundary": "administrative",
        },
        source="openstreetmap",
        source_id="relation/7893056",
    )

    # Calculate the area of Sector 17.
    area_km2 = calculate_area_km2(boundary)

    # Retrieve hospitals inside Sector 17.
    hospitals = await get_features_in_area(
        boundary=boundary,
        feature_type="hospital",
    )

    # Count hospitals that are actually inside the boundary.
    count = count_features_in_area(
        boundary=boundary,
        features=hospitals,
    )

    print(f"Sector 17 area: {area_km2:.3f} km²")
    print(f"Hospitals inside Sector 17: {count}")
    print(f"Found {len(hospitals)} hospitals")

    for hospital in hospitals:
        print(hospital.model_dump())


if __name__ == "__main__":
    asyncio.run(main())