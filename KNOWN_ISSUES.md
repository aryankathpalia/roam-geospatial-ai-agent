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
- **Sibling-boundary borrowing (single call)** — implemented, zero regressions,
  zero false positives on bucket-2, currently zero measured benefit on bucket-1
  because real subdivision lots in this corpus typically need 2+ borrowed sides,
  not 1. Multi-borrow extension not yet scoped.

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

## Fixed and committed this session (2026-09-29)

- **Calibrated-reprojection pivot used the wrong ring's bounding-box
  center.** `confirm_boundary` re-derived the reprojection pivot (the
  point the scale/rotation transform is applied around) from
  `vision_geometry.boundary_calls` -- the RAW, uncorrected vision-extracted
  calls -- instead of `resolved_boundary_calls`, the Track-B-corrected
  calls the pipeline actually walks to produce `boundary_geojson` (and
  therefore the ring the frontend's own `computeLocalTransform` pivots
  against when it first seeds pixel vertices). On NVZ this is the
  difference between a combined-tract 903.15'-wide rectangle (raw) and
  the correctly split 352.40'/550.75'-wide one (resolved) -- two rings
  with very different bounding-box centers. Pivoting around the wrong
  center is a translation error independent of the scale/rotation values
  themselves being correct: reprojecting with a perfectly-calibrated
  scale and rotation still lands the whole parcel on the wrong spot if
  the pivot point itself is wrong. Fixed by reading
  `resolved_boundary_calls` instead (`app/routes/documents.py`); it is
  never mutated by confirm-boundary itself, so this stays immune to the
  repeat-confirm self-contamination risk the raw-calls version was
  already built to avoid.
- **Edge-corroboration scale check discarded every edge with more than
  one nearby printed number, which is the common case, not the rare
  one.** `calibration.calibrate`'s independent edge-based scale estimate
  only trusted an edge if EXACTLY ONE OCR number proximity-matched it,
  on the theory that multiple candidates meant unresolvable ambiguity.
  In practice, real plats routinely print more than one number near the
  same physical line: a phantom combined-tract distance sharing the
  edge with its own corrected value (confirmed on NVZ: edge3 has both
  the true `550.75'` and the phantom `903.15'` within a few pixels of
  each other), or a parenthetical prior-deed reference bearing sitting
  next to the real one (confirmed on NVZ edge0: `220.00'` next to
  `(N0*02'31"W 250.00") (R4)`). The "exactly one candidate" rule
  silently excluded every edge that actually had a valid, correct label
  on it, leaving only edges whose sole nearby number was an unrelated
  stray (a legend figure, tax ID fragment) to vote on scale -- producing
  a nonsense "corroborated" scale that disagreed with the area-based one
  by 88% on a first live test, even though the document's real printed
  dimensions agreed with the area-based scale to within 0.3-2%. Fixed by
  using the area-based scale as a loose (20%) sanity prefilter among an
  edge's candidates, then taking whichever remaining candidate best
  matches that edge's own predicted length, instead of requiring
  single-candidacy (`app/services/calibration.py`). Confirmed on NVZ
  Parcel 1 (352.40' corroborates twice at 2.0-2.2% error, 220.00' at
  2.15%, the phantom 903.15' correctly rejected at 302% error) and
  Parcel 2 (550.75' at 0.30%, 220.00' at 0.37%, 903.15' rejected at
  64.5% error) -- both now reach `cross_validated`.

## Fixed and committed this session (2026-09-30)

- **The workspace map could render an uncalibrated parcel as trusted
  ("Verified", green).** `/workspace`'s `verdict()` and map-coloring logic
  only checked `spatial_validation.valid` (does the drawn shape close and
  match the stated area) and anchor precision -- neither has any
  awareness of `parcel.calibration.status`, the field that actually
  records whether a confirmed parcel's real-world scale/rotation were
  ever independently corroborated against the document's own printed
  evidence (see `app/services/calibration.py`, added earlier this
  session). A parcel whose calibration is `unverified` -- or one that
  was human-confirmed but never reached calibration at all, e.g. because
  its document has no usable anchor -- could still show the green
  "Verified" label and a green polygon on the real map if its shape
  happened to close and its anchor happened to be geocoded, since those
  are properties of the *shape*, not of whether its *placement* was ever
  checked. Fixed by adding `calibrationGate()`
  (`frontend/src/routes/(app)/workspace/+page.svelte`), which forces a
  distinct "Unconfirmed placement" state (new `.pill.unconfirmed` style,
  `frontend/src/app.css` -- deliberately not a shade of the existing
  red/amber states, since this is a different concern from shape
  validity or anchor precision) for any human-confirmed parcel whose
  calibration is `unverified` or missing, before either the shape or
  anchor checks run. Confirmed live: MAP 7 LOT 48 and LOT 48-3 (both
  `calibration.status: "unverified"`) now show gray "Unconfirmed
  placement" with a gray (`#6b6a63`) polygon, not green; NVZ Parcel 1 and
  Parcel 2 (both `cross_validated`) are unaffected and still show green
  "Verified" -- the fix doesn't over-trigger on genuinely placeable
  parcels.

## Still open, not fixed this session

- **A parcel's stored `spatial_validation` can silently go stale relative
  to its own stored `resolved_boundary_calls`.** Investigated after the
  Planned Utility Easement showed an apparent contradiction: its
  `assembly_notes` claimed dropping a call ("`S87°30'20"W 1358.92'`")
  left the remaining lines "closing and matching the stated acreage,"
  while its stored `spatial_validation` showed a 72% area mismatch.
  Direct reproduction (re-running `assemble_traverse` and `walk_traverse`
  on the exact stored `resolved_boundary_calls`, fresh) shows the
  assembly step's own claim is actually TRUE under current code -- 13.1%
  area difference, within the 15% `_AREA_TOLERANCE` gate
  (`app/services/geometry.py`) -- and does not reproduce the stored 72%
  figure at all. **The stored `spatial_validation` does not match a
  fresh recomputation from the document's own stored
  `resolved_boundary_calls`.** Checked the same way across every
  vertex-count-mismatched parcel found this session (see the topology
  entry below) and found the identical symptom in all 4 of 4: MAP 7 LOT
  48-3 (stored 9,120 sqft vs. fresh 18,451 sqft), Ada County Parcel 1
  (stored 26,994 sqft vs. fresh 77,218 sqft), Payette Parcel 1 (stored
  34,612 sqft vs. fresh 124,387 sqft), and the Easement (stored 12,393
  sqft vs. fresh 50,756 sqft). Every one of these documents currently
  carries a `spatial_validation` field that disagrees with what its own
  `resolved_boundary_calls` actually produce today -- not a rare
  coincidence, but the norm in this small sample. Root cause not
  pinned down: `assemble_traverse`/`walk_traverse`/`validate_traverse`
  are called together and store all three derived fields in the same
  statement in both `document_pipeline.py` call sites checked
  (`run_vision_stage` and `recompute_parcel_from_calls`), so nothing in
  the code as it exists today explains how they drifted apart -- this
  would need per-document processing history (which fields were
  overwritten, when, by which pipeline version) that isn't currently
  retained anywhere. **Do not treat a stored `result.json`'s
  `spatial_validation` as authoritative for a parcel without a live
  recompute against that same parcel's current `resolved_boundary_calls`
  first** -- confirmed unreliable in every case checked this session.

- **A human-confirmed boundary's vertex count routinely diverges from the
  original extracted traverse's -- 4 of 7 parcels checked this session,
  not a one-off.** Confirmed vertices vs. `resolved_boundary_calls`
  count: NVZ Parcel 1 (4 vs. 4, match), NVZ Parcel 2 (4 vs. 4, match),
  MAP 7 LOT 48 (7 vs. 7, match), **MAP 7 LOT 48-3 (6 vs. 5, mismatch),
  Planned Utility Easement (4 vs. 5, mismatch), Ada County Parcel 1 (4
  vs. 5, mismatch), Payette Parcel 1 (8 vs. 3, mismatch)**. This matters
  for calibration specifically: edge-based corroboration assumes a
  confirmed edge's nearest printed label is meaningful, which requires
  the confirmed shape's edges to roughly correspond to the original
  extraction's edges -- an assumption that silently breaks whenever a
  human redraws a materially different shape.
  Investigated per-parcel rather than assumed to share one cause, since
  they don't:
  - **Payette (8 vs. 3, the largest gap)**: the original extraction only
    found 3 calls for what must be a much more complex real boundary --
    a straightforward, already-documented extraction-completeness gap
    (see "General accuracy beyond the NVZ document" below). The human
    correctly traced the real, richer shape by hand. Not caused by any
    assembly decision.
  - **Ada County Parcel 1 and MAP 7 LOT 48-3**: same signature as
    Payette -- fresh recomputation of their stored `resolved_boundary_calls`
    still shows large area mismatches (44% and 75% respectively, see the
    stale-`spatial_validation` entry above), meaning the original
    extraction was genuinely incomplete/inaccurate for these too, not a
    borderline case an assembly tweak could have saved.
  - **Planned Utility Easement**: the one case traced to an actual
    assembly decision (dropping `S87°30'20"W 1358.92'`) -- but that
    decision is independently DEFENSIBLE under today's code (13.1% area
    agreement, within the 15% tolerance; see the stale-data entry
    above). The human's 4-vertex redraw doesn't correct a wrong
    assembly choice so much as suggest the original 6-call extraction
    had an extraneous or misattributed call in the first place (the
    easement may simply have 4 real sides, not 5) -- an extraction
    accuracy question, not an assembly-logic bug.
  Net: vertex-count mismatch is common and worth tracking as its own
  signal (e.g. surfaced distinctly from calibration status), but it is
  NOT one bug with one fix -- three of four cases trace to ordinary
  extraction incompleteness already tracked elsewhere in this document,
  and the fourth traces to the stale-`spatial_validation` issue above,
  not to a flaw in `assemble_traverse`'s own drop logic.

- **Pre-calibration confirmed placements used an unvalidated display-fit
  scale, not a measured one.** Before `app/services/calibration.py` existed,
  `confirm_boundary` reprojected confirmed vertices by inheriting whatever
  scale/rotation the original (possibly wrong) vision-extracted seed implied
  -- a scale chosen only to fit the seed ring into ~85% of the crop image for
  display, never checked against anything printed on the page. Confirmed
  concretely on NVZ Parcel 1: the pre-calibration scale was 0.490 ft/px, but
  independently deriving scale from stated acreage and cross-checking it
  against the sheet's own printed `352.40'`/`220.00'` calls (see
  predict-and-verify, and `calibration.py`) gives ~0.594-0.656 ft/px instead
  -- roughly a 20-30% discrepancy. The pre-calibration placement still looked
  approximately right on a zoomed-out satellite basemap (not obviously wrong
  at a glance), which is why this went unnoticed until the scale was actually
  checked against printed evidence. **Do not treat any pre-calibration
  confirmed boundary's stored coordinates as ground truth** -- only the
  calibration status (`cross_validated`/`single_source`/`unverified`) now
  stored per parcel indicates whether a placement's scale/rotation were
  actually checked against the document's own printed numbers.
- **A document's own printed graphic scale bar cannot be trusted as a
  calibration source, even when OCR reads it correctly.** Confirmed on NVZ:
  OCR cleanly read "GRAPHIC SCALE" + "1inch = 40 ft." off the page, which at
  this pipeline's 200 DPI render implies 0.20 ft/px -- but the real scale
  (independently corroborated: a confirmed boundary's edge matched a printed
  `220.00'` call within 0.5% error) is 0.705 ft/px, a 3.5x discrepancy.
  Likely cause: county-recorded sheets are frequently reduced from their
  original full-size plot (e.g. D-size to letter) before filing/scanning,
  which silently invalidates the printed scale-bar ratio relative to the
  rendered page's actual pixel scale. Reading the scale-bar text is not the
  hard part -- trusting its value without independently verifying the
  document wasn't rescaled is the risk. Do not use a printed scale
  annotation as a calibration source without a second, independent check
  (e.g. a stated acreage cross-check, as the predict-and-verify approach
  uses instead).
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
- **Duplicate region detection on layout-overlapping pages** — FIXED. Root-caused:
  the layout detector (documented as "NMS-free by design") emitted two overlapping
  boxes for one drawing on 13 region-pairs across 5 of the 10 corpus documents
  (`27211ae5`, `3affa529`, `8d75d3ed` x6, `510ea60f` x3, plus a live case on a Derry,
  NH lot-line-adjustment plat), confirmed at 79-97% IoU. Each duplicate got its own
  independent vision call, and since extraction is already non-deterministic run to
  run, the two came back with DIFFERENT incomplete readings instead of one correct
  one -- doubling vision cost and leaving the reviewer looking at whichever of two
  competing partial results happened to have data, with no indication a
  near-identical twin existed. Fixed with a safety-net NMS pass in
  `layout_detector_onnx.py` (IoU >= 0.6, same class -> keep only the higher-
  confidence box), applied before anything downstream sees the detections.
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
- **Phantom-label call misattribution** (`db54d473`, `27211ae5`, `798850fc`
  pattern) — root cause: structuring's generation step sometimes attributes real
  `boundary_calls` (belonging to a genuine parcel) to a second, illegitimate
  label (a citation, reference, or otherwise non-existent parcel) that happens to
  be enumerated. This is NOT detectable post-hoc: the misattributed calls are
  indistinguishable from real content once generated (same format, same
  plausibility, sometimes MORE complete than the real parcel's own calls), and
  the raw-notes context that might explain the mistake is itself non-deterministic
  across reruns (may or may not contain a citation marker near the phantom label,
  depending on tile-read variance).

  Four independent fix attempts, all failed, all reverted:
  1. Rule-1 Pattern A/B rewrite (explicit shared-edge handling in-prompt) —
     regressed real parcel extraction on previously-working pages.
  2. Enumeration-exclusion broadening alone (minimal, isolated version of #1) —
     also regressed real parcels, confirming the issue isn't prompt verbosity.
  3. Region-level content-density signal (bearing/distance token count per
     region) — doesn't target the bug at all; phantom labels can appear in
     content-rich regions.
  4. Per-label citation-context regex check (post-hoc, on stored structuring
     output) — mechanically unreliable: no position-linking field exists between
     a label and its supporting text, and even a string-search approach fails on
     the motivating case because the citation text is itself non-deterministic
     across tile-read runs, and misattributed calls carry no distinguishing
     signature from legitimate ones.

  Conclusion: this cannot be fixed by prompt engineering or post-hoc filtering
  with current architecture. A real fix would require either (a) structural
  change to how calls are linked to labels at generation time — e.g. requiring
  the model to cite which raw-notes line each call came from, creating
  traceability that doesn't currently exist — or (b) accepting this as a known
  failure mode and ensuring it's always caught downstream by existing validation
  (acreage mismatch, closure failure), which already happens today (these pages
  already correctly show Needs Review, never false-Valid). Not pursued further
  this session.

- **Georeferencing anchor selection picks the wrong address on documents with
  irrelevant text-heavy content** (`798850fc`'s pattern: a 29-page scanned
  packet — city council meeting minutes — with one embedded survey plat).
  `find_anchor_query` (`app/services/georeference.py`) scores every OCR'd
  line in the whole document for how address-like it looks and geocodes the
  winner; on this document the winner was City Hall's letterhead address from
  an unrelated page, not the parcel's own stated location, because both
  scored identically and the letterhead was encountered first.

  Three escalating attempts to fix this via proximity-to-the-plat-page and
  PLSS (section/township/range) context signals were tried and reverted:
  1. Proximity + PLSS + length-tiebreak as additive score bonuses — fixed
     the target case but regressed all 10 other corpus documents: a real,
     ZIP-qualified address elsewhere in a document started losing to a bare
     county name, or to a deed's "recorded in Book X, Page Y" sentence,
     because a bare 5-digit number (a book/page number, a date fragment)
     false-matches the ZIP regex and got boosted just for sitting near the
     plat page.
  2. Gating those bonuses on already-address-shaped text (state/county hint
     present) — cut it to 8 regressions, still broad.
  3. Restructuring so proximity/PLSS can only break an exact tie in the
     original score, never outrank a genuinely higher-scoring candidate —
     down to 1 real regression, but that regression was itself a new
     problem: a naive length-based tiebreak (shorter line wins) picked the
     survey firm's own office address (a different town, ~15 miles away)
     over city hall, with no regard for actual relevance.

  Reverted to the original scoring untouched. The one change kept: stripping
  a "City of X" / "Town of X" prefix before geocoding (Nominatim reliably
  returns zero results for "City of Payette, Idaho" but resolves "Payette,
  Payette County, Idaho" correctly) — confirmed safe (0/10 corpus documents'
  output changed) and confirmed to help in isolation, since it's applied
  after the original selection, not part of it.

  Root-caused the specific target case beyond the anchor bug itself: even
  with perfect anchor selection, `798850fc` p28 would still fail to
  georeference, because its winning candidate contains a real OCR typo
  ("Avefue" instead of "Avenue") that breaks Nominatim's matching regardless
  of phrasing or which line is chosen — confirmed directly (the
  correctly-spelled version geocodes; every garbled variant tested does
  not). No text-selection heuristic can fix a misread street name. Not
  pursued further this session.

No further extraction fixes were made after this point in either session; the above
reflects a deliberate stopping point for re-scoping, not an exhaustive fix pass.

## Gemini edge association for calibration (built, NOT yet validated on real documents)

`confirm_boundary` makes one extra Gemini call per confirm
(`app/services/gemini_edge_association.py`, gated by
`CALIBRATION_GEMINI_ASSOCIATION` + `GEMINI_API_KEY`): one padded crop, ONE
polygon drawn with numbered edges, one target parcel. Do not broaden to
multiple polygons / whole-sheet reads (tested worse twice). Candidates enter
`calibrate(extra_candidates=...)` and go through the same prefilter, 3% match,
5% scale agreement, rotation consensus and 180deg disambiguation as OCR ones.
Each corroboration is stored in `parcel.calibration.corroborations` with
`source` = `ocr_proximity` | `gemini_association` and shown in /boundary-review.
Failure (no key/quota) falls back to OCR-only with a note.

Status: unit-tested with mocks only (tests/test_gemini_edge_association.py).
The repeated-run evaluation on MAP 7 LOT 48 / LOT 48-3 and NVZ 1/2 still has to
be run against real documents + a live key; no results exist yet.
