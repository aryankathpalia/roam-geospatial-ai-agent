# Known issues

Status as of 2026-09-24, after a full session of diagnosis and fixes on the vision
extraction pipeline (`app/services/vision.py`, `app/services/geometry.py`,
`app/services/spatial_validation.py`, `app/pipeline/document_pipeline.py`). Evidence
for each item below came from live testing against real documents in
`data/documents/`, not assumption — see commit messages for the specific traces.

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
- **General accuracy beyond the NVZ document** — across a broader sample of 8 real
  documents (37 parcels total), 0 reached Valid. The fixes above are real and
  verified, but none of them individually close the gap between "extraction runs
  without crashing" and "extraction is correct" on documents structurally different
  from NVZ. This remains the primary open problem.

No further extraction fixes were made after this point in the session; the above
reflects a deliberate stopping point for re-scoping, not an exhaustive fix pass.
