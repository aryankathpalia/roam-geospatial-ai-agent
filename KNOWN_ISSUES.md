# Known issues

Status as of 2026-09-25, after two sessions of diagnosis and fixes on the vision
extraction pipeline (`app/services/vision.py`, `app/services/geometry.py`,
`app/services/spatial_validation.py`, `app/pipeline/document_pipeline.py`). Evidence
for each item below came from live testing against real documents in
`data/documents/`, not assumption — see commit messages for the specific traces.

As of this update, the "general accuracy beyond NVZ" item below has a real
measurement behind it for the first time, via `tests/regression/` (a fixed
10-document corpus with hand-labeled regions, plus a scorer that separates real
survey plats from aerials/vicinity maps/assessor exhibits instead of scoring all
three as one number — see `tests/regression/scoring.py` for why that split matters).

## Fixed and committed this session

- **Tie-line misclassification** — a tie line to a section-corner monument was read
  as an ordinary boundary call, inflating parcel area by 20-30x. Fixed by moving the
  tie/monument exclusion rule into the tile-read prompt, where the surrounding
  context (monument label, coordinates) is still present.
- **Tile-boundary text loss** — dimension labels sitting on a tile seam were split in
  half and dropped. Fixed with a tile overlap margin.
- **Structuring under-splitting** — a region with multiple explicitly labeled parcels
  was collapsed into one entry. Fixed by forcing the model to enumerate every parcel
  label before extracting any calls (two-step structuring prompt).
- **Combined-tract width resolution (Track B)** — two parcels sharing one boundary
  line both used the line's combined length instead of their own segment. Fixed by
  testing each candidate segment against the parcel's own stated acreage and
  rejecting any that matches the combined acreage instead.
- **503 fallback** — Gemini's "high demand" errors (per-model server capacity, not
  quota) now retry on a configured fallback model instead of failing the request.
- **Over-enumeration / garbled labels** — the enumeration fix above (structuring
  under-splitting) introduced two new problems: it started listing neighbor/
  reference-only citations and ingredient tax-parcel APNs as if they were real final
  parcels, and it occasionally fused two unrelated adjacent labels (e.g. a road name
  and a lot label) into one bogus string. Fixed by applying the existing
  neighbor-citation exclusion rule at enumeration time too, and requiring each listed
  label be a single atomic identifier.
- **Curly-quote bearing parsing** — Gemini sometimes renders the minute/second
  symbols as typographic quotes (U+2019/U+2018, U+201D/U+201C) instead of straight
  marks, which the bearing parser didn't recognize, silently dropping those calls.
  Fixed by accepting the curly variants.

## Fixed and committed this session (2026-09-25)

- **Weak-closure / self-intersection diagnostics** — both messages now name the
  specific call(s) responsible when the evidence supports it (a single call whose
  removal meaningfully improves closure; the two edges that actually cross),
  instead of only saying something is wrong.
- **UI silently dropping no-geometry parcels** — the workspace view filtered out any
  parcel without `boundary_geojson_wgs84`, which meant `extraction_note` and
  `georeference_error` — real signal the backend was already producing — never
  reached the screen. Fixed; these now render as "No geometry" cards with the reason.
- **Regression test harness** (`tests/regression/`) — a fixed, hand-labeled
  10-document corpus, a runner (`run_baseline.py`, plus a `--vision-only` mode that
  replays stored layout/OCR through just the vision stage in minutes instead of
  hours), and a category-aware scorer (`scoring.py`) that reports real plats,
  undimensioned drawings, and non-plats separately instead of one blended number.
  `replay.py` re-runs just the deterministic geometry stage on stored extractions —
  free, and how every fix below was checked for regressions before committing.
- **Traverse assembly** (`assemble_traverse`) — plats label each line from
  whichever end the drafter chose and list them in reading order, not walking
  order, so a fully-correct set of extracted lines could still fail to close. This
  finds the direction assignment that actually closes the traverse (trying line
  reversals, and — only when a parcel's own stated acreage independently confirms
  it — dropping up to 2 lines), falling back to bearing-order when the given order
  self-intersects. Every change it makes is surfaced on the parcel card, never
  applied silently. Moved real plats reaching Valid from 0% to 3% with zero false
  positives; a no-op on already-closing traverses (confirmed via `replay.py`).
- **503 outage resilience** — `_generate_with_fallback` now retries in rounds
  (20s, 60s) when every configured model is at capacity at once, instead of
  failing outright; a live regression run recovered from exactly this mid-run.
  Each vision chunk's failure is now isolated too — one chunk erroring no longer
  blanks every other region in the same document (confirmed live: a full-document
  503 spike previously zeroed all 27 regions of one document in a single run).
- **ParcelMap rotation correction** (`ocr.upright`) — a PaddleOCR orientation
  classifier auto-rotates a crop before it's tiled for vision, scoped to ParcelMap
  crops only. Measured 5/5 correct on real rotated plats in the labeled corpus, 0
  false rotations on the other 18. Confirmed safe but did not move the target
  metric (3+ calls reached) on its own — the rotated plats in this corpus are also
  heavily watermarked/degraded, and rotation wasn't their actual bottleneck.
- **Curve-call support** (`curve_calls`, `merge_curve_calls`, `_curve_chord`) —
  implemented, unit-tested, zero regression, confirmed working on real data
  (`8d75d3ed` LOT 2 captured a real Δ/R/L curve and walked it into the traverse).
  Does not fix subdivision-plat completeness on its own — that's the same per-lot
  missing-sides problem as before, now unblocked from closing once completeness
  improves elsewhere.
- **Model escalation (lite→flash on failure)** — implemented, safe, zero-regression,
  hard fallback to lite result always preserved. Confirmed working (5 regions fixed
  in corpus test) but bounded by Gemini's 20-requests/day free-tier cap on the
  stronger model (~10 real escalation attempts/day account-wide) — not a meaningful
  lever on stage-2 accuracy at current scale or document volume. Would need
  paid-tier quota to matter at real scale.

## Explicitly held, not shipped

- **Region triage** (`vision.classify_regions`) — a cheap pre-pass that sorts
  ParcelMap crops (survey plat / undimensioned drawing / not a parcel drawing) so
  only real plats reach the expensive tiled read. Correctly excluded 89% of
  non-plat noise (aerials, vicinity maps, index maps) in eval, and cut invented
  parcels from non-plats by ~9x in a live run. **Held because it drops real
  plats**: 9 of 23 plat region-runs in the labeled corpus, including one plat
  (`510ea60f` p57) missed in all 3 live runs. The classifier function exists and
  is tested (`tests/regression/eval_triage.py`), but is NOT called from
  `run_vision_stage` — every `needs_vision` region still goes to extraction until
  triage's false-negative rate on real plats is fixed.

## Still open, not fixed this session

- **Batching-completeness non-determinism** — both the tile-read and structuring
  stages have been shown to return different results on identical input across
  repeated calls (confirmed via direct rerun tests, not inferred). Neither reducing
  tile-batch size nor reducing regions-per-call reliably fixed this; the cause is not
  isolated. This is the largest remaining source of inconsistent output.
- **Shared-edge / stacked-parcel boundary attribution beyond 2 parcels** —
  `2f896c95` pages 6-8 show 5 parcels sharing boundary lines (the same structural
  pattern Track B fixed for NVZ's 2-parcel case), but with N>2 parcels stacked. Track
  B's resolver was built and tested for the 2-parcel case only; it has not been
  extended or verified for N>2.
- **Duplicate region detection on layout-overlapping pages** — `27211ae5` page 13
  produced two identical regions (same labels, same calls) for what appears to be one
  drawing, likely from the layout detector finding two overlapping boxes. Not
  investigated further.
- **Genuinely undimensioned parcels** — some parcels have no boundary dimensions
  anywhere in the source document for that specific parcel (confirmed by direct
  inspection of source pages, not assumed): their boundary is only implied by
  neighboring parcels' calls. This is a product/UX question (how to represent
  "cannot be extracted from this page" clearly), not an extraction bug to chase.
- **General accuracy beyond the NVZ document** — measured for the first time on the
  labeled 10-document corpus (real plats only, 3 runs pooled): **30% reach 3+
  parsed lines, 3% reach Valid** (up from 0% before traverse assembly). The
  remaining gap is extraction completeness on the read step itself, not geometry —
  a hand legibility check on every consistently-failing plat page found the
  dimension text readable by a human at the resolution the model already receives
  in every case; none were a resolution/crop problem. Two distinct sub-causes
  identified, not yet fixed:
  - a real capability/prompt gap even on simple, uncluttered plats — `2f896c95`
    p6 has 4 large, unambiguous bearings and zero curves, and still failed 0/3
    live runs.
  - dense multi-lot subdivision plats (`8d75d3ed` p10/p11/p39/p103) where each
    individual lot's own sides are scattered across a shared drawing and only 2-3
    of a lot's ~5 sides get attributed to it — a completeness problem curve
    support (above) doesn't fix on its own.
- **Whether a stronger read-step model is worth it** — an open, deliberately
  unscoped question. Evidence so far only supports testing it against the first
  sub-cause above (simple, fully-legible plats that still fail, like `2f896c95`
  p6) — not a general model swap, and not the subdivision-completeness problem,
  which is architectural rather than a model-strength question.

No further extraction fixes were made after this point in either session; the above
reflects a deliberate stopping point for re-scoping, not an exhaustive fix pass.
