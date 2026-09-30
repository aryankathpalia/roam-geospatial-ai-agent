"""
Rerun of the association-accuracy validation using the ACTUAL strict
parsers (parse_bearing / parse_distance from app/services/geometry.py) to
select genuine calls, instead of the loose regex from assoc_test.py.

Adds the many-to-one resolution rule: when multiple genuine calls land on
the same confirmed-polygon edge, don't treat every label as independent
evidence -- solve a consensus (scale, rotation) from edges that have
exactly ONE candidate (unambiguous "seed" edges), then for each
multi-candidate edge, pick whichever candidate's implied (scale, rotation)
agrees best with that consensus.

Throwaway diagnostic, no pipeline integration.
"""

import sys
import io
import math
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import cv2
import numpy as np
from PIL import Image
from app.services.ocr import run_parcelmap_ocr
from app.services.geometry import parse_bearing, parse_distance

OUT_DIR = Path("scratch_diag/cv_line_test_out")
OUT_DIR.mkdir(exist_ok=True)

CASES = {
    "27211ae5_shared_edge": {
        "path": "data/documents/27211ae5-0099-4b0d-8176-f1483897b766/pages/page_013.png",
        "polygon": [[1145, 1123], [1652, 1434], [2005, 1163], [2392, 1827], [1125, 1828]],
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


ASSOC_DIST_THRESHOLD = 130  # px -- a genuine call more than this from any edge isn't confidently associated


def edge_pixel_geometry(polygon, i):
    n = len(polygon)
    ax, ay = polygon[i]
    bx, by = polygon[(i + 1) % n]
    length_px = math.hypot(bx - ax, by - ay)
    # pixel-space direction angle, clockwise from "up" (-y), to match a
    # compass bearing's own clockwise-from-north convention
    angle_deg = math.degrees(math.atan2(bx - ax, -(by - ay))) % 360
    return length_px, angle_deg


def implied_scale_rotation(call_distance_ft, call_bearing_az, edge_length_px, edge_angle_px):
    """One matched edge => one implied (scale ft/px, rotation deg)."""
    scale = call_distance_ft / edge_length_px if edge_length_px else None
    rotation = (call_bearing_az - edge_angle_px) % 360
    return scale, rotation


def circular_diff(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def run_case(name, cfg):
    print(f"\n{'='*70}\n{name}\n{'='*70}")
    img = Image.open(cfg["path"])
    lines, _ = run_parcelmap_ocr(img)
    polygon = cfg["polygon"]

    # STRICT genuine-call selection:
    # - a bearing call: parse_bearing succeeds on the OCR text (the
    #   structured N/S..E/W regex from geometry.py -- this is the real
    #   parser the pipeline itself uses to walk calls, not a loose filter).
    # - a standalone distance call: the OCR text, once stripped of a
    #   trailing quote mark, is NOTHING BUT a number (rules out numbers
    #   embedded in prose/notes, which the loose regex in assoc_test.py
    #   let through).
    genuine = []
    for line in lines:
        text = line.text.strip()
        az = parse_bearing(text)
        if az is not None:
            dist = parse_distance(text)
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

    print(f"genuine calls (strict parser): {len(genuine)}")

    by_edge = defaultdict(list)
    unassociated = []
    for g in genuine:
        x1, y1, x2, y2 = g["line"].bbox
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        edge_idx, dist_px = nearest_edge((cx, cy), polygon)
        g["center"] = (cx, cy)
        g["edge_dist_px"] = dist_px
        if dist_px <= ASSOC_DIST_THRESHOLD:
            by_edge[edge_idx].append(g)
        else:
            unassociated.append(g)

    print(f"associated (<= {ASSOC_DIST_THRESHOLD}px of an edge): {sum(len(v) for v in by_edge.values())}")
    print(f"unassociated (too far from any edge): {len(unassociated)}")
    for g in unassociated:
        print(f"    [unassoc] {g['kind']:16s} text={g['line'].text!r} nearest_edge_dist={g['edge_dist_px']:.0f}px")

    for edge_idx in sorted(by_edge):
        cands = by_edge[edge_idx]
        length_px, angle_px = edge_pixel_geometry(polygon, edge_idx)
        print(f"\n  edge{edge_idx}  (pixel length={length_px:.0f}px, pixel angle={angle_px:.1f}deg)  -- {len(cands)} candidate(s):")
        for g in cands:
            print(f"    text={g['line'].text!r:45s} kind={g['kind']:16s} dist_px={g['edge_dist_px']:.0f}")

    # --- many-to-one resolution ---
    # Seed edges: exactly one bearing+distance candidate -> gives a
    # direct (scale, rotation) estimate with no ambiguity to resolve.
    seed_estimates = []
    for edge_idx, cands in by_edge.items():
        bd = [c for c in cands if c["kind"] == "bearing+distance" and c["distance_ft"]]
        if len(bd) == 1:
            length_px, angle_px = edge_pixel_geometry(polygon, edge_idx)
            scale, rotation = implied_scale_rotation(bd[0]["distance_ft"], bd[0]["azimuth"], length_px, angle_px)
            if scale:
                seed_estimates.append((edge_idx, scale, rotation))

    print(f"\n  seed (unambiguous) edges: {len(seed_estimates)} -> {[(i, round(s,4), round(r,1)) for i,s,r in seed_estimates]}")

    if seed_estimates:
        consensus_scale = float(np.median([s for _, s, _ in seed_estimates]))
        # circular median approximated via mean of unit vectors -- fine
        # for a small, mostly-consistent set
        rot_vecs = [(math.cos(math.radians(r)), math.sin(math.radians(r))) for _, _, r in seed_estimates]
        mean_vec = (sum(v[0] for v in rot_vecs) / len(rot_vecs), sum(v[1] for v in rot_vecs) / len(rot_vecs))
        consensus_rotation = math.degrees(math.atan2(mean_vec[1], mean_vec[0])) % 360
        print(f"  consensus: scale={consensus_scale:.4f} ft/px, rotation={consensus_rotation:.1f} deg")

        print("\n  --- resolving multi-candidate edges against consensus ---")
        for edge_idx, cands in by_edge.items():
            if len(cands) < 2:
                continue
            length_px, angle_px = edge_pixel_geometry(polygon, edge_idx)
            print(f"  edge{edge_idx}:")
            scored = []
            for g in cands:
                if g["kind"] != "bearing+distance" or not g["distance_ft"]:
                    # distance-only candidates: score purely on scale agreement
                    if g["distance_ft"]:
                        implied_scale = g["distance_ft"] / length_px
                        scale_resid = abs(implied_scale - consensus_scale) / consensus_scale
                        scored.append((scale_resid, g, implied_scale, None))
                    continue
                scale, rotation = implied_scale_rotation(g["distance_ft"], g["azimuth"], length_px, angle_px)
                scale_resid = abs(scale - consensus_scale) / consensus_scale
                rot_resid = circular_diff(rotation, consensus_rotation) / 180
                combined = scale_resid + rot_resid
                scored.append((combined, g, scale, rotation))
            scored.sort(key=lambda t: t[0])
            for resid, g, scale, rotation in scored:
                flag = "<-- CHOSEN" if resid == scored[0][0] else "    rejected"
                print(f"      resid={resid:.3f}  text={g['line'].text!r:40s} implied_scale={scale:.4f}  {flag}")

    # overlay for visual sanity check
    cv_img = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    n = len(polygon)
    for i in range(n):
        cv2.line(cv_img, tuple(polygon[i]), tuple(polygon[(i + 1) % n]), (255, 0, 0), 3)
    colors = [(0, 165, 255), (0, 255, 255), (255, 0, 255), (0, 255, 0), (0, 100, 255)]
    for edge_idx, cands in by_edge.items():
        for g in cands:
            cx, cy = g["center"]
            cv2.circle(cv_img, (int(cx), int(cy)), 10, colors[edge_idx % len(colors)], 3)
    for g in unassociated:
        cx, cy = g["center"]
        cv2.circle(cv_img, (int(cx), int(cy)), 10, (128, 128, 128), 2)
    cv2.imwrite(str(OUT_DIR / f"assoc2_{name}.png"), cv_img)


if __name__ == "__main__":
    for name, cfg in CASES.items():
        run_case(name, cfg)
