"""
Generates draft (NOT ground-truth) annotations for a document's rendered
pages, for import into CVAT as a starting point for human review.

Output, under data/documents/<document_id>/annotated/:
  - coco_annotations.json  -- COCO 1.0 format, importable directly into a
                               CVAT task via Actions -> Upload Annotations.
  - review_manifest.json   -- per-page summary (does this page have any
                               Picture-class detection, i.e. does it need
                               careful review) so a human reviewer can
                               triage pages instead of reviewing all of
                               them at the same depth.

Every box in coco_annotations.json is a suggestion produced by ROAM's
fine-tuned layout model (models/roam_layout_v1). It has known weak spots
(see models/roam_layout_v1/metrics_summary.json -- ScannedPrintout in
particular) and will occasionally miss or mislabel a region. None of
this file is ground truth until a human has reviewed it in CVAT.
"""

import json
from pathlib import Path
from typing import Any

DOCUMENT_ROOT = Path("data/documents")


def generate_pre_annotations(document_id: str) -> dict[str, Any]:
    """
    Run the pretrained layout detector over every rendered page of a
    document and write draft COCO annotations + a review manifest.

    Requires that the document has already been processed (rendered
    pages must exist under data/documents/<document_id>/pages).

    Uses the PyTorch/doclayout_yolo layout detector (not the ONNX
    deployment path) since this is a CVAT-annotation-prep dev tool, not
    the served pipeline. Imported lazily, inside this function, so
    torch is only loaded into memory if this endpoint is actually
    called -- not on every app startup. That matters on a memory-capped
    deployment (e.g. Cloud Run's free tier), where the served pipeline
    (app/pipeline/processor.py) uses the much lighter ONNX path.
    """

    from app.services.layout_detector import (
        ROAM_CATEGORIES,
        ROAM_CATEGORY_IDS,
        detect_page_layout,
        get_image_size,
    )

    document_dir = DOCUMENT_ROOT / document_id
    pages_dir = document_dir / "pages"

    if not pages_dir.exists():
        raise FileNotFoundError(
            f"No rendered pages found for document {document_id}. "
            "Run the processing pipeline before generating pre-annotations."
        )

    page_files = sorted(pages_dir.glob("page_*.png"))

    if not page_files:
        raise FileNotFoundError(
            f"Pages directory exists but contains no rendered pages: {pages_dir}"
        )

    images: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    manifest_pages: list[dict[str, Any]] = []

    annotation_id = 1
    class_counts: dict[str, int] = {c["name"]: 0 for c in ROAM_CATEGORIES}

    for image_id, page_file in enumerate(page_files, start=1):
        width, height = get_image_size(str(page_file))

        images.append({
            "id": image_id,
            "file_name": page_file.name,
            "width": width,
            "height": height,
        })

        detections = detect_page_layout(str(page_file))

        page_needs_review = False
        page_classes: list[str] = []

        for detection in detections:
            category_id = ROAM_CATEGORY_IDS[detection.roam_class]

            annotations.append({
                "id": annotation_id,
                "image_id": image_id,
                "category_id": category_id,
                "bbox": [
                    round(detection.x, 2),
                    round(detection.y, 2),
                    round(detection.width, 2),
                    round(detection.height, 2),
                ],
                "area": round(detection.width * detection.height, 2),
                "iscrowd": 0,
                # Non-standard fields, ignored by CVAT's importer but
                # useful for the review manifest / debugging.
                "score": round(detection.confidence, 3),
                "model": "roam_layout_v1",
                "needs_review": detection.needs_review,
            })
            annotation_id += 1

            class_counts[detection.roam_class] += 1
            page_classes.append(detection.roam_class)

            if detection.needs_review:
                page_needs_review = True

        manifest_pages.append({
            "page_number": image_id,
            "file_name": page_file.name,
            "detected_classes": page_classes,
            "has_picture_detection": "Picture" in page_classes,
            "needs_review": page_needs_review,
        })

    coco = {
        "images": images,
        "annotations": annotations,
        "categories": ROAM_CATEGORIES,
    }

    annotated_dir = document_dir / "annotated"
    annotated_dir.mkdir(parents=True, exist_ok=True)

    coco_path = annotated_dir / "coco_annotations.json"
    manifest_path = annotated_dir / "review_manifest.json"

    coco_path.write_text(json.dumps(coco, indent=2))

    pages_needing_review = sum(1 for p in manifest_pages if p["needs_review"])

    manifest = {
        "document_id": document_id,
        "page_count": len(page_files),
        "pages_needing_review": pages_needing_review,
        "class_counts": class_counts,
        "note": (
            "AI-generated draft annotations from ROAM's fine-tuned layout "
            "model (roam_layout_v1). Every box is a suggestion, not ground "
            "truth. Boxes flagged needs_review=true (low confidence, or a "
            "class the held-out evaluation showed to be weaker, e.g. "
            "ScannedPrintout) need the closest review; pages with no "
            "detections still need a human pass to check for missed "
            "ParcelMap regions."
        ),
        "pages": manifest_pages,
    }

    manifest_path.write_text(json.dumps(manifest, indent=2))

    return {
        "annotated_dir": str(annotated_dir),
        "coco_annotations_path": str(coco_path),
        "review_manifest_path": str(manifest_path),
        "page_count": len(page_files),
        "pages_needing_review": pages_needing_review,
        "class_counts": class_counts,
    }
