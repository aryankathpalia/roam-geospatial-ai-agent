"""
Per-page OCR + geometric matching back to ROAM's layout regions.

Dense pages are split into horizontal BANDS so a single crowded page
can't dominate a document's total processing time. Measured directly:
one 70-region page took 68.1s on its own -- almost the entire 95.6s a
whole 11-page document took even with page-level parallel fan-out,
because page-level parallelism only helps once other pages are also
running; it does nothing once every OTHER page has finished and one
dense page is still grinding through alone. Splitting that page into
bands lets ITS OWN work run across multiple containers too.

Each band is OCR'd independently (app.services.ocr.run_page_ocr), line
bounding boxes are offset back to full-page coordinates, then all of a
page's bands are merged and matched to ROAM's layout regions -- the
smallest containing region box wins, so a Seal box nested inside a
larger ParcelMap box correctly claims its own lines.

Band-level OCR is the unit of work fanned out via Modal's .map() (see
modal_app.py's ocr_band_remote) -- pure functions of (image bytes) ->
(line list), no Modal dependency, so they run identically locally and
inside a Modal container.
"""

import io

from PIL import Image

from app.services.ocr import OCRLine, run_page_ocr

OCR_ELIGIBLE_CLASSES = {"Text", "Table", "Seal", "ScannedPrintout", "ParcelMap"}
VISION_CLASSES = {"ParcelMap"}

# Region count thresholds -> how many horizontal bands to split a page
# into. Picked from this session's measurements: a 70-region page took
# 68s as one band: a 4-way split brings that down toward ~20s (roughly
# proportional, since OCR cost scales with area/text volume, not call
# count) while a light 2-5 region page isn't worth splitting at all.
_BAND_THRESHOLDS = [(40, 4), (15, 3), (5, 2)]


def page_needs_ocr(regions: list[dict]) -> bool:
    """True if this page has at least one region worth OCR'ing."""

    return any(r["class"] in OCR_ELIGIBLE_CLASSES for r in regions)


def band_count_for(regions: list[dict]) -> int:
    """How many horizontal bands to split this page's image into."""

    eligible_count = sum(1 for r in regions if r["class"] in OCR_ELIGIBLE_CLASSES)
    for threshold, bands in _BAND_THRESHOLDS:
        if eligible_count > threshold:
            return bands
    return 1


def split_page_into_bands(
    image: Image.Image, num_bands: int
) -> list[tuple[bytes, float]]:
    """
    Split a page into `num_bands` horizontal strips, returning each
    strip as PNG bytes alongside its y-offset in the original page's
    coordinates (needed to map OCR'd line boxes back afterward).
    """

    width, height = image.size
    band_height = height / num_bands

    bands: list[tuple[bytes, float]] = []
    for i in range(num_bands):
        y0 = int(i * band_height)
        y1 = int((i + 1) * band_height) if i < num_bands - 1 else height

        band_image = image.crop((0, y0, width, y1))
        buffer = io.BytesIO()
        band_image.save(buffer, format="PNG")
        bands.append((buffer.getvalue(), float(y0)))

    return bands


def ocr_band(band_png_bytes: bytes, y_offset: float) -> list[OCRLine]:
    """
    OCR one horizontal band, returning its lines with bounding boxes
    already offset back to full-page coordinates.
    """

    image = Image.open(io.BytesIO(band_png_bytes)).convert("RGB")
    lines = run_page_ocr(image)

    return [
        OCRLine(
            text=line.text,
            confidence=line.confidence,
            bbox=(
                line.bbox[0],
                line.bbox[1] + y_offset,
                line.bbox[2],
                line.bbox[3] + y_offset,
            ),
        )
        for line in lines
    ]


def _line_center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    x1, y1, x2, y2 = bbox
    return (x1 + x2) / 2, (y1 + y2) / 2


def _point_in_region(px: float, py: float, region_bbox: list[float]) -> bool:
    x, y, w, h = region_bbox
    return x <= px <= x + w and y <= py <= y + h


def match_lines_to_regions(lines: list[OCRLine], regions: list[dict]) -> None:
    """Mutates `regions` in place, filling extraction_status/ocr_text/ocr_confidence."""

    eligible = [r for r in regions if r["class"] in OCR_ELIGIBLE_CLASSES]
    buckets: dict[int, list[OCRLine]] = {id(r): [] for r in eligible}

    for line in lines:
        px, py = _line_center(line.bbox)

        best_region = None
        best_area = None
        for region in eligible:
            if _point_in_region(px, py, region["bbox"]):
                _, _, w, h = region["bbox"]
                area = w * h
                if best_area is None or area < best_area:
                    best_region = region
                    best_area = area

        if best_region is not None:
            buckets[id(best_region)].append(line)

    for region in regions:
        if region["class"] not in OCR_ELIGIBLE_CLASSES:
            region["extraction_status"] = "skipped"
            continue

        lines_here = buckets.get(id(region), [])
        region["extraction_status"] = "ocr_complete"
        region["ocr_text"] = "\n".join(line.text for line in lines_here)
        region["ocr_confidence"] = (
            round(sum(line.confidence for line in lines_here) / len(lines_here), 3)
            if lines_here
            else None
        )

        if region["class"] in VISION_CLASSES:
            region["needs_vision"] = True
