from pathlib import Path
from typing import Any

from app.services.pdf_inspector import inspect_pdf
from app.services.pdf_renderer import render_page
from app.services.page_analyzer import analyze_page
from app.services.page_router import route_page
from app.services.layout_detector_onnx import detect_page_layout
from app.services.region_cropper import extract_region_crops

DOCUMENT_ROOT = Path("data/documents")


def process_document(document_id: str) -> dict[str, Any]:
    """
    Run the ROAM document-processing pipeline.

    Current pipeline:

        1. Inspect PDF
        2. Render every page
        3. Analyze every rendered page
        4. Detect layout regions on every page and crop them

    Region crops are produced in memory for downstream per-class
    extraction (see app/services/region_cropper.py). Only crops flagged
    needs_review are persisted to disk, under
    data/documents/<id>/review_crops/, so a large document doesn't leave
    hundreds of stray crop files behind.
    """

    document_dir = DOCUMENT_ROOT / document_id
    pdf_path = document_dir / "original.pdf"

    if not pdf_path.exists():
        raise FileNotFoundError(
            f"Document not found: {document_id}"
        )

    # ---------------------------------------------------------
    # STEP 1 — Inspect PDF
    # ---------------------------------------------------------

    inspection = inspect_pdf(str(pdf_path))

    # ---------------------------------------------------------
    # STEP 2 + 3 + 4 — Render, analyze, and detect layout regions
    # on every page
    # ---------------------------------------------------------

    pages_dir = document_dir / "pages"
    review_crops_dir = document_dir / "review_crops"
    analyzed_pages = []

    for page_number in range(1, inspection["page_count"] + 1):

        output_path = pages_dir / f"page_{page_number:03d}.png"

        render_page(
            pdf_path=str(pdf_path),
            page_number=page_number,
            output_path=str(output_path),
            dpi=200,
        )

        analysis = analyze_page(str(output_path))

        page_inspection = inspection["pages"][page_number - 1]

        routing = route_page(
            inspection=page_inspection,
            analysis=analysis,
        )

        detections = detect_page_layout(str(output_path))

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

        analyzed_pages.append({
            "page_number": page_number,
            "inspection": page_inspection,
            "analysis": analysis,
            "routing": routing,
            "regions": regions,
        })

    pages_needing_review = sum(
        1 for page in analyzed_pages
        if any(region["needs_review"] for region in page["regions"])
    )

    return {
        "document_id": document_id,
        "inspection": inspection,
        "pages": analyzed_pages,
        "pages_needing_review": pages_needing_review,
    }