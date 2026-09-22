"""
ONNX Runtime inference path for ROAM's fine-tuned layout model -- the
actual deployment target (models/roam_layout_v1/roam_layout_best.int8.onnx).

INT8 dynamic quantization was chosen over downsizing the input image
(1024px -> 640px) because it preserves accuracy: 640px measurably
degraded Seal (0.92 -> 0.28 confidence, dropped below the review
threshold) and lost Table/Picture detections outright. INT8 at the full
1024px resolution matched FP32 detections almost exactly across every
test page while cutting per-page inference cost.

Same interface as app.services.layout_detector (Detection dataclass),
so callers don't care which backend produced a detection.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

ONNX_WEIGHTS_PATH = Path("models/roam_layout_v1/roam_layout_best.int8.onnx")
INPUT_SIZE = 1024

CLASS_NAMES = ["Text", "Table", "Picture", "Seal", "ParcelMap", "ScannedPrintout"]

REVIEW_CONFIDENCE_THRESHOLD = 0.5
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


_session: ort.InferenceSession | None = None


def get_session() -> ort.InferenceSession:
    """Load (and cache) the quantized ONNX Runtime session."""

    global _session

    if _session is None:
        if not ONNX_WEIGHTS_PATH.exists():
            raise FileNotFoundError(
                f"Quantized ONNX model not found at {ONNX_WEIGHTS_PATH}. "
                "Export it first (see models/roam_layout_v1/export_onnx.py)."
            )
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # CUDAExecutionProvider is silently skipped by ONNX Runtime if
        # unavailable (plain CPU-only `onnxruntime` package, no GPU) --
        # safe to always list it first rather than branching on an env
        # var the way app/services/ocr.py has to for PaddleOCR.
        _session = ort.InferenceSession(
            str(ONNX_WEIGHTS_PATH),
            opts,
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )

    return _session


def _preprocess(image_path: str) -> tuple[np.ndarray, int, int]:
    with Image.open(image_path) as img:
        img = img.convert("RGB")
        orig_w, orig_h = img.size
        resized = img.resize((INPUT_SIZE, INPUT_SIZE))

    arr = np.asarray(resized).astype(np.float32) / 255.0
    arr = arr.transpose(2, 0, 1)[None, :, :, :]  # NCHW
    return arr, orig_w, orig_h


def detect_page_layout(image_path: str, conf: float = 0.25) -> list[Detection]:
    """
    Run the quantized ONNX layout model on one rendered page image.

    Model output is (1, 300, 6): pre-NMS'd [x1, y1, x2, y2, conf, cls] in
    1024x1024 model-input coordinates -- YOLOv10 is NMS-free by design.
    """

    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Page image not found: {image_path}")

    session = get_session()
    input_name = session.get_inputs()[0].name

    arr, orig_w, orig_h = _preprocess(str(path))
    output = session.run(None, {input_name: arr})[0][0]

    scale_x = orig_w / INPUT_SIZE
    scale_y = orig_h / INPUT_SIZE

    detections: list[Detection] = []

    for x1, y1, x2, y2, score, cls_id in output:
        if score < conf:
            continue

        roam_class = CLASS_NAMES[int(cls_id)]
        x1, x2 = x1 * scale_x, x2 * scale_x
        y1, y2 = y1 * scale_y, y2 * scale_y

        detections.append(
            Detection(
                roam_class=roam_class,
                confidence=float(score),
                x=float(x1),
                y=float(y1),
                width=float(x2 - x1),
                height=float(y2 - y1),
                needs_review=bool(
                    score < REVIEW_CONFIDENCE_THRESHOLD
                    or roam_class in LOW_CONFIDENCE_CLASSES
                ),
            )
        )

    return detections


def get_image_size(image_path: str) -> tuple[int, int]:
    with Image.open(image_path) as image:
        return image.size
