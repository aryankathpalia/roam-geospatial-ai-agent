"""
Streaming document pipeline.

The batch pipeline (app/pipeline/processor.py) detects layout on every
page, then only afterwards would extraction begin -- page 1's regions
sit idle while page 20 is still being detected. This module fixes that:
each page's regions are dispatched to extraction (OCR / vision) the
moment THAT page's detection finishes, running concurrently with
detection of the next page, instead of waiting for the whole document.

Layout detection is CPU-bound (ONNX Runtime releases the GIL during
inference, but a single page still takes real wall-clock time). It runs
in a background thread via run_in_executor so the event loop stays free
to run extraction tasks concurrently. Vision extraction calls are
network I/O, so they cost the CPU nothing while a detection call is
running -- true overlap, not just interleaving.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.services.layout_detector_onnx import detect_page_layout
from app.services.pdf_inspector import inspect_pdf
from app.services.pdf_renderer import render_page
from app.services.region_cropper import extract_region_crops

DOCUMENT_ROOT = Path("data/documents")

PageRegions = dict[str, Any]

# Called once per page as soon as that page's regions are ready. Runs
# concurrently with detection of later pages -- must not block the
# event loop (use awaits for I/O, run_in_executor for CPU-bound work).
ExtractorFn = Callable[[PageRegions], Awaitable[None]]


async def _extract_noop(page_regions: PageRegions) -> None:
    """Default extractor: marks regions ready, does no real extraction.

    Placeholder until OCR/vision extraction (pipeline step 4) is wired
    in -- replace with a real async function of the same signature.
    """

    for region in page_regions["regions"]:
        region["extraction_status"] = "not_yet_wired"


async def process_document_streaming(
    document_id: str,
    on_page_ready: ExtractorFn = _extract_noop,
) -> dict[str, Any]:
    """
    Render + detect every page, dispatching each page's regions to
    `on_page_ready` as soon as that page is done -- concurrently with
    detection of subsequent pages, not after the whole document.
    """

    document_dir = DOCUMENT_ROOT / document_id
    pdf_path = document_dir / "original.pdf"

    if not pdf_path.exists():
        raise FileNotFoundError(f"Document not found: {document_id}")

    inspection = inspect_pdf(str(pdf_path))
    pages_dir = document_dir / "pages"
    review_crops_dir = document_dir / "review_crops"

    loop = asyncio.get_running_loop()
    extraction_tasks: list[asyncio.Task] = []
    page_results: list[PageRegions] = []
    timeline: list[dict[str, Any]] = []  # for observability / debugging

    for page_number in range(1, inspection["page_count"] + 1):
        output_path = pages_dir / f"page_{page_number:03d}.png"

        # Rendering is fast (raster to PNG); no need to offload it.
        render_page(
            pdf_path=str(pdf_path),
            page_number=page_number,
            output_path=str(output_path),
            dpi=200,
        )

        # Detection is the CPU-bound step -- run off the event loop so
        # extraction tasks dispatched below can make progress while the
        # NEXT page's detection call is in flight.
        t_start = time.monotonic()
        detections = await loop.run_in_executor(
            None, detect_page_layout, str(output_path)
        )
        t_detected = time.monotonic()

        crops = extract_region_crops(
            str(output_path),
            detections,
            save_dir=str(review_crops_dir),
            save_only_needs_review=True,
        )

        page_regions: PageRegions = {
            "page_number": page_number,
            "regions": [
                {
                    "class": crop.roam_class,
                    "confidence": round(crop.confidence, 3),
                    "bbox": [round(v, 1) for v in crop.bbox],
                    "needs_review": crop.needs_review,
                    "review_crop_path": crop.saved_path,
                    # In-memory only -- an extractor (on_page_ready) reads
                    # this to run OCR/vision. Stripped before the final
                    # result is returned, so it never leaks into a JSON
                    # response.
                    "_crop_image": crop.image,
                }
                for crop in crops
            ],
        }
        page_results.append(page_regions)

        timeline.append({
            "page_number": page_number,
            "detected_at": round(t_detected - t_start, 3),
        })

        # Dispatch extraction NOW, without awaiting it -- the loop moves
        # straight on to rendering/detecting the next page. This task
        # runs concurrently with that work.
        extraction_tasks.append(
            asyncio.create_task(on_page_ready(page_regions))
        )

    # All pages have been detected and their extraction dispatched.
    # Now wait for whatever extraction work is still in flight.
    await asyncio.gather(*extraction_tasks)

    # Strip the in-memory crop images now that extraction has consumed
    # them -- the returned result must stay JSON-serializable.
    for page in page_results:
        for region in page["regions"]:
            region.pop("_crop_image", None)

    return {
        "document_id": document_id,
        "inspection": inspection,
        "pages": page_results,
        "pages_needing_review": sum(
            1 for p in page_results
            if any(r["needs_review"] for r in p["regions"])
        ),
        "timeline": timeline,
    }
