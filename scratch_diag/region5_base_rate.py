"""
Diagnostic ONLY. Measures region 5 (65be453d p8) true base success
rate: region's own tiles in one tile-read call (1 region/call), then
production structuring + resolvers. 16 independent runs. Metric =
usable post-structuring calls (bearing AND distance), not regex.
"""

import io
import json
import math
import sys

sys.path.insert(0, ".")

from PIL import Image
from google.genai import types

from app.services import vision
from app.services.geometry import resolve_ambiguous_calls, drop_conflicting_axis_duplicates

CROP = "scratch_diag/zero_call_investigation/65be453d-f014-4024-84d6-60a2d0052aeb_region5_p8.png"
N = 16

client = vision._get_client()
tiles = vision._tile_image(Image.open(CROP).convert("RGB"))
parts = []
for t in tiles:
    buf = io.BytesIO()
    t.save(buf, format="PNG")
    parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))
read_prompt = vision._DOCUMENT_TILE_PROMPT_TEMPLATE.format(
    region_count=1, region_layout=f"Region 1: {len(tiles)} piece(s)"
)


def call(fn):
    for attempt in range(3):
        try:
            return fn()
        except Exception as e:
            print(f"    retry after error: {e}")
    raise RuntimeError("3 failures")


counts = []
for i in range(1, N + 1):
    vision._wait_for_rate_limit()
    notes = call(lambda: client.models.generate_content(
        model=vision.settings.GEMINI_MODEL,
        contents=parts + [read_prompt],
        config=types.GenerateContentConfig(temperature=0),
    )).text.strip()

    vision._wait_for_rate_limit()
    resp = call(lambda: client.models.generate_content(
        model=vision.settings.GEMINI_MODEL,
        contents=[vision._BATCH_STRUCTURE_PROMPT.format(
            region_count=1, region_list="Region 1", notes=notes)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=vision._BATCH_RESPONSE_SCHEMA,
            temperature=0,
        ),
    ))
    parsed = json.loads(resp.text)
    total = 0
    per_parcel = []
    for p in parsed.get("parcels", []):
        c = p.get("boundary_calls") or []
        if c and p.get("ambiguous_alternates"):
            c = resolve_ambiguous_calls(c, p["ambiguous_alternates"])
        if c:
            c = drop_conflicting_axis_duplicates(c)
        per_parcel.append((p.get("parcel_label"), len(c)))
        total += len(c)
    counts.append(total)
    print(f"run {i}: total usable calls={total}  per parcel={per_parcel}", flush=True)


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


for thr, name in [(1, ">=1 usable call"), (3, ">=3 usable calls (walkable)")]:
    k = sum(1 for c in counts if c >= thr)
    lo, hi = wilson(k, N)
    print(f"{name}: {k}/{N} = {k/N:.0%}  (95% Wilson CI {lo:.0%}-{hi:.0%})")
print("counts:", counts)
