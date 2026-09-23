"""
Main document processing pipeline: render + detect every page (fast,
sequential), then fan out OCR across every page's BANDS concurrently --
not per page, per band. See app/pipeline/page_ocr.py for why: a single
70-region page took 68s on its own, which meant page-level parallelism
alone still bottlenecked on that one page once every other page had
finished. Splitting dense pages into bands and flattening ALL bands
across the WHOLE document into one fan-out means no single page (or
band) can dominate the total time.

The OCR fan-out is pluggable (`ocr_dispatcher`). Locally, bands just
run one after another in a thread pool -- fine for dev. On Modal,
modal_app.py overrides DEFAULT_OCR_DISPATCHER with one that fans bands
out to separate containers via Function.map().
"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from PIL import Image

from app.services.geocoding import GeocodingError, geocode_place
from app.services.georeference import find_anchor_query, georeference_traverse_to_geojson
from app.services.geometry import (
    drop_conflicting_axis_duplicates,
    resolve_ambiguous_calls,
    traverse_to_geojson,
    walk_traverse,
)
from app.services.layout_detector_onnx import detect_page_layout
from app.services.spatial_validation import (
    check_combined_tract_dimension,
    parse_stated_area_acres,
    validate_traverse,
)
from app.services.ocr import OCRLine
from app.services.pdf_inspector import inspect_pdf
from app.services.pdf_renderer import render_page
from app.services.region_cropper import extract_region_crops
from app.services.vision import extract_parcel_geometries_batch
from app.pipeline.page_ocr import (
    band_count_for,
    match_lines_to_regions,
    ocr_band,
    page_needs_ocr,
    split_page_into_bands,
)

DOCUMENT_ROOT = Path("data/documents")

# (band_png_bytes, y_offset) in -> OCR'd lines out, one pair per band.
BandJob = tuple[bytes, float]
OcrDispatcher = Callable[[list[BandJob]], Awaitable[list[list[OCRLine]]]]


async def _default_ocr_dispatcher(jobs: list[BandJob]) -> list[list[OCRLine]]:
    """Local-dev fallback: bands processed one after another in a thread."""

    loop = asyncio.get_running_loop()
    results = []
    for band_bytes, y_offset in jobs:
        lines = await loop.run_in_executor(None, ocr_band, band_bytes, y_offset)
        results.append(lines)
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
    # Split every OCR-eligible page into bands, and flatten ALL bands
    # from the WHOLE document into one fan-out -- so a dense page's own
    # bands run alongside other pages' bands, not just alongside other
    # whole pages.
    # ---------------------------------------------------------

    band_jobs: list[BandJob] = []
    # Parallel list: which page_entries index each band job belongs to.
    band_owner: list[int] = []

    for entry_index, entry in enumerate(page_entries):
        if not page_needs_ocr(entry["regions"]):
            for region in entry["regions"]:
                region["extraction_status"] = "skipped"
            continue

        with Image.open(entry["path"]) as image:
            image = image.convert("RGB")
            num_bands = band_count_for(entry["regions"])
            for band_bytes, y_offset in split_page_into_bands(image, num_bands):
                band_jobs.append((band_bytes, y_offset))
                band_owner.append(entry_index)

    if band_jobs:
        band_results = await ocr_dispatcher(band_jobs)

        lines_by_page: dict[int, list[OCRLine]] = {}
        for entry_index, lines in zip(band_owner, band_results):
            lines_by_page.setdefault(entry_index, []).extend(lines)

        for entry_index, lines in lines_by_page.items():
            match_lines_to_regions(lines, page_entries[entry_index]["regions"])

    # ---------------------------------------------------------
    # Find a real-world anchor for georeferencing (see
    # app/services/georeference.py for why this -- not a state-plane
    # projection -- is the approach): scan this document's own OCR'd
    # text for the most address-like line, geocode it ONCE per
    # document via the existing Nominatim geocoder. None if nothing
    # plausible is found -- traverses stay local-only, not guessed.
    # ---------------------------------------------------------

    anchor_lat: float | None = None
    anchor_lon: float | None = None
    anchor_query = find_anchor_query(
        [{"regions": e["regions"]} for e in page_entries]
    )
    if anchor_query:
        try:
            candidates = await geocode_place(anchor_query, limit=1)
            if candidates:
                anchor_lat = candidates[0].latitude
                anchor_lon = candidates[0].longitude
        except GeocodingError:
            pass

    # ---------------------------------------------------------
    # Vision escalation: ParcelMap regions (flagged needs_vision by
    # match_lines_to_regions) get sent to Gemini to pull out the
    # boundary traverse / tie point / basis of bearings needed for
    # geometry reconstruction -- OCR alone gives flat text, not
    # structured survey data.
    #
    # ALL ParcelMap regions in the document go into ONE batch call
    # (extract_parcel_geometries_batch), not one call per region --
    # Gemini's free tier caps gemini-3.5-flash-lite at 15 requests/
    # MINUTE, and a per-region approach previously needed ~2 calls PER
    # region (confirmed: 429s on half the regions in a 4-ParcelMap-
    # region document processed one at a time). Batching means a
    # document needs 2 Gemini calls total regardless of how many
    # ParcelMap regions it has.
    # ---------------------------------------------------------

    vision_targets = [
        (entry, region)
        for entry in page_entries
        for region in entry["regions"]
        if region.get("needs_vision")
    ]

    if vision_targets:
        crops = []
        for entry, region in vision_targets:
            x, y, w, h = region["bbox"]
            with Image.open(entry["path"]) as page_image:
                crops.append(page_image.convert("RGB").crop((x, y, x + w, y + h)))

        try:
            regions_parcels = await loop.run_in_executor(
                None, extract_parcel_geometries_batch, crops
            )
            for (entry, region), parcels in zip(vision_targets, regions_parcels):
                # A region's bounding box wraps the whole drawing, not
                # necessarily one parcel -- a "Parcel Map Exhibit" sheet
                # showing two adjacent parcels side by side is one
                # region but two parcels (confirmed via a live diagnostic
                # against a real such document: assuming 1 region = 1
                # parcel made the model interleave both parcels' calls
                # into one nonsensical traverse, reproducible even with
                # a single region processed in total isolation). So a
                # region now holds a LIST of parcels, each walked and
                # validated independently.
                region["parcels"] = []
                stated_sqfts = [
                    parse_stated_area_acres(g.get("stated_area_acres")) for g in parcels
                ]

                for idx, geometry in enumerate(parcels):
                    parcel_result: dict = {"vision_geometry": geometry}

                    # Walk the extracted boundary calls into an actual
                    # polygon. closure_error_ft is a real, standard
                    # surveying QA signal, not something we invented: a
                    # traverse that doesn't return near its start point
                    # is an honest sign the extracted calls are
                    # incomplete or include non-boundary noise --
                    # surfaced rather than hidden, since a wrong-looking
                    # polygon on the eventual map is worse than an
                    # honest "couldn't close" flag.
                    calls = geometry.get("boundary_calls") or []
                    # Vision may have flagged some calls as ambiguous
                    # (a bearing with more than one plausible distance
                    # reading -- see vision.py's ambiguous_alternates
                    # field). Resolving which reading is correct is a
                    # geometry question (which one actually closes the
                    # traverse), so it happens here in deterministic
                    # Python rather than asking vision to guess.
                    if calls and geometry.get("ambiguous_alternates"):
                        calls = resolve_ambiguous_calls(
                            calls,
                            geometry["ambiguous_alternates"],
                            stated_area_sqft=stated_sqfts[idx],
                            sibling_stated_sqfts=[
                                s for j, s in enumerate(stated_sqfts) if j != idx and s
                            ],
                        )
                    # Separate, second deterministic pass: catches
                    # same-axis opposite-direction calls with mismatched
                    # distances that vision never linked to each other
                    # as alternates (see drop_conflicting_axis_duplicates'
                    # docstring for the real case this was built for).
                    if calls:
                        calls = drop_conflicting_axis_duplicates(calls)
                    if calls:
                        traverse = walk_traverse(calls)
                        # The calls actually walked, after the resolvers --
                        # can differ from vision_geometry.boundary_calls
                        # (e.g. a parcel's own 352.40' segment replacing
                        # the combined 903.15' line vision read).
                        parcel_result["resolved_boundary_calls"] = calls
                        parcel_result["boundary_geojson"] = traverse_to_geojson(traverse)
                        parcel_result["spatial_validation"] = validate_traverse(
                            traverse,
                            region.get("ocr_text") or "",
                            stated_area_acres=geometry.get("stated_area_acres"),
                            calls=calls,
                        )

                        # Project onto the real map if we found an
                        # anchor for this document -- otherwise this
                        # parcel stays local-only (flagged, not
                        # silently dropped).
                        if anchor_lat is not None and anchor_lon is not None:
                            parcel_result["boundary_geojson_wgs84"] = (
                                georeference_traverse_to_geojson(
                                    traverse, anchor_lat, anchor_lon
                                )
                            )
                        else:
                            parcel_result["georeference_error"] = (
                                "no geocodable address found in this document's OCR text"
                            )
                    else:
                        # A distinct, confirmed case: vision found this
                        # parcel (it has its own label/legal description
                        # in the notes) but couldn't confidently
                        # attribute any dimensions to it specifically --
                        # e.g. its notes only ever describe it in prose,
                        # with no bearing/distance sitting near its own
                        # label. This is NOT the same as ordinary
                        # extraction noise (a bad/self-intersecting
                        # traverse); there's no traverse attempt at all,
                        # so it must be surfaced distinctly rather than
                        # left indistinguishable from other empty
                        # failures in whatever consumes this result.
                        parcel_result["extraction_note"] = (
                            "This parcel was identified in the document (it has its own "
                            "label/description) but no boundary dimensions could be "
                            "confidently attributed to it specifically -- needs manual "
                            "review against the source document."
                        )

                    region["parcels"].append(parcel_result)

                # Cross-parcel check, run once per region across all of
                # its parcels together: a parcel that used a combined/
                # gross tract dimension instead of its own individual
                # segment can still close perfectly (see
                # spatial_validation.check_combined_tract_dimension's
                # docstring for the real confirmed case), so this uses
                # each parcel's independently-known stated acreage as
                # separate evidence closure can't provide. Never
                # auto-corrects -- only appends an explicit warning and
                # flags the parcel invalid if it wasn't already.
                combined_warnings = check_combined_tract_dimension(
                    [
                        {
                            "parcel_label": p["vision_geometry"].get("parcel_label"),
                            "area_sqft": p.get("spatial_validation", {}).get("area_sqft"),
                            "stated_area_sqft": p.get("spatial_validation", {}).get(
                                "stated_area_sqft"
                            ),
                        }
                        for p in region["parcels"]
                    ]
                )
                for parcel_result, warning in zip(region["parcels"], combined_warnings):
                    if warning and "spatial_validation" in parcel_result:
                        parcel_result["spatial_validation"]["issues"].append(warning)
                        parcel_result["spatial_validation"]["valid"] = False
        except Exception as exc:
            # Vision is an enhancement on top of OCR text, not a hard
            # requirement (e.g. GEMINI_API_KEY not set yet) -- degrade
            # gracefully rather than failing the request.
            for entry, region in vision_targets:
                region["vision_error"] = str(exc)

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
