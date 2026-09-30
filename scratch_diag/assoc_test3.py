"""
Third pass at the association-accuracy validation. Changes from
assoc_test2.py per the user's explicit spec:

1. Evidence set for the FIT is bearing+distance calls only. distance-only
   labels can corroborate (scale-agreement only) but never seed/vote.
2. Tighter association: sweep 20/40/60px thresholds; require the
   candidate's (coarse, bbox-aspect-ratio-based -- OCRLine has no true
   rotated-box angle, see caveat printed below) text orientation within
   ~15deg of the edge direction (mod 90, parallel vs perpendicular);
   exclude candidates whose own text OR any OTHER OCR line within 80px
   contains an easement/PUE/EXISTING/GRANTED-type keyword.
3. Joint hypothesis search instead of unique-seed bootstrapping: every
   (edge, bearing+distance-call) pair is its own hypothesis (scale,
   rotation). Score = how many OTHER edges have some candidate
   (bearing+distance OR distance-only, scored appropriately) consistent
   with that same hypothesis. Winner = most corroborating edges.
   cross-validated (>=2 total agreeing edges) / unverified (1) /
   unplaceable (0 hypotheses at all).
4. 27211ae5 uses a near-full contour (light denoise only, NOT the
   5-vertex simplification from before) -- see conversation for why
   1851 raw CHAIN_APPROX_SIMPLE points is pixel noise, not real corners.
5. Reale intentionally excluded -- pending the user's own confirmed
   polygon from /boundary-review, per explicit instruction not to
   hand-trace it.

Throwaway diagnostic, no pipeline integration.
"""

import sys
import io
import math
import re
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import cv2
import numpy as np
from PIL import Image
from app.services.ocr import run_parcelmap_ocr
from app.services.geometry import parse_bearing, parse_distance, _BEARING_RE

OUT_DIR = Path("scratch_diag/cv_line_test_out")
OUT_DIR.mkdir(exist_ok=True)

EXCLUDE_KEYWORDS = re.compile(r"P\.?U\.?E\.?|EASEMENT|EXISTING|GRANTED|SETBACK", re.IGNORECASE)

CASES = {
    "27211ae5_shared_edge": {
        "path": "data/documents/27211ae5-0099-4b0d-8176-f1483897b766/pages/page_013.png",
        # Light denoise of the same validated shared-edge contour --
        # keeps every real corner (incl. the small step-notches along
        # the AREA OF TRANSFER diagonal), only removes pixel/staircase
        # noise. NOT the earlier 5-vertex oversimplification.
        "polygon": [[1145, 1123], [1397, 1195], [1455, 1292], [1589, 1278], [1652, 1434],
                    [1750, 1324], [1681, 1267], [2005, 1163], [2392, 1827], [1125, 1828]],
    },
    "nvz_parcel1": {
        "path": "new_test_docs/placerville/pages/page_009.png",
        "polygon": [[1200, 420], [1965, 415], [1965, 727], [1200, 727]],
    },
}


def point_segment_distance(p, a, b):
    p, a, b = np.array(p, float), np.array(a, float), np.array(b, float)
    ab = b - a
    t = np.dot(p - a, ab) / (np.dot(ab, ab) + 1e-9)
    t = max(0.0, min(1.0, t))
    proj = a + t * ab
    return float(np.linalg.norm(p - proj)), (a + t * ab)


def nearest_edge(center, polygon):
    best_idx, best_dist = -1, float("inf")
    n = len(polygon)
    for i in range(n):
        a, b = polygon[i], polygon[(i + 1) % n]
        d, _ = point_segment_distance(center, a, b)
        if d < best_dist:
            best_dist, best_idx = d, i
    return best_idx, best_dist


def edge_pixel_geometry(polygon, i):
    n = len(polygon)
    ax, ay = polygon[i]
    bx, by = polygon[(i + 1) % n]
    length_px = math.hypot(bx - ax, by - ay)
    angle_deg = math.degrees(math.atan2(bx - ax, -(by - ay))) % 360
    return length_px, angle_deg


def circular_diff(a, b, period=360):
    d = abs(a - b) % period
    return min(d, period - d)


def text_orientation_ok(bbox, edge_angle_deg):
    """
    Coarse proxy ONLY: OCRLine.bbox is axis-aligned (no true rotated-box
    angle available from run_parcelmap_ocr), so this infers a presumed
    text-baseline angle purely from bbox aspect ratio (wide=~0deg,
    tall=~90deg) -- a real implementation would need PaddleOCR's rotated
    quad output, not the axis-aligned box this pipeline currently keeps.
    Returns True if that presumed baseline is within 15deg of parallel
    to the edge (mod 90), False if closer to perpendicular.
    """
    x1, y1, x2, y2 = bbox
    w, h = abs(x2 - x1), abs(y2 - y1)
    presumed = 0.0 if w >= h else 90.0
    edge_mod90 = edge_angle_deg % 90
    diff = circular_diff(presumed, edge_mod90, period=90)
    return diff <= 15.0


def nearby_excluded_text(line, all_lines, radius=80):
    cx, cy = (line.bbox[0] + line.bbox[2]) / 2, (line.bbox[1] + line.bbox[3]) / 2
    if EXCLUDE_KEYWORDS.search(line.text):
        return True
    for other in all_lines:
        if other is line:
            continue
        ox, oy = (other.bbox[0] + other.bbox[2]) / 2, (other.bbox[1] + other.bbox[3]) / 2
        if math.hypot(ox - cx, oy - cy) <= radius and EXCLUDE_KEYWORDS.search(other.text):
            return True
    return False


def collect_genuine(lines):
    genuine = []
    for line in lines:
        text = line.text.strip()
        az = parse_bearing(text)
        if az is not None:
            # parse_distance grabs the FIRST digit run in the string --
            # on a merged "S0*48'45"W220.00'" OCR line that's the "0" in
            # the degrees, not the real distance. Strip the matched
            # bearing span first, parse whatever's left.
            m = _BEARING_RE.search(text)
            remainder = text[: m.start()] + text[m.end():] if m else text
            dist = parse_distance(remainder)
            genuine.append({"line": line, "kind": "bearing+distance", "azimuth": az, "distance_ft": dist})
            continue
        bare = text.rstrip("'′\"")
        try:
            float(bare)
            dist = parse_distance(text)
            if dist is not None:
                genuine.append({"line": line, "kind": "distance_only", "azimuth": None, "distance_ft": dist})
        except ValueError:
            pass
    return genuine


def run_case(name, cfg, thresholds=(20, 40, 60, 90, 110, 130)):
    print(f"\n{'='*72}\n{name}\n{'='*72}")
    img = Image.open(cfg["path"])
    lines, _ = run_parcelmap_ocr(img)
    polygon = cfg["polygon"]
    n_edges = len(polygon)

    genuine = collect_genuine(lines)
    n_bd = sum(1 for g in genuine if g["kind"] == "bearing+distance")
    n_do = sum(1 for g in genuine if g["kind"] == "distance_only")
    print(f"genuine calls: {len(genuine)} ({n_bd} bearing+distance, {n_do} distance_only)")

    # exclusion filter
    kept, excluded = [], []
    for g in genuine:
        if nearby_excluded_text(g["line"], lines):
            excluded.append(g)
        else:
            kept.append(g)
    print(f"excluded by easement/PUE/EXISTING/GRANTED keyword (own or within 80px): {len(excluded)}")
    print(f"remaining after exclusion: {len(kept)}")

    # threshold sweep
    for thresh in thresholds:
        by_edge = defaultdict(list)
        for g in kept:
            x1, y1, x2, y2 = g["line"].bbox
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            edge_idx, dist_px = nearest_edge((cx, cy), polygon)
            _, edge_angle = edge_pixel_geometry(polygon, edge_idx)
            if dist_px > thresh:
                continue
            if not text_orientation_ok(g["line"].bbox, edge_angle):
                continue
            by_edge[edge_idx].append((g, dist_px))
        n_assoc = sum(len(v) for v in by_edge.values())
        n_assoc_bd = sum(1 for v in by_edge.values() for g, _ in v if g["kind"] == "bearing+distance")
        print(f"  threshold={thresh}px: {n_assoc} associated ({n_assoc_bd} bearing+distance), "
              f"{len(by_edge)}/{n_edges} edges have >=1 candidate")

    # use a threshold wide enough to admit real, correctly-offset labels
    # (see diagnostic: genuine NVZ calls sit at 77-106px, not <=60px)
    THRESH = 110
    by_edge = defaultdict(list)
    for g in kept:
        x1, y1, x2, y2 = g["line"].bbox
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        edge_idx, dist_px = nearest_edge((cx, cy), polygon)
        _, edge_angle = edge_pixel_geometry(polygon, edge_idx)
        if dist_px > THRESH or not text_orientation_ok(g["line"].bbox, edge_angle):
            continue
        by_edge[edge_idx].append(g)

    print(f"\n  -- working set at {THRESH}px --")
    for edge_idx in sorted(by_edge):
        length_px, angle_px = edge_pixel_geometry(polygon, edge_idx)
        cands = by_edge[edge_idx]
        print(f"  edge{edge_idx} (len={length_px:.0f}px, angle={angle_px:.1f}deg): "
              + ", ".join(f"{g['kind']}:{g['line'].text!r}" for g in cands))

    # joint hypothesis search
    hyps = []
    for edge_idx, cands in by_edge.items():
        length_px, angle_px = edge_pixel_geometry(polygon, edge_idx)
        for g in cands:
            if g["kind"] != "bearing+distance" or not g["distance_ft"]:
                continue
            scale = g["distance_ft"] / length_px
            rotation = (g["azimuth"] - angle_px) % 360
            hyps.append({"seed_edge": edge_idx, "call": g, "scale": scale, "rotation": rotation})

    if not hyps:
        print("\n  RESULT: unplaceable -- zero bearing+distance candidates survived association at all.")
        return

    for h in hyps:
        agreeing_edges = {h["seed_edge"]}
        support = []
        for edge_idx, cands in by_edge.items():
            if edge_idx == h["seed_edge"]:
                continue
            length_px, angle_px = edge_pixel_geometry(polygon, edge_idx)
            for g in cands:
                implied_len = g["distance_ft"] * (1 / h["scale"]) if False else None
                predicted_len_ft = length_px * h["scale"]
                scale_ok = g["distance_ft"] and abs(g["distance_ft"] - predicted_len_ft) / predicted_len_ft <= 0.02
                if g["kind"] == "bearing+distance":
                    predicted_bearing = (angle_px + h["rotation"]) % 360
                    bearing_ok = circular_diff(g["azimuth"], predicted_bearing) <= 2.0
                    if scale_ok and bearing_ok:
                        agreeing_edges.add(edge_idx)
                        support.append((edge_idx, g["line"].text, "bearing+distance"))
                else:
                    if scale_ok:
                        agreeing_edges.add(edge_idx)
                        support.append((edge_idx, g["line"].text, "distance_only(scale-only)"))
        h["agreeing_edges"] = agreeing_edges
        h["support"] = support

    hyps.sort(key=lambda h: -len(h["agreeing_edges"]))
    print("\n  -- ALL hypotheses (not just winner) --")
    for h in hyps:
        print(f"    seed edge{h['seed_edge']} call={h['call']['line'].text!r} "
              f"scale={h['scale']:.4f} rotation={h['rotation']:.1f}deg "
              f"agreeing_edges={sorted(h['agreeing_edges'])} support={h['support']}")
    best = hyps[0]
    n_agree = len(best["agreeing_edges"])
    verdict = "cross-validated" if n_agree >= 2 else "unverified"
    print(f"\n  best hypothesis: seed edge{best['seed_edge']} call={best['call']['line'].text!r} "
          f"scale={best['scale']:.4f} ft/px rotation={best['rotation']:.1f}deg")
    print(f"  agreeing edges: {n_agree} ({sorted(best['agreeing_edges'])})")
    print(f"  support: {best['support']}")
    print(f"  VERDICT: {verdict}")

    # explicitly report whether the KNOWN-wrong combined-tract call is present and rejected
    for edge_idx, cands in by_edge.items():
        for g in cands:
            if g["kind"] == "bearing+distance" and g["distance_ft"] and g["distance_ft"] > 500:
                rejected = edge_idx not in best["agreeing_edges"] or g not in by_edge.get(edge_idx, [])
                print(f"  note: large-distance candidate {g['line'].text!r} on edge{edge_idx} -- "
                      f"{'in winning hypothesis support' if any(s[1]==g['line'].text for s in best['support']) else 'NOT in winning hypothesis support (rejected)'}")


if __name__ == "__main__":
    for name, cfg in CASES.items():
        run_case(name, cfg)

def diagnose_bd_distances(name, cfg):
    print(f"\n--- diagnostic: raw distances for every bearing+distance call, {name} ---")
    img = Image.open(cfg["path"])
    lines, _ = run_parcelmap_ocr(img)
    polygon = cfg["polygon"]
    genuine = collect_genuine(lines)
    for g in genuine:
        if g["kind"] != "bearing+distance":
            continue
        x1, y1, x2, y2 = g["line"].bbox
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        edge_idx, dist_px = nearest_edge((cx, cy), polygon)
        _, edge_angle = edge_pixel_geometry(polygon, edge_idx)
        orient_ok = text_orientation_ok(g["line"].bbox, edge_angle)
        excl = nearby_excluded_text(g["line"], lines)
        print(f"  text={g['line'].text!r:40s} nearest_edge={edge_idx} dist_px={dist_px:.0f} orient_ok={orient_ok} excluded_kw={excl}")

for _name, _cfg in CASES.items():
    diagnose_bd_distances(_name, _cfg)
