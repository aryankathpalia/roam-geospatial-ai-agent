import json
from uuid import uuid4
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from PIL import Image
from pydantic import BaseModel

from app.pipeline.document_pipeline import (
    process_document,
    recompute_parcel_from_calls,
)
from app.services.pre_annotation import generate_pre_annotations
from app.services.vision import extract_parcel_geometries_batch


router = APIRouter(
    prefix="/documents",
    tags=["documents"],
)


DOCUMENT_ROOT = Path("data/documents")


def _result_path(document_id: str) -> Path:
    return DOCUMENT_ROOT / document_id / "result.json"


def _load_result(document_id: str) -> dict:
    result_path = _result_path(document_id)
    if not result_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No processed result found for document: {document_id}",
        )
    return json.loads(result_path.read_text(encoding="utf-8"))


def _save_result(document_id: str, result: dict) -> None:
    _result_path(document_id).write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )


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
    # 4. Run the ROAM processing pipeline: render + detect every page,
    # then fan out per-page OCR concurrently (see
    # app/pipeline/document_pipeline.py).
    # --------------------------------------------------

    try:
        pipeline_result = await process_document(document_id)

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Document processing failed: {exc}",
        ) from exc

    # --------------------------------------------------
    # 5. Persist the result so it can be reloaded for review later --
    # otherwise it only ever existed in this response.
    # --------------------------------------------------

    _save_result(document_id, pipeline_result)

    return {
        "document_id": document_id,
        "filename": file.filename,
        "status": "processed",
        "result": pipeline_result,
    }


@router.get("/{document_id}")
def get_document(document_id: str):
    """
    Reload a previously-processed document's stored result, for the
    review UI to resume without re-uploading/re-processing.
    """

    result = _load_result(document_id)
    return {
        "document_id": document_id,
        "status": "processed",
        "result": result,
    }


class RecomputeRequest(BaseModel):
    page_number: int
    region_index: int
    parcel_index: int
    boundary_calls: list[dict]


@router.post("/{document_id}/recompute")
def recompute_parcel(document_id: str, body: RecomputeRequest):
    """
    Recompute one parcel's geometry/validation from a human-edited
    boundary_calls list (the review UI's editable call table), and
    persist the result in place so the edit survives a reload.
    """

    result = _load_result(document_id)

    page = next(
        (p for p in result["pages"] if p["page_number"] == body.page_number), None
    )
    if page is None:
        raise HTTPException(status_code=404, detail=f"No page {body.page_number}")
    if not (0 <= body.region_index < len(page["regions"])):
        raise HTTPException(status_code=404, detail="Region index out of range")
    region = page["regions"][body.region_index]
    parcels = region.get("parcels") or []
    if not (0 <= body.parcel_index < len(parcels)):
        raise HTTPException(status_code=404, detail="Parcel index out of range")
    parcel = parcels[body.parcel_index]

    updated = recompute_parcel_from_calls(
        body.boundary_calls,
        stated_area_acres=parcel.get("vision_geometry", {}).get("stated_area_acres"),
        region_ocr_text=region.get("ocr_text") or "",
        anchor_lat=result.get("anchor_lat"),
        anchor_lon=result.get("anchor_lon"),
    )

    # Keep the original vision_geometry (what the model originally read)
    # for reference, but replace everything downstream of it with the
    # human-edited recompute. assembly_notes from the automated resolvers
    # no longer apply to hand-edited calls, so they're cleared here.
    parcel.pop("assembly_notes", None)
    parcel.pop("extraction_note", None)
    parcel.pop("georeference_error", None)
    parcel.pop("boundary_geojson", None)
    parcel.pop("boundary_geojson_wgs84", None)
    parcel.pop("spatial_validation", None)
    parcel.update(updated)
    parcel["human_edited"] = True

    _save_result(document_id, result)

    return {"document_id": document_id, "parcel": parcel}


class ReextractRequest(BaseModel):
    page_number: int
    region_index: int
    parcel_index: int


@router.post("/{document_id}/reextract")
async def reextract_parcel(document_id: str, body: ReextractRequest):
    """
    Re-run vision extraction on just one parcel's own region crop (a
    small, single-drawing image reads more reliably than the whole
    sheet) and return the fresh reading for the reviewer to accept or
    reject -- never applied automatically.
    """

    result = _load_result(document_id)

    page = next(
        (p for p in result["pages"] if p["page_number"] == body.page_number), None
    )
    if page is None:
        raise HTTPException(status_code=404, detail=f"No page {body.page_number}")
    if not (0 <= body.region_index < len(page["regions"])):
        raise HTTPException(status_code=404, detail="Region index out of range")
    region = page["regions"][body.region_index]
    parcels = region.get("parcels") or []
    if not (0 <= body.parcel_index < len(parcels)):
        raise HTTPException(status_code=404, detail="Parcel index out of range")

    page_path = DOCUMENT_ROOT / document_id / "pages" / f"page_{body.page_number:03d}.png"
    if not page_path.exists():
        raise HTTPException(status_code=404, detail=f"Rendered page not found: {page_path}")

    x, y, w, h = region["bbox"]
    with Image.open(page_path) as page_image:
        crop = page_image.convert("RGB").crop((x, y, x + w, y + h))

    geometries = extract_parcel_geometries_batch([crop])
    parcels_found = geometries[0]
    if isinstance(parcels_found, Exception):
        raise HTTPException(
            status_code=502, detail=f"Re-extraction failed: {parcels_found}"
        )
    if body.parcel_index >= len(parcels_found):
        raise HTTPException(
            status_code=404,
            detail="Re-extraction returned fewer parcels than expected for this region",
        )

    return {
        "document_id": document_id,
        "vision_geometry": parcels_found[body.parcel_index],
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