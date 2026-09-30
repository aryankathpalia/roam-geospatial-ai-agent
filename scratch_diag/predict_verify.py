"""
Predict-and-verify placement test: instead of bootstrapping scale from a
single edge's bearing+distance call (which produced ties/garbage-in-
garbage-out in the association test), derive scale directly from the
document's own STATED acreage (already OCR/vision-extracted, a real
number the pipeline already has) divided into the confirmed polygon's
pixel area. Then predict every edge's real length from that scale and
check which printed numbers on the page actually match, at various
distance/tolerance settings.

Throwaway diagnostic, no pipeline integration.
"""

import sys
import io
import math
import re
import glob
import json
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np
from PIL import Image
from app.services.ocr import run_parcelmap_ocr
from app.services.geometry import parse_bearing, parse_distance, _BEARING_RE

OUT_DIR = Path("scratch_diag/cv_line_test_out")
SQFT_PER_ACRE = 43560.0

CASES = {
    "nvz_parcel1": {
        "path": "new_test_docs/placerville/pages/page_009.png",
        "polygon": [[1200, 420], [1965, 415], [1965, 727], [1200, 727]],
        "stated_sqft": 1.78 * SQFT_PER_ACRE,
        "known_scale_ftpx": 220.00 / 312.0,  # edge1 length / distance, from earlier association test
    },
    "nvz_parcel2": {
        "path": "new_test_docs/placerville/pages/page_009.png",
        "polygon": [[430, 420], [1200, 420], [1200, 725], [430, 725]],
        "stated_sqft": 2.78 * SQFT_PER_ACRE,
        "known_scale_ftpx": None,
    },
    "27211ae5_lot48-3": {
        "path": "data/documents/27211ae5-0099-4b0d-8176-f1483897b766/pages/page_013.png",
        "polygon": [[1145, 1123], [1397, 1195], [1455, 1292], [1589, 1278], [1652, 1434],
                    [1750, 1324], [1681, 1267], [2005, 1163], [2392, 1827], [1125, 1828]],
        "stated_sqft": 0.95 * SQFT_PER_ACRE,
        "known_scale_ftpx": None,
    },
    "db54d473_parcel1": {
        "path": "data/documents/db54d473-c7ab-4bec-bbf4-8ff9a1e3bd9b/pages/page_014.png",
        # CV contour, visually confirmed against the "PARCEL 1" label this session
        "polygon": [[750, 134], [440, 928], [193, 921], [291, 140]],
        "stated_sqft": 85396.0,  # printed directly on the page: "PARCEL 1  85,396 SQ. FT."
        "known_scale_ftpx": None,
    },
}


def polygon_area(poly):
    n = len(poly)
    s = 0.0
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def edge_pixel_geometry(polygon, i):
    n = len(polygon)
    ax, ay = polygon[i]
    bx, by = polygon[(i + 1) % n]
    length_px = math.hypot(bx - ax, by - ay)
    angle_deg = math.degrees(math.atan2(bx - ax, -(by - ay))) % 360
    return length_px, angle_deg


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


def circular_diff(a, b, period=360):
    d = abs(a - b) % period
    return min(d, period - d)


def text_orientation_diff(bbox, edge_angle_deg):
    x1, y1, x2, y2 = bbox
    w, h = abs(x2 - x1), abs(y2 - y1)
    presumed = 0.0 if w >= h else 90.0
    return circular_diff(presumed, edge_angle_deg % 90, period=90)


def collect_all_numbers(lines):
    """Every printed number on the page -- distance_only INCLUDED per spec,
    plus the distance portion of any bearing+distance call."""
    out = []
    for line in lines:
        text = line.text.strip()
        az = parse_bearing(text)
        if az is not None:
            m = _BEARING_RE.search(text)
            remainder = text[: m.start()] + text[m.end():] if m else text
            dist = parse_distance(remainder)
            out.append({"line": line, "value": dist, "kind": "bearing+distance", "azimuth": az})
            continue
        bare = text.rstrip("'′\"")
        try:
            float(bare)
        except ValueError:
            continue
        dist = parse_distance(text)
        if dist is not None:
            out.append({"line": line, "value": dist, "kind": "distance_only", "azimuth": None})
    return out


def run_case(name, cfg):
    print(f"\n{'='*72}\n{name}\n{'='*72}")
    img = Image.open(cfg["path"])
    lines, _ = run_parcelmap_ocr(img)
    polygon = cfg["polygon"]
    n_edges = len(polygon)

    pixel_area = polygon_area(polygon)
    scale = math.sqrt(cfg["stated_sqft"] / pixel_area)
    print(f"pixel_area={pixel_area:.0f}px^2  stated_sqft={cfg['stated_sqft']:.0f}  "
          f"scale(sqrt method)={scale:.4f} ft/px")
    if cfg["known_scale_ftpx"]:
        err = abs(scale - cfg["known_scale_ftpx"]) / cfg["known_scale_ftpx"] * 100
        print(f"cross-check vs known direct scale ({cfg['known_scale_ftpx']:.4f} ft/px): {err:.1f}% error")

    numbers = collect_all_numbers(lines)
    print(f"printed numbers on page: {len(numbers)}")

    # predicted length per edge
    edge_predictions = {}
    for i in range(n_edges):
        length_px, angle_px = edge_pixel_geometry(polygon, i)
        edge_predictions[i] = (length_px * scale, length_px, angle_px)

    for tol_pct in (2, 3, 5):
        matches_by_edge = defaultdict(list)
        for num in numbers:
            if not num["value"]:
                continue
            x1, y1, x2, y2 = num["line"].bbox
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            edge_idx, dist_px = nearest_edge((cx, cy), polygon)
            predicted_ft, length_px, angle_px = edge_predictions[edge_idx]
            pct_err = abs(num["value"] - predicted_ft) / predicted_ft * 100 if predicted_ft else 999
            orient_diff = text_orientation_diff(num["line"].bbox, angle_px)
            if pct_err <= tol_pct and dist_px <= 110 and orient_diff <= 15:
                matches_by_edge[edge_idx].append((num, dist_px, pct_err))
        total = sum(len(v) for v in matches_by_edge.values())
        print(f"  tol={tol_pct}%: {total} matches across {len(matches_by_edge)}/{n_edges} edges")
        if tol_pct == 3:
            for edge_idx in sorted(matches_by_edge):
                predicted_ft = edge_predictions[edge_idx][0]
                print(f"    edge{edge_idx} (predicted {predicted_ft:.1f}ft):")
                for num, dist_px, pct_err in matches_by_edge[edge_idx]:
                    print(f"      text={num['line'].text!r:35s} dist_px={dist_px:.0f} pct_err={pct_err:.1f}%")

    # explicit check on known non-matching values (e.g. NVZ's 903.15, easement dims)
    print("  -- explicit rejects check (3% tolerance) --")
    for num in numbers:
        if not num["value"]:
            continue
        x1, y1, x2, y2 = num["line"].bbox
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        edge_idx, dist_px = nearest_edge((cx, cy), polygon)
        predicted_ft, length_px, angle_px = edge_predictions[edge_idx]
        pct_err = abs(num["value"] - predicted_ft) / predicted_ft * 100 if predicted_ft else 999
        if num["value"] > 500 or (dist_px <= 110 and pct_err > 20):
            print(f"    text={num['line'].text!r:35s} value={num['value']} nearest_edge={edge_idx} "
                  f"predicted={predicted_ft:.1f} pct_err={pct_err:.1f}% dist_px={dist_px:.0f}")


def check_scale_bar(path, label):
    print(f"\n-- scale bar OCR check: {label} --")
    img = Image.open(path)
    lines, _ = run_parcelmap_ocr(img)
    hits = [l for l in lines if re.search(r"SCALE|1\s*\"?\s*=\s*\d+|INCH", l.text, re.IGNORECASE)]
    for l in hits:
        print(f"    text={l.text!r} bbox={l.bbox}")
    if not hits:
        print("    no scale-bar-like text found")


def corpus_ceiling():
    print(f"\n{'='*72}\nCorpus ceiling: parcels with a stated acreage already extracted\n{'='*72}")
    total_parcels = 0
    with_stated = 0
    for path in glob.glob("data/documents/*/result.json"):
        try:
            r = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        for p in r.get("pages", []):
            for reg in p.get("regions", []):
                for par in (reg.get("parcels") or []):
                    total_parcels += 1
                    stated = par.get("vision_geometry", {}).get("stated_area_acres")
                    if stated:
                        with_stated += 1
    print(f"total parcels with any geometry: {total_parcels}")
    print(f"parcels with a non-null stated_area_acres: {with_stated} "
          f"({100*with_stated/total_parcels:.1f}%)" if total_parcels else "")


if __name__ == "__main__":
    for name, cfg in CASES.items():
        run_case(name, cfg)
    check_scale_bar("new_test_docs/placerville/pages/page_009.png", "NVZ")
    check_scale_bar("scratch_diag/new_pages/packet_p28.png", "Payette")
    check_scale_bar("data/documents/db54d473-c7ab-4bec-bbf4-8ff9a1e3bd9b/pages/page_014.png", "db54d473 (has printed SCALE 1\"=40')")
    corpus_ceiling()
