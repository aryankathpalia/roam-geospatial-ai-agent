"""
Adaptive local gap-closing using VISION-DERIVED interruption coordinates
(read directly off the page by visual inspection at pixel-grid resolution,
simulating what a vision step would output as structured [{x,y}] points),
instead of blind cv2.HoughCircles detection (which cv_line_test5.py showed
mostly finds false positives -- title block glyphs, scale bar -- and
misses most real corner-pin markers).

Throwaway diagnostic, no pipeline integration.
"""

import cv2
import numpy as np
from pathlib import Path

OUT_DIR = Path("scratch_diag/cv_line_test_out")
OUT_DIR.mkdir(exist_ok=True)
PATH = "scratch_diag/new_pages/petition_p5.png"

# Vision-derived interruption points: read directly off the rendered page
# by visually tracing the boundary polygon and noting each corner-marker
# symbol / crossing-text interruption location. Not from any CV detector.
INTERRUPTION_POINTS = [
    (610, 195), (598, 300), (525, 410), (480, 505),
    (795, 330), (790, 385), (785, 590), (750, 790), (765, 895), (760, 940),
    (770, 1020), (795, 1055), (815, 1100), (510, 1270),
    (1035, 1330), (1225, 1230), (1330, 1270),
    (1310, 1565), (1290, 1625), (1265, 1650),
    (1245, 1720), (1195, 1800), (1145, 1880), (1105, 1905), (1150, 2035),
]


def main():
    img = cv2.imread(PATH)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    print(f"size: {w}x{h}, interruption points: {len(INTERRUPTION_POINTS)}")

    _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)

    marker_overlay = img.copy()
    for (x, y) in INTERRUPTION_POINTS:
        cv2.circle(marker_overlay, (x, y), 10, (0, 0, 255), 2)
    cv2.imwrite(str(OUT_DIR / "plainfield_vision_points.png"), marker_overlay)

    kernel_big = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
    closed_full = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel_big)

    mask = np.zeros_like(binary)
    for (x, y) in INTERRUPTION_POINTS:
        cv2.circle(mask, (x, y), 22, 255, -1)
    adaptive = np.where(mask > 0, closed_full, binary)

    n_changed = int(np.sum((adaptive > 0) != (binary > 0)))
    print(f"pixels changed by adaptive closing: {n_changed}")

    contours, hierarchy = cv2.findContours(adaptive, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    big = sorted([c for c in contours if cv2.contourArea(c) > 3000], key=cv2.contourArea, reverse=True)
    print(f"contours area>3000: {len(big)}")
    for c in big[:8]:
        area = cv2.contourArea(c)
        x, y, cw, ch = cv2.boundingRect(c)
        print(f"    area={area:.0f} bbox=({x},{y},{cw},{ch})")

    # expected parcel area, for sanity comparison: 14.2 ac at scale 1"=60'
    # page rendered at 200dpi -> 1 real foot = 200/60 px = 3.333px; 1 ac = 43560 sqft
    px_per_ft = 200 / 60
    expected_area_px = 14.2 * 43560 * (px_per_ft ** 2)
    print(f"expected parcel area in px^2 at this render scale: ~{expected_area_px:.0f}")

    overlay = img.copy()
    colors = [(0, 255, 0), (255, 0, 255), (0, 165, 255), (255, 255, 0), (0, 0, 255), (255, 128, 0)]
    for k, c in enumerate(big[:8]):
        cv2.drawContours(overlay, [c], -1, colors[k % len(colors)], 3)
    cv2.imwrite(str(OUT_DIR / "plainfield_contours_vision_adaptive.png"), overlay)
    print("saved: plainfield_vision_points.png, plainfield_contours_vision_adaptive.png")


if __name__ == "__main__":
    main()
