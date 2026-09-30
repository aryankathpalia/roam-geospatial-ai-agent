import sys
sys.path.insert(0, ".")
from PIL import Image
from app.services import vision
from google import genai
from google.genai import types
import io

img = Image.open("scratch_diag/crop_p9.png")
client = vision._get_client()
tiles = vision._tile_image(img)
parts = []
for t in tiles:
    buf = io.BytesIO()
    t.convert("RGB").save(buf, format="PNG")
    parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))

prompt = vision._DOCUMENT_TILE_PROMPT_TEMPLATE.format(
    region_count=1, region_layout="Region 1: %d piece(s)" % len(tiles)
)
resp = client.models.generate_content(
    model=vision.settings.GEMINI_MODEL,
    contents=parts + [prompt],
    config=types.GenerateContentConfig(temperature=0),
)
print(resp.text)
with open("scratch_diag/notes_latest.txt", "w", encoding="utf-8") as f:
    f.write(resp.text)
