"""
Builds a YOLO-format fine-tuning dataset from the 16 human-annotated
documents, ready to zip and upload to Kaggle.

Reads:  data/documents/<doc_id>/pages/page_*.png
        data/documents/<doc_id>/final_human_annotation/instances_default.json
Writes: data/finetune_dataset/
            images/train/*.png, images/val/*.png
            labels/train/*.txt, labels/val/*.txt   (YOLO format)
            data.yaml
            manifest.json   (which doc went to which split, for the record)

Split is by DOCUMENT, not by page, to avoid leaking a document's visual
style across train/val.
"""

import json
import shutil
from pathlib import Path

DOCUMENTS_ROOT = Path("data/documents")
OUTPUT_ROOT = Path("data/finetune_dataset")

CLASS_NAMES = ["Text", "Table", "Picture", "Seal", "ParcelMap", "ScannedPrintout"]

# Chosen to keep every rare class (ScannedPrintout, Seal, Picture) represented
# in both train and val, while keeping val a reasonable fraction of pages.
VAL_DOCS = {
    "65be453d-f014-4024-84d6-60a2d0052aeb",
    "db54d473-c7ab-4bec-bbf4-8ff9a1e3bd9b",
    "83ab22d0-b950-4f5d-ad33-5f0fd6105dbe",
    "a1136318-03ef-438d-9f02-8fcdcb5dca30",
    "2f896c95-b0f3-4a49-a447-4b3ca6129c27",
    "c56c0f0f-2b74-4d45-a7d8-b166f717624f",
    "27211ae5-0099-4b0d-8176-f1483897b766",
}


def coco_bbox_to_yolo(bbox: list[float], img_w: int, img_h: int) -> tuple[float, float, float, float]:
    x, y, w, h = bbox
    cx = (x + w / 2) / img_w
    cy = (y + h / 2) / img_h
    return cx, cy, w / img_w, h / img_h


def main() -> None:
    if OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)

    for split in ("train", "val"):
        (OUTPUT_ROOT / "images" / split).mkdir(parents=True, exist_ok=True)
        (OUTPUT_ROOT / "labels" / split).mkdir(parents=True, exist_ok=True)

    manifest = {"train_docs": [], "val_docs": [], "class_names": CLASS_NAMES}
    class_counts = {"train": {c: 0 for c in CLASS_NAMES}, "val": {c: 0 for c in CLASS_NAMES}}
    page_counts = {"train": 0, "val": 0}

    for doc_dir in sorted(DOCUMENTS_ROOT.iterdir()):
        ann_path = doc_dir / "final_human_annotation" / "instances_default.json"
        if not ann_path.exists():
            continue

        doc_id = doc_dir.name
        split = "val" if doc_id in VAL_DOCS else "train"
        manifest[f"{split}_docs"].append(doc_id)

        coco = json.load(open(ann_path))
        cat_id_to_name = {c["id"]: c["name"] for c in coco["categories"]}
        # Fixed class order/ids for YOLO, independent of this file's own category ids.
        name_to_yolo_id = {name: i for i, name in enumerate(CLASS_NAMES)}

        anns_by_image: dict[int, list[dict]] = {}
        for a in coco["annotations"]:
            anns_by_image.setdefault(a["image_id"], []).append(a)

        for img in coco["images"]:
            src_image = doc_dir / "pages" / img["file_name"]
            if not src_image.exists():
                raise FileNotFoundError(f"Missing rendered page: {src_image}")

            # Prefix with doc_id to keep filenames unique across documents.
            dst_stem = f"{doc_id}_{img['file_name'].rsplit('.', 1)[0]}"
            dst_image = OUTPUT_ROOT / "images" / split / f"{dst_stem}.png"
            dst_label = OUTPUT_ROOT / "labels" / split / f"{dst_stem}.txt"

            shutil.copy(src_image, dst_image)

            lines = []
            for a in anns_by_image.get(img["id"], []):
                class_name = cat_id_to_name[a["category_id"]]
                yolo_id = name_to_yolo_id[class_name]
                cx, cy, w, h = coco_bbox_to_yolo(a["bbox"], img["width"], img["height"])
                lines.append(f"{yolo_id} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
                class_counts[split][class_name] += 1

            dst_label.write_text("\n".join(lines))
            page_counts[split] += 1

    data_yaml = OUTPUT_ROOT / "data.yaml"
    data_yaml.write_text(
        "path: .\n"
        "train: images/train\n"
        "val: images/val\n"
        f"nc: {len(CLASS_NAMES)}\n"
        f"names: {CLASS_NAMES}\n"
    )

    manifest["page_counts"] = page_counts
    manifest["class_counts"] = class_counts
    (OUTPUT_ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print("Dataset written to", OUTPUT_ROOT)
    print("Pages:", page_counts)
    print("Train class counts:", class_counts["train"])
    print("Val class counts:", class_counts["val"])


if __name__ == "__main__":
    main()
