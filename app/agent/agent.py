"""ROAM agent orchestration."""

import json

from openai import OpenAI

from app.agent.tools import (
    calculate_area_tool,
    execute_tool,
    geocode_place_tool_definition,
    get_tool_schema,
)
from app.core.config import settings


class ROAMAgent:
    """Orchestrates LLM reasoning and ROAM tool execution."""

    def __init__(self) -> None:
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=settings.OPENROUTER_API_KEY,
        )

        self.tools = [
            get_tool_schema(calculate_area_tool),
            get_tool_schema(geocode_place_tool_definition),
        ]

    async def run(self, user_message: str) -> str:
        """Process a user message using the ROAM tool-calling loop."""

        messages = [
            {
                "role": "system",
                "content": (
                    "You are ROAM, a geospatial document intelligence and "
                    "spatial validation agent. Use the available tools when "
                    "they are appropriate."
                ),
            },
            {
                "role": "user",
                "content": user_message,
            },
        ]

        response = self.client.chat.completions.create(
            model=settings.OPENROUTER_MODEL,
            messages=messages,
            tools=self.tools,
            tool_choice="auto",
        )

        message = response.choices[0].message

        if not message.tool_calls:
            return message.content or ""

        messages.append(message)

        for tool_call in message.tool_calls:

            tool_name = tool_call.function.name

            arguments = json.loads(
                tool_call.function.arguments
            )

            tool_result = await execute_tool(
                name=tool_name,
                arguments=arguments,
            )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(tool_result),
                }
            )

        final_response = self.client.chat.completions.create(
            model=settings.OPENROUTER_MODEL,
            messages=messages,
        )

        return final_response.choices[0].message.content or ""