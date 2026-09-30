"""
Predict-and-verify, rerun against REAL user-confirmed polygons (not
hand-traced ones), for Tier 1.

Converts each parcel's confirmed_boundary_pixels (crop-relative) into
PAGE-ABSOLUTE pixel coordinates before doing anything else -- this
sidesteps the crop-padding-formula alignment question entirely (some
of these were confirmed before today's serve-time padding fix and are
crop-relative to an older, differently-framed crop; converting to
page-absolute coordinates makes that irrelevant, since OCR label
positions are also in page-absolute coordinates).

Origin-detection: if the confirmed shape's stored crop_width/height
exactly matches the region's raw bbox width/height (zero margin, the
old get_region_crop behavior), the crop origin was (bbox_x, bbox_y)
exactly. Otherwise it matches the current padded-crop formula.

Corrected acreages used: 27211ae5-content Lot 48 = 0.95 ac, Lot 48-3 =
1.72 ac (the earlier predict-and-verify run used 0.95 for Lot 48-3,
which was confounded -- see conversation).

Report only. No production code.
"""

import sys
import io
import math
import re
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import json
import numpy as np
from PIL import Image
from app.services.ocr import run_parcelmap_ocr
from app.services.geometry import parse_bearing, parse_distance, _BEARING_RE

SQFT_PER_ACRE = 43560.0

CASES = [
    {"name": "nvz_parcel1", "doc": "6d8534f5-f07a-460d-970f-4ccf5c40a3e4", "label": "PARCEL 1", "stated_sqft": 1.78 * SQFT_PER_ACRE},
    {"name": "nvz_parcel2", "doc": "6d8534f5-f07a-460d-970f-4ccf5c40a3e4", "label": "PARCEL 2", "stated_sqft": 2.78 * SQFT_PER_ACRE},
    {"name": "map7lot48", "doc": "3ea4cc01-4d39-472b-9465-a105306d63dc", "label": "MAP 7 LOT 48", "stated_sqft": 0.95 * SQFT_PER_ACRE},
    {"name": "map7lot48-3", "doc": "3ea4cc01-4d39-472b-9465-a105306d63dc", "label": "MAP 7 LOT 48-3", "stated_sqft": 1.72 * SQFT_PER_ACRE},
    {"name": "ada_parcel1", "doc": "bec76ab0-f70c-482c-a2e2-71a18cec7ef9", "label": "PARCEL 1", "stated_sqft": 1.23 * SQFT_PER_ACRE},
    {"name": "payette_parcel1", "doc": "6cbca296-7d7e-4457-a66b-37f04a7c2628", "label": "PARCEL 1", "stated_sqft": 6.389 * SQFT_PER_ACRE},
    {"name": "planned_utility_easement", "doc": "1600dbf6-9174-4360-9cae-55886bc7ca7f", "label": "Planned Utility Easement", "stated_sqft": 1.03 * SQFT_PER_ACRE},
]

DOCS_ROOT = Path("data/documents")


def load_confirmed_polygon_page_absolute(doc_id, label):
    r = json.load(open(DOCS_ROOT / doc_id / "result.json", encoding="utf-8"))
    for p in r["pages"]:
        for reg in p["regions"]:
            if reg.get("class") != "ParcelMap":
                continue
            for par in reg.get("parcels") or []:
                if par.get("vision_geometry", {}).get("parcel_label") != label:
                    continue
                if not par.get("human_confirmed"):
                    continue
                cb = par["confirmed_boundary_pixels"]
                bx, by, bw, bh = reg["bbox"]
                page_path = DOCS_ROOT / doc_id / "pages" / f"page_{p['page_number']:03d}.png"
                with Image.open(page_path) as im:
                    pw, ph = im.size

                zero_margin_w, zero_margin_h = bw, bh
                padded_mx = max(40, bw * 0.08)
                padded_my = max(40, bh * 0.08)
                padded_x1 = max(0, bx - padded_mx)
                padded_y1 = max(0, by - padded_my)
                padded_w = min(pw, bx + bw + padded_mx) - padded_x1
                padded_h = min(ph, by + bh + padded_my) - padded_y1

                if abs(cb["crop_width"] - zero_margin_w) < 2 and abs(cb["crop_height"] - zero_margin_h) < 2:
                    origin_x, origin_y = bx, by
                elif abs(cb["crop_width"] - padded_w) < 2 and abs(cb["crop_height"] - padded_h) < 2:
                    origin_x, origin_y = padded_x1, padded_y1
                else:
                    raise ValueError(
                        f"{doc_id} {label}: stored crop {cb['crop_width']}x{cb['crop_height']} matches "
                        f"neither zero-margin ({zero_margin_w:.0f}x{zero_margin_h:.0f}) nor current-padded "
                        f"({padded_w:.0f}x{padded_h:.0f}) framing -- can't determine origin safely."
                    )

                page_verts = [[origin_x + x, origin_y + y] for x, y in cb["vertices"]]
                return page_verts, str(page_path), p["page_number"]
    return None, None, None


def polygon_area(poly):
    n = len(poly)
    s = 0.0
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def edge_geom(polygon, i):
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
    for i in range(len(polygon)):
        a, b = polygon[i], polygon[(i + 1) % len(polygon)]
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


def run_case(cfg):
    name = cfg["name"]
    print(f"\n{'='*72}\n{name}  ({cfg['doc'][:8]} / {cfg['label']!r})\n{'='*72}")
    try:
        polygon, page_path, page_num = load_confirmed_polygon_page_absolute(cfg["doc"], cfg["label"])
    except ValueError as exc:
        print(f"  SKIPPED: {exc}")
        return None
    if polygon is None:
        print("  SKIPPED: not found / not confirmed")
        return None

    n_edges = len(polygon)
    pixel_area = polygon_area(polygon)
    scale = math.sqrt(cfg["stated_sqft"] / pixel_area)
    print(f"page={page_num}  vertices={n_edges}  pixel_area={pixel_area:.0f}px^2  "
          f"stated_sqft={cfg['stated_sqft']:.0f}  scale={scale:.4f} ft/px")

    img = Image.open(page_path)
    lines, _ = run_parcelmap_ocr(img)
    numbers = collect_all_numbers(lines)

    edge_pred = {i: edge_geom(polygon, i) for i in range(n_edges)}

    results_by_tol = {}
    for tol_pct in (2, 3, 5):
        matches_by_edge = defaultdict(list)
        for num in numbers:
            if not num["value"]:
                continue
            x1, y1, x2, y2 = num["line"].bbox
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            edge_idx, dist_px = nearest_edge((cx, cy), polygon)
            length_px, angle_px = edge_pred[edge_idx]
            predicted_ft = length_px * scale
            pct_err = abs(num["value"] - predicted_ft) / predicted_ft * 100 if predicted_ft else 999
            orient_diff = text_orientation_diff(num["line"].bbox, angle_px)
            if pct_err <= tol_pct and dist_px <= 110 and orient_diff <= 15:
                matches_by_edge[edge_idx].append((num, dist_px, pct_err))
        total = sum(len(v) for v in matches_by_edge.values())
        results_by_tol[tol_pct] = matches_by_edge
        print(f"  tol={tol_pct}%: {total} matches, {len(matches_by_edge)}/{n_edges} edges covered")
        if tol_pct == 3:
            for edge_idx in sorted(matches_by_edge):
                predicted_ft = edge_pred[edge_idx][0] * scale
                for num, dist_px, pct_err in matches_by_edge[edge_idx]:
                    print(f"    edge{edge_idx} (pred={predicted_ft:.1f}ft): {num['line'].text!r} "
                          f"dist_px={dist_px:.0f} err={pct_err:.1f}%")

    # sanity check: edges with NO matching label anywhere at ANY tolerance up to 5%
    unverified_edges = [i for i in range(n_edges) if i not in results_by_tol[5]]
    print(f"  SANITY CHECK: {len(unverified_edges)}/{n_edges} edges have NO matching printed label "
          f"at any tolerance up to 5% -> ", end="")
    if len(unverified_edges) == n_edges:
        print("shape or placement UNVERIFIED (zero corroboration at all)")
    elif unverified_edges:
        print(f"partially unverified (edges {unverified_edges} uncorroborated)")
    else:
        print("fully corroborated (every edge matched)")

    # explicit reject check for known bad values (>500ft, i.e. combined-tract style)
    for num in numbers:
        if num["value"] and num["value"] > 500:
            x1, y1, x2, y2 = num["line"].bbox
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            edge_idx, dist_px = nearest_edge((cx, cy), polygon)
            length_px, angle_px = edge_pred[edge_idx]
            predicted_ft = length_px * scale
            pct_err = abs(num["value"] - predicted_ft) / predicted_ft * 100 if predicted_ft else 999
            rejected = pct_err > 5 or dist_px > 110
            print(f"  large-value check: {num['line'].text!r} value={num['value']} -> "
                  f"{'REJECTED (correct)' if rejected else 'MATCHED (unexpected)'} (err={pct_err:.1f}%, dist={dist_px:.0f}px)")

    return {"name": name, "n_edges": n_edges, "unverified": len(unverified_edges)}


if __name__ == "__main__":
    outcomes = []
    for cfg in CASES:
        outcomes.append(run_case(cfg))
    print(f"\n{'='*72}\nSUMMARY\n{'='*72}")
    for o in outcomes:
        if o:
            print(f"  {o['name']}: {o['n_edges']} edges, {o['unverified']} fully unverified")
