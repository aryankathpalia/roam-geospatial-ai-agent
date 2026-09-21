import asyncio
import httpx

from app.core.config import settings


async def main():
    query = """
    [out:json][timeout:25];
    node
      ["amenity"="hospital"]
      (30.72, 76.76, 30.75, 76.80);
    out;
    """

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            settings.OVERPASS_URL,
            data={"data": query},
            headers={
                "User-Agent": settings.NOMINATIM_USER_AGENT,
                "Accept": "application/json",
            },
        )

        print("Status:", response.status_code)
        print("Elements:", len(response.json().get("elements", [])))


if __name__ == "__main__":
    asyncio.run(main())