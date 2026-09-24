"""
Regression harness, step 2: run the current pipeline against every
document in corpus.json, several times each (non-determinism means one
run per document isn't trustworthy -- see this session's base-rate
testing), and save the results.

Two outputs, on purpose:

- scratch_diag/regression_baseline/<timestamp>_full.json -- everything,
  including per-parcel spatial_validation issue TEXT, which can quote
  real bearings/distances from the source documents. Gitignored, local
  only, same as the rest of scratch_diag/.

- tests/regression/baseline_scorecard.json -- numeric-only aggregates
  (counts and rates, no bearing/distance/label text). Safe to commit;
  this is the number future fixes get compared against.

Usage:
    python tests/regression/run_baseline.py             # N=3 runs/doc
    python tests/regression/run_baseline.py --runs 5
    python tests/regression/run_baseline.py --doc 2f896c95-b0f3-4a49-a447-4b3ca6129c27
"""

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.pipeline.document_pipeline import process_document  # noqa: E402

CORPUS_PATH = Path(__file__).parent / "corpus.json"
FULL_OUTPUT_DIR = Path("scratch_diag/regression_baseline")
SCORECARD_PATH = Path(__file__).parent / "baseline_scorecard.json"

# Issue messages are free text (validate_traverse composes them per
# call), so they're bucketed by their known-fixed prefixes rather than
# matched verbatim -- keeps the scorecard stable even as message
# wording evolves.
_ISSUE_BUCKETS = [
    ("weak_closure", "Weak closure"),
    ("self_intersects", "Traverse self-intersects"),
    ("too_few_calls", "boundary call(s) parsed"),
    ("area_mismatch", "differs from the document's stated area"),
]


def _bucket_issue(issue_text: str) -> str:
    for bucket, prefix in _ISSUE_BUCKETS:
        if prefix in issue_text:
            return bucket
    return "other"


def _summarize_run(result: dict) -> dict:
    """Strips a process_document() result down to counts only -- no
    bearing/distance/label text, safe to fold into the committed
    scorecard."""

    n_regions = 0
    n_parcels = 0
    n_valid = 0
    n_with_geometry = 0
    precision_ratios: list[float] = []
    area_match_count = 0
    area_match_checked = 0
    issue_buckets: Counter = Counter()

    for page in result["pages"]:
        for region in page["regions"]:
            if "parcels" not in region:
                continue
            n_regions += 1
            for parcel in region["parcels"]:
                n_parcels += 1
                validation = parcel.get("spatial_validation")
                if not validation:
                    continue
                n_with_geometry += 1
                if validation["valid"]:
                    n_valid += 1
                if validation.get("precision_ratio") is not None:
                    precision_ratios.append(validation["precision_ratio"])
                if validation.get("area_matches_stated") is not None:
                    area_match_checked += 1
                    if validation["area_matches_stated"]:
                        area_match_count += 1
                for issue in validation.get("issues", []):
                    issue_buckets[_bucket_issue(issue)] += 1

    return {
        "n_regions": n_regions,
        "n_parcels": n_parcels,
        "n_parcels_with_geometry": n_with_geometry,
        "n_valid": n_valid,
        "valid_rate": round(n_valid / n_with_geometry, 3) if n_with_geometry else None,
        "mean_precision_ratio": (
            round(sum(precision_ratios) / len(precision_ratios), 1)
            if precision_ratios
            else None
        ),
        "area_match_rate": (
            round(area_match_count / area_match_checked, 3) if area_match_checked else None
        ),
        "issue_buckets": dict(issue_buckets),
        "pages_needing_review": result["pages_needing_review"],
    }


async def _run_document(doc_id: str, runs: int) -> dict:
    full_runs = []
    summaries = []
    for i in range(runs):
        print(f"  run {i + 1}/{runs}...", flush=True)
        start = time.time()
        try:
            result = await process_document(doc_id)
        except Exception as exc:  # noqa: BLE001 -- a failed run is itself a result
            full_runs.append({"run": i, "error": str(exc)})
            summaries.append({"run": i, "error": str(exc)})
            continue
        elapsed = round(time.time() - start, 1)
        full_runs.append({"run": i, "elapsed_s": elapsed, "result": result})
        summary = _summarize_run(result)
        summary["run"] = i
        summary["elapsed_s"] = elapsed
        summaries.append(summary)
    return {"full_runs": full_runs, "summaries": summaries}


def _aggregate_summaries(summaries: list[dict]) -> dict:
    ok = [s for s in summaries if "error" not in s]
    errors = [s for s in summaries if "error" in s]

    valid_rates = [s["valid_rate"] for s in ok if s["valid_rate"] is not None]
    area_match_rates = [s["area_match_rate"] for s in ok if s["area_match_rate"] is not None]
    n_regions = [s["n_regions"] for s in ok]
    n_parcels = [s["n_parcels"] for s in ok]

    issue_totals: Counter = Counter()
    for s in ok:
        issue_totals.update(s["issue_buckets"])

    return {
        "runs_completed": len(ok),
        "runs_errored": len(errors),
        "errors": [e["error"] for e in errors],
        "n_regions_by_run": n_regions,
        "n_parcels_by_run": n_parcels,
        "mean_valid_rate": round(sum(valid_rates) / len(valid_rates), 3) if valid_rates else None,
        "valid_rate_by_run": valid_rates,
        "mean_area_match_rate": (
            round(sum(area_match_rates) / len(area_match_rates), 3) if area_match_rates else None
        ),
        "issue_totals_across_runs": dict(issue_totals),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3, help="Runs per document (default 3)")
    parser.add_argument("--doc", type=str, default=None, help="Only run this document id")
    args = parser.parse_args()

    corpus = json.loads(CORPUS_PATH.read_text())["documents"]
    if args.doc:
        corpus = [d for d in corpus if d["id"] == args.doc]
        if not corpus:
            print(f"Document {args.doc} not in corpus.json")
            return

    FULL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")

    full_results = {}
    scorecard = {"generated_at": timestamp, "runs_per_document": args.runs, "documents": {}}

    for doc in corpus:
        doc_id = doc["id"]
        print(f"[{doc_id}] ({doc['pages']} pages, {doc['parcelmap_regions']} parcelmap regions)")
        run_data = await _run_document(doc_id, args.runs)
        full_results[doc_id] = run_data
        scorecard["documents"][doc_id] = _aggregate_summaries(run_data["summaries"])

    full_path = FULL_OUTPUT_DIR / f"{timestamp}_full.json"
    full_path.write_text(json.dumps(full_results, indent=2, default=str))
    print(f"\nFull results (local only, gitignored): {full_path}")

    SCORECARD_PATH.write_text(json.dumps(scorecard, indent=2))
    print(f"Scorecard (safe to commit): {SCORECARD_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
