import asyncio

from app.services.boundary import get_osm_boundary


async def main():
    feature = await get_osm_boundary(
        osm_type="relation",
        osm_id=7893056,
    )

    print(feature.model_dump())


if __name__ == "__main__":
    asyncio.run(main())