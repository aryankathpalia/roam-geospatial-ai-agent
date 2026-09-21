"""
PaddleOCR-based text extraction for Text/Table/Seal/ScannedPrintout
region crops.

Two things had to be fixed empirically before this was usable, not
assumed from docs:

1. PaddlePaddle 3.3.0+ has a regression where the default
   enable_mkldnn=True crashes with "ConvertPirAttribute2RuntimeAttribute
   not support" on CPU inference (confirmed: PaddlePaddle/Paddle#77340).
   requirements.txt pins paddlepaddle==3.2.2, which predates it.

2. PaddleOCR's default "medium" model preset (PP-OCRv6_medium_*) took
   ~105s for a single mid-sized crop -- unusable. Switching to the
   "mobile" preset (PP-OCRv5_mobile_det/rec) cut that to ~6s for a dense
   table crop and ~1s for a typical text crop, with no meaningful
   accuracy loss (0.96-1.00 confidence on the same content). Always use
   the mobile preset here; do not switch back to the default.

Crops are passed in as numpy arrays / PIL Images -- no disk round-trip.
"""

import threading
from dataclasses import dataclass

import numpy as np
from paddleocr import PaddleOCR
from PIL import Image


@dataclass
class OCRLine:
    text: str
    confidence: float


_engine: PaddleOCR | None = None

# PaddleOCR's underlying predictor is not thread-safe -- calling
# .predict() on the same engine from two threads at once (which happens
# here because the streaming pipeline dispatches extraction for
# multiple pages concurrently) crashes with
# "PreconditionNotMetError: Tensor holds no memory." Serializing calls
# is the fix; the streaming pipeline still overlaps extraction with the
# NEXT page's layout detection, which is what actually mattered.
_engine_lock = threading.Lock()


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


def run_ocr(image: Image.Image) -> list[OCRLine]:
    """Run OCR on one region crop, returning its text lines in reading order."""

    arr = np.array(image.convert("RGB"))
    with _engine_lock:
        result = get_engine().predict(arr)

    lines: list[OCRLine] = []
    for page_result in result:
        texts = page_result.get("rec_texts", [])
        scores = page_result.get("rec_scores", [])
        for text, score in zip(texts, scores):
            lines.append(OCRLine(text=text, confidence=round(float(score), 3)))

    return lines
