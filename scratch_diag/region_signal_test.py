"""
One-off diagnostic: for every hand-labeled region in region_labels.json,
run OCR only (no vision, no API cost) and test whether a simple
deterministic signal -- count of bearing-shaped and distance-shaped
tokens in the region's OCR text -- separates true "plat" (geocoding
source) regions from "undimensioned"/"not_a_plat" ones.
"""

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scratch_diag.filtered_vision_test import build_page_entries, iou

LABELS_PATH = Path("tests/regression/region_labels.json")

_BEARING_RE = re.compile(r"[NSns]\s*\d{1,3}\s*[°*ov]\s*\d{0,2}\s*['’′]?\s*\d{0,2}\s*[\"”″]?\s*[EWew]")
_DISTANCE_RE = re.compile(r"\d{2,4}\.\d{2}\s*'")


def bearing_distance_score(text: str) -> tuple[int, int]:
    return len(_BEARING_RE.findall(text)), len(_DISTANCE_RE.findall(text))


async def main():
    labels = json.loads(LABELS_PATH.read_text())["regions"]
    labels_by_doc = {}
    for r in labels:
        labels_by_doc.setdefault(r["doc"], []).append(r)

    rows = []
    for doc_id, regions in labels_by_doc.items():
        print(f"[{doc_id}] OCR-only pass for {len(regions)} labeled regions...", flush=True)
        try:
            page_entries = await build_page_entries(doc_id)
        except Exception as exc:
            print(f"  FAILED: {exc}")
            continue

        for g in regions:
            page = g["page"]
            entry = next((e for e in page_entries if e["page_number"] == page), None)
            if entry is None:
                continue
            best = None
            for region in entry["regions"]:
                score = iou(g["bbox"], region["bbox"])
                if best is None or score > best[0]:
                    best = (score, region)
            if best is None or best[0] < 0.3:
                rows.append({"doc": doc_id, "page": page, "category": g["category"], "matched": False})
                continue
            region = best[1]
            text = region.get("ocr_text") or ""
            n_bearing, n_distance = bearing_distance_score(text)
            rows.append(
                {
                    "doc": doc_id,
                    "page": page,
                    "category": g["category"],
                    "matched": True,
                    "n_bearing": n_bearing,
                    "n_distance": n_distance,
                    "has_signal": (n_bearing >= 2 and n_distance >= 2),
                }
            )

    Path("scratch_diag/region_signal_test_result.json").write_text(json.dumps(rows, indent=2))

    matched = [r for r in rows if r["matched"]]
    plat = [r for r in matched if r["category"] == "plat"]
    non_plat = [r for r in matched if r["category"] != "plat"]

    plat_with_signal = sum(1 for r in plat if r["has_signal"])
    non_plat_with_signal = sum(1 for r in non_plat if r["has_signal"])

    print("\n=== SIGNAL SEPARABILITY (>=2 bearing tokens AND >=2 distance tokens) ===")
    print(f"plat regions:     {len(plat)}, flagged-as-source: {plat_with_signal} ({plat_with_signal/len(plat):.1%})" if plat else "no plat regions matched")
    print(f"non-plat regions: {len(non_plat)}, WRONGLY flagged-as-source: {non_plat_with_signal} ({non_plat_with_signal/len(non_plat):.1%})" if non_plat else "no non-plat regions matched")


if __name__ == "__main__":
    asyncio.run(main())
