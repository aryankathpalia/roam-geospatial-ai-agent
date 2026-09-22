from uuid import uuid4
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.pipeline.extract_regions import extract_page_regions
from app.pipeline.streaming_processor import process_document_streaming
from app.services.pre_annotation import generate_pre_annotations


router = APIRouter(
    prefix="/documents",
    tags=["documents"],
)


DOCUMENT_ROOT = Path("data/documents")


@router.post("/upload")
async def upload_document(file: UploadFile = File(...)):
    """
    Upload a PDF document and start the ROAM processing pipeline.
    """

    # --------------------------------------------------
    # 1. Validate uploaded file
    # --------------------------------------------------

    if file.content_type != "application/pdf":
        raise HTTPException(
            status_code=400,
            detail="Only PDF documents are supported.",
        )

    # --------------------------------------------------
    # 2. Create a unique document ID
    # --------------------------------------------------

    document_id = str(uuid4())

    document_dir = DOCUMENT_ROOT / document_id
    document_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------
    # 3. Save the original PDF
    # --------------------------------------------------

    pdf_path = document_dir / "original.pdf"

    contents = await file.read()
    pdf_path.write_bytes(contents)

    # --------------------------------------------------
    # 4. Run the ROAM processing pipeline: layout detection, streamed
    # per-page, with each page's regions sent for OCR/vision
    # extraction as soon as that page is detected (see
    # app/pipeline/streaming_processor.py).
    # --------------------------------------------------

    try:
        pipeline_result = await process_document_streaming(
            document_id, extract_page_regions
        )

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Document processing failed: {exc}",
        ) from exc

    # --------------------------------------------------
    # 5. Return processing result
    # --------------------------------------------------

    return {
        "document_id": document_id,
        "filename": file.filename,
        "status": "processed",
        "result": pipeline_result,
    }


@router.post("/{document_id}/annotate")
def annotate_document(document_id: str):
    """
    (Re-)generate draft layout annotations for an already-processed
    document. Useful for documents rendered outside the upload flow, or
    to regenerate annotations after the layout detector is fine-tuned.

    Output is a draft for human review in CVAT, never ground truth.
    """

    document_dir = DOCUMENT_ROOT / document_id

    if not document_dir.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Document not found: {document_id}",
        )

    try:
        result = generate_pre_annotations(document_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Pre-annotation failed: {exc}",
        ) from exc

    return {
        "document_id": document_id,
        "status": "annotated",
        "result": result,
    }