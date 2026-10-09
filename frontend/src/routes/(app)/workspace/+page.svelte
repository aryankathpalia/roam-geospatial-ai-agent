<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { boundaryRefs, primaryCandidates } from '$lib/boundaryCandidates';
  import AgentChat from '$lib/AgentChat.svelte';
  import AccountButton from '$lib/components/AccountButton.svelte';
  import { user } from '$lib/auth';
  import { setCurrentDocument } from '$lib/currentDocument';

  const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';
  const LAST_DOCUMENT_KEY = 'roam:lastDocumentId';

  type Phase = 'idle' | 'uploading' | 'processing' | 'error' | 'done';

  // Pipeline stages in the order they actually execute server-side
  // (app/services/progress.py is the source of truth this mirrors).
  // "detail" starts null and fills in with a real count once that
  // stage's work is actually done -- rendered as an em-dash
  // placeholder until then, never a fake/simulated number.
  const STAGE_ORDER = ['rendering', 'layout_detection', 'triage', 'candidates_ready', 'roster', 'ocr', 'georeferencing', 'vision_extraction'] as const;
  const STAGE_LABELS: Record<string, string> = {
    rendering: 'Rendering document pages',
    layout_detection: 'Detecting layout & document structure',
    triage: 'Judging the role of each map sheet',
    candidates_ready: 'Parcel maps ready for boundary confirmation',
    roster: 'Reading parcel labels',
    ocr: 'Reading document text',
    georeferencing: 'Locating document on the map',
    vision_extraction: 'Extracting parcel boundary geometry'
  };

  type ProgressState = {
    stage: string;
    detail: string | null;
    error: string | null;
    history?: { stage: string; detail: string | null }[];
  };
  let progressState: ProgressState | null = null;
  $: traceCurrentIdx = progressState ? STAGE_ORDER.indexOf(progressState.stage as any) : -1;
  $: traceDetailByStage = Object.fromEntries(
    (progressState?.history ?? []).map((h) => [h.stage, h.detail])
  ) as Record<string, string | null>;
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let processingDocId: string | null = null;
  let processingStartedAt: number | null = null;

  let phase: Phase = 'idle';
  let errorMessage = '';
  let dragOver = false;
  let fileInput: HTMLInputElement;

  let result: any = null;
  let documentId: string | null = null;
  let usingSample = false;
  let showAllCategories = false;

  // Reprocessing a document re-runs it through Gemini from scratch --
  // expensive, slow, and non-deterministic (a fresh run can score worse
  // than the one you were just looking at). The result is now persisted
  // server-side (data/documents/{id}/result.json), so remembering the
  // last document_id here lets a page refresh resume it via GET instead
  // of forcing a full reupload.
  let lastDocumentId: string | null = null;
  let resuming = false;

  // ---- sample gallery, playground copies, own documents -----------------------------------------
  // A sample opens as a private playground copy (POST /samples/{id}/open): change anything, nothing is
  // kept. Which documents in this tab are playground copies (and of what) lives in sessionStorage, so the
  // banner survives a trip to the boundary editor and is gone with the tab.
  const PLAYGROUND_KEY = 'roam.playground';
  type Sample = { id: string; title: string; location: string; pages: number; parcels: number; highlight: string; thumbnail: string };
  let samples: Sample[] = [];
  let samplesError = '';
  let openingSample: string | null = null;

  function playgrounds(): Record<string, string> {
    try {
      return JSON.parse(sessionStorage.getItem(PLAYGROUND_KEY) ?? '{}');
    } catch {
      return {};
    }
  }
  $: if (documentId) setCurrentDocument(documentId);
  $: sandboxOf = documentId ? (playgrounds()[documentId] ?? null) : null;
  $: sandboxSample = sandboxOf ? (samples.find((sm) => sm.id === sandboxOf) ?? null) : null;

  async function loadSamples() {
    try {
      const res = await fetch(`${API_BASE}/samples`);
      if (!res.ok) throw new Error(`${res.status}`);
      samples = (await res.json()).samples;
    } catch {
      samplesError = `Could not reach the ROAM backend at ${API_BASE}.`;
    }
  }

  async function openSample(sample: Sample, fresh = false) {
    openingSample = sample.id;
    errorMessage = '';
    try {
      // reuse this tab's copy unless asked for a fresh one
      const existing = Object.entries(playgrounds()).find(([, of]) => of === sample.id)?.[0];
      let id: string | null = !fresh && existing ? existing : null;
      if (id && !(await fetch(`${API_BASE}/documents/${id}`)).ok) id = null; // expired on the server
      if (!id) {
        const res = await fetch(`${API_BASE}/samples/${sample.id}/open`, { method: 'POST' });
        const body = await res.json().catch(() => null);
        if (!res.ok) throw new Error(body?.detail ?? `${res.status} ${res.statusText}`);
        id = body.document_id as string;
        try {
          const kept = Object.fromEntries(Object.entries(playgrounds()).filter(([, of]) => of !== sample.id));
          sessionStorage.setItem(PLAYGROUND_KEY, JSON.stringify({ ...kept, [id]: sample.id }));
        } catch {
          // no session storage: the copy still works, the banner just will not survive a reload
        }
      }
      selectedKey = null;
      editingKey = null;
      await openDocument(id, false);
    } catch (err: any) {
      errorMessage = `Could not open the sample: ${err.message}`;
      phase = 'error';
    } finally {
      openingSample = null;
    }
  }

  function resetPlayground() {
    if (sandboxSample && confirm('Discard your changes and start this sample again?')) openSample(sandboxSample, true);
  }

  // the signed-in user's own uploads, listed under the upload box
  let myDocuments: { id: string; filename: string | null; created_at: number | null }[] = [];
  async function loadMyDocuments() {
    try {
      const res = await fetch(`${API_BASE}/me/documents`);
      myDocuments = res.ok ? (await res.json()).documents : [];
    } catch {
      myDocuments = [];
    }
  }
  $: if ($user) loadMyDocuments();
  else myDocuments = [];

  async function openDocument(id: string, remember = true) {
    lastDocumentId = id;
    await resumeLastDocument(remember);
  }

  onMount(() => {
    loadSamples();
    try {
      lastDocumentId = localStorage.getItem(LAST_DOCUMENT_KEY);
    } catch {
      // localStorage unavailable (private window, blocked storage) --
      // resume just won't be offered, upload still works normally.
    }
    // /boundary-review links back here with ?doc=<id> so the map shows the
    // just-confirmed geometry without a manual resume.
    const docParam = new URLSearchParams(window.location.search).get('doc');
    if (docParam) {
      lastDocumentId = docParam;
      resumeLastDocument();
    }
  });

  // Verification runs in the background after a boundary is confirmed
  // (calibration.status === 'pending'); refresh the document until it lands.
  // Also keep refreshing while the pipeline is still running, or while a parcel the user
  // outlined (sheet entity with a confirmed_polygon) hasn't been bound to its extracted parcel
  // yet -- an outline drawn before processing finished is only attached at the end, so a
  // snapshot taken in between shows the parcel as unconfirmed with no geometry.
  $: hasPendingVerification =
    result?.processing?.complete === false ||
    (result?.pages ?? []).some(
      (pg: any) =>
        controlReadInProgress(pg) ||
        (pg.regions ?? []).some((r: any) => (r.parcels ?? []).some((pc: any) => pc.calibration?.status === 'pending')) ||
        (pg.sheet?.parcels ?? []).some((e: any) => {
          if (!e.confirmed_polygon) return false;
          const ref = e.evidence_ref;
          if (!ref) return false; // nothing to bind to once processing is done -- don't poll forever
          return !pg.regions?.[ref.region]?.parcels?.[ref.parcel]?.human_confirmed;
        })
    );
  let pendingTimer: ReturnType<typeof setInterval> | null = null;
  $: if (hasPendingVerification && documentId && !usingSample) {
    if (!pendingTimer) pendingTimer = setInterval(refreshPending, 4000);
  } else if (pendingTimer) {
    clearInterval(pendingTimer);
    pendingTimer = null;
  }
  async function refreshPending() {
    if (!documentId) return;
    try {
      const res = await fetch(`${API_BASE}/documents/${documentId}`);
      if (!res.ok) return;
      const data = await res.json();
      result = data.result;
      queueMicrotask(renderMap);
    } catch {
      // transient -- try again on the next tick
    }
  }

  // Manual boundary confirmation (/boundary-review) is a step of the main
  // flow, offered only for the parcels ROAM's ParcelMap selection picked --
  // see $lib/boundaryCandidates.
  // Likely-target parcels when there are any; otherwise every extracted parcel -- the
  // user must always have a way to confirm, even when triage ranked every sheet as an
  // "other map" (it can be wrong; see $lib/boundaryCandidates).
  $: boundaryCandidates = (() => {
    if (usingSample || !result) return [];
    const refs = boundaryRefs(result);
    const primary = primaryCandidates(refs);
    return primary.length ? primary : refs.filter((r) => r.kind === 'parcel' && !r.excludedReason);
  })();
  $: boundaryConfirmedCount = boundaryCandidates.filter((r) => r.parcel.human_confirmed).length;

  function boundaryReviewHref(parcelKey?: string): string {
    const q = new URLSearchParams({ doc: documentId ?? '' });
    if (parcelKey) q.set('parcel', parcelKey);
    return `/boundary-review?${q.toString()}`;
  }

  function rememberDocumentId(id: string | null) {
    documentId = id;
    if (!id) return;
    try {
      localStorage.setItem(LAST_DOCUMENT_KEY, id);
    } catch {
      // best-effort only
    }
  }

  async function resumeLastDocument(remember = true) {
    if (!lastDocumentId) return;
    resuming = true;
    errorMessage = '';
    try {
      const res = await fetch(`${API_BASE}/documents/${lastDocumentId}`);
      if (!res.ok) {
        const body = await res.text();
        throw new Error(`${res.status}: ${body.slice(0, 200)}`);
      }
      const data = await res.json();
      const openedId = data.document_id ?? lastDocumentId;
      if (remember && !playgrounds()[openedId]) rememberDocumentId(openedId);
      else documentId = openedId;
      usingSample = false;
      if (data.status === 'processing') {
        // Only the candidate maps exist so far; wait for the finished result.
        processingDocId = data.document_id ?? lastDocumentId;
        processingStartedAt = Date.now();
        phase = 'processing';
        startPolling();
        return;
      }
      result = data.result;
      phase = 'done';
      queueMicrotask(renderMap);
    } catch (err: any) {
      errorMessage = `Could not resume the last document: ${err.message}`;
      phase = 'error';
    } finally {
      resuming = false;
    }
  }

  let map: any;
  let mapEl: HTMLDivElement;
  let L: any;
  let layerGroup: any;
  let lastFitBounds: any = null;

  let selectedKey: string | null = null;

  // Outline colour palette, per document page ('*' = every page), remembered in this browser.
  const PALETTE = ['#ff1f3d', '#12b53b', '#1c7ed6', '#ffd43b', '#d100d1', '#15c5d6', '#ff8c00', '#ffffff', '#000000'];
  let outlineColors: Record<string, string> = {};
  const colorsKey = () => `roam.outlineColors.${documentId ?? ''}`;
  function loadOutlineColors() {
    try {
      outlineColors = JSON.parse(localStorage.getItem(colorsKey()) ?? '{}') ?? {};
    } catch {
      outlineColors = {};
    }
  }
  $: if (documentId) loadOutlineColors();
  $: paletteScope = selectedKey ? Number(selectedKey.split('-')[0]) : null;
  function setOutlineColor(c: string | null) {
    const scope = paletteScope === null || Number.isNaN(paletteScope) ? '*' : String(paletteScope);
    const next = { ...outlineColors };
    if (c === null) delete next[scope];
    else next[scope] = c;
    outlineColors = next;
    try {
      localStorage.setItem(colorsKey(), JSON.stringify(next));
    } catch {
      // storage unavailable: the choice still applies for this visit
    }
    renderMap();
  }

  function regionKey(pageNumber: number, i: number) {
    return `${pageNumber}-${i}`;
  }

  // Flattened list of every PARCEL vision identified (not regions),
  // across every page -- INCLUDING ones with no boundary geometry. A
  // single ParcelMap region can hold more than one parcel -- a "Parcel
  // Map Exhibit" sheet showing two adjacent parcels side by side is
  // one region but two parcels (see app/services/vision.py's Parcel
  // Target Resolver) -- so this flattens one level deeper than region:
  // page -> region -> parcel.
  //
  // Deliberately NOT filtered to parcels with boundary_geojson_wgs84:
  // a parcel vision found a label for but couldn't attribute calls to
  // (extraction_note) or couldn't georeference (georeference_error) is
  // real, useful information -- it was silently dropped from this list
  // before, which meant those two fields were written by the backend
  // but never actually reached the screen. renderMap skips drawing a
  // polygon for these (there's nothing to draw) but they still appear
  // as cards with their reason shown.
  // When the document has parcel-map SHEETS with parcels (the roster the boundary editor offers), only the
  // extracted parcels bound to one of those sheet entries are real, listable parcels. The rest is evidence:
  // an extraction fragment flagged under-evidenced, or a metes-and-bounds legal description read from a text
  // page. A document that is only a legal description (no drawing, so no sheet entries) keeps listing
  // whatever was extracted.
  $: sheetBound = (() => {
    const bound = new Set<string>();
    let anySheet = false;
    for (const p of result?.pages ?? []) {
      for (const e of p.sheet?.parcels ?? []) {
        anySheet = true;
        if (e.excluded_reason || !e.evidence_ref) continue;
        bound.add(`${p.page_number}-${e.evidence_ref.region}-${e.evidence_ref.parcel}`);
      }
    }
    return anySheet ? bound : null;
  })();

  $: allParcelRegions = result
    ? (result.pages ?? []).flatMap((p: any) =>
        (p.regions ?? []).flatMap((region: any, regionIndex: number) =>
          (region.parcels ?? [])
            .map((parcel: any, parcelIndex: number) => ({
              page: p.page_number,
              regionIndex,
              parcelIndex,
              i: `${regionIndex}-${parcelIndex}`,
              category: region.category ?? null,
              parcel
            }))
            .filter((e: any) => !sheetBound || sheetBound.has(`${e.page}-${e.regionIndex}-${e.parcelIndex}`))
        )
      )
    : [];

  // Region classification (idea 7) is a DISPLAY filter, never a drop --
  // every region still gets extracted server-side regardless of
  // category. "not_a_parcel_drawing" content is deprioritized by
  // default (it was the majority, 72/132, of noise in the labeled
  // corpus) but always one click away via showAllCategories, so a
  // misclassification costs a click, never a lost parcel.
  // A parcel read off a non-target sheet (aerial/location map, reference survey) whose label matches
  // one on the target parcel map is the SAME land drawn twice -- e.g. the Patnaude packet's page-6
  // display map repeats page 7's "Remainder Parcel 17-2-1-4" and "Parcel 1". Listing it as its own
  // card (with "Boundary not confirmed") reads as extra parcels, so it is never listed. Matched by
  // word set, so "REMAINDER PARCEL" matches "REMAINDER PARCEL 17-2-1-4" but "PARCEL 1" never
  // matches "PARCEL 10".
  const labelWords = (s: string | null | undefined) =>
    new Set((s ?? '').toUpperCase().replace(/[^A-Z0-9\s-]/g, ' ').split(/\s+/).filter(Boolean));
  $: targetLabelSets = (result?.pages ?? [])
    .filter((p: any) => p.sheet?.role === 'target_parcel_map')
    .flatMap((p: any) => (p.regions ?? []).flatMap((r: any) => (r.parcels ?? []).map((pc: any) => labelWords(pc.vision_geometry?.parcel_label))))
    .filter((s: Set<string>) => s.size > 0);
  function isOtherSheetCopy(entry: any): boolean {
    const page = (result?.pages ?? []).find((p: any) => p.page_number === entry.page);
    const role = page?.sheet?.role;
    if (!role || role === 'target_parcel_map') return false;
    const words = labelWords(entry.parcel.vision_geometry?.parcel_label);
    return targetLabelSets.some((t: Set<string>) => {
      const [small, big] = t.size <= words.size ? [t, words] : [words, t];
      return [...small].every((w) => big.has(w));
    });
  }

  // The same parcel is often drawn on several sheets of one packet (Imperial County plat: Parcels A, B
  // and C on pages 9, 21, 23 and 25 -- twelve cards for three parcels). Cards of one parcel (label
  // words contained in each other, stated acreage within 8% when both state one) on DIFFERENT pages
  // collapse to the one worth looking at: the confirmed one, else the one that has geometry, else the
  // first page. Removed (deleted) parcels are never listed.
  const statedAcres = (e: any) => {
    const m = String(e.parcel.vision_geometry?.stated_area_acres ?? '').match(/\d+(?:\.\d+)?/);
    return m ? parseFloat(m[0]) : null;
  };
  function sameParcel(a: any, b: any): boolean {
    if (a.page === b.page) return false;
    const wa = labelWords(a.parcel.vision_geometry?.parcel_label);
    const wb = labelWords(b.parcel.vision_geometry?.parcel_label);
    if (!wa.size || !wb.size) return false;
    const [small, big] = wa.size <= wb.size ? [wa, wb] : [wb, wa];
    if (![...small].every((w) => big.has(w))) return false;
    const aa = statedAcres(a);
    const ab = statedAcres(b);
    return aa === null || ab === null || Math.abs(aa - ab) <= 0.08 * Math.max(aa, ab);
  }
  const copyRank = (e: any) =>
    (e.parcel.human_confirmed ? 0 : 4) + (e.parcel.boundary_geojson_wgs84 ? 0 : 2) + (e.parcel.spatial_validation ? 0 : 1);
  function dedupeCopies(list: any[]): any[] {
    const kept: any[] = [];
    for (const e of [...list].sort((x, y) => copyRank(x) - copyRank(y) || x.page - y.page)) {
      if (!kept.some((k) => sameParcel(k, e))) kept.push(e);
    }
    return list.filter((e) => kept.includes(e));
  }

  $: parcelRegions = dedupeCopies(
    (showAllCategories
      ? allParcelRegions
      : allParcelRegions.filter((r: any) => r.category !== 'not_a_parcel_drawing')
    ).filter((r: any) => !isOtherSheetCopy(r) && !r.parcel.deleted)
  );

  $: hiddenCount = allParcelRegions.filter((r: any) => r.category === 'not_a_parcel_drawing' && !isOtherSheetCopy(r)).length;

  // Parcels vision found no attributable boundary calls for ("No
  // geometry" cards) are real, useful info -- but a document can have a
  // dozen of them ahead of the 2-3 real ParcelMap results, burying what
  // the reviewer actually came to look at. Collapsed by default, one
  // click away, same pattern as the category filter above.
  let showNoGeometry = false;
  // A confirmed parcel still being calibrated/placed has no spatial_validation yet, but it is a
  // real parcel mid-processing, not a "no geometry" region -- keep it (with its spinner) in the
  // main list instead of the collapsed group.
  $: geometryRegions = parcelRegions.filter((e: any) => e.parcel.spatial_validation || verdict(e.parcel, e.page).busy);
  $: noGeometryRegions = parcelRegions.filter((e: any) => !e.parcel.spatial_validation && !verdict(e.parcel, e.page).busy);

  async function ensureLeaflet() {
    if (!L) {
      L = await import('leaflet');
    }
  }

  async function renderMap(keepView = false) {
    await ensureLeaflet();
    if (!map) {
      map = L.map(mapEl, { zoomControl: true, fadeAnimation: false, zoomAnimation: false, maxZoom: 23 });
      // Satellite imagery, not OSM's street-vector style -- a parcel
      // can sit on undeveloped rural land with almost nothing drawn at
      // high zoom in the vector style, which reads as "broken" even
      // when it's rendering correctly. Imagery always has ground detail.
      // maxNativeZoom caps actual tile requests at Esri's real resolution
      // (beyond ~19 most areas have no sharper imagery); maxZoom lets the
      // user keep zooming past that with Leaflet upscaling the last tile,
      // which is still useful for lining up a boundary call against a
      // fence or corner precisely rather than being capped at 18.
      L.tileLayer(
        'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        { attribution: 'Esri, Maxar, Earthstar Geographics', maxZoom: 23, maxNativeZoom: 19 }
      ).addTo(map);
      // Settle the container's real size before the first fitBounds --
      // calling fitBounds before the size is known (or calling it twice
      // in quick succession, e.g. an initial setView followed shortly
      // by a fitBounds) makes Leaflet abandon in-flight tile fetches
      // for the first view when the second one starts, leaving broken/
      // half-loaded tile fragments instead of a clean render.
      map.invalidateSize(false);
    }

    if (layerGroup) layerGroup.remove();
    layerGroup = L.layerGroup().addTo(map);
    clearVertexMarkers();

    const bounds: any[] = [];
    layerByKey = new Map();

    for (const entryItem of parcelRegions) {
      const { page, i, parcel } = entryItem;
      if (!parcel.boundary_geojson_wgs84) continue; // nothing to draw -- see card for why
      // The vision-extracted outline is evidence, not a result: only a boundary the
      // user confirmed is drawn.
      if (!parcel.human_confirmed) continue;

      const shapeOk = parcel.spatial_validation?.valid;
      const locationPrecision = parcelLocationPrecision(parcel);
      const gate = calibrationGate(parcel);
      // Vivid on purpose: grey outlines vanished into roads and rooftops on satellite imagery. Green =
      // placed and verified; orange = shape fine, location approximate; red = placement not corroborated;
      // magenta = the shape itself failed its checks. A colour picked from the palette overrides all.
      const autoColor = gate === 'unconfirmed' || gate === 'pending'
        ? '#ff1f3d'
        : shapeOk
          ? (parcel.human_confirmed
              ? confirmedPlacementOk(parcel)
              : locationPrecision === 'surveyed' || locationPrecision === 'manual')
            ? '#12b53b'
            : '#ff8c00'
          : '#d100d1';
      const color = outlineColors[page] ?? outlineColors['*'] ?? autoColor;
      const key = regionKey(page, i);

      const layer = L.geoJSON(parcel.boundary_geojson_wgs84, {
        style: {
          color,
          weight: selectedKey === key ? 4.5 : 3,
          fillColor: color,
          fillOpacity: selectedKey === key ? 0.3 : 0.18
        }
      });

      layer.on('click', () => {
        if (justDragged) return; // the mouse-up that ended a drag is not a selection
        selectRegion(key);
      });
      layer.on('mousedown', (e: any) => startMove(e, entryItem));
      layerByKey.set(key, layer);
      layer.addTo(layerGroup);

      if (editingKey === key) {
        addDraggableVertices(entryItem, layer);
      }

      const b = layer.getBounds();
      if (b.isValid()) bounds.push(b);
    }

    if (bounds.length) {
      let combined = bounds[0];
      for (const b of bounds.slice(1)) combined = combined.extend(b);
      lastFitBounds = combined;
      if (!keepView) map.fitBounds(combined, { padding: [40, 40], animate: false });
    } else {
      lastFitBounds = null;
      map.setView([20, 0], 2);
    }
    drawAgentPreview();
  }

  // ---- AI placement review ---------------------------------------------------------------------
  // The chat panel (lib/AgentChat.svelte) proposes fixes; a proposed move is previewed here as a dashed
  // outline of the sheet's parcels at the proposed spot, and applying one reloads the document.
  let agentOpen = false;
  let agentFocus: { page: number; label: string | null } | null = null;
  let agentPreview: any = null;
  let previewLayer: any = null;

  function openAgent(entry: any | null) {
    agentFocus = entry ? { page: entry.page, label: entry.parcel?.vision_geometry?.parcel_label ?? null } : null;
    agentOpen = true;
    setTimeout(() => document.querySelector('.agent')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50);
  }
  function closeAgent() {
    agentOpen = false;
    setAgentPreview(null);
  }
  function setAgentPreview(p: any) {
    agentPreview = p;
    drawAgentPreview(true);
  }
  function drawAgentPreview(fit = false) {
    if (previewLayer) {
      previewLayer.remove();
      previewLayer = null;
    }
    if (!map || !L || !agentPreview || agentPreview.kind !== 'move') return;
    const shift = (coords: any[]) =>
      coords.map(([lng, lat]: number[]) => [
        lng + agentPreview.east_m / mPerDegLng(lat), lat + agentPreview.north_m / M_PER_DEG_LAT
      ]);
    previewLayer = L.layerGroup().addTo(map);
    for (const e of parcelRegions) {
      if (e.page !== agentPreview.page_number || !e.parcel.human_confirmed || !e.parcel.boundary_geojson_wgs84) continue;
      const g = e.parcel.boundary_geojson_wgs84.geometry;
      const moved = { ...e.parcel.boundary_geojson_wgs84, geometry: { ...g, coordinates: g.coordinates.map(shift) } };
      L.geoJSON(moved, {
        interactive: false,
        style: { color: '#4f46e5', weight: 3, dashArray: '8 6', fillColor: '#6366f1', fillOpacity: 0.12 }
      }).addTo(previewLayer);
    }
    if (fit) {
      // show where it is now AND where it would go
      let b: any = null;
      previewLayer.eachLayer((l: any) => (b = b ? b.extend(l.getBounds()) : l.getBounds()));
      if (b && lastFitBounds) b = b.extend(lastFitBounds);
      if (b?.isValid()) map.fitBounds(b, { padding: [40, 40], animate: false });
    }
  }
  async function onAgentApplied() {
    setAgentPreview(null);
    await refreshPending();
  }

  // ---- drag-to-move ----------------------------------------------------------------------------
  // Pick a parcel up and drop it where it belongs: a translation only, applied to the whole sheet's
  // confirmed parcels together by default (so shared edges stay shared) or to one parcel. The server
  // stores it as an offset from the computed position (POST /move-parcels), which survives
  // re-verification and which the automatic fits leave alone.
  let moveMode = false;
  let moveWholeSheet = true;
  let moveError = '';
  let justDragged = false;
  let layerByKey: Map<string, any> = new Map();
  let moving: { entries: any[]; start: any; originals: Map<string, any>; moved: boolean } | null = null;

  const M_PER_DEG_LAT = 110574;
  const mPerDegLng = (lat: number) => 111320 * Math.cos((lat * Math.PI) / 180);

  function moveGroupFor(entry: any): any[] {
    const ok = (e: any) => e.parcel.human_confirmed && e.parcel.boundary_geojson_wgs84;
    if (!moveWholeSheet) return [entry];
    return parcelRegions.filter((e: any) => e.page === entry.page && e.regionIndex === entry.regionIndex && ok(e));
  }

  function shiftLatLngs(ls: any, dLat: number, dLng: number): any {
    return Array.isArray(ls) ? ls.map((x: any) => shiftLatLngs(x, dLat, dLng)) : L.latLng(ls.lat + dLat, ls.lng + dLng);
  }

  function startMove(e: any, entry: any) {
    if (!moveMode || !entry.parcel.human_confirmed || !entry.parcel.boundary_geojson_wgs84) return;
    L.DomEvent.stopPropagation(e);
    map.dragging.disable();
    const entries = moveGroupFor(entry);
    const originals = new Map<string, any[]>();
    for (const en of entries) {
      const layer = layerByKey.get(regionKey(en.page, en.i));
      if (layer) originals.set(regionKey(en.page, en.i), layer.getLayers().map((l: any) => l.getLatLngs()));
    }
    moving = { entries, start: e.latlng, originals, moved: false };
    moveError = '';
    map.on('mousemove', onMoveDrag);
    map.once('mouseup', endMove);
    document.addEventListener('mouseup', endMove, { once: true });
  }

  function onMoveDrag(e: any) {
    if (!moving) return;
    moving.moved = true;
    const dLat = e.latlng.lat - moving.start.lat;
    const dLng = e.latlng.lng - moving.start.lng;
    for (const [key, originals] of moving.originals) {
      const layer = layerByKey.get(key);
      layer?.getLayers().forEach((l: any, i: number) => l.setLatLngs(shiftLatLngs(originals[i], dLat, dLng)));
    }
  }

  async function endMove(e?: any) {
    if (!moving) return;
    const m = moving;
    moving = null;
    map.off('mousemove', onMoveDrag);
    document.removeEventListener('mouseup', endMove);
    map.dragging.enable();
    const end = e?.latlng ?? m.start;
    const east = (end.lng - m.start.lng) * mPerDegLng(m.start.lat);
    const north = (end.lat - m.start.lat) * M_PER_DEG_LAT;
    if (!m.moved || Math.hypot(east, north) < 0.3) {
      renderMap(true); // a click, not a drag
      return;
    }
    justDragged = true;
    setTimeout(() => (justDragged = false), 250);
    await postMove(m.entries, east, north);
  }

  async function postMove(entries: any[], east: number, north: number) {
    const first = entries[0];
    try {
      const res = await fetch(`${API_BASE}/documents/${documentId}/move-parcels`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          page_number: first.page,
          region_index: first.regionIndex,
          parcel_indexes: entries.map((en: any) => en.parcelIndex),
          east_m: east,
          north_m: north
        })
      });
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail ?? `${res.status} ${res.statusText}`);
      applyServerParcels(first, entries, (await res.json()).parcels);
    } catch (err) {
      moveError = err instanceof Error ? err.message : 'Could not move the parcel.';
      renderMap(true);
    }
  }

  function applyServerParcels(first: any, entries: any[], parcels: any[]) {
    const page = result.pages.find((p: any) => p.page_number === first.page);
    const list = page?.regions?.[first.regionIndex]?.parcels;
    if (!list) return;
    entries.forEach((en: any, k: number) => (list[en.parcelIndex] = parcels[k]));
    result = result;
    queueMicrotask(() => renderMap(true));
  }

  async function resetPosition(entry: any) {
    const entries = moveGroupFor(entry);
    try {
      const res = await fetch(`${API_BASE}/documents/${documentId}/reset-position`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          page_number: entry.page,
          region_index: entry.regionIndex,
          parcel_indexes: entries.map((en: any) => en.parcelIndex)
        })
      });
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail ?? `${res.status} ${res.statusText}`);
      applyServerParcels(entry, entries, (await res.json()).parcels);
    } catch (err) {
      moveError = err instanceof Error ? err.message : 'Could not reset the position.';
    }
  }

  // Arrow keys nudge the selected parcel (1 m; Shift 5 m) while move mode is on; quick presses are
  // sent as one move.
  let nudge = { east: 0, north: 0 };
  let nudgeTimer: ReturnType<typeof setTimeout> | null = null;
  function onKeydown(e: KeyboardEvent) {
    if (!moveMode || !selectedKey) return;
    const tag = (e.target as HTMLElement | null)?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    const step = e.shiftKey ? 5 : 1;
    const d: Record<string, [number, number]> = {
      ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, step], ArrowDown: [0, -step]
    };
    const delta = d[e.key];
    if (!delta) return;
    e.preventDefault();
    nudge = { east: nudge.east + delta[0], north: nudge.north + delta[1] };
    if (nudgeTimer) clearTimeout(nudgeTimer);
    nudgeTimer = setTimeout(async () => {
      const entry = parcelRegions.find((r: any) => regionKey(r.page, r.i) === selectedKey);
      const send = nudge;
      nudge = { east: 0, north: 0 };
      if (entry && entry.parcel.human_confirmed && entry.parcel.boundary_geojson_wgs84) await postMove(moveGroupFor(entry), send.east, send.north);
    }, 350);
  }

  $: if (map) {
    moveMode;
    map.getContainer().style.cursor = moveMode ? 'move' : '';
  }

  // Bearing/distance between two lat/lng points, formatted to match the
  // "N 45°30'15" E" quadrant-bearing strings the backend traverse walker
  // expects -- lets a dragged vertex feed straight back into
  // recompute_parcel_from_calls without a separate coordinate-based path.
  const _EARTH_RADIUS_FT = 20_925_646.3;

  function bearingDistanceBetween(from: { lat: number; lng: number }, to: { lat: number; lng: number }) {
    const toRad = (d: number) => (d * Math.PI) / 180;
    const toDeg = (r: number) => (r * 180) / Math.PI;
    const phi1 = toRad(from.lat);
    const phi2 = toRad(to.lat);
    const dPhi = toRad(to.lat - from.lat);
    const dLambda = toRad(to.lng - from.lng);

    const a =
      Math.sin(dPhi / 2) ** 2 + Math.cos(phi1) * Math.cos(phi2) * Math.sin(dLambda / 2) ** 2;
    const distanceFt = _EARTH_RADIUS_FT * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));

    const y = Math.sin(dLambda) * Math.cos(phi2);
    const x = Math.cos(phi1) * Math.sin(phi2) - Math.sin(phi1) * Math.cos(phi2) * Math.cos(dLambda);
    const compassBearing = (toDeg(Math.atan2(y, x)) + 360) % 360;

    let ns: 'N' | 'S';
    let ew: 'E' | 'W';
    let quadrantAngle: number;
    if (compassBearing <= 90) {
      ns = 'N'; ew = 'E'; quadrantAngle = compassBearing;
    } else if (compassBearing <= 180) {
      ns = 'S'; ew = 'E'; quadrantAngle = 180 - compassBearing;
    } else if (compassBearing <= 270) {
      ns = 'S'; ew = 'W'; quadrantAngle = compassBearing - 180;
    } else {
      ns = 'N'; ew = 'W'; quadrantAngle = 360 - compassBearing;
    }

    const deg = Math.floor(quadrantAngle);
    const minFloat = (quadrantAngle - deg) * 60;
    const min = Math.floor(minFloat);
    const sec = Math.round((minFloat - min) * 60);

    const bearing = `${ns} ${deg}°${String(min).padStart(2, '0')}'${String(sec).padStart(2, '0')}"${ew}`;
    const distance = `${distanceFt.toFixed(2)}'`;
    return { bearing, distance };
  }

  let vertexMarkers: any[] = [];

  function clearVertexMarkers() {
    for (const m of vertexMarkers) m.remove();
    vertexMarkers = [];
  }

  function addDraggableVertices(entry: any, layer: any) {
    const ring = entry.parcel.boundary_geojson_wgs84?.geometry?.coordinates?.[0];
    if (!ring || ring.length < 3) return;
    // GeoJSON repeats the first point at the end to close the ring --
    // drop that duplicate, and drop the anchor (vertex 0) from being
    // draggable since it's the georeferenced tie point, not a call.
    const points = ring.slice(0, -1).map(([lng, lat]: [number, number]) => ({ lat, lng }));

    points.forEach((pt: { lat: number; lng: number }, idx: number) => {
      if (idx === 0) return; // anchor stays fixed
      const marker = L.circleMarker([pt.lat, pt.lng], {
        radius: 6,
        color: '#1d4ed8',
        fillColor: '#3b82f6',
        fillOpacity: 1,
        weight: 2,
        draggable: true
      }).addTo(layerGroup);

      // L.CircleMarker has no built-in drag support, so drag is wired
      // manually: disable map panning on mousedown, follow the mouse via
      // the map's own mousemove/mouseup (not the tiny marker element),
      // and only commit (recompute bearings, hit the backend) on mouseup.
      marker.on('mousedown', () => {
        map.dragging.disable();
        const onMove = (e: any) => {
          marker.setLatLng(e.latlng);
          const updated = points.map((p, i) => (i === idx ? { lat: e.latlng.lat, lng: e.latlng.lng } : p));
          const closedRing = [...updated, updated[0]].map((p) => [p.lng, p.lat]);
          layer.clearLayers();
          layer.addData({ type: 'Feature', geometry: { type: 'Polygon', coordinates: [closedRing] } });
        };
        const onUp = (e: any) => {
          map.off('mousemove', onMove);
          map.off('mouseup', onUp);
          map.dragging.enable();

          const finalPoint = { lat: e.latlng.lat, lng: e.latlng.lng };
          const prev = points[idx - 1];
          const next = points[(idx + 1) % points.length];
          // Only line calls get rewritten by a drag -- a curve's chord
          // is derived from radius/delta/turn, not a bearing/distance
          // pair, so overwriting a curve slot here would silently turn
          // it into a bogus straight line. Dragging a vertex next to a
          // curve just doesn't adjust that curve; edit it by hand instead.
          const newCalls = [...editCalls];
          if (newCalls[idx - 1]?.type === 'line') newCalls[idx - 1] = { type: 'line', ...bearingDistanceBetween(prev, finalPoint) };
          const nextIdx = idx % newCalls.length;
          if (newCalls[nextIdx]?.type === 'line') newCalls[nextIdx] = { type: 'line', ...bearingDistanceBetween(finalPoint, next) };
          editCalls = newCalls;
          submitRecompute(entry, false, true);
        };
        map.on('mousemove', onMove);
        map.on('mouseup', onUp);
      });

      vertexMarkers.push(marker);
    });
  }

  function recenter() {
    if (!map) return;
    if (lastFitBounds) {
      map.fitBounds(lastFitBounds, { padding: [40, 40] });
    } else {
      map.setView([20, 0], 2);
    }
  }

  function selectRegion(key: string) {
    selectedKey = selectedKey === key ? null : key;
    if (selectedKey !== key) {
      editingKey = null;
    }
    renderMap();
  }

  // ---------------------------------------------------------------
  // Human-in-the-loop review: editable call table with live recompute
  // (idea 1), plus a large side-by-side reference viewer so the
  // reviewer can read the source drawing while editing. Not available
  // on the static sample result, since there's no real backend
  // document behind it.
  // ---------------------------------------------------------------

  let editingKey: string | null = null;
  type EditCall =
    | { type: 'line'; bearing: string; distance: string }
    | {
        type: 'curve';
        radius: string;
        arc_length: string;
        delta: string;
        turn: 'L' | 'R';
        start_tangent_bearing: string;
      };
  let editCalls: EditCall[] = [];
  let editAnchorLat = '';
  let editAnchorLon = '';
  let recomputing = false;
  let recomputeError = '';


  // Two independent checks. "Shape" is the traverse's own math
  // (closes, doesn't cross itself, matches stated acreage). "Location"
  // is how the shape was pinned to the map: only coordinates printed on
  // the document are surveyed-accurate; a geocoded street address lands
  // on the address point, not the parcel's corner. Green only when both
  // pass -- a perfect shape in the wrong spot is not a correct result.
  const LOCATION_LABELS: Record<string, string> = {
    surveyed: 'Surveyed coordinates',
    // A PLSS section-corner monument the document names, resolved against
    // BLM's own data -- real, but not yet "surveyed"-grade confidence
    // here because nothing else on the sheet corroborated it (see
    // app/services/plss.py). Corroborated PLSS resolutions are reported
    // as plain 'surveyed', same as any other fully-trusted source.
    plss_single_source: 'PLSS monument (uncorroborated)',
    aliquot: 'Matches PLSS legal description (BLM)',
    control: 'Fitted to 2 printed survey control points',
    apn: 'Fitted to county parcel records (APN)',
    apn_parcel: 'County parcel record (APN)',
    // The document anchor is a printed monument coordinate, but nothing confirmed where THIS outline
    // sits relative to it -- shown amber, not green.
    surveyed_anchor: 'Near a printed survey monument (position unconfirmed)',
    street: 'Street address (approx.)',
    city: 'City / ZIP only (coarse)',
    manual: 'Manually pinned',
    none: 'Not placed'
  };

  $: anchorPrecision = !result
    ? 'none'
    : result.anchor?.precision ?? (result.anchor_lat != null ? 'street' : 'none');
  $: anchorSource = result?.anchor?.source ?? null;
  $: aliquotFit = (result?.pages ?? [])
    .flatMap((p: any) => (p.regions ?? []).flatMap((r: any) => r.parcels ?? []))
    .map((pc: any) => pc.placement?.aliquot_fit)
    .find((f: any) => f?.corroborated) ?? null;

  // A reviewer can pin one parcel's anchor by hand (below the call
  // editor) rather than trust the document-wide geocoded/state-plane
  // guess -- that override is per-parcel, so its location check has to
  // be evaluated per-parcel too, not from the shared anchorPrecision.
  function parcelLocationPrecision(parcel: any): string {
    if (parcel.anchor_override || parcel.manual_position) return 'manual';
    // Fitted onto the BLM aliquot part the legal description names, with combined area and both
    // outer extents agreeing within 2% (app/routes/documents.py::_fit_sheet_to_aliquot).
    if (parcel.placement?.aliquot_fit?.corroborated) return 'aliquot';
    // Placed from two printed surveyed coordinates, the second predicted from the first within tolerance
    // (app/services/control_points.py).
    if (parcel.placement?.control_fit?.validated) return 'control';
    // Seated between (or onto) the parcels the plat names by APN, in the county's own parcel layer
    // (app/routes/documents.py::_fit_sheet_to_apn).
    if (parcel.placement?.apn_fit?.corroborated) return 'apn';
    if (parcel.human_confirmed && anchorPrecision === 'surveyed' && !confirmedPlacementOk(parcel)) return 'surveyed_anchor';
    return anchorPrecision;
  }
  const locationConfirmed = (precision: string) => precision === 'surveyed' || precision === 'manual' || precision === 'aliquot' || precision === 'control' || precision === 'apn';

  // A confirmed boundary's SHAPE can close and match the stated area
  // (spatial_validation.valid) while its real-world scale/rotation were
  // never independently checked against anything the document actually
  // prints -- that's what parcel.calibration.status tracks (see
  // app/services/calibration.py), and it is a wholly separate question
  // from shape validity or anchor precision. A parcel calibration marked
  // "unverified" -- or one that was human_confirmed but never reached
  // calibration at all, e.g. because this document has no usable anchor
  // -- must never present as trusted here just because its outline
  // happens to close and its anchor happens to be geocoded. Only
  // parcels never run through confirm-boundary at all are unaffected:
  // calibration doesn't apply to them, so they fall through to the
  // pre-existing shape/anchor-precision verdict unchanged.
  function calibrationGate(parcel: any): 'not_applicable' | 'placeable' | 'unconfirmed' | 'pending' {
    if (!parcel.human_confirmed) return 'not_applicable';
    const status = parcel.calibration?.status;
    if (status === 'pending') return 'pending';
    if (status === 'cross_validated' || status === 'single_source') return 'placeable';
    // Rotation/scale unverified from printed bearings, but the outline's area AND extents match the
    // BLM aliquot part within 2% -- that independently corroborates scale and orientation too.
    if (parcel.placement?.aliquot_fit?.corroborated || parcel.placement?.control_fit?.validated || parcel.placement?.apn_fit?.corroborated) return 'placeable';
    // status === 'unverified', or calibration missing entirely despite
    // human_confirmed (no anchor to calibrate against) -- both mean the
    // same thing to a viewer: this placement was never corroborated.
    return 'unconfirmed';
  }

  // Calibration only verifies scale/rotation. WHERE a confirmed polygon
  // sits is parcel.placement.status (app/services/placement.py):
  // "surveyed_corner" means one of its own vertices was bound to a printed
  // parcel-corner coordinate in a CRS the sheet states. The document-level
  // anchorPrecision can say "surveyed" merely because the sheet prints a
  // control monument somewhere -- confirmed on NVZ, where that put Parcel 1
  // ~2,600 ft off -- so it must not stand in for a confirmed parcel's own
  // placement.
  function confirmedPlacementOk(parcel: any): boolean {
    return !!parcel.anchor_override || !!parcel.manual_position || parcel.placement?.status === 'surveyed_corner' || !!parcel.placement?.aliquot_fit?.corroborated || !!parcel.placement?.apn_fit?.corroborated;
  }

  // The user drew an outline for this parcel (its sheet entity holds a confirmed_polygon) but it
  // hasn't been bound to this extracted parcel yet -- that happens when processing finishes.
  function outlineAwaitingBind(parcel: any): boolean {
    if (parcel.human_confirmed || !parcel.roster_id) return false;
    return (result?.pages ?? []).some((pg: any) =>
      (pg.sheet?.parcels ?? []).some((e: any) => e.id === parcel.roster_id && e.confirmed_polygon)
    );
  }

  // The server is still reading the page's printed survey coordinates to refine this sheet's placement
  // (a few minutes; a marker older than 8 min is a read that died with its process).
  function controlReadInProgress(pg: any): boolean {
    const cp = pg?.control_points;
    return cp?.status === 'reading' && Date.now() / 1000 - (cp.started_at ?? 0) < 480;
  }

  function verdict(parcel: any, pageNumber?: number): { cls: string; label: string; busy?: boolean } {
    const shapeOk = !!parcel.spatial_validation?.valid;
    const locationPrecision = parcelLocationPrecision(parcel);
    const locationOk = parcel.human_confirmed
      ? confirmedPlacementOk(parcel)
      : locationPrecision === 'surveyed' || locationPrecision === 'manual';
    if (outlineAwaitingBind(parcel)) return { cls: 'busy', label: 'Confirming boundary…', busy: true };
    if (!parcel.human_confirmed) return { cls: 'moderate', label: 'Boundary not confirmed' };
    if (
      pageNumber !== undefined &&
      parcel.placement?.status !== 'surveyed_corner' &&
      !parcel.placement?.apn_fit?.corroborated &&
      controlReadInProgress((result?.pages ?? []).find((p: any) => p.page_number === pageNumber))
    ) {
      return { cls: 'busy', label: 'Refining placement…', busy: true };
    }
    const gate = calibrationGate(parcel);
    if (gate === 'pending' || (parcel.placement?.status === 'pending')) {
      return { cls: 'busy', label: 'Calibrating & placing…', busy: true };
    }
    if (!parcel.spatial_validation) return { cls: 'moderate', label: 'Outline saved · not placed on the map' };
    // The reviewer put it where it belongs: the computed placement's confidence no longer describes it.
    if (parcel.manual_position || parcel.anchor_override) {
      return shapeOk ? { cls: 'moderate', label: 'Placed by hand' } : { cls: 'high', label: 'Placed by hand · check area' };
    }
    if (gate === 'unconfirmed') {
      return {
        cls: 'unconfirmed',
        label: parcel.calibration?.status === 'unverified'
          ? 'Unconfirmed placement'
          : 'Unconfirmed placement · no anchor'
      };
    }
    if (shapeOk && locationOk) return { cls: 'low', label: 'Verified' };
    if (shapeOk) return { cls: 'moderate', label: 'Shape OK · location approx.' };
    return { cls: 'high', label: 'Needs review' };
  }

  // Large side-by-side reference viewer (pan / zoom / rotate).
  let viewer: { page: number; regionIndex: number } | null = null;
  let viewerMode: 'region' | 'page' = 'region';
  let zoom = 1;
  let panX = 0;
  let panY = 0;
  let rotation = 0;
  let dragging = false;
  let dragStart = { x: 0, y: 0, panX: 0, panY: 0 };

  function openViewer(entry: any) {
    viewer = { page: entry.page, regionIndex: entry.regionIndex };
    viewerMode = 'region';
    resetView();
    queueMicrotask(() => map?.invalidateSize());
  }

  function closeViewer() {
    viewer = null;
    queueMicrotask(() => map?.invalidateSize());
  }

  function resetView() {
    zoom = 1;
    panX = 0;
    panY = 0;
    rotation = 0;
  }

  function zoomBy(factor: number) {
    zoom = Math.min(12, Math.max(0.3, zoom * factor));
  }

  function onViewerWheel(e: WheelEvent) {
    e.preventDefault();
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const cx = e.clientX - rect.left - rect.width / 2;
    const cy = e.clientY - rect.top - rect.height / 2;
    const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    const next = Math.min(12, Math.max(0.3, zoom * factor));
    // keep the point under the cursor fixed while zooming
    panX = cx - ((cx - panX) * next) / zoom;
    panY = cy - ((cy - panY) * next) / zoom;
    zoom = next;
  }

  function onViewerDown(e: PointerEvent) {
    dragging = true;
    dragStart = { x: e.clientX, y: e.clientY, panX, panY };
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  }

  function onViewerMove(e: PointerEvent) {
    if (!dragging) return;
    panX = dragStart.panX + (e.clientX - dragStart.x);
    panY = dragStart.panY + (e.clientY - dragStart.y);
  }

  function onViewerUp() {
    dragging = false;
  }

  $: viewerSrc = viewer && documentId
    ? viewerMode === 'page'
      ? `${API_BASE}/documents/${documentId}/pages/${viewer.page}.png`
      : `${API_BASE}/documents/${documentId}/pages/${viewer.page}/regions/${viewer.regionIndex}/crop.png`
    : '';

  // A call row is either a straight line ({bearing, distance}) or a
  // circular curve ({radius, arc_length, delta, turn: 'L'|'R'}) -- same
  // two shapes app/services/geometry.py's walk_traverse already
  // understands (call_type: "curve" for the latter). Curve support
  // already existed server-side; this editor previously only read
  // bearing/distance, so opening "Edit calls" on a parcel with a real
  // curve in it silently dropped that curve the moment you saved.
  function callsToEdit(parcel: any) {
    const source = parcel.resolved_boundary_calls ?? parcel.vision_geometry?.boundary_calls ?? [];
    return source.map((c: any) =>
      c.call_type === 'curve'
        ? {
            type: 'curve',
            radius: c.radius ?? '',
            arc_length: c.arc_length ?? '',
            delta: c.delta ?? '',
            turn: c.turn === 'L' ? 'L' : 'R',
            start_tangent_bearing: c.start_tangent_bearing ?? ''
          }
        : { type: 'line', bearing: c.bearing ?? '', distance: c.distance ?? '' }
    );
  }

  function startEdit(entry: any) {
    const key = regionKey(entry.page, entry.i);
    if (editingKey === key) {
      editingKey = null;
      renderMap();
      return;
    }
    editingKey = key;
    editCalls = callsToEdit(entry.parcel);
    const override = entry.parcel.anchor_override;
    editAnchorLat = override ? String(override.lat) : result?.anchor_lat != null ? String(result.anchor_lat) : '';
    editAnchorLon = override ? String(override.lon) : result?.anchor_lon != null ? String(result.anchor_lon) : '';
    recomputeError = '';
    renderMap();
  }

  // Inverse of callsToEdit -- what actually goes over the wire to
  // /recompute, in the shape walk_traverse expects: a curve row becomes
  // {call_type: "curve", radius, arc_length, delta, turn,
  // start_tangent_bearing?}, a line row stays {bearing, distance}.
  // start_tangent_bearing is optional -- omitted entirely (not sent as
  // an empty string) when blank, so the backend's default
  // tangent-chaining behavior applies unchanged, same as before this
  // field existed. Empty rows of either type are dropped.
  function callsToWire(calls: typeof editCalls) {
    return calls
      .filter((c: any) =>
        c.type === 'curve' ? c.radius?.trim() && c.delta?.trim() : c.bearing?.trim() || c.distance?.trim()
      )
      .map((c: any) =>
        c.type === 'curve'
          ? {
              call_type: 'curve',
              radius: c.radius,
              arc_length: c.arc_length,
              delta: c.delta,
              turn: c.turn,
              ...(c.start_tangent_bearing?.trim() ? { start_tangent_bearing: c.start_tangent_bearing } : {})
            }
          : { bearing: c.bearing, distance: c.distance }
      );
  }

  function addCallRow() {
    editCalls = [...editCalls, { type: 'line', bearing: '', distance: '' }];
  }

  function addCurveRow() {
    editCalls = [
      ...editCalls,
      { type: 'curve', radius: '', arc_length: '', delta: '', turn: 'R', start_tangent_bearing: '' }
    ];
  }

  function removeCallRow(idx: number) {
    editCalls = editCalls.filter((_, i) => i !== idx);
  }

  let pasteText = '';
  let pasteError = '';
  const _BEARING_RE = /([NS])\s*(\d{1,3})\s*[°oO*]\s*(\d{1,2})\s*['’′]\s*(\d{1,2}(?:\.\d+)?)\s*["”″]\s*([EW])/i;
  const _DISTANCE_RE = /(\d[\d,]*\.?\d*)\s*'?/;
  const _LATLON_RE = /-?\d{1,3}\.\d+/;
  const _CURVE_FIELD_RE = /^(arc|delta|radius|direction|turn|start tangent|start_tangent_bearing|tangent)\s*[:=]?\s*(.+)$/i;

  // Handles both plain bearing/distance lines AND the block format an
  // external tool (ChatGPT, etc.) tends to produce for curved surveys:
  // an "ANCHOR" block (two decimal lines = lat/lon) and "CURVE" blocks
  // (Arc/Delta/Radius/Direction/Start tangent, one per line, in any
  // order -- Start tangent is optional, same as everywhere else it
  // appears). Neither block is required -- plain line-call text still
  // works exactly as before.
  function parsePastedCalls() {
    pasteError = '';
    const lines = pasteText.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    const parsed: EditCall[] = [];
    let anchorLat: string | null = null;
    let anchorLon: string | null = null;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];

      if (/^anchor$/i.test(line)) {
        const a = lines[i + 1]?.match(_LATLON_RE);
        const b = lines[i + 2]?.match(_LATLON_RE);
        if (a && b) {
          anchorLat = a[0];
          anchorLon = b[0];
          i += 2;
        }
        continue;
      }

      if (/^curve$/i.test(line)) {
        const curve: any = { type: 'curve', radius: '', arc_length: '', delta: '', turn: 'R', start_tangent_bearing: '' };
        let j = i + 1;
        while (j < lines.length) {
          const fieldMatch = lines[j].match(_CURVE_FIELD_RE);
          if (!fieldMatch) break;
          const [, field, value] = fieldMatch;
          const key = field.toLowerCase();
          if (key === 'arc') curve.arc_length = value.trim();
          else if (key === 'delta') curve.delta = value.trim();
          else if (key === 'radius') curve.radius = value.trim();
          else if (key === 'direction' || key === 'turn') curve.turn = /^l/i.test(value.trim()) ? 'L' : 'R';
          else if (key === 'start tangent' || key === 'start_tangent_bearing' || key === 'tangent') {
            curve.start_tangent_bearing = value.trim();
          }
          j++;
        }
        if (curve.radius && curve.delta) parsed.push(curve);
        i = j - 1;
        continue;
      }

      const bearingMatch = line.match(_BEARING_RE);
      if (!bearingMatch) continue;
      const bearing = `${bearingMatch[1].toUpperCase()} ${bearingMatch[2]}°${bearingMatch[3]}'${bearingMatch[4]}"${bearingMatch[5].toUpperCase()}`;
      const rest = line.slice(bearingMatch.index! + bearingMatch[0].length);
      const distMatch = rest.match(_DISTANCE_RE);
      if (!distMatch) continue;
      const distance = `${distMatch[1].replace(/,/g, '')}'`;
      parsed.push({ type: 'line', bearing, distance });
    }

    if (parsed.length === 0) {
      pasteError = "Couldn't find any calls in that text -- expected lines like N 22°04'05\" W 75.09', or ANCHOR/CURVE blocks";
      return;
    }
    editCalls = parsed;
    if (anchorLat && anchorLon) {
      editAnchorLat = anchorLat;
      editAnchorLon = anchorLon;
    }
    pasteText = '';
  }

  // A manually confirmed polygon's geometry source of truth is its
  // confirmed pixel vertices (see calibration.py / _derive_confirmed_geometry),
  // NOT resolved_boundary_calls -- the call table this editor shows for
  // such a parcel is disconnected evidence, not its shape. Routing an
  // anchor-only change through the calls-based /recompute silently
  // discarded any uncorroborated vertex (a 4-vertex confirmed rectangle
  // with 3 verified edges became a 3-call triangle the moment the anchor
  // was touched). /update-anchor re-georeferences the EXISTING confirmed
  // polygon instead of rebuilding it -- vertex count/topology/calibration
  // are untouched, only the map position changes.
  async function submitAnchorUpdate(entry: any, keepOpen: boolean = false) {
    if (!documentId) return;
    const lat = Number(editAnchorLat);
    const lon = Number(editAnchorLon);
    if (!editAnchorLat.trim() || !editAnchorLon.trim() || Number.isNaN(lat) || Number.isNaN(lon)) {
      recomputeError = 'Enter a valid anchor latitude and longitude first.';
      return;
    }
    recomputing = true;
    recomputeError = '';
    try {
      const res = await fetch(`${API_BASE}/documents/${documentId}/update-anchor`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          page_number: entry.page,
          region_index: entry.regionIndex,
          parcel_index: entry.parcelIndex,
          anchor_lat: lat,
          anchor_lon: lon
        })
      });
      if (!res.ok) {
        const body = await res.text();
        throw new Error(`${res.status}: ${body.slice(0, 200)}`);
      }
      const data = await res.json();
      Object.assign(entry.parcel, data.parcel);
      result = result;
      if (keepOpen) {
        if (entry.parcel.anchor_override) {
          editAnchorLat = String(entry.parcel.anchor_override.lat);
          editAnchorLon = String(entry.parcel.anchor_override.lon);
        }
      } else {
        editingKey = null;
      }
      queueMicrotask(renderMap);
    } catch (err: any) {
      recomputeError = `Updating position failed: ${err.message}`;
    } finally {
      recomputing = false;
    }
  }

  async function submitRecompute(entry: any, autoFix: boolean = false, keepOpen: boolean = false) {
    if (!documentId) return;
    // "Edit calls -> Recompute/Auto-fix" intentionally rebuilds geometry
    // from the edited call table -- the correct, unchanged path for a
    // parcel whose calls ARE its geometry source of truth. "Change
    // anchor -> Recompute" on an already manually confirmed polygon
    // means reposition the existing shape, not rebuild it -- see
    // submitAnchorUpdate's docstring. Auto-fix never redirects: it's
    // explicitly a calls operation.
    if (!autoFix && entry.parcel.human_confirmed) {
      return submitAnchorUpdate(entry, keepOpen);
    }
    recomputing = true;
    recomputeError = '';
    try {
      const res = await fetch(`${API_BASE}/documents/${documentId}/recompute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          page_number: entry.page,
          region_index: entry.regionIndex,
          parcel_index: entry.parcelIndex,
          boundary_calls: callsToWire(editCalls),
          auto_fix: autoFix,
          ...(editAnchorLat.trim() && editAnchorLon.trim() && !Number.isNaN(Number(editAnchorLat)) && !Number.isNaN(Number(editAnchorLon))
            ? { anchor_lat: Number(editAnchorLat), anchor_lon: Number(editAnchorLon) }
            : {})
        })
      });
      if (!res.ok) {
        const body = await res.text();
        throw new Error(`${res.status}: ${body.slice(0, 200)}`);
      }
      const data = await res.json();
      // Merge the recomputed fields back into the in-memory result so
      // the map/card update without a full reload.
      Object.assign(entry.parcel, data.parcel);
      result = result; // re-trigger reactivity
      if (autoFix || keepOpen) {
        // Keep the editor open and show what actually changed -- closing
        // immediately would hide the before/after (an auto-fix reversal,
        // or the two calls a dragged vertex just recomputed) the
        // reviewer needs to see to trust the result.
        editCalls = callsToEdit(entry.parcel);
        if (entry.parcel.anchor_override) {
          editAnchorLat = String(entry.parcel.anchor_override.lat);
          editAnchorLon = String(entry.parcel.anchor_override.lon);
        }
      } else {
        editingKey = null;
      }
      queueMicrotask(renderMap);
    } catch (err: any) {
      recomputeError = `Recompute failed: ${err.message}`;
    } finally {
      recomputing = false;
    }
  }

  async function loadSample() {
    errorMessage = '';
    const res = await fetch('/sample-data/sample-result.json');
    result = await res.json();
    documentId = result.document_id ?? null; // sample is not a real backend document -- not remembered for resume
    usingSample = true;
    phase = 'done';
    queueMicrotask(renderMap);
  }

  async function uploadFile(file: File) {
    if (file.type !== 'application/pdf') {
      errorMessage = 'Only PDF documents are supported.';
      phase = 'error';
      return;
    }

    phase = 'uploading';
    errorMessage = '';
    usingSample = false;
    progressState = null;

    const formData = new FormData();
    formData.append('file', file);

    try {
      const res = await fetch(`${API_BASE}/documents/upload`, {
        method: 'POST',
        body: formData
      });
      if (!res.ok) {
        const body = await res.text();
        throw new Error(`${res.status}: ${body.slice(0, 200)}`);
      }
      const data = await res.json();
      rememberDocumentId(data.document_id ?? null);
      processingDocId = data.document_id ?? null;
      processingStartedAt = Date.now();
      autoOpenReview = true;
      phase = 'processing';
      startPolling();
    } catch (err: any) {
      errorMessage =
        err?.message?.includes('Failed to fetch') || err?.name === 'TypeError'
          ? `Could not reach the ROAM backend at ${API_BASE}. It may be offline right now. Try the sample result below instead.`
          : `Upload failed: ${err.message}`;
      phase = 'error';
    }
  }

  // Set only for a document uploaded in THIS session: it goes straight to boundary
  // confirmation once ROAM has named the parcel maps. A resumed document that is
  // still processing just waits for the finished result.
  let autoOpenReview = false;

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(pollProgress, 1200);
    pollProgress();
  }

  async function pollProgress() {
    if (!processingDocId) return;
    try {
      const res = await fetch(`${API_BASE}/documents/${processingDocId}/progress`);
      if (!res.ok) return; // transient -- try again on the next tick
      const state: ProgressState = await res.json();
      progressState = state;

      if (state.error) {
        stopPolling();
        errorMessage = `Processing failed: ${state.error}`;
        phase = 'error';
        return;
      }

      // The boundary the user confirms is an early INPUT to the pipeline: open the
      // review as soon as ROAM has named the candidate parcel maps. OCR, anchoring
      // and parcel extraction keep running in the background meanwhile.
      const ready = state.history?.find((h) => h.stage === 'candidates_ready');
      if (autoOpenReview && ready && !(ready.detail ?? '').startsWith('no ')) {
        stopPolling();
        autoOpenReview = false;
        await goto(`/boundary-review?doc=${encodeURIComponent(processingDocId)}`);
        return;
      }

      if (state.stage === 'done') {
        stopPolling();
        await loadFinishedDocument();
      }
    } catch {
      // network blip -- keep polling, don't surface every dropped tick
    }
  }

  async function loadFinishedDocument() {
    if (!processingDocId) return;
    try {
      const res = await fetch(`${API_BASE}/documents/${processingDocId}`);
      if (!res.ok) {
        const body = await res.text();
        throw new Error(`${res.status}: ${body.slice(0, 200)}`);
      }
      const data = await res.json();
      result = data.result;
      phase = 'done';
      queueMicrotask(renderMap);
    } catch (err: any) {
      errorMessage = `Finished processing, but couldn't load the result: ${err.message}`;
      phase = 'error';
    }
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    dragOver = false;
    const file = e.dataTransfer?.files?.[0];
    if (file) uploadFile(file);
  }

  function onFileChange(e: Event) {
    const file = (e.target as HTMLInputElement).files?.[0];
    if (file) uploadFile(file);
  }

  function reset() {
    stopPolling();
    phase = 'idle';
    result = null;
    documentId = null;
    errorMessage = '';
    selectedKey = null;
    editingKey = null;
    showAllCategories = false;
    progressState = null;
    processingDocId = null;
    processingStartedAt = null;
  }

  onDestroy(() => {
    stopPolling();
    if (pendingTimer) clearInterval(pendingTimer);
    map?.remove();
  });
</script>

<svelte:window on:keydown={onKeydown} />

<svelte:head>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
</svelte:head>

<div class="workspace">
  <header class="ws-header">
    <div>
      <p class="kicker">Workspace</p>
      <h1>Document intelligence</h1>
    </div>
    {#if phase === 'done'}
      <button class="btn btn-ghost" on:click={reset}>New document</button>
    {/if}
  </header>

  {#if phase === 'idle' || phase === 'error'}
    <section class="gallery fade-up" aria-label="Sample documents">
      <div class="gallery-head">
        <h2>Try a sample document</h2>
        <p>Already processed. Open one to explore the map, the parcels, the report and the AI review. You get your own copy: change anything, nothing is saved.</p>
      </div>
      {#if samplesError}<p class="gallery-err">{samplesError}</p>{/if}
      <div class="gallery-grid">
        {#each samples as sm (sm.id)}
          <button class="sample-card panel" disabled={openingSample !== null} on:click={() => openSample(sm)}>
            <div class="sample-thumb"><img src={`${API_BASE}${sm.thumbnail}`} alt={`Plat: ${sm.title}`}/></div>
            <div class="sample-body">
              <span class="sample-title">{sm.title}</span>
              <span class="sample-loc">{sm.location} · {sm.pages} pages · {sm.parcels} parcels</span>
              <span class="sample-hl">{sm.highlight}</span>
            </div>
            {#if openingSample === sm.id}<span class="sample-opening">Opening…</span>{/if}
          </button>
        {/each}
      </div>
    </section>
  {/if}

  {#if (phase === 'idle' || phase === 'uploading' || phase === 'error') && !$user}
    <section class="signin-card panel" aria-label="Upload your own document">
      <div>
        <h2>Process your own document</h2>
        <p>Sign in with Google to upload a scanned deed, plat or survey PDF. Your documents are private to your account.</p>
      </div>
      <AccountButton text="signin_with" />
    </section>
  {/if}

  {#if (phase === 'idle' || phase === 'uploading' || phase === 'error') && $user}
    <section
      class="dropzone panel"
      class:drag={dragOver}
      aria-label="Upload a document"
      on:dragover|preventDefault={() => (dragOver = true)}
      on:dragleave={() => (dragOver = false)}
      on:drop={onDrop}
    >
      {#if phase === 'uploading'}
        <div class="dz-state">
          <div class="spinner"></div>
          <p>Uploading file…</p>
        </div>
      {:else}
        <div class="dz-state">
          <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4">
            <path d="M12 3v12m0 0-4-4m4 4 4-4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
          </svg>
          <p class="dz-title">Drop a scanned deed, plat or survey PDF</p>
          <p class="dz-sub">or</p>
          <button class="btn btn-primary" on:click={() => fileInput.click()}>Choose a file</button>
          <input
            bind:this={fileInput}
            type="file"
            accept="application/pdf"
            class="hidden-input"
            on:change={onFileChange}
          />
        </div>
      {/if}

      {#if phase === 'error'}
        <div class="dz-error">
          <p>{errorMessage}</p>
        </div>
      {/if}

      <div class="dz-footer">
        <span>API target: <code class="mono">{API_BASE}</code></span>
        {#if lastDocumentId}
          <button class="link-btn" on:click={() => resumeLastDocument()} disabled={resuming}>
            {resuming ? 'Resuming…' : 'Resume last document →'}
          </button>
        {/if}
        {#if samplesError}<button class="link-btn" on:click={loadSample}>View an offline sample result →</button>{/if}
      </div>
    </section>
    {#if myDocuments.length}
      <section class="my-docs panel" aria-label="Your uploads">
        <h2>Your uploads</h2>
        <ul>
          {#each myDocuments as d (d.id)}
            <li>
              <button class="link-btn" on:click={() => openDocument(d.id)}>{d.filename ?? d.id.slice(0, 8)}</button>
              {#if d.created_at}<span class="muted">{new Date(d.created_at * 1000).toLocaleDateString()}</span>{/if}
            </li>
          {/each}
        </ul>
      </section>
    {/if}
  {/if}

  {#if phase === 'processing'}
    <section class="agent-trace panel fade-up">
      <div class="agent-trace-header">
        <span class="agent-pulse"></span>
        <span class="agent-trace-title">Running extraction pipeline</span>
      </div>
      <ol class="trace-steps">
        {#each STAGE_ORDER as stage, idx}
          {@const isDone = progressState?.stage === 'done' || traceCurrentIdx > idx}
          {@const isActive = traceCurrentIdx === idx && progressState?.stage !== 'done'}
          <li class="trace-step" class:done={isDone} class:active={isActive}>
            <span class="trace-dot">
              {#if isDone}
                <svg width="10" height="10" viewBox="0 0 16 16" fill="none"><path d="M3 8.5l3 3 7-7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
              {:else if isActive}
                <span class="trace-dot-spin"></span>
              {/if}
            </span>
            <span class="trace-label">
              {STAGE_LABELS[stage]}
              <span class="trace-detail mono">{traceDetailByStage[stage] ?? '-'}</span>
            </span>
          </li>
        {/each}
      </ol>
    </section>
  {/if}

  {#if phase === 'done' && result}
    <section class="results fade-up">
      {#if sandboxOf}
        <div class="playground-banner panel">
          <span class="pg-dot"></span>
          <span><strong>Playground copy</strong>{#if sandboxSample}&nbsp;of “{sandboxSample.title}”{/if}: move parcels, re-confirm boundaries, ask the AI. Nothing is saved: your copy is discarded when you leave.</span>
          <span class="pg-actions">
            <button class="btn btn-ghost btn-sm" on:click={resetPlayground}>Reset sample</button>
            <button class="btn btn-ghost btn-sm" on:click={reset}>All samples</button>
          </span>
        </div>
      {/if}
      {#if usingSample}
        <div class="sample-banner panel">
          <span class="status-dot"></span>
          Showing a sample result: {result.source_note}
        </div>
      {/if}

      <div class="results-summary">
        <div class="stat panel">
          <span class="stat-label">Pages</span>
          <span class="stat-value mono">{result.total_pages ?? result.pages?.length ?? '-'}</span>
        </div>
        <div class="stat panel">
          <span class="stat-label">Need review</span>
          <span class="stat-value mono">{result.pages_needing_review ?? '-'}</span>
        </div>
        <div class="stat panel">
          <span class="stat-label">Parcels</span>
          <span class="stat-value mono">{parcelRegions.length}</span>
        </div>
      </div>

      <div class="location-banner panel" class:ok={anchorPrecision === 'surveyed' || aliquotFit}>
        <strong>Location:</strong> {LOCATION_LABELS[anchorPrecision]}
        {#if anchorSource}<span class="location-source">· {anchorSource}</span>{/if}
        {#if aliquotFit}
          <span class="location-source">
            Confirmed parcels fitted onto the {aliquotFit.description} from BLM survey data: combined area
            {aliquotFit.confirmed_acres} ac vs {aliquotFit.aliquot_acres} ac ({aliquotFit.area_error_pct}% off),
            outer dimensions within {aliquotFit.extent_error_pct}%.
          </span>
        {:else if anchorPrecision !== 'surveyed'}
          <span class="location-source">Shapes are exact, but where they sit on the map is approximate, so no parcel on this document can be marked Verified.</span>
        {/if}
      </div>

      {#if boundaryCandidates.length > 0 && documentId}
        <div class="location-banner panel" class:ok={boundaryConfirmedCount === boundaryCandidates.length}>
          <strong>Boundary confirmation:</strong>
          {boundaryConfirmedCount} of {boundaryCandidates.length} candidate parcel{boundaryCandidates.length === 1 ? '' : 's'} confirmed
          <span class="location-source">· The target parcel's outline is what ROAM places on the map. Confirm it on the drawing to calibrate and place it.</span>
          <a class="btn btn-ghost btn-sm" href={boundaryReviewHref()}>Confirm boundaries →</a>
          <a class="btn btn-ghost btn-sm" href={`/report?doc=${encodeURIComponent(documentId ?? '')}`}>Report &amp; export →</a>
        </div>
      {/if}

      <div class="results-grid" class:with-viewer={viewer}>
        {#if viewer}
          <div class="viewer-panel panel">
            <div class="viewer-toolbar">
              <span class="viewer-title">Page {viewer.page} · source drawing</span>
              <div class="viewer-actions">
                <button class="btn btn-ghost btn-sm" class:active={viewerMode === 'region'} on:click={() => { viewerMode = 'region'; resetView(); }}>Region</button>
                <button class="btn btn-ghost btn-sm" class:active={viewerMode === 'page'} on:click={() => { viewerMode = 'page'; resetView(); }}>Full page</button>
                <button class="btn btn-ghost btn-sm" on:click={() => zoomBy(1.4)} aria-label="Zoom in">+</button>
                <button class="btn btn-ghost btn-sm" on:click={() => zoomBy(1 / 1.4)} aria-label="Zoom out">−</button>
                <button class="btn btn-ghost btn-sm" on:click={() => (rotation = (rotation + 90) % 360)} aria-label="Rotate">⟳ 90°</button>
                <button class="btn btn-ghost btn-sm" on:click={resetView}>Fit</button>
                <a class="btn btn-ghost btn-sm" href={viewerSrc} target="_blank" rel="noopener">Open ↗</a>
                <button class="btn btn-ghost btn-sm" on:click={closeViewer} aria-label="Close viewer">✕</button>
              </div>
            </div>
            <div
              class="viewer-stage"
              class:dragging
              on:wheel={onViewerWheel}
              on:pointerdown={onViewerDown}
              on:pointermove={onViewerMove}
              on:pointerup={onViewerUp}
              on:pointercancel={onViewerUp}
              role="img"
              aria-label="Source drawing, scroll to zoom, drag to pan"
            >
              <img
                src={viewerSrc}
                alt="Source drawing"
                draggable="false"
                style="transform: translate({panX}px, {panY}px) scale({zoom}) rotate({rotation}deg);"
              />
            </div>
            <p class="viewer-hint">Scroll to zoom · drag to pan · {Math.round(zoom * 100)}%</p>
          </div>
        {/if}

        <div class="map-panel panel">
          <div bind:this={mapEl} class="map-container"></div>
          <button
            class="recenter-btn"
            on:click={recenter}
            disabled={!lastFitBounds}
            title={lastFitBounds
              ? 'Recenter on parcels'
              : 'No georeferenced parcels to center on -- this document had no geocodable address'}
            aria-label="Recenter map on parcels"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="12" cy="12" r="3" />
              <path d="M12 2v3M12 19v3M2 12h3M19 12h3" />
            </svg>
          </button>
        </div>

        <div class="region-list">
          {#if parcelRegions.length === 0 && allParcelRegions.length === 0}
            <div class="panel empty-state">
              <p>No georeferenced parcel geometry in this result yet. Vision extraction may not have found boundary calls on this document's ParcelMap regions.</p>
            </div>
          {/if}

          {#if documentId && !usingSample}
            {#if agentOpen}
              {#key `${documentId}`}
                <AgentChat
                  apiBase={API_BASE}
                  {documentId}
                  focus={agentFocus}
                  previewId={agentPreview?.id ?? null}
                  on:preview={(e) => setAgentPreview(e.detail)}
                  on:applied={onAgentApplied}
                  on:close={closeAgent}
                />
              {/key}
            {:else}
              <button class="ask-ai panel" on:click={() => openAgent(null)}>
                <span class="ask-ai-icon" aria-hidden="true">✦</span>
                <span><strong>Ask AI</strong>: something placed wrong? Describe it and the AI checks the evidence</span>
              </button>
            {/if}
          {/if}

          <div class="palette panel move-panel">
            <label class="move-toggle">
              <input type="checkbox" bind:checked={moveMode} />
              <span><strong>Move parcels</strong>: drag an outline on the map to drop it where it belongs</span>
            </label>
            {#if moveMode}
              <label class="move-toggle sub">
                <input type="checkbox" bind:checked={moveWholeSheet} />
                <span>Move all of this sheet's parcels together</span>
              </label>
              <p class="move-hint">Shape and size stay as they are. Arrow keys nudge the selected parcel 1 m (Shift: 5 m).</p>
            {/if}
            {#if moveError}<p class="move-hint err">{moveError}</p>{/if}
          </div>
          <div class="palette panel">
            <span class="palette-title">Outline colour · {paletteScope === null || Number.isNaN(paletteScope) ? 'all pages' : `page ${paletteScope}`}</span>
            <div class="palette-row">
              <button class="swatch auto" class:active={!(outlineColors[String(paletteScope)] ?? outlineColors['*'])} title="Automatic: green verified, orange approximate, red unconfirmed" on:click={() => setOutlineColor(null)}>Auto</button>
              {#each PALETTE as c}
                <button
                  class="swatch"
                  class:active={(outlineColors[String(paletteScope)] ?? outlineColors['*']) === c}
                  style={`background:${c}`}
                  title={c}
                  aria-label={`Outline colour ${c}`}
                  on:click={() => setOutlineColor(c)}
                ></button>
              {/each}
            </div>
          </div>
          {#if hiddenCount > 0}
            <button class="category-banner panel" on:click={() => (showAllCategories = true)}>
              {hiddenCount} region{hiddenCount === 1 ? '' : 's'} likely not a boundary map (aerial, vicinity map, certificate) hidden. Click to show
            </button>
          {:else if showAllCategories && allParcelRegions.length}
            <button class="category-banner panel" on:click={() => (showAllCategories = false)}>
              Showing all regions. Click to hide likely non-plat content again
            </button>
          {/if}

          {#each geometryRegions as entry (regionKey(entry.page, entry.i))}
            {@render regionCard(entry)}
          {/each}

          {#if noGeometryRegions.length > 0}
            <button class="category-banner panel" on:click={() => (showNoGeometry = !showNoGeometry)}>
              {showNoGeometry ? 'Hide' : 'Show'} {noGeometryRegions.length} region{noGeometryRegions.length === 1 ? '' : 's'} with no boundary geometry
            </button>
          {/if}

          {#if showNoGeometry}
            {#each noGeometryRegions as entry (regionKey(entry.page, entry.i))}
              {@render regionCard(entry)}
            {/each}
          {/if}

          {#snippet regionCard(entry)}
            {@const key = regionKey(entry.page, entry.i)}
            {@const parcel = entry.parcel}
            {@const v = parcel.spatial_validation}
            <div class="region-card panel" class:selected={selectedKey === key}>
              <button class="region-card-head-btn" on:click={() => selectRegion(key)}>
                <div class="region-head">
                  <span class="region-title">
                    {parcel.vision_geometry?.parcel_label || `Page ${entry.page} parcel`}
                    {#if parcel.human_edited}<span class="edited-badge">edited</span>{/if}
                  </span>
                  {#if v || parcel.extraction_note || parcel.georeference_error || verdict(parcel, entry.page).busy}
                    {@const verdictInfo = verdict(parcel, entry.page)}
                    <span class="pill {verdictInfo.cls}">{#if verdictInfo.busy}<span class="spinner" aria-hidden="true"></span>{/if}{verdictInfo.label}</span>
                  {/if}
                </div>
                {#if verdict(parcel, entry.page).busy}
                  <p class="busy-note">{verdict(parcel, entry.page).label === 'Refining placement…' ? 'Reading the survey coordinates printed on the sheet to fix its exact position. This card updates by itself.' : 'Checking scale and orientation against the drawing, then placing it on the map. This card updates by itself.'}</p>
                {/if}

                {#if v}
                  {@const locationPrecision = parcelLocationPrecision(parcel)}
                  <div class="checks">
                    <span class="check" class:ok={v.valid}>
                      {v.valid ? '✓' : '✗'} Shape
                    </span>
                    <span class="check" class:ok={locationConfirmed(locationPrecision)}>
                      {locationConfirmed(locationPrecision) ? '✓' : '✗'} Location · {LOCATION_LABELS[locationPrecision]}
                    </span>
                  </div>
                {/if}

                {#if v}
                  <dl class="region-metrics">
                    <div><dt>Precision</dt><dd class="mono">{v.precision_ratio ? `1:${v.precision_ratio}` : '-'}</dd></div>
                    <div><dt>Closure</dt><dd class="mono">{parcel.boundary_geojson_wgs84?.properties?.closure_error_ft ?? '-'} ft</dd></div>
                    <div><dt>Area</dt><dd class="mono">{v.area_acres ? `${v.area_acres} ac` : '-'}</dd></div>
                  </dl>

                  {#if selectedKey === key && v.issues?.length}
                    <ul class="region-issues">
                      {#each v.issues as issue}
                        <li>{issue}</li>
                      {/each}
                    </ul>
                  {/if}
                  {#if selectedKey === key && parcel.assembly_notes?.length}
                    <ul class="region-notes">
                      {#each parcel.assembly_notes as note}
                        <li>{note}</li>
                      {/each}
                    </ul>
                  {/if}
                {:else if parcel.extraction_note || parcel.georeference_error}
                  <p class="region-note">
                    {parcel.extraction_note || parcel.georeference_error}
                  </p>
                {/if}
              </button>

              {#if selectedKey === key && parcel.calibration}
                {@const cal = parcel.calibration}
                <details class="verify-details">
                  <summary>Verification details</summary>
                  <p>
                    Scale/rotation: <strong>{cal.status === 'cross_validated' ? 'cross-validated' : cal.status === 'single_source' ? 'single source' : cal.status === 'pending' ? 'verifying…' : 'not verified'}</strong>
                    {#if cal.scale_ft_per_px} · {cal.scale_ft_per_px.toFixed(4)} ft/px{/if}
                    {#if cal.rotation_deg !== null && cal.rotation_deg !== undefined} · rotation {cal.rotation_deg.toFixed(1)}°{/if}
                    · {cal.corroborating_edge_count} corroborating edge(s)
                  </p>
                  {#if parcel.placement}
                    <p>Position: <strong>{parcel.placement.status === 'surveyed_corner' ? 'tied to a printed corner coordinate' : 'approximate (document anchor)'}</strong></p>
                  {/if}
                  {#if cal.notes?.length || parcel.placement?.notes?.length}
                    <ul>
                      {#each [...(cal.notes ?? []), ...(parcel.placement?.notes ?? [])] as note}<li>{note}</li>{/each}
                    </ul>
                  {/if}
                </details>
              {/if}

              {#if selectedKey === key && documentId}
                <div class="review-tools">
                  <div class="review-tools-row">
                    <button class="btn btn-ghost btn-sm" on:click={() => startEdit(entry)}>
                      {editingKey === key ? 'Cancel edit' : 'Edit calls'}
                    </button>
                    <button class="btn btn-ghost btn-sm" on:click={() => openViewer(entry)}>
                      Open source drawing
                    </button>
                    {#if parcel.manual_position}
                      <button class="btn btn-ghost btn-sm" title="Put it back where the app computed it" on:click={() => resetPosition(entry)}>
                        Reset position
                      </button>
                    {/if}
                    {#if parcel.human_confirmed && parcel.boundary_geojson_wgs84 && !usingSample}
                      <button class="btn btn-ghost btn-sm ask-ai-btn" on:click={() => openAgent(entry)}>✦ Ask AI</button>
                    {/if}
                    <a class="btn btn-ghost btn-sm" href={boundaryReviewHref(parcel.roster_id ? `${entry.page}-${parcel.roster_id}` : `${entry.page}-${entry.i}`)}>
                      {parcel.human_confirmed ? 'Re-confirm boundary' : 'Confirm boundary'}
                    </a>
                  </div>

                  {#if editingKey === key}
                    <div class="call-editor">
                      <div class="anchor-box">
                        <span class="anchor-label">Anchor (pivot point)</span>
                        <input class="mono" type="text" placeholder="Lat, e.g. 42.8752" bind:value={editAnchorLat} />
                        <input class="mono" type="text" placeholder="Lon, e.g. -71.2303" bind:value={editAnchorLon} />
                        {#if parcel.anchor_override}
                          <span class="anchor-pinned">📍 manually pinned</span>
                        {/if}
                      </div>
                      <div class="paste-box">
                        <textarea
                          class="mono"
                          rows="3"
                          placeholder={"Paste all calls at once -- lines, or CURVE blocks (Radius/Delta/Arc/Start tangent/Direction), or an ANCHOR block (lat then lon), e.g.\nN 22°04'05\" W 75.09'\nCURVE\nRadius: 435.00'\nDelta: 15°13'50\"\nArc: 115.63'\nStart tangent: N 47°31'07\" W\nDirection: R"}
                          bind:value={pasteText}
                        ></textarea>
                        <button class="btn btn-ghost btn-sm" on:click={parsePastedCalls}>Fill table from paste</button>
                        {#if pasteError}<div class="paste-error">{pasteError}</div>{/if}
                      </div>
                      <table class="call-table">
                        <thead>
                          <tr><th colspan="4">Call</th><th></th></tr>
                        </thead>
                        <tbody>
                          {#each editCalls as call, idx}
                            {#if call.type === 'curve'}
                              <tr class="curve-row">
                                <td colspan="4">
                                  <div class="curve-fields">
                                    <span class="curve-tag">curve</span>
                                    <label>
                                      <span>Radius</span>
                                      <input class="mono" bind:value={call.radius} placeholder="435.00'" />
                                    </label>
                                    <label>
                                      <span>Delta</span>
                                      <input class="mono" bind:value={call.delta} placeholder="15°13'50&quot;" />
                                    </label>
                                    <label>
                                      <span>Arc (optional)</span>
                                      <input class="mono" bind:value={call.arc_length} placeholder="115.63'" />
                                    </label>
                                    <label>
                                      <span>Turn</span>
                                      <select class="mono" bind:value={call.turn} title="Which side the curve bows toward">
                                        <option value="R">R (clockwise)</option>
                                        <option value="L">L (counter-clockwise)</option>
                                      </select>
                                    </label>
                                    <label class="curve-tangent">
                                      <span>Start tangent (optional)</span>
                                      <input
                                        class="mono"
                                        bind:value={call.start_tangent_bearing}
                                        placeholder="N 47°31'07&quot; W -- overrides inherited tangent"
                                        title="Overrides the tangent this curve inherits from the previous call. Leave blank to keep default (tangent-chained) behavior."
                                      />
                                    </label>
                                  </div>
                                </td>
                                <td><button class="row-remove" on:click={() => removeCallRow(idx)} aria-label="Remove call">×</button></td>
                              </tr>
                            {:else}
                              <tr>
                                <td colspan="2"><input class="mono" bind:value={call.bearing} placeholder="N 45°00'00&quot; W" /></td>
                                <td colspan="2"><input class="mono" bind:value={call.distance} placeholder="100.00'" /></td>
                                <td><button class="row-remove" on:click={() => removeCallRow(idx)} aria-label="Remove call">×</button></td>
                              </tr>
                            {/if}
                          {/each}
                        </tbody>
                      </table>
                      <div class="review-tools-row">
                        <button class="btn btn-ghost btn-sm" on:click={addCallRow}>+ Add call</button>
                        <button class="btn btn-ghost btn-sm" on:click={addCurveRow}>+ Add curve</button>
                        <button
                          class="btn btn-ghost btn-sm"
                          disabled={recomputing}
                          title="Tries reversing/reordering/dropping calls to close the shape -- deterministic, checked against the stated acreage, never guesses"
                          on:click={() => submitRecompute(entry, true)}
                        >
                          {recomputing ? 'Trying…' : '✨ Auto-fix'}
                        </button>
                        <button
                          class="btn btn-primary btn-sm"
                          disabled={recomputing}
                          on:click={() => submitRecompute(entry)}
                        >
                          {recomputing ? 'Recomputing…' : 'Recompute'}
                        </button>
                      </div>
                      {#if recomputeError}
                        <p class="review-error">{recomputeError}</p>
                      {/if}
                    </div>
                  {/if}
                </div>
              {/if}
            </div>
          {/snippet}
        </div>
      </div>
    </section>
  {/if}
</div>

<style>
.workspace {
  display: flex;
  flex-direction: column;
  gap: 22px;
}

.ws-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
}

.kicker {
  margin: 0 0 4px;
  text-transform: uppercase;
  font-size: 0.7rem;
  letter-spacing: 0.12em;
  font-weight: 700;
  color: var(--accent);
}

h1 {
  margin: 0;
  font-size: 1.5rem;
}

.dropzone {
  padding: 48px 32px;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 14px;
  border-style: dashed;
  border-width: 1.5px;
  transition: border-color 0.2s ease, background 0.2s ease;
}

.dropzone.drag {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.dz-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  color: var(--muted);
}

.dz-title {
  color: var(--text);
  font-weight: 600;
  margin: 4px 0 0;
}

.dz-sub {
  margin: 0;
  font-size: 0.8rem;
}

.hidden-input {
  display: none;
}

.dz-error {
  color: var(--danger);
  font-size: 0.88rem;
  max-width: 46ch;
}

.dz-footer {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 10px;
  font-size: 0.76rem;
  color: var(--muted-dim);
}

.link-btn {
  background: none;
  border: none;
  color: var(--accent);
  font-size: 0.82rem;
  cursor: pointer;
  padding: 0;
}

.spinner {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  border: 2.5px solid var(--line);
  border-top-color: var(--accent);
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.agent-trace {
  padding: 24px 26px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.agent-trace-header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.agent-trace-title {
  font-size: 0.92rem;
  font-weight: 600;
  letter-spacing: -0.01em;
}

.agent-pulse {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 0 rgba(52, 224, 161, 0.5);
  animation: pulse-ring 1.6s ease-out infinite;
  flex-shrink: 0;
}

@keyframes pulse-ring {
  0% { box-shadow: 0 0 0 0 rgba(52, 224, 161, 0.45); }
  70% { box-shadow: 0 0 0 8px rgba(52, 224, 161, 0); }
  100% { box-shadow: 0 0 0 0 rgba(52, 224, 161, 0); }
}

.trace-steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  position: relative;
}

.trace-step {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 9px 0;
  position: relative;
}

.trace-step::before {
  /* connecting vertical line between dots, like an agent trace/timeline */
  content: '';
  position: absolute;
  left: 8px;
  top: -2px;
  bottom: -2px;
  width: 1px;
  background: var(--line);
}

.trace-step:first-child::before {
  top: 50%;
}

.trace-step:last-child::before {
  bottom: 50%;
}

.trace-dot {
  width: 17px;
  height: 17px;
  border-radius: 50%;
  border: 1.5px solid var(--line);
  background: var(--surface);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  z-index: 1;
  color: var(--bg);
  transition: border-color 0.2s ease, background 0.2s ease;
}

.trace-step.done .trace-dot {
  border-color: var(--accent);
  background: var(--accent);
}

.trace-step.active .trace-dot {
  border-color: var(--accent);
}

.trace-dot-spin {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent);
  animation: spin-dot-pulse 1s ease-in-out infinite;
}

@keyframes spin-dot-pulse {
  0%, 100% { opacity: 0.35; transform: scale(0.8); }
  50% { opacity: 1; transform: scale(1); }
}

.trace-label {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 0.86rem;
  color: var(--muted-dim);
  padding-top: 1px;
}

.trace-step.active .trace-label,
.trace-step.done .trace-label {
  color: var(--text);
}

.trace-detail {
  font-size: 0.76rem;
  color: var(--muted);
}

.sample-banner {
  padding: 10px 16px;
  font-size: 0.82rem;
  color: var(--muted);
  display: flex;
  align-items: center;
  gap: 10px;
}

.results {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.results-summary {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 14px;
}

.stat {
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.stat-label {
  font-size: 0.76rem;
  color: var(--muted);
}

.stat-value {
  font-size: 1.4rem;
  font-weight: 700;
}

.results-grid {
  display: grid;
  grid-template-columns: 1fr 340px;
  gap: 18px;
  align-items: start;
}

.map-panel {
  padding: 6px;
  overflow: hidden;
  position: relative;
}

.map-container {
  height: 560px;
  border-radius: 12px;
  overflow: hidden;
}

.recenter-btn {
  position: absolute;
  top: 16px;
  right: 16px;
  z-index: 500;
  width: 34px;
  height: 34px;
  border-radius: 8px;
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--text);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.15);
}

.recenter-btn:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.recenter-btn:hover {
  background: var(--accent-soft);
}

.region-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.empty-state {
  padding: 20px;
  color: var(--muted);
  font-size: 0.86rem;
}

.region-card {
  text-align: left;
  padding: 0;
  background: var(--surface);
  transition: border-color 0.15s ease, background 0.15s ease;
}

.region-card.selected {
  border-color: rgba(52, 224, 161, 0.5);
  background: var(--accent-soft);
}

.region-card-head-btn {
  display: block;
  width: 100%;
  text-align: left;
  padding: 14px 16px;
  cursor: pointer;
  background: none;
  border: none;
  color: inherit;
  font: inherit;
}

.category-banner {
  padding: 10px 14px;
  font-size: 0.78rem;
  color: var(--muted);
  text-align: left;
  cursor: pointer;
  background: var(--surface);
  border: none;
  width: 100%;
}

.edited-badge {
  margin-left: 8px;
  font-size: 0.64rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--accent);
  border: 1px solid var(--accent);
  border-radius: 999px;
  padding: 1px 6px;
  vertical-align: middle;
}

.verify-details {
  margin: 0 16px 10px;
  font-size: 12px;
  color: var(--ink-2, #55534b);
}
.verify-details summary {
  cursor: pointer;
}
.verify-details p,
.verify-details ul {
  margin: 6px 0 0;
}
.verify-details ul {
  padding-left: 18px;
}
.review-tools {
  padding: 0 16px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  border-top: 1px solid var(--line);
  margin-top: 4px;
  padding-top: 12px;
}

.review-tools-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.btn-sm {
  padding: 5px 10px;
  font-size: 0.76rem;
}

.review-error {
  margin: 0;
  font-size: 0.76rem;
  color: var(--danger);
}




.call-editor {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.anchor-box {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  padding: 8px;
  border: 1px dashed var(--border, #ccc);
  border-radius: 6px;
}

.anchor-box input {
  width: 130px;
  font-size: 0.78rem;
  padding: 4px 6px;
}

.anchor-label {
  font-size: 0.72rem;
  color: var(--muted, #777);
  white-space: nowrap;
}

.anchor-pinned {
  font-size: 0.72rem;
  color: #2f7a4f;
}

.paste-box {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  border: 1px dashed var(--border, #ccc);
  border-radius: 6px;
}

.paste-box textarea {
  width: 100%;
  resize: vertical;
  font-size: 0.78rem;
  padding: 6px;
}

.paste-error {
  font-size: 0.75rem;
  color: #b3261e;
}




.call-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.8rem;
}

.call-table th {
  text-align: left;
  font-size: 0.66rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--muted-dim);
  padding: 0 6px 6px;
}

.call-table td {
  padding: 3px;
}

.call-table input {
  width: 100%;
  box-sizing: border-box;
  padding: 5px 7px;
  border-radius: 6px;
  border: 1px solid var(--line);
  background: var(--bg);
  color: var(--text);
  font-size: 0.78rem;
}

.curve-row {
  background: color-mix(in srgb, var(--accent, #c9762f) 8%, transparent);
}

.curve-fields {
  display: flex;
  flex-wrap: wrap;
  align-items: end;
  gap: 8px;
  padding: 4px 0;
}

.curve-tag {
  align-self: center;
  font-size: 0.66rem;
  text-transform: uppercase;
  color: var(--accent, #c9762f);
  white-space: nowrap;
  font-weight: 600;
}

.curve-fields label {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 0.66rem;
  color: var(--muted-dim);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.curve-tangent {
  flex-basis: 100%;
}

.curve-tangent input {
  width: 100% !important;
}

.curve-fields input {
  width: 110px;
  box-sizing: border-box;
  padding: 6px 8px;
  border-radius: 6px;
  border: 1px solid var(--line);
  background: var(--bg);
  color: var(--text);
  font-size: 0.8rem;
}

.curve-fields select {
  width: 150px;
  border-radius: 6px;
  border: 1px solid var(--line);
  background: var(--bg);
  color: var(--text);
  font-size: 0.78rem;
  padding: 6px;
}

.row-remove {
  background: none;
  border: none;
  color: var(--muted);
  cursor: pointer;
  font-size: 1rem;
  line-height: 1;
  padding: 4px;
}

.region-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.move-panel {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.move-toggle {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  font-size: 0.8rem;
  cursor: pointer;
}
.move-toggle.sub {
  padding-left: 18px;
  font-size: 0.74rem;
  color: var(--muted);
}
.move-hint {
  margin: 0;
  font-size: 0.72rem;
  color: var(--muted);
}
.move-hint.err {
  color: #c53b3b;
}
.palette {
  padding: 10px 12px;
  margin-bottom: 10px;
}
.palette-title {
  display: block;
  font-size: 0.72rem;
  color: var(--muted);
  margin-bottom: 6px;
}
.palette-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.swatch {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  border: 1px solid rgba(0, 0, 0, 0.35);
  cursor: pointer;
  padding: 0;
}
.swatch.auto {
  width: auto;
  border-radius: 11px;
  padding: 0 9px;
  font-size: 0.72rem;
  background: #fff;
}
.swatch.active {
  outline: 2px solid #1c7ed6;
  outline-offset: 2px;
}
.busy-note {
  margin: 0 0 10px;
  font-size: 0.78rem;
  color: #3b6cc5;
}

.region-title {
  font-size: 0.9rem;
  font-weight: 600;
}

.region-metrics {
  margin: 0;
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
}

.region-metrics dt {
  font-size: 0.66rem;
  color: var(--muted-dim);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.region-metrics dd {
  margin: 2px 0 0;
  font-size: 0.82rem;
}

.region-issues {
  margin: 12px 0 0;
  padding-left: 16px;
  font-size: 0.78rem;
  color: var(--muted);
  line-height: 1.5;
}

.region-notes {
  margin: 8px 0 0;
  padding-left: 16px;
  font-size: 0.78rem;
  color: var(--muted);
  line-height: 1.5;
  font-style: italic;
}

.region-note {
  margin: 12px 0 0;
  font-size: 0.78rem;
  color: var(--muted);
  line-height: 1.5;
}

.location-banner {
  padding: 10px 16px;
  font-size: 0.82rem;
  color: var(--muted);
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: baseline;
  border-left: 3px solid #c98a1a;
}

.location-banner.ok {
  border-left-color: #2f7a4f;
}

.location-banner strong {
  color: var(--text);
}

.location-source {
  font-size: 0.76rem;
}

.checks {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
}

.check {
  font-size: 0.7rem;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--line);
  color: #c53b3b;
}

.check.ok {
  color: #2f7a4f;
}

.results-grid.with-viewer {
  grid-template-columns: 1fr 1fr 340px;
}

.results-grid.with-viewer .map-container {
  height: 72vh;
}

.viewer-panel {
  padding: 6px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.viewer-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  padding: 4px 6px;
}

.viewer-title {
  font-size: 0.8rem;
  font-weight: 600;
}

.viewer-actions {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.viewer-actions .active {
  border-color: var(--accent);
  color: var(--accent);
}

.viewer-stage {
  height: 72vh;
  overflow: hidden;
  border-radius: 12px;
  background: #fff;
  border: 1px solid var(--line);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: grab;
  touch-action: none;
}

.viewer-stage.dragging {
  cursor: grabbing;
}

.viewer-stage img {
  max-width: 100%;
  max-height: 100%;
  transform-origin: center center;
  user-select: none;
  pointer-events: none;
}

.viewer-hint {
  margin: 0;
  font-size: 0.72rem;
  color: var(--muted-dim);
  text-align: center;
}

@media (max-width: 1280px) {
  .results-grid.with-viewer {
    grid-template-columns: 1fr 1fr;
  }
  .results-grid.with-viewer .region-list {
    grid-column: 1 / -1;
  }
}

.gallery {
  margin-bottom: 24px;
}
.gallery-head h2,
.signin-card h2,
.gallery-head p,
.signin-card p {
  margin: 0 0 14px;
  font-size: 14px;
  color: var(--text-muted, #6b7280);
  max-width: 720px;
}
.gallery-err {
  color: #b45309;
  font-size: 13px;
}
.gallery-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 14px;
}
.sample-card {
  position: relative;
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
  text-align: left;
  font: inherit;
  color: inherit;
  cursor: pointer;
  transition: transform 0.12s ease, box-shadow 0.12s ease;
}
.sample-card:hover:not(:disabled) {
  transform: translateY(-2px);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
}
.sample-card:disabled {
  cursor: progress;
}
.sample-thumb {
  height: 150px;
  background: #f3f1ec;
  overflow: hidden;
  border-bottom: 1px solid var(--border, #e7e3dc);
}
.sample-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.sample-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px 14px 14px;
}
.sample-title {
  font-weight: 600;
  font-size: 14.5px;
}
.sample-loc {
  font-size: 12.5px;
  color: var(--text-muted, #6b7280);
}
.sample-hl {
  font-size: 12.5px;
  line-height: 1.4;
  color: #374151;
}
.sample-opening {
  position: absolute;
  top: 10px;
  right: 10px;
  padding: 3px 10px;
  border-radius: 999px;
  background: #111827;
  color: #fff;
  font-size: 12px;
}
.signin-card {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 20px 22px;
  margin-bottom: 24px;
}
.signin-card p {
  margin: 0;
}
.my-docs {
  margin-top: 16px;
  padding: 16px 20px;
}
.my-docs h2 {
  margin: 0 0 4px;
  font-size: 18px;
}
.my-docs ul {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.my-docs .muted {
  margin-left: 8px;
  color: var(--text-muted, #9ca3af);
  font-size: 12px;
}
.playground-banner {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px 14px;
  padding: 12px 16px;
  margin-bottom: 14px;
  border: 1px solid #c7d2fe;
  background: #f5f7ff;
  font-size: 13.5px;
  color: #312e81;
}
.pg-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #6366f1;
}
.pg-actions {
  display: flex;
  gap: 6px;
  margin-left: auto;
}

@media (max-width: 980px) {
  .results-grid {
    grid-template-columns: 1fr;
  }
  .results-summary {
    grid-template-columns: 1fr;
  }
}

.ask-ai {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  text-align: left;
  font: inherit;
  font-size: 13px;
  padding: 12px 14px;
  border: 1px solid #c7d2fe;
  background: #f5f7ff;
  color: #312e81;
  cursor: pointer;
}
.ask-ai:hover {
  background: #eef2ff;
}
.ask-ai-icon {
  font-size: 16px;
  color: #4f46e5;
}
.ask-ai-btn {
  color: #4f46e5;
}
</style>
