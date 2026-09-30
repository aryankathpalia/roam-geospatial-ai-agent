"""Trace 27211ae5 p13 and 798850fc p28's stated-acreage extraction."""
import io
import sys

sys.path.insert(0, ".")

from PIL import Image

from app.services import vision

TARGETS = {
    "27211ae5_p13": ("27211ae5-0099-4b0d-8176-f1483897b766", 13, [125, 117, 2717, 1592]),
    "798850fc_p28": ("798850fc-1be3-4bc6-9f53-1441c39cbd04", 28, [133, 288, 1502, 1207]),
}

client = vision._get_client()
for name, (doc, page, bbox) in TARGETS.items():
    x, y, w, h = bbox
    im = Image.open(f"data/documents/{doc}/pages/page_{page:03d}.png").convert("RGB").crop((x, y, x + w, y + h))
    im = vision.upright(im)
    tiles = vision._tile_image(im)
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
    open(f"scratch_diag/{name}_notes.txt", "w", encoding="utf-8").write(notes)
    print(f"=== {name} NOTES saved ===")

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
    open(f"scratch_diag/{name}_structured.json", "w", encoding="utf-8").write(resp2.text)
    print(f"=== {name} STRUCTURED ===")
    print(resp2.text)
