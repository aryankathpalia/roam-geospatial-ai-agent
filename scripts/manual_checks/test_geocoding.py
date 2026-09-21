import asyncio

from app.services.geocoding import geocode_place


async def main():
    results = await geocode_place("Sector 17 Chandigarh")

    for location in results:
        print(location.model_dump())


if __name__ == "__main__":
    asyncio.run(main())