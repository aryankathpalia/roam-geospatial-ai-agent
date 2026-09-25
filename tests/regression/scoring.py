"""
Label-aware scoring for the regression harness. Every region vision
reads is matched to its hand label in region_labels.json and scored by
what SHOULD happen for that kind of region:

- plat (bearing/distance survey plat): we want at least one parcel
  whose traverse actually closes. Headline: plat_closed_rate.
- undimensioned / not_a_plat: there is no boundary traverse to find,
  so any parcel with >=3 calls here is invented geometry. Headline:
  phantom_parcels (lower is better) and false_valid (must stay 0).

Output is counts and rates only -- no bearings, labels or text -- so
scorecards are safe to commit.
"""

import json
from collections import defaultdict
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.geometry import parse_bearing, parse_distance  # noqa: E402

LABELS_PATH = Path(__file__).parent / "region_labels.json"

# Looser than validate_traverse's 1:2000 "valid" bar on purpose: this
# answers "did we find the right shape at all", while the stricter bar
# is still reported separately as plat_valid_rate.
CLOSED_MIN_PRECISION = 500


def region_key(doc_id: str, page: int, bbox) -> tuple:
    return (doc_id, page, tuple(round(v) for v in bbox))


def load_labels() -> dict[tuple, dict]:
    data = json.loads(LABELS_PATH.read_text())
    return {region_key(r["doc"], r["page"], r["bbox"]): r for r in data["regions"]}


def _parsed_call_count(parcel: dict) -> int:
    calls = parcel.get("resolved_boundary_calls") or parcel["vision_geometry"].get(
        "boundary_calls"
    ) or []
    return sum(
        1
        for c in calls
        if parse_bearing(str(c.get("bearing") or "")) is not None
        and parse_distance(str(c.get("distance") or "")) is not None
    )


def _closed(parcel: dict) -> bool:
    sv = parcel.get("spatial_validation")
    if not sv or _parsed_call_count(parcel) < 3 or not sv.get("area_sqft"):
        return False
    ratio = sv.get("precision_ratio")
    return ratio is None or ratio >= CLOSED_MIN_PRECISION  # None == perfect closure


def score_run(result: dict, labels: dict[tuple, dict]) -> dict:
    doc_id = result["document_id"]
    c = defaultdict(int)
    for page in result["pages"]:
        for region in page["regions"]:
            if not ("parcels" in region or region.get("vision_error") or region.get("vision_triage")):
                continue
            label = labels.get(region_key(doc_id, page["page_number"], region["bbox"]))
            cat = label["category"] if label else "unlabeled"
            parcels = region.get("parcels") or []
            c[f"{cat}_regions"] += 1
            if region.get("vision_triage"):
                c[f"{cat}_regions_triaged_out"] += 1
            if region.get("vision_error"):
                c["vision_error_regions"] += 1
            c[f"{cat}_parcels"] += len(parcels)
            with_geom = [p for p in parcels if _parsed_call_count(p) >= 3]
            c[f"{cat}_parcels_3plus_calls"] += len(with_geom)
            c[f"{cat}_parcels_valid"] += sum(
                1 for p in parcels if (p.get("spatial_validation") or {}).get("valid")
            )
            if cat == "plat" and label.get("closeable_from_calls"):
                c["closeable_plat_regions"] += 1
                c["closeable_plat_regions_any_3plus"] += bool(with_geom)
                c["closeable_plat_regions_closed"] += any(_closed(p) for p in parcels)
                c["closeable_plat_regions_valid"] += any(
                    (p.get("spatial_validation") or {}).get("valid") for p in parcels
                )
    return dict(c)


def _rate(num, den):
    return round(num / den, 3) if den else None


def summarize(counts: dict) -> dict:
    cp = counts.get("closeable_plat_regions", 0)
    return {
        "plat_closed_rate": _rate(counts.get("closeable_plat_regions_closed", 0), cp),
        "plat_valid_rate": _rate(counts.get("closeable_plat_regions_valid", 0), cp),
        "plat_any_3plus_calls_rate": _rate(counts.get("closeable_plat_regions_any_3plus", 0), cp),
        "phantom_parcels": counts.get("undimensioned_parcels_3plus_calls", 0)
        + counts.get("not_a_plat_parcels_3plus_calls", 0),
        "nonplat_parcels_emitted": counts.get("undimensioned_parcels", 0)
        + counts.get("not_a_plat_parcels", 0),
        "false_valid": counts.get("undimensioned_parcels_valid", 0)
        + counts.get("not_a_plat_parcels_valid", 0),
        "plats_dropped_by_triage": counts.get("plat_regions_triaged_out", 0),
        "nonplats_skipped_by_triage": counts.get("undimensioned_regions_triaged_out", 0)
        + counts.get("not_a_plat_regions_triaged_out", 0),
        "vision_error_regions": counts.get("vision_error_regions", 0),
        "unlabeled_regions": counts.get("unlabeled_regions", 0),
        "raw_counts": counts,
    }


def score_full_results(full_results: dict) -> dict:
    """full_results: {doc_id: {"full_runs": [{"run": i, "result": {...}} | {"error": ...}]}}.
    Counts are summed over all runs, so rates are pooled across runs."""

    labels = load_labels()
    per_doc = {}
    total = defaultdict(int)
    runs_per_doc = {}
    for doc_id, data in full_results.items():
        doc_counts = defaultdict(int)
        runs = [r for r in data["full_runs"] if "result" in r]
        runs_per_doc[doc_id] = len(runs)
        for run in runs:
            for k, v in score_run(run["result"], labels).items():
                doc_counts[k] += v
                total[k] += v
        per_doc[doc_id] = summarize(dict(doc_counts))
    return {
        "overall": summarize(dict(total)),
        "runs_per_document": runs_per_doc,
        "documents": per_doc,
    }
