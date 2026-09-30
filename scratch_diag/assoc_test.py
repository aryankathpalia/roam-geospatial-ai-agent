"""
Validates the weakest link in the "confirmed shape + OCR/vision evidence"
design: does proximity-based association (OCR label bbox centroid -> nearest
confirmed-polygon edge) actually pick the RIGHT edge often enough to matter?

For each document: a real "confirmed polygon" (either the already-validated
CV contour for 27211ae5, or hand-traced vertices read directly off a
pixel-gridded render for NVZ and Reale -- see conversation), run the
existing run_parcelmap_ocr(), filter to bearing/distance-shaped lines,
assign each to its nearest polygon edge by point-to-segment distance, and
render an overlay for manual correctness verification.

Throwaway diagnostic, no pipeline integration.
"""

import sys
import io
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import cv2
import numpy as np
from PIL import Image
from app.services.ocr import run_parcelmap_ocr

OUT_DIR = Path("scratch_diag/cv_line_test_out")
OUT_DIR.mkdir(exist_ok=True)

BEARING_LIKE = re.compile(r"[NnSs].{0,15}[EeWw]|\d{1,4}\.\d{2}")

CASES = {
    "27211ae5_shared_edge": {
        "path": "data/documents/27211ae5-0099-4b0d-8176-f1483897b766/pages/page_013.png",
        "clutter": "moderate",
        # 5-vertex simplification of the already-validated CV contour
        # (child of the closed shared-edge parent, this session).
        "polygon": [[1145, 1123], [1652, 1434], [2005, 1163], [2392, 1827], [1125, 1828]],
    },
    "nvz_parcel1": {
        "path": "new_test_docs/placerville/pages/page_009.png",
        "clutter": "clean",
        # Hand-read off a pixel-gridded render -- Parcel 1 is a clean
        # rectangle in this document.
        "polygon": [[1200, 420], [1965, 415], [1965, 727], [1200, 727]],
    },
    "reale_lot1": {
        "path": "data/documents/a1136318-03ef-438d-9f02-8fcdcb5dca30/pages/page_005.png",
        "clutter": "dense",
        # Approximate hand trace off a pixel-gridded render -- dense,
        # cluttered sheet (walls/decks/utilities crossing the boundary).
        "polygon": [[905, 440], [1155, 445], [1205, 580], [1180, 660], [1155, 990], [905, 900]],
    },
}


def point_segment_distance(p, a, b):
    p, a, b = np.array(p, float), np.array(a, float), np.array(b, float)
    ab = b - a
    t = np.dot(p - a, ab) / (np.dot(ab, ab) + 1e-9)
    t = max(0.0, min(1.0, t))
    proj = a + t * ab
    return float(np.linalg.norm(p - proj))


def nearest_edge(center, polygon):
    best_idx, best_dist = -1, float("inf")
    n = len(polygon)
    for i in range(n):
        a, b = polygon[i], polygon[(i + 1) % n]
        d = point_segment_distance(center, a, b)
        if d < best_dist:
            best_dist, best_idx = d, i
    return best_idx, best_dist


def run_case(name, cfg):
    print(f"\n{'='*70}\n{name}  (clutter={cfg['clutter']})\n{'='*70}")
    img = Image.open(cfg["path"])
    lines, is_map = run_parcelmap_ocr(img)
    polygon = cfg["polygon"]

    candidates = [l for l in lines if BEARING_LIKE.search(l.text) and len(l.text) >= 4]
    print(f"OCR lines: {len(lines)}, bearing/distance-like candidates: {len(candidates)}")

    cv_img = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    n = len(polygon)
    for i in range(n):
        a, b = polygon[i], polygon[(i + 1) % n]
        cv2.line(cv_img, tuple(a), tuple(b), (255, 0, 0), 3)
    for i, (x, y) in enumerate(polygon):
        cv2.circle(cv_img, (x, y), 8, (255, 0, 0), -1)
        cv2.putText(cv_img, f"v{i}", (x + 10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0), 2)

    colors = [(0, 165, 255), (0, 255, 255), (255, 0, 255), (0, 255, 0), (0, 100, 255), (255, 255, 0)]
    rows = []
    for cand in candidates:
        x1, y1, x2, y2 = cand.bbox
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        edge_idx, dist = nearest_edge((cx, cy), polygon)
        rows.append((cand.text, round(cx), round(cy), edge_idx, round(dist)))
        color = colors[edge_idx % len(colors)]
        cv2.circle(cv_img, (int(cx), int(cy)), 10, color, 3)
        cv2.putText(cv_img, f"e{edge_idx}", (int(cx) + 12, int(cy) + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

    for text, cx, cy, edge_idx, dist in sorted(rows, key=lambda r: r[3]):
        print(f"  edge{edge_idx} (dist={dist:>4}px)  text={text!r}  center=({cx},{cy})")

    out_path = OUT_DIR / f"assoc_{name}.png"
    cv2.imwrite(str(out_path), cv_img)
    print(f"saved: {out_path.name}")


if __name__ == "__main__":
    for name, cfg in CASES.items():
        run_case(name, cfg)
