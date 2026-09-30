"""Trace db54d473 p15's stated-acreage extraction: raw tile-read notes
first, then structuring output -- same method used all session."""
import io
import sys

sys.path.insert(0, ".")

from PIL import Image

from app.services import vision

DOC = "db54d473-c7ab-4bec-bbf4-8ff9a1e3bd9b"
x, y, w, h = 654, 187, 4637, 3615
im = Image.open(f"data/documents/{DOC}/pages/page_015.png").convert("RGB").crop((x, y, x + w, y + h))
im = vision.upright(im)
tiles = vision._tile_image(im)
print("n_tiles", len(tiles))

client = vision._get_client()
parts = []
for t in tiles:
    buf = io.BytesIO()
    t.convert("RGB").save(buf, format="PNG")
    parts.append(vision.types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))

prompt = vision._DOCUMENT_TILE_PROMPT_TEMPLATE.format(
    region_count=1, region_layout="Region 1: %d piece(s)" % len(tiles)
)
vision._wait_for_rate_limit()
resp = vision._generate_with_fallback(
    client, contents=parts + [prompt], config=vision.types.GenerateContentConfig(temperature=0)
)
notes = resp.text.strip()
open("scratch_diag/db54d473_p15_notes.txt", "w", encoding="utf-8").write(notes)
print("=== NOTES (saved to scratch_diag/db54d473_p15_notes.txt) ===")
print(notes)

print("\n=== STRUCTURE STEP ===")
structure_prompt = vision._BATCH_STRUCTURE_PROMPT.format(
    region_count=1, region_list="Region 1", notes=notes
)
vision._wait_for_rate_limit()
resp2 = vision._generate_with_fallback(
    client,
    contents=[structure_prompt],
    config=vision.types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=vision._BATCH_RESPONSE_SCHEMA,
        temperature=0,
    ),
)
open("scratch_diag/db54d473_p15_structured.json", "w", encoding="utf-8").write(resp2.text)
print(resp2.text)
