from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image


def analyze_page(image_path: str) -> dict[str, Any]:
    """
    Build a structural fingerprint of a rendered document page.

    This stage measures page structure only.
    It does NOT perform OCR, Vision, or semantic classification.
    """

    path = Path(image_path)

    if not path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    # ---------------------------------------------------------
    # Load image
    # ---------------------------------------------------------

    image = Image.open(path).convert("L")
    pixels = np.asarray(image, dtype=np.uint8)

    height, width = pixels.shape

    # ---------------------------------------------------------
    # Basic image statistics
    # Kept for diagnostics only.
    # These should NOT drive routing.
    # ---------------------------------------------------------

    brightness = float(pixels.mean())
    contrast = float(pixels.std())
    dark_pixel_ratio = float((pixels < 100).mean())

    # ---------------------------------------------------------
    # Threshold image
    #
    # Black = foreground / ink
    # White = background
    # ---------------------------------------------------------

    _, binary = cv2.threshold(
        pixels,
        200,
        255,
        cv2.THRESH_BINARY_INV,
    )

    # ---------------------------------------------------------
    # 1. Horizontal projection profile
    #
    # Text produces repeated horizontal bands.
    # We measure how regularly those bands occur.
    # ---------------------------------------------------------

    horizontal_projection = (binary > 0).sum(axis=1)

    # Rows containing meaningful foreground pixels.
    row_threshold = max(5, int(width * 0.002))
    active_rows = horizontal_projection > row_threshold

    # Find contiguous horizontal runs.
    runs = []
    start = None

    for i, active in enumerate(active_rows):
        if active and start is None:
            start = i

        elif not active and start is not None:
            runs.append((start, i - 1))
            start = None

    if start is not None:
        runs.append((start, height - 1))

    # Remove extremely large runs caused by borders/maps.
    line_runs = [
        (start, end)
        for start, end in runs
        if 1 <= (end - start + 1) <= 20
    ]

    line_centers = [
        (start + end) / 2
        for start, end in line_runs
    ]

    # Distances between consecutive horizontal runs.
    spacings = np.diff(line_centers)

    if len(spacings) >= 3:
        spacing_std = float(np.std(spacings))
        spacing_mean = float(np.mean(spacings))

        if spacing_mean > 0:
            text_line_periodicity = float(
                1.0 / (1.0 + spacing_std / spacing_mean)
            )
        else:
            text_line_periodicity = 0.0
    else:
        text_line_periodicity = 0.0

    # ---------------------------------------------------------
    # 2. Connected components
    #
    # Text tends to create many small components.
    # Maps can contain long/large components.
    # ---------------------------------------------------------

    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        binary,
        connectivity=8,
    )

    # Ignore background.
    component_stats = stats[1:]

    if len(component_stats) > 0:

        areas = component_stats[:, cv2.CC_STAT_AREA]
        component_widths = component_stats[:, cv2.CC_STAT_WIDTH]
        component_heights = component_stats[:, cv2.CC_STAT_HEIGHT]

        component_count = int(len(component_stats))

        small_components = areas <= 100
        large_components = areas >= 1000

        small_component_ratio = float(
            small_components.mean()
        )

        large_component_ratio = float(
            large_components.mean()
        )

        # Long thin components are especially interesting
        # for parcel boundaries and diagram geometry.
        elongated = (
            (component_widths >= 100)
            & (component_widths / np.maximum(component_heights, 1) >= 5)
        ) | (
            (component_heights >= 100)
            & (component_heights / np.maximum(component_widths, 1) >= 5)
        )

        elongated_component_ratio = float(
            elongated.mean()
        )

    else:
        component_count = 0
        small_component_ratio = 0.0
        large_component_ratio = 0.0
        elongated_component_ratio = 0.0


    # ---------------------------------------------------------
    # 3. Hough line detection
    #
    # Detect long straight geometry.
    # ---------------------------------------------------------

    edges = cv2.Canny(
        pixels,
        threshold1=50,
        threshold2=150,
    )

    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180,
        threshold=100,
        minLineLength=max(50, int(min(width, height) * 0.05)),
        maxLineGap=10,
    )

    line_lengths = []
    line_angles = []

    if lines is not None:

        # OpenCV normally returns (N, 1, 4), but normalize
        # whatever shape we receive into rows of [x1,y1,x2,y2].
        lines = np.asarray(lines).reshape(-1, 4)

        for x1, y1, x2, y2 in lines:

            dx = x2 - x1
            dy = y2 - y1

            length = float(np.sqrt(dx * dx + dy * dy))
            angle = float(np.degrees(np.arctan2(dy, dx)))

            line_lengths.append(length)
            line_angles.append(angle)

    if line_lengths:

        line_count = len(line_lengths)

        long_line_threshold = min(width, height) * 0.10

        long_line_ratio = float(
            np.mean(
                np.array(line_lengths) >= long_line_threshold
            )
        )

        angles = np.abs(np.array(line_angles)) % 180

        horizontal_or_vertical = (
            (angles <= 10)
            | (np.abs(angles - 90) <= 10)
            | (angles >= 170)
        )

        angled_line_ratio = float(
            1.0 - np.mean(horizontal_or_vertical)
        )

    else:

        line_count = 0
        long_line_ratio = 0.0
        angled_line_ratio = 0.0

    # ---------------------------------------------------------
    # Edge density
    #
    # Reuses the Canny edge map computed above for Hough lines.
    # ---------------------------------------------------------

    edge_density = float((edges > 0).mean())

    # ---------------------------------------------------------
    # Composition label
    #
    # Minimal categorical summary so this analyzer keeps
    # satisfying page_router.py's existing contract. Not a
    # redesign of routing logic — that lives in page_router.py.
    # ---------------------------------------------------------

    if contrast < 15 and edge_density < 0.01:
        composition = "DEGRADED"
    elif (
        elongated_component_ratio > 0.05
        or angled_line_ratio > 0.3
        or long_line_ratio > 0.1
    ) and text_line_periodicity < 0.5:
        composition = "VISUAL_COMPLEX"
    else:
        composition = "SIMPLE"

    return {
        "brightness": brightness,
        "contrast": contrast,
        "dark_pixel_ratio": dark_pixel_ratio,
        "edge_density": edge_density,
        "composition": composition,
        "text_line_periodicity": text_line_periodicity,
        "component_count": component_count,
        "small_component_ratio": small_component_ratio,
        "large_component_ratio": large_component_ratio,
        "elongated_component_ratio": elongated_component_ratio,
        "line_count": line_count,
        "long_line_ratio": long_line_ratio,
        "angled_line_ratio": angled_line_ratio,
    }