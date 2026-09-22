"""
Main document processing pipeline: render + detect every page (fast,
sequential), then fan out per-page OCR concurrently for only the pages
that actually have OCR-eligible regions.

The OCR fan-out is pluggable (`ocr_dispatcher`). Locally, pages just
run one after another in a thread pool -- fine for dev. On Modal,
modal_app.py overrides DEFAULT_OCR_DISPATCHER with one that fans pages
out to separate containers via Function.map(), which is what actually
matters: detection is cheap (~1-2s/page), OCR is the real cost
(~15-25s/page), and running N pages' OCR in N parallel containers
turns "sum of every page's OCR time" into "roughly the slowest page's
OCR time".
"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.services.layout_detector_onnx import detect_page_layout
from app.services.pdf_inspector import inspect_pdf
from app.services.pdf_renderer import render_page
from app.services.region_cropper import extract_region_crops
from app.pipeline.page_ocr import ocr_and_match_page, page_needs_ocr

DOCUMENT_ROOT = Path("data/documents")

# (page_png_bytes, regions) in -> updated regions out, one pair per OCR job.
OcrJob = tuple[bytes, list[dict]]
OcrDispatcher = Callable[[list[OcrJob]], Awaitable[list[list[dict]]]]


async def _default_ocr_dispatcher(jobs: list[OcrJob]) -> list[list[dict]]:
    """Local-dev fallback: pages processed one after another in a thread."""

    loop = asyncio.get_running_loop()
    results = []
    for page_bytes, regions in jobs:
        updated = await loop.run_in_executor(None, ocr_and_match_page, page_bytes, regions)
        results.append(updated)
    return results


# Modal's deployment entrypoint overwrites this once at container
# startup (see modal_app.py) -- process_document() always reads it
# fresh at call time, so the override takes effect for every request.
DEFAULT_OCR_DISPATCHER: OcrDispatcher = _default_ocr_dispatcher


async def process_document(
    document_id: str, ocr_dispatcher: OcrDispatcher | None = None
) -> dict[str, Any]:
    if ocr_dispatcher is None:
        ocr_dispatcher = DEFAULT_OCR_DISPATCHER

    document_dir = DOCUMENT_ROOT / document_id
    pdf_path = document_dir / "original.pdf"

    if not pdf_path.exists():
        raise FileNotFoundError(f"Document not found: {document_id}")

    inspection = inspect_pdf(str(pdf_path))
    pages_dir = document_dir / "pages"
    review_crops_dir = document_dir / "review_crops"

    loop = asyncio.get_running_loop()
    page_entries: list[dict[str, Any]] = []

    # ---------------------------------------------------------
    # Render + detect every page. Fast enough (~1-2s/page) that it's
    # not worth parallelizing -- the real cost is OCR, handled below.
    # ---------------------------------------------------------

    for page_number in range(1, inspection["page_count"] + 1):
        output_path = pages_dir / f"page_{page_number:03d}.png"

        render_page(
            pdf_path=str(pdf_path),
            page_number=page_number,
            output_path=str(output_path),
            dpi=200,
        )

        detections = await loop.run_in_executor(
            None, detect_page_layout, str(output_path)
        )

        crops = extract_region_crops(
            str(output_path),
            detections,
            save_dir=str(review_crops_dir),
            save_only_needs_review=True,
        )

        regions = [
            {
                "class": crop.roam_class,
                "confidence": round(crop.confidence, 3),
                "bbox": [round(v, 1) for v in crop.bbox],
                "needs_review": crop.needs_review,
                "review_crop_path": crop.saved_path,
            }
            for crop in crops
        ]

        page_entries.append(
            {"page_number": page_number, "path": output_path, "regions": regions}
        )

    # ---------------------------------------------------------
    # Fan out OCR for only the pages that have something worth
    # reading -- a pure-Picture or empty page costs nothing here.
    # ---------------------------------------------------------

    ocr_jobs: list[OcrJob] = []
    ocr_job_entries: list[dict[str, Any]] = []

    for entry in page_entries:
        if page_needs_ocr(entry["regions"]):
            ocr_jobs.append((entry["path"].read_bytes(), entry["regions"]))
            ocr_job_entries.append(entry)
        else:
            for region in entry["regions"]:
                region["extraction_status"] = "skipped"

    if ocr_jobs:
        updated_regions_list = await ocr_dispatcher(ocr_jobs)
        for entry, updated_regions in zip(ocr_job_entries, updated_regions_list):
            entry["regions"] = updated_regions

    pages_result = [
        {"page_number": e["page_number"], "regions": e["regions"]}
        for e in page_entries
    ]

    pages_needing_review = sum(
        1 for page in pages_result if any(r["needs_review"] for r in page["regions"])
    )

    return {
        "document_id": document_id,
        "inspection": inspection,
        "pages": pages_result,
        "pages_needing_review": pages_needing_review,
    }
