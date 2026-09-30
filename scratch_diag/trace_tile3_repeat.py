"""
Diagnostic ONLY. Sends TILE 3 ALONE (not all 6 tiles batched together)
20x to measure the real flicker rate of '1077.9' / '1077.91' / '1077.90'
under single-image (non-batched) conditions -- also directly answers
whether removing batching changes anything, since this call is
inherently unbatched (one image per request).
"""

import sys, io, re
sys.path.insert(0, ".")
from PIL import Image
from google.genai import types
from app.services import vision

img = Image.open("scratch_diag/trace_tiles/tile_3.png").convert("RGB")
buf = io.BytesIO()
img.save(buf, format="PNG")
part_bytes = buf.getvalue()

client = vision._get_client()
prompt = vision._DOCUMENT_TILE_PROMPT_TEMPLATE.format(
    region_count=1, region_layout="Region 1: 1 piece(s)"
)

N = 20
hits_any_1077 = 0
hits_exact_91 = 0
hits_exact_90 = 0
classified_as_tie = 0
missing = 0

for i in range(1, N + 1):
    vision._wait_for_rate_limit()
    part = types.Part.from_bytes(data=part_bytes, mime_type="image/png")
    resp = client.models.generate_content(
        model=vision.settings.GEMINI_MODEL,
        contents=[part, prompt],
        config=types.GenerateContentConfig(temperature=0),
    )
    text = resp.text.strip()
    with open(f"scratch_diag/trace_tiles/tile3_repeat_{i}.txt", "w", encoding="utf-8") as f:
        f.write(text)

    has_1077 = bool(re.search(r"1077\.9\d?", text))
    has_91 = "1077.91" in text
    has_90 = "1077.90" in text
    is_tie = has_1077 and bool(re.search(r"1077\.9\d?[^\n]{0,80}(tie|reference)", text, re.IGNORECASE))

    if has_1077:
        hits_any_1077 += 1
    else:
        missing += 1
    if has_91:
        hits_exact_91 += 1
    if has_90:
        hits_exact_90 += 1
    if is_tie:
        classified_as_tie += 1

    print(f"run {i}: found_1077x={has_1077}  exact_.91={has_91}  exact_.90={has_90}  classified_tie={is_tie}")

print(f"\n=== SUMMARY over {N} runs (tile 3 ALONE, unbatched) ===")
print(f"any '1077.9x' present: {hits_any_1077}/{N}")
print(f"exact '1077.91': {hits_exact_91}/{N}")
print(f"exact '1077.90' (misread): {hits_exact_90}/{N}")
print(f"classified as tie/reference (when present): {classified_as_tie}/{hits_any_1077 if hits_any_1077 else 1}")
print(f"missing entirely: {missing}/{N}")
