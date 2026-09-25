"""
Replays stored vision output through the CURRENT post-extraction code
(walk_region_parcels) -- no Gemini calls, deterministic, seconds not
hours. Use it to measure geometry/validation changes; extraction-stage
changes (prompts, cropping, rotation) still need run_baseline.py.

Prints the stored run's score next to the replayed score, so a change
that helps one category but hurts another is visible immediately.

Usage:
    python tests/regression/replay.py                 # latest full run
    python tests/regression/replay.py path/to/X_full.json
"""

import copy
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.pipeline.document_pipeline import walk_region_parcels  # noqa: E402
from tests.regression.scoring import score_full_results  # noqa: E402

FULL_DIR = Path("scratch_diag/regression_baseline")


def replay(full_results: dict) -> dict:
    out = copy.deepcopy(full_results)
    for data in out.values():
        for run in data["full_runs"]:
            if "result" not in run:
                continue
            for page in run["result"]["pages"]:
                for region in page["regions"]:
                    if "parcels" not in region:
                        continue
                    geometries = [p["vision_geometry"] for p in region["parcels"]]
                    region["parcels"] = walk_region_parcels(
                        geometries, region.get("ocr_text") or ""
                    )
    return out


def main() -> None:
    path = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(sorted(glob.glob(str(FULL_DIR / "*_full.json")))[-1])
    )
    stored = json.loads(path.read_text(encoding="utf-8"))
    before = score_full_results(stored)
    after = score_full_results(replay(stored))

    keys = [k for k in before["overall"] if k != "raw_counts"]
    print(f"source: {path}")
    print(f"{'metric':30s} {'stored':>10s} {'replayed':>10s}")
    for k in keys:
        print(f"{k:30s} {str(before['overall'][k]):>10s} {str(after['overall'][k]):>10s}")
    print("\nper document plat_closed_rate (stored -> replayed):")
    for doc in before["documents"]:
        b = before["documents"][doc]["plat_closed_rate"]
        a = after["documents"][doc]["plat_closed_rate"]
        print(f"  {doc[:8]}  {b} -> {a}")

    out = Path(__file__).parent / "replay_scorecard.json"
    out.write_text(json.dumps({"source": path.name, "stored": before, "replayed": after}, indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
