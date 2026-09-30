"""
Improved pass: classify Hough segments by TRUE local stroke half-width
(via distance transform on the binary ink mask), not just presence/
absence of a nearby dark pixel -- the first pass's on/off proxy failed
to separate bold from thin because it didn't measure width at all.
"""

import cv2
import numpy as np
from pathlib import Path

IMG_PATH = "data/documents/db54d473-c7ab-4bec-bbf4-8ff9a1e3bd9b/pages/page_014.png"
OUT_DIR = Path("scratch_diag/cv_line_test_out")

img = cv2.imread(IMG_PATH)
gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

_, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)

# Distance transform: for every ink pixel, the distance to the nearest
# background pixel. At the centerline of a stroke of width W, this is
# approximately W/2 -- a direct, per-pixel thickness measurement,
# unlike the first pass's crude "any dark pixel in a fixed window".
dist = cv2.distanceTransform(binary, cv2.DIST_L2, 5)
print("distance transform stats: max", dist.max(), "mean(nonzero)", dist[dist > 0].mean())

lines = cv2.HoughLinesP(binary, rho=1, theta=np.pi / 360, threshold=40, minLineLength=25, maxLineGap=4)
print(f"Hough segments: {0 if lines is None else len(lines)}")


def sample_width_and_continuity(dist_img, gray_img, x1, y1, x2, y2):
    length = max(1, int(np.hypot(x2 - x1, y2 - y1)))
    dx, dy = (x2 - x1) / length, (y2 - y1) / length
    widths = []
    on_pattern = []
    for t in range(0, length, 2):
        cx, cy = int(round(x1 + dx * t)), int(round(y1 + dy * t))
        if 0 <= cx < dist_img.shape[1] and 0 <= cy < dist_img.shape[0]:
            widths.append(dist_img[cy, cx] * 2)  # diameter, not radius
            on_pattern.append(1 if gray_img[cy, cx] < 150 else 0)
    return widths, on_pattern


results = []
if lines is not None:
    for seg in lines:
        x1, y1, x2, y2 = seg[0]
        length = np.hypot(x2 - x1, y2 - y1)
        if length < 25:
            continue
        widths, on_pattern = sample_width_and_continuity(dist, gray, x1, y1, x2, y2)
        if not widths:
            continue
        # median over only the ON samples -- OFF samples correctly
        # read ~0 width and would just drag the average down without
        # telling us about the stroke's own thickness.
        on_widths = [w for w, o in zip(widths, on_pattern) if o]
        if not on_widths:
            continue
        median_width = float(np.median(on_widths))
        on_ratio = sum(on_pattern) / len(on_pattern)
        results.append(
            {
                "x1": int(x1), "y1": int(y1), "x2": int(x2), "y2": int(y2),
                "length": round(float(length), 1),
                "median_width_px": round(median_width, 2),
                "on_ratio": round(on_ratio, 2),
            }
        )

widths_all = [r["median_width_px"] for r in results]
print(f"segment count with valid width: {len(results)}")
if widths_all:
    print("width distribution: p10=%.2f p25=%.2f p50=%.2f p75=%.2f p90=%.2f max=%.2f" % (
        np.percentile(widths_all, 10), np.percentile(widths_all, 25),
        np.percentile(widths_all, 50), np.percentile(widths_all, 75),
        np.percentile(widths_all, 90), max(widths_all)
    ))

# Classify: bold = thick AND mostly-continuous; dashed = thin-ish AND
# clearly gapped (on_ratio well below 1 despite maxLineGap bridging
# small gaps); everything else = ambiguous.
bold = [r for r in results if r["median_width_px"] >= 3.5 and r["on_ratio"] >= 0.8]
dashed = [r for r in results if r["median_width_px"] < 3.5 and 0.3 <= r["on_ratio"] < 0.95]
thin_solid = [r for r in results if r["median_width_px"] < 3.5 and r["on_ratio"] >= 0.95]
print(f"bold: {len(bold)}, dashed: {len(dashed)}, thin_solid(text/gridlines/etc): {len(thin_solid)}, other: {len(results)-len(bold)-len(dashed)-len(thin_solid)}")

overlay = img.copy()
for r in bold:
    cv2.line(overlay, (r["x1"], r["y1"]), (r["x2"], r["y2"]), (0, 0, 255), 2)  # red = bold
for r in dashed:
    cv2.line(overlay, (r["x1"], r["y1"]), (r["x2"], r["y2"]), (255, 0, 0), 1)  # blue = dashed
cv2.imwrite(str(OUT_DIR / "hough_classified_v2.png"), overlay)

# Bold-only overlay, to see if it isolates just the parcel boundary
bold_only = np.ones_like(img) * 255
for r in bold:
    cv2.line(bold_only, (r["x1"], r["y1"]), (r["x2"], r["y2"]), (0, 0, 0), 2)
cv2.imwrite(str(OUT_DIR / "bold_only.png"), bold_only)

print("saved:", OUT_DIR / "hough_classified_v2.png", "and bold_only.png")
