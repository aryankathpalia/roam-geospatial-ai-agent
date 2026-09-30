"""
Throwaway CV feasibility test -- NOT integrated with the pipeline.
db54d473 page 14: can classical CV (Hough lines + contours) isolate
Parcel 1/2's bold boundary from thin/dashed reference lines, using only
line thickness + stroke-continuity, no text reading at all?
"""

import cv2
import numpy as np
from pathlib import Path

IMG_PATH = "data/documents/db54d473-c7ab-4bec-bbf4-8ff9a1e3bd9b/pages/page_014.png"
OUT_DIR = Path("scratch_diag/cv_line_test_out")
OUT_DIR.mkdir(exist_ok=True)

img = cv2.imread(IMG_PATH)
gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
h, w = gray.shape
print(f"page size: {w}x{h}")

# Binarize: ink = white on black, for Hough/contour work.
_, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)

# --- 1. Hough line segment detection ---
lines = cv2.HoughLinesP(
    binary, rho=1, theta=np.pi / 360, threshold=40, minLineLength=25, maxLineGap=4
)
print(f"Hough segments detected: {0 if lines is None else len(lines)}")


def sample_stroke(gray_img, x1, y1, x2, y2, perp_width=3):
    """
    Walks along the segment in ~1.5px steps, checks whether ink is
    present in a small perpendicular window at each step (thickness
    proxy: how many perpendicular pixels are dark), and records the
    on/off pattern along the segment (continuity proxy: solid vs
    dashed/dotted).
    """
    length = max(1, int(np.hypot(x2 - x1, y2 - y1)))
    dx, dy = (x2 - x1) / length, (y2 - y1) / length
    # perpendicular unit vector
    px, py = -dy, dx
    on_pattern = []
    thicknesses = []
    for t in range(0, length, 2):
        cx, cy = x1 + dx * t, y1 + dy * t
        dark = 0
        for k in range(-perp_width, perp_width + 1):
            sx, sy = int(round(cx + px * k)), int(round(cy + py * k))
            if 0 <= sx < gray_img.shape[1] and 0 <= sy < gray_img.shape[0]:
                if gray_img[sy, sx] < 150:
                    dark += 1
        on_pattern.append(1 if dark > 0 else 0)
        thicknesses.append(dark)
    return on_pattern, thicknesses


results = []
if lines is not None:
    for seg in lines:
        x1, y1, x2, y2 = seg[0]
        length = np.hypot(x2 - x1, y2 - y1)
        if length < 20:
            continue
        on_pattern, thicknesses = sample_stroke(gray, x1, y1, x2, y2)
        if not on_pattern:
            continue
        on_ratio = sum(on_pattern) / len(on_pattern)
        mean_thickness = sum(thicknesses) / len(thicknesses)
        # crude run-length check for dash/gap regularity
        runs = []
        cur = on_pattern[0]
        run_len = 1
        for v in on_pattern[1:]:
            if v == cur:
                run_len += 1
            else:
                runs.append((cur, run_len))
                cur, run_len = v, 1
        runs.append((cur, run_len))
        n_gaps = sum(1 for v, l in runs if v == 0)
        style = "solid" if on_ratio > 0.85 else ("dashed" if 0.3 < on_ratio <= 0.85 and n_gaps >= 2 else "sparse/noise")
        results.append(
            {
                "x1": int(x1), "y1": int(y1), "x2": int(x2), "y2": int(y2),
                "length": round(float(length), 1),
                "on_ratio": round(on_ratio, 2),
                "mean_thickness_px": round(mean_thickness, 2),
                "n_gaps": n_gaps,
                "style": style,
            }
        )

solid_bold = [r for r in results if r["style"] == "solid" and r["mean_thickness_px"] >= 2.0]
dashed = [r for r in results if r["style"] == "dashed"]
other = [r for r in results if r["style"] == "sparse/noise"]

print(f"Classified: {len(solid_bold)} solid/bold, {len(dashed)} dashed, {len(other)} sparse/noise (of {len(results)} segments >=20px)")

# --- overlay for visual inspection ---
overlay = img.copy()
for r in solid_bold:
    cv2.line(overlay, (r["x1"], r["y1"]), (r["x2"], r["y2"]), (0, 0, 255), 2)  # red = bold/solid
for r in dashed:
    cv2.line(overlay, (r["x1"], r["y1"]), (r["x2"], r["y2"]), (255, 0, 0), 1)  # blue = dashed
cv2.imwrite(str(OUT_DIR / "hough_classified.png"), overlay)

# --- 2. Contours ---
contours, hierarchy = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
print(f"Total contours found: {len(contours)}")

big_contours = [c for c in contours if cv2.contourArea(c) > 5000]
print(f"Contours with area > 5000px^2: {len(big_contours)}")
for i, c in enumerate(sorted(big_contours, key=cv2.contourArea, reverse=True)[:15]):
    area = cv2.contourArea(c)
    x, y, cw, ch = cv2.boundingRect(c)
    print(f"  contour {i}: area={area:.0f} bbox=({x},{y},{cw},{ch}) closed={cv2.isContourConvex(c)}")

contour_overlay = img.copy()
cv2.drawContours(contour_overlay, sorted(big_contours, key=cv2.contourArea, reverse=True)[:15], -1, (0, 255, 0), 2)
cv2.imwrite(str(OUT_DIR / "contours.png"), contour_overlay)

print("\nSaved overlays to", OUT_DIR)
