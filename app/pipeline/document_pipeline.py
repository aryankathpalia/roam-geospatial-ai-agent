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
import logging
import math
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from PIL import Image

from app.services.geocoding import geocode_anchor
from app.services.georeference import (
    ANCHOR_APN,
    ANCHOR_CITY,
    ANCHOR_SURVEYED,
    classify_anchor_query,
    find_anchor_candidates,
    find_explicit_coordinates,
    find_surveyed_coordinates,
    georeference_traverse_to_geojson,
)
from app.services.plss import resolve_plss_anchor
from app.services.apn import COARSE_ANCHOR_RADIUS_M, resolve_apn_site
from app.services import location_evidence
from app.services.geometry import (
    align_to_shared_edge,
    assemble_traverse,
    borrow_sibling_call,
    drop_conflicting_axis_duplicates,
    find_shared_edge,
    merge_curve_calls,
    resolve_ambiguous_calls,
    traverse_to_geojson,
    walk_traverse,
)
from app.services.layout_detector_onnx import detect_page_layout
from app.services.legal_description import parse_legal_descriptions
from app.services.spatial_validation import (
    check_combined_tract_dimension,
    find_containing_acreage_sqft,
    parse_stated_area_acres,
    validate_traverse,
)
from app.services.ocr import OCRLine
from app.services.pdf_inspector import inspect_pdf
from app.services import progress as progress_tracker
from app.services.pdf_renderer import render_page
from app.services.region_cropper import extract_region_crops, padded_crop_box
from app.services import parcel_roster
from app.services.vision import (
    ESCALATION_MODEL,
    classify_regions,
    classify_sheets,
    extract_parcel_geometries_batch,
    read_parcel_roster,
)
from app.pipeline.page_ocr import (
    band_count_for,
    match_lines_to_regions,
    ocr_band,
    page_needs_ocr,
    split_page_into_bands,
    VISION_CLASSES,
)

DOCUMENT_ROOT = Path("data/documents")

logger = logging.getLogger(__name__)

# printed state-plane coordinates replace a county-parcel (APN) anchor only within this distance of it
_SURVEYED_AGREES_WITH_APN_M = 300.0

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
    document_id: str,
    ocr_dispatcher: OcrDispatcher | None = None,
    on_checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if ocr_dispatcher is None:
        ocr_dispatcher = DEFAULT_OCR_DISPATCHER

    document_dir = DOCUMENT_ROOT / document_id
    pdf_path = document_dir / "original.pdf"

    if not pdf_path.exists():
        raise FileNotFoundError(f"Document not found: {document_id}")

    progress_tracker.start(document_id)

    inspection = inspect_pdf(str(pdf_path))
    pages_dir = document_dir / "pages"
    review_crops_dir = document_dir / "review_crops"
    progress_tracker.update(
        document_id, "rendering", f"{inspection['page_count']} pages"
    )

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

    total_regions = sum(len(e["regions"]) for e in page_entries)
    parcelmap_regions = sum(
        1 for e in page_entries for r in e["regions"] if r["class"] == "ParcelMap"
    )
    progress_tracker.update(
        document_id,
        "layout_detection",
        f"{total_regions} regions detected, {parcelmap_regions} parcel map(s)",
    )

    # ---------------------------------------------------------
    # Candidate triage + early checkpoint. The boundary the user confirms is
    # an INPUT to everything below, so it is asked for as soon as ROAM knows
    # which regions are genuine parcel maps -- one batched Gemini call over
    # the ParcelMap crops (see run_triage_stage) -- not after OCR, geocoding
    # and vision extraction have all finished. Those stages keep running in
    # this same task while the user draws; process_document's caller joins
    # the confirmed polygon with their results (see
    # app/routes/documents.py::_finalize_and_bind).
    # ---------------------------------------------------------

    progress_tracker.update(
        document_id, "triage", f"judging the role of {parcelmap_regions} map region(s) by sheet" if parcelmap_regions else "no parcel map regions"
    )
    await run_triage_stage(page_entries)
    if on_checkpoint is not None:
        on_checkpoint(
            {
                "document_id": document_id,
                "inspection": inspection,
                "pages": [
                    {"page_number": e["page_number"], "regions": e["regions"], "sheet": e.get("sheet")}
                    for e in page_entries
                ],
                "pages_needing_review": 0,
                "anchor_lat": None,
                "anchor_lon": None,
                "anchor": None,
                "processing": {"complete": False},
            }
        )
    candidates = sum(
        1 for e in page_entries if e.get("sheet") and e["sheet"]["role"] in (None, "target_parcel_map")
    )
    progress_tracker.update(
        document_id,
        "candidates_ready",
        f"{candidates} parcel map sheet(s) ready for boundary confirmation" if candidates else "no parcel map candidates found",
    )

    # ---------------------------------------------------------
    # Parcel roster. The review screen is already open (it opened at candidates_ready); this
    # reads WHICH parcels each target sheet shows -- one small request, one image per sheet,
    # no tiling -- so the user can pick and outline "Parcel 1" or "Remainder Parcel" by name
    # while OCR and the tiled extraction run below. Saved as a second checkpoint.
    # ---------------------------------------------------------
    if candidates:
        progress_tracker.update(document_id, "roster", f"reading parcel labels on {candidates} sheet(s)")
    await run_roster_stage(page_entries)
    if on_checkpoint is not None and candidates:
        on_checkpoint(
            {
                "document_id": document_id,
                "inspection": inspection,
                "pages": [
                    {"page_number": e["page_number"], "regions": e["regions"], "sheet": e.get("sheet")}
                    for e in page_entries
                ],
                "pages_needing_review": 0,
                "anchor_lat": None,
                "anchor_lon": None,
                "anchor": None,
                "processing": {"complete": False},
            }
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

    progress_tracker.update(
        document_id, "ocr", f"{len(band_jobs)} regions read" if band_jobs else "no text regions to read"
    )

    # ---------------------------------------------------------
    # Find a real-world anchor for georeferencing (see
    # app/services/georeference.py for why this -- not a state-plane
    # projection -- is the approach).
    #
    # First choice: literal "Lat X Long Y" coordinates printed on the
    # drawing itself (some documents -- e.g. utility easement exhibits
    # labeling pole locations -- state these directly). Exact, no
    # address-matching ambiguity, no geocoder round-trip at all, so
    # this is checked before any address-based candidate.
    #
    # Fallback: scan this document's own OCR'd text for the most
    # address-like line, geocode it via the existing Nominatim
    # geocoder. None if nothing plausible is found -- traverses stay
    # local-only, not guessed.
    # ---------------------------------------------------------

    anchor_lat: float | None = None
    anchor_lon: float | None = None
    anchor: dict | None = None
    anchor_state: str | None = None  # the state the geocode landed in, for the county parcel lookup
    progress_tracker.update(document_id, "georeferencing", "searching document text for a location")

    # What the sheets' drawings say about their location (read with the roster), each number checked
    # against this document's OCR text -- see app/services/location_evidence.py. It only ADDS candidates
    # and role labels to the deterministic readers below; each is still checked against independent data.
    doc_text = "\n".join(r.get("ocr_text") or "" for e in page_entries for r in e["regions"])
    evidence = location_evidence.merge([
        location_evidence.verify(e["sheet"]["location_evidence"], doc_text)
        for e in page_entries if (e.get("sheet") or {}).get("location_evidence")
    ])
    regions_for_anchor = [{"regions": e["regions"]} for e in page_entries]
    canonical_plss = location_evidence.plss_text(evidence)
    if canonical_plss:
        # the drawing's PLSS description in the wording plss.py parses, AFTER the OCR text (a fallback
        # only: e.g. a rotated title block OCR cannot read)
        regions_for_anchor = regions_for_anchor + [{"regions": [{"class": "Text", "ocr_text": canonical_plss}]}]

    explicit_coords = find_explicit_coordinates(
        [{"regions": e["regions"]} for e in page_entries]
    )
    if explicit_coords:
        anchor_lat, anchor_lon = explicit_coords
        anchor = {"precision": ANCHOR_SURVEYED, "source": "coordinates printed on the document"}
        progress_tracker.update(
            document_id,
            "georeferencing",
            f"anchored to coordinates printed on the document ({anchor_lat:.5f}, {anchor_lon:.5f})",
        )
    else:
        evidence_queries = location_evidence.geocode_queries(evidence)
        anchor_candidates = list(dict.fromkeys(
            evidence_queries[:1]  # the subject's printed street address, verified in the OCR text
            + find_anchor_candidates([{"regions": e["regions"]} for e in page_entries], limit=6)
            + evidence_queries[1:]  # its county and state: coarse, but names the state
        ))
        geocoded, matched_query = await geocode_anchor(anchor_candidates)
        if geocoded:
            anchor_lat = geocoded[0].latitude
            anchor_lon = geocoded[0].longitude
            anchor = {
                "precision": classify_anchor_query(matched_query),
                "source": f'geocoded "{matched_query}"',
            }
            # A geocoded address is only ever approximate. If this
            # document also prints a state-plane corner coordinate (a
            # real surveyed tie point), try converting it now that the
            # geocode tells us which STATE to look in -- see
            # find_surveyed_coordinates' docstring for why this can only
            # make the anchor more precise, never wrong in a new way.
            anchor_state = geocoded[0].region or (evidence or {}).get("state")
            if geocoded[0].region:
                surveyed = find_surveyed_coordinates(
                    [{"regions": e["regions"]} for e in page_entries],
                    geocoded[0].region,
                    anchor_lat,
                    anchor_lon,
                    parcel_pairs=location_evidence.parcel_coordinate_pairs(evidence),
                    ground_to_grid=location_evidence.ground_to_grid(evidence),
                )
                if surveyed:
                    anchor_lat, anchor_lon = surveyed
                    anchor = {
                        "precision": ANCHOR_SURVEYED,
                        "source": "state-plane coordinate printed on the document",
                    }
                else:
                    # Still only a geocoded address -- no printed lat/long,
                    # no printed state-plane tie. Last precise option: a US
                    # PLSS (Public Land Survey System) section-corner
                    # monument the document names relative to a township/
                    # range/meridian ("...BEING THE 1/4 CORNER OF SECTIONS
                    # 18 AND 17, TOWNSHIP 22 NORTH, RANGE 21 EAST, M.D.M.").
                    # Resolved against BLM's own public PLSS data, never
                    # guessed -- see plss.py's docstring for the geometry
                    # and the many ways it refuses rather than guesses.
                    # Gated the same way state-plane is: only engages once
                    # a coarser geocode already identified the state, and
                    # only replaces the geocode, never something already
                    # better (the explicit-coordinate branch above, or the
                    # state-plane branch just checked).
                    try:
                        plss_anchor = resolve_plss_anchor(regions_for_anchor, geocoded[0].region)
                    except Exception:  # noqa: BLE001 -- a network/service hiccup; keep the geocode
                        plss_anchor = None
                    if plss_anchor:
                        anchor_lat, anchor_lon = plss_anchor.lat, plss_anchor.lon
                        anchor = {
                            # Full-confidence only once a SECOND monument on
                            # the sheet independently resolves and agrees --
                            # same single-source/cross-validated discipline
                            # every other evidence type in this pipeline
                            # already uses (OCR, Gemini, calibration).
                            "precision": ANCHOR_SURVEYED if plss_anchor.corroborated else "plss_single_source",
                            "source": f"PLSS monument printed on the document ({plss_anchor.description})",
                            "notes": plss_anchor.notes,
                            "aliquot": plss_anchor.aliquot,
                        }
            progress_tracker.update(
                document_id, "georeferencing", f"anchored near {geocoded[0].name}"
            )
        else:
            progress_tracker.update(
                document_id, "georeferencing", "no geocodable location found in document text"
            )

    # The APNs the document prints (its own and its neighbours'), looked up in the county's public parcel
    # layer: real parcel polygons on the ground, used after confirmation to seat the outlines between their
    # neighbours (app/routes/documents.py::_fit_sheet_to_apn). Evidence only -- the anchor is unchanged.
    apn_site = None
    if anchor_lat is not None and anchor_lon is not None:
        try:
            coarse = (anchor or {}).get("precision") == ANCHOR_CITY
            site = resolve_apn_site(
                doc_text, anchor_state, anchor_lat, anchor_lon,
                subject=location_evidence.subject_apns(evidence),
                extra=[a["apn"] for a in (evidence or {}).get("apns", [])],
                **({"radius_m": COARSE_ANCHOR_RADIUS_M} if coarse else {}),
            )
            apn_site = site.to_dict() if site else None
        except Exception:  # noqa: BLE001 -- a county service hiccup must not fail the document
            site, apn_site = None, None
        if apn_site:
            progress_tracker.update(
                document_id, "georeferencing",
                f"found {len(apn_site['neighbours']) + (1 if apn_site['target'] else 0)} printed APN(s) in county parcel records",
            )
            # A county / city centroid is no anchor for a parcel (Washoe's is ~110 km from Reno): the
            # parcels the plat names ARE on the ground, so the site's centre replaces it -- and the printed
            # state-plane coordinates, rejected as too far from the centroid, get another chance.
            centre = site.centre
            if coarse and centre:
                anchor_lon, anchor_lat = centre
                own = apn_site["target_apn"] if apn_site["target"] else None
                anchor = {
                    "precision": ANCHOR_APN,
                    "source": (
                        f"county parcel record for APN {own}" if own
                        else f"between {len(apn_site['neighbours'])} neighbouring parcels named by APN"
                    ) + f" ({apn_site['source']})",
                }
                if anchor_state:
                    surveyed = find_surveyed_coordinates(
                        [{"regions": e["regions"]} for e in page_entries], anchor_state, anchor_lat, anchor_lon,
                        parcel_pairs=location_evidence.parcel_coordinate_pairs(evidence),
                        ground_to_grid=location_evidence.ground_to_grid(evidence),
                    )
                    # only when the two agree: a printed control monument may sit far from the parcel
                    if surveyed and math.hypot(
                        (surveyed[0] - centre[1]) * 110_540,
                        (surveyed[1] - centre[0]) * 111_320 * math.cos(math.radians(centre[1])),
                    ) <= _SURVEYED_AGREES_WITH_APN_M:
                        anchor_lat, anchor_lon = surveyed
                        anchor = {
                            "precision": ANCHOR_SURVEYED,
                            "source": "state-plane coordinate printed on the document (agrees with county parcel records)",
                        }
                progress_tracker.update(document_id, "georeferencing", f"anchored by {anchor['source']}")

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

    needs_vision_count = sum(
        1 for e in page_entries for r in e["regions"] if r.get("needs_vision")
    )
    progress_tracker.update(
        document_id,
        "vision_extraction",
        f"reading {needs_vision_count} parcel map region(s)" if needs_vision_count else "no parcel map regions to read",
    )

    await run_vision_stage(page_entries, anchor_lat, anchor_lon)
    apply_legal_descriptions(page_entries, anchor_lat, anchor_lon)
    flag_spurious_duplicate_parcelmap_regions(page_entries)
    join_roster_evidence(page_entries)

    pages_result = [
        {"page_number": e["page_number"], "regions": e["regions"], "sheet": e.get("sheet")}
        for e in page_entries
    ]

    pages_needing_review = sum(
        1 for page in pages_result if any(r["needs_review"] for r in page["regions"])
    )

    valid_parcels = sum(
        1
        for page in pages_result
        for region in page["regions"]
        for parcel in region.get("parcels", [])
        if (parcel.get("spatial_validation") or {}).get("valid")
    )
    total_parcels = sum(
        len(region.get("parcels", []))
        for page in pages_result
        for region in page["regions"]
    )
    progress_tracker.update(
        document_id,
        "done",
        f"{valid_parcels} of {total_parcels} parcel(s) closed" if total_parcels else "no parcels extracted",
    )

    return {
        "document_id": document_id,
        "inspection": inspection,
        "pages": pages_result,
        "pages_needing_review": pages_needing_review,
        "anchor_lat": anchor_lat,
        "anchor_lon": anchor_lon,
        "anchor": anchor,
        "apn_site": apn_site,
        "location_evidence": evidence,
        "processing": {"complete": True},
    }


def flag_spurious_duplicate_parcelmap_regions(page_entries: list[dict]) -> None:
    """
    A layout region classified ParcelMap can be a genuine detailed
    survey/plat drawing OR a small locus/vicinity-map inset that
    happens to share the same visual features (lines, labels, a north
    arrow) and gets the same class -- confirmed on a real document
    where a certification page's inset locus map got its own,
    much-smaller ParcelMap region, and vision attached a real
    parcel_label to it that duplicated a parcel already correctly
    read from the actual detailed drawing elsewhere in the document.

    This is a different failure than the same-region IoU dedup in
    layout_detector_onnx.py's _deduplicate -- these are two genuinely
    separate, non-overlapping regions that happen to collide on the
    same label text, not one region detected twice.

    When the same parcel_label shows up in ParcelMap regions of very
    different area on the same document, flag the parcel from the
    much smaller region as a likely-spurious duplicate -- flagged,
    not silently dropped, consistent with this pipeline's convention
    elsewhere (assembly_notes, georeference_error, etc.) of surfacing
    an automated judgment call rather than hiding it.
    """

    by_label: dict[str, list[tuple[float, dict]]] = {}
    for entry in page_entries:
        for region in entry["regions"]:
            if region.get("class") != "ParcelMap":
                continue
            bbox = region.get("bbox")
            area = bbox[2] * bbox[3] if bbox else 0
            for parcel in region.get("parcels") or []:
                label = (parcel.get("vision_geometry", {}).get("parcel_label") or "").strip().upper()
                if not label:
                    continue
                by_label.setdefault(label, []).append((area, parcel))

    for occurrences in by_label.values():
        if len(occurrences) < 2:
            continue
        occurrences.sort(key=lambda t: -t[0])
        largest_area = occurrences[0][0]
        for area, parcel in occurrences[1:]:
            # Only flag a LOPSIDED size mismatch -- two legitimately
            # similar-sized ParcelMap regions sharing a label (e.g. a
            # real cross-page duplicate) should stay a normal, visible
            # review item, not get silently downgraded here.
            if largest_area > 0 and area < largest_area * SHEET_INSET_AREA_FRACTION:
                parcel["likely_duplicate_region"] = True
                parcel["duplicate_note"] = (
                    f"Same parcel_label also found in a ~{largest_area / area:.0f}x larger "
                    "ParcelMap region on this document -- this is likely a vicinity/locus "
                    "map inset misclassified as a real boundary drawing, not a genuine "
                    "second parcel."
                )


def walk_region_parcels(
    parcels: list[dict],
    region_ocr_text: str = "",
    anchor_lat: float | None = None,
    anchor_lon: float | None = None,
) -> list[dict]:
    """
    Deterministic post-extraction stage for one region: resolves,
    walks, validates and (if anchored) georeferences each parcel vision
    returned. Split out of process_document so the regression harness
    can replay stored vision output through the exact production code
    without calling Gemini.
    """

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
    parcel_results: list[dict] = []
    stated_sqfts = [
        parse_stated_area_acres(g.get("stated_area_acres")) for g in parcels
    ]
    # Cross-check/override vision's own per-parcel acreage field against
    # a "CONTAINING X Acres" legal-description phrase, when the region
    # has exactly one parcel AND exactly one such phrase (both required
    # -- see find_containing_acreage_sqft's docstring on why an
    # ambiguous multi-match region falls back to vision's field
    # unchanged, same as before). Confirmed on a real document: vision
    # extracted an unrelated, much larger acreage for a 1.03-acre
    # easement (confused by a nearby "parent tract...recorded in Book
    # 834" reference in the same paragraph); the CONTAINING phrase
    # correctly isolated the parcel's own stated 1.03 acres, and once
    # that was corrected, assemble_traverse (already the automatic
    # drop/reorder search for every parcel) closed the traverse and
    # dropped the one bad call with zero manual editing.
    if len(parcels) == 1:
        containing_sqft = find_containing_acreage_sqft(region_ocr_text)
        if containing_sqft:
            stated_sqfts[0] = containing_sqft
            # Overwrite vision's own field too, not just the local
            # stated_sqfts array -- validate_traverse and sibling-
            # borrowing both re-read parcels[0]["stated_area_acres"]
            # directly later in this function, so leaving it
            # unchanged would silently un-fix the override for those.
            parcels[0]["stated_area_acres"] = str(round(containing_sqft / 43_560.0, 3))

    # validate_traverse falls back to scanning region_ocr_text for the
    # first bare "<number> acres/sqft" figure when vision's own
    # per-parcel field is empty. That fallback is only safe for a
    # single-parcel region -- a multi-parcel "Parcel Map Exhibit" sheet
    # has one stated area PER parcel (confirmed on a real 2-parcel
    # subdivision plat: both parcels got compared against the same
    # mis-scanned figure -- an OCR-garbled "335 AC" that should have
    # read "3.35 AC", which is the TOTAL of both parcels combined, not
    # either one's own area -- producing a false ~99% area mismatch on
    # both). Passing "" instead of the real text makes the fallback
    # return None (skip the check) rather than guess wrong.
    area_check_text = region_ocr_text if len(parcels) == 1 else ""

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
            # Curve calls are merged in AFTER the resolvers above --
            # none of them understand call_type "curve" (they only
            # match on bearing axis), so a curve call must stay out of
            # their input and only join the walking order right before
            # walk_traverse actually needs it.
            calls = merge_curve_calls(calls, geometry.get("curve_calls") or [])
            calls, assembly_notes = assemble_traverse(calls, stated_sqfts[idx])
            if assembly_notes:
                parcel_result["assembly_notes"] = assembly_notes
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
                area_check_text,
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

        parcel_results.append(parcel_result)

    # Sibling-boundary borrowing: a SECOND pass, only possible once every
    # parcel in the region has its own resolved_boundary_calls (this is
    # why it's a separate loop, not folded into the one above). Track B
    # (resolve_ambiguous_calls, above) only resolves AMBIGUITY -- it
    # picks between candidate readings vision already offered for one of
    # THIS parcel's own calls. It has no mechanism for a side that's
    # missing outright, which is the common case on an N-lot subdivision
    # plat: a shared line between two lots is labeled once, near
    # whichever lot's label the drafter put it closest to, and vision's
    # own extraction rule never assigns it to the other lot at all. This
    # generalizes past Track B's original 2-parcel case by searching
    # SIBLING parcels' own already-resolved calls for the missing side,
    # nearest sibling first (index distance in the extraction order is
    # the only adjacency proxy available without real coordinates), and
    # keeping a borrow only under the same acreage-match discipline
    # Track B already uses (see borrow_sibling_call's docstring).
    for idx, parcel_result in enumerate(parcel_results):
        calls = parcel_result.get("resolved_boundary_calls")
        if not calls or parcel_result.get("spatial_validation", {}).get("valid"):
            continue
        own_sqft = stated_sqfts[idx]
        if not own_sqft:
            continue
        siblings = sorted(
            (
                (j, parcel_results[j]["vision_geometry"].get("parcel_label"))
                for j in range(len(parcel_results))
                if j != idx and parcel_results[j].get("resolved_boundary_calls")
            ),
            key=lambda pair: abs(pair[0] - idx),
        )
        sibling_calls = [
            (label, parcel_results[j]["resolved_boundary_calls"]) for j, label in siblings
        ]
        sibling_sqfts = [stated_sqfts[j] for j, _ in siblings if stated_sqfts[j]]
        disqualifying_areas = list(sibling_sqfts)
        disqualifying_areas.extend(own_sqft + s for s in sibling_sqfts)
        if sibling_sqfts:
            disqualifying_areas.append(own_sqft + sum(sibling_sqfts))

        borrowed_calls, note = borrow_sibling_call(calls, own_sqft, sibling_calls, disqualifying_areas)
        if note is None:
            continue

        traverse = walk_traverse(borrowed_calls)
        parcel_result["resolved_boundary_calls"] = borrowed_calls
        parcel_result["boundary_geojson"] = traverse_to_geojson(traverse)
        parcel_result["spatial_validation"] = validate_traverse(
            traverse,
            area_check_text,
            stated_area_acres=parcel_result["vision_geometry"].get("stated_area_acres"),
            calls=borrowed_calls,
        )
        parcel_result.setdefault("assembly_notes", []).append(note)
        if anchor_lat is not None and anchor_lon is not None:
            parcel_result["boundary_geojson_wgs84"] = georeference_traverse_to_geojson(
                traverse, anchor_lat, anchor_lon
            )

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
            for p in parcel_results
        ]
    )
    for parcel_result, warning in zip(parcel_results, combined_warnings):
        if warning and "spatial_validation" in parcel_result:
            parcel_result["spatial_validation"]["issues"].append(warning)
            parcel_result["spatial_validation"]["valid"] = False

    # Shared-edge alignment: siblings drawn on the SAME sheet that
    # describe a literal common boundary line (printed once, each read
    # independently from its own side -- same length, reversed bearing)
    # must be placed so that line actually coincides on the map, not each
    # independently re-centered on the document's single anchor point.
    # Confirmed real bug: two correctly-shaped, correctly-closing
    # siblings on the Patnaude packet ("Remainder Parcel" and "Parcel 1")
    # landed with a visible gap between them and no shared edge, because
    # each is walked from its own point of beginning and (see
    # georeference_traverse_to_geojson) georeferenced as if THAT were the
    # anchor. Only ever TRANSLATES a later sibling onto the first one
    # that already has a usable traverse -- every parcel's own shape,
    # scale and rotation are untouched; a region with no literal shared
    # edge in its data (most of them) is untouched entirely.
    anchor_idx = next(
        (i for i, p in enumerate(parcel_results) if p.get("resolved_boundary_calls")), None
    )
    if anchor_idx is not None and anchor_lat is not None and anchor_lon is not None:
        anchor_points = walk_traverse(parcel_results[anchor_idx]["resolved_boundary_calls"]).points
        for i, parcel_result in enumerate(parcel_results):
            if i == anchor_idx:
                continue
            calls = parcel_result.get("resolved_boundary_calls")
            if not calls:
                continue
            own_traverse = walk_traverse(calls)
            match = find_shared_edge(anchor_points, own_traverse.points)
            if match is None:
                continue
            seg_a, seg_b = match
            own_traverse.points = align_to_shared_edge(anchor_points, own_traverse.points, seg_a, seg_b)
            parcel_result["boundary_geojson_wgs84"] = georeference_traverse_to_geojson(
                own_traverse, anchor_lat, anchor_lon
            )
            anchor_label = parcel_results[anchor_idx]["vision_geometry"].get("parcel_label") or "a sibling parcel"
            parcel_result.setdefault("assembly_notes", []).append(
                f"placement aligned to share a common boundary with {anchor_label}"
            )

    return parcel_results


_LEGAL_ACREAGE_MATCH = 0.05


def apply_legal_descriptions(
    page_entries: list[dict[str, Any]],
    anchor_lat: float | None = None,
    anchor_lon: float | None = None,
) -> None:
    """
    Parses written metes-and-bounds descriptions out of each page's OCR
    text (see app/services/legal_description.py) and uses any that
    close as the authoritative boundary. A vision parcel whose stated
    acreage matches the description's and isn't already valid gets its
    calls replaced; a description with no matching parcel is added as
    its own parcel, since vision reading the drawing can miss or merge
    parcels that the prose states unambiguously.
    """

    all_parcels = [
        p
        for e in page_entries
        for r in e["regions"]
        for p in r.get("parcels", [])
    ]

    for entry in page_entries:
        text = "\n".join((r.get("ocr_text") or "") for r in entry["regions"])
        for desc in parse_legal_descriptions(text):
            acres = desc["stated_area_acres"]
            target = None
            if acres:
                wanted = float(acres)
                for parcel in all_parcels:
                    stated = parcel["vision_geometry"].get("stated_area_acres")
                    try:
                        stated_f = float(stated) if stated else None
                    except ValueError:
                        stated_f = None
                    if stated_f and abs(stated_f - wanted) / wanted <= _LEGAL_ACREAGE_MATCH:
                        target = parcel
                        break
            if target and (target.get("spatial_validation") or {}).get("valid"):
                continue

            updated = recompute_parcel_from_calls(
                desc["boundary_calls"],
                stated_area_acres=acres,
                anchor_lat=anchor_lat,
                anchor_lon=anchor_lon,
            )
            note = (
                f"Boundary taken from the written legal description on page "
                f"{entry['page_number']} -- it closes and is read deterministically, "
                "so it replaces the drawing-based reading."
            )
            updated["assembly_notes"] = [note]

            if target:
                for key in (
                    "extraction_note", "georeference_error", "boundary_geojson",
                    "boundary_geojson_wgs84", "spatial_validation", "assembly_notes",
                ):
                    target.pop(key, None)
                target.update(updated)
                continue

            region = next(
                (r for r in entry["regions"] if r["class"] == "ParcelMap"),
                max(entry["regions"], key=lambda r: len(r.get("ocr_text") or "")),
            )
            new_parcel = {
                "vision_geometry": {
                    "parcel_label": f"Legal description (page {entry['page_number']})",
                    "boundary_calls": desc["boundary_calls"],
                    "stated_area_acres": acres,
                },
                **updated,
            }
            region.setdefault("parcels", []).append(new_parcel)
            all_parcels.append(new_parcel)


def recompute_parcel_from_calls(
    calls: list[dict],
    stated_area_acres: str | None = None,
    region_ocr_text: str = "",
    anchor_lat: float | None = None,
    anchor_lon: float | None = None,
    auto_fix: bool = False,
) -> dict:
    """
    Recompute geometry + validation for a human-edited boundary_calls
    list. Used by the review UI's editable call table: a reviewer edits
    a bearing/distance, adds a missing call, or removes a bad one, and
    this returns the updated polygon/closure/validity immediately.

    By default, deliberately skips the resolver/assembly stage
    (resolve_ambiguous_calls, drop_conflicting_axis_duplicates,
    assemble_traverse) -- those exist to make sense of raw, unreviewed
    vision output, and re-running them on calls a human already edited
    would silently reorder or drop what the reviewer just typed. Curve
    calls are also expected pre-merged (the caller passes whatever mix
    of line/curve dicts it wants walked, in the exact order to walk
    them).

    auto_fix=True is the one deliberate exception: it runs the edited
    calls through assemble_traverse before walking -- the same bounded,
    deterministic reversal/reorder/drop search already proven this
    session (0%->3% Valid on real plats), scored against closure AND
    the parcel's own stated acreage, never against vision's plausibility
    alone. This is the "auto-fix" button in the review UI: instead of a
    human guessing which call's direction is flipped by trial and
    error, or reaching for a fresh LLM call with no way to verify its
    guess, it runs the same explainable search that already works,
    surfaces exactly what it changed via assembly_notes, and reports
    plainly when it can't find a closing configuration rather than
    guessing one.
    """

    assembly_notes: list[str] = []
    if auto_fix and calls:
        stated_area_sqft = parse_stated_area_acres(stated_area_acres)
        calls, assembly_notes = assemble_traverse(calls, stated_area_sqft)

    parcel_result: dict = {"resolved_boundary_calls": calls}
    if assembly_notes:
        parcel_result["assembly_notes"] = assembly_notes
    if not calls:
        parcel_result["extraction_note"] = "No boundary calls to walk."
        return parcel_result

    traverse = walk_traverse(calls)
    parcel_result["boundary_geojson"] = traverse_to_geojson(traverse)
    parcel_result["spatial_validation"] = validate_traverse(
        traverse,
        region_ocr_text,
        stated_area_acres=stated_area_acres,
        calls=calls,
    )
    if auto_fix and not assembly_notes and not parcel_result["spatial_validation"]["valid"]:
        parcel_result["assembly_notes"] = [
            "Auto-fix couldn't find a call reversal, reorder or drop that "
            "closes this traverse and matches the stated acreage -- needs "
            "manual correction."
        ]
    if anchor_lat is not None and anchor_lon is not None:
        parcel_result["boundary_geojson_wgs84"] = georeference_traverse_to_geojson(
            traverse, anchor_lat, anchor_lon
        )
    else:
        parcel_result["georeference_error"] = (
            "no geocodable address found in this document's OCR text"
        )
    return parcel_result


async def _classify_into_regions(targets: list[tuple[dict, dict]], crops: list[Image.Image]) -> None:
    """
    classify_regions is a DISPLAY LABEL / ordering hint only, never a gate --
    measured on the regression corpus, it drops 9 real-plat region-runs out of
    23 (one page missed in all 3 runs), which fails the bar for silently
    skipping extraction. Every ParcelMap region still goes to extraction
    regardless of what this returns; the review UI uses region["category"] to
    separate likely target sheets from other maps, with the rest always one
    click away, never hidden.
    """

    loop = asyncio.get_running_loop()
    try:
        categories = await loop.run_in_executor(None, classify_regions, crops)
        for (entry, region), category in zip(targets, categories):
            region["category"] = category
    except Exception as exc:  # noqa: BLE001 -- display-only, never fatal
        logger.warning("Region classification failed, leaving uncategorized: %s", exc)


# A ParcelMap-class region smaller than this fraction of the page's largest one is an
# INSET of that sheet (vicinity map, detail, key map), not a map of its own. Same
# threshold flag_spurious_duplicate_parcelmap_regions uses for duplicate insets.
SHEET_INSET_AREA_FRACTION = 0.2

# What the sheet-level role means for the older per-region `category` label, which the
# vision stage's duplicate suppression and the workspace's "show all regions" filter
# still read: a target or reference survey is a plat; everything else is not.
_ROLE_TO_CATEGORY = {
    "target_parcel_map": "boundary_plat",
    "reference_survey": "boundary_plat",
    "other_parcel_drawing": "undimensioned_drawing",
    "location_or_aerial_map": "not_a_parcel_drawing",
    "not_a_map": "not_a_parcel_drawing",
}


def build_sheets(page_entries: list[dict[str, Any]]) -> None:
    """
    REGION -> SHEET. A sheet is a page that holds ParcelMap-class regions; it is ONE
    candidate however many such regions the layout model found on it. Its drawing
    region is the largest one; others at least SHEET_INSET_AREA_FRACTION of that size
    are sibling drawings of the same sheet (parcels on them nest under the sheet);
    smaller ones are insets and are folded in. Written to entry["sheet"]:
    {main_region, regions, inset_regions, role, reason} -- role/reason are filled in by
    the sheet-level triage.
    """

    for entry in page_entries:
        entry.pop("sheet", None)
        maps = [(i, r) for i, r in enumerate(entry["regions"]) if r["class"] in VISION_CLASSES]
        if not maps:
            continue
        area = lambda r: r["bbox"][2] * r["bbox"][3]  # noqa: E731
        main_i, main = max(maps, key=lambda ir: area(ir[1]))
        drawings = [i for i, r in maps if i == main_i or area(r) >= SHEET_INSET_AREA_FRACTION * area(main)]
        entry["sheet"] = {
            "main_region": main_i,
            "regions": sorted(drawings),
            "inset_regions": sorted(i for i, _ in maps if i not in drawings),
            "role": None,
            "reason": None,
        }


async def run_triage_stage(page_entries: list[dict[str, Any]]) -> None:
    """
    Sheet-level triage: group ParcelMap-class regions into sheets (build_sheets),
    then ask Gemini ONE question per sheet -- its ROLE in the packet (the packet's
    own parcel map, a cited reference survey, another drawing, a location/aerial map,
    not a map) -- over whole-page thumbnails in a single batched request, so the
    sheets are judged against each other. Needs only the rendered pages and layout
    boxes, so it runs before OCR. The role is stored on entry["sheet"] and mirrored
    to each region's legacy `category`; a failed call leaves the roles unset, which
    the UI treats as "candidate" -- never hidden.
    """

    build_sheets(page_entries)
    sheeted = [e for e in page_entries if e.get("sheet")]
    if not sheeted:
        return
    images = []
    for entry in sheeted:
        with Image.open(entry["path"]) as page_image:
            thumb = page_image.convert("RGB")
        thumb.thumbnail((1400, 1400))
        images.append(thumb)
    loop = asyncio.get_running_loop()
    try:
        answers = await loop.run_in_executor(None, classify_sheets, images)
    except Exception as exc:  # noqa: BLE001 -- ordering aid only, never fatal
        logger.warning("Sheet classification failed, leaving sheets unclassified: %s", exc)
        return
    for entry, answer in zip(sheeted, answers):
        sheet = entry["sheet"]
        sheet["role"], sheet["reason"] = answer["role"], answer["reason"]
        category = _ROLE_TO_CATEGORY.get(answer["role"])
        for i, region in enumerate(entry["regions"]):
            if region["class"] not in VISION_CLASSES:
                continue
            region["category"] = "not_a_parcel_drawing" if i in sheet["inset_regions"] else category


async def run_roster_stage(page_entries: list[dict[str, Any]]) -> None:
    """
    Read the parcels of every TARGET (or not-yet-judged) sheet into entry["sheet"]["parcels"],
    one image per sheet: its main drawing, cropped exactly as the review canvas is, so each
    parcel's `point` lines up with the canvas. Other sheets are not read here (their parcels
    appear when the extraction finishes). entry["sheet"]["roster"]["status"] is one of
    skipped | ready | empty | failed -- the UI offers a hand-named outline when nothing was read.
    """

    sheeted = [e for e in page_entries if e.get("sheet")]
    targets = [e for e in sheeted if e["sheet"]["role"] in (None, "target_parcel_map")]
    for entry in sheeted:
        entry["sheet"]["parcels"] = []
        entry["sheet"]["roster"] = {"status": "pending" if entry in targets else "skipped"}
    if not targets:
        return
    images = []
    for entry in targets:
        region = entry["regions"][entry["sheet"]["main_region"]]
        with Image.open(entry["path"]) as page_image:
            box = padded_crop_box(region["bbox"], *page_image.size, region["class"])
            images.append(page_image.convert("RGB").crop(tuple(int(round(v)) for v in box)))
    loop = asyncio.get_running_loop()
    try:
        answers = await loop.run_in_executor(None, read_parcel_roster, images)
    except Exception as exc:  # noqa: BLE001 -- the user can still name an outline by hand
        logger.warning("Parcel roster failed, leaving it empty: %s", exc)
        for entry in targets:
            entry["sheet"]["roster"] = {"status": "failed"}
        return
    locations = getattr(answers, "locations", None) or [None] * len(targets)
    for entry, parcels, location in zip(targets, answers, locations):
        sheet = entry["sheet"]
        if location:
            sheet["location_evidence"] = location  # unverified until OCR (location_evidence.verify)
        for item in parcels:
            sheet["parcels"].append(
                parcel_roster.new_entity(
                    entry["page_number"], sheet["parcels"], label=item["label"], region_index=sheet["main_region"],
                    source="roster", printed_id=item["printed_id"], stated_area=item["stated_area"], point=item["point"],
                    stated_area_sqft=item.get("stated_area_sqft"),
                )
            )
        sheet["roster"] = {"status": "ready" if sheet["parcels"] else "empty"}


def join_roster_evidence(page_entries: list[dict[str, Any]]) -> None:
    """
    EVIDENCE JOIN. Once OCR and the tiled extraction are done, attach the extracted parcels
    (calls, stated area) to the roster entities they match -- by label, then stated area -- and
    turn every extracted parcel nothing matched into an entity of its own. A printed id the
    sheet's own text does not contain is dropped first. A user's polygons are not touched here:
    they live on the entities and are carried over by id when the result is saved.
    """

    for entry in page_entries:
        sheet = entry.get("sheet")
        if not sheet:
            continue
        text = "\n".join(entry["regions"][i].get("ocr_text") or "" for i in sheet.get("regions", []))
        parcel_roster.verify_printed_ids(sheet.get("parcels", []), text)
        parcel_roster.join_evidence(entry["page_number"], sheet, entry["regions"])


async def run_vision_stage(
    page_entries: list[dict[str, Any]],
    anchor_lat: float | None = None,
    anchor_lon: float | None = None,
) -> None:
    """
    Triage, extract and walk every needs_vision region, writing results
    into the region dicts in place. Each entry needs "path" (the
    rendered page PNG) and "regions" (with bbox / needs_vision /
    ocr_text). Separate from process_document so the regression
    harness can re-run just this stage on stored OCR/layout output.
    """

    loop = asyncio.get_running_loop()
    vision_targets = [
        (entry, region)
        for entry in page_entries
        for region in entry["regions"]
        if region.get("needs_vision")
    ]

    crops = []
    if vision_targets:
        for entry, region in vision_targets:
            x, y, w, h = region["bbox"]
            with Image.open(entry["path"]) as page_image:
                crops.append(page_image.convert("RGB").crop((x, y, x + w, y + h)))

        # Triage normally already ran right after layout detection
        # (run_triage_stage); this only covers callers that start from stored
        # layout/OCR output (the regression harness) with no categories yet.
        if any(region.get("category") is None for _, region in vision_targets):
            await _classify_into_regions(vision_targets, crops)

    if vision_targets:
        try:
            regions_parcels = await loop.run_in_executor(
                None, extract_parcel_geometries_batch, crops
            )
            for (entry, region), parcels in zip(vision_targets, regions_parcels):
                if isinstance(parcels, Exception):
                    region["vision_error"] = str(parcels)
                    continue
                region["parcels"] = walk_region_parcels(
                    parcels, region.get("ocr_text") or "", anchor_lat, anchor_lon
                )

            # A later "boundary_plat" region whose parcels are ALL too
            # incomplete to form any polygon (fewer than 3 resolved
            # calls each -- spatial_validation.area_sqft stays null),
            # once an EARLIER boundary_plat region in this same document
            # already extracted at least one real polygon, is far more
            # likely to be a garbled re-read of the same drawing than a
            # genuine new lot -- confirmed on a real 2-lot subdivision
            # packet that reprints its own parcel exhibit on a later,
            # lower-quality-OCR sheet: the reprint's extraction found
            # only 1 call and mislabeled it with the pre-subdivision
            # parent APN, producing a spurious "3rd parcel" next to the
            # 2 real ones. Only suppressed once a real geometry has
            # already been seen, so the FIRST region to attempt
            # extraction is never hidden even if it's the one that fails.
            seen_real_geometry = False
            for entry, region in vision_targets:
                if region.get("category") != "boundary_plat":
                    continue
                parcels = region.get("parcels") or []
                region_has_real_geometry = any(
                    (p.get("spatial_validation") or {}).get("area_sqft") is not None
                    for p in parcels
                )
                if parcels and not region_has_real_geometry and seen_real_geometry:
                    region["duplicate_note"] = (
                        "Every parcel extracted here is too incomplete to form a "
                        "shape, and a real ParcelMap on an earlier page already "
                        "extracted successfully -- likely a garbled re-read of "
                        "the same drawing rather than a new lot; not shown."
                    )
                    region["parcels"] = []
                elif region_has_real_geometry:
                    seen_real_geometry = True

            # Lite-then-escalate: a region that didn't reach Valid on
            # the default (cheap, fast) model gets ONE retry on a
            # stronger model -- scoped per-region, not per-document, so
            # a document with 5 regions where only 1 failed only pays
            # the stronger model's cost/latency for that 1. Measured
            # this session: on 3 simple, human-legible plats that
            # consistently failed on the default model, the stronger
            # model reached Valid 5/5 runs on one of them (0/5 before)
            # -- a real fix, not a marginal one -- but only a
            # completeness gain (still didn't close) on the other two.
            # The stronger model is NOT swapped in as the default: its
            # free-tier quota is documented in app/core/config.py as
            # capped at 20 requests/DAY on this account (confirmed via
            # a real 429), so it can only be a rare, targeted retry.
            failed = [
                (entry, region, crop)
                for (entry, region), crop in zip(vision_targets, crops)
                if not any(
                    (p.get("spatial_validation") or {}).get("valid")
                    for p in region.get("parcels", [])
                )
            ]
            if failed:
                escalated_crops = [crop for _, _, crop in failed]
                try:
                    escalated_results = await loop.run_in_executor(
                        None,
                        extract_parcel_geometries_batch,
                        escalated_crops,
                        ESCALATION_MODEL,
                    )
                except Exception as exc:  # noqa: BLE001 -- keep the lite result
                    logger.warning("Escalation call failed, keeping lite result: %s", exc)
                    escalated_results = [exc] * len(failed)
                for (entry, region, _crop), parcels in zip(failed, escalated_results):
                    region["vision_escalated"] = True
                    if isinstance(parcels, Exception):
                        continue  # keep the lite result as-is -- see docstring above
                    escalated_parcels = walk_region_parcels(
                        parcels, region.get("ocr_text") or "", anchor_lat, anchor_lon
                    )
                    if any(
                        (p.get("spatial_validation") or {}).get("valid")
                        for p in escalated_parcels
                    ):
                        region["parcels"] = escalated_parcels
                        region["vision_escalated_won"] = True
        except Exception as exc:
            # Vision is an enhancement on top of OCR text, not a hard
            # requirement (e.g. GEMINI_API_KEY not set yet) -- degrade
            # gracefully rather than failing the request.
            for entry, region in vision_targets:
                region["vision_error"] = str(exc)
