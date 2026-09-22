"""
Vision escalation for ParcelMap crops -- reads a survey drawing and
extracts the structured data needed to reconstruct real geometry:
the boundary traverse (bearing/distance calls), any ground-coordinate
tie point, and the basis of bearings (coordinate system/zone), which
together are what let app/pipeline (not built yet) turn this into an
actual GeoJSON polygon instead of just flat OCR text.

Uses Gemini's structured-output mode (response_schema) rather than
free-text parsing -- the model enforces the JSON shape itself, so we
don't need to hand-roll a bearing/distance regex parser.

GEMINI_MODEL is env-configurable rather than hardcoded: Gemini's model
lineup moves fast (multiple new versions shipped in 2026 alone), so a
hardcoded model id would go stale. Get a free API key at
https://aistudio.google.com/apikey and set GEMINI_API_KEY in .env.
"""

import json
import os

from google import genai
from google.genai import types
from PIL import Image

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

_PROMPT = """\
You are reading a land survey / parcel map drawing. Extract the
following as JSON, using null for anything not present on the drawing:

- boundary_calls: the property boundary traverse, in order, as drawn.
  Each call is one bearing + distance pair, e.g. "N89*11'15"E" and
  "903.15'". Include every call that forms the parcel boundary.
- tie_point: a ground/state-plane coordinate explicitly given on the
  drawing (e.g. "GROUND COORDINATES N 14926910.28 E 2251599.70"), if
  present, with which boundary corner it anchors.
- basis_of_bearings: the stated coordinate system/datum/zone the
  bearings are referenced to (e.g. "Nevada State Plane, West Zone,
  NAD83"), if stated.
- parcel_label: the parcel/lot identifier and acreage if shown.

Only report what is actually legible on the drawing. Do not guess or
fill in typical values.
"""

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "boundary_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "bearing": {"type": "string"},
                    "distance": {"type": "string"},
                },
                "required": ["bearing", "distance"],
            },
        },
        "tie_point": {
            "type": "object",
            "nullable": True,
            "properties": {
                "northing": {"type": "string"},
                "easting": {"type": "string"},
                "anchors_corner": {"type": "string"},
            },
        },
        "basis_of_bearings": {"type": "string", "nullable": True},
        "parcel_label": {"type": "string", "nullable": True},
    },
    "required": ["boundary_calls"],
}


def _get_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Get a free key at "
            "https://aistudio.google.com/apikey and set it in .env."
        )
    return genai.Client(api_key=api_key)


def extract_parcel_geometry(image: Image.Image) -> dict:
    """
    Sends a ParcelMap crop to Gemini and returns the parsed structured
    result (see _RESPONSE_SCHEMA). Raises on API/parsing failure --
    callers should catch and degrade gracefully (this is an
    enhancement on top of OCR text, not a hard requirement).
    """

    import io

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")

    client = _get_client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/png"),
            _PROMPT,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_RESPONSE_SCHEMA,
        ),
    )

    return json.loads(response.text)
