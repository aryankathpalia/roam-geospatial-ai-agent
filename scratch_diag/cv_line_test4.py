import cv2
import numpy as np
from pathlib import Path

OUT_DIR = Path("scratch_diag/cv_line_test_out")
OUT_DIR.mkdir(exist_ok=True)

CASES = {
    "payette_packet_p28": "scratch_diag/new_pages/packet_p28.png",
    "plainfield_petition_p5": "scratch_diag/new_pages/petition_p5.png",
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


def run_case(name, path, thresh=200, min_len_bold=150, close_ksize=5):
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
    long_bold = [r for r in bold if r["length"] >= min_len_bold]
    all_solidish = [r for r in results if r["on_ratio"] >= 0.8 and r["length"] >= min_len_bold]
    print(f"bold(all widths): {len(bold)}, bold+long: {len(long_bold)}, ANY solid-ish+long (width-agnostic): {len(all_solidish)}")

    out = np.ones_like(img) * 255
    for r in long_bold:
        cv2.line(out, (r["x1"], r["y1"]), (r["x2"], r["y2"]), (0, 0, 255), 2)
    cv2.imwrite(str(OUT_DIR / f"{name}_bold_long.png"), out)

    out2 = np.ones_like(img) * 255
    for r in all_solidish:
        cv2.line(out2, (r["x1"], r["y1"]), (r["x2"], r["y2"]), (0, 0, 0), 2)
    cv2.imwrite(str(OUT_DIR / f"{name}_solidish_long.png"), out2)

    # contours, with and without morphological closing
    for label, use_close in [("raw", False), ("closed5", True)]:
        proc = binary
        if use_close:
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_ksize, close_ksize))
            proc = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        contours, hierarchy = cv2.findContours(proc, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        big = [(i, c) for i, c in enumerate(contours) if cv2.contourArea(c) > 3000]
        big.sort(key=lambda ic: -cv2.contourArea(ic[1]))
        print(f"[{label}] contours total: {len(contours)}, area>3000: {len(big)}")
        for i, c in big[:10]:
            area = cv2.contourArea(c)
            x, y, cw, ch = cv2.boundingRect(c)
            parent = hierarchy[0][i][3]
            print(f"    idx={i} area={area:.0f} bbox=({x},{y},{cw},{ch}) parent={parent}")

        overlay = img.copy()
        colors = [(0, 255, 0), (255, 0, 255), (0, 165, 255), (255, 255, 0), (0, 0, 255), (255,128,0), (0,255,255)]
        for k, (i, c) in enumerate(big[:10]):
            cv2.drawContours(overlay, [c], -1, colors[k % len(colors)], 3)
        cv2.imwrite(str(OUT_DIR / f"{name}_contours_{label}.png"), overlay)
    print(f"saved outputs for {name}")


if __name__ == "__main__":
    for name, path in CASES.items():
        run_case(name, path)
