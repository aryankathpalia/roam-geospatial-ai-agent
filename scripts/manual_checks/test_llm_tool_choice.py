import asyncio
import json

from openai import OpenAI

from app.agent.tools import (
    calculate_area_tool,
    execute_tool,
    geocode_place_tool_definition,
    get_tool_schema,
)
from app.core.config import settings


# Connect to OpenRouter.
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=settings.OPENROUTER_API_KEY,
)


# Give Qwen all currently available ROAM tools.
tools = [
    get_tool_schema(calculate_area_tool),
    get_tool_schema(geocode_place_tool_definition),
]


# The user's question.
user_message = "Where is Sector 17 Chandigarh?"


messages = [
    {
        "role": "system",
        "content": (
            "You are ROAM, a spatial intelligence assistant. "
            "Use the available tools when they are appropriate. "
            "After receiving a tool result, answer the user's question "
            "using the information returned by the tool."
        ),
    },
    {
        "role": "user",
        "content": user_message,
    },
]


# First LLM call: let the model decide what to do.
response = client.chat.completions.create(
    model=settings.OPENROUTER_MODEL,
    messages=messages,
    tools=tools,
    tool_choice="auto",
)


message = response.choices[0].message

print("\n===== FIRST LLM RESPONSE =====")
print("Content:", message.content)
print("Tool calls:", message.tool_calls)


# Add the assistant's response to the conversation.
messages.append(message)


# Execute the tool selected by the LLM.
if message.tool_calls:

    for tool_call in message.tool_calls:

        tool_name = tool_call.function.name
        arguments = json.loads(tool_call.function.arguments)

        print("\n===== AGENT TOOL REQUEST =====")
        print("Tool:", tool_name)
        print("Arguments:", arguments)

        tool_result = asyncio.run(
            execute_tool(
                name=tool_name,
                arguments=arguments,
            )
        )

        print("\n===== TOOL RESULT =====")
        print(tool_result)

        # Give the result back to the LLM.
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(tool_result),
            }
        )


    # Second LLM call: produce the final answer.
    final_response = client.chat.completions.create(
        model=settings.OPENROUTER_MODEL,
        messages=messages,
    )

    final_message = final_response.choices[0].message

    print("\n===== FINAL LLM ANSWER =====")
    print(final_message.content)