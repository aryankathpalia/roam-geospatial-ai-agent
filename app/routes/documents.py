import asyncio
import json
import logging
from uuid import uuid4
from pathlib import Path

import io

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from PIL import Image
from pydantic import BaseModel

from app.core.config import settings
from app.pipeline.document_pipeline import (
    process_document,
    recompute_parcel_from_calls,
)
from app.services import progress as progress_tracker
from app.services.pre_annotation import generate_pre_annotations
from app.services.geometry import TraverseResult, traverse_to_geojson, walk_traverse
from app.services.region_cropper import PARCELMAP_CROP_MARGIN_FRAC, PARCELMAP_CROP_MARGIN_MIN_PX
from app.services.ocr import run_parcelmap_ocr
from app.services import calibration as calibration_service
from app.services import gemini_edge_association
from app.services.georeference import georeference_traverse_to_geojson
from app.services.spatial_validation import validate_traverse

logger = logging.getLogger(__name__)


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
    # 4. Kick off the ROAM processing pipeline in the background and
    # return immediately -- render + detect every page, fan out OCR,
    # vision extraction and georeferencing (see
    # app/pipeline/document_pipeline.py) can take minutes on a large
    # scan, and the review UI polls GET /{id}/progress to show real
    # per-stage status instead of blocking on one request for all of
    # it.
    # --------------------------------------------------

    asyncio.create_task(_process_and_save(document_id))

    return {
        "document_id": document_id,
        "filename": file.filename,
        "status": "processing",
    }


async def _process_and_save(document_id: str) -> None:
    try:
        pipeline_result = await process_document(document_id)
    except Exception as exc:  # noqa: BLE001 -- reported via progress, not raised
        logger.exception("Document processing failed for %s", document_id)
        progress_tracker.fail(document_id, str(exc))
        return

    # Persist the result so it can be reloaded for review later --
    # otherwise it only ever existed in this background task.
    _save_result(document_id, pipeline_result)


@router.get("/{document_id}/progress")
def get_progress(document_id: str):
    """
    Polled by the review UI while a document is processing. Returns
    the current pipeline stage and a human-readable detail string that
    fills in with real counts once each stage completes (see
    app/services/progress.py). 404 if nothing has started processing
    for this document_id -- distinct from a legitimate in-progress
    state, so the frontend can tell "never uploaded" from "still
    working".
    """

    state = progress_tracker.get(document_id)
    if state is None:
        raise HTTPException(
            status_code=404, detail=f"No processing found for document: {document_id}"
        )
    return {"document_id": document_id, **state}


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


@router.get("/{document_id}/pages/{page_number}.png")
def get_page_image(document_id: str, page_number: int):
    """
    The full rendered page, for the review UI's reference viewer --
    a region crop can cut off labels (a legend, an acreage table, a
    corner coordinate) that sit just outside the detected bbox.
    """

    page_path = DOCUMENT_ROOT / document_id / "pages" / f"page_{page_number:03d}.png"
    if not page_path.exists():
        raise HTTPException(status_code=404, detail=f"Rendered page not found: {page_path}")
    return StreamingResponse(open(page_path, "rb"), media_type="image/png")


@router.get("/{document_id}/pages/{page_number}/regions/{region_index}/crop.png")
def get_region_crop(document_id: str, page_number: int, region_index: int):
    """
    Crops and returns one region's own source image straight from the
    rendered page PNG, using the same bbox vision reads from -- so the
    review UI can show a reviewer the actual drawing next to the
    editable call table, instead of them needing to reopen the
    original PDF and hunt for the right page to check a correction
    against.

    Applies the same ParcelMap padding region_cropper.py applies at
    ingestion time, HERE too, at serve time -- not just once at
    ingestion. A document processed before that padding existed (or
    before any future retuning of it) has an unpadded bbox baked into
    its stored result.json forever; re-deriving the padding on every
    request means a bug fix here covers every already-processed
    document immediately, not only new ones. Confirmed on a real case:
    a ParcelMap region's bottom edge was cut off in the review UI on a
    document ingested before the ingestion-time padding was added.
    """

    result = _load_result(document_id)
    page = next((p for p in result["pages"] if p["page_number"] == page_number), None)
    if page is None:
        raise HTTPException(status_code=404, detail=f"No page {page_number}")
    if not (0 <= region_index < len(page["regions"])):
        raise HTTPException(status_code=404, detail="Region index out of range")
    region = page["regions"][region_index]

    page_path = DOCUMENT_ROOT / document_id / "pages" / f"page_{page_number:03d}.png"
    if not page_path.exists():
        raise HTTPException(status_code=404, detail=f"Rendered page not found: {page_path}")

    x, y, w, h = region["bbox"]
    with Image.open(page_path) as page_image:
        page_image = page_image.convert("RGB")
        page_w, page_h = page_image.size
        if region.get("class") == "ParcelMap":
            margin_x = max(PARCELMAP_CROP_MARGIN_MIN_PX, w * PARCELMAP_CROP_MARGIN_FRAC)
            margin_y = max(PARCELMAP_CROP_MARGIN_MIN_PX, h * PARCELMAP_CROP_MARGIN_FRAC)
        else:
            margin_x = margin_y = 0
        x1 = max(0, x - margin_x)
        y1 = max(0, y - margin_y)
        x2 = min(page_w, x + w + margin_x)
        y2 = min(page_h, y + h + margin_y)
        crop = page_image.crop((x1, y1, x2, y2))

    buffer = io.BytesIO()
    crop.save(buffer, format="PNG")
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="image/png")


class RecomputeRequest(BaseModel):
    page_number: int
    region_index: int
    parcel_index: int
    boundary_calls: list[dict]
    auto_fix: bool = False
    # Lets a reviewer pin this parcel's anchor by hand instead of trusting
    # the pipeline's geocoded/state-plane guess -- stored on the parcel
    # itself (not the document-level anchor) so it survives independently
    # of every other parcel on the same page.
    anchor_lat: float | None = None
    anchor_lon: float | None = None


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

    manual_anchor = body.anchor_lat is not None and body.anchor_lon is not None
    anchor_lat = body.anchor_lat if manual_anchor else result.get("anchor_lat")
    anchor_lon = body.anchor_lon if manual_anchor else result.get("anchor_lon")

    # See walk_region_parcels' area_check_text comment in
    # document_pipeline.py: the region-OCR-scan area fallback can only
    # be trusted when this parcel is the only one in its region --
    # otherwise it risks validating against a sibling's (or the
    # region's combined) stated area instead of this parcel's own.
    area_check_text = region.get("ocr_text") or "" if len(parcels) == 1 else ""

    updated = recompute_parcel_from_calls(
        body.boundary_calls,
        stated_area_acres=parcel.get("vision_geometry", {}).get("stated_area_acres"),
        region_ocr_text=area_check_text,
        anchor_lat=anchor_lat,
        anchor_lon=anchor_lon,
        auto_fix=body.auto_fix,
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
    if manual_anchor:
        # Per-parcel override -- distinct from result["anchor"], which is
        # the document-wide pipeline guess every other parcel still uses.
        parcel["anchor_override"] = {"lat": body.anchor_lat, "lon": body.anchor_lon}

    _save_result(document_id, result)

    return {"document_id": document_id, "parcel": parcel}


class ConfirmBoundaryRequest(BaseModel):
    page_number: int
    region_index: int
    parcel_index: int
    # Vertices in the SAME pixel space as the region crop image
    # (GET .../regions/{region_index}/crop.png), e.g. [[x, y], ...],
    # ring not necessarily closed. This is a prototype for the
    # confirm-and-edit boundary review screen: it stores the
    # human-confirmed shape as-is, with no attempt to back-derive
    # bearing/distance calls from it (see scoping discussion -- a
    # dragged polygon is still useful ground truth even without a
    # perfectly reverse-engineered call list).
    vertices: list[list[float]]
    crop_width: float
    crop_height: float
    # Same ring, already projected back into the parcel's ORIGINAL
    # local (anchor-relative, feet) traverse coordinate system by the
    # frontend -- the exact inverse of whatever affine it used to seed
    # pixel vertices from boundary_geojson in the first place. Any
    # vertex the user never dragged round-trips to its exact original
    # local coordinate; a dragged/added one gets an approximate local
    # position at the same display scale. Null when this parcel had no
    # original traverse ring to invert against (nothing to project).
    local_vertices: list[list[float]] | None = None


@router.post("/{document_id}/confirm-boundary")
def confirm_boundary(document_id: str, body: ConfirmBoundaryRequest):
    """
    Confirm-and-edit boundary review: stores a human-confirmed/
    corrected polygon (in region-crop pixel space) on the parcel, and,
    when local_vertices is provided, feeds that SAME shape into the
    normal geometry pipeline in place of the vision-extracted
    traverse -- replacing boundary_geojson/boundary_geojson_wgs84/
    spatial_validation with ones built from the confirmed contour,
    still walked against this document's own anchor and re-validated
    the normal way (see validate_traverse), so /workspace renders the
    confirmed shape exactly like it would any other parcel. The
    original vision_geometry/resolved_boundary_calls are left alone
    for comparison -- only the derived geometry is replaced.
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

    if len(body.vertices) < 3:
        raise HTTPException(status_code=400, detail="A boundary needs at least 3 vertices")

    parcel["confirmed_boundary_pixels"] = {
        "vertices": body.vertices,
        "crop_width": body.crop_width,
        "crop_height": body.crop_height,
    }
    parcel["human_confirmed"] = True

    georeferenced_from_confirmation = False

    anchor_override = parcel.get("anchor_override") or {}
    anchor_lat = anchor_override.get("lat", result.get("anchor_lat"))
    anchor_lon = anchor_override.get("lon", result.get("anchor_lon"))

    if body.local_vertices and len(body.local_vertices) >= 3 and anchor_lat is not None and anchor_lon is not None:
        old_local_points = [(float(x), float(y)) for x, y in body.local_vertices]

        # --- calibrated reprojection ---
        # Try to independently verify scale AND rotation against this
        # document's own printed evidence (stated acreage + OCR'd
        # bearing/distance calls near the CONFIRMED edges) before
        # trusting them, instead of blindly inheriting whatever
        # scale/rotation the original (possibly wrong) vision-extracted
        # seed implied. See app/services/calibration.py.
        calibration_info = None
        try:
            page_path = DOCUMENT_ROOT / document_id / "pages" / f"page_{body.page_number:03d}.png"
            with Image.open(page_path) as page_img:
                page_w, page_h = page_img.size
                bx, by, bw, bh = region["bbox"]
                if region.get("class") == "ParcelMap":
                    mx = max(PARCELMAP_CROP_MARGIN_MIN_PX, bw * PARCELMAP_CROP_MARGIN_FRAC)
                    my = max(PARCELMAP_CROP_MARGIN_MIN_PX, bh * PARCELMAP_CROP_MARGIN_FRAC)
                else:
                    mx = my = 0
                origin_x = max(0, bx - mx)
                origin_y = max(0, by - my)
                polygon_page_px = [(origin_x + x, origin_y + y) for x, y in body.vertices]
                ocr_lines, _ = run_parcelmap_ocr(page_img)

                # One Gemini call, one polygon, one crop (see
                # gemini_edge_association). Best-effort: failure (no key,
                # quota) leaves OCR-only calibration exactly as before.
                gemini_candidates: list[dict] = []
                gemini_note = None
                if settings.CALIBRATION_GEMINI_ASSOCIATION and settings.GEMINI_API_KEY:
                    try:
                        crop_box = (
                            int(origin_x), int(origin_y),
                            int(min(page_w, bx + bw + mx)), int(min(page_h, by + bh + my)),
                        )
                        gemini_candidates = gemini_edge_association.associate_edges(
                            page_img, crop_box, [tuple(v) for v in body.vertices],
                        )
                    except Exception as exc:  # noqa: BLE001
                        gemini_note = f"gemini edge association failed ({type(exc).__name__}: {exc}); OCR-only."

                # The pivot the ORIGINAL (uncalibrated) seed transform
                # used: crop center in pixels <-> the true vision-
                # extracted ring's own bounding-box center in local
                # feet. Re-derived fresh from vision_geometry.boundary_calls
                # every time (never from parcel["boundary_geojson"],
                # which a PRIOR confirm may have already overwritten) so
                # repeat confirms can't contaminate this reference.
                crop_w = min(page_w, bx + bw + mx) - origin_x
                crop_h = min(page_h, by + bh + my) - origin_y
                page_pivot = (origin_x + crop_w / 2, origin_y + crop_h / 2)
                local_pivot = None
                # NOTE: this must be resolved_boundary_calls, not
                # vision_geometry.boundary_calls -- the pipeline seeds
                # boundary_geojson (and therefore the frontend's original
                # pivot) from resolved_boundary_calls (post Track-B
                # combined-tract-width correction), never from the raw
                # vision-extracted calls. Using the raw calls here pivots
                # off the wrong ring's bbox center whenever Track B
                # resolved a different width than vision extracted
                # (confirmed on NVZ: 352.40'/550.75' resolved vs 903.15'
                # raw). resolved_boundary_calls is never mutated by
                # confirm-boundary itself, so this stays immune to
                # repeat-confirm contamination the same way the raw-calls
                # version was.
                original_calls = parcel.get("resolved_boundary_calls")
                if original_calls:
                    try:
                        original_ring = walk_traverse(original_calls).points[:-1]
                        rxs = [p[0] for p in original_ring]
                        rys = [p[1] for p in original_ring]
                        local_pivot = ((min(rxs) + max(rxs)) / 2, (min(rys) + max(rys)) / 2)
                    except Exception:  # noqa: BLE001 -- fall through to "no pivot" below
                        local_pivot = None

            stated_sqft = None
            stated_acres_str = parcel.get("vision_geometry", {}).get("stated_area_acres")
            if stated_acres_str:
                try:
                    stated_sqft = float(stated_acres_str) * 43560.0
                except (TypeError, ValueError):
                    stated_sqft = None

            calibration_info = calibration_service.calibrate(
                polygon_page_px, ocr_lines, stated_sqft,
                old_local_points=old_local_points, page_pivot=page_pivot, local_pivot=local_pivot,
                extra_candidates=gemini_candidates,
            )
            if gemini_note:
                calibration_info.notes.append(gemini_note)
            if not gemini_note:
                calibration_info.notes.append(f"gemini association returned {len(gemini_candidates)} edge reading(s)")
        except Exception as exc:  # noqa: BLE001 -- calibration is best-effort; never blocks a save
            calibration_info = calibration_service.CalibrationResult(
                status="unverified", scale_ft_per_px=None, rotation_deg=None,
                scale_from_area=None, scale_from_edges=None, scale_agreement_pct=None,
                corroborating_edge_count=0, notes=[f"calibration attempt failed: {exc}"],
            )

        if calibration_info.status in ("cross_validated", "single_source") and calibration_info.rotation_deg is not None:
            # Rebuild the local-feet ring using the CALIBRATED scale and
            # rotation, pivoted at the SAME (page_pivot -> local_pivot)
            # point the original transform used -- so only scale/
            # rotation get upgraded, translation stays exactly as
            # approximate as it already was (still just the document's
            # geocoded/surveyed anchor, per confirm-boundary's existing
            # behavior). NOT the shape's own centroid -- see
            # reproject_page_px_to_local's docstring for why that's wrong.
            points = calibration_service.reproject_page_px_to_local(
                polygon_page_px, calibration_info.scale_ft_per_px, calibration_info.rotation_deg,
                page_pivot, local_pivot,
            )
            boundary_source = "manual_confirmed_calibrated"
        else:
            points = old_local_points
            boundary_source = "manual_confirmed_uncalibrated"
            if calibration_info.status == "unverified" and not calibration_info.notes:
                calibration_info.notes.append("no calibration evidence found; using inherited scale/rotation from the original seed.")

        # The confirmed ring is already closed by construction (it's a
        # human-drawn shape, not a directional walk) -- closure_error_ft
        # is 0 here, honestly, not because closure was achieved but
        # because there's no accumulated-error walk to measure.
        traverse = TraverseResult(points=points, closure_error_ft=0.0, unparsed_calls=0)

        parcel["boundary_geojson"] = traverse_to_geojson(traverse)
        parcel["boundary_geojson_wgs84"] = georeference_traverse_to_geojson(
            traverse, anchor_lat, anchor_lon
        )
        parcel["boundary_geojson_wgs84"]["properties"]["georeferenced"] = "from_confirmed_boundary"

        # Re-run the normal area-mismatch/self-intersection checks
        # against the CONFIRMED shape, using whatever calls vision
        # already resolved for this parcel purely as the source of the
        # stated-area/OCR-text cross-check -- not to re-walk anything.
        region_ocr_text = region.get("ocr_text") or "" if len(parcels) == 1 else ""
        parcel["spatial_validation"] = validate_traverse(
            traverse,
            region_ocr_text,
            stated_area_acres=parcel.get("vision_geometry", {}).get("stated_area_acres"),
            calls=parcel.get("resolved_boundary_calls"),
        )
        parcel["boundary_source"] = boundary_source
        parcel["calibration"] = {
            "status": calibration_info.status,
            "scale_ft_per_px": calibration_info.scale_ft_per_px,
            "rotation_deg": calibration_info.rotation_deg,
            "scale_from_area": calibration_info.scale_from_area,
            "scale_from_edges": calibration_info.scale_from_edges,
            "scale_agreement_pct": calibration_info.scale_agreement_pct,
            "corroborating_edge_count": calibration_info.corroborating_edge_count,
            "notes": calibration_info.notes,
            "corroborations": calibration_info.corroborations,
            "independent_bearing_edges": calibration_info.independent_bearing_edges,
            "quadrant_resolved_edges": calibration_info.quadrant_resolved_edges,
        }
        parcel.pop("georeference_error", None)
        georeferenced_from_confirmation = True

    _save_result(document_id, result)

    return {
        "document_id": document_id,
        "parcel": parcel,
        "georeferenced_from_confirmation": georeferenced_from_confirmation,
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