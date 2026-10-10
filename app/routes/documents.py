import asyncio
import copy
import json
import logging
import math
import re
import threading
import time
from uuid import uuid4
from pathlib import Path

import io

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from PIL import Image
from pydantic import BaseModel

from app.core.config import settings
from app.pipeline.document_pipeline import (
    process_document,
    recompute_parcel_from_calls,
)
from app.services import doc_access
from app.services import progress as progress_tracker
from app.services.pre_annotation import generate_pre_annotations
from app.services.geometry import TraverseResult, traverse_to_geojson, walk_traverse
from app.services.region_cropper import PARCELMAP_CROP_MARGIN_FRAC, PARCELMAP_CROP_MARGIN_MIN_PX, padded_crop_box
from app.services.ocr import run_parcelmap_ocr
from app.services import calibration as calibration_service
from app.services import control_points
from app.services import parcel_roster
from app.services import gemini_edge_association
from app.services import placement as placement_service
from app.services.georeference import georeference_traverse_to_geojson, ground_to_grid_multiplier
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
    # Write-then-rename: a GET (or the background verification) reading
    # result.json mid-save must never see a half-written file.
    path = _result_path(document_id)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(result, indent=2), encoding="utf-8")
    tmp.replace(path)


@router.post("/upload")
async def upload_document(request: Request, file: UploadFile = File(...)):
    """
    Upload a PDF document and start the ROAM processing pipeline. Signed-in users only (main.py's gate); the
    document belongs to them (services/doc_access.py).
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
    user = getattr(request.state, "user", None)
    if user:
        from app.services import doc_access

        doc_access.write_access(document_id, {"owner": user["email"], "filename": file.filename, "created_at": time.time()})

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
    def checkpoint(partial: dict) -> None:
        # Persist the candidate sheets (after triage) and then their parcel roster, so the
        # review UI can open and the user can start outlining parcels while OCR, geocoding
        # and vision extraction keep running below. The user's polygons are carried over:
        # a pipeline save must never discard what was drawn since the last one.
        with _RESULT_LOCK:
            try:
                parcel_roster.carry_over_user_data(_load_result(document_id), partial)
            except HTTPException:
                pass  # first checkpoint: nothing stored yet
            _save_result(document_id, partial)

    try:
        pipeline_result = await process_document(document_id, on_checkpoint=checkpoint)
    except Exception as exc:  # noqa: BLE001 -- reported via progress, not raised
        logger.exception("Document processing failed for %s", document_id)
        progress_tracker.fail(document_id, str(exc))
        return

    # Persist the result so it can be reloaded for review later --
    # otherwise it only ever existed in this background task.
    await _finalize_and_bind(document_id, pipeline_result)


async def _finalize_and_bind(document_id: str, final: dict) -> None:
    """
    Joins the pipeline's finished output with whatever the user did while it ran. The pipeline
    already attached its extracted parcels to the roster (process_document's evidence join); here
    the user's side is added: every entity's OWN confirmed polygon is carried over by id, parcels
    they named by hand are joined by label too, and each confirmed entity is then bound to its
    evidence parcel and given calibration + placement in the background -- independently.
    """

    to_verify: list[tuple[ConfirmBoundaryRequest, str]] = []
    with _RESULT_LOCK:
        try:
            parcel_roster.carry_over_user_data(_load_result(document_id), final)
        except HTTPException:
            pass
        for page in final["pages"]:
            sheet = page.get("sheet")
            if not sheet:
                continue
            parcel_roster.join_evidence(page["page_number"], sheet, page["regions"])
            for entity in sheet.get("parcels", []):
                pending = _bind_entity(document_id, final, page["page_number"], entity)
                if pending:
                    to_verify.append(pending)
        _save_result(document_id, final)

    loop = asyncio.get_running_loop()
    for body, confirmation_id in to_verify:
        await loop.run_in_executor(None, _verify_confirmation, document_id, body, confirmation_id)


@router.post("/{document_id}/reprocess")
def reprocess_document(document_id: str, background_tasks: BackgroundTasks):
    """
    Re-runs the pipeline for a document whose original.pdf is already on
    disk, from scratch.

    Processing is kicked off as a plain in-memory asyncio task (see
    /upload) with no persisted queue or resume point -- if the backend
    process running it restarts or dies (a dev-server reload, a crash,
    anything) while a document is mid-pipeline, that task is simply gone.
    The document is left stuck forever at processing.complete=False with
    whatever partial checkpoint it last saved (confirmed in practice: a
    document can sit with sheets detected but zero parcels in the roster,
    indistinguishable in the UI from "still reading" -- GET .../progress
    404s because the NEW process's progress tracker never heard of it).
    This is a manual recovery action for exactly that state: there is no
    way to tell from the document's own saved state alone that it is
    orphaned rather than genuinely still running, so this never fires on
    its own -- the user (or the review UI, once it detects a long-stuck
    "still reading" state) has to ask for it.
    """

    pdf_path = DOCUMENT_ROOT / document_id / "original.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail=f"No original.pdf saved for document {document_id}")
    background_tasks.add_task(_process_and_save, document_id)
    return {"document_id": document_id, "status": "processing"}


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
def get_document(document_id: str, background_tasks: BackgroundTasks):
    """
    Reload a previously-processed document's stored result, for the
    review UI to resume without re-uploading/re-processing.
    """

    result = _load_result(document_id)
    # Verifications orphaned by a restart: re-queue them rather than leave the cards on "Calibrating &
    # placing…" forever.
    # Never WAIT for the result lock here: a long fit running under it must not freeze every page poll. If
    # it is busy, skip -- the next poll tries again.
    requeued: list[tuple] = []
    if _RESULT_LOCK.acquire(blocking=False):
        try:
            result = _load_result(document_id)  # fresh copy, read under the lock
            requeued = _requeue_orphaned_verifications(document_id, result)
            if requeued:
                _save_result(document_id, result)
        finally:
            _RESULT_LOCK.release()
    for body, confirmation_id in requeued:
        background_tasks.add_task(_verify_confirmation, document_id, body, confirmation_id)
    # A control-point read that died with its process (a dev-server reload mid-read) leaves a stale
    # "reading" marker; the page poll that finds it restarts the read instead of leaving the cards on
    # "Refining placement…" forever. _ensure_control_points does nothing for a fresh or finished read.
    for page in result.get("pages", []):
        cp = page.get("control_points") or {}
        if cp.get("status") == "reading" and not _control_read_fresh(page):
            background_tasks.add_task(_ensure_control_points, document_id, page["page_number"])
    return {
        "document_id": document_id,
        # "processing": only the candidate regions exist so far (saved right after
        # layout + triage); OCR, anchor and vision extraction are still running.
        "status": "processed" if (result.get("processing") or {}).get("complete", True) else "processing",
        "result": result,
    }


@router.get("/{document_id}/original.pdf")
def get_original_pdf(document_id: str):
    """The uploaded PDF, as uploaded."""

    path = doc_access.doc_file(document_id, "original.pdf")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Original PDF not found")
    return StreamingResponse(open(path, "rb"), media_type="application/pdf",
                             headers={"Content-Disposition": f'inline; filename="roam-{document_id[:8]}.pdf"'})


@router.get("/{document_id}/pages/{page_number}/thumb.jpg")
def get_page_thumbnail(document_id: str, page_number: int):
    """A small JPEG of a page for the source-document grid (made once, cached beside the pages)."""

    from PIL import Image

    src = doc_access.doc_file(document_id, "pages", f"page_{page_number:03d}.png")
    if not src.exists():
        raise HTTPException(status_code=404, detail="Rendered page not found")
    thumb = DOCUMENT_ROOT / document_id / "page_thumbs" / f"page_{page_number:03d}.jpg"
    if not thumb.exists():
        thumb.parent.mkdir(parents=True, exist_ok=True)
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((420, 420))
            tmp = thumb.with_suffix(".tmp")
            im.save(tmp, format="JPEG", quality=78)
            tmp.replace(thumb)
    return Response(content=thumb.read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/{document_id}/pages/{page_number}.png")
def get_page_image(document_id: str, page_number: int):
    """
    The full rendered page, for the review UI's reference viewer --
    a region crop can cut off labels (a legend, an acreage table, a
    corner coordinate) that sit just outside the detected bbox.
    """

    page_path = doc_access.doc_file(document_id, "pages", f"page_{page_number:03d}.png")
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

    page_path = doc_access.doc_file(document_id, "pages", f"page_{page_number:03d}.png")
    if not page_path.exists():
        raise HTTPException(status_code=404, detail=f"Rendered page not found: {page_path}")

    x, y, w, h = region["bbox"]
    with Image.open(page_path) as page_image:
        page_image = page_image.convert("RGB")
        page_w, page_h = page_image.size
        crop = page_image.crop(padded_crop_box(region["bbox"], page_w, page_h, region.get("class")))

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


class UpdateAnchorRequest(BaseModel):
    page_number: int
    region_index: int
    parcel_index: int
    anchor_lat: float
    anchor_lon: float


@router.post("/{document_id}/update-anchor")
def update_anchor(document_id: str, body: UpdateAnchorRequest):
    """
    Re-georeference an ALREADY-CONFIRMED parcel's full polygon against a
    new anchor -- geographic placement only, nothing about the shape
    itself changes.

    Deliberately separate from /recompute (which rebuilds geometry from
    a human-edited boundary_calls list -- the editable call table).
    /recompute has no concept of confirmed_boundary_pixels at all: every
    call to it unconditionally discards boundary_geojson/_wgs84 and
    rebuilds from whatever calls it's given, which silently destroyed a
    manually confirmed polygon's inferred (uncorroborated) vertices the
    moment a reviewer only wanted to move the anchor -- a 4-vertex
    confirmed rectangle with just 3 edges independently verified became
    a 3-call triangle, because the call table it read from
    (resolved_boundary_calls) was never the confirmed polygon's source
    of truth in the first place.

    This endpoint never looks at resolved_boundary_calls or
    confirmed_boundary_pixels' pixel coordinates at all -- it doesn't
    need to. parcel["boundary_geojson"] (set by _derive_confirmed_geometry,
    see app/routes/documents.py) already holds the confirmed polygon's
    LOCAL (anchor-independent, feet-from-origin) ring -- every vertex,
    corroborated or not, in the exact topology the human confirmed.
    Georeferencing is a pure function of that local ring plus the
    anchor (georeference_traverse_to_geojson, app/services/georeference.py);
    re-running it against a new anchor cannot change vertex count,
    topology, calibration, or evidence -- only where the shape sits on
    the real map.
    """

    result = _load_result(document_id)
    page = next((p for p in result["pages"] if p["page_number"] == body.page_number), None)
    if page is None:
        raise HTTPException(status_code=404, detail=f"No page {body.page_number}")
    if not (0 <= body.region_index < len(page["regions"])):
        raise HTTPException(status_code=404, detail="Region index out of range")
    region = page["regions"][body.region_index]
    parcels = region.get("parcels") or []
    if not (0 <= body.parcel_index < len(parcels)):
        raise HTTPException(status_code=404, detail="Parcel index out of range")
    parcel = parcels[body.parcel_index]

    # Never silently fall back to the calls-based path for a parcel that
    # was never confirmed by drawing -- that parcel's geometry source of
    # truth IS resolved_boundary_calls, and this endpoint isn't it.
    if not parcel.get("confirmed_boundary_pixels"):
        raise HTTPException(
            status_code=400,
            detail="This parcel has no confirmed polygon to re-georeference -- use /recompute instead.",
        )

    local_geojson = parcel.get("boundary_geojson")
    coords = (local_geojson or {}).get("geometry", {}).get("coordinates") or []
    ring = coords[0] if coords else []
    points = [(float(x), float(y)) for x, y in (ring[:-1] if ring and ring[0] == ring[-1] else ring)]
    if len(points) < 3:
        # Explicit, clear failure -- never downgrade a confirmed polygon
        # to a call-table reconstruction just because this endpoint
        # can't do its one job right now (e.g. verification hasn't run
        # yet, so no local ring has been computed at all).
        raise HTTPException(
            status_code=422,
            detail="No usable local geometry stored for this parcel yet (verification may still be running) -- cannot re-georeference without it.",
        )

    # Compute the replacement BEFORE touching the parcel at all -- if
    # georeferencing fails for any reason, the stored parcel must be
    # completely untouched, not left with its old geometry popped and
    # nothing to replace it.
    traverse = TraverseResult(
        points=points,
        closure_error_ft=(local_geojson["properties"].get("closure_error_ft") or 0.0),
        unparsed_calls=(local_geojson["properties"].get("unparsed_calls") or 0),
    )
    new_wgs84 = georeference_traverse_to_geojson(traverse, body.anchor_lat, body.anchor_lon)
    new_wgs84["properties"]["georeferenced"] = "from_confirmed_boundary"

    # Only the fields that genuinely depend on the anchor change.
    # confirmed_boundary_pixels, boundary_geojson (local), boundary_source,
    # calibration (corroborations, independent_bearing_edges, ...), and
    # placement are left exactly as they were -- this operation re-places
    # the existing confirmed shape, it doesn't re-derive or re-verify it.
    parcel["boundary_geojson_wgs84"] = new_wgs84
    parcel["anchor_override"] = {"lat": body.anchor_lat, "lon": body.anchor_lon}
    parcel.pop("georeference_error", None)

    _save_result(document_id, result)

    return {"document_id": document_id, "parcel": parcel}


class ConfirmBoundaryRequest(BaseModel):
    page_number: int
    # The region (crop) the outline is drawn on: the sheet's main drawing, or the region an
    # extracted parcel was read from.
    region_index: int
    # THE PARCEL being confirmed: a sheet entity id (page.sheet.parcels[].id). Each parcel keeps its
    # own polygon, so outlining one can never overwrite another.
    parcel_id: str | None = None
    # A name for a parcel the user is adding by hand (no parcel_id): identity is NOT detected
    # automatically and is marked as such.
    label: str | None = None
    # Legacy: index into region.parcels, for documents processed before parcel entities existed.
    parcel_index: int | None = None
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
    # Curved edges, so the outline can be re-opened and edited: the user's corners and each curved
    # edge's control point ({"vertices": [...], "curves": {edge: [a, b]}, "corner_indices": [...]}).
    # `vertices` above are then the corners PLUS points sampled along each curve, so the stored polygon
    # (area, map ring) follows the curve; everything downstream just sees a polygon.
    curve_spec: dict | None = None


_RESULT_LOCK = threading.RLock()

_DERIVED_PARCEL_KEYS = (
    "boundary_geojson", "boundary_geojson_wgs84", "spatial_validation",
    "boundary_source", "calibration", "placement",
)


def _locate_parcel(result: dict, body) -> tuple[dict, list, dict] | None:
    page = next((p for p in result["pages"] if p["page_number"] == body.page_number), None)
    if page is None or not (0 <= body.region_index < len(page["regions"])):
        return None
    region = page["regions"][body.region_index]
    parcels = region.get("parcels") or []
    if not (0 <= body.parcel_index < len(parcels)):
        return None
    return region, parcels, parcels[body.parcel_index]


def _anchor_for(result: dict, parcel: dict) -> tuple[float | None, float | None]:
    override = parcel.get("anchor_override") or {}
    return override.get("lat", result.get("anchor_lat")), override.get("lon", result.get("anchor_lon"))


def _stated_acres(result: dict, parcel: dict) -> float | None:
    """
    The parcel's printed area in acres. The vision pass only fills `stated_area_acres` when the plat prints
    ACRES; the sheet roster reads whatever the sheet prints beside each parcel label ("85,396 SQ. FT.") and
    parcel_roster.parse_acres converts it -- so a parcel linked to a roster entity falls back to that. Without
    it a plat that states square feet had no area, hence no scale, hence outlines sized from a guess.
    """

    vg = parcel.get("vision_geometry") or {}
    acres = parcel_roster.parse_acres(vg.get("stated_area_acres"))
    if acres:
        return acres
    roster_id = parcel.get("roster_id")
    if roster_id:
        for page in result.get("pages", []):
            for entity in (page.get("sheet") or {}).get("parcels", []):
                if entity.get("id") == roster_id:
                    if entity.get("stated_area_sqft"):  # the model's unit reading, checked against the text
                        return entity["stated_area_sqft"] / 43560.0
                    return parcel_roster.parse_acres(entity.get("stated_area")) or None
    return None


def _derive_confirmed_geometry(
    document_id: str, body, result: dict, region: dict, parcels: list, parcel: dict,
    anchor_lat: float, anchor_lon: float, verify: bool,
) -> None:
    """
    Builds the map geometry for a confirmed polygon and stores it on `parcel`.

    verify=False is the fast path used when the user confirms: no OCR, no
    Gemini, no calibration -- the polygon is placed at the provisional
    position the original seed implied, and calibration/placement are marked
    "pending". verify=True (run in the background after the save, see
    _verify_confirmation) does the calibration and absolute placement.
    """

    # None for a parcel with no prior extraction to seed a position from
    # (e.g. a hand-confirmed "remainder parcel") -- the confirmed polygon's
    # own pixel vertices are still the geometric source of truth in that
    # case; only the 180deg-disambiguation reference and the uncalibrated
    # fallback placement lose their anchor, both handled below.
    old_local_points = (
        [(float(x), float(y)) for x, y in body.local_vertices] if body.local_vertices else None
    )
    calibration_info = None
    calibration_ocr_lines = None
    polygon_page_px = None
    page_pivot = None
    local_pivot = None
    if verify:
        # --- calibrated reprojection ---
        # Try to independently verify scale AND rotation against this
        # document's own printed evidence (stated acreage + OCR'd
        # bearing/distance calls near the CONFIRMED edges) before
        # trusting them, instead of blindly inheriting whatever
        # scale/rotation the original (possibly wrong) vision-extracted
        # seed implied. See app/services/calibration.py.
        calibration_info = None
        calibration_ocr_lines = None
        polygon_page_px = None
        try:
            page_path = doc_access.doc_file(document_id, "pages", f"page_{body.page_number:03d}.png")
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
                calibration_ocr_lines = ocr_lines

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
                if local_pivot is None:
                    # No prior extraction to stay translation-consistent
                    # with (e.g. a hand-confirmed "remainder parcel" with
                    # zero resolved_boundary_calls) -- the only reference
                    # left is the document anchor itself. Pin it at the
                    # REGION'S crop-center page_pivot (set above, same
                    # value for every parcel confirmed out of this same
                    # ParcelMap region) rather than this parcel's OWN
                    # pixel bbox center.
                    #
                    # This used to re-pin page_pivot to each parcel's own
                    # bbox center, which independently placed EVERY such
                    # parcel's own center at the exact same document
                    # anchor lat/lon -- fine for a single parcel, but for
                    # two sibling parcels sharing one region/sheet (e.g.
                    # the Patnaude packet's "Remainder Parcel" and
                    # "Parcel 1", both with zero extracted calls) it threw
                    # away their true relative pixel offset entirely, so
                    # confirming one right after the other put each one's
                    # centroid on top of the SAME real-world point instead
                    # of leaving them correctly adjacent on the ground.
                    # Keeping the shared crop-center pivot means both
                    # parcels get projected through the same page-pixel
                    # -> feet -> geodesic transform, so their real,
                    # pixel-accurate relative position (and any shared
                    # edge) survives georeferencing.
                    local_pivot = (0.0, 0.0)

            stated_acres = _stated_acres(result, parcel)
            stated_sqft = stated_acres * 43560.0 if stated_acres else None

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

    else:
        calibration_info = calibration_service.CalibrationResult(
            status="pending", scale_ft_per_px=None, rotation_deg=None,
            scale_from_area=None, scale_from_edges=None, scale_agreement_pct=None,
            corroborating_edge_count=0, notes=["verification is running in the background"],
        )

    if verify and calibration_info.status in ("cross_validated", "single_source") and calibration_info.rotation_deg is not None:
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
        # Rotation unverified does not make the SIZE a guess. The seed's display
        # scale fits the vision ring to the crop, so on a sheet where the seed ring
        # is one small parcel of a bigger drawing it is ~10x off (Patnaude packet:
        # a 40-acre parcel drawn as 3.95 acres). When calibration did accept a
        # scale (area-based, corroborated by the printed distances), use it and
        # keep the seed's north-up orientation; the status stays unverified.
        if verify and calibration_info.scale_ft_per_px and polygon_page_px is not None and local_pivot is not None:
            points = calibration_service.reproject_page_px_to_local(
                polygon_page_px, calibration_info.scale_ft_per_px, 0.0, page_pivot, local_pivot,
            )
            calibration_info.notes.append(
                "size uses the stated-area/edge-corroborated scale; orientation assumes the drawing is "
                "north-up because rotation could not be verified"
            )
        if calibration_info.status == "unverified" and not calibration_info.notes:
            calibration_info.notes.append("no calibration evidence found; using inherited scale/rotation from the original seed.")

    if points is None:
        # No prior extraction to seed a fallback position from (e.g. a
        # hand-confirmed "remainder parcel"), AND -- only reachable when
        # verify=True -- no stated acreage either, so even the area-based
        # scale fallback above had nothing to reproject with. Nothing to
        # place -- the confirmed outline is kept, not discarded, but
        # cannot be put on the map. verify=True means Gemini + OCR both
        # genuinely ran first (see calibration_info.notes for what they
        # found); verify=False just means the fast path hasn't attempted
        # verification yet, same "pending" state as the normal case.
        parcel["boundary_source"] = boundary_source
        parcel["placement"] = {
            "status": placement_service.PLACEMENT_APPROXIMATE if verify else "pending",
            "notes": (
                ["no scale evidence found (no stated acreage, no prior extraction) -- outline saved but cannot be placed on the map"]
                if verify else ["placement verification is running in the background"]
            ),
        }
        parcel["calibration"] = {
            "status": calibration_info.status, "scale_ft_per_px": calibration_info.scale_ft_per_px,
            "rotation_deg": calibration_info.rotation_deg, "scale_from_area": calibration_info.scale_from_area,
            "scale_from_edges": calibration_info.scale_from_edges, "scale_agreement_pct": calibration_info.scale_agreement_pct,
            "corroborating_edge_count": calibration_info.corroborating_edge_count, "notes": calibration_info.notes,
            "corroborations": calibration_info.corroborations,
            "independent_bearing_edges": calibration_info.independent_bearing_edges,
            "quadrant_resolved_edges": calibration_info.quadrant_resolved_edges,
            "rotation_ambiguous_candidates_deg": calibration_info.rotation_ambiguous_candidates_deg,
        }
        parcel.pop("georeference_error", None)
        return

    # The confirmed ring is already closed by construction (it's a
    # human-drawn shape, not a directional walk) -- closure_error_ft
    # is 0 here, honestly, not because closure was achieved but
    # because there's no accumulated-error walk to measure.
    # validate_traverse (shoelace area, perimeter) and traverse_to_geojson expect a
    # ring whose last point repeats the first, as walk_traverse returns. An open
    # ring silently drops the closing edge -- a 345x225 ft rectangle measured half
    # its area and every confirmed parcel showed a false 50% mismatch. `points`
    # itself stays open: placement wants one point per confirmed vertex.
    ring_points = points if points[0] == points[-1] else list(points) + [points[0]]
    traverse = TraverseResult(points=ring_points, closure_error_ft=0.0, unparsed_calls=0)

    parcel["boundary_geojson"] = traverse_to_geojson(traverse)
    parcel["boundary_geojson_wgs84"] = georeference_traverse_to_geojson(
        traverse, anchor_lat, anchor_lon
    )
    parcel["boundary_geojson_wgs84"]["properties"]["georeferenced"] = "from_confirmed_boundary"

    # --- absolute placement (separate from calibration) ---
    # The line above puts local (0,0) at the document anchor, which is
    # never checked to be a point of THIS parcel (on NVZ it is a section-
    # corner control monument, ~2,600 ft away). Replace it only when a
    # confirmed vertex can be bound to a printed parcel-corner coordinate
    # in a CRS the sheet states; otherwise keep it but mark it approximate.
    # Needs calibrated shape AND orientation, so only when calibration is placeable.
    placement_info = {"status": placement_service.PLACEMENT_APPROXIMATE,
                      "notes": ["position comes from the document-level anchor, which is not tied to any vertex of this parcel"]}
    if boundary_source == "manual_confirmed_calibrated" and calibration_ocr_lines is not None:
        try:
            sheet_text = "\n".join(
                [ln.text for ln in calibration_ocr_lines]
                + [r.get("ocr_text") or "" for pg in result.get("pages", []) for r in pg.get("regions", [])]
            )
            placed = placement_service.place(polygon_page_px, points, calibration_ocr_lines, sheet_text)
            placement_info = {"status": placed.status, "notes": placed.notes, "details": placed.details}
            if placed.geojson is not None:
                parcel["boundary_geojson_wgs84"] = placed.geojson
        except Exception as exc:  # noqa: BLE001 -- placement is best-effort; never blocks a save
            placement_info["notes"].append(f"placement attempt failed: {exc}")
    elif verify:
        placement_info["notes"].append("scale/rotation not calibrated, so no vertex can be bound to a surveyed coordinate")
    if not verify:
        placement_info = {"status": "pending", "notes": ["placement verification is running in the background"]}
    parcel["placement"] = placement_info

    # Re-run the normal area-mismatch/self-intersection checks
    # against the CONFIRMED shape, using whatever calls vision
    # already resolved for this parcel purely as the source of the
    # stated-area/OCR-text cross-check -- not to re-walk anything.
    region_ocr_text = region.get("ocr_text") or "" if len(parcels) == 1 else ""
    parcel["spatial_validation"] = validate_traverse(
        traverse,
        region_ocr_text,
        stated_area_acres=(
            parcel.get("vision_geometry", {}).get("stated_area_acres")
            or (str(_stated_acres(result, parcel)) if _stated_acres(result, parcel) else None)
        ),
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
            "rotation_ambiguous_candidates_deg": calibration_info.rotation_ambiguous_candidates_deg,
    }
    parcel.pop("georeference_error", None)


# Verifications running in THIS process, by confirmation id -> start time. A verification that dies with its
# process (a dev-server reload, a crash) leaves its parcel "pending" on disk forever; because this registry
# is empty after a restart, a pending parcel that is not in it is known to be orphaned and is re-queued
# (_requeue_orphaned_verifications).
_VERIFYING: dict[str, float] = {}
_VERIFY_STALE_S = 900


def _verification_running(confirmation_id: str | None) -> bool:
    started = _VERIFYING.get(confirmation_id or "")
    return started is not None and time.time() - started < _VERIFY_STALE_S


def _verify_confirmation(document_id: str, body, confirmation_id: str) -> None:
    _VERIFYING[confirmation_id] = time.time()
    try:
        _verify_confirmation_inner(document_id, body, confirmation_id)
    finally:
        _VERIFYING.pop(confirmation_id, None)


def _requeue_orphaned_verifications(document_id: str, result: dict) -> list[tuple]:
    """
    Confirmed parcels left "pending" with no verification running for them -- the process that was verifying
    them died. Re-applies each one's confirmed outline (provisional placement) and returns the
    (body, confirmation_id) pairs to verify again, registered as running so a poll arriving before the task
    starts does not queue them twice. Mutates `result`; the caller saves it.
    """

    if not (result.get("processing") or {}).get("complete", True):
        return []
    out = []
    for page in result.get("pages", []):
        for entity in (page.get("sheet") or {}).get("parcels", []):
            ref = entity.get("evidence_ref")
            if not entity.get("confirmed_polygon") or not ref:
                continue
            region = page["regions"][ref["region"]]
            parcels = region.get("parcels") or []
            parcel = parcels[ref["parcel"]] if ref["parcel"] < len(parcels) else None
            if not parcel or not parcel.get("human_confirmed"):
                continue
            pending = (parcel.get("placement") or {}).get("status") == "pending" or (parcel.get("calibration") or {}).get("status") == "pending"
            if not pending or _verification_running((parcel.get("confirmed_boundary_pixels") or {}).get("id")):
                continue
            queued = _bind_entity(document_id, result, page["page_number"], entity)
            if queued:
                _VERIFYING[queued[1]] = time.time()
                out.append(queued)
    return out


def _verify_confirmation_inner(document_id: str, body, confirmation_id: str) -> None:
    """
    Background half of confirm-boundary: runs calibration and absolute
    placement on a snapshot (slow -- OCR and a Gemini call, no lock held),
    then writes the result onto the parcel -- unless the user re-confirmed
    it in the meantime, in which case the newer confirmation's own
    verification wins.
    """

    scratch = None
    failure = None
    try:
        snapshot = _load_result(document_id)
        located = _locate_parcel(snapshot, body)
        if located is None:
            return
        region, parcels, parcel = located
        if (parcel.get("confirmed_boundary_pixels") or {}).get("id") != confirmation_id:
            return
        anchor_lat, anchor_lon = _anchor_for(snapshot, parcel)
        scratch = copy.deepcopy(parcel)
        _derive_confirmed_geometry(
            document_id, body, snapshot, region, parcels, scratch, anchor_lat, anchor_lon, verify=True
        )
    except Exception as exc:  # noqa: BLE001 -- never leave a parcel "pending" forever
        failure = f"{type(exc).__name__}: {exc}"
        logging.getLogger(__name__).warning("verification failed for %s: %s", document_id, failure)

    with _RESULT_LOCK:
        try:
            result = _load_result(document_id)
        except HTTPException:
            return
        located = _locate_parcel(result, body)
        if located is None:
            return
        parcel = located[2]
        if (parcel.get("confirmed_boundary_pixels") or {}).get("id") != confirmation_id:
            return
        if scratch is None:
            parcel["calibration"] = {
                "status": "unverified", "scale_ft_per_px": None, "rotation_deg": None,
                "corroborating_edge_count": 0, "notes": [f"verification failed: {failure}"], "corroborations": [],
            }
            parcel["placement"] = {"status": "approximate", "notes": [f"verification failed: {failure}"]}
        else:
            for key in _DERIVED_PARCEL_KEYS:
                if key in scratch:
                    parcel[key] = scratch[key]
            parcel.pop("georeference_error", None)
            _reapply_manual_position(parcel)
        _unify_sheet_frame(result, body.page_number)
        _fit_sheet_to_aliquot(result, body.page_number)
        _fit_sheet_to_apn(result, body.page_number)
        _fit_sheet_to_control_points(result, body.page_number)
        _save_result(document_id, result)
    _ensure_control_points(document_id, body.page_number)


# The confirmed parcels' combined area must match the aliquot part's to
# within this before they're fitted onto it -- otherwise they evidently
# don't make up that whole aliquot part and a group fit would be a guess.
_ALIQUOT_FIT_AREA_TOL = 0.03
# Stricter bar for calling the fitted placement CONFIRMED rather than just
# better: combined area AND both outer extents within this percent of the
# BLM aliquot part (Patnaude: 0.2% area).
_ALIQUOT_CORROBORATED_PCT = 2.0


def _contain_in_aliquot(members: list, aliquot: dict, total_acres: float) -> bool:
    """
    Shifts the sheet's confirmed parcels, as a group and by the smallest amount, so they lie
    inside the aliquot part the legal description says they are a portion of. Position stays
    "approximate": nothing here confirms WHERE inside it they sit, only that the placement from
    the document anchor must not put them outside land the survey itself names. A group larger
    than the aliquot part is left alone (it cannot be contained). Returns True when it handled
    the sheet (shifted, or already inside).
    """

    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    pts = [pt for _, u in members for pt in u["geometry"]["coordinates"][0]]
    poly = aliquot["polygon"]
    g_w, g_e = min(p[0] for p in pts), max(p[0] for p in pts)
    g_s, g_n = min(p[1] for p in pts), max(p[1] for p in pts)
    a_w, a_e = min(p[0] for p in poly), max(p[0] for p in poly)
    a_s, a_n = min(p[1] for p in poly), max(p[1] for p in poly)
    if g_e - g_w > a_e - a_w or g_n - g_s > a_n - a_s:
        return False
    dx = (a_w - g_w) if g_w < a_w else (a_e - g_e) if g_e > a_e else 0.0
    dy = (a_s - g_s) if g_s < a_s else (a_n - g_n) if g_n > a_n else 0.0
    shift_m = geod.inv(g_w, g_s, g_w + dx, g_s + dy)[2]
    for parcel, unfitted in members:
        placement = parcel.setdefault("placement", {"status": "approximate", "notes": []})
        placement["notes"] = [n for n in placement.get("notes", []) if not n.startswith("constrained to lie within")]
        if shift_m < 0.5:  # already inside: nothing to change (and undo any earlier shift)
            if placement.pop("aliquot_fit", None):
                parcel["boundary_geojson_wgs84"] = unfitted
                parcel.pop("boundary_geojson_wgs84_unfitted", None)
            continue
        moved = copy.deepcopy(unfitted)
        moved["geometry"]["coordinates"] = [
            [[lon + dx, lat + dy] for lon, lat in ring] for ring in unfitted["geometry"]["coordinates"]
        ]
        moved["properties"]["georeferenced"] = "contained_in_aliquot_part"
        parcel["boundary_geojson_wgs84_unfitted"] = unfitted
        parcel["boundary_geojson_wgs84"] = moved
        placement["aliquot_fit"] = {
            "mode": "containment", "corroborated": False, "description": aliquot["description"],
            "shift_m": round(shift_m, 1), "confirmed_acres": round(total_acres, 2),
            "aliquot_acres": round(aliquot["acres"], 2),
        }
        placement["notes"].append(
            f"constrained to lie within the {aliquot['description']} (BLM section corners) the legal description "
            f"says these parcels are a portion of: moved {shift_m:.0f} m from the anchor-based position. "
            "Where inside it they sit is not confirmed."
        )
    return True


def _fit_sheet_to_aliquot(result: dict, page_number: int) -> None:
    """
    Places a sheet's confirmed parcels, as a GROUP, onto the aliquot part of
    a section the plat's legal description names ("N 1/2 of S 1/2 of Section
    17"), resolved from BLM's own section corners (result["anchor"]["aliquot"],
    see plss._resolve_aliquot).

    Why: a parcel with no extracted calls is placed by pinning the document
    anchor to the CENTRE of its drawing crop, not to the corner the anchor
    monument actually marks. On the Patnaude packet that left the confirmed
    parcels ~1 km west of the land the description names, though the anchor
    itself (the W 1/4 corner of Sec 17) was exactly right.

    Gated on corroboration: only when the confirmed parcels' combined area
    matches the aliquot part's area within _ALIQUOT_FIT_AREA_TOL (Patnaude:
    160.4 ac confirmed vs 160.08 ac from BLM). Translation only -- shapes and
    their relative positions (including shared edges) are untouched. Parcels
    the user pinned by hand (anchor_override) are left alone. Recomputed from
    each parcel's unfitted placement every time, so re-verifying one parcel
    can't compound or desync the fit.
    """

    aliquot = (result.get("anchor") or {}).get("aliquot")
    page = next((p for p in result.get("pages", []) if p["page_number"] == page_number), None)
    if not aliquot or page is None:
        return

    members = []
    for region in page.get("regions", []):
        for parcel in region.get("parcels") or []:
            if not parcel.get("human_confirmed") or not parcel.get("boundary_geojson_wgs84"):
                continue
            if parcel.get("anchor_override") or parcel.get("manual_position"):
                continue
            placement = parcel.get("placement") or {}
            if (placement.get("control_fit") or {}).get("validated"):
                continue  # placed from printed control points -- the stronger evidence
            if placement.get("status") == "pending":
                return  # wait until every confirmed parcel on the sheet is verified
            # verification rewrites `placement`, so a missing marker means the
            # current geometry is fresh (unfitted); otherwise use the saved original
            if placement.get("aliquot_fit") and parcel.get("boundary_geojson_wgs84_unfitted"):
                unfitted = parcel["boundary_geojson_wgs84_unfitted"]
            else:
                unfitted = copy.deepcopy(parcel["boundary_geojson_wgs84"])
            members.append((parcel, unfitted))
    if not members:
        return

    total_acres = sum((p.get("spatial_validation") or {}).get("area_acres") or 0 for p, _ in members)
    target_acres = aliquot["acres"]
    if not target_acres or abs(total_acres - target_acres) / target_acres > _ALIQUOT_FIT_AREA_TOL:
        # The parcels are only a PART of the described land ("a portion of the W1/2 of the NE1/4"):
        # no scale/extent check is possible, but they must lie inside it.
        if aliquot.get("portion_of") and _contain_in_aliquot(members, aliquot, total_acres):
            return
        for parcel, unfitted in members:  # undo any earlier fit that no longer holds
            if (parcel.get("placement") or {}).pop("aliquot_fit", None):
                parcel["boundary_geojson_wgs84"] = unfitted
                parcel.pop("boundary_geojson_wgs84_unfitted", None)
        return

    from pyproj import Geod
    geod = Geod(ellps="WGS84")
    pts = [pt for _, u in members for pt in u["geometry"]["coordinates"][0]]
    cur = ((min(p[0] for p in pts) + max(p[0] for p in pts)) / 2, (min(p[1] for p in pts) + max(p[1] for p in pts)) / 2)
    poly = aliquot["polygon"]
    tgt = (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))
    azimuth, _, shift_m = geod.inv(cur[0], cur[1], tgt[0], tgt[1])

    # How well the group's outer extents match the aliquot part's, east-west
    # and north-south. Area alone can agree for a wrong shape (or a rotated /
    # mis-scaled one); matching both extents too is what makes this
    # independent corroboration of scale, orientation AND position.
    def extents_m(points: list) -> tuple[float, float]:
        lons, lats = [p[0] for p in points], [p[1] for p in points]
        mid_lat, mid_lon = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
        ew = geod.inv(min(lons), mid_lat, max(lons), mid_lat)[2]
        ns = geod.inv(mid_lon, min(lats), mid_lon, max(lats))[2]
        return ew, ns
    group_ew, group_ns = extents_m(pts)
    aliq_ew, aliq_ns = extents_m(poly)
    area_err_pct = abs(total_acres - target_acres) / target_acres * 100
    # Relative to the aliquot part's LONG side: hand-drawn outlines land a few
    # tens of feet off, which on the short side of a long strip (Patnaude: 38 ft
    # of a 1,345 ft N-S extent) would read as several percent though it's under
    # 1% of the parcel. A genuinely wrong shape is off by thousands of feet.
    extent_err_pct = max(abs(group_ew - aliq_ew), abs(group_ns - aliq_ns)) / max(aliq_ew, aliq_ns) * 100
    corroborated = area_err_pct <= _ALIQUOT_CORROBORATED_PCT and extent_err_pct <= _ALIQUOT_CORROBORATED_PCT

    for parcel, unfitted in members:
        fitted = copy.deepcopy(unfitted)
        fitted["geometry"]["coordinates"] = [
            [list(geod.fwd(lon, lat, azimuth, shift_m)[:2]) for lon, lat in ring]
            for ring in unfitted["geometry"]["coordinates"]
        ]
        fitted["properties"]["georeferenced"] = "fitted_to_aliquot_part"
        parcel["boundary_geojson_wgs84_unfitted"] = unfitted
        parcel["boundary_geojson_wgs84"] = fitted
        placement = parcel.setdefault("placement", {"status": "approximate", "notes": []})
        placement["aliquot_fit"] = {
            "description": aliquot["description"], "shift_m": round(shift_m, 1),
            "confirmed_acres": round(total_acres, 2), "aliquot_acres": round(target_acres, 2),
            "area_error_pct": round(area_err_pct, 2), "extent_error_pct": round(extent_err_pct, 2),
            "corroborated": corroborated,
        }
        note = (
            f"fitted with the sheet's other confirmed parcels onto the {aliquot['description']} "
            f"(BLM section corners): combined {total_acres:.2f} ac vs {target_acres:.2f} ac; moved {shift_m:.0f} m"
        )
        placement["notes"] = [n for n in placement.get("notes", []) if not n.startswith("fitted with")] + [note]


def _fit_sheet_to_apn(result: dict, page_number: int) -> None:
    """
    Seats a sheet's confirmed parcels, as a GROUP, on the county's own parcel polygons for the APNs the
    document prints (result["apn_site"], see app/services/apn.py): onto the target parcel's polygon when
    it still exists and its area matches, otherwise in the gap between the neighbours, overlapping none
    of them and hugging their boundaries. A plat's neighbour APNs are exact, independent evidence of
    where it sits; on a Reno 3-lot split the printed-monument anchor left the lots ~65 m off, with the
    public road running through the middle lot.

    Translation only, applied only when the fit is corroborated (see apn.placement_by_apn), recomputed
    from each parcel's unfitted placement every time. Takes precedence over the aliquot fit (finer
    evidence); parcels placed from validated control points or by hand are left alone.
    """

    from app.services.apn import placement_by_apn

    site = result.get("apn_site")
    page = next((p for p in result.get("pages", []) if p["page_number"] == page_number), None)
    if not site or page is None:
        return

    members = []
    for region in page.get("regions", []):
        for parcel in region.get("parcels") or []:
            if not parcel.get("human_confirmed") or not parcel.get("boundary_geojson_wgs84"):
                continue
            if parcel.get("anchor_override") or parcel.get("manual_position"):
                continue
            placement = parcel.get("placement") or {}
            if (placement.get("control_fit") or {}).get("validated"):
                continue
            if placement.get("status") == "pending":
                return  # wait until every confirmed parcel on the sheet is verified
            fitted_before = placement.get("apn_fit") or placement.get("aliquot_fit")
            if fitted_before and parcel.get("boundary_geojson_wgs84_unfitted"):
                unfitted = parcel["boundary_geojson_wgs84_unfitted"]
            else:
                unfitted = copy.deepcopy(parcel["boundary_geojson_wgs84"])
            members.append((parcel, unfitted))
    if not members:
        return

    fit = placement_by_apn([u["geometry"]["coordinates"][0] for _, u in members], site)
    for parcel, unfitted in members:
        placement = parcel.setdefault("placement", {"status": "approximate", "notes": []})
        placement["notes"] = [n for n in placement.get("notes", []) if not n.startswith("seated on the county parcel")]
        if not fit or not (fit["corroborated"] or fit.get("improves")):
            if placement.pop("apn_fit", None) and not placement.get("aliquot_fit"):
                parcel["boundary_geojson_wgs84"] = unfitted  # undo an earlier fit that no longer holds
                parcel.pop("boundary_geojson_wgs84_unfitted", None)
            continue
        turned = _rotate_feature(unfitted, fit.get("pivot"), fit.get("rotation_deg") or 0.0)
        moved = _shift_feature(turned, fit["east_m"], fit["north_m"])
        moved.setdefault("properties", {})["georeferenced"] = "fitted_to_county_parcels"
        parcel["boundary_geojson_wgs84_unfitted"] = unfitted
        parcel["boundary_geojson_wgs84"] = moved
        placement.pop("aliquot_fit", None)
        shift_m = math.hypot(fit["east_m"], fit["north_m"])
        placement["apn_fit"] = {**fit, "shift_m": round(shift_m, 1), "source": site.get("source")}
        how = (
            f"onto APN {site.get('target_apn')}" if fit["mode"] == "target_parcel"
            else f"between {len(site.get('neighbours') or [])} neighbouring parcels the plat names by APN"
        )
        turn = fit.get("rotation_deg") or 0.0
        placement["notes"].append(
            f"seated on the county parcel records ({site.get('source')}) {how}: moved {shift_m:.0f} m "
            + (f"and turned {turn:+.1f} deg " if abs(turn) >= 0.05 else "")
            + "from the anchor-based position."
            + ("" if fit["corroborated"] else
               " Orientation decided by the neighbouring parcels; position along the street front not pinned"
               " down, so the location stays unconfirmed.")
        )


def _unify_sheet_frame(result: dict, page_number: int) -> bool:
    """
    Parcels outlined on the SAME drawing share its scale and rotation, but each is calibrated on
    its own evidence and pinned by its own pivot -- so two parcels that share a boundary line can
    land apart and rotated differently (Payette ROS: Parcel 1 verified at 1.7 deg, Parcel 2
    unverified at 0 deg, ~120 m apart though they share a 490 ft line).

    The best-calibrated parcel keeps its placement; every other confirmed parcel on the sheet is
    re-expressed from the same pixel frame with that parcel's scale and rotation. Only
    translation/rotation of the group changes -- each outline's own shape and vertex count are
    untouched. Skips regions where the existing trusted placement already placed a parcel, and
    parcels with no placed geometry (the control-point fallback handles those).
    """

    page = next((p for p in result.get("pages", []) if p["page_number"] == page_number), None)
    if page is None:
        return False
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    changed = False
    for region in page.get("regions", []):
        members = _control_fit_members(region)
        if not members:
            continue
        movable = [
            p for p in members
            if (p.get("confirmed_boundary_pixels") or {}).get("vertices")
            and not (p.get("placement") or {}).get("control_fit", {}).get("validated")
        ]
        sheet_scale = _sheet_area_scale(movable) or _corroborated_parcel_scale(movable)
        placed = [p for p in movable if p.get("boundary_geojson_wgs84")]
        if not placed:
            continue
        # A confirmed outline the pipeline could not place on its own (no survey calls attributed to it)
        # is still fixed RELATIVE to the others by the drawing: it joins the frame only with a sheet scale.
        if sheet_scale is None:
            movable = placed
        scaled = [p for p in movable if (p.get("calibration") or {}).get("scale_ft_per_px") and p.get("boundary_geojson_wgs84")]
        if sheet_scale is None and (len(scaled) < 2 or not scaled):
            continue
        if len(movable) < 2:
            continue
        if not scaled:
            scaled = placed

        def verified(p):
            c = p.get("calibration") or {}
            return c.get("status") in ("cross_validated", "single_source") and c.get("rotation_deg") is not None

        pool = [p for p in scaled if verified(p)] or scaled
        best = min(pool, key=lambda p: (p.get("calibration") or {}).get("scale_agreement_pct") if (p.get("calibration") or {}).get("scale_agreement_pct") is not None else 1e9)
        # Several parcels' stated areas agreeing on ONE scale is sheet-level evidence, stronger than any
        # single parcel's (Washoe 4-lot map: 0.4998-0.5023 ft/px from four areas, while two parcels had
        # rejected their own area scale over a 9% disagreement with one misread edge).
        scale = sheet_scale["scale"] if sheet_scale else best["calibration"]["scale_ft_per_px"]
        rotation = best["calibration"]["rotation_deg"] if verified(best) else 0.0

        def working(p):
            if not p.get("boundary_geojson_wgs84"):
                p["boundary_geojson_wgs84"] = {
                    "type": "Feature", "properties": {"parcel_label": p.get("parcel_label")},
                    "geometry": {"type": "Polygon", "coordinates": [[]]},
                }
                p.pop("georeference_error", None)
            return p.get("boundary_geojson_wgs84_unfitted") or p["boundary_geojson_wgs84"]

        ref_px = tuple(best["confirmed_boundary_pixels"]["vertices"][0])
        ref_ll = working(best)["geometry"]["coordinates"][0][0]
        for p in (movable if sheet_scale else scaled):
            own = (p.get("calibration") or {}).get("scale_ft_per_px")
            if sheet_scale or (own and abs(own - scale) / scale > _SHEET_SCALE_TOL):
                # redrawn at a scale other than its own: its area check must be at the scale it is drawn at
                stated_acres = _stated_acres(result, p)
                _rescale_validation(
                    p, scale, sheet_scale or {"source": "frame parcel scale"}, stated_acres * 43560 if stated_acres else None
                )
            ring = []
            for x, y in p["confirmed_boundary_pixels"]["vertices"]:
                dx, dy = (x - ref_px[0]) * scale, -(y - ref_px[1]) * scale
                az = (math.degrees(math.atan2(dx, dy)) + rotation) % 360
                lon, lat = geod.fwd(ref_ll[0], ref_ll[1], az, math.hypot(dx, dy) * 0.3048)[:2]
                ring.append([lon, lat])
            ring.append(list(ring[0]))
            geo = working(p)
            old = geo["geometry"]["coordinates"][0]
            if len(old) == len(ring) and max(geod.inv(a[0], a[1], b[0], b[1])[2] for a, b in zip(old, ring)) < 0.3:
                continue  # already in this frame
            geo["geometry"] = {"type": "Polygon", "coordinates": [ring]}
            geo.setdefault("properties", {})["georeferenced"] = "sheet_common_frame"
            placement = p.setdefault("placement", {"status": "approximate", "notes": []})
            placement["common_frame"] = {
                "from": (best.get("vision_geometry") or {}).get("parcel_label"),
                "rotation_deg": round(rotation, 3), "scale_ft_per_px": scale,
            }
            changed = True
    return changed


_SHEET_SCALE_TOL = 0.02


def _pixel_area(vertices: list) -> float:
    pts = [(float(x), float(y)) for x, y in vertices]
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))) / 2


def _sheet_area_scale(parcels: list[dict]) -> dict | None:
    """
    One scale for the whole sheet from the parcels' STATED areas: each confirmed outline's pixel area
    and its printed area imply ft/px; when two or more agree within _SHEET_SCALE_TOL, their median is
    the sheet's scale. None when fewer than two parcels state an area or they disagree.
    """

    implied = []
    for p in parcels:
        stated = (p.get("spatial_validation") or {}).get("stated_area_sqft")
        px = _pixel_area(p["confirmed_boundary_pixels"]["vertices"])
        if stated and px > 0:
            implied.append(math.sqrt(stated / px))
    if len(implied) < 2:
        return None
    median = sorted(implied)[len(implied) // 2]
    spread = max(abs(v - median) for v in implied) / median
    if spread > _SHEET_SCALE_TOL:
        return None
    return {"scale": median, "count": len(implied), "spread_pct": round(spread * 100, 2)}


_CORROBORATED_SCALE_PCT = 2.0  # stated-area scale and measured-edge scale agree this closely ...
_CORROBORATED_SCALE_EDGES = 2  # ... on at least this many edges


def _corroborated_parcel_scale(parcels: list[dict]) -> dict | None:
    """
    The sheet's scale from ONE parcel whose stated area and printed edge lengths agree on it, for a
    sheet where another confirmed parcel has no scale of its own (it rejected its evidence -- e.g. an
    area misread from a blurred label, which then disagreed with its edges). The parcels are drawn on
    the same sheet at the same scale, so the corroborated one stands for both. None unless some parcel
    lacks a scale and exactly one clear best exists.
    """

    if not any(not (p.get("calibration") or {}).get("scale_ft_per_px") for p in parcels):
        return None
    strong = [
        p for p in parcels
        if (c := p.get("calibration") or {}).get("scale_ft_per_px")
        and c.get("scale_agreement_pct") is not None and c["scale_agreement_pct"] <= _CORROBORATED_SCALE_PCT
        and (c.get("corroborating_edge_count") or 0) >= _CORROBORATED_SCALE_EDGES
    ]
    scales = [p["calibration"]["scale_ft_per_px"] for p in strong]
    if not scales or (max(scales) - min(scales)) / min(scales) > _SHEET_SCALE_TOL:
        return None  # several strong parcels that disagree: no single sheet scale to trust
    median = sorted(scales)[len(scales) // 2]
    return {"scale": median, "count": len(scales), "spread_pct": None, "source": "corroborated parcel scale"}


def _rescale_validation(parcel: dict, scale: float, sheet_scale: dict, stated_sqft: float | None = None) -> None:
    """Recomputes a parcel's area check at the sheet scale (its own calibration may have had none). A
    parcel the pipeline never placed gets one built from its confirmed outline."""

    vertices = parcel["confirmed_boundary_pixels"]["vertices"]
    sv = parcel.get("spatial_validation")
    if not sv:
        perimeter = sum(math.dist(a, b) for a, b in zip(vertices, vertices[1:] + vertices[:1])) * scale
        sv = parcel["spatial_validation"] = {
            "valid": True, "issues": [], "perimeter_ft": round(perimeter, 2), "precision_ratio": None,
            "self_intersects": False, "stated_area_sqft": stated_sqft, "curve_diagnostics": [],
            "source": "confirmed outline at the sheet scale",
        }
    area = _pixel_area(vertices) * scale * scale
    stated = sv.get("stated_area_sqft") or stated_sqft
    sv["stated_area_sqft"] = stated
    sv["area_sqft"] = round(area, 2)
    sv["area_acres"] = round(area / 43560, 3)
    sv["issues"] = [i for i in sv.get("issues", []) if not i.startswith("Walked area")]
    if stated:
        diff = abs(area - stated) / stated * 100
        sv["area_diff_pct"] = round(diff, 2)
        sv["area_matches_stated"] = diff <= 15
        sv["valid"] = diff <= 15 and not sv.get("self_intersects") and not sv["issues"]
    cal = parcel.setdefault("calibration", {})
    cal["sheet_scale"] = {**sheet_scale, "scale_ft_per_px": scale}
    if not cal.get("scale_ft_per_px"):
        cal["scale_ft_per_px"] = scale


def _rotate_feature(feature: dict, pivot: list | None, clockwise_deg: float) -> dict:
    """The GeoJSON polygon turned `clockwise_deg` about `pivot` (lon, lat), in a local metric frame."""

    out = copy.deepcopy(feature)
    if not pivot or abs(clockwise_deg) < 1e-9:
        return out
    lon0, lat0 = pivot
    kx, ky = 111_320 * math.cos(math.radians(lat0)), 110_540
    t = math.radians(clockwise_deg)
    c, s_ = math.cos(t), math.sin(t)
    rings = []
    for ring in feature["geometry"]["coordinates"]:
        new = []
        for lon, lat in ring:
            x, y = (lon - lon0) * kx, (lat - lat0) * ky
            new.append([lon0 + (x * c + y * s_) / kx, lat0 + (-x * s_ + y * c) / ky])
        rings.append(new)
    out["geometry"]["coordinates"] = rings
    return out


def _shift_feature(feature: dict, east_m: float, north_m: float) -> dict:
    """The same GeoJSON polygon translated by (east, north) metres: every vertex moves by the same vector."""

    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    az = math.degrees(math.atan2(east_m, north_m)) % 360
    dist = math.hypot(east_m, north_m)
    out = copy.deepcopy(feature)
    if dist == 0:
        return out
    out["geometry"]["coordinates"] = [
        [list(geod.fwd(lon, lat, az, dist)[:2]) for lon, lat in ring] for ring in feature["geometry"]["coordinates"]
    ]
    return out


def _reapply_manual_position(parcel: dict) -> None:
    """
    A user's move is stored as an offset from the COMPUTED position, so a re-verification (which
    recomputes that position) keeps it. A move made against an outline that has since been
    re-confirmed with different vertices no longer applies and is dropped.
    """

    manual = parcel.get("manual_position")
    if not manual:
        return
    if manual.get("confirmation_id") != (parcel.get("confirmed_boundary_pixels") or {}).get("id"):
        parcel.pop("manual_position", None)
        parcel.pop("boundary_geojson_wgs84_computed", None)
        return
    if not parcel.get("boundary_geojson_wgs84"):
        return
    parcel["boundary_geojson_wgs84_computed"] = copy.deepcopy(parcel["boundary_geojson_wgs84"])
    parcel["boundary_geojson_wgs84"] = _shift_feature(
        parcel["boundary_geojson_wgs84_computed"], manual["east_m"], manual["north_m"]
    )
    parcel["boundary_geojson_wgs84"].setdefault("properties", {})["georeferenced"] = "manually_placed"


class MoveParcelsRequest(BaseModel):
    page_number: int
    region_index: int
    parcel_indexes: list[int]
    east_m: float
    north_m: float


class ResetPositionRequest(BaseModel):
    page_number: int
    region_index: int
    parcel_indexes: list[int]


def _parcels_for(result: dict, page_number: int, region_index: int, indexes: list[int]) -> list[dict]:
    page = next((p for p in result["pages"] if p["page_number"] == page_number), None)
    if page is None or not (0 <= region_index < len(page["regions"])):
        raise HTTPException(status_code=404, detail="Page or region index out of range")
    parcels = page["regions"][region_index].get("parcels") or []
    out = []
    for i in indexes:
        if not (0 <= i < len(parcels)):
            raise HTTPException(status_code=404, detail="Parcel index out of range")
        out.append(parcels[i])
    return out


@router.post("/{document_id}/move-parcels")
def move_parcels(document_id: str, body: MoveParcelsRequest):
    """
    Drag-to-move: translates the given confirmed parcels (the whole sheet's group, or one) by
    (east_m, north_m). Shape, scale and rotation are untouched, so shared edges stay shared; only
    where the outline sits changes. The total move is stored on each parcel as an offset from its
    computed position (`manual_position`), which later verification re-applies and the automatic
    fits (common frame, aliquot, control points) leave alone. Reset restores the computed position.
    """

    if not (math.isfinite(body.east_m) and math.isfinite(body.north_m)) or math.hypot(body.east_m, body.north_m) > 50_000:
        raise HTTPException(status_code=400, detail="Move distance is not valid")
    with _RESULT_LOCK:
        result = _load_result(document_id)
        parcels = _parcels_for(result, body.page_number, body.region_index, body.parcel_indexes)
        for parcel in parcels:
            if not parcel.get("human_confirmed") or not parcel.get("boundary_geojson_wgs84"):
                raise HTTPException(status_code=400, detail="Only a confirmed, placed parcel can be moved")
        for parcel in parcels:
            manual = parcel.get("manual_position")
            conf_id = (parcel.get("confirmed_boundary_pixels") or {}).get("id")
            if not manual or manual.get("confirmation_id") != conf_id:
                parcel["boundary_geojson_wgs84_computed"] = copy.deepcopy(parcel["boundary_geojson_wgs84"])
                manual = {"confirmation_id": conf_id, "east_m": 0.0, "north_m": 0.0}
            manual["east_m"] = round(manual["east_m"] + body.east_m, 3)
            manual["north_m"] = round(manual["north_m"] + body.north_m, 3)
            parcel["manual_position"] = manual
            parcel["boundary_geojson_wgs84"] = _shift_feature(
                parcel["boundary_geojson_wgs84_computed"], manual["east_m"], manual["north_m"]
            )
            parcel["boundary_geojson_wgs84"].setdefault("properties", {})["georeferenced"] = "manually_placed"
            placement = parcel.setdefault("placement", {"status": "approximate", "notes": []})
            placement["notes"] = [n for n in placement.get("notes", []) if not n.startswith("moved by hand")] + [
                f"moved by hand {math.hypot(manual['east_m'], manual['north_m']):.1f} m from the computed position "
                f"({manual['east_m']:+.1f} m east, {manual['north_m']:+.1f} m north)"
            ]
        _save_result(document_id, result)
        return {"document_id": document_id, "parcels": parcels}


@router.post("/{document_id}/reset-position")
def reset_position(document_id: str, body: ResetPositionRequest):
    """Puts the given parcels back at their computed position (undoes drag-to-move)."""

    with _RESULT_LOCK:
        result = _load_result(document_id)
        parcels = _parcels_for(result, body.page_number, body.region_index, body.parcel_indexes)
        for parcel in parcels:
            computed = parcel.pop("boundary_geojson_wgs84_computed", None)
            if parcel.pop("manual_position", None) and computed:
                parcel["boundary_geojson_wgs84"] = computed
            placement = parcel.get("placement") or {}
            placement["notes"] = [n for n in placement.get("notes", []) if not n.startswith("moved by hand")]
        _save_result(document_id, result)
        return {"document_id": document_id, "parcels": parcels}


class StatedAreaRequest(BaseModel):
    page_number: int
    entity_id: str
    stated_area: str  # as printed, e.g. "1.55± AC." or "67,400 SQ. FT."; empty clears it


_AREA_TOLERANCE = 0.15  # as spatial_validation's area check


def _recheck_stated_area(sv: dict, stated_sqft: float | None) -> None:
    """The area check of a parcel's validation against a corrected printed area (its walked area stays)."""

    sv["stated_area_sqft"] = stated_sqft
    sv["issues"] = [i for i in sv.get("issues", []) if not i.startswith("Walked area")]
    area = sv.get("area_sqft")
    if stated_sqft and area is not None:
        diff = abs(area - stated_sqft) / stated_sqft
        sv["area_diff_pct"] = round(diff * 100, 2)
        sv["area_matches_stated"] = diff <= _AREA_TOLERANCE
        if diff > _AREA_TOLERANCE:
            sv["issues"].append(
                f"Walked area ({area:,.0f} sqft) differs from the document's stated area ({stated_sqft:,.0f} sqft) "
                f"by {diff:.0%} -- beyond the {_AREA_TOLERANCE:.0%} tolerance."
            )
    else:
        sv["area_diff_pct"] = None
        sv["area_matches_stated"] = None
    sv["valid"] = not sv["issues"] and not sv.get("self_intersects")


@router.put("/{document_id}/stated-area")
def set_stated_area(document_id: str, body: StatedAreaRequest):
    """
    Corrects a parcel's printed area where it was misread (a blurred label on a scan). Updates the sheet
    entity and every parcel bound to it, and re-runs their area check at their current scale; the reading
    as first extracted is kept as `stated_area_as_read`. Re-confirming the boundary recalibrates with it.
    """

    text = body.stated_area.strip()
    acres = parcel_roster.parse_acres(text) if text else None
    if text and not acres:
        raise HTTPException(status_code=400, detail="Area not understood -- e.g. '1.55 AC' or '67,400 SQ. FT.'")
    with _RESULT_LOCK:
        result = _load_result(document_id)
        page = next((p for p in result["pages"] if p["page_number"] == body.page_number), None)
        entity = next((e for e in ((page or {}).get("sheet") or {}).get("parcels", []) if e.get("id") == body.entity_id), None)
        if entity is None:
            raise HTTPException(status_code=404, detail="Parcel not found on this sheet")
        entity.setdefault("stated_area_as_read", entity.get("stated_area"))
        entity["stated_area"] = text or None
        entity["stated_area_sqft"] = acres * 43560 if acres else None
        entity["stated_area_edited"] = True
        bound = [p for r in page.get("regions", []) for p in r.get("parcels") or [] if p.get("roster_id") == body.entity_id]
        for parcel in bound:
            parcel.setdefault("vision_geometry", {})["stated_area_acres"] = str(acres) if acres else None
            if parcel.get("spatial_validation"):
                _recheck_stated_area(parcel["spatial_validation"], entity["stated_area_sqft"])
        _save_result(document_id, result)
        return {"document_id": document_id, "entity": entity, "parcels": bound}


class AgentChatRequest(BaseModel):
    message: str
    history: list[dict] = []  # earlier visible turns: [{"role": "user"|"assistant", "content": str}]
    page_number: int | None = None
    label: str | None = None


@router.post("/{document_id}/agent/chat")
def agent_chat(document_id: str, body: AgentChatRequest):
    """
    One turn with the placement-review agent (services/placement_agent.py). It reads a snapshot of the
    result -- no lock is held while it works (tens of seconds) -- and may return proposals, which are stored
    as pending on the result; nothing else changes until the user applies one.
    """

    from app.services import placement_agent

    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    snapshot = _load_result(document_id)
    focus = {"page_number": body.page_number, "label": body.label} if body.page_number is not None else None
    try:
        out = placement_agent.run(snapshot, body.message, body.history, focus)
    except placement_agent.AgentError as exc:
        raise HTTPException(status_code=502, detail=f"AI agent unavailable: {exc}")
    if out["proposals"]:
        with _RESULT_LOCK:
            result = _load_result(document_id)
            result.setdefault("agent_proposals", []).extend(out["proposals"])
            _save_result(document_id, result)
    return out


@router.post("/{document_id}/agent/chat/stream")
def agent_chat_stream(document_id: str, body: AgentChatRequest):
    """The same turn as /agent/chat, streamed as newline-delimited JSON events (placement_agent.run_events) so
    the panel shows each check as it happens. Proposals are stored when the turn finishes."""

    from fastapi.responses import StreamingResponse

    from app.services import placement_agent

    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    snapshot = _load_result(document_id)
    focus = {"page_number": body.page_number, "label": body.label} if body.page_number is not None else None

    def events():
        for event in placement_agent.run_events(snapshot, body.message, body.history, focus):
            if event["type"] == "done" and event["proposals"]:
                with _RESULT_LOCK:
                    result = _load_result(document_id)
                    result.setdefault("agent_proposals", []).extend(event["proposals"])
                    _save_result(document_id, result)
            yield json.dumps(event, default=str) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})


def _agent_proposal(result: dict, proposal_id: str) -> dict:
    proposal = next((p for p in result.get("agent_proposals", []) if p.get("id") == proposal_id), None)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if proposal.get("status") != "pending":
        raise HTTPException(status_code=409, detail=f"Proposal already {proposal.get('status')}")
    return proposal


@router.post("/{document_id}/agent/proposals/{proposal_id}/apply")
def apply_agent_proposal(document_id: str, proposal_id: str):
    """Applies a pending agent proposal through the same paths a user's own edit takes: a move is a hand move
    of the sheet's confirmed parcels (Reset position undoes it), an area is a stated-area correction."""

    with _RESULT_LOCK:
        result = _load_result(document_id)
        proposal = _agent_proposal(result, proposal_id)
    if proposal["kind"] == "move":
        page = next((p for p in result["pages"] if p["page_number"] == proposal["page_number"]), None)
        if page is None:
            raise HTTPException(status_code=404, detail="Page not found")
        for ri, region in enumerate(page.get("regions", [])):
            idx = [i for i, p in enumerate(region.get("parcels") or [])
                   if p.get("human_confirmed") and p.get("boundary_geojson_wgs84")]
            if idx:
                move_parcels(document_id, MoveParcelsRequest(
                    page_number=proposal["page_number"], region_index=ri, parcel_indexes=idx,
                    east_m=proposal["east_m"], north_m=proposal["north_m"],
                ))
    elif proposal["kind"] == "stated_area":
        page = next((p for p in result["pages"] if p["page_number"] == proposal["page_number"]), None)
        parcel = next((p for r in (page or {}).get("regions", []) for p in r.get("parcels") or []
                       if ((p.get("vision_geometry") or {}).get("parcel_label") or "").lower() == proposal["label"].lower()
                       and p.get("roster_id")), None)
        if parcel is None:
            raise HTTPException(status_code=404, detail="Parcel not found")
        set_stated_area(document_id, StatedAreaRequest(
            page_number=proposal["page_number"], entity_id=parcel["roster_id"], stated_area=proposal["stated_area"],
        ))
    else:
        raise HTTPException(status_code=400, detail="Unknown proposal kind")
    with _RESULT_LOCK:
        result = _load_result(document_id)
        stored = next(p for p in result.get("agent_proposals", []) if p.get("id") == proposal_id)
        stored.update(status="applied", applied_at=time.time())
        _save_result(document_id, result)
    return {"document_id": document_id, "proposal": stored}


@router.post("/{document_id}/agent/proposals/{proposal_id}/discard")
def discard_agent_proposal(document_id: str, proposal_id: str):
    with _RESULT_LOCK:
        result = _load_result(document_id)
        proposal = _agent_proposal(result, proposal_id)
        proposal.update(status="discarded", discarded_at=time.time())
        _save_result(document_id, result)
    return {"document_id": document_id, "proposal": proposal}


def _control_fit_members(region: dict) -> list[dict] | None:
    """
    Confirmed parcels of one region the control-point fallback may place, or None when the
    region must be left alone: a parcel is still verifying, or the existing trusted placement
    (a vertex bound to a printed parcel corner, a corroborated aliquot fit, a hand-pinned
    anchor) already placed it.
    """

    members = []
    for p in region.get("parcels") or []:
        if not p.get("human_confirmed") or not (p.get("confirmed_boundary_pixels") or {}).get("vertices"):
            continue
        placement = p.get("placement") or {}
        if placement.get("status") == "pending":
            return None
        reliable = (
            p.get("anchor_override")
            or p.get("manual_position")
            or (placement.get("status") == "surveyed_corner" and not placement.get("control_fit"))
            or (placement.get("aliquot_fit") or {}).get("corroborated")
        )
        if reliable:
            return None
        members.append(p)
    return members or None


def _control_fit_applicable(result: dict) -> bool:
    # The zone can only be identified when the document anchor is itself a printed state-plane pair.
    return "state-plane" in ((result.get("anchor") or {}).get("source") or "")


# A "reading" marker older than this is a read that died with its process (e.g. a dev-server
# reload): it must neither block a retry nor keep the UI showing "refining" forever. Real reads take
# 2-5 minutes; a dead one is noticed on the next page load (see get_document) and re-run.
_CONTROL_READ_STALE_S = 480


def _control_read_fresh(page: dict) -> bool:
    cp = page.get("control_points") or {}
    return cp.get("status") == "reading" and time.time() - cp.get("started_at", 0) < _CONTROL_READ_STALE_S


def _region_page_origin(region: dict) -> tuple[float, float]:
    bx, by, bw, bh = region["bbox"]
    if region.get("class") == "ParcelMap":
        mx = max(PARCELMAP_CROP_MARGIN_MIN_PX, bw * PARCELMAP_CROP_MARGIN_FRAC)
        my = max(PARCELMAP_CROP_MARGIN_MIN_PX, bh * PARCELMAP_CROP_MARGIN_FRAC)
    else:
        mx = my = 0
    return max(0, bx - mx), max(0, by - my)


def _ensure_control_points(document_id: str, page_number: int) -> None:
    """
    Reads and caches the page's printed control points (slow: OCR) when the fallback might need
    them, then runs the fit. The heavy OCR runs with no lock held; a "reading" marker on the page
    tells the UI the placement is still being refined.
    """

    with _RESULT_LOCK:
        try:
            result = _load_result(document_id)
        except HTTPException:
            return
        page = next((p for p in result.get("pages", []) if p["page_number"] == page_number), None)
        if page is None or not _control_fit_applicable(result):
            return
        existing = page.get("control_points")
        if existing is not None and (existing.get("status") != "reading" or _control_read_fresh(page)):
            return
        focus = []
        eligible = False
        for region in page.get("regions", []):
            members = _control_fit_members(region)
            # A sheet already seated on the county parcel records needs no coordinate read (minutes of OCR).
            if members and all(((p.get("placement") or {}).get("apn_fit") or {}).get("corroborated") for p in members):
                continue
            if members and any((p.get("calibration") or {}).get("scale_ft_per_px") for p in members):
                eligible = True
                ox, oy = _region_page_origin(region)
                focus += [(ox + v[0], oy + v[1]) for p in members for v in p["confirmed_boundary_pixels"]["vertices"]]
        if not eligible:
            if existing is not None:  # a dead read's marker, and nothing to read for: leave no stale state
                page.pop("control_points", None)
                _save_result(document_id, result)
            return
        page["control_points"] = {"status": "reading", "started_at": time.time()}
        _save_result(document_id, result)
    try:
        with Image.open(doc_access.doc_file(document_id, "pages", f"page_{page_number:03d}.png")) as img:
            img.load()
            first_lines, _ = run_parcelmap_ocr(img)
            first = control_points.extract_control_points(first_lines, "ocr", positions_trusted=False)
            # Labels sit beside the corners they name: read the tiles around the confirmed polygon
            # first, and the rest of the page only if that did not find two complete pairs.
            origins = control_points.tile_origins(*img.size)
            near = control_points.tiles_near(origins, focus, radius=450)
            tile_lines = control_points.recover_control_points(img, run_parcelmap_ocr, only_tiles=near)
            if len(control_points.extract_control_points(tile_lines, "ocr_recovered", True)) < 2 and len(near) < len(origins):
                rest = [o for o in origins if o not in set(near)]
                tile_lines += control_points.recover_control_points(img, run_parcelmap_ocr, only_tiles=rest)
    except Exception as exc:  # noqa: BLE001 -- best-effort; the existing placement stands
        logging.getLogger(__name__).warning("control point read failed for %s: %s", document_id, exc)
        with _RESULT_LOCK:
            try:
                result = _load_result(document_id)
                pg = next(p for p in result["pages"] if p["page_number"] == page_number)
                pg.pop("control_points", None)  # not a result: let a later verification retry
                _save_result(document_id, result)
            except (HTTPException, StopIteration):
                pass
        return
    tiled = control_points.extract_control_points(tile_lines, "ocr_recovered", positions_trusted=True)
    near_pt = lambda a, b: math.hypot(a.northing - b.northing, a.easting - b.easting) <= 0.5  # noqa: E731
    for t in tiled:
        if any(near_pt(f, t) for f in first):
            t.source = "ocr"  # the first pass already saw this pair; only its position is new
    merged = tiled + [f for f in first if not any(near_pt(f, t) for t in tiled)]
    with _RESULT_LOCK:
        try:
            result = _load_result(document_id)
        except HTTPException:
            return
        page = next((p for p in result.get("pages", []) if p["page_number"] == page_number), None)
        if page is None:
            return
        page["control_points"] = {"status": "done", "points": [c.to_dict() for c in merged], "tile_pass": True}
        _fit_sheet_to_control_points(result, page_number)
        _save_result(document_id, result)


def _fit_sheet_to_control_points(result: dict, page_number: int) -> bool:
    """
    Fallback placement from TWO printed surveyed coordinates (see services/control_points.py).
    Runs only where the existing trusted placement did not place a region's confirmed parcels,
    never changes a polygon's own vertices, and applies one transform to every confirmed parcel
    of the region -- so parcels sharing an edge keep sharing it, from the same survey framework.
    """

    page = next((p for p in result.get("pages", []) if p["page_number"] == page_number), None)
    cache = (page or {}).get("control_points")
    if not cache or not cache.get("points") or not _control_fit_applicable(result) or result.get("anchor_lat") is None:
        return False
    points = [control_points.ControlPoint.from_dict(d) for d in cache["points"]]
    if len(points) < 2:
        return False
    # Some sheets print GROUND coordinates plus a combined factor (Washoe County); the anchor was converted
    # with it (georeference.find_surveyed_coordinates), so the control points must be too.
    page_text = "\n".join(r.get("ocr_text") or "" for pg in result.get("pages", []) for r in pg.get("regions", []))
    to_grid = ground_to_grid_multiplier(page_text) or 1.0
    if to_grid != 1.0:
        for c in points:
            c.northing *= to_grid
            c.easting *= to_grid
    epsg = control_points.resolve_crs_from_anchor(points, result["anchor_lat"], result["anchor_lon"])

    from pyproj import Transformer

    applied = False
    for region in page.get("regions", []):
        members = _control_fit_members(region)
        if not members:
            continue
        if epsg is None:
            outcome = control_points.ControlSolution(
                "unverified", "no state-plane zone converts a printed coordinate onto the document anchor"
            )
        else:
            vertices = [tuple(v) for p in members for v in p["confirmed_boundary_pixels"]["vertices"]]
            scaled = [p for p in members if (p.get("calibration") or {}).get("scale_ft_per_px")]
            best = min(
                scaled or members,
                key=lambda p: (p.get("calibration") or {}).get("scale_agreement_pct")
                if (p.get("calibration") or {}).get("scale_agreement_pct") is not None else 1e9,
            )
            cal = best.get("calibration") or {}
            known = cal.get("rotation_deg") if cal.get("status") in ("cross_validated", "single_source") else None
            outcome = control_points.solve_control_points(
                points, vertices, cal.get("scale_ft_per_px"),
                rotation_candidates_deg=cal.get("rotation_ambiguous_candidates_deg") or None,
                known_rotation_deg=known,
            )
        if outcome.status != "validated":
            for p in members:
                placement = p.setdefault("placement", {"status": "approximate", "notes": []})
                if not (placement.get("control_fit") or {}).get("validated"):
                    placement["control_fit"] = {"validated": False, "status": outcome.status, "reason": outcome.reason}
            continue
        to_wgs84 = Transformer.from_crs(epsg, 4326, always_xy=True)
        a, b = points[outcome.anchor_index], points[outcome.other_index]
        for parcel in members:
            verts = [tuple(v) for v in parcel["confirmed_boundary_pixels"]["vertices"]]
            ring = [list(to_wgs84.transform(e, n)) for e, n in control_points.place_vertices(outcome, verts)]
            ring.append(list(ring[0]))
            borrowed_scale = not (parcel.get("calibration") or {}).get("scale_ft_per_px")
            geo = parcel.get("boundary_geojson_wgs84") or {"type": "Feature", "properties": {}}
            geo["geometry"] = {"type": "Polygon", "coordinates": [ring]}
            geo.setdefault("properties", {})["georeferenced"] = "fitted_to_control_points"
            parcel["boundary_geojson_wgs84"] = geo
            parcel.pop("boundary_geojson_wgs84_unfitted", None)
            if not parcel.get("spatial_validation"):
                # Shape checks for a parcel whose own calibration produced no geometry: the placed ring
                # in ground feet, measured the same way as every other confirmed parcel.
                en = control_points.place_vertices(outcome, verts)
                local = [(e - outcome.anchor_en[0], n - outcome.anchor_en[1]) for e, n in en]
                traverse = TraverseResult(points=local + [local[0]], closure_error_ft=0.0, unparsed_calls=0)
                parcel["boundary_geojson"] = traverse_to_geojson(traverse)
                parcel["spatial_validation"] = validate_traverse(
                    traverse, "", stated_area_acres=(parcel.get("vision_geometry") or {}).get("stated_area_acres"),
                    calls=parcel.get("resolved_boundary_calls"),
                )
            placement = parcel.setdefault("placement", {"status": "approximate", "notes": []})
            placement.pop("aliquot_fit", None)
            placement["status"] = "surveyed_corner"
            placement["control_fit"] = {
                "validated": True, "method": "two_control_points", "epsg": epsg,
                "residual_ft": outcome.residual_ft, "tolerance_ft": outcome.tolerance_ft,
                "separation_ft": outcome.separation_ft, "rotation_deg": round(outcome.rotation_deg, 2),
                "rotation_source": outcome.rotation_source, "scale_ft_per_px": outcome.scale_ft_per_px,
                "anchor_control_point": a.to_dict(), "check_control_point": b.to_dict(),
                "candidate_correspondences": outcome.details.get("candidate_correspondences"),
                "scale_borrowed_from_sheet": borrowed_scale,
                "ground_to_grid_multiplier": to_grid if to_grid != 1.0 else None,
            }
            stale = ("two printed control points", "position comes from the document-level anchor", "scale/rotation not calibrated")
            placement["notes"] = [
                n for n in placement.get("notes", [])
                if not n.startswith(stale) and "cannot be placed" not in n
            ] + [
                f"two printed control points (N {a.northing} E {a.easting} and N {b.northing} E {b.easting}) matched one "
                f"vertex pair {outcome.separation_ft:.1f} ft apart; rotation {outcome.rotation_deg:.1f} deg from "
                f"{outcome.rotation_source}; the second point was predicted within {outcome.residual_ft:.1f} ft "
                f"(tolerance {outcome.tolerance_ft:.1f} ft)"
            ]
        applied = True
    return applied


def _seed_local_vertices(parcel: dict, body) -> list[list[float]] | None:
    """
    Server-side twin of the review screen's pixel->local-feet seed transform
    (computeLocalTransform/toLocalVertices): the confirmed pixel ring projected
    back into the local, anchor-relative feet system of the parcel's own
    extracted traverse. Used when the polygon was drawn before the parcel (and
    so its seed ring) existed in the browser. Built from resolved_boundary_calls,
    never from boundary_geojson, which a previous confirm may have replaced.
    """

    calls = parcel.get("resolved_boundary_calls")
    if not calls:
        return None
    try:
        ring = walk_traverse(calls).points[:-1]
    except Exception:  # noqa: BLE001
        return None
    if len(ring) < 3:
        return None
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    w, h = (max(xs) - min(xs)) or 1.0, (max(ys) - min(ys)) or 1.0
    pad = 0.85
    scale = min(body.crop_width * pad / w, body.crop_height * pad / h)
    cx, cy = body.crop_width / 2, body.crop_height / 2
    mid_x, mid_y = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    return [[mid_x + (px - cx) / scale, mid_y - (py - cy) / scale] for px, py in body.vertices]


def _apply_confirmation(document_id: str, result: dict, body, wait: bool) -> tuple[dict, bool, str]:
    """
    Stores a polygon on a parcel and gives it a provisional map position at the
    document anchor (verification follows in the background, or inline with
    wait=True). Caller holds _RESULT_LOCK and saves `result`.
    Returns (parcel, placed_on_map, confirmation_id).
    """

    located = _locate_parcel(result, body)
    if located is None:
        raise HTTPException(status_code=404, detail="Page, region or parcel index out of range")
    region, parcels, parcel = located
    if len(body.vertices) < 3:
        raise HTTPException(status_code=400, detail="A boundary needs at least 3 vertices")

    confirmation_id = uuid4().hex
    parcel["confirmed_boundary_pixels"] = {
        "vertices": body.vertices,
        "crop_width": body.crop_width,
        "crop_height": body.crop_height,
        "id": confirmation_id,
    }
    if body.curve_spec:
        parcel["confirmed_boundary_pixels"]["curve_spec"] = body.curve_spec
    parcel["human_confirmed"] = True

    if not body.local_vertices:
        body.local_vertices = _seed_local_vertices(parcel, body)
    anchor_lat, anchor_lon = _anchor_for(result, parcel)
    placed = False
    # A manually confirmed polygon is sufficient on its own to enter
    # verification (Gemini edge-association + OCR) -- body.local_vertices
    # is no longer required here. It used to gate this entirely, which
    # meant a parcel with zero resolved_boundary_calls (nothing for
    # _seed_local_vertices to build a seed from -- exactly a hand-confirmed
    # "remainder parcel") never reached Gemini at all, even though the
    # confirmed polygon's own pixel vertices are a perfectly good starting
    # point. local_vertices (possibly None) still flows into
    # _derive_confirmed_geometry, which now handles that case explicitly.
    if len(body.vertices) >= 3 and anchor_lat is not None and anchor_lon is not None:
        _derive_confirmed_geometry(
            document_id, body, result, region, parcels, parcel, anchor_lat, anchor_lon, verify=wait
        )
        placed = True
    else:
        parcel["calibration"] = {
            "status": "unverified", "scale_ft_per_px": None, "rotation_deg": None,
            "corroborating_edge_count": 0, "corroborations": [],
            "notes": ["no document anchor for this parcel, so the outline is saved but cannot be placed on the map"],
        }
        parcel["placement"] = {"status": "approximate", "notes": parcel["calibration"]["notes"]}
    return parcel, placed, confirmation_id


def _page(result: dict, page_number: int) -> dict:
    return next(p for p in result["pages"] if p["page_number"] == page_number)


def _ensure_evidence(result: dict, page_number: int, entity: dict) -> None:
    """
    An entity the user outlined that no extracted parcel matched still needs a record the rest
    of the app can show: a parcel is created from it (it carries the roster's printed area), with
    no survey calls, so the outline is kept but cannot be placed on the map.
    """

    if entity.get("evidence_ref"):
        return
    region = _page(result, page_number)["regions"][entity["region_index"]]
    parcels = region.setdefault("parcels", [])
    area = parcel_roster.parse_acres(entity.get("stated_area"))
    parcels.append({
        "vision_geometry": {"parcel_label": entity["label"], "stated_area_acres": str(area) if area else None},
        "resolved_boundary_calls": [],
        "created_from_confirmed_boundary": True,
        "roster_id": entity["id"],
    })
    entity["evidence_ref"] = {"region": entity["region_index"], "parcel": len(parcels) - 1}


def _bind_entity(document_id: str, result: dict, page_number: int, entity: dict):
    """
    Applies ONE entity's confirmed polygon to its evidence parcel (provisional placement now,
    calibration/placement in the background). Returns (body, confirmation_id) to verify, or None.
    Only once the pipeline has finished -- before that there is no evidence to attach to.
    """

    poly = entity.get("confirmed_polygon")
    if not poly or not (result.get("processing") or {}).get("complete", True):
        return None
    _ensure_evidence(result, page_number, entity)
    ref = entity["evidence_ref"]
    body = ConfirmBoundaryRequest(
        page_number=page_number, region_index=ref["region"], parcel_index=ref["parcel"],
        vertices=poly["vertices"], crop_width=poly["crop_width"], crop_height=poly["crop_height"],
        local_vertices=poly.get("local_vertices"), curve_spec=poly.get("curve_spec"),
    )
    if (poly.get("region_index") is not None) and poly["region_index"] != ref["region"]:
        # drawn on the sheet's main drawing, but the extracted parcel was read from another region:
        # the pixel frame differs, so the outline is kept but not projected through that region's ring.
        body.local_vertices = None
    parcel, placed, confirmation_id = _apply_confirmation(document_id, result, body, wait=False)
    poly["applied_id"] = confirmation_id
    return (body, confirmation_id) if placed else None


def _sheet_and_entity(result: dict, body) -> tuple[dict, dict | None]:
    page = next((p for p in result["pages"] if p["page_number"] == body.page_number), None)
    if page is None or not (0 <= body.region_index < len(page["regions"])):
        raise HTTPException(status_code=404, detail="Page or region index out of range")
    sheet = page.get("sheet")
    if not sheet:
        raise HTTPException(status_code=400, detail="This page has no parcel-map sheet to attach a parcel to")
    entity = None
    if body.parcel_id:
        entity = next((e for e in sheet.get("parcels", []) if e["id"] == body.parcel_id), None)
        if entity is None:
            raise HTTPException(status_code=404, detail=f"No parcel {body.parcel_id} on page {body.page_number}")
    return sheet, entity


@router.post("/{document_id}/confirm-boundary")
def confirm_boundary(
    document_id: str, body: ConfirmBoundaryRequest, background_tasks: BackgroundTasks, wait: bool = False
):
    """
    Confirm-and-edit boundary review: stores a human-confirmed/corrected polygon (in the pixel
    frame of its region's crop image) and places it on the map right away at the document
    anchor. The confirmed polygon is the authoritative geometry; the vision-extracted traverse is
    only evidence for scale/orientation.

    The outline belongs to ONE PARCEL, addressed by `parcel_id` (a sheet entity: Parcel 1,
    Remainder Parcel, ...) -- or, with `label` and no id, to a parcel the user is naming by hand.
    Each entity holds its own polygon: confirming one never touches another. If the pipeline has
    finished, the outline is bound to that parcel's extracted evidence now; if not, it is kept on
    the entity and bound when extraction completes (the evidence join). Saving never waits on
    verification: calibration and absolute placement run afterwards in the background
    (calibration.status/placement.status are "pending" until then; `wait=true` runs them inline,
    for diagnostics). `parcel_index` alone is the legacy path for documents with no entities.
    """

    with _RESULT_LOCK:
        result = _load_result(document_id)

        if body.parcel_id is None and not (body.label or "").strip():
            # legacy: a parcel addressed by index into region.parcels
            if body.parcel_index is None:
                raise HTTPException(status_code=400, detail="parcel_id, label or parcel_index is required")
            parcel, placed, confirmation_id = _apply_confirmation(document_id, result, body, wait)
            if placed and not wait:
                background_tasks.add_task(_verify_confirmation, document_id, body, confirmation_id)
            _save_result(document_id, result)
            return {"document_id": document_id, "state": "bound", "parcel": parcel,
                    "georeferenced_from_confirmation": placed}

        if len(body.vertices) < 3:
            raise HTTPException(status_code=400, detail="A boundary needs at least 3 vertices")
        sheet, entity = _sheet_and_entity(result, body)
        if entity is None:  # a parcel the user is naming by hand
            entities = sheet.setdefault("parcels", [])
            entity = parcel_roster.new_entity(
                body.page_number, entities, label=body.label.strip(), region_index=body.region_index,
                source="manual", manual=True,
            )
            entities.append(entity)
        entity["confirmed_polygon"] = {
            "vertices": body.vertices, "crop_width": body.crop_width, "crop_height": body.crop_height,
            "local_vertices": body.local_vertices, "region_index": body.region_index, "id": uuid4().hex,
            **({"curve_spec": body.curve_spec} if body.curve_spec else {}),
        }
        pending = None
        if (result.get("processing") or {}).get("complete", True):
            if not entity.get("evidence_ref"):  # e.g. a parcel just named by hand: join it by label
                parcel_roster.join_evidence(body.page_number, sheet, _page(result, body.page_number)["regions"])
            pending = _bind_entity(document_id, result, body.page_number, entity)
            if pending and not wait:
                background_tasks.add_task(_verify_confirmation, document_id, *pending)
        _save_result(document_id, result)

        state = "waiting_for_document" if not (result.get("processing") or {}).get("complete", True) else "bound"
        ref = entity.get("evidence_ref")
        parcel = (
            _page(result, body.page_number)["regions"][ref["region"]]["parcels"][ref["parcel"]]
            if ref and state == "bound" else None
        )

    return {"document_id": document_id, "state": state, "entity": entity, "parcel": parcel,
            "georeferenced_from_confirmation": bool(parcel and parcel.get("boundary_geojson_wgs84"))}


@router.delete("/{document_id}/pages/{page_number}/parcels/{parcel_id}")
def delete_parcel_entity(document_id: str, page_number: int, parcel_id: str):
    """
    Removes one sheet-owned parcel entity (a roster entry or an unmatched
    extraction fragment) that the user has identified as spurious -- e.g. a
    scattered extraction fragment or a duplicate from a secondary sheet that
    slipped past the automatic exclusion heuristics (see parcel_roster's
    excluded_reason gate). A manual escape hatch for whatever that heuristic
    misses, since it can only catch the failure signatures it already knows
    about.

    Refuses to delete a parcel the user has already confirmed
    (entity["confirmed_polygon"] set) -- that is real, hand-drawn work, not
    a spurious candidate, and removing it needs a deliberate "unconfirm"
    action this endpoint does not perform.
    """

    with _RESULT_LOCK:
        result = _load_result(document_id)
        page = next((p for p in result["pages"] if p["page_number"] == page_number), None)
        if page is None or not page.get("sheet"):
            raise HTTPException(status_code=404, detail="Page or sheet not found")
        entities = page["sheet"].get("parcels") or []
        entity = next((e for e in entities if e["id"] == parcel_id), None)
        if entity is None:
            raise HTTPException(status_code=404, detail=f"No parcel {parcel_id} on page {page_number}")
        if entity.get("confirmed_polygon"):
            raise HTTPException(
                status_code=400,
                detail="This parcel has already been confirmed and cannot be deleted this way.",
            )
        entities.remove(entity)
        # The extracted parcel it was bound to is what the workspace lists: mark it removed too, or the
        # same parcel keeps appearing there (and the next evidence join would recreate the entity).
        ref = entity.get("evidence_ref")
        if ref:
            try:
                page["regions"][ref["region"]]["parcels"][ref["parcel"]]["deleted"] = True
            except (IndexError, KeyError):
                pass
        _save_result(document_id, result)

    return {"document_id": document_id, "deleted": parcel_id}


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

# ------------------------------------------------------------------------------------------------
# Final report: viewer model, reviewer edits, figures and exports (app/services/report*.py)
# ------------------------------------------------------------------------------------------------

class ReportEditsRequest(BaseModel):
    project: dict[str, str | None] = {}
    parcels: dict[str, dict[str, str | None]] = {}
    editor: str | None = None


_EXPORTS = {
    # fmt: (filename suffix, media type)
    "pdf": ("report.pdf", "application/pdf"),
    "zip": ("package.zip", "application/zip"),
    "geojson": ("parcels.geojson", "application/geo+json"),
    "kml": ("parcels.kml", "application/vnd.google-earth.kml+xml"),
    "shp": ("shapefile_wgs84.zip", "application/zip"),
    "shp-stateplane": ("shapefile_stateplane.zip", "application/zip"),
    "csv": ("attributes.csv", "text/csv"),
    "vertices-csv": ("vertices.csv", "text/csv"),
}


def _report_for(document_id: str) -> dict:
    from app.services.report import build_report

    report = build_report(_load_result(document_id), document_id)
    if not report["parcels"]:
        raise HTTPException(status_code=409, detail="No confirmed, placed parcels yet -- confirm boundaries first.")
    return report


@router.get("/{document_id}/report")
def get_report(document_id: str):
    """The report model the viewer shows (and every export is rendered from)."""

    return _report_for(document_id)


@router.put("/{document_id}/report/edits")
def save_report_edits(document_id: str, body: ReportEditsRequest):
    """Merges a reviewer's edits (project header, parcel label / APN / review status / notes) into the
    document, logging each change, and returns the updated report."""

    from app.services.report import merge_edits

    with _RESULT_LOCK:
        result = _load_result(document_id)
        result["report_edits"] = merge_edits(
            result.get("report_edits") or {}, body.model_dump(), body.editor
        )
        _save_result(document_id, result)
    return _report_for(document_id)


@router.get("/{document_id}/report/figure/{name}")
def report_figure(document_id: str, name: str):
    """A report figure (parcels.png / location.png), cached per parcel geometry."""

    import hashlib
    from fastapi.responses import Response
    from app.services import static_map

    if name not in ("parcels.png", "location.png"):
        raise HTTPException(status_code=404, detail="Unknown figure")
    report = _report_for(document_id)
    rings = [p["ring"] for p in report["parcels"]]
    labels = [p["label"] for p in report["parcels"]]
    key = hashlib.sha1(json.dumps([name, rings, labels]).encode()).hexdigest()[:16]
    cache = DOCUMENT_ROOT / document_id / "report_cache" / f"{key}.png"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        png = (
            static_map.render(rings, labels, width=1400, height=1000) if name == "parcels.png"
            else static_map.render(rings, zoom_out=5, marker_only=True, place_labels=True, width=1400, height=800)
        )
        cache.write_bytes(png)
    return Response(content=cache.read_bytes(), media_type="image/png")


@router.get("/{document_id}/export/{fmt}")
def export_report(document_id: str, fmt: str):
    """Downloads the report / data in one format, or the whole deliverable package (fmt=zip)."""

    from fastapi.responses import Response
    from app.services import report_export as rx

    if fmt not in _EXPORTS:
        raise HTTPException(status_code=404, detail=f"Unknown export format. One of: {', '.join(_EXPORTS)}")
    report = _report_for(document_id)
    builders = {
        "pdf": lambda: rx.pdf(report),
        "zip": lambda: rx.package(report),
        "geojson": lambda: rx.geojson(report),
        "kml": lambda: rx.kml(report),
        "shp": lambda: rx.shapefile_zip(report),
        "shp-stateplane": lambda: rx.shapefile_zip(report, report["crs"]["state_plane"]),
        "csv": lambda: rx.attributes_csv(report),
        "vertices-csv": lambda: rx.vertices_csv(report),
    }
    if fmt == "shp-stateplane" and not report["crs"]["state_plane"]:
        raise HTTPException(status_code=409, detail="No state-plane zone for this location.")
    suffix, media = _EXPORTS[fmt]
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(report["project"]["title"]).stem)[:60].strip("_") or "roam"
    return Response(
        content=builders[fmt](), media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{stem}_{suffix}"'},
    )


class FillRequest(BaseModel):
    x: float
    y: float
    gap: int = 5


@router.post("/{document_id}/pages/{page_number}/regions/{region_index}/fill")
def fill_region_at(document_id: str, page_number: int, region_index: int, body: FillRequest):
    """Click-to-fill for the boundary editor: the region around (x, y) -- crop pixels, the same crop
    .../crop.png serves -- bounded by the drawing's linework, as a polygon (app/services/region_fill.py)."""

    from app.services.region_fill import crop_gray, fill_region

    result = _load_result(document_id)
    page = next((p for p in result["pages"] if p["page_number"] == page_number), None)
    if page is None or not (0 <= region_index < len(page["regions"])):
        raise HTTPException(status_code=404, detail="No such page/region")
    region = page["regions"][region_index]
    page_path = doc_access.doc_file(document_id, "pages", f"page_{page_number:03d}.png")
    if not page_path.exists():
        raise HTTPException(status_code=404, detail="Rendered page not found")
    return fill_region(crop_gray(page_path, region["bbox"], region.get("class")), body.x, body.y, body.gap)
