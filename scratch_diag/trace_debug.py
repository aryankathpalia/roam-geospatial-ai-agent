"""
One-off diagnostic trace, NOT part of the production pipeline. Does NOT
modify vision.py/geometry.py/document_pipeline.py. Purpose: find the
FIRST stage where a known-bad value gets associated with Parcel 1's
boundary, by calling Gemini once PER TILE (production batches all
tiles into one call, which hides per-tile attribution) and recording
everything at each stage.
"""

import io
import json
import sys

sys.path.insert(0, ".")

from PIL import Image, ImageDraw
from google.genai import types

from app.services import vision
from app.services.geometry import resolve_ambiguous_calls, walk_traverse
from app.services.spatial_validation import validate_traverse

TARGET_VALUES = ["220.00", "1077.91", "2637.36", "325.30", "329.80", "336.41"]

img = Image.open("scratch_diag/crop_p9.png").convert("RGB")
width, height = img.size
print(f"=== 1. CROP DIMENSIONS ===\n{width} x {height} px\n")

# --- replicate _tile_image's exact grid math to get real bboxes ---
tile_size = 700
cols = max(1, round(width / tile_size))
rows = max(1, round(height / tile_size))
tile_w, tile_h = width / cols, height / rows

tile_bboxes = []
for row in range(rows):
    for col in range(cols):
        x0, y0 = int(col * tile_w), int(row * tile_h)
        x1 = width if col == cols - 1 else int((col + 1) * tile_w)
        y1 = height if row == rows - 1 else int((row + 1) * tile_h)
        tile_bboxes.append((x0, y0, x1, y1))

print(f"=== 2. TILE GRID: {cols} cols x {rows} rows = {len(tile_bboxes)} tiles ===")
tiles = []
for i, (x0, y0, x1, y1) in enumerate(tile_bboxes, start=1):
    tile_img = img.crop((x0, y0, x1, y1))
    tiles.append(tile_img)
    tile_path = f"scratch_diag/trace_tiles/tile_{i}.png"
    tile_img.save(tile_path)
    print(f"  Tile {i}: bbox=({x0},{y0})-({x1},{y1})  size={x1-x0}x{y1-y0}  saved={tile_path}")

# --- overview image with tile grid overlaid on the original crop ---
overview = img.copy()
draw = ImageDraw.Draw(overview)
for i, (x0, y0, x1, y1) in enumerate(tile_bboxes, start=1):
    draw.rectangle([x0, y0, x1 - 1, y1 - 1], outline=(255, 0, 0), width=4)
    draw.text((x0 + 10, y0 + 10), f"Tile {i}", fill=(255, 0, 0))
overview.save("scratch_diag/trace_tiles/overview_grid.png")
print("  Overview with grid saved: scratch_diag/trace_tiles/overview_grid.png\n")

client = vision._get_client()

# Mirrors the production tile-read prompt (vision.py's
# _DOCUMENT_TILE_PROMPT_TEMPLATE) after adding the tie/monument/
# parentheses/road-frontage context rules directly to the tile-read
# stage, instead of leaving them only in the later structuring prompt.
PER_TILE_PROMPT = vision._DOCUMENT_TILE_PROMPT_TEMPLATE.format(
    region_count=1, region_layout="Region 1: 1 piece (this image)"
)

print("=== 3+4+5. PER-TILE RAW GEMINI OUTPUT ===")
per_tile_raw = []
for i, tile_img in enumerate(tiles, start=1):
    buf = io.BytesIO()
    tile_img.save(buf, format="PNG")
    part = types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png")

    vision._wait_for_rate_limit()
    resp = client.models.generate_content(
        model=vision.settings.GEMINI_MODEL,
        contents=[part, PER_TILE_PROMPT],
        config=types.GenerateContentConfig(temperature=0),
    )
    text = resp.text.strip()
    per_tile_raw.append(text)
    print(f"\n--- Tile {i} raw output ---")
    print(text)

    with open(f"scratch_diag/trace_tiles/tile_{i}_raw.txt", "w", encoding="utf-8") as f:
        f.write(text)

# --- which tile each target value appears in ---
print("\n=== TARGET VALUE -> TILE ATTRIBUTION (from per-tile raw output) ===")
value_tile_map = {v: [] for v in TARGET_VALUES}
for i, text in enumerate(per_tile_raw, start=1):
    for v in TARGET_VALUES:
        if v in text:
            value_tile_map[v].append(i)

for v, tile_list in value_tile_map.items():
    print(f"  {v}  ->  tile(s) {tile_list if tile_list else 'NOT FOUND in any single-tile read'}")

# --- assemble combined notes in the SAME format production's combined
# multi-tile call would produce, so the structuring call downstream
# sees an equivalent input (labeled "Region 1, Piece N:") ---
notes_lines = [f"Region 1, Piece {i}:\n{text}" for i, text in enumerate(per_tile_raw, start=1)]
combined_notes = "\n\n".join(notes_lines)

print("\n=== 6. COMBINED NOTES TEXT PASSED TO STRUCTURING CALL ===")
print(combined_notes)
with open("scratch_diag/trace_tiles/combined_notes.txt", "w", encoding="utf-8") as f:
    f.write(combined_notes)

structure_prompt = vision._BATCH_STRUCTURE_PROMPT.format(
    region_count=1, region_list="Region 1", notes=combined_notes
)

vision._wait_for_rate_limit()
structure_response = client.models.generate_content(
    model=vision.settings.GEMINI_MODEL,
    contents=[structure_prompt],
    config=types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=vision._BATCH_RESPONSE_SCHEMA,
        temperature=0,
    ),
)

print("\n=== 7. RAW STRUCTURING CALL OUTPUT ===")
print(structure_response.text)
with open("scratch_diag/trace_tiles/structuring_raw.json", "w", encoding="utf-8") as f:
    f.write(structure_response.text)

parsed = json.loads(structure_response.text)

print("\n=== PARSED PARCELS ===")
for item in parsed:
    print(f"\n{item.get('parcel_label')} (region {item.get('region_index')}) stated_acres={item.get('stated_area_acres')}")
    for c in item.get("boundary_calls", []):
        print(f"  boundary_call: {c['bearing']}  {c['distance']}")
    for a in item.get("ambiguous_alternates", []) or []:
        print(f"  ambiguous_alternate: {a['bearing']}  {a['distance']}")

# focus on Parcel 1 for the resolver + final trace
parcel1 = next((p for p in parsed if p.get("parcel_label") and "1" in p["parcel_label"]), parsed[0])
calls = parcel1.get("boundary_calls") or []
alternates = parcel1.get("ambiguous_alternates") or []

print("\n=== 8. AFTER resolve_ambiguous_calls ===")
resolved = resolve_ambiguous_calls(calls, alternates) if alternates else calls
if resolved == calls:
    print("  (no change -- no alternates to resolve, or resolver made no change)")
for c in resolved:
    print(f"  {c['bearing']}  {c['distance']}")

print("\n=== 9. FINAL GEOMETRY / VALIDATION (Parcel 1) ===")
if resolved:
    traverse = walk_traverse(resolved)
    validation = validate_traverse(traverse, "", stated_area_acres=parcel1.get("stated_area_acres"))
    print(json.dumps(validation, indent=2))
else:
    print("  no boundary_calls survived -- cannot walk traverse")

# --- explicit per-value trace table ---
print("\n=== EXPLICIT VALUE-BY-VALUE TRACE ===")
struct_calls_by_value = {}
for item in parsed:
    for c in item.get("boundary_calls", []):
        struct_calls_by_value[c["distance"].rstrip("'")] = ("boundary_calls", item.get("parcel_label"))
    for a in item.get("ambiguous_alternates", []) or []:
        struct_calls_by_value.setdefault(a["distance"].rstrip("'"), ("ambiguous_alternates", item.get("parcel_label")))

resolved_values = {c["distance"].rstrip("'") for c in resolved}

for v in TARGET_VALUES:
    tile_list = value_tile_map[v]
    struct_info = struct_calls_by_value.get(v, ("NOT PRESENT in structuring output", None))
    final = "USED in final traverse" if v in resolved_values else "EXCLUDED from final traverse"
    print(f"\n  {v}:")
    print(f"    seen in tile(s):        {tile_list if tile_list else 'none'}")
    print(f"    structuring classified: {struct_info[0]}  (parcel_label={struct_info[1]})")
    print(f"    final:                  {final}")
