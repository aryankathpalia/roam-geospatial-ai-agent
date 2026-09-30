"""
Generalization test: same two CV techniques as cv_line_test2.py, run
against 3 new pages chosen for different failure modes: shared-edge
adjacent parcels, dense multi-lot subdivision, and watermark noise.
Throwaway diagnostic, no pipeline integration.
"""

import cv2
import numpy as np
from pathlib import Path

OUT_DIR = Path("scratch_diag/cv_line_test_out")
OUT_DIR.mkdir(exist_ok=True)

CASES = {
    "shared_edge_27211ae5_p13": "data/documents/27211ae5-0099-4b0d-8176-f1483897b766/pages/page_013.png",
    "multilot_8d75d3ed_p10": "data/documents/8d75d3ed-d9e7-4bfb-874a-0b52063883eb/pages/page_010.png",
    "watermark_65be453d_p3": "data/documents/65be453d-f014-4024-84d6-60a2d0052aeb/pages/page_003.png",
}


def sample_width_and_continuity(dist_img, gray_img, x1, y1, x2, y2):
    length = max(1, int(np.hypot(x2 - x1, y2 - y1)))
    dx, dy = (x2 - x1) / length, (y2 - y1) / length
    widths, on_pattern = [], []
    for t in range(0, length, 2):
        cx, cy = int(round(x1 + dx * t)), int(round(y1 + dy * t))
        if 0 <= cx < dist_img.shape[1] and 0 <= cy < dist_img.shape[0]:
            widths.append(dist_img[cy, cx] * 2)
            on_pattern.append(1 if gray_img[cy, cx] < 150 else 0)
    return widths, on_pattern


def run_case(name, path, thresh=200, min_len_bold=150):
    print(f"\n{'='*60}\n{name}  ({path})\n{'='*60}")
    img = cv2.imread(path)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    print(f"size: {w}x{h}")

    _, binary = cv2.threshold(gray, thresh, 255, cv2.THRESH_BINARY_INV)
    dist = cv2.distanceTransform(binary, cv2.DIST_L2, 5)

    lines = cv2.HoughLinesP(binary, rho=1, theta=np.pi / 360, threshold=40, minLineLength=25, maxLineGap=4)
    n_lines = 0 if lines is None else len(lines)
    print(f"Hough segments: {n_lines}")

    results = []
    if lines is not None:
        for seg in lines:
            x1, y1, x2, y2 = seg[0]
            length = np.hypot(x2 - x1, y2 - y1)
            if length < 25:
                continue
            widths, on_pattern = sample_width_and_continuity(dist, gray, x1, y1, x2, y2)
            on_widths = [wd for wd, o in zip(widths, on_pattern) if o]
            if not on_widths:
                continue
            median_width = float(np.median(on_widths))
            on_ratio = sum(on_pattern) / len(on_pattern)
            results.append({"x1": int(x1), "y1": int(y1), "x2": int(x2), "y2": int(y2),
                             "length": round(float(length), 1), "median_width_px": round(median_width, 2),
                             "on_ratio": round(on_ratio, 2)})

    bold = [r for r in results if r["median_width_px"] >= 3.5 and r["on_ratio"] >= 0.8]
    dashed = [r for r in results if r["median_width_px"] < 3.5 and 0.3 <= r["on_ratio"] < 0.95]
    long_bold = [r for r in bold if r["length"] >= min_len_bold]
    print(f"bold: {len(bold)}, dashed: {len(dashed)}, bold+long(>={min_len_bold}px): {len(long_bold)}")

    out = np.ones_like(img) * 255
    for r in long_bold:
        cv2.line(out, (r["x1"], r["y1"]), (r["x2"], r["y2"]), (0, 0, 0), 2)
    cv2.imwrite(str(OUT_DIR / f"{name}_bold_long.png"), out)

    # Contours -- use RETR_CCOMP to get parent/child hierarchy, which is
    # what would let two regions sharing one drawn line show up as two
    # distinct (sibling or nested) contours rather than one blob.
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    big = [(i, c) for i, c in enumerate(contours) if cv2.contourArea(c) > 3000]
    big.sort(key=lambda ic: -cv2.contourArea(ic[1]))
    print(f"contours total: {len(contours)}, area>3000: {len(big)}")
    for i, c in big[:12]:
        area = cv2.contourArea(c)
        x, y, cw, ch = cv2.boundingRect(c)
        parent = hierarchy[0][i][3]
        print(f"  contour idx={i} area={area:.0f} bbox=({x},{y},{cw},{ch}) parent={parent}")

    contour_overlay = img.copy()
    colors = [(0, 255, 0), (255, 0, 255), (0, 165, 255), (255, 255, 0), (0, 0, 255)]
    for k, (i, c) in enumerate(big[:10]):
        cv2.drawContours(contour_overlay, [c], -1, colors[k % len(colors)], 3)
    cv2.imwrite(str(OUT_DIR / f"{name}_contours.png"), contour_overlay)
    print(f"saved: {name}_bold_long.png, {name}_contours.png")


if __name__ == "__main__":
    for name, path in CASES.items():
        run_case(name, path)
