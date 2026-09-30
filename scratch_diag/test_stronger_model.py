"""
Test 1 of 2 (model-strength, case 1 ONLY): does a stronger model fix
completeness on simple, legible, currently-failing plats?

Scope: 2f896c95 p6, 2f896c95 p8, db54d473 p14 -- all hand-verified
legible at the resolution the model receives (see this session's
legibility check), all consistently 0/3 in the real regression runs.
Explicitly NOT subdivision/curve/dense plats -- that's a separate test.

Does NOT touch production code: settings.GEMINI_MODEL is monkeypatched
in this process only, and _generate_with_fallback still runs (so a
503 on the stronger model still falls back the same way production
would -- fallbacks list is temporarily cleared to isolate the target
model's own performance, restored after).

Same tile-read + structuring prompts and schema as production
(extract_parcel_geometries_batch, unmodified) -- only the model swaps.
"""

import json
import sys
import time

sys.path.insert(0, ".")

from PIL import Image

from app.core.config import settings
from app.services.vision import extract_parcel_geometries_batch
from app.pipeline.document_pipeline import walk_region_parcels

TARGETS = {
    "2f896c95_p6": ("2f896c95-b0f3-4a49-a447-4b3ca6129c27", 6, [55, 89, 2065, 1344]),
    "2f896c95_p8": ("2f896c95-b0f3-4a49-a447-4b3ca6129c27", 8, [878, 268, 5330, 3403]),
    "db54d473_p14": ("db54d473-c7ab-4bec-bbf4-8ff9a1e3bd9b", 14, [-4, 51, 1787, 1344]),
}

MODELS = {
    "current (lite)": "gemini-3.5-flash-lite",
    "stronger (full flash)": "gemini-3.5-flash",
}

RUNS = 5


def load_crop(key):
    doc, page, bbox = TARGETS[key]
    x, y, w, h = bbox
    x, y = max(x, 0), max(y, 0)
    im = Image.open(f"data/documents/{doc}/pages/page_{page:03d}.png").convert("RGB")
    return im.crop((x, y, x + w, y + h))


def run_once(model_name):
    original_model = settings.GEMINI_MODEL
    original_fallbacks = settings.GEMINI_FALLBACK_MODELS
    settings.GEMINI_MODEL = model_name
    settings.GEMINI_FALLBACK_MODELS = ""  # isolate this model's own performance
    try:
        crops = [load_crop(k) for k in TARGETS]
        start = time.time()
        results = extract_parcel_geometries_batch(crops)
        elapsed = time.time() - start
    finally:
        settings.GEMINI_MODEL = original_model
        settings.GEMINI_FALLBACK_MODELS = original_fallbacks

    per_page = {}
    for key, geometries in zip(TARGETS, results):
        if isinstance(geometries, Exception):
            per_page[key] = {"error": str(geometries)}
            continue
        parcels = walk_region_parcels(geometries)
        best = None
        for p in parcels:
            resolved = p.get("resolved_boundary_calls") or []
            sv = p.get("spatial_validation") or {}
            n_calls = len(resolved)
            if best is None or n_calls > best["n_calls"]:
                best = {
                    "n_calls": n_calls,
                    "precision_ratio": sv.get("precision_ratio"),
                    "valid": sv.get("valid"),
                }
        per_page[key] = best or {"n_calls": 0, "precision_ratio": None, "valid": False}
    return per_page, elapsed


def main():
    all_results = {}
    for label, model in MODELS.items():
        print(f"\n=== {label} ({model}) ===", flush=True)
        runs = []
        for i in range(RUNS):
            print(f"  run {i + 1}/{RUNS}...", flush=True)
            try:
                per_page, elapsed = run_once(model)
            except Exception as exc:  # noqa: BLE001
                print(f"    call failed: {exc}", flush=True)
                runs.append({"error": str(exc)})
                continue
            print(f"    elapsed {elapsed:.1f}s: {per_page}", flush=True)
            per_page["elapsed_s"] = round(elapsed, 1)
            runs.append(per_page)
        all_results[label] = runs

    print("\n\n=== SUMMARY ===")
    for label, runs in all_results.items():
        ok_runs = [r for r in runs if "error" not in r]
        print(f"\n{label}: {len(ok_runs)}/{RUNS} runs completed")
        for key in TARGETS:
            n3 = sum(1 for r in ok_runs if r.get(key, {}).get("n_calls", 0) >= 3)
            closed = sum(
                1
                for r in ok_runs
                if r.get(key, {}).get("precision_ratio")
                and r[key]["precision_ratio"] >= 500
            )
            valid = sum(1 for r in ok_runs if r.get(key, {}).get("valid"))
            print(f"  {key}: 3+_calls {n3}/{len(ok_runs)}  closed(1:500+) {closed}/{len(ok_runs)}  valid {valid}/{len(ok_runs)}")
        elapsed_vals = [r["elapsed_s"] for r in ok_runs if "elapsed_s" in r]
        if elapsed_vals:
            print(f"  mean call time: {sum(elapsed_vals)/len(elapsed_vals):.1f}s")

    with open("scratch_diag/stronger_model_results.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)


if __name__ == "__main__":
    main()
