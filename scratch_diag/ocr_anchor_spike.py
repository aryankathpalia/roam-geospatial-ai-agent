"""
Spike: does existing OCR bearing/distance bbox data (OCRLine.bbox from
app/services/ocr.py, computed but currently discarded before it reaches
document_pipeline.py) land close enough to real boundary vertices to seed
a pixel-space overlay for the confirm-and-edit UI, for vision-extracted
(non-CV) shapes?

Runs the REAL, already-existing run_parcelmap_ocr() (deterministic, local,
no LLM cost) on db54d473 page 14, then prints each merged bearing/distance
line + its pixel bbox for manual comparison against the plat.

Throwaway diagnostic, no pipeline integration.
"""

import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
from app.services.ocr import run_parcelmap_ocr

IMG_PATH = "data/documents/27211ae5-0099-4b0d-8176-f1483897b766/pages/page_013.png"
OUT_DIR = Path("scratch_diag/cv_line_test_out")
OUT_DIR.mkdir(exist_ok=True)


def main():
    img = Image.open(IMG_PATH)
    print(f"page size: {img.size}")
    lines, is_map = run_parcelmap_ocr(img)
    print(f"is_parcelmap-like: {is_map}, lines: {len(lines)}")

    import cv2
    import numpy as np
    overlay = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)

    import re
    bearing_like = re.compile(r"[NnSs].{0,15}[EeWw]|\d{1,4}\.\d{2}")

    kept = 0
    for i, line in enumerate(lines):
        x1, y1, x2, y2 = line.bbox
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        is_candidate = bool(bearing_like.search(line.text))
        color = (0, 0, 255) if is_candidate else (200, 200, 200)
        cv2.rectangle(overlay, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)
        if is_candidate:
            kept += 1
            print(f"[{i}] text={line.text!r} bbox=({x1:.0f},{y1:.0f},{x2:.0f},{y2:.0f}) center=({cx:.0f},{cy:.0f})")
            cv2.putText(overlay, str(i), (int(x1), int(y1) - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 2)
    print(f"bearing/distance-like candidates: {kept} / {len(lines)}")

    out_name = Path(IMG_PATH).parts[-3] + "_ocr_bboxes.png"
    cv2.imwrite(str(OUT_DIR / out_name), overlay)
    print(f"saved: {out_name}")


if __name__ == "__main__":
    main()
