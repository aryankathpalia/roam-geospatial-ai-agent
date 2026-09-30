"""Offline: does sign-flip (+ small drop) recover closure on stored extractions?
Includes a null control using random calls drawn from unrelated parcels."""
import glob
import itertools
import json
import math
import random
import sys

import numpy as np

sys.path.insert(0, ".")
from app.services.geometry import parse_bearing, parse_distance  # noqa: E402

f = sorted(glob.glob("scratch_diag/regression_baseline/*_full.json"))[-1]
data = json.load(open(f, encoding="utf-8"))

MAX_N = 16
SIGNS = {n: np.array(list(itertools.product([1, -1], repeat=n - 1))) for n in range(2, MAX_N + 1)}


def vecs(calls):
    out = []
    for c in calls:
        az = parse_bearing(str(c.get("bearing") or ""))
        d = parse_distance(str(c.get("distance") or ""))
        if az is None or d is None or d <= 0:
            continue
        r = math.radians(az)
        out.append((d * math.sin(r), d * math.cos(r)))
    return np.array(out)


def best_sign_closure(v):
    """Min closure over sign flips (first call's sign fixed -- global flip is symmetric)."""
    n = len(v)
    if n < 3 or n > MAX_N:
        return None
    s = np.hstack([np.ones((SIGNS[n].shape[0], 1)), SIGNS[n]])
    tot = s @ v
    cl = np.hypot(tot[:, 0], tot[:, 1])
    return float(cl.min())


def best_with_drops(v, max_drop):
    n = len(v)
    best = (float("inf"), None)
    for k in range(0, max_drop + 1):
        if n - k < 3:
            break
        for drop in itertools.combinations(range(n), k):
            keep = [i for i in range(n) if i not in drop]
            sub = v[keep]
            c = best_sign_closure(sub)
            if c is None:
                continue
            per = np.hypot(sub[:, 0], sub[:, 1]).sum()
            prec = per / c if c > 0 else 1e9
            if prec > best[0] or best[1] is None:
                if best[1] is None or prec > best[0]:
                    best = (prec, k)
    return best


parcels = []
for doc, dd in data.items():
    for run in dd["full_runs"]:
        if "result" not in run:
            continue
        for page in run["result"]["pages"]:
            for r in page["regions"]:
                for p in r.get("parcels", []):
                    calls = p.get("resolved_boundary_calls") or []
                    v = vecs(calls)
                    sv = p.get("spatial_validation") or {}
                    parcels.append((doc, v, sv.get("precision_ratio")))

print("total parcels:", len(parcels))
usable = [(d, v, pr) for d, v, pr in parcels if 3 <= len(v) <= MAX_N]
print("usable (3..16 calls):", len(usable))

THRESH = [500, 2000]


def summarize(label, precs):
    precs = [p for p in precs if p is not None]
    out = [label, f"n={len(precs)}"]
    for t in THRESH:
        out.append(f">=1:{t}: {sum(p >= t for p in precs)} ({sum(p >= t for p in precs)/max(len(precs),1):.0%})")
    print("  ".join(out))


base = []
for d, v, pr in usable:
    per = np.hypot(v[:, 0], v[:, 1]).sum()
    cl = np.hypot(*v.sum(axis=0))
    base.append(per / cl if cl > 0 else 1e9)
summarize("as-walked      ", base)

sign_only = []
for d, v, pr in usable:
    c = best_sign_closure(v)
    per = np.hypot(v[:, 0], v[:, 1]).sum()
    sign_only.append(per / c if c > 0 else 1e9)
summarize("sign-flip      ", sign_only)

drop1 = [best_with_drops(v, 1)[0] for d, v, pr in usable if len(v) <= 14]
summarize("sign+drop<=1   ", drop1)
drop2 = [best_with_drops(v, 2)[0] for d, v, pr in usable if len(v) <= 12]
summarize("sign+drop<=2   ", drop2)

# Null control: same call-count distribution, calls drawn randomly from OTHER parcels.
random.seed(0)
pool = [row for d, v, pr in usable for row in v]
null_sign, null_d1, null_d2 = [], [], []
for d, v, pr in usable:
    fake = np.array(random.sample(pool, len(v)))
    per = np.hypot(fake[:, 0], fake[:, 1]).sum()
    c = best_sign_closure(fake)
    null_sign.append(per / c if c > 0 else 1e9)
    if len(v) <= 14:
        null_d1.append(best_with_drops(fake, 1)[0])
    if len(v) <= 12:
        null_d2.append(best_with_drops(fake, 2)[0])
print("--- NULL CONTROL (random calls from unrelated parcels) ---")
summarize("null sign-flip ", null_sign)
summarize("null drop<=1   ", null_d1)
summarize("null drop<=2   ", null_d2)
