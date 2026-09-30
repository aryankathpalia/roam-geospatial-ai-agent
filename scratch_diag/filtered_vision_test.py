"""
One-off diagnostic (not part of tests/regression, not committed to prod
code): render+layout+OCR each corpus doc same as process_document, but
send ONLY the hand-verified ground-truth "plat" regions (region_labels.json)
to vision -- no junk/decoy regions in the batch at all. Compares
completeness (3+ calls / closed / valid) against the CURRENT full-batch
baseline's plat-only numbers already recorded in replay_scorecard.json.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pipeline.document_pipeline import (
    DOCUMENT_ROOT,
    run_vision_stage,
    walk_region_parcels,
)
from app.services.layout_detector_onnx import detect_page_layout
from app.services.pdf_inspector import inspect_pdf
from app.services.pdf_renderer import render_page
from app.services.region_cropper import extract_region_crops
from app.pipeline.page_ocr import (
    band_count_for,
    match_lines_to_regions,
    page_needs_ocr,
    split_page_into_bands,
)
from app.pipeline.document_pipeline import _default_ocr_dispatcher
from PIL import Image

CORPUS_PATH = Path("tests/regression/corpus.json")
LABELS_PATH = Path("tests/regression/region_labels.json")


def iou(a, b):
    ax1, ay1, aw, ah = a
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx1, by1, bw, bh = b
    bx2, by2 = bx1 + bw, by1 + bh
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0


async def build_page_entries(doc_id: str):
    document_dir = DOCUMENT_ROOT / doc_id
    pdf_path = document_dir / "original.pdf"
    pages_dir = document_dir / "pages"

    inspection = inspect_pdf(str(pdf_path))
    page_entries = []
    for page_number in range(1, inspection["page_count"] + 1):
        output_path = pages_dir / f"page_{page_number:03d}.png"
        if not output_path.exists():
            render_page(pdf_path=str(pdf_path), page_number=page_number, output_path=str(output_path), dpi=200)
        detections = detect_page_layout(str(output_path))
        crops = extract_region_crops(str(output_path), detections, save_dir=None, save_only_needs_review=False)
        regions = [
            {
                "class": c.roam_class,
                "confidence": round(c.confidence, 3),
                "bbox": [round(v, 1) for v in c.bbox],
                "needs_review": c.needs_review,
            }
            for c in crops
        ]
        page_entries.append({"page_number": page_number, "path": output_path, "regions": regions})

    band_jobs = []
    band_owner = []
    for entry_index, entry in enumerate(page_entries):
        if not page_needs_ocr(entry["regions"]):
            continue
        with Image.open(entry["path"]) as image:
            image = image.convert("RGB")
            num_bands = band_count_for(entry["regions"])
            for band_bytes, y_offset in split_page_into_bands(image, num_bands):
                band_jobs.append((band_bytes, y_offset))
                band_owner.append(entry_index)
    if band_jobs:
        band_results = await _default_ocr_dispatcher(band_jobs)
        lines_by_page = {}
        for entry_index, lines in zip(band_owner, band_results):
            lines_by_page.setdefault(entry_index, []).extend(lines)
        for entry_index, lines in lines_by_page.items():
            match_lines_to_regions(lines, page_entries[entry_index]["regions"])

    return page_entries


def region_summary(parcels):
    any_3plus = any(len(p.get("resolved_boundary_calls") or p.get("vision_geometry", {}).get("boundary_calls", [])) >= 3 for p in parcels)
    any_valid = any((p.get("spatial_validation") or {}).get("valid") for p in parcels)
    any_closed = any(
        (p.get("spatial_validation") or {}).get("precision_ratio") is not None
        and (p.get("spatial_validation") or {}).get("precision_ratio", 1e9) >= 2000
        for p in parcels
    )
    return any_3plus, any_closed, any_valid


async def main():
    corpus = json.loads(CORPUS_PATH.read_text())["documents"]
    labels = json.loads(LABELS_PATH.read_text())["regions"]
    labels_by_doc = {}
    for r in labels:
        labels_by_doc.setdefault(r["doc"], []).append(r)

    totals = {"plat_regions": 0, "any_3plus": 0, "closed": 0, "valid": 0}
    per_doc = {}

    for doc in corpus:
        doc_id = doc["id"]
        gt = [r for r in labels_by_doc.get(doc_id, []) if r["category"] == "plat"]
        if not gt:
            continue
        print(f"[{doc_id}] {len(gt)} ground-truth plat regions -- building page entries...", flush=True)
        page_entries = await build_page_entries(doc_id)

        kept = 0
        for entry in page_entries:
            page = entry["page_number"]
            page_gt = [g for g in gt if g["page"] == page]
            for region in entry["regions"]:
                match = None
                for g in page_gt:
                    if iou(g["bbox"], region["bbox"]) > 0.3:
                        match = g
                        break
                region["needs_vision"] = match is not None
                if match is not None:
                    kept += 1

        print(f"  -> {kept} regions marked needs_vision (should equal {len(gt)})", flush=True)
        await run_vision_stage(page_entries)

        doc_totals = {"plat_regions": 0, "any_3plus": 0, "closed": 0, "valid": 0}
        for entry in page_entries:
            for region in entry["regions"]:
                if not region.get("needs_vision"):
                    continue
                parcels = region.get("parcels")
                if parcels is None:
                    continue
                walked = walk_region_parcels(parcels, region.get("ocr_text") or "")
                any_3plus, any_closed, any_valid = region_summary(walked)
                doc_totals["plat_regions"] += 1
                doc_totals["any_3plus"] += int(any_3plus)
                doc_totals["closed"] += int(any_closed)
                doc_totals["valid"] += int(any_valid)

        per_doc[doc_id] = doc_totals
        for k in totals:
            totals[k] += doc_totals[k]
        print(f"  -> {doc_totals}", flush=True)

    print("\n=== FILTERED (true-plat-only batches) TOTALS ===")
    print(json.dumps(totals, indent=2))
    n = totals["plat_regions"] or 1
    print(f"any_3plus rate: {totals['any_3plus']}/{n} = {totals['any_3plus']/n:.3f}")
    print(f"closed rate:    {totals['closed']}/{n} = {totals['closed']/n:.3f}")
    print(f"valid rate:     {totals['valid']}/{n} = {totals['valid']/n:.3f}")

    Path("scratch_diag/filtered_vision_test_result.json").write_text(
        json.dumps({"per_doc": per_doc, "totals": totals}, indent=2)
    )


if __name__ == "__main__":
    asyncio.run(main())
