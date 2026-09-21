"""Throwaway: draw ground-truth boxes on a page image for visual spot-check."""

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COLORS = {
    "Text": (70, 130, 180),
    "Table": (218, 165, 32),
    "Picture": (128, 128, 128),
    "Seal": (186, 85, 211),
    "ParcelMap": (34, 139, 34),
    "ScannedPrintout": (220, 20, 60),
}

doc_id, page_file = sys.argv[1], sys.argv[2]

ann = json.load(open(f"data/annotations/{doc_id}/instances_default.json"))
cats = {c["id"]: c["name"] for c in ann["categories"]}
image_entry = next(i for i in ann["images"] if i["file_name"] == page_file)
boxes = [a for a in ann["annotations"] if a["image_id"] == image_entry["id"]]

img = Image.open(f"data/documents/{doc_id}/pages/{page_file}").convert("RGB")
draw = ImageDraw.Draw(img)

for b in boxes:
    name = cats[b["category_id"]]
    x, y, w, h = b["bbox"]
    color = COLORS.get(name, (0, 0, 0))
    draw.rectangle([x, y, x + w, y + h], outline=color, width=4)
    draw.rectangle([x, y - 28, x + 8 + len(name) * 11, y], fill=color)
    draw.text((x + 4, y - 26), name, fill=(255, 255, 255))

out_dir = Path("data/annotations/_preview")
out_dir.mkdir(parents=True, exist_ok=True)
out_path = out_dir / f"{doc_id}_{page_file}"
img.save(out_path)
print(out_path, "boxes:", len(boxes))
