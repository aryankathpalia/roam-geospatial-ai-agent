// Which parcels get offered for manual boundary confirmation, and in what order.
//
// The manual step comes AFTER ROAM's own ParcelMap selection and never
// replaces it: parcels only exist on regions the layout model classed
// ParcelMap (the only class sent to vision, see app/pipeline/page_ocr.py).
//
// Nothing here GATES on vision triage (region.category). The pipeline's own
// comment (document_pipeline.py, classify_regions) records that triage drops
// 9 of 23 real plats when used as a gate, and confirmed on a real
// application packet (WTDLP22-0003) that it hid the packet's own map. The
// category is only a sort key (never shown as a warning). The only exclusion is the
// pre-existing vicinity-inset duplicate flag (likely_duplicate_region).
//
// Application packets attach older recorded surveys they cite as references.
// Those are real ParcelMap sheets with real parcels, but they are not the
// target map. A sheet is marked "referenced" when another sheet cites its
// record number ("RECORD OF SURVEY MAP 966") and the sheet does not cite
// that number itself. That is a demotion (sorted last, badged), never a
// removal -- a text heuristic, checked on ONE document.

export type BoundaryRef = {
  key: string;
  pageNumber: number;
  regionIndex: number;
  // -1 for a parcel MAP item: the user can draw on it before ROAM has finished
  // reading which parcels it holds (the polygon is saved on the region and bound
  // to a parcel when extraction completes).
  parcelIndex: number;
  isRegion: boolean;
  // An outline drawn on this region's map before its parcels existed, still
  // waiting for the user to say which parcel it belongs to.
  pendingPolygon: any | null;
  label: string;
  parcel: any;
  category: string | null;
  regionArea: number;
  // Why this parcel is excluded from the default list, or null.
  excludedReason: string | null;
  // Set when this parcel's sheet is a survey cited by another sheet.
  referencedNote: string | null;
};

// primary: a sheet ROAM considers a likely target (triaged as a dimensioned plat, not a
// cited reference). Everything else is grouped under "Other maps" -- listed, never hidden.
export type BoundaryGroup = { pageNumber: number; referencedNote: string | null; primary: boolean; refs: BoundaryRef[] };

// Same-line matches only ([ \t], never \s): OCR line order is not reading
// order, so a title's last word and the next line's scale ("1"=400'") must
// not join. A number followed by a foot/inch mark is a measurement.
const CITE_RE =
  /(?:RECORD[ \t]+OF[ \t]+SURVEY|SURVEY[ \t]+MAP|PARCEL[ \t]+MAP)[ \t]*(?:MAP[ \t]*)?(?:NO\.?[ \t]*|#[ \t]*)?(\d{3,7})\b(?!['"′″.]?\d)(?!['"′″])/gi;

function pageText(page: any): string {
  return (page.regions ?? []).map((r: any) => r.ocr_text ?? '').join('\n');
}

function citedRecordNumbers(text: string): Set<string> {
  const out = new Set<string>();
  for (const m of text.matchAll(CITE_RE)) out.add(m[1]);
  return out;
}

// page number -> note, for sheets that other sheets cite as references.
export function referencedSheets(result: any): Map<number, string> {
  const pages: any[] = result?.pages ?? [];
  const texts = new Map<number, string>(pages.map((p) => [p.page_number, pageText(p)]));
  const cites = new Map<number, Set<string>>(pages.map((p) => [p.page_number, citedRecordNumbers(texts.get(p.page_number)!)]));
  const out = new Map<number, string>();
  for (const a of pages) {
    const textA = texts.get(a.page_number)!;
    if (!/SURVEY/i.test(textA)) continue;
    for (const b of pages) {
      if (b.page_number === a.page_number) continue;
      for (const n of cites.get(b.page_number)!) {
        if (cites.get(a.page_number)!.has(n)) continue; // a cites it too: not a pure reference
        if (new RegExp(`(^|[^\\d])${n}([^\\d]|$)`).test(textA)) {
          out.set(a.page_number, `Looks like the survey (No. ${n}) cited as a reference on page ${b.page_number}, not the target map.`);
        }
      }
    }
  }
  return out;
}

export function boundaryRefs(result: any, pinnedKey: string | null = null): BoundaryRef[] {
  const referenced = referencedSheets(result);
  const reading = result?.processing?.complete === false;
  const out: BoundaryRef[] = [];
  for (const page of result?.pages ?? []) {
    (page.regions ?? []).forEach((region: any, regionIndex: number) => {
      if (region.class !== 'ParcelMap' && !(region.parcels ?? []).length) return;
      const bbox = region.bbox ?? [0, 0, 0, 0];
      const common = {
        pageNumber: page.page_number,
        regionIndex,
        category: region.category ?? null,
        regionArea: bbox[2] * bbox[3],
        referencedNote: referenced.get(page.page_number) ?? null
      };
      const poly = region.confirmed_polygon ?? null;
      const parcels: any[] = region.parcels ?? [];
      const regionKey = `${page.page_number}-${regionIndex}-r`;
      // The map itself is an item until its parcels are read -- and stays one while
      // the user is working on it, so a list that refreshes underneath a drawing
      // never silently re-targets it at a parcel.
      if (!parcels.length || regionKey === pinnedKey) {
        out.push({
          ...common,
          key: regionKey,
          parcelIndex: -1,
          isRegion: true,
          pendingPolygon: null,
          label: reading && !parcels.length ? 'Parcel map · reading parcels…' : 'Parcel map',
          parcel: poly
            ? { human_confirmed: true, confirmed_boundary_pixels: poly, vision_geometry: {} }
            : { vision_geometry: {} },
          excludedReason: null
        });
      }
      parcels.forEach((parcel: any, parcelIndex: number) => {
        out.push({
          ...common,
          key: `${page.page_number}-${regionIndex}-${parcelIndex}`,
          parcelIndex,
          isRegion: false,
          pendingPolygon: poly && poly.needs_parcel ? poly : null,
          label:
            parcel?.vision_geometry?.parcel_label ||
            `Region ${regionIndex} · Parcel ${parcelIndex + 1}`,
          parcel,
          excludedReason: parcel?.likely_duplicate_region
            ? (parcel.duplicate_note ?? 'Likely a vicinity/locus-map duplicate of a parcel drawn elsewhere.')
            : null
        });
      });
    });
  }
  return out;
}

// Candidates grouped by sheet, most likely target first: sheets not cited as
// references, then sheets vision triage called a dimensioned plat, then
// larger drawings. Order only -- every group stays selectable.
export function groupCandidates(refs: BoundaryRef[]): BoundaryGroup[] {
  const groups = new Map<number, BoundaryGroup>();
  for (const r of refs) {
    if (r.excludedReason) continue;
    if (!groups.has(r.pageNumber)) {
      const primary = !r.referencedNote && (r.category === null || r.category === 'boundary_plat');
      groups.set(r.pageNumber, { pageNumber: r.pageNumber, referencedNote: r.referencedNote, primary, refs: [] });
    }
    groups.get(r.pageNumber)!.refs.push(r);
  }
  const rank = (g: BoundaryGroup) => {
    const first = g.refs[0];
    return [g.primary ? 0 : 1, g.referencedNote ? 1 : 0, -first.regionArea, g.pageNumber];
  };
  return [...groups.values()].sort((a, b) => {
    const ra = rank(a), rb = rank(b);
    for (let i = 0; i < ra.length; i++) if (ra[i] !== rb[i]) return ra[i] - rb[i];
    return 0;
  });
}

// What the workspace banner counts: parcels on likely-target sheets (not "Other maps").
export function primaryCandidates(refs: BoundaryRef[]): BoundaryRef[] {
  return groupCandidates(refs)
    .filter((g) => g.primary)
    .flatMap((g) => g.refs)
    .filter((r) => !r.isRegion);
}
