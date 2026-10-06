"""
Click-to-fill for the boundary editor: the region of the drawing around a reviewer's click, bounded by the
drawing's own linework, as a simplified polygon -- the "paint bucket" / magic-wand tool of image editors.

The reviewer supplies what automatic line detection cannot: WHICH region is the parcel. Measured on the
confirmed sheets: lots enclosed by solid boundary lines come back at 0.93-0.98 IoU from one click; regions
whose boundary is broken (dimension text, faint or dashed lines) leak or under-fill -- the `gap` setting
bridges small breaks, and a fill that floods most of the sheet is refused rather than returned.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from app.services.region_cropper import padded_crop_box

_INK = 150  # grey level below which a pixel is linework
_MAX_FRACTION = 0.6  # a fill covering more of the sheet than this has leaked
_NUDGE = 15
_HALF_LINE = 2  # px: half a typical boundary line at the 200 dpi render  # a click landing on a line moves to the nearest free pixel within this


@lru_cache(maxsize=8)
def _gray_crop(page_path: str, mtime: float, bbox: tuple, region_class: str | None) -> np.ndarray:
    with Image.open(page_path) as im:
        gray = im.convert("L")
        box = padded_crop_box(list(bbox), *gray.size, region_class)
        return np.array(gray.crop(tuple(int(round(v)) for v in box)))


def crop_gray(page_path: Path, bbox: list, region_class: str | None) -> np.ndarray:
    return _gray_crop(str(page_path), page_path.stat().st_mtime, tuple(bbox), region_class)


def _drop_spikes(pts: list[list[float]], min_angle_deg: float = 25.0) -> list[list[float]]:
    """Removes vertices where the outline doubles back on itself (an interior angle under min_angle)."""

    import math

    changed = True
    while changed and len(pts) > 3:
        changed = False
        for i in range(len(pts)):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
            v1, v2 = (a[0] - b[0], a[1] - b[1]), (c[0] - b[0], c[1] - b[1])
            n1, n2 = math.hypot(*v1), math.hypot(*v2)
            if n1 < 1e-9 or n2 < 1e-9:
                pts.pop(i); changed = True; break
            cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
            if math.degrees(math.acos(cos)) < min_angle_deg:
                pts.pop(i); changed = True; break
    return pts


def fill_region(gray: np.ndarray, x: float, y: float, gap: int = 5) -> dict:
    """{"vertices": [[x, y], ...] in crop pixels, "fraction": share of the crop} or {"error": ...}."""

    h, w = gray.shape
    x, y = int(round(x)), int(round(y))
    if not (0 <= x < w and 0 <= y < h):
        return {"error": "click is outside the drawing"}
    lines = (gray < _INK).astype(np.uint8)
    gap = max(0, min(int(gap), 25))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (gap, gap)) if gap > 1 else None
    if kernel is not None:
        lines = cv2.dilate(lines, kernel)  # bridge small breaks in the boundary
    free = (1 - lines).astype(np.uint8)
    if not free[y, x]:
        y0, x0 = max(0, y - _NUDGE), max(0, x - _NUDGE)
        ys, xs = np.nonzero(free[y0 : y + _NUDGE + 1, x0 : x + _NUDGE + 1])
        if not len(xs):
            return {"error": "clicked on a line -- click inside the parcel"}
        i = int(np.argmin((xs + x0 - x) ** 2 + (ys + y0 - y) ** 2))
        x, y = int(xs[i] + x0), int(ys[i] + y0)
    mask = np.zeros((h + 2, w + 2), np.uint8)
    cv2.floodFill(free, mask, (x, y), 2, flags=4 | (255 << 8) | cv2.FLOODFILL_MASK_ONLY)
    region = mask[1:-1, 1:-1]
    if kernel is not None:
        region = cv2.dilate(region, kernel)  # give back the boundary width the bridging took
    # thin protrusions where the fill crept into a gap along a line
    region = cv2.morphologyEx(region, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
    # out to the middle of the boundary lines (where a reviewer traces), not their inner edge
    region = cv2.dilate(region, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * _HALF_LINE + 1, 2 * _HALF_LINE + 1)))
    fraction = float(np.count_nonzero(region)) / (w * h)
    if fraction > _MAX_FRACTION:
        return {"error": "the fill leaked outside the parcel -- raise the gap, or draw it", "fraction": fraction}
    contours, _ = cv2.findContours(region, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return {"error": "nothing to fill here"}
    c = max(contours, key=cv2.contourArea)
    approx = cv2.approxPolyDP(c, 0.006 * cv2.arcLength(c, True), True)
    vertices = _drop_spikes([[float(p[0][0]), float(p[0][1])] for p in approx])
    if len(vertices) < 3:
        return {"error": "the filled region is too small"}
    return {"vertices": vertices, "fraction": round(fraction, 4)}
