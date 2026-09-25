"""
Scores vision.classify_regions against region_labels.json. The number
that matters most is plats_dropped: a real plat classified as anything
other than boundary_plat is lost from extraction entirely.

Usage: python tests/regression/eval_triage.py
"""

import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PIL import Image  # noqa: E402

from app.services.vision import classify_regions  # noqa: E402

LABELS = json.loads((Path(__file__).parent / "region_labels.json").read_text())["regions"]
CACHE = Path("scratch_diag/regression_baseline/triage_eval_cache.json")
EXPECTED = {"plat": "boundary_plat", "undimensioned": "undimensioned_drawing", "not_a_plat": "not_a_parcel_drawing"}


def main() -> None:
    by_doc = defaultdict(list)
    for r in LABELS:
        by_doc[r["doc"]].append(r)

    confusion = Counter()
    dropped = []
    # Resumable: a document already classified in an earlier (crashed)
    # run isn't re-sent. Delete the cache after changing the triage prompt.
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    for doc, regions in by_doc.items():
        if doc not in cache:
            crops = []
            for r in regions:
                x, y, w, h = r["bbox"]
                page = Image.open(f"data/documents/{doc}/pages/page_{r['page']:03d}.png").convert("RGB")
                crops.append(page.crop((x, y, x + w, y + h)))
            for attempt in range(5):
                try:
                    cache[doc] = classify_regions(crops)
                    break
                except Exception as exc:  # noqa: BLE001 -- Gemini capacity outages
                    print(f"{doc[:8]}: attempt {attempt + 1} failed ({str(exc)[:60]}); waiting 120s", flush=True)
                    time.sleep(120)
            else:
                raise RuntimeError(f"{doc}: triage failed 5 times")
            CACHE.write_text(json.dumps(cache))
        got = cache[doc]
        for r, g in zip(regions, got):
            confusion[(r["category"], g)] += 1
            if r["category"] == "plat" and g != "boundary_plat":
                dropped.append((doc[:8], r["page"], g))
        print(f"{doc[:8]}: {len(regions)} regions classified", flush=True)

    print("\nlabel -> predicted counts:")
    for (label, pred), n in sorted(confusion.items()):
        mark = "" if EXPECTED[label] == pred else "  <-- mismatch"
        print(f"  {label:14s} -> {pred:22s} {n}{mark}")
    plats = sum(n for (l, _), n in confusion.items() if l == "plat")
    nonplat_kept = sum(n for (l, p), n in confusion.items() if l != "plat" and p == "boundary_plat")
    nonplats = sum(n for (l, _), n in confusion.items() if l != "plat")
    print(f"\nplats_dropped: {len(dropped)}/{plats}  {dropped}")
    print(f"nonplats still sent to extraction: {nonplat_kept}/{nonplats}")


if __name__ == "__main__":
    main()
