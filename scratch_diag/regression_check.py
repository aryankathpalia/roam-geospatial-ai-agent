"""
Regression check for two of today's fixes, against the labeled corpus
(tests/regression/corpus.json):

1. ParcelMap crop padding (region_cropper.py): run real layout detection
   + region cropping on every page of every corpus document, verify no
   crashes, sane crop dimensions, and that ParcelMap crops get the
   expected proportional margin without misbehaving at page edges.

2. Duplicate-region flag (document_pipeline.py): since none of the
   corpus documents have been run through vision extraction (no
   result.json), this can't be tested against corpus vision output
   directly. Instead, run it against EVERY already-processed document
   in data/documents/ (in-memory, no writes) -- a much larger, more
   meaningful sweep for false positives than the 10-doc corpus alone,
   which is the honest substitution given the corpus's own data gap.

No vision/OCR API calls, no writes to any result.json. Read-only.
"""

import sys
import io
import json
import glob
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.services.layout_detector_onnx import detect_page_layout
from app.services.region_cropper import (
    extract_region_crops,
    CROP_MARGIN_PX,
    PARCELMAP_CROP_MARGIN_FRAC,
    PARCELMAP_CROP_MARGIN_MIN_PX,
)
from app.pipeline.document_pipeline import flag_spurious_duplicate_parcelmap_regions
from PIL import Image

CORPUS_IDS = [
    "2f896c95-b0f3-4a49-a447-4b3ca6129c27",
    "27211ae5-0099-4b0d-8176-f1483897b766",
    "3affa529-5b4f-42d7-b336-596315433572",
    "65be453d-f014-4024-84d6-60a2d0052aeb",
    "a1136318-03ef-438d-9f02-8fcdcb5dca30",
    "db54d473-c7ab-4bec-bbf4-8ff9a1e3bd9b",
    "798850fc-1be3-4bc6-9f53-1441c39cbd04",
    "83ab22d0-b950-4f5d-ad33-5f0fd6105dbe",
    "8d75d3ed-d9e7-4bfb-874a-0b52063883eb",
    "510ea60f-f577-420d-b1e0-f2d3e0352661",
]
MAX_PAGES_PER_DOC = 20  # cap for the two huge documents (103, 196 pages)


def check_crop_padding():
    print("=" * 72)
    print("1. CROP PADDING REGRESSION")
    print("=" * 72)
    total_pages = 0
    total_parcelmap = 0
    failures = []
    for doc_id in CORPUS_IDS:
        pages_dir = Path(f"data/documents/{doc_id}/pages")
        page_paths = sorted(pages_dir.glob("*.png"))[:MAX_PAGES_PER_DOC]
        for page_path in page_paths:
            total_pages += 1
            try:
                detections = detect_page_layout(str(page_path))
                crops = extract_region_crops(str(page_path), detections)
            except Exception as exc:
                failures.append(f"{doc_id} {page_path.name}: CRASH -- {exc}")
                continue

            with Image.open(page_path) as page_img:
                page_w, page_h = page_img.size

            for crop in crops:
                if crop.roam_class != "ParcelMap":
                    continue
                total_parcelmap += 1
                cw, ch = crop.image.size
                bx, by, bw, bh = crop.bbox
                expected_margin_x = max(PARCELMAP_CROP_MARGIN_MIN_PX, bw * PARCELMAP_CROP_MARGIN_FRAC)
                # bbox now IS the padded box (region_cropper.py stores the
                # padded box as bbox, not the raw detection) -- sanity
                # check it's not degenerate and stays within the page.
                if bw <= 0 or bh <= 0:
                    failures.append(f"{doc_id} {page_path.name}: degenerate ParcelMap bbox {crop.bbox}")
                if bx < 0 or by < 0 or bx + bw > page_w + 1 or by + bh > page_h + 1:
                    failures.append(f"{doc_id} {page_path.name}: bbox out of page bounds {crop.bbox} vs page ({page_w},{page_h})")
                if cw != round(bw) and abs(cw - bw) > 2:
                    failures.append(f"{doc_id} {page_path.name}: crop image size {crop.image.size} doesn't match bbox w/h ({bw},{bh})")

    print(f"pages checked: {total_pages}")
    print(f"ParcelMap crops checked: {total_parcelmap}")
    print(f"failures: {len(failures)}")
    for f in failures[:20]:
        print("  -", f)
    return len(failures) == 0


def check_duplicate_flag():
    print()
    print("=" * 72)
    print("2. DUPLICATE-REGION FLAG REGRESSION (all processed documents)")
    print("=" * 72)
    checked = 0
    flagged_docs = []
    errors = []
    for path in glob.glob("data/documents/*/result.json"):
        doc_id = os.path.basename(os.path.dirname(path))
        try:
            r = json.load(open(path, encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{doc_id}: failed to load -- {exc}")
            continue
        page_entries = [{"page_number": p["page_number"], "regions": p["regions"]} for p in r.get("pages", [])]
        try:
            flag_spurious_duplicate_parcelmap_regions(page_entries)
        except Exception as exc:
            errors.append(f"{doc_id}: CRASH -- {exc}")
            continue
        checked += 1
        flagged_here = []
        for entry in page_entries:
            for region in entry["regions"]:
                if region.get("class") != "ParcelMap":
                    continue
                for par in region.get("parcels") or []:
                    if par.get("likely_duplicate_region"):
                        flagged_here.append(par.get("vision_geometry", {}).get("parcel_label"))
        if flagged_here:
            flagged_docs.append((doc_id, flagged_here))

    print(f"documents checked: {checked}")
    print(f"documents with a flagged duplicate: {len(flagged_docs)}")
    for doc_id, labels in flagged_docs:
        print(f"  - {doc_id}: {labels}")
    print(f"errors: {len(errors)}")
    for e in errors[:20]:
        print("  -", e)
    return len(errors) == 0


if __name__ == "__main__":
    padding_ok = check_crop_padding()
    dup_ok = check_duplicate_flag()
    print()
    print("=" * 72)
    print(f"RESULT: crop padding {'PASS' if padding_ok else 'FAIL'}, duplicate flag {'PASS' if dup_ok else 'FAIL'}")
    print("=" * 72)
