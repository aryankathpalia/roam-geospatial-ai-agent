<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { boundaryRefs, groupCandidates, type BoundaryRef } from '$lib/boundaryCandidates';
  import * as curveLib from '$lib/curves';

  // Boundary confirmation step of the main flow: /workspace links here
  // (?doc=<id>[&parcel=<page-region-parcel>]) after a document is
  // processed, and "Back to map" returns to /workspace?doc=<id>.
  //
  // Originally a prototype screen: confirm-and-edit boundary review.
  //
  // Separate from the main workspace page on purpose (see scoping
  // discussion) -- this isolates the vertex-drag interaction so it can
  // be fixed/validated without touching the live review flow. The
  // existing Leaflet drag editor on the workspace page edits vertices
  // by recomputing bearing/distance to the NEXT point and re-walking
  // the traverse forward from there, which cascades a single drag
  // through every downstream vertex. This screen edits vertices in
  // absolute pixel space instead: dragging one point only moves that
  // point and its two adjacent edges, nothing else shifts.
  //
  // Reuses existing routes: GET /documents/{id}, GET .../crop.png, and
  // a new POST /documents/{id}/confirm-boundary (see
  // app/routes/documents.py) that stores the confirmed polygon as-is,
  // with no attempt to back-derive bearing/distance calls from it.

  const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';
  const LAST_DOCUMENT_KEY = 'roam:lastDocumentId';

  let documentId = '';
  let loadError = '';
  let loading = false;
  let result: any = null;

  type ParcelRef = BoundaryRef;

  let parcelRefs: ParcelRef[] = [];
  let selectedKey: string | null = null;
  $: selected = parcelRefs.find((p) => p.key === selectedKey) ?? null;

  let cropSrc = '';
  let cropWidth = 0;
  let cropHeight = 0;
  let cropImgEl: HTMLImageElement;

  // Vertices in crop-pixel space, [x, y] each. Edited in absolute
  // space -- see header comment.
  let vertices: [number, number][] = [];
  // Curved edges: edge i (vertex i -> i+1) -> its quadratic control point in the edge's own frame
  // (see $lib/curves). Straight edges are simply absent.
  let curves: curveLib.Curves = {};
  let curveMode = false;
  let selectedEdgeIdx: number | null = null;
  let draggingWaypoint: { edge: number; k: number } | null = null;
  let draggingIdx: number | null = null;
  let selectedVertexIdx: number | null = null;

  // ---- Drawing tools (edit = drag corners; pen = click corners in order; rect = drag a box;
  // fill = click inside a lot to take the region its lines enclose; move = drag to move, drag outside
  // the shape to rotate it). Undo/redo covers every change; zoom with the wheel, pan with the right
  // mouse button or Space+drag.
  type Tool = 'edit' | 'pen' | 'rect' | 'fill' | 'move' | 'hand';
  const TOOLS: [Tool, string, string, string][] = [
    ['edit', 'Edit', 'V', 'Drag corners; double-click an edge to add one'],
    ['pen', 'Draw', 'P', 'Click each corner in order; click the first corner or press Enter to close. Shift locks 45/90 degrees'],
    ['rect', 'Rectangle', 'R', 'Drag a box'],
    ['fill', 'Fill', 'F', 'Click inside a lot: takes the region its lines enclose'],
    ['move', 'Move / rotate', 'M', 'Drag inside the shape to move it, outside to rotate it'],
    ['hand', 'Hand', 'H', 'Drag to move around the drawing (also: drag empty paper in Edit, or two-finger swipe)']
  ];
  let tool: Tool = 'edit';
  const TOOL_ICONS: Record<Tool, string> = {
    edit: 'M4 20l4.5-1 10-10a2.1 2.1 0 0 0-3-3l-10 10L4 20zM14 7l3 3',
    pen: 'M12 19l7-7 3 3-7 7-3-3zM18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5zM2 2l7.6 7.6M11 13a2 2 0 1 0 0-4 2 2 0 0 0 0 4z',
    rect: 'M4 5h16v14H4z',
    fill: 'M19 11l-8-8-8.5 8.5a2.1 2.1 0 0 0 0 3L7 19a2.1 2.1 0 0 0 3 0L19 11zM5 2l5 5M20 15s2 2.4 2 4a2 2 0 0 1-4 0c0-1.6 2-4 2-4z',
    move: 'M5 9l-3 3 3 3M9 5l3-3 3 3M15 19l-3 3-3-3M19 9l3 3-3 3M2 12h20M12 2v20',
    hand: 'M18 11V6a2 2 0 0 0-4 0v5M14 10V4a2 2 0 0 0-4 0v6M10 10.5V6a2 2 0 0 0-4 0v8a8 8 0 0 0 16 0v-2a2 2 0 0 0-4 0'
  };
  // display options and layout
  let showVertices = true;
  let showOthers = true;
  let centreFull = false;
  let simplifyMsg = '';
  let penPoints: [number, number][] = [];
  let cursorPt: [number, number] | null = null;
  let rectStart: [number, number] | null = null;
  let fillGap = 5;
  let fillBusy = false;
  let fillMsg = '';
  let snapOn = true;
  let moveDrag: {
    mode: 'move' | 'rotate';
    start: [number, number];
    orig: [number, number][];
    centre: [number, number];
  } | null = null;
  type Snapshot = { vertices: [number, number][]; curves: curveLib.Curves };
  let undoStack: Snapshot[] = [];
  let redoStack: Snapshot[] = [];
  let zoom = 1;
  let pan: [number, number] = [0, 0];
  let panDrag: { start: [number, number]; orig: [number, number]; moved: boolean } | null = null;
  // two-finger touch: pinch to zoom, move both fingers to pan
  let touches = new Map<number, [number, number]>();
  let pinch: { dist: number; mid: [number, number]; zoom: number; pan: [number, number] } | null = null;
  let spaceDown = false;
  let stageEl: HTMLDivElement;
  let stageW = 0;
  // drawing units per screen pixel: handles are sized in SCREEN pixels, so a corner stays easy to grab
  // (and wins over the edge under it) however large the sheet or however far it is zoomed
  $: px = cropWidth && stageW ? cropWidth / (stageW * zoom) : 1;
  let svgEl: SVGSVGElement;

  let saveStatus: 'idle' | 'saving' | 'saved' | 'error' = 'idle';
  let seedSource: 'confirmed' | 'traverse' | 'none' = 'none';

  // The exact affine used to seed pixel vertices from the parcel's
  // original local (feet, anchor-relative) traverse ring -- kept so a
  // confirmed shape can be projected BACK into that same local-feet
  // system on save (see toLocalVertices). Forward and inverse use the
  // identical scale/midX/midY/cx/cy, so any vertex the user never
  // touched round-trips to its EXACT original local coordinate; only
  // dragged/added vertices get an approximate one (at the same scale
  // the seed was displayed at). Null when this parcel has no original
  // traverse ring to anchor against -- confirming then saves a shape
  // with no real-world projection possible.
  let localTransform: { scale: number; cx: number; cy: number; midX: number; midY: number } | null =
    null;

  onMount(() => {
    const params = new URLSearchParams(window.location.search);
    const docParam = params.get('doc');
    if (docParam) {
      documentId = docParam;
      loadDocument(params.get('parcel'));
      return;
    }
    try {
      const last = localStorage.getItem(LAST_DOCUMENT_KEY);
      if (last) documentId = last;
    } catch {
      // ignore -- localStorage unavailable, not fatal
    }
  });

  async function loadDocument(preferredKey: string | null = null) {
    if (!documentId.trim()) return;
    loading = true;
    loadError = '';
    result = null;
    parcelRefs = [];
    selectedKey = null;
    try {
      const res = await fetch(`${API_BASE}/documents/${documentId.trim()}`);
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const data = await res.json();
      result = data.result;
      parcelRefs = flattenParcels(result);
      try {
        localStorage.setItem(LAST_DOCUMENT_KEY, documentId.trim());
      } catch {
        // best-effort only
      }
      // A parcel asked for by key is brought into the list even if it was
      // excluded by default -- the user chose it explicitly.
      const excludedMatch = preferredKey ? excludedParcels.find((r) => r.key === preferredKey) : null;
      if (excludedMatch) restoreExcludedParcel(excludedMatch);
      const preferred = preferredKey ? parcelRefs.find((r) => r.key === preferredKey) : null;
      if (preferred) {
        selectParcel(preferred);
      } else if (parcelRefs.length > 0) {
        selectParcel(parcelRefs.find((r) => r.kind !== 'reading') ?? parcelRefs[0]);
      }
      if (result?.processing?.complete === false) startReadingPoll();
    } catch (err: any) {
      loadError = err?.message ?? 'Failed to load document';
    } finally {
      loading = false;
    }
  }

  // The list is SHEETS with their PARCELS nested (see $lib/boundaryCandidates): the parcels are
  // what you confirm, the sheet is the shared drawing canvas. Sheets are ordered most likely
  // target first; nothing is hidden.
  function flattenParcels(res: any): ParcelRef[] {
    const all = boundaryRefs(res);
    excludedParcels = all.filter((r) => r.excludedReason);
    return groupCandidates(all).flatMap((g) => [...g.refs, ...g.duplicates.flatMap((d) => d.refs)]);
  }

  // Sheets that repeat another sheet's parcels are folded under it (see $lib/boundaryCandidates);
  // "show" lists them again, marked as copies.
  let expandedCopies = new Set<number>();
  function toggleCopies(page: number) {
    const next = new Set(expandedCopies);
    if (next.has(page)) next.delete(page);
    else next.add(page);
    expandedCopies = next;
  }
  $: parcelGroups = groupCandidates(parcelRefs);
  $: displayGroups = parcelGroups.flatMap((g) => [
    { ...g, copyOf: null as number | null },
    ...(expandedCopies.has(g.pageNumber) ? g.duplicates.map((d) => ({ ...d, copyOf: g.pageNumber as number | null })) : [])
  ]);
  $: reading = result?.processing?.complete === false;

  // The pipeline keeps reading the document (OCR, anchor, parcel extraction) while
  // the user draws. Refresh the list underneath them; their drawing (`vertices`)
  // and the map item they are drawing on (pinned) are never reset.
  let readingTimer: ReturnType<typeof setInterval> | null = null;
  let readingStartedAt: number | null = null;
  // "Still reading" can mean exactly that, OR it can mean the backend
  // process that was running this document's pipeline is gone (a dev-
  // server restart, a crash) and nothing will ever finish it -- the two
  // look identical from here (GET .../progress 404s either way; see
  // documents.py's reprocess_document docstring for how this was
  // confirmed on a real stuck document). There's no reliable way to tell
  // them apart from the saved state alone, so after a while just offer
  // the escape hatch rather than pretend everything is fine.
  const STUCK_HINT_AFTER_MS = 90_000;
  let showReprocessHint = false;
  function startReadingPoll() {
    if (readingTimer) return;
    readingStartedAt = Date.now();
    showReprocessHint = false;
    readingTimer = setInterval(() => {
      refreshDocument();
      if (readingStartedAt && Date.now() - readingStartedAt > STUCK_HINT_AFTER_MS) {
        showReprocessHint = true;
      }
    }, 3000);
  }
  function stopReadingPoll() {
    if (readingTimer) clearInterval(readingTimer);
    readingTimer = null;
    readingStartedAt = null;
    showReprocessHint = false;
  }
  onDestroy(stopReadingPoll);

  let reprocessing = false;
  async function reprocessDocument() {
    reprocessing = true;
    try {
      const res = await fetch(`${API_BASE}/documents/${documentId.trim()}/reprocess`, { method: 'POST' });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      readingStartedAt = Date.now();
      showReprocessHint = false;
    } catch (err) {
      loadError = err instanceof Error ? err.message : 'Failed to restart processing.';
    } finally {
      reprocessing = false;
    }
  }

  async function refreshDocument() {
    try {
      const res = await fetch(`${API_BASE}/documents/${documentId.trim()}`);
      if (!res.ok) return;
      const data = await res.json();
      result = data.result;
      const wasReading = selected?.kind === 'reading';
      parcelRefs = flattenParcels(result);
      if (wasReading && !parcelRefs.some((r) => r.key === selectedKey)) {
        // The parcel labels arrived while the map was showing: move to the first real parcel.
        const first = parcelRefs.find((r) => r.pageNumber === (selected?.pageNumber ?? -1) && r.kind !== 'reading');
        if (first) selectParcel(first);
      }
      if (data.status === 'processed') stopReadingPoll();
    } catch {
      // transient -- next tick
    }
  }

  let excludedParcels: ParcelRef[] = [];

  function restoreExcludedParcel(ref: ParcelRef) {
    excludedParcels = excludedParcels.filter((r) => r.key !== ref.key);
    parcelRefs = [...parcelRefs, ref];
  }

  let deleteError = '';

  // Correcting a misread printed area (a blurred label): saved on the sheet entity, rechecked server-side.
  let editingArea = false;
  let areaDraft = '';
  let areaError = '';
  let areaSaving = false;
  $: selectedEntity = selected?.parcelId
    ? (result?.pages ?? []).find((p: any) => p.page_number === selected?.pageNumber)?.sheet?.parcels?.find(
        (e: any) => e.id === selected?.parcelId
      ) ?? null
    : null;
  $: if (selected?.key !== areaEditKey) {
    editingArea = false;
    areaError = '';
    areaEditKey = selected?.key ?? null;
  }
  let areaEditKey: string | null = null;

  function startAreaEdit() {
    areaDraft = selectedEntity?.stated_area ?? '';
    areaError = '';
    editingArea = true;
  }

  async function saveStatedArea() {
    if (!selected?.parcelId) return;
    areaSaving = true;
    areaError = '';
    try {
      const res = await fetch(`${API_BASE}/documents/${documentId.trim()}/stated-area`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ page_number: selected.pageNumber, entity_id: selected.parcelId, stated_area: areaDraft })
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail ?? `${res.status} ${res.statusText}`);
      }
      editingArea = false;
      await refreshDocument();
    } catch (err) {
      areaError = err instanceof Error ? err.message : 'Could not save the area.';
    } finally {
      areaSaving = false;
    }
  }

  // Removes a spurious, never-confirmed sheet entity (a scattered extraction
  // fragment, a duplicate from another sheet, or anything else the
  // excluded_reason heuristic didn't catch). The backend refuses this for a
  // parcel that already has a confirmed_polygon, so real work can't be lost
  // through this button.
  async function deleteParcelEntity(ref: ParcelRef) {
    if (!ref.parcelId) return;
    if (!confirm(`Delete "${ref.label}" from page ${ref.pageNumber}? This cannot be undone.`)) return;
    deleteError = '';
    try {
      const res = await fetch(
        `${API_BASE}/documents/${documentId.trim()}/pages/${ref.pageNumber}/parcels/${ref.parcelId}`,
        { method: 'DELETE' }
      );
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail ?? `${res.status} ${res.statusText}`);
      }
      excludedParcels = excludedParcels.filter((r) => r.key !== ref.key);
      parcelRefs = parcelRefs.filter((r) => r.key !== ref.key);
      if (selectedKey === ref.key) selectedKey = null;
    } catch (err) {
      deleteError = err instanceof Error ? err.message : 'Failed to delete parcel.';
    }
  }

  let cropLoadToken = 0;

  let manualName = '';

  async function selectParcel(ref: ParcelRef) {
    selectedKey = ref.key;
    manualName = '';
    saveStatus = 'idle';
    selectedVertexIdx = null;
    undoStack = [];
    redoStack = [];
    penPoints = [];
    rectStart = null;
    zoom = 1;
    pan = [0, 0];
    fillMsg = '';
    simplifyMsg = '';
    const src = `${API_BASE}/documents/${documentId.trim()}/pages/${ref.pageNumber}/regions/${ref.regionIndex}/crop.png`;
    cropSrc = src;
    cropWidth = 0;
    cropHeight = 0;
    vertices = [];
    seedSource = 'none';

    // Determine the image's real dimensions with a dedicated probe
    // Image, not the displayed <img>/on:load -- checking the displayed
    // element's `.complete`/`naturalWidth` right after a reactive src
    // change is inherently racy (the browser can report a transitional,
    // inconsistent state -- complete:false with a stale naturalWidth --
    // for a moment after the attribute changes, and how long that
    // takes is not deterministic). A fresh Image object has no such
    // shared lifecycle to race with: either it's already cached
    // (resolves synchronously) or `load`/`error` fires exactly once.
    const token = ++cropLoadToken;
    const probe = new Image();
    probe.src = src;
    if (!(probe.complete && probe.naturalWidth)) {
      await new Promise<void>((resolve) => {
        probe.onload = () => resolve();
        probe.onerror = () => resolve();
      });
    }
    // A later selectParcel call may have started (and finished) while
    // this probe was loading -- don't clobber its result with a stale one.
    if (token !== cropLoadToken) return;
    if (probe.naturalWidth) applyCropSize(ref, probe);
  }

  function applyCropSize(ref: ParcelRef, img: HTMLImageElement) {
    cropWidth = img.naturalWidth;
    cropHeight = img.naturalHeight;
    seedVertices(ref);
  }

  function onCropLoad(e: Event) {
    // Fallback only -- selectParcel's own probe-Image already handles
    // the normal path. Guard against `selected` being stale/null the
    // same way the probe path avoids it, by only trusting this when it
    // still matches the currently selected parcel.
    if (selected) applyCropSize(selected, e.target as HTMLImageElement);
  }

  // Takes the parcel explicitly rather than reading the reactive
  // `selected` derived value -- `selected` (a `$:` statement) only
  // updates on Svelte's next microtask flush, not synchronously the
  // instant `selectedKey` is assigned. When the crop image is already
  // browser-cached, selectParcel's probe resolves synchronously in the
  // SAME tick as setting selectedKey, which used to call this before
  // `selected` had flushed to the new parcel -- silently seeding from
  // whichever parcel was selected previously instead.
  function seedVertices(ref: ParcelRef) {
    if (!cropWidth || !cropHeight) return;
    curves = {};
    curveMode = false;
    selectedEdgeIdx = null;

    // Compute (and keep) the local<->pixel transform whenever this
    // parcel has an original traverse ring, REGARDLESS of which branch
    // below is actually used to seed the displayed vertices -- it's
    // what makes confirming a reloaded/previously-confirmed shape (or
    // one seeded from scratch) still projectable to real-world
    // coordinates on save, not just a freshly-seeded traverse shape.
    const ring = ref.parcel?.boundary_geojson?.geometry?.coordinates?.[0];
    localTransform =
      ring && ring.length >= 3 ? computeLocalTransform(ring, cropWidth, cropHeight) : null;

    // Waiting for the parcel labels: the map shows, but there is nothing to outline yet.
    if (ref.kind === 'reading') {
      vertices = [];
      seedSource = 'none';
      return;
    }

    // This parcel's OWN outline: the entity's polygon (kept even before extraction has produced
    // a record for it), else what was applied to its extracted record.
    const confirmed = ref.entity?.confirmed_polygon ?? ref.parcel.confirmed_boundary_pixels;
    if (confirmed?.vertices?.length >= 3) {
      // Re-scale a previously confirmed shape if the crop size differs
      // (shouldn't normally happen, but keeps this robust).
      const sx = cropWidth / (confirmed.crop_width || cropWidth);
      const sy = cropHeight / (confirmed.crop_height || cropHeight);
      const spec = confirmed.curve_spec;
      if (spec?.vertices?.length >= 3) {
        // saved with curved edges: the corners and the curves, not the sampled points
        vertices = spec.vertices.map(([x, y]: [number, number]) => [x * sx, y * sy]);
        curves = curveLib.migrateCurves(spec.curves);
      } else {
        vertices = confirmed.vertices.map(([x, y]: [number, number]) => [x * sx, y * sy]);
      }
      seedSource = 'confirmed';
      return;
    }

    if (ring && ring.length >= 3 && localTransform) {
      vertices = applyLocalTransform(ring, localTransform);
      seedSource = 'traverse';
      return;
    }

    // (The roster's guessed position of each parcel is not used to place the starting shape: it was
    // often in the wrong lot, and a misleading start is worse than a neutral one.)

    // No seed available at all -- start with a rough centered square
    // the user can drag into place from scratch.
    const pad = 0.25;
    const x0 = cropWidth * pad;
    const y0 = cropHeight * pad;
    const x1 = cropWidth * (1 - pad);
    const y1 = cropHeight * (1 - pad);
    vertices = [
      [x0, y0],
      [x1, y0],
      [x1, y1],
      [x0, y1]
    ];
    seedSource = 'none';
  }

  type LocalTransform = { scale: number; cx: number; cy: number; midX: number; midY: number };

  // Fits the local (anchor-relative, feet) traverse ring into the
  // crop's pixel bounding box, preserving aspect ratio. This is a
  // best-guess starting point ONLY -- the display scale is a crude
  // "fit with padding" choice, not the document's true feet-per-pixel.
  // What makes that OK: toLocalVertices (below) inverts this SAME
  // transform, so any vertex the user never drags maps back to its
  // EXACT original local coordinate on save regardless of what display
  // scale was chosen here -- the arbitrary scale cancels out perfectly
  // for round-tripped points. Only a dragged/added vertex's new local
  // position is approximate (at this display scale).
  function computeLocalTransform(
    ring: [number, number][],
    targetW: number,
    targetH: number
  ): LocalTransform {
    const xs = ring.map((p) => p[0]);
    const ys = ring.map((p) => p[1]);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const w = maxX - minX || 1;
    const h = maxY - minY || 1;

    const pad = 0.85; // leave a margin so vertices aren't flush against the crop edge
    const scale = Math.min((targetW * pad) / w, (targetH * pad) / h);

    return {
      scale,
      cx: targetW / 2,
      cy: targetH / 2,
      midX: (minX + maxX) / 2,
      midY: (minY + maxY) / 2
    };
  }

  // Local traverse Y is north-positive (up); image Y is down -- flip Y
  // when projecting into pixel space.
  function applyLocalTransform(ring: [number, number][], t: LocalTransform): [number, number][] {
    const closed =
      ring.length > 1 && ring[0][0] === ring[ring.length - 1][0] && ring[0][1] === ring[ring.length - 1][1];
    return ring
      .slice(0, closed ? -1 : undefined)
      .map(([x, y]) => [t.cx + (x - t.midX) * t.scale, t.cy - (y - t.midY) * t.scale]);
  }

  // Inverse of applyLocalTransform -- pixel space back to local feet.
  function toLocalVertices(pixelVerts: [number, number][], t: LocalTransform): [number, number][] {
    return pixelVerts.map(([px, py]) => [t.midX + (px - t.cx) / t.scale, t.midY - (py - t.cy) / t.scale]);
  }

  function svgPoint(e: PointerEvent, svg: SVGSVGElement): [number, number] {
    const rect = svg.getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width) * cropWidth;
    const y = ((e.clientY - rect.top) / rect.height) * cropHeight;
    return [x, y];
  }

  function onVertexDown(idx: number, e: PointerEvent) {
    e.stopPropagation();
    if (tool !== 'edit') return;
    checkpoint();
    draggingIdx = idx;
    selectedVertexIdx = idx;
    (e.target as Element).setPointerCapture(e.pointerId);
  }

  function onWaypointDown(k: number, e: PointerEvent) {
    e.stopPropagation();
    if (selectedEdgeIdx === null) return;
    checkpoint();
    draggingWaypoint = { edge: selectedEdgeIdx, k };
    (e.target as Element).setPointerCapture(e.pointerId);
  }

  // The diamond at the middle of a still-straight selected edge: pulling it creates the first waypoint.
  function onMidHandleDown(e: PointerEvent) {
    e.stopPropagation();
    if (selectedEdgeIdx === null || !handlePt) return;
    checkpoint();
    const made = curveLib.addWaypoint(vertices, curves, selectedEdgeIdx, handlePt);
    if (made.index < 0) return;
    curves = made.curves;
    draggingWaypoint = { edge: selectedEdgeIdx, k: made.index };
    (e.target as Element).setPointerCapture(e.pointerId);
  }

  // Curve mode: clicking the selected edge drops another waypoint on it.
  function onEdgeClick(i: number, e: MouseEvent) {
    const svgNode = (e.currentTarget as SVGElement).closest('svg') as SVGSVGElement;
    const at = svgPoint(e as unknown as PointerEvent, svgNode);
    const near = vertices.findIndex((v) => Math.hypot(v[0] - at[0], v[1] - at[1]) <= 12 * px);
    if (near >= 0) {
      selectedVertexIdx = near;
      selectedEdgeIdx = null;
      return;
    }
    if (curveMode && i === selectedEdgeIdx) {
      const svg = (e.currentTarget as SVGElement).closest('svg') as SVGSVGElement;
      const [x, y] = svgPoint(e as unknown as PointerEvent, svg);
      curves = curveLib.addWaypoint(vertices, curves, i, [x, y]).curves;
    } else {
      selectEdge(i);
    }
  }

  function removeWaypointAt(k: number) {
    if (selectedEdgeIdx === null) return;
    curves = curveLib.removeWaypoint(curves, selectedEdgeIdx, k);
  }

  function selectEdge(i: number) {
    selectedEdgeIdx = i;
    selectedVertexIdx = null;
  }

  function straightenSelectedEdge() {
    if (selectedEdgeIdx === null) return;
    checkpoint();
    const next = { ...curves };
    delete next[selectedEdgeIdx];
    curves = next;
  }

  function onSvgMove(e: PointerEvent, svg: SVGSVGElement) {
    if (e.pointerType === 'touch' && pinchMove(e)) return;
    if (panDrag) {
      const dx = e.clientX - panDrag.start[0];
      const dy = e.clientY - panDrag.start[1];
      if (!panDrag.moved && Math.hypot(dx, dy) < 4) return;
      panDrag.moved = true;
      pan = [panDrag.orig[0] + dx, panDrag.orig[1] + dy];
      return;
    }
    const raw = svgPoint(e, svg);
    cursorPt = tool === 'pen' ? penTarget(raw, e.shiftKey) : raw;
    if (moveDrag) {
      dragShape(raw);
      return;
    }
    if (draggingWaypoint !== null && vertices[draggingWaypoint.edge]) {
      const [cx, cy] = svgPoint(e, svg);
      const p: [number, number] = [Math.max(0, Math.min(cropWidth, cx)), Math.max(0, Math.min(cropHeight, cy))];
      curves = curveLib.moveWaypoint(vertices, curves, draggingWaypoint.edge, draggingWaypoint.k, p);
      return;
    }
    if (draggingIdx === null) return;
    const [x, y] = svgPoint(e, svg);
    // Absolute-space edit: only the dragged vertex moves. No bearing/
    // distance recompute, no cascading re-walk of the rest of the
    // ring -- this is the fix for the distortion bug from the
    // Leaflet-based editor.
    vertices[draggingIdx] = snapPoint([
      Math.max(0, Math.min(cropWidth, x)),
      Math.max(0, Math.min(cropHeight, y))
    ], draggingIdx);
    vertices = vertices; // trigger reactivity
  }

  function onSvgUp(e?: PointerEvent) {
    if (e?.pointerType === 'touch') touchEnd(e);
    if (panDrag && !panDrag.moved) {
      selectedVertexIdx = null; // a plain click on empty paper
      selectedEdgeIdx = null;
    }
    panDrag = null;
    moveDrag = null;
    if (rectStart && cursorPt) {
      const [x0, y0] = rectStart;
      const [x1, y1] = cursorPt;
      if (Math.abs(x1 - x0) > 3 && Math.abs(y1 - y0) > 3) {
        checkpoint();
        const box: [number, number][] = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
        vertices = box.map((pt) => snapPoint(pt));
        curves = {};
        tool = 'edit';
      }
      rectStart = null;
    }
    draggingIdx = null;
    if (draggingWaypoint !== null) curves = curveLib.settle(curves, draggingWaypoint.edge);
    draggingWaypoint = null;
  }

  function addVertexOnEdge(edgeIdx: number, e: MouseEvent) {
    const svg = (e.currentTarget as SVGElement).closest('svg') as SVGSVGElement;
    const [x, y] = svgPoint(e as unknown as PointerEvent, svg);
    insertVertexOnEdge(edgeIdx, [x, y]);
  }

  // Inserts a vertex on edge `edgeIdx`. On a curved edge the new vertex goes ON the curve and its
  // waypoints are shared out between the two halves; the curve indexes after it shift up by one.
  function insertVertexOnEdge(edgeIdx: number, at: [number, number]) {
    checkpoint();
    const insertAt = edgeIdx + 1;
    const vertex = curves[edgeIdx] ? curveLib.pointOnEdge(vertices, curves, edgeIdx, at) : at;
    curves = curveLib.curvesAfterInsert(vertices, curves, edgeIdx, vertex);
    vertices = [...vertices.slice(0, insertAt), vertex, ...vertices.slice(insertAt)];
    selectedEdgeIdx = null;
  }

  // Explicit "Add point" button, for complex shapes where double-
  // clicking a specific edge isn't obvious/discoverable enough --
  // inserts a new vertex at the midpoint of the current longest edge
  // (the spot most likely to need an extra point to follow a bend the
  // straight-line edge is currently cutting across).
  function addPointOnLongestEdge() {
    if (vertices.length < 2) return;
    let bestIdx = 0;
    let bestLen = -1;
    for (let i = 0; i < vertices.length; i++) {
      const [x1, y1] = vertices[i];
      const [x2, y2] = vertices[(i + 1) % vertices.length];
      const len = Math.hypot(x2 - x1, y2 - y1);
      if (len > bestLen) {
        bestLen = len;
        bestIdx = i;
      }
    }
    const mid = curveLib.midpointOf(vertices[bestIdx], vertices[(bestIdx + 1) % vertices.length], curves[bestIdx]);
    insertVertexOnEdge(bestIdx, mid);
    selectedVertexIdx = bestIdx + 1;
  }

  function deleteSelectedVertex() {
    if (selectedVertexIdx === null || vertices.length <= 3) return;
    checkpoint();
    curves = curveLib.curvesAfterDelete(curves, selectedVertexIdx, vertices.length);
    vertices = vertices.filter((_, i) => i !== selectedVertexIdx);
    selectedVertexIdx = null;
    selectedEdgeIdx = null;
  }

  // ---- history
  function checkpoint() {
    undoStack = [
      ...undoStack.slice(-79),
      { vertices: vertices.map((v) => [v[0], v[1]] as [number, number]), curves: JSON.parse(JSON.stringify(curves)) }
    ];
    redoStack = [];
  }
  function undo() {
    if (penPoints.length) {
      penPoints = penPoints.slice(0, -1);
      return;
    }
    const last = undoStack[undoStack.length - 1];
    if (!last) return;
    redoStack = [...redoStack, { vertices, curves }];
    undoStack = undoStack.slice(0, -1);
    vertices = last.vertices;
    curves = last.curves;
    selectedVertexIdx = null;
    selectedEdgeIdx = null;
  }
  function redo() {
    const next = redoStack[redoStack.length - 1];
    if (!next) return;
    undoStack = [...undoStack, { vertices, curves }];
    redoStack = redoStack.slice(0, -1);
    vertices = next.vertices;
    curves = next.curves;
  }

  // ---- snapping: corners of the sheet's other confirmed parcels (adjoining lots share them exactly)
  $: ghosts = selected
    ? parcelRefs
        .filter((r) => r.key !== selectedKey && r.pageNumber === selected.pageNumber && r.regionIndex === selected.regionIndex)
        .map((r: any) => r.entity?.confirmed_polygon ?? r.parcel?.confirmed_boundary_pixels)
        .filter((c: any) => c?.vertices?.length >= 3)
        .map((c: any) =>
          (c.curve_spec?.vertices ?? c.vertices).map(
            ([x, y]: [number, number]) =>
              [(x * cropWidth) / (c.crop_width || cropWidth), (y * cropHeight) / (c.crop_height || cropHeight)] as [number, number]
          )
        )
    : [];
  $: snapTargets = (ghosts as [number, number][][]).flat();

  function snapRadius(): number {
    // about 12 screen pixels, in crop units
    const w = svgEl?.getBoundingClientRect().width || cropWidth;
    return (12 * cropWidth) / w;
  }
  function snapPoint(p: [number, number], skipOwn?: number): [number, number] {
    if (!snapOn) return p;
    let best: [number, number] | null = null;
    let bestD = snapRadius();
    const own = tool === 'pen' ? penPoints : vertices.filter((_, i) => i !== skipOwn);
    for (const q of [...snapTargets, ...own]) {
      const d = Math.hypot(q[0] - p[0], q[1] - p[1]);
      if (d < bestD) {
        bestD = d;
        best = q;
      }
    }
    return best ? [best[0], best[1]] : p;
  }

  // ---- pen: the next corner, snapped; Shift locks the new edge to 45-degree steps from the last one
  function penTarget(p: [number, number], shift: boolean): [number, number] {
    const n = penPoints.length;
    if (shift && n >= 1) {
      const a = penPoints[n - 1];
      const ref = n >= 2 ? Math.atan2(a[1] - penPoints[n - 2][1], a[0] - penPoints[n - 2][0]) : 0;
      const ang = Math.atan2(p[1] - a[1], p[0] - a[0]);
      const step = Math.PI / 4;
      const snapped = ref + Math.round((ang - ref) / step) * step;
      const len = Math.hypot(p[0] - a[0], p[1] - a[1]);
      return [a[0] + Math.cos(snapped) * len, a[1] + Math.sin(snapped) * len];
    }
    return snapPoint(p);
  }
  function finishPen() {
    if (penPoints.length >= 3) {
      checkpoint();
      vertices = penPoints;
      curves = {};
      selectedVertexIdx = null;
      selectedEdgeIdx = null;
      tool = 'edit';
    }
    penPoints = [];
  }

  // ---- canvas presses, by tool
  function onCanvasDown(e: PointerEvent) {
    if (e.pointerType === 'touch' && trackTouch(e)) return; // second finger: pinch
    const onPaper = e.button === 0 && tool === 'edit' && !isHandle(e.target);
    if (e.button === 2 || e.button === 1 || spaceDown || (e.button === 0 && tool === 'hand') || onPaper) {
      // a press on empty paper in Edit is a click (deselect) until it moves a few pixels
      panDrag = { start: [e.clientX, e.clientY], orig: [pan[0], pan[1]], moved: !onPaper };
      (e.currentTarget as Element).setPointerCapture(e.pointerId);
      e.preventDefault();
      return;
    }
    if (e.button !== 0) return;
    const p = svgPoint(e, svgEl);
    if (tool === 'pen') {
      const target = penTarget(p, e.shiftKey);
      if (penPoints.length >= 3 && Math.hypot(target[0] - penPoints[0][0], target[1] - penPoints[0][1]) < snapRadius()) {
        finishPen(); // clicked the first corner again: close the shape
      } else {
        penPoints = [...penPoints, target];
      }
    } else if (tool === 'rect') {
      rectStart = snapPoint(p);
      (e.currentTarget as Element).setPointerCapture(e.pointerId);
    } else if (tool === 'fill') {
      fillAt(p);
    } else if (tool === 'move' && vertices.length >= 3) {
      checkpoint();
      moveDrag = {
        mode: pointInPolygon(p, vertices) ? 'move' : 'rotate',
        start: p,
        orig: vertices.map((v) => [v[0], v[1]] as [number, number]),
        centre: centroid(vertices)
      };
      (e.currentTarget as Element).setPointerCapture(e.pointerId);
    } else if (tool === 'edit') {
      selectedVertexIdx = null;
    }
  }

  function dragShape(p: [number, number]) {
    if (!moveDrag) return;
    const { mode, start, orig, centre } = moveDrag;
    if (mode === 'move') {
      const dx = p[0] - start[0];
      const dy = p[1] - start[1];
      vertices = orig.map(([x, y]) => [x + dx, y + dy] as [number, number]);
    } else {
      const a = Math.atan2(p[1] - centre[1], p[0] - centre[0]) - Math.atan2(start[1] - centre[1], start[0] - centre[0]);
      const c = Math.cos(a);
      const s = Math.sin(a);
      vertices = orig.map(
        ([x, y]) => [centre[0] + (x - centre[0]) * c - (y - centre[1]) * s, centre[1] + (x - centre[0]) * s + (y - centre[1]) * c] as [number, number]
      );
    }
  }

  function centroid(pts: [number, number][]): [number, number] {
    return [pts.reduce((a, p) => a + p[0], 0) / pts.length, pts.reduce((a, p) => a + p[1], 0) / pts.length];
  }
  function pointInPolygon([x, y]: [number, number], pts: [number, number][]): boolean {
    let inside = false;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const [xi, yi] = pts[i];
      const [xj, yj] = pts[j];
      if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  // ---- fill: the region around the click, bounded by the drawing's lines (server-side, same crop)
  async function fillAt(p: [number, number]) {
    if (!selected || fillBusy) return;
    fillBusy = true;
    fillMsg = '';
    try {
      const res = await fetch(
        `${API_BASE}/documents/${documentId.trim()}/pages/${selected.pageNumber}/regions/${selected.regionIndex}/fill`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ x: p[0], y: p[1], gap: fillGap }) }
      );
      const data = await res.json();
      if (data.vertices?.length >= 3) {
        checkpoint();
        vertices = data.vertices.map((v: [number, number]) => snapPoint(v));
        curves = {};
        selectedVertexIdx = null;
        selectedEdgeIdx = null;
        fillMsg = `Filled with ${data.vertices.length} corners. Check it against the lines; fix a corner in Edit, or Undo and try another gap.`;
      } else {
        fillMsg = data.error ?? 'Nothing was filled.';
      }
    } catch {
      fillMsg = 'Fill failed.';
    } finally {
      fillBusy = false;
    }
  }

  // ---- zoom (wheel, around the cursor) and pan
  // Wheel: a trackpad PINCH arrives as a wheel event with ctrlKey -> smooth zoom; a two-finger SWIPE
  // (horizontal component, or small fine-grained steps) pans; a mouse wheel's coarse notches zoom.
  function onWheel(e: WheelEvent) {
    if (!stageEl) return;
    e.preventDefault();
    const rect = stageEl.getBoundingClientRect();
    const at: [number, number] = [e.clientX - rect.left, e.clientY - rect.top];
    if (e.ctrlKey) {
      zoomAt(at, zoom * Math.exp(-e.deltaY * 0.01));
      return;
    }
    const swipe = e.deltaMode === 0 && (Math.abs(e.deltaX) > 0 || Math.abs(e.deltaY) < 40);
    if (swipe) {
      pan = [pan[0] - e.deltaX, pan[1] - e.deltaY];
      return;
    }
    zoomAt(at, zoom * (e.deltaY < 0 ? 1.2 : 1 / 1.2));
  }
  function zoomAt([cx, cy]: [number, number], target: number) {
    const next = Math.min(12, Math.max(1, target));
    pan = next === 1 ? [0, 0] : [cx - ((cx - pan[0]) * next) / zoom, cy - ((cy - pan[1]) * next) / zoom];
    zoom = next;
  }
  function isHandle(t: EventTarget | null): boolean {
    const cls = (t as Element | null)?.getAttribute?.('class') ?? '';
    return /vertex-hit|edge-hit|curve-handle/.test(cls);
  }
  function stagePoint(e: PointerEvent): [number, number] {
    const r = stageEl.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  }
  // returns true once two fingers are down (the event then belongs to the pinch, not a tool)
  function trackTouch(e: PointerEvent): boolean {
    touches.set(e.pointerId, stagePoint(e));
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
    if (touches.size === 2) {
      const [a, b] = [...touches.values()];
      pinch = { dist: Math.hypot(a[0] - b[0], a[1] - b[1]) || 1, mid: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], zoom, pan: [pan[0], pan[1]] };
      panDrag = null;
      moveDrag = null;
      draggingIdx = null;
      return true;
    }
    return false;
  }
  function pinchMove(e: PointerEvent): boolean {
    if (!touches.has(e.pointerId)) return false;
    touches.set(e.pointerId, stagePoint(e));
    if (!pinch || touches.size < 2) return false;
    const [a, b] = [...touches.values()];
    const dist = Math.hypot(a[0] - b[0], a[1] - b[1]) || 1;
    const mid: [number, number] = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
    const next = Math.min(12, Math.max(1, (pinch.zoom * dist) / pinch.dist));
    // keep the drawing point that was under the fingers' midpoint under it, then follow the midpoint
    const k = next / pinch.zoom;
    pan = [mid[0] - (pinch.mid[0] - pinch.pan[0]) * k, mid[1] - (pinch.mid[1] - pinch.pan[1]) * k];
    zoom = next;
    return true;
  }
  function touchEnd(e: PointerEvent) {
    touches.delete(e.pointerId);
    if (touches.size < 2) pinch = null;
  }
  function resetView() {
    zoom = 1;
    pan = [0, 0];
  }
  // zoom buttons: about the middle of the visible drawing
  function zoomBy(f: number) {
    if (!stageEl) return;
    const rect = stageEl.getBoundingClientRect();
    const cx = rect.width / 2;
    const cy = rect.height / 2;
    const next = Math.min(12, Math.max(1, zoom * f));
    pan = next === 1 ? [0, 0] : [cx - ((cx - pan[0]) * next) / zoom, cy - ((cy - pan[1]) * next) / zoom];
    zoom = next;
  }

  // Simplify: drop corners that lie (almost) on the straight line through their neighbours -- tidies the
  // extra points a Fill leaves along a straight lot line. Curved edges are kept as they are.
  function simplifyOutline() {
    if (vertices.length <= 3 || Object.keys(curves).length) {
      simplifyMsg = Object.keys(curves).length ? 'Straighten curved edges first; Simplify keeps curves untouched.' : '';
      return;
    }
    const tolerance = Math.max(cropWidth, cropHeight) * 0.003; // about 0.3% of the sheet
    let pts = vertices.map((v) => [v[0], v[1]] as [number, number]);
    let changed = true;
    while (changed && pts.length > 3) {
      changed = false;
      for (let i = 0; i < pts.length; i++) {
        const a = pts[(i - 1 + pts.length) % pts.length];
        const b = pts[i];
        const c = pts[(i + 1) % pts.length];
        const len = Math.hypot(c[0] - a[0], c[1] - a[1]) || 1;
        const off = Math.abs((c[0] - a[0]) * (a[1] - b[1]) - (a[0] - b[0]) * (c[1] - a[1])) / len;
        if (off < tolerance) {
          pts.splice(i, 1);
          changed = true;
          break;
        }
      }
    }
    const removed = vertices.length - pts.length;
    if (removed) {
      checkpoint();
      vertices = pts;
      selectedVertexIdx = null;
      selectedEdgeIdx = null;
    }
    simplifyMsg = removed ? `Removed ${removed} redundant corner${removed === 1 ? '' : 's'}.` : 'Nothing to simplify.';
  }

  function setTool(t: Tool) {
    tool = t;
    penPoints = [];
    rectStart = null;
    fillMsg = '';
    curveMode = false;
  }

  function onKeydown(e: KeyboardEvent) {
    const tag = (e.target as HTMLElement)?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
      e.preventDefault();
      if (e.shiftKey) redo();
      else undo();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') {
      e.preventDefault();
      redo();
      return;
    }
    if (e.key === ' ') {
      spaceDown = true;
      e.preventDefault();
      return;
    }
    if (e.key === 'Escape') {
      setTool('edit');
      return;
    }
    if (e.key === 'Enter' && tool === 'pen') {
      finishPen();
      return;
    }
    if (!e.ctrlKey && !e.metaKey && !e.altKey) {
      const keyTool: Record<string, Tool> = { v: 'edit', p: 'pen', r: 'rect', f: 'fill', m: 'move', h: 'hand' };
      const t = keyTool[e.key.toLowerCase()];
      if (t) {
        setTool(t);
        return;
      }
    }
    if ((e.key === 'Delete' || e.key === 'Backspace') && selectedVertexIdx !== null) {
      e.preventDefault();
      deleteSelectedVertex();
    }
  }
  function onKeyup(e: KeyboardEvent) {
    if (e.key === ' ') spaceDown = false;
  }


  function resetToSeed() {
    if (!selected) return;
    checkpoint();
    seedVertices(selected);
  }

  async function confirmBoundary() {
    if (!selected || vertices.length < 3) return;
    saveStatus = 'saving';
    try {
      const flat = curveLib.flatten(vertices, curves);
      const local_vertices = localTransform ? toLocalVertices(flat.points, localTransform) : null;
      const curve_spec = Object.keys(curves).length ? { vertices, curves, corner_indices: flat.cornerIndices } : null;
      const res = await fetch(`${API_BASE}/documents/${documentId.trim()}/confirm-boundary`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          page_number: selected.pageNumber,
          region_index: selected.regionIndex,
          // THE PARCEL being confirmed (a sheet entity); a parcel named by hand sends its name instead
          parcel_id: selected.parcelId,
          label: selected.kind === 'manual_new' ? manualName.trim() : null,
          // legacy documents (no parcel entities): the extracted parcel's index
          parcel_index: selected.parcelId || selected.kind === 'manual_new' ? null : selected.parcelIndex,
          vertices: flat.points,
          crop_width: cropWidth,
          crop_height: cropHeight,
          local_vertices,
          curve_spec
        })
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const data = await res.json();
      // The backend replaces boundary_geojson/boundary_geojson_wgs84
      // with ones projected from this confirmed shape (when
      // local_vertices + a document anchor were available) -- pull the
      // whole updated parcel back, not just the pixel-shape fields, so
      // this screen's own state stays consistent with what /workspace
      // will now render for the same document.
      if (data.parcel) Object.assign(selected.parcel, data.parcel);
      saveStatus = 'saved';
      savedState = data.state ?? 'bound';
      georeferenced = !!data.parcel?.boundary_geojson_wgs84 && data.georeferenced_from_confirmation;
      const named = selected.kind === 'manual_new' ? data.entity : null;
      await refreshDocument();
      // A parcel just named by hand now exists as an entity: stay on it.
      if (named) selectedKey = `${selected?.pageNumber ?? ''}-${named.id}`.replace(/^-/, '');
      else parcelRefs = parcelRefs; // reassign to trigger reactivity (confirmedCount, badges)
    } catch (err) {
      saveStatus = 'error';
    }
  }

  let georeferenced = false;
  let savedState: 'bound' | 'waiting_for_document' = 'bound';

  $: handlePt =
    selectedEdgeIdx !== null && vertices[selectedEdgeIdx]
      ? curveLib.midpointOf(vertices[selectedEdgeIdx], vertices[(selectedEdgeIdx + 1) % vertices.length], curves[selectedEdgeIdx])
      : null;
  $: waypointPts = selectedEdgeIdx !== null && vertices[selectedEdgeIdx] ? curveLib.waypointPoints(vertices, curves, selectedEdgeIdx) : [];

  function polygonPoints(pts: [number, number][]): string {
    return pts.map(([x, y]) => `${x},${y}`).join(' ');
  }

  $: realParcels = parcelRefs.filter((r) => r.kind === 'parcel');
  $: confirmedCount = realParcels.filter((r) => r.parcel.human_confirmed).length;

</script>

<svelte:window on:keydown={onKeydown} on:keyup={onKeyup} />

<div class="br" class:full={centreFull}>
  <header class="br-head">
    <a class="back" href={documentId.trim() ? `/workspace?doc=${encodeURIComponent(documentId.trim())}` : '/workspace'} title="Back to map">←</a>
    <div class="titles">
      <h1>Confirm Parcel Boundary</h1>
      <p>Select a parcel, outline it on the drawing, and confirm to save it on the map.</p>
    </div>
    {#if parcelRefs.length > 0}
      <div class="progress" title="Parcels confirmed on this document">
        <span>{confirmedCount} / {realParcels.length} confirmed</span>
        <div class="bar"><div style={`width:${realParcels.length ? (confirmedCount / realParcels.length) * 100 : 0}%`}></div></div>
      </div>
      <a
        class="btn primary"
        class:disabled={confirmedCount === 0}
        href={confirmedCount === 0 ? undefined : `/workspace?doc=${encodeURIComponent(documentId.trim())}`}
      >Continue to map →</a>
    {/if}
  </header>

  <div class="loadbar">
    <input type="text" placeholder="Document ID" bind:value={documentId} on:keydown={(e) => e.key === 'Enter' && loadDocument()} />
    <button class="btn" on:click={() => loadDocument()} disabled={loading}>{loading ? 'Loading…' : 'Load'}</button>
    {#if loadError}<span class="error">{loadError}</span>{/if}
  </div>

  {#if parcelRefs.length > 0}
    <div class="br-grid">
      <!-- ============ LEFT: sheets and their parcels ============ -->
      <aside class="col left">
        {#if reading}
          <div class="notice">
            ROAM is still reading the document — you can start outlining now.
            {#if showReprocessHint}
              <div class="stuck">
                Taking longer than usual; the process may have been interrupted.
                <button class="btn sm" disabled={reprocessing} on:click={reprocessDocument}>{reprocessing ? 'Restarting…' : 'Restart processing'}</button>
              </div>
            {/if}
          </div>
        {/if}

        {#each displayGroups as group, gi}
          {#if !group.primary && !group.copyOf && (gi === 0 || displayGroups.slice(0, gi).every((g) => g.primary || g.copyOf))}
            <h2 class="col-title other">Other maps in this document</h2>
            <p class="muted small">Probably not this packet's own parcel map (reference surveys, aerial or location maps, other drawings) — still selectable.</p>
          {/if}
          <section class="card sheet" class:active-sheet={selected && group.refs.some((r) => r.key === selectedKey)} class:other-sheet={!group.primary && !group.copyOf}>
            <div class="sheet-head">
              <span class="sheet-name">Page {group.pageNumber} · Parcel map</span>
              {#if selected && group.refs.some((r) => r.key === selectedKey)}<span class="pill green">Active sheet</span>{/if}
              {#if group.badge}<span class="pill grey" title={group.note ?? ''}>{group.badge}</span>{/if}
              {#if group.copyOf}<span class="pill grey">copy of page {group.copyOf}</span>{/if}
            </div>
            {#if group.duplicates.length > 0 && !group.copyOf}
              <p class="muted small">
                Same {group.refs.length === 1 ? 'parcel' : 'parcels'} also on page{group.duplicates.length === 1 ? '' : 's'}
                {group.duplicates.map((d) => d.pageNumber).join(', ')} — folded away.
                <button class="link" on:click={() => toggleCopies(group.pageNumber)}>{expandedCopies.has(group.pageNumber) ? 'Hide' : 'Show'}</button>
              </p>
            {/if}
            {#if group.note}<p class="muted small">{group.note}</p>{/if}
            <div class="list-head"><span>Parcels on this sheet</span><span class="pill grey">{group.refs.filter((r) => r.kind === 'parcel').length}</span></div>
            <ul class="parcels">
              {#each group.refs as ref}
                <li>
                  <button class="parcel-card" class:selected={ref.key === selectedKey} class:muted-row={ref.kind !== 'parcel'} on:click={() => selectParcel(ref)}>
                    <span class="radio" class:on={ref.key === selectedKey}></span>
                    <span class="pc-body">
                      <span class="pc-name">{ref.label}</span>
                      <span class="pc-tags">
                        {#if ref.parcel?.human_confirmed}<span class="pill green">Confirmed</span>
                        {:else if ref.kind === 'parcel'}<span class="pill amber">Not confirmed</span>{/if}
                        {#if ref.manual}<span class="pill grey" title="You named this parcel; ROAM did not detect its identity.">named by you</span>{/if}
                      </span>
                      {#if ref.meta}<span class="pc-meta">{ref.meta}</span>{/if}
                    </span>
                  </button>
                  {#if ref.parcelId && !ref.parcel?.human_confirmed}
                    <button class="icon-btn" title="Delete this parcel (only before it is confirmed)" on:click={() => deleteParcelEntity(ref)}>✕</button>
                  {/if}
                </li>
              {/each}
            </ul>
          </section>
        {/each}

        {#if deleteError}<p class="error small">{deleteError}</p>{/if}

        {#if excludedParcels.length > 0}
          <section class="card">
            <div class="sheet-head"><span class="sheet-name">Not offered by default ({excludedParcels.length})</span></div>
            <p class="muted small">Flagged as likely vicinity/locus-map duplicates or under-evidenced fragments. Restore if this looks wrong.</p>
            <ul class="excluded">
              {#each excludedParcels as ref}
                <li>
                  <span title={ref.excludedReason ?? ''}>Page {ref.pageNumber} · {ref.label}</span>
                  <button class="link" on:click={() => restoreExcludedParcel(ref)}>Restore</button>
                  {#if ref.parcelId}<button class="icon-btn" title="Delete for good" on:click={() => deleteParcelEntity(ref)}>✕</button>{/if}
                </li>
              {/each}
            </ul>
          </section>
        {/if}
      </aside>

      <!-- ============ CENTRE: tools and the drawing ============ -->
      <section class="col centre">
        {#if selected}
          <div class="toolbar" role="toolbar" aria-label="Drawing tools">
            {#each TOOLS as [t, label, key, tip]}
              <button class="tool" class:on={tool === t} on:click={() => setTool(t)} title={`${tip} (${key})`}>
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d={TOOL_ICONS[t]} /></svg>
                <span>{label}</span>
              </button>
            {/each}
            {#if tool === 'fill'}
              <label class="gap" title="Bridges breaks in the boundary lines: raise it if the fill leaks out, lower it if it stops short">
                Gap <input type="range" min="1" max="21" step="2" bind:value={fillGap} /> <b>{fillGap}px</b>
              </label>
            {/if}
            <span class="grow"></span>
            <button class="tool icon" on:click={undo} disabled={!undoStack.length && !penPoints.length} title="Undo (Ctrl+Z)">
              <svg viewBox="0 0 24 24"><path d="M9 14 4 9l5-5M4 9h11a5 5 0 0 1 0 10h-3" /></svg>
            </button>
            <button class="tool icon" on:click={redo} disabled={!redoStack.length} title="Redo (Ctrl+Shift+Z)">
              <svg viewBox="0 0 24 24"><path d="m15 14 5-5-5-5M20 9H9a5 5 0 0 0 0 10h3" /></svg>
            </button>
            <button class="tool" on:click={resetView} disabled={zoom === 1} title="Fit the whole sheet">Reset view</button>
            <button class="tool icon" on:click={() => (centreFull = !centreFull)} title={centreFull ? 'Exit full screen' : 'Full screen'}>
              <svg viewBox="0 0 24 24"><path d={centreFull ? 'M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5' : 'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5'} /></svg>
            </button>
          </div>
          <p class="tool-hint">
            {#if tool === 'pen'}Draw: click each corner in order{penPoints.length ? ` (${penPoints.length} placed)` : ''}. Click the first corner or press Enter to close · Esc cancels · hold Shift for 45°/90° turns.
            {:else if tool === 'fill'}{fillBusy ? 'Filling…' : fillMsg || 'Fill: click inside a lot — the region its lines enclose becomes the outline.'}
            {:else if tool === 'rect'}Rectangle: drag from one corner to the opposite corner.
            {:else if tool === 'move'}Move: drag inside the shape · Rotate: drag outside it.
            {:else if tool === 'hand'}Hand: drag to move around the drawing.
            {:else}Edit: drag a corner · double-click an edge to add one · click a corner and press Delete to remove it · drag empty paper to move the page.{/if}
          </p>

          {#if selected.kind === 'reading'}
            <p class="notice">Reading parcel labels from this map… you can outline a parcel as soon as the names appear.</p>
          {:else if selected.kind === 'manual_new'}
            <label class="manual-name">
              <span>Parcel name</span>
              <input type="text" bind:value={manualName} placeholder="e.g. Parcel 1" />
              <small>You are naming this parcel yourself — ROAM did not detect its identity.</small>
            </label>
          {/if}

          <div class="stage-wrap">
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <div class="stage" bind:this={stageEl} bind:clientWidth={stageW} on:wheel={onWheel} on:contextmenu|preventDefault class:panning={spaceDown} class:dragging-page={!!panDrag?.moved} data-tool={tool}>
              <div class="zoom-layer" style={`transform: translate(${pan[0]}px, ${pan[1]}px) scale(${zoom})`}>
                <img bind:this={cropImgEl} src={cropSrc} alt="source drawing" on:load={onCropLoad} />
                {#if cropWidth > 0}
                  <svg
                    bind:this={svgEl}
                    viewBox={`0 0 ${cropWidth} ${cropHeight}`}
                    preserveAspectRatio="none"
                    on:pointerdown={onCanvasDown}
                    on:pointermove={(e) => onSvgMove(e, e.currentTarget)}
                    on:pointerup={onSvgUp}
                    on:pointerleave={onSvgUp}
                    on:dblclick={() => tool === 'pen' && finishPen()}
                  >
                    {#if showOthers}
                      {#each ghosts as g}
                        <polygon points={polygonPoints(g)} class="ghost" />
                      {/each}
                    {/if}
                    <path d={curveLib.outlinePath(vertices, curves)} class="boundary-poly" />
                    {#if selectedEdgeIdx !== null && vertices[selectedEdgeIdx]}
                      <path d={curveLib.edgePath(vertices, curves, selectedEdgeIdx)} class="edge-selected" />
                    {/if}
                    {#each vertices as _v, i}
                      <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
                      <path
                        d={curveLib.edgePath(vertices, curves, i)}
                        class="edge-hit"
                        class:inactive={tool !== 'edit'}
                        on:click={(e) => onEdgeClick(i, e)}
                        on:dblclick={(e) => addVertexOnEdge(i, e)}
                      />
                    {/each}
                    {#each vertices as [x, y], i}
                      {#if showVertices || i === selectedVertexIdx}
                        <circle cx={x} cy={y} r={(i === selectedVertexIdx ? 6 : 4.5) * px} class="vertex" class:selected={i === selectedVertexIdx} />
                      {/if}
                      <circle cx={x} cy={y} r={11 * px} class="vertex-hit" class:inactive={tool !== 'edit'} on:pointerdown={(e) => onVertexDown(i, e)} />
                    {/each}
                    {#if tool === 'pen' && penPoints.length}
                      <polyline points={polygonPoints(cursorPt ? [...penPoints, cursorPt] : penPoints)} class="pen-line" />
                      {#each penPoints as [x, y], i}
                        <circle cx={x} cy={y} r={5 * px} class="pen-dot" class:first={i === 0} />
                      {/each}
                    {/if}
                    {#if tool === 'rect' && rectStart && cursorPt}
                      <rect x={Math.min(rectStart[0], cursorPt[0])} y={Math.min(rectStart[1], cursorPt[1])} width={Math.abs(cursorPt[0] - rectStart[0])} height={Math.abs(cursorPt[1] - rectStart[1])} class="pen-line" />
                    {/if}
                    {#if curveMode && selectedEdgeIdx !== null}
                      {#each waypointPts as [wx, wy], k}
                        <rect
                          x={wx - 5 * px}
                          y={wy - 5 * px}
                          width={10 * px}
                          height={10 * px}
                          transform={`rotate(45 ${wx} ${wy})`}
                          class="curve-handle"
                        />
                        <!-- svelte-ignore a11y_no_static_element_interactions -->
                        <circle cx={wx} cy={wy} r={11 * px} class="vertex-hit" on:pointerdown={(e) => onWaypointDown(k, e)} on:dblclick={() => removeWaypointAt(k)} />
                      {/each}
                      {#if waypointPts.length === 0 && handlePt}
                        <rect
                          x={handlePt[0] - 5 * px}
                          y={handlePt[1] - 5 * px}
                          width={10 * px}
                          height={10 * px}
                          transform={`rotate(45 ${handlePt[0]} ${handlePt[1]})`}
                          class="curve-handle"
                        />
                        <circle cx={handlePt[0]} cy={handlePt[1]} r={11 * px} class="vertex-hit" on:pointerdown={onMidHandleDown} />
                      {/if}
                    {/if}
                  </svg>
                {/if}
              </div>
            </div>
            <div class="zoom-ctl">
              <button on:click={() => zoomBy(1.25)} title="Zoom in">+</button>
              <button on:click={() => zoomBy(1 / 1.25)} title="Zoom out">−</button>
              <button on:click={resetView} title="Fit sheet">⤢</button>
            </div>
            <div class="zoom-pct">{Math.round(zoom * 100)}%</div>
          </div>
          <p class="muted small foot-hint">Mouse wheel or pinch zooms · drag empty paper, two-finger swipe or Space+drag moves the page · Ctrl+Z / Ctrl+Shift+Z undo and redo · keys: V edit, P draw, R rectangle, F fill, M move, H hand.</p>
        {:else}
          <div class="empty">
            <p><b>Pick a parcel</b> on the left to outline it.</p>
            <p class="muted">Tip: <b>Fill</b> (F) outlines a lot from one click inside it.</p>
          </div>
        {/if}
      </section>

      <!-- ============ RIGHT: the selected parcel ============ -->
      <aside class="col right">
        {#if selected}
          <section class="card">
            <div class="sel-head">
              <span class="muted small">Selected parcel</span>
              {#if selected.parcel?.human_confirmed}<span class="pill green">Confirmed</span>{:else}<span class="pill amber">Not confirmed</span>{/if}
            </div>
            <h2 class="sel-name">{selected.kind === 'manual_new' ? manualName || 'New parcel' : selected.label}</h2>
            <div class="stats">
              {#if selectedEntity}
                <div class="stat">
                  <span>Stated area{#if selectedEntity.stated_area_edited} <em class="edited">corrected</em>{/if}</span>
                  {#if editingArea}
                    <form class="area-edit" on:submit|preventDefault={saveStatedArea}>
                      <!-- svelte-ignore a11y-autofocus -->
                      <input bind:value={areaDraft} placeholder="e.g. 1.55 AC or 67,400 SQ. FT." autofocus />
                      <div class="area-actions">
                        <button type="submit" class="mini primary" disabled={areaSaving}>{areaSaving ? 'Saving…' : 'Save'}</button>
                        <button type="button" class="mini" on:click={() => (editingArea = false)}>Cancel</button>
                      </div>
                      <small>As printed on the sheet. Re-confirm the boundary afterwards to recalibrate with it.</small>
                    </form>
                  {:else}
                    <b>{selectedEntity.stated_area || '—'}</b>
                    {#if selectedEntity.stated_area_edited && selectedEntity.stated_area_as_read}
                      <small>read as {selectedEntity.stated_area_as_read}</small>
                    {/if}
                    <button type="button" class="link-btn" on:click={startAreaEdit}>Wrong? Edit</button>
                  {/if}
                  {#if areaError}<small class="err">{areaError}</small>{/if}
                </div>
              {:else if selected.meta}
                <div class="stat"><span>Stated area</span><b>{selected.meta}</b></div>
              {/if}
              {#if selected.parcel?.spatial_validation?.area_sqft}
                <div class="stat"><span>Area (computed)</span><b>{Math.round(selected.parcel.spatial_validation.area_sqft).toLocaleString()} sq ft</b><small>{selected.parcel.spatial_validation.area_acres} ac</small></div>
              {/if}
              {#if selected.parcel?.spatial_validation?.perimeter_ft}
                <div class="stat"><span>Perimeter</span><b>{Number(selected.parcel.spatial_validation.perimeter_ft).toLocaleString()} ft</b></div>
              {/if}
              <div class="stat"><span>Corners</span><b>{vertices.length}</b></div>
            </div>
            {#if !localTransform && !reading && selected.kind === 'parcel' && !selected.parcel?.human_confirmed}
              <p class="warn small">No survey calls were read for this parcel; ROAM sizes it from the sheet's stated areas once confirmed.</p>
            {/if}
          </section>

          <section class="card">
            <h3 class="card-title">Editing tools</h3>
            <div class="tool-grid">
              <button on:click={addPointOnLongestEdge} title="Add a corner on the longest edge (or double-click any edge)">
                <svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg><span>Add point</span>
              </button>
              <button on:click={deleteSelectedVertex} disabled={selectedVertexIdx === null || vertices.length <= 3} title="Delete the selected corner (or press Delete)">
                <svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" /></svg><span>Delete point</span>
              </button>
              <button class:on={tool === 'move'} on:click={() => setTool('move')} title="Move or rotate the whole outline (M)">
                <svg viewBox="0 0 24 24"><path d={TOOL_ICONS.move} /></svg><span>Move / rotate</span>
              </button>
              <button on:click={straightenSelectedEdge} disabled={selectedEdgeIdx === null || !curves[selectedEdgeIdx]} title="Make the selected curved edge straight again">
                <svg viewBox="0 0 24 24"><path d="M5 19 19 5" /></svg><span>Straighten</span>
              </button>
              <button class:on={curveMode} on:click={() => (curveMode = !curveMode)} disabled={selectedEdgeIdx === null} title="Click an edge first, then pull the diamond to bend it">
                <svg viewBox="0 0 24 24"><path d="M5 19C5 10 10 5 19 5" /></svg><span>{curveMode ? 'Done curving' : 'Curve edge'}</span>
              </button>
              <button on:click={simplifyOutline} disabled={vertices.length <= 3} title="Remove corners that sit on a straight line (tidies a Fill result)">
                <svg viewBox="0 0 24 24"><path d="M3 17l6-6 4 4 8-8" /></svg><span>Simplify</span>
              </button>
            </div>
            {#if simplifyMsg}<p class="muted small">{simplifyMsg}</p>{/if}
          </section>

          <section class="card">
            <h3 class="card-title">Display options</h3>
            <label class="check"><input type="checkbox" bind:checked={showVertices} /> Show corner points</label>
            <label class="check"><input type="checkbox" bind:checked={snapOn} /> Snap to corners of other parcels</label>
            <label class="check"><input type="checkbox" bind:checked={showOthers} /> Show other parcels on this sheet</label>
          </section>

          <section class="card actions">
            <button
              class="btn primary block"
              on:click={confirmBoundary}
              disabled={vertices.length < 3 || selected.kind === 'reading' || (selected.kind === 'manual_new' && !manualName.trim())}
            >✓ Confirm boundary</button>
            {#if saveStatus === 'saving'}<p class="muted small">Saving…</p>{/if}
            {#if saveStatus === 'saved' && savedState === 'waiting_for_document'}
              <p class="ok small">Saved ✓ — placed on the map automatically when ROAM finishes reading.</p>
            {:else if saveStatus === 'saved' && georeferenced}
              <p class="ok small">Saved ✓ — placed on the map. <a href={`/workspace?doc=${encodeURIComponent(documentId.trim())}`}>View →</a></p>
            {:else if saveStatus === 'saved'}
              <p class="ok small">Saved ✓ (outline stored; not yet placed).</p>
            {/if}
            {#if saveStatus === 'error'}<p class="error small">Save failed.</p>{/if}
            <button class="btn block" on:click={resetToSeed} title="Back to the starting outline">↺ Reset outline</button>
            {#if selected.parcelId && !selected.parcel?.human_confirmed}
              <button class="btn danger block" on:click={() => selected && deleteParcelEntity(selected)}>Delete parcel</button>
            {/if}
          </section>
        {/if}
      </aside>
    </div>
  {:else if result}
    <p class="muted">No parcels with geometry found on this document.</p>
  {/if}
</div>

<style>
  .br {
    --blue: #2563eb;
    --blue-soft: #eff4ff;
    --line: #e5e7eb;
    --ink: #111827;
    --muted: #6b7280;
    --green: #15803d;
    --green-soft: #dcfce7;
    --amber: #b45309;
    --amber-soft: #fef3c7;
    color: var(--ink);
    font-size: 14px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .br-head {
    display: flex;
    align-items: center;
    gap: 16px;
    background: #fff;
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 14px 18px;
  }
  .back {
    font-size: 20px;
    text-decoration: none;
    color: var(--ink);
    width: 36px;
    height: 36px;
    display: grid;
    place-items: center;
    border-radius: 10px;
    border: 1px solid var(--line);
  }
  .titles {
    flex: 1;
    min-width: 0;
  }
  .titles h1 {
    margin: 0;
    font-size: 22px;
  }
  .titles p {
    margin: 2px 0 0;
    color: var(--muted);
  }
  .progress {
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 180px;
    font-size: 13px;
  }
  .progress .bar {
    height: 6px;
    background: #e5e7eb;
    border-radius: 99px;
    overflow: hidden;
  }
  .progress .bar div {
    height: 100%;
    background: var(--blue);
  }
  .btn {
    border: 1px solid var(--line);
    background: #fff;
    border-radius: 9px;
    padding: 8px 14px;
    font: inherit;
    cursor: pointer;
    text-decoration: none;
    color: var(--ink);
    text-align: center;
  }
  .btn:disabled,
  .btn.disabled {
    opacity: 0.45;
    pointer-events: none;
  }
  .btn.primary {
    background: var(--blue);
    border-color: var(--blue);
    color: #fff;
  }
  .btn.danger {
    color: #b91c1c;
    border-color: #fecaca;
  }
  .btn.block {
    display: block;
    width: 100%;
  }
  .btn.sm {
    padding: 4px 10px;
    font-size: 12px;
  }
  .loadbar {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .loadbar input {
    flex: 0 1 380px;
    padding: 8px 12px;
    border: 1px solid var(--line);
    border-radius: 9px;
    font: inherit;
  }
  .br-grid {
    display: grid;
    grid-template-columns: 260px minmax(0, 1fr) 260px;
    gap: 14px;
    align-items: start;
  }
  .col.centre {
    container-type: inline-size;
  }
  /* a narrow drawing column: tool buttons show their icon only (name in the tooltip) */
  @container (max-width: 760px) {
    .toolbar .tool span {
      display: none;
    }
    .toolbar .tool {
      padding: 7px 9px;
    }
  }
  .col {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
  }
  .card {
    background: #fff;
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 14px;
  }
  .card-title,
  .col-title {
    margin: 0 0 10px;
    font-size: 15px;
  }
  .col-title.other {
    margin: 6px 0 0;
  }
  .sheet.active-sheet {
    border-color: #bfd3fe;
  }
  .sheet.other-sheet {
    background: #fafafa;
  }
  .sheet-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    margin-bottom: 6px;
  }
  .sheet-name {
    font-weight: 600;
    font-size: 12.5px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .list-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin: 8px 0 6px;
    font-weight: 600;
  }
  .pill {
    font-size: 11.5px;
    padding: 2px 8px;
    border-radius: 99px;
    white-space: nowrap;
    font-weight: 500;
  }
  .pill.green {
    background: var(--green-soft);
    color: var(--green);
  }
  .pill.amber {
    background: var(--amber-soft);
    color: var(--amber);
  }
  .pill.grey {
    background: #f3f4f6;
    color: #4b5563;
  }
  .parcels,
  .excluded {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .parcels li,
  .excluded li {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .parcel-card {
    flex: 1;
    display: flex;
    gap: 10px;
    align-items: flex-start;
    text-align: left;
    padding: 10px 12px;
    border: 1px solid var(--line);
    border-radius: 11px;
    background: #fff;
    cursor: pointer;
    font: inherit;
    color: inherit;
  }
  .parcel-card:hover {
    border-color: #c7d2fe;
  }
  .parcel-card.selected {
    border-color: var(--blue);
    background: var(--blue-soft);
  }
  .parcel-card.muted-row {
    opacity: 0.8;
  }
  .radio {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    border: 2px solid #9ca3af;
    margin-top: 3px;
    flex: none;
  }
  .radio.on {
    border-color: var(--blue);
    background: radial-gradient(var(--blue) 45%, #fff 50%);
  }
  .pc-body {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }
  .pc-name {
    font-weight: 600;
  }
  .pc-tags {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
  }
  .pc-meta {
    font-size: 12.5px;
    color: var(--muted);
  }
  .icon-btn {
    border: none;
    background: none;
    color: #9ca3af;
    cursor: pointer;
    font-size: 14px;
    padding: 4px;
  }
  .icon-btn:hover {
    color: #b91c1c;
  }
  .link {
    border: none;
    background: none;
    color: var(--blue);
    cursor: pointer;
    font: inherit;
    padding: 0 2px;
  }
  .muted {
    color: var(--muted);
  }
  .small {
    font-size: 12.5px;
    margin: 4px 0;
  }
  .notice {
    background: var(--blue-soft);
    border: 1px solid #c7d7fe;
    border-radius: 11px;
    padding: 10px 12px;
    font-size: 13px;
  }
  .notice .stuck {
    margin-top: 6px;
  }
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px;
    background: #fff;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 6px;
  }
  .tool {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    border: 1px solid transparent;
    background: none;
    border-radius: 8px;
    padding: 7px 11px;
    font: inherit;
    cursor: pointer;
    color: var(--ink);
  }
  .tool:hover {
    background: #f3f4f6;
  }
  .tool.on {
    background: var(--blue);
    color: #fff;
  }
  .tool:disabled {
    opacity: 0.4;
    cursor: default;
  }
  .tool svg,
  .tool-grid svg {
    width: 17px;
    height: 17px;
    fill: none;
    stroke: currentColor;
    stroke-width: 2;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .tool.icon {
    padding: 7px 8px;
  }
  .grow {
    flex: 1;
  }
  .gap {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    margin-left: 6px;
    padding: 4px 10px;
    background: var(--blue-soft);
    border-radius: 8px;
  }
  .tool-hint {
    margin: 0;
    font-size: 13px;
    color: #374151;
  }
  .manual-name {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 13px;
  }
  .manual-name input {
    padding: 8px 10px;
    border: 1px solid var(--line);
    border-radius: 8px;
    font: inherit;
    max-width: 320px;
  }
  .stage-wrap {
    position: relative;
  }
  .stage {
    position: relative;
    background: #fff;
    border: 1px solid var(--line);
    border-radius: 12px;
    line-height: 0;
    overflow: hidden;
  }
  .zoom-layer {
    position: relative;
    transform-origin: 0 0;
  }
  .stage img {
    width: 100%;
    display: block;
  }
  .stage svg {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    touch-action: none;
  }
  .stage[data-tool='pen'] svg,
  .stage[data-tool='rect'] svg,
  .stage[data-tool='fill'] svg {
    cursor: crosshair;
  }
  .stage[data-tool='hand'] svg {
    cursor: grab;
  }
  .stage.panning svg,
  .stage.dragging-page svg {
    cursor: grabbing !important;
  }
  .stage[data-tool='move'] svg {
    cursor: move;
  }
  .stage.panning svg {
    cursor: grab;
  }
  .zoom-ctl {
    position: absolute;
    top: 10px;
    right: 10px;
    display: flex;
    flex-direction: column;
    background: #fff;
    border: 1px solid var(--line);
    border-radius: 10px;
    overflow: hidden;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.08);
  }
  .zoom-ctl button {
    width: 34px;
    height: 32px;
    border: none;
    border-bottom: 1px solid var(--line);
    background: #fff;
    font-size: 17px;
    cursor: pointer;
  }
  .zoom-ctl button:last-child {
    border-bottom: none;
  }
  .zoom-pct {
    position: absolute;
    bottom: 10px;
    right: 10px;
    font-size: 12px;
    background: rgba(255, 255, 255, 0.92);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 3px 8px;
    line-height: 1.4;
  }
  .foot-hint {
    margin-top: 0;
  }
  .empty {
    background: #fff;
    border: 1px dashed #d1d5db;
    border-radius: 14px;
    padding: 60px 20px;
    text-align: center;
  }
  .sel-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .sel-name {
    margin: 4px 0 10px;
    font-size: 18px;
  }
  .area-edit {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-top: 4px;
  }
  .area-edit input {
    font: inherit;
    padding: 6px 8px;
    border: 1px solid var(--border, #d6d3cc);
    border-radius: 6px;
    background: var(--surface, #fff);
    color: inherit;
  }
  .area-actions {
    display: flex;
    gap: 6px;
  }
  .mini {
    font: inherit;
    font-size: 12px;
    padding: 4px 10px;
    border-radius: 6px;
    border: 1px solid var(--border, #d6d3cc);
    background: var(--surface, #fff);
    color: inherit;
    cursor: pointer;
  }
  .mini.primary {
    background: #2563eb;
    border-color: #2563eb;
    color: #fff;
  }
  .link-btn {
    align-self: flex-start;
    padding: 0;
    border: 0;
    background: none;
    color: #2563eb;
    font: inherit;
    font-size: 12px;
    cursor: pointer;
  }
  .edited {
    font-style: normal;
    font-size: 11px;
    color: #b45309;
  }
  .stat .err {
    color: #b91c1c;
  }
  .stats {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .stat {
    display: flex;
    flex-direction: column;
    background: #f9fafb;
    border: 1px solid #f0f0f0;
    border-radius: 9px;
    padding: 8px 10px;
  }
  .stat span {
    font-size: 12px;
    color: var(--muted);
  }
  .stat small {
    color: var(--muted);
  }
  .tool-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 6px;
  }
  .tool-grid button {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 5px;
    padding: 10px 4px;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: #fff;
    font: inherit;
    font-size: 12px;
    cursor: pointer;
    color: var(--ink);
  }
  .tool-grid button:hover:not(:disabled) {
    border-color: #c7d2fe;
    background: var(--blue-soft);
  }
  .tool-grid button.on {
    border-color: var(--blue);
    background: var(--blue-soft);
    color: var(--blue);
  }
  .tool-grid button:disabled {
    opacity: 0.4;
    cursor: default;
  }
  .check {
    display: flex;
    gap: 8px;
    align-items: center;
    font-size: 13.5px;
    padding: 4px 0;
  }
  .actions {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .warn {
    color: var(--amber);
  }
  .ok {
    color: var(--green);
  }
  .error {
    color: #b91c1c;
  }
  /* full screen: the drawing column takes the whole window */
  .br.full .col.centre {
    position: fixed;
    inset: 0;
    z-index: 50;
    background: #f5f5f4;
    padding: 12px;
    overflow: auto;
  }
  /* drawing overlays */
  .boundary-poly {
    fill: rgba(37, 99, 235, 0.14);
    stroke: #2563eb;
    stroke-width: 2;
    vector-effect: non-scaling-stroke;
    pointer-events: none;
  }
  .edge-selected {
    fill: none;
    stroke: #f97316;
    stroke-width: 3;
    vector-effect: non-scaling-stroke;
    pointer-events: none;
  }
  .edge-hit {
    fill: none;
    stroke: transparent;
    stroke-width: 8;
    vector-effect: non-scaling-stroke;
    cursor: pointer;
  }
  .vertex {
    fill: #fff;
    stroke: #2563eb;
    stroke-width: 2;
    vector-effect: non-scaling-stroke;
    pointer-events: none;
  }
  .vertex.selected {
    fill: #f97316;
    stroke: #f97316;
  }
  .vertex-hit {
    fill: transparent;
    cursor: grab;
  }
  .curve-handle {
    fill: #f97316;
    stroke: #fff;
    stroke-width: 1;
    vector-effect: non-scaling-stroke;
    pointer-events: none;
  }
  .ghost {
    fill: rgba(107, 114, 128, 0.14);
    stroke: #6b7280;
    stroke-width: 1.5;
    stroke-dasharray: 5 4;
    vector-effect: non-scaling-stroke;
    pointer-events: none;
  }
  .pen-line {
    fill: rgba(234, 88, 12, 0.08);
    stroke: #ea580c;
    stroke-width: 2;
    vector-effect: non-scaling-stroke;
    pointer-events: none;
  }
  .pen-dot {
    fill: #ea580c;
    pointer-events: none;
  }
  .pen-dot.first {
    fill: #fff;
    stroke: #ea580c;
    stroke-width: 2;
    vector-effect: non-scaling-stroke;
  }
  .inactive {
    pointer-events: none;
  }
  @media (max-width: 1180px) {
    .br-grid {
      grid-template-columns: 260px minmax(0, 1fr);
    }
    .col.right {
      grid-column: 1 / -1;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    }
  }
  @media (max-width: 760px) {
    .br-grid {
      grid-template-columns: 1fr;
    }
    .br-head {
      flex-wrap: wrap;
    }
  }
</style>
