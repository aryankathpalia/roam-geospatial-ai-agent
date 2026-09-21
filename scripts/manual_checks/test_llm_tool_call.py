import json

from openai import OpenAI

from app.agent.tools import calculate_area_tool, execute_tool, get_tool_schema
from app.core.config import settings


# 1. Create an OpenRouter client.
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=settings.OPENROUTER_API_KEY,
)


# 2. This is the geometry of Sector 17 that we already retrieved.
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


# 3. Give the model a question and the tool it is allowed to use.
response = client.chat.completions.create(
    model=settings.OPENROUTER_MODEL,
    messages=[
        {
            "role": "system",
            "content": (
                "You are ROAM, a GIS assistant. "
                "Use the calculate_area tool when it is appropriate."
            ),
        },
        {
            "role": "user",
            "content": (
                "Calculate the area of this GeoJSON polygon "
                "in square kilometres:\n"
                f"{json.dumps(sector_17_geometry)}"
            ),
        },
    ],
    tools=[
        get_tool_schema(calculate_area_tool)
    ],
    tool_choice="auto",
)


# 4. Inspect exactly what the model returned.
# 4. Get the model's message.
message = response.choices[0].message

print("\n===== MODEL TOOL REQUEST =====")
print("Tool calls:", message.tool_calls)


# 5. Execute the requested tool.
if message.tool_calls:

    tool_call = message.tool_calls[0]

    tool_name = tool_call.function.name
    arguments = json.loads(tool_call.function.arguments)

    print("\n===== TOOL REQUEST =====")
    print("Tool:", tool_name)
    print("Arguments:", arguments)

    result = execute_tool(
        name=tool_name,
        arguments=arguments,
    )

    print("\n===== TOOL RESULT =====")
    print(result)


    # 6. Send the tool result back to the LLM.
    messages = [
        {
            "role": "system",
            "content": (
                "You are ROAM, a GIS assistant. "
                "Give concise, factual answers based on tool results."
            ),
        },
        {
            "role": "user",
            "content": (
                "Calculate the area of this GeoJSON polygon "
                "in square kilometres:\n"
                f"{json.dumps(sector_17_geometry)}"
            ),
        },
        message,
        {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": str(result),
        },
    ]


    # 7. Ask the LLM to produce the final answer.
    final_response = client.chat.completions.create(
        model=settings.OPENROUTER_MODEL,
        messages=messages,
    )

    final_message = final_response.choices[0].message

    print("\n===== FINAL LLM ANSWER =====")
    print(final_message.content)