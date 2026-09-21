import asyncio

from app.agent.agent import ROAMAgent


async def main():
    agent = ROAMAgent()

    answer = await agent.run(
        "Where is Sector 17 Chandigarh?"
    )

    print("\n===== ROAM ANSWER =====")
    print(answer)


asyncio.run(main())