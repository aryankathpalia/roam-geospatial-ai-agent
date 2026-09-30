"""
Diagnostic ONLY. Re-derives the ParcelMap crops for the 3 documents
that produced zero-boundary-call regions, saves the crop images, and
captures RAW tile-read + structuring output (same method as the
2637.36 trace) to determine whether the failure is:
  (a) vision genuinely returned nothing for this parcel's boundary, or
  (b) upstream -- a bad/undersized crop from the layout detector, low
      resolution, or the wrong page/content entirely.
"""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from PIL import Image
from google.genai import types

from app.services.pdf_inspector import inspect_pdf
from app.services.pdf_renderer import render_page
from app.services.layout_detector_onnx import detect_page_layout
from app.services.region_cropper import extract_region_crops
from app.services import vision

DOC_ROOT = Path("data/documents")
OUT = Path("scratch_diag/zero_call_investigation")
OUT.mkdir(parents=True, exist_ok=True)

TARGET_DOCS = [
    "42f617a1-1d02-4f26-bf05-2a17b23f8fda",
    "65be453d-f014-4024-84d6-60a2d0052aeb",
    "83ab22d0-b950-4f5d-ad33-5f0fd6105dbe",
]

client = vision._get_client()

for doc_id in TARGET_DOCS:
    pdf_path = DOC_ROOT / doc_id / "original.pdf"
    inspection = inspect_pdf(str(pdf_path))
    page_count = inspection["page_count"]
    print(f"\n{'='*70}\n{doc_id} ({page_count} pages)\n{'='*70}")

    crops_info = []  # (page_num, bbox, region_img)
    for page_num in range(1, page_count + 1):
        out_path = OUT / f"{doc_id}_p{page_num:03d}.png"
        render_page(pdf_path=str(pdf_path), page_number=page_num, output_path=str(out_path), dpi=200)
        detections = detect_page_layout(str(out_path))
        crops = extract_region_crops(str(out_path), detections, save_dir=None, save_only_needs_review=False)
        page_img = Image.open(out_path).convert("RGB")
        for crop in crops:
            if crop.roam_class == "ParcelMap":
                x, y, w, h = crop.bbox
                region_img = page_img.crop((x, y, x + w, y + h))
                crops_info.append((page_num, crop.bbox, crop.confidence, region_img))

    print(f"ParcelMap crops: {len(crops_info)}")
    for i, (page_num, bbox, conf, region_img) in enumerate(crops_info, start=1):
        crop_path = OUT / f"{doc_id}_region{i}_p{page_num}.png"
        region_img.save(crop_path)
        print(f"  region {i}: page {page_num}, bbox={bbox}, confidence={conf}, "
              f"crop_size={region_img.size}, saved={crop_path}")

    if not crops_info:
        continue

    images = [c[3] for c in crops_info]
    tiles_per_region = [vision._tile_image(img) for img in images]

    all_parts = []
    region_layout_lines = []
    for idx, tiles in enumerate(tiles_per_region, start=1):
        region_layout_lines.append(f"Region {idx}: {len(tiles)} piece(s)")
        for tile in tiles:
            buf = io.BytesIO()
            tile.convert("RGB").save(buf, format="PNG")
            all_parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))

    read_prompt = vision._DOCUMENT_TILE_PROMPT_TEMPLATE.format(
        region_count=len(images), region_layout="\n".join(region_layout_lines)
    )
    vision._wait_for_rate_limit()
    read_resp = client.models.generate_content(
        model=vision.settings.GEMINI_MODEL,
        contents=all_parts + [read_prompt],
        config=types.GenerateContentConfig(temperature=0),
    )
    notes = read_resp.text.strip()
    notes_path = OUT / f"{doc_id}_raw_notes.txt"
    notes_path.write_text(notes, encoding="utf-8")
    print(f"\n  Raw tile-read notes saved to {notes_path} ({len(notes)} chars)")
    print(f"  --- notes preview ---")
    print(notes[:3000])

    structure_prompt = vision._BATCH_STRUCTURE_PROMPT.format(
        region_count=len(images),
        region_list=", ".join(f"Region {i}" for i in range(1, len(images) + 1)),
        notes=notes,
    )
    vision._wait_for_rate_limit()
    struct_resp = client.models.generate_content(
        model=vision.settings.GEMINI_MODEL,
        contents=[structure_prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=vision._BATCH_RESPONSE_SCHEMA,
            temperature=0,
        ),
    )
    struct_path = OUT / f"{doc_id}_structuring_raw.json"
    struct_path.write_text(struct_resp.text, encoding="utf-8")
    print(f"\n  Structuring output saved to {struct_path}")
    parsed = json.loads(struct_resp.text)
    for item in parsed:
        print(f"    region_index={item['region_index']} label={item.get('parcel_label')} "
              f"num_calls={len(item.get('boundary_calls', []))} stated_acres={item.get('stated_area_acres')}")
