// Which parcels get offered for manual boundary confirmation.
//
// The manual step comes AFTER ROAM's own ParcelMap selection and never
// replaces it: parcels only exist on regions the layout model classed
// ParcelMap (the only class sent to vision, see app/pipeline/page_ocr.py),
// and of those, a candidate must also
//   - not be flagged as a vicinity/locus-inset duplicate
//     (likely_duplicate_region, see flag_spurious_duplicate_parcelmap_regions), and
//   - sit in a region vision triage called a real "boundary_plat" (or was
//     never triaged) -- not an undimensioned drawing (assessor/site plan)
//     or not_a_parcel_drawing (see vision.classify_regions).
// Everything else is still listed separately, one click away, never
// silently dropped -- region["category"] is a display label, not a gate.

export type BoundaryRef = {
  key: string;
  pageNumber: number;
  regionIndex: number;
  parcelIndex: number;
  label: string;
  parcel: any;
  // Why this parcel is not a default candidate, or null if it is one.
  excludedReason: string | null;
};

export function boundaryRefs(result: any): BoundaryRef[] {
  const out: BoundaryRef[] = [];
  for (const page of result?.pages ?? []) {
    (page.regions ?? []).forEach((region: any, regionIndex: number) => {
      (region.parcels ?? []).forEach((parcel: any, parcelIndex: number) => {
        let excludedReason: string | null = null;
        if (parcel?.likely_duplicate_region) {
          excludedReason =
            parcel.duplicate_note ?? 'Likely a vicinity/locus-map duplicate of a parcel drawn elsewhere.';
        } else if (region.category && region.category !== 'boundary_plat') {
          excludedReason =
            region.category === 'undimensioned_drawing'
              ? 'Region looks like a drawing without bearing/distance labels (assessor map, site plan, ...).'
              : 'Region does not look like a parcel drawing.';
        }
        out.push({
          key: `${page.page_number}-${regionIndex}-${parcelIndex}`,
          pageNumber: page.page_number,
          regionIndex,
          parcelIndex,
          label:
            parcel?.vision_geometry?.parcel_label ||
            `Page ${page.page_number} · Region ${regionIndex} · Parcel ${parcelIndex + 1}`,
          parcel,
          excludedReason
        });
      });
    });
  }
  return out;
}
