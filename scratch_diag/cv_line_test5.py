"""
Test: vision-described boundary style + adaptive local gap-closing on
Plainfield. Vision description (given directly, no legend lookup):
  "The parcel boundary is a thin, mostly-continuous solid line forming
  an irregular polygon. It is interrupted at nearly every vertex by
  small circular corner-marker symbols (iron pin / monument found) and
  by bearing/distance text labels crossing the line."

This script uses that description operationally: instead of one global
morphological-close kernel over the whole page, detect small circular
blobs (candidate corner markers) via HoughCircles, and apply a small
morphological close ONLY within a local radius around each detected
circle center -- leaving the rest of the page untouched.

Throwaway diagnostic, no pipeline integration.
"""

import cv2
import numpy as np
from pathlib import Path

OUT_DIR = Path("scratch_diag/cv_line_test_out")
OUT_DIR.mkdir(exist_ok=True)

PATH = "scratch_diag/new_pages/petition_p5.png"


def main():
    img = cv2.imread(PATH)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    print(f"size: {w}x{h}")

    _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)

    # --- detect small circular corner markers ---
    blurred = cv2.medianBlur(gray, 3)
    circles = cv2.HoughCircles(
        blurred, cv2.HOUGH_GRADIENT, dp=1, minDist=15,
        param1=80, param2=18, minRadius=4, maxRadius=14,
    )
    n_circ = 0 if circles is None else circles.shape[1]
    print(f"candidate corner-marker circles detected: {n_circ}")

    circle_overlay = img.copy()
    if circles is not None:
        for c in circles[0]:
            cx, cy, r = c
            cv2.circle(circle_overlay, (int(cx), int(cy)), int(r), (0, 0, 255), 2)
    cv2.imwrite(str(OUT_DIR / "plainfield_detected_circles.png"), circle_overlay)

    # --- baseline: raw contours (no closing at all) ---
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    big = sorted([c for c in contours if cv2.contourArea(c) > 3000], key=cv2.contourArea, reverse=True)
    print(f"[raw] contours area>3000: {len(big)}, top area: {cv2.contourArea(big[0]) if big else 0:.0f}")

    # --- global close (5x5), for comparison with earlier run ---
    kernel5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    global_closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel5)
    contours_g, _ = cv2.findContours(global_closed, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    big_g = sorted([c for c in contours_g if cv2.contourArea(c) > 3000], key=cv2.contourArea, reverse=True)
    print(f"[global close 5x5] contours area>3000: {len(big_g)}, top area: {cv2.contourArea(big_g[0]) if big_g else 0:.0f}")

    # --- adaptive: close only in a local radius around each detected circle ---
    adaptive = binary.copy()
    kernel_big = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    closed_full = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel_big)
    if circles is not None:
        mask = np.zeros_like(binary)
        for c in circles[0]:
            cx, cy, r = c
            cv2.circle(mask, (int(cx), int(cy)), int(r) + 12, 255, -1)
        adaptive = np.where(mask > 0, closed_full, binary)

    n_changed = int(np.sum((adaptive > 0) != (binary > 0)))
    print(f"pixels changed by adaptive closing: {n_changed} (vs global close would touch ~{int(np.sum((global_closed>0)!=(binary>0)))})")

    contours_a, hierarchy_a = cv2.findContours(adaptive, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    big_a = sorted([c for c in contours_a if cv2.contourArea(c) > 3000], key=cv2.contourArea, reverse=True)
    print(f"[adaptive local close] contours area>3000: {len(big_a)}")
    for c in big_a[:6]:
        area = cv2.contourArea(c)
        x, y, cw, ch = cv2.boundingRect(c)
        print(f"    area={area:.0f} bbox=({x},{y},{cw},{ch})")

    overlay = img.copy()
    colors = [(0, 255, 0), (255, 0, 255), (0, 165, 255), (255, 255, 0), (0, 0, 255), (255, 128, 0)]
    for k, c in enumerate(big_a[:6]):
        cv2.drawContours(overlay, [c], -1, colors[k % len(colors)], 3)
    cv2.imwrite(str(OUT_DIR / "plainfield_contours_adaptive.png"), overlay)
    print("saved: plainfield_detected_circles.png, plainfield_contours_adaptive.png")


if __name__ == "__main__":
    main()
