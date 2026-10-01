// Which sheets and parcels get offered for manual boundary confirmation, and in what order.
//
//   REGION -> SHEET -> PARCEL(S)
//
// A ParcelMap is a SHEET (a page), not a region: the layout model may box one page several
// times (the main drawing, a vicinity inset, a detail), and a sheet may hold several parcels
// (a lot and its remainder). So the unit offered to the user is the sheet -- ONE candidate
// per page that holds a parcel map -- with its parcels nested under it, and the boundary is
// drawn on the sheet's main drawing and then assigned to one of those parcels.
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
  regionIndex: number;
  // -1 for the sheet's MAP item: the user can draw on it before ROAM has finished reading
  // which parcels it holds (the polygon is saved on the region and bound to a parcel when
  // extraction completes).
  parcelIndex: number;
  isRegion: boolean;
  // An outline drawn on this region's map before its parcels existed, still waiting for the
  // user to say which parcel it belongs to.
  pendingPolygon: any | null;
  label: string;
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

export function boundaryRefs(result: any, pinnedKey: string | null = null): BoundaryRef[] {
  const reading = result?.processing?.complete === false;
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

    const parcelItem = (regionIndex: number, parcelIndex: number, excludedReason: string | null): BoundaryRef => {
      const region = regions[regionIndex];
      const parcel = region.parcels[parcelIndex];
      const poly = region.confirmed_polygon ?? null;
      return {
        ...common,
        regionIndex,
        key: `${page.page_number}-${regionIndex}-${parcelIndex}`,
        parcelIndex,
        isRegion: false,
        pendingPolygon: poly && poly.needs_parcel ? poly : null,
        label: parcel?.vision_geometry?.parcel_label || `Region ${regionIndex} · Parcel ${parcelIndex + 1}`,
        parcel,
        excludedReason:
          excludedReason ??
          (parcel?.likely_duplicate_region
            ? (parcel.duplicate_note ?? 'Likely a vicinity/locus-map duplicate of a parcel drawn elsewhere.')
            : null)
      };
    };

    const sheetParcels = sheet.regions.reduce((n, ri) => n + (regions[ri]?.parcels ?? []).length, 0);
    const regionKey = `${page.page_number}-${sheet.mainRegion}-r`;
    // The sheet's map is an item until its parcels are read -- and stays one while the user
    // is working on it, so a list that refreshes underneath a drawing never silently
    // re-targets it at a parcel.
    if (!sheetParcels || regionKey === pinnedKey) {
      const poly = main?.confirmed_polygon ?? null;
      out.push({
        ...common,
        regionIndex: sheet.mainRegion,
        key: regionKey,
        parcelIndex: -1,
        isRegion: true,
        pendingPolygon: null,
        label: reading && !sheetParcels ? 'Parcel map · reading parcels…' : 'Parcel map',
        parcel: poly ? { human_confirmed: true, confirmed_boundary_pixels: poly, vision_geometry: {} } : { vision_geometry: {} },
        excludedReason: null
      });
    }
    for (const ri of sheet.regions) {
      (regions[ri]?.parcels ?? []).forEach((_: any, pi: number) => out.push(parcelItem(ri, pi, null)));
    }
    for (const ri of sheet.insetRegions) {
      (regions[ri]?.parcels ?? []).forEach((_: any, pi: number) =>
        out.push(parcelItem(ri, pi, 'Read from a small inset of this sheet (likely a vicinity or detail map), not its main drawing.'))
      );
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
    .filter((r) => !r.isRegion);
}
