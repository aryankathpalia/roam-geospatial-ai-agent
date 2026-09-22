"""
Per-page OCR + geometric matching back to ROAM's layout regions.

Runs ONE OCR pass on the whole page (app.services.ocr.run_page_ocr),
then assigns each detected text line to whichever region's box it
falls inside -- the smallest containing box wins, so a Seal box nested
inside a larger ParcelMap box correctly claims its own lines instead of
losing them to the bigger box.

This is the unit of work fanned out across pages in parallel (see
modal_app.py's ocr_page_remote + .map()). Pure function of
(page image bytes, region list) -> updated region list, with no Modal
dependency, so it runs identically locally and inside a Modal
container.
"""

import io

from PIL import Image

from app.services.ocr import run_page_ocr

OCR_ELIGIBLE_CLASSES = {"Text", "Table", "Seal", "ScannedPrintout", "ParcelMap"}
VISION_CLASSES = {"ParcelMap"}


def page_needs_ocr(regions: list[dict]) -> bool:
    """True if this page has at least one region worth OCR'ing."""

    return any(r["class"] in OCR_ELIGIBLE_CLASSES for r in regions)


def _line_center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    x1, y1, x2, y2 = bbox
    return (x1 + x2) / 2, (y1 + y2) / 2


def _point_in_region(px: float, py: float, region_bbox: list[float]) -> bool:
    x, y, w, h = region_bbox
    return x <= px <= x + w and y <= py <= y + h


def match_lines_to_regions(lines, regions: list[dict]) -> None:
    """Mutates `regions` in place, filling extraction_status/ocr_text/ocr_confidence."""

    eligible = [r for r in regions if r["class"] in OCR_ELIGIBLE_CLASSES]
    buckets: dict[int, list] = {id(r): [] for r in eligible}

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


def ocr_and_match_page(page_png_bytes: bytes, regions: list[dict]) -> list[dict]:
    """Runs one OCR pass on the page and matches lines back to regions."""

    image = Image.open(io.BytesIO(page_png_bytes)).convert("RGB")
    lines = run_page_ocr(image)
    match_lines_to_regions(lines, regions)
    return regions
