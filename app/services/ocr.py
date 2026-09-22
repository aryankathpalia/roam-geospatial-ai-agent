"""
PaddleOCR-based text extraction, one call per PAGE (not per detected
region). See app/pipeline/page_ocr.py for why: detecting+reading every
region separately (up to 185 calls for one document) took ~150s in
production; running one full-page OCR pass and matching the resulting
lines back to ROAM's layout regions geometrically cuts that to one call
per page, fanned out across pages in parallel (see modal_app.py).

Two things had to be fixed empirically before this was usable, not
assumed from docs:

1. PaddlePaddle 3.3.0+ has a regression where the default
   enable_mkldnn=True crashes with "ConvertPirAttribute2RuntimeAttribute
   not support" on CPU inference (confirmed: PaddlePaddle/Paddle#77340).
   requirements.txt pins paddlepaddle==3.2.2, which predates it.

2. PaddleOCR's default "medium" model preset (PP-OCRv6_medium_*) took
   ~105s for a single mid-sized crop -- unusable. Switching to the
   "mobile" preset (PP-OCRv5_mobile_det/rec) cut that dramatically, with
   no meaningful accuracy loss (0.96-1.00 confidence on the same
   content). Always use the mobile preset here.

GPU was investigated and ruled out: Modal requires a payment method on
file for any GPU function, even within the free credit, which conflicts
with this deployment's no-card requirement. A CPU-only alternative
engine (RapidOCR) was also tested directly against our real documents
and was slower than this setup even after tuning (16-22s vs 6-8s on the
same crop) -- not used.
"""

import threading
from dataclasses import dataclass

import numpy as np
from paddleocr import PaddleOCR
from PIL import Image

_engine: PaddleOCR | None = None

# PaddleOCR's underlying predictor is not thread-safe -- calling
# .predict() on the same engine from two threads at once crashes with
# "PreconditionNotMetError: Tensor holds no memory." Each Modal
# container running ocr_page_remote has its own process (and therefore
# its own engine instance), so this only serializes calls *within* one
# container, not across the parallel fan-out.
_engine_lock = threading.Lock()


@dataclass
class OCRLine:
    text: str
    confidence: float
    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2, page coordinates


def get_engine() -> PaddleOCR:
    """Load (and cache) the PaddleOCR engine, mobile preset."""

    global _engine

    if _engine is None:
        _engine = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            cpu_threads=8,
        )

    return _engine


def run_page_ocr(image: Image.Image) -> list[OCRLine]:
    """
    Run OCR once on a whole rendered page, returning every detected
    text line with its bounding box -- callers match lines back to
    ROAM's layout regions geometrically (app/pipeline/page_ocr.py).
    """

    arr = np.array(image.convert("RGB"))
    with _engine_lock:
        result = get_engine().predict(arr)

    lines: list[OCRLine] = []
    for page_result in result:
        texts = page_result.get("rec_texts", [])
        scores = page_result.get("rec_scores", [])
        boxes = page_result.get("rec_boxes", [])
        for text, score, box in zip(texts, scores, boxes):
            x1, y1, x2, y2 = (float(v) for v in box)
            lines.append(
                OCRLine(
                    text=text,
                    confidence=round(float(score), 3),
                    bbox=(x1, y1, x2, y2),
                )
            )

    return lines
