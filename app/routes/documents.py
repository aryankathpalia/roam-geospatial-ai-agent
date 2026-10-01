import asyncio
import copy
import json
import logging
import threading
from uuid import uuid4
from pathlib import Path

import io

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
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
from app.services.region_cropper import PARCELMAP_CROP_MARGIN_FRAC, PARCELMAP_CROP_MARGIN_MIN_PX, padded_crop_box
from app.services.ocr import run_parcelmap_ocr
from app.services import calibration as calibration_service
from app.services import parcel_roster
from app.services import gemini_edge_association
from app.services import placement as placement_service
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
    # Write-then-rename: a GET (or the background verification) reading
    # result.json mid-save must never see a half-written file.
    path = _result_path(document_id)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(result, indent=2), encoding="utf-8")
    tmp.replace(path)


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
        # "processing": only the candidate regions exist so far (saved right after
        # layout + triage); OCR, anchor and vision extraction are still running.
        "status": "processed" if (result.get("processing") or {}).get("complete", True) else "processing",
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

    old_local_points = [(float(x), float(y)) for x, y in body.local_vertices]
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


def _verify_confirmation(document_id: str, body, confirmation_id: str) -> None:
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
        _save_result(document_id, result)


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
    parcel["human_confirmed"] = True

    if not body.local_vertices:
        body.local_vertices = _seed_local_vertices(parcel, body)
    anchor_lat, anchor_lon = _anchor_for(result, parcel)
    placed = False
    if body.local_vertices and len(body.local_vertices) >= 3 and anchor_lat is not None and anchor_lon is not None:
        _derive_confirmed_geometry(
            document_id, body, result, region, parcels, parcel, anchor_lat, anchor_lon, verify=wait
        )
        placed = True
    else:
        parcel["calibration"] = {
            "status": "unverified", "scale_ft_per_px": None, "rotation_deg": None,
            "corroborating_edge_count": 0, "corroborations": [],
            "notes": ["no extracted survey calls or document anchor for this parcel, so the outline is saved but cannot be placed on the map"],
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
        local_vertices=poly.get("local_vertices"),
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