// Which sheets and parcels get offered for manual boundary confirmation, and in what order.
//
//   REGION -> SHEET -> PARCEL(S)
//
// A ParcelMap is a SHEET (a page), not a region, and a PARCEL is not a map: the layout model may box one page several
// times (the main drawing, a vicinity inset, a detail), and a sheet may hold several parcels
// (a lot and its remainder). So the unit offered to the user is the sheet -- ONE candidate
// per page that holds a parcel map -- with its parcels nested under it, and the boundary is
// drawn on the sheet's main drawing and then assigned to one of those parcels.
//
// What the user confirms is a PARCEL -- "Parcel 1", "Remainder Parcel" -- a first-class entity
// owned by the sheet (page.sheet.parcels[]), read early by the roster call and holding its own
// confirmed polygon. The sheet is only the shared drawing canvas. Entities exist from the
// roster on; before that the sheet shows "Reading parcel labels…", and if the roster is empty
// or failed the user can outline a parcel and NAME it by hand (marked as manually named).
// Documents processed before entities existed (no sheet.parcels) fall back to the extracted
// region.parcels.
//
// The backend builds the sheet (app/pipeline/document_pipeline.py::build_sheets: largest
// ParcelMap region = main drawing, regions >= 20% of it are sibling drawings, smaller ones
// are insets folded into the sheet) and asks Gemini for the sheet's ROLE in its packet
// (target_parcel_map | reference_survey | other_parcel_drawing | location_or_aerial_map |
// not_a_map). Documents processed before sheets existed have no `page.sheet`; the same rule
// is re-derived here, with no role.
//
// Nothing GATES on the role. A target sheet is a "likely target"; every other role is
// listed under "Other maps" with the model's reason -- never hidden (the pipeline's own
// measurement is that triage used as a gate drops 9 of 23 real plats). A sheet the model
// could not judge has no role and counts as a candidate. The only exclusions are the
// pre-existing duplicate-inset flag (likely_duplicate_region) and parcels read off a small
// inset region of a sheet.
//
// (An earlier OCR heuristic -- "this sheet's record number is cited on another sheet, so it is a
// reference survey" -- was removed: on the real NVZ packet it demoted the true parcel sheets,
// whose own notes cite the same records ("as shown on RS 5122"). Telling "is survey N" from
// "mentions survey N" takes a look at the sheet, which is what the role does.)

export const SHEET_INSET_AREA_FRACTION = 0.2;

export const ROLE_LABELS: Record<string, string> = {
  reference_survey: 'reference survey',
  other_parcel_drawing: 'other drawing',
  location_or_aerial_map: 'location / aerial map',
  not_a_map: 'not a map'
};

export type SheetInfo = {
  role: string | null;
  reason: string | null;
  mainRegion: number;
  regions: number[]; // drawing regions of the sheet (main first in order of index)
  insetRegions: number[];
};

export type BoundaryRef = {
  key: string;
  pageNumber: number;
  // The region (crop) the outline is drawn on.
  regionIndex: number;
  // THE PARCEL's stable id (a sheet entity); null for legacy extracted parcels and for the
  // "name it by hand" row.
  parcelId: string | null;
  // Legacy: index into region.parcels (-1 for entities that have no extracted parcel yet).
  parcelIndex: number;
  // parcel: a real, selectable parcel | reading: the roster is still being read (not selectable) |
  // manual_new: no parcel was read -- draw an outline and name it.
  kind: 'parcel' | 'reading' | 'manual_new';
  label: string;
  // "17-2-1-4 · 120.4 AC.±": only what the sheet prints.
  meta: string | null;
  // True when the user named it: identity was not detected automatically.
  manual: boolean;
  // An approximate point inside the parcel, as fractions of its region's crop (canvas).
  point: [number, number] | null;
  // The sheet entity (null for legacy parcels).
  entity: any | null;
  // The extracted/derived parcel record (geometry, calibration, placement), or a stub.
  parcel: any;
  role: string | null;
  sheetReason: string | null;
  regionArea: number;
  // Why this parcel is excluded from the default list, or null.
  excludedReason: string | null;
};

// One sheet: its parcels (or its map item, before they are read) nested under one heading.
// primary: a likely target (role target_parcel_map, or not judged).
export type BoundaryGroup = {
  pageNumber: number;
  role: string | null;
  primary: boolean;
  badge: string | null;
  note: string | null;
  refs: BoundaryRef[];
};

// The page's sheet: from the backend when present, else re-derived from its regions.
export function sheetOf(page: any): SheetInfo | null {
  const regions: any[] = page.regions ?? [];
  const given = page.sheet;
  if (given) {
    return {
      role: given.role ?? null,
      reason: given.reason ?? null,
      mainRegion: given.main_region,
      regions: given.regions ?? [given.main_region],
      insetRegions: given.inset_regions ?? []
    };
  }
  const maps = regions
    .map((r, i) => ({ r, i, area: (r.bbox?.[2] ?? 0) * (r.bbox?.[3] ?? 0) }))
    .filter(({ r }) => r.class === 'ParcelMap' || (r.parcels ?? []).length);
  if (!maps.length) return null;
  const main = maps.reduce((best, m) => (m.area > best.area ? m : best));
  const drawings = maps.filter((m) => m.i === main.i || m.area >= SHEET_INSET_AREA_FRACTION * main.area).map((m) => m.i);
  const legacy = main.r.category;
  return {
    role: legacy === 'not_a_parcel_drawing' ? 'location_or_aerial_map' : legacy === 'undimensioned_drawing' ? 'other_parcel_drawing' : null,
    reason: null,
    mainRegion: main.i,
    regions: drawings.sort((a, b) => a - b),
    insetRegions: maps.filter((m) => !drawings.includes(m.i)).map((m) => m.i)
  };
}

export function boundaryRefs(result: any): BoundaryRef[] {
  const out: BoundaryRef[] = [];
  for (const page of result?.pages ?? []) {
    const sheet = sheetOf(page);
    if (!sheet) continue;
    const regions: any[] = page.regions ?? [];
    const main = regions[sheet.mainRegion];
    const bbox = main?.bbox ?? [0, 0, 0, 0];
    const common = {
      pageNumber: page.page_number,
      role: sheet.role,
      sheetReason: sheet.reason,
      regionArea: bbox[2] * bbox[3]
    };
    const entities: any[] | undefined = page.sheet?.parcels;
    const rosterStatus: string | undefined = page.sheet?.roster?.status;
    const meta = (e: any) =>
      [e.printed_id, e.stated_area].filter(Boolean).join(' · ') || null;
    const before = out.length;

    if (entities) {
      for (const e of entities) {
        const ref = e.evidence_ref;
        const evidence = ref ? regions[ref.region]?.parcels?.[ref.parcel] : null;
        const poly = e.confirmed_polygon ?? null;
        const regionIndex = ref ? ref.region : (e.region_index ?? sheet.mainRegion);
        out.push({
          ...common,
          key: `${page.page_number}-${e.id}`,
          regionIndex,
          parcelId: e.id,
          parcelIndex: ref ? ref.parcel : -1,
          kind: 'parcel',
          label: e.label,
          meta: meta(e),
          manual: !!e.manual,
          point: e.point ?? null,
          entity: e,
          // The record the rest of the app uses; a stub until extraction has produced one.
          parcel: evidence ?? {
            vision_geometry: { parcel_label: e.label },
            human_confirmed: !!poly,
            confirmed_boundary_pixels: poly
          },
          excludedReason: e.excluded_reason ?? null
        });
      }
    } else {
      // Documents processed before parcel entities existed: the extracted parcels, as before.
      const parcelItem = (regionIndex: number, parcelIndex: number, excluded: string | null): BoundaryRef => {
        const parcel = regions[regionIndex].parcels[parcelIndex];
        return {
          ...common,
          key: `${page.page_number}-${regionIndex}-${parcelIndex}`,
          regionIndex,
          parcelId: null,
          parcelIndex,
          kind: 'parcel',
          label: parcel?.vision_geometry?.parcel_label || `Region ${regionIndex} · Parcel ${parcelIndex + 1}`,
          meta: null,
          manual: false,
          point: null,
          entity: null,
          parcel,
          excludedReason:
            excluded ??
            (parcel?.likely_duplicate_region
              ? (parcel.duplicate_note ?? 'Likely a vicinity/locus-map duplicate of a parcel drawn elsewhere.')
              : null)
        };
      };
      for (const ri of sheet.regions) (regions[ri]?.parcels ?? []).forEach((_: any, pi: number) => out.push(parcelItem(ri, pi, null)));
      for (const ri of sheet.insetRegions)
        (regions[ri]?.parcels ?? []).forEach((_: any, pi: number) =>
          out.push(parcelItem(ri, pi, 'Read from a small inset of this sheet (likely a vicinity or detail map), not its main drawing.'))
        );
    }

    // Nothing to pick yet: say so, or offer the hand-named outline. Never a generic "map" item.
    if (out.length === before) {
      const reading = result?.processing?.complete === false && (!entities || rosterStatus === 'pending' || rosterStatus === undefined);
      out.push({
        ...common,
        key: `${page.page_number}-${reading ? 'reading' : 'new'}`,
        regionIndex: sheet.mainRegion,
        parcelId: null,
        parcelIndex: -1,
        kind: reading ? 'reading' : 'manual_new',
        label: reading ? 'Reading parcel labels…' : 'Draw an outline and name it',
        meta: reading ? null : 'no parcel was detected on this sheet',
        manual: false,
        point: null,
        entity: null,
        parcel: { vision_geometry: {} },
        excludedReason: null
      });
    }
  }
  return out;
}

// Candidate SHEETS, most likely target first (target or not-yet-judged sheets, then every
// other role), larger drawings first. Order only -- every sheet stays selectable, and a sheet
// with several parcels is still ONE group.
export function groupCandidates(refs: BoundaryRef[]): BoundaryGroup[] {
  const groups = new Map<number, BoundaryGroup>();
  for (const r of refs) {
    if (r.excludedReason) continue;
    if (!groups.has(r.pageNumber)) {
      const primary = r.role === null || r.role === 'target_parcel_map';
      groups.set(r.pageNumber, {
        pageNumber: r.pageNumber,
        role: r.role,
        primary,
        badge: primary ? null : (ROLE_LABELS[r.role!] ?? 'other map'),
        note: primary ? null : r.sheetReason,
        refs: []
      });
    }
    groups.get(r.pageNumber)!.refs.push(r);
  }
  const area = new Map(refs.map((r) => [r.pageNumber, r.regionArea]));
  return [...groups.values()].sort(
    (a, b) =>
      Number(!a.primary) - Number(!b.primary) ||
      (area.get(b.pageNumber) ?? 0) - (area.get(a.pageNumber) ?? 0) ||
      a.pageNumber - b.pageNumber
  );
}

// What the workspace banner counts: parcels on likely-target sheets (not "Other maps").
export function primaryCandidates(refs: BoundaryRef[]): BoundaryRef[] {
  return groupCandidates(refs)
    .filter((g) => g.primary)
    .flatMap((g) => g.refs)
    .filter((r) => r.kind === 'parcel');
}
