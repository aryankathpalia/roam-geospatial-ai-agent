"""
Per-class region extraction -- the ExtractorFn plugged into
streaming_processor.process_document_streaming's on_page_ready slot.

Routing, per the classes ROAM's layout model predicts:
    Text / Table / Seal / ScannedPrintout -> OCR (PaddleOCR, mobile preset)
    ParcelMap                             -> OCR now; vision escalation
                                              (Gemini Flash) is a separate,
                                              not-yet-wired step -- flagged
                                              here as needs_vision=True
                                              rather than skipped, so the
                                              caller knows more work is owed
    Picture                               -> skipped entirely, by design
                                              (see project routing table)

OCR is CPU-bound and blocking (PaddleOCR has no native asyncio API), so
each call runs in the default thread pool executor -- this is what lets
region extraction for one page overlap with layout detection of the
next page in the streaming pipeline.
"""

import asyncio

from app.services.ocr import run_ocr

OCR_CLASSES = {"Text", "Table", "Seal", "ScannedPrintout", "ParcelMap"}
SKIPPED_CLASSES = {"Picture"}
VISION_CLASSES = {"ParcelMap"}


async def extract_page_regions(page_regions: dict) -> None:
    """Mutates each region in place with its extraction result."""

    loop = asyncio.get_running_loop()

    for region in page_regions["regions"]:
        roam_class = region["class"]
        crop_image = region.get("_crop_image")

        if roam_class in SKIPPED_CLASSES:
            region["extraction_status"] = "skipped"
            continue

        if roam_class not in OCR_CLASSES or crop_image is None:
            region["extraction_status"] = "skipped"
            continue

        ocr_lines = await loop.run_in_executor(None, run_ocr, crop_image)

        region["extraction_status"] = "ocr_complete"
        region["ocr_text"] = "\n".join(line.text for line in ocr_lines)
        region["ocr_confidence"] = (
            round(sum(line.confidence for line in ocr_lines) / len(ocr_lines), 3)
            if ocr_lines
            else None
        )

        if roam_class in VISION_CLASSES:
            region["needs_vision"] = True
