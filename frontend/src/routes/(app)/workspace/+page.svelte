<script lang="ts">
  import { onDestroy, onMount } from 'svelte';

  const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';
  const LAST_DOCUMENT_KEY = 'roam:lastDocumentId';

  type Phase = 'idle' | 'uploading' | 'processing' | 'error' | 'done';

  // Pipeline stages in the order they actually execute server-side
  // (app/services/progress.py is the source of truth this mirrors).
  // "detail" starts null and fills in with a real count once that
  // stage's work is actually done -- rendered as an em-dash
  // placeholder until then, never a fake/simulated number.
  const STAGE_ORDER = ['rendering', 'layout_detection', 'ocr', 'georeferencing', 'vision_extraction'] as const;
  const STAGE_LABELS: Record<string, string> = {
    rendering: 'Rendering document pages',
    layout_detection: 'Detecting layout & document structure',
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

  onMount(() => {
    try {
      lastDocumentId = localStorage.getItem(LAST_DOCUMENT_KEY);
    } catch {
      // localStorage unavailable (private window, blocked storage) --
      // resume just won't be offered, upload still works normally.
    }
  });

  function rememberDocumentId(id: string | null) {
    documentId = id;
    if (!id) return;
    try {
      localStorage.setItem(LAST_DOCUMENT_KEY, id);
    } catch {
      // best-effort only
    }
  }

  async function resumeLastDocument() {
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
      result = data.result;
      rememberDocumentId(data.document_id ?? lastDocumentId);
      usingSample = false;
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
  $: allParcelRegions = result
    ? (result.pages ?? []).flatMap((p: any) =>
        (p.regions ?? []).flatMap((region: any, regionIndex: number) =>
          (region.parcels ?? []).map((parcel: any, parcelIndex: number) => ({
            page: p.page_number,
            regionIndex,
            parcelIndex,
            i: `${regionIndex}-${parcelIndex}`,
            category: region.category ?? null,
            parcel
          }))
        )
      )
    : [];

  // Region classification (idea 7) is a DISPLAY filter, never a drop --
  // every region still gets extracted server-side regardless of
  // category. "not_a_parcel_drawing" content is deprioritized by
  // default (it was the majority, 72/132, of noise in the labeled
  // corpus) but always one click away via showAllCategories, so a
  // misclassification costs a click, never a lost parcel.
  $: parcelRegions = showAllCategories
    ? allParcelRegions
    : allParcelRegions.filter((r: any) => r.category !== 'not_a_parcel_drawing');

  $: hiddenCount = allParcelRegions.length - parcelRegions.length;

  // Parcels vision found no attributable boundary calls for ("No
  // geometry" cards) are real, useful info -- but a document can have a
  // dozen of them ahead of the 2-3 real ParcelMap results, burying what
  // the reviewer actually came to look at. Collapsed by default, one
  // click away, same pattern as the category filter above.
  let showNoGeometry = false;
  $: geometryRegions = parcelRegions.filter((e: any) => e.parcel.spatial_validation);
  $: noGeometryRegions = parcelRegions.filter((e: any) => !e.parcel.spatial_validation);

  async function ensureLeaflet() {
    if (!L) {
      L = await import('leaflet');
    }
  }

  async function renderMap() {
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

    for (const entryItem of parcelRegions) {
      const { page, i, parcel } = entryItem;
      if (!parcel.boundary_geojson_wgs84) continue; // nothing to draw -- see card for why

      const shapeOk = parcel.spatial_validation?.valid;
      const locationPrecision = parcelLocationPrecision(parcel);
      const gate = calibrationGate(parcel);
      const color = gate === 'unconfirmed'
        ? '#6b6a63' // never green/amber for an uncorroborated placement -- see calibrationGate()
        : shapeOk
          ? locationPrecision === 'surveyed' || locationPrecision === 'manual'
            ? '#2f7a4f'
            : '#c98a1a'
          : '#c53b3b';
      const key = regionKey(page, i);

      const layer = L.geoJSON(parcel.boundary_geojson_wgs84, {
        style: {
          color,
          weight: selectedKey === key ? 3.5 : 2,
          fillColor: color,
          fillOpacity: selectedKey === key ? 0.22 : 0.12
        }
      });

      layer.on('click', () => selectRegion(key));
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
      map.fitBounds(combined, { padding: [40, 40], animate: false });
    } else {
      lastFitBounds = null;
      map.setView([20, 0], 2);
    }
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
    street: 'Street address (approx.)',
    city: 'City / ZIP only (coarse)',
    manual: 'Manually pinned',
    none: 'Not placed'
  };

  $: anchorPrecision = !result
    ? 'none'
    : result.anchor?.precision ?? (result.anchor_lat != null ? 'street' : 'none');
  $: anchorSource = result?.anchor?.source ?? null;

  // A reviewer can pin one parcel's anchor by hand (below the call
  // editor) rather than trust the document-wide geocoded/state-plane
  // guess -- that override is per-parcel, so its location check has to
  // be evaluated per-parcel too, not from the shared anchorPrecision.
  function parcelLocationPrecision(parcel: any): string {
    return parcel.anchor_override ? 'manual' : anchorPrecision;
  }

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
  function calibrationGate(parcel: any): 'not_applicable' | 'placeable' | 'unconfirmed' {
    if (!parcel.human_confirmed) return 'not_applicable';
    const status = parcel.calibration?.status;
    if (status === 'cross_validated' || status === 'single_source') return 'placeable';
    // status === 'unverified', or calibration missing entirely despite
    // human_confirmed (no anchor to calibrate against) -- both mean the
    // same thing to a viewer: this placement was never corroborated.
    return 'unconfirmed';
  }

  function verdict(parcel: any) {
    const shapeOk = !!parcel.spatial_validation?.valid;
    const locationPrecision = parcelLocationPrecision(parcel);
    const locationOk = locationPrecision === 'surveyed' || locationPrecision === 'manual';
    if (!parcel.spatial_validation) return { cls: 'high', label: 'No geometry' };
    const gate = calibrationGate(parcel);
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

  async function submitRecompute(entry: any, autoFix: boolean = false, keepOpen: boolean = false) {
    if (!documentId) return;
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
      phase = 'processing';
      startPolling();
    } catch (err: any) {
      errorMessage =
        err?.message?.includes('Failed to fetch') || err?.name === 'TypeError'
          ? `Could not reach the ROAM backend at ${API_BASE}. It may be offline right now — try the sample result below instead.`
          : `Upload failed: ${err.message}`;
      phase = 'error';
    }
  }

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
    map?.remove();
  });
</script>

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

  {#if phase === 'idle' || phase === 'uploading' || phase === 'error'}
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
          <button class="link-btn" on:click={resumeLastDocument} disabled={resuming}>
            {resuming ? 'Resuming…' : 'Resume last document →'}
          </button>
        {/if}
        <button class="link-btn" on:click={loadSample}>View a sample result instead →</button>
      </div>
    </section>
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
              <span class="trace-detail mono">{traceDetailByStage[stage] ?? '—'}</span>
            </span>
          </li>
        {/each}
      </ol>
    </section>
  {/if}

  {#if phase === 'done' && result}
    <section class="results fade-up">
      {#if usingSample}
        <div class="sample-banner panel">
          <span class="status-dot"></span>
          Showing a sample result — {result.source_note}
        </div>
      {/if}

      <div class="results-summary">
        <div class="stat panel">
          <span class="stat-label">Pages</span>
          <span class="stat-value mono">{result.total_pages ?? result.pages?.length ?? '—'}</span>
        </div>
        <div class="stat panel">
          <span class="stat-label">Need review</span>
          <span class="stat-value mono">{result.pages_needing_review ?? '—'}</span>
        </div>
        <div class="stat panel">
          <span class="stat-label">Parcels</span>
          <span class="stat-value mono">{parcelRegions.length}</span>
        </div>
      </div>

      <div class="location-banner panel" class:ok={anchorPrecision === 'surveyed'}>
        <strong>Location:</strong> {LOCATION_LABELS[anchorPrecision]}
        {#if anchorSource}<span class="location-source">— {anchorSource}</span>{/if}
        {#if anchorPrecision !== 'surveyed'}
          <span class="location-source">Shapes are exact, but where they sit on the map is approximate, so no parcel on this document can be marked Verified.</span>
        {/if}
      </div>

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
              <p>No georeferenced parcel geometry in this result yet — vision extraction may not have found boundary calls on this document's ParcelMap regions.</p>
            </div>
          {/if}

          {#if hiddenCount > 0}
            <button class="category-banner panel" on:click={() => (showAllCategories = true)}>
              {hiddenCount} region{hiddenCount === 1 ? '' : 's'} likely not a boundary map (aerial, vicinity map, certificate) hidden — click to show
            </button>
          {:else if showAllCategories && allParcelRegions.length}
            <button class="category-banner panel" on:click={() => (showAllCategories = false)}>
              Showing all regions — click to hide likely non-plat content again
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
                  {#if v || parcel.extraction_note || parcel.georeference_error}
                    {@const verdictInfo = verdict(parcel)}
                    <span class="pill {verdictInfo.cls}">{verdictInfo.label}</span>
                  {/if}
                </div>

                {#if v}
                  {@const locationPrecision = parcelLocationPrecision(parcel)}
                  <div class="checks">
                    <span class="check" class:ok={v.valid}>
                      {v.valid ? '✓' : '✗'} Shape
                    </span>
                    <span class="check" class:ok={locationPrecision === 'surveyed' || locationPrecision === 'manual'}>
                      {locationPrecision === 'surveyed' || locationPrecision === 'manual' ? '✓' : '✗'} Location · {LOCATION_LABELS[locationPrecision]}
                    </span>
                  </div>
                {/if}

                {#if v}
                  <dl class="region-metrics">
                    <div><dt>Precision</dt><dd class="mono">{v.precision_ratio ? `1:${v.precision_ratio}` : '—'}</dd></div>
                    <div><dt>Closure</dt><dd class="mono">{parcel.boundary_geojson_wgs84?.properties?.closure_error_ft ?? '—'} ft</dd></div>
                    <div><dt>Area</dt><dd class="mono">{v.area_acres ? `${v.area_acres} ac` : '—'}</dd></div>
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

              {#if selectedKey === key && documentId}
                <div class="review-tools">
                  <div class="review-tools-row">
                    <button class="btn btn-ghost btn-sm" on:click={() => startEdit(entry)}>
                      {editingKey === key ? 'Cancel edit' : 'Edit calls'}
                    </button>
                    <button class="btn btn-ghost btn-sm" on:click={() => openViewer(entry)}>
                      Open source drawing
                    </button>
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

@media (max-width: 980px) {
  .results-grid {
    grid-template-columns: 1fr;
  }
  .results-summary {
    grid-template-columns: 1fr;
  }
}
</style>
