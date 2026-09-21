"""
Document layout detection using ROAM's own fine-tuned model
(models/roam_layout_v1/roam_layout_best.pt), a DocLayout-YOLO backbone
fine-tuned on 16 human-annotated land-record documents (761 pages).

This replaces the earlier zero-shot pretrained-model approach. The
fine-tuned model predicts ROAM's 6 classes natively -- there is no
DocLayNet remapping step anymore, and it IS allowed to predict
Seal/ParcelMap/ScannedPrintout directly (the old model could not
distinguish these, so it only ever emitted a generic "Picture").
"""

from dataclasses import dataclass
from pathlib import Path

import torch
from PIL import Image

# Our own checkpoint, trusted -- restore PyTorch's pre-2.6 default so
# `doclayout_yolo`'s internal torch.load calls succeed. Same fix used in
# the Kaggle notebook and new_test_docs/run_finetuned_model.py.
_original_torch_load = torch.load


def _torch_load_weights_only_false(*args, **kwargs):
    kwargs.setdefault("weights_only", False)
    return _original_torch_load(*args, **kwargs)


torch.load = _torch_load_weights_only_false

from doclayout_yolo import YOLOv10  # noqa: E402

WEIGHTS_PATH = Path("models/roam_layout_v1/roam_layout_best.pt")

# ROAM's fixed label set / ids, matching the order the model was trained
# with (see app/pipeline/prepare_finetune_dataset.py CLASS_NAMES) and the
# CVAT project's label config, so COCO exports stay consistent.
ROAM_CATEGORIES: list[dict[str, int | str]] = [
    {"id": 1, "name": "Text"},
    {"id": 2, "name": "Table"},
    {"id": 3, "name": "Picture"},
    {"id": 4, "name": "Seal"},
    {"id": 5, "name": "ParcelMap"},
    {"id": 6, "name": "ScannedPrintout"},
]
ROAM_CATEGORY_IDS = {c["name"]: c["id"] for c in ROAM_CATEGORIES}
CLASS_NAMES = [c["name"] for c in ROAM_CATEGORIES]

# Below this confidence, a detection is flagged for review rather than
# trusted outright.
REVIEW_CONFIDENCE_THRESHOLD = 0.5

# Classes the held-out evaluation showed to be meaningfully weaker than
# the rest (see models/roam_layout_v1/metrics_summary.json). Flagged for
# review even at higher confidence until more training data closes the gap.
LOW_CONFIDENCE_CLASSES = {"ScannedPrintout"}


@dataclass
class Detection:
    roam_class: str
    confidence: float
    x: float
    y: float
    width: float
    height: float
    needs_review: bool


_model: YOLOv10 | None = None


def get_model() -> YOLOv10:
    """Load (and cache) ROAM's fine-tuned layout model."""

    global _model

    if _model is None:
        if not WEIGHTS_PATH.exists():
            raise FileNotFoundError(
                f"Fine-tuned layout model not found at {WEIGHTS_PATH}. "
                "Expected models/roam_layout_v1/roam_layout_best.pt."
            )
        _model = YOLOv10(str(WEIGHTS_PATH))

    return _model


def detect_page_layout(image_path: str, conf: float = 0.25) -> list[Detection]:
    """
    Run ROAM's fine-tuned layout model on one rendered page image.

    `needs_review` is True for low-confidence detections and for classes
    the held-out evaluation flagged as weaker (currently ScannedPrintout).
    """

    path = Path(image_path)

    if not path.exists():
        raise FileNotFoundError(f"Page image not found: {image_path}")

    model = get_model()
    results = model.predict(str(path), imgsz=1024, conf=conf, device="cpu", verbose=False)
    result = results[0]

    detections: list[Detection] = []

    if result.boxes is None:
        return detections

    for cls_id, conf_score, xyxy in zip(
        result.boxes.cls.tolist(),
        result.boxes.conf.tolist(),
        result.boxes.xyxy.tolist(),
    ):
        roam_class = CLASS_NAMES[int(cls_id)]
        x1, y1, x2, y2 = xyxy

        detections.append(
            Detection(
                roam_class=roam_class,
                confidence=float(conf_score),
                x=float(x1),
                y=float(y1),
                width=float(x2 - x1),
                height=float(y2 - y1),
                needs_review=(
                    conf_score < REVIEW_CONFIDENCE_THRESHOLD
                    or roam_class in LOW_CONFIDENCE_CLASSES
                ),
            )
        )

    return detections


def get_image_size(image_path: str) -> tuple[int, int]:
    """Return (width, height) for a rendered page image."""

    with Image.open(image_path) as image:
        return image.size
