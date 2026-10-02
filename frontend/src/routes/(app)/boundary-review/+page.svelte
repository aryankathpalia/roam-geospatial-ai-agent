<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { boundaryRefs, groupCandidates, type BoundaryRef } from '$lib/boundaryCandidates';

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
  let draggingIdx: number | null = null;
  let selectedVertexIdx: number | null = null;

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
    return groupCandidates(all).flatMap((g) => g.refs);
  }

  $: parcelGroups = groupCandidates(parcelRefs);
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
      vertices = confirmed.vertices.map(([x, y]: [number, number]) => [x * sx, y * sy]);
      seedSource = 'confirmed';
      return;
    }

    if (ring && ring.length >= 3 && localTransform) {
      vertices = applyLocalTransform(ring, localTransform);
      seedSource = 'traverse';
      return;
    }

    // The roster's point inside this parcel: a small square there, to drag onto its boundary.
    if (ref.point) {
      const hw = cropWidth * 0.07;
      const hh = cropHeight * 0.07;
      const cx = Math.min(Math.max(ref.point[0] * cropWidth, hw), cropWidth - hw);
      const cy = Math.min(Math.max(ref.point[1] * cropHeight, hh), cropHeight - hh);
      vertices = [
        [cx - hw, cy - hh],
        [cx + hw, cy - hh],
        [cx + hw, cy + hh],
        [cx - hw, cy + hh]
      ];
      seedSource = 'none';
      return;
    }

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
    draggingIdx = idx;
    selectedVertexIdx = idx;
    (e.target as Element).setPointerCapture(e.pointerId);
  }

  function onSvgMove(e: PointerEvent, svg: SVGSVGElement) {
    if (draggingIdx === null) return;
    const [x, y] = svgPoint(e, svg);
    // Absolute-space edit: only the dragged vertex moves. No bearing/
    // distance recompute, no cascading re-walk of the rest of the
    // ring -- this is the fix for the distortion bug from the
    // Leaflet-based editor.
    vertices[draggingIdx] = [
      Math.max(0, Math.min(cropWidth, x)),
      Math.max(0, Math.min(cropHeight, y))
    ];
    vertices = vertices; // trigger reactivity
  }

  function onSvgUp() {
    draggingIdx = null;
  }

  function addVertexOnEdge(edgeIdx: number, e: MouseEvent) {
    const svg = (e.currentTarget as SVGElement).closest('svg') as SVGSVGElement;
    const [x, y] = svgPoint(e as unknown as PointerEvent, svg);
    const insertAt = edgeIdx + 1;
    vertices = [...vertices.slice(0, insertAt), [x, y], ...vertices.slice(insertAt)];
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
    const [x1, y1] = vertices[bestIdx];
    const [x2, y2] = vertices[(bestIdx + 1) % vertices.length];
    const mid: [number, number] = [(x1 + x2) / 2, (y1 + y2) / 2];
    const insertAt = bestIdx + 1;
    vertices = [...vertices.slice(0, insertAt), mid, ...vertices.slice(insertAt)];
    selectedVertexIdx = insertAt;
  }

  function deleteSelectedVertex() {
    if (selectedVertexIdx === null || vertices.length <= 3) return;
    vertices = vertices.filter((_, i) => i !== selectedVertexIdx);
    selectedVertexIdx = null;
  }

  function onKeydown(e: KeyboardEvent) {
    if ((e.key === 'Delete' || e.key === 'Backspace') && selectedVertexIdx !== null) {
      e.preventDefault();
      deleteSelectedVertex();
    }
  }

  function resetToSeed() {
    if (selected) seedVertices(selected);
  }

  async function confirmBoundary() {
    if (!selected || vertices.length < 3) return;
    saveStatus = 'saving';
    try {
      const local_vertices = localTransform ? toLocalVertices(vertices, localTransform) : null;
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
          vertices,
          crop_width: cropWidth,
          crop_height: cropHeight,
          local_vertices
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

  function polygonPoints(pts: [number, number][]): string {
    return pts.map(([x, y]) => `${x},${y}`).join(' ');
  }

  // Where ROAM read each parcel on this canvas (the roster's points), so you can see which is which.
  $: markers = selected
    ? parcelRefs
        .filter((r) => r.kind === 'parcel' && r.point && r.pageNumber === selected.pageNumber && r.regionIndex === selected.regionIndex)
        .map((r) => ({ x: r.point![0] * cropWidth, y: r.point![1] * cropHeight, label: r.label, active: r.key === selectedKey }))
    : [];
  $: realParcels = parcelRefs.filter((r) => r.kind === 'parcel');
  $: confirmedCount = realParcels.filter((r) => r.parcel.human_confirmed).length;

</script>

<svelte:window on:keydown={onKeydown} />

<div class="page">
  <header>
    <h1>Confirm parcel boundary</h1>
    <p class="hint">
      Parcels on the ParcelMap sheets ROAM found are listed by sheet, most likely target sheet first. Pick the target parcel, drag
      the outline onto its boundary on the drawing (seeded from a previous confirmation, else the
      vision-extracted calls, else a blank square), then Confirm. Confirming saves your outline and places
      it on the map; verification details are on the map page.
      {#if documentId.trim()}
        <a href={`/workspace?doc=${encodeURIComponent(documentId.trim())}`}>Back to map →</a>
      {/if}
    </p>
  </header>

  <div class="load-row">
    <input
      type="text"
      placeholder="document id"
      bind:value={documentId}
      on:keydown={(e) => e.key === 'Enter' && loadDocument()}
    />
    <button on:click={() => loadDocument()} disabled={loading}>{loading ? 'Loading…' : 'Load'}</button>
    {#if loadError}<span class="error">{loadError}</span>{/if}
    {#if parcelRefs.length > 0}
      <span class="spacer" />
      <span class="confirmed-count">{confirmedCount} / {realParcels.length} confirmed</span>
      <a
        class="primary-link"
        class:disabled={confirmedCount === 0}
        href={confirmedCount === 0 ? undefined : `/workspace?doc=${encodeURIComponent(documentId.trim())}`}
      >
        Continue to map →
      </a>
    {/if}
  </div>

  {#if parcelRefs.length > 0}
    <div class="body">
      <aside>
        <h2>Parcels</h2>
        {#if reading}
          <p class="hint reading-note">ROAM is still reading the document in the background — you can start drawing now.</p>
          {#if showReprocessHint}
            <p class="hint reading-note stuck-hint">
              Taking longer than usual — if this doesn't finish, the process reading it may have
              been interrupted.
              <button class="restore-btn" disabled={reprocessing} on:click={reprocessDocument}>
                {reprocessing ? 'Restarting…' : 'Restart processing'}
              </button>
            </p>
          {/if}
        {/if}
        {#each parcelGroups as group, gi}
          {#if !group.primary && (gi === 0 || parcelGroups[gi - 1].primary)}
            <h2 class="other-heading">Other maps</h2>
            <p class="hint sheet-note">Sheets that are probably not this packet's own parcel map (reference surveys, aerial or location maps, other drawings) — still selectable.</p>
          {/if}
          <h3 class="sheet-heading">
            Page {group.pageNumber} · parcel map
            {#if group.badge}<span class="ref-badge" title={group.note ?? ''}>{group.badge}</span>{/if}
          </h3>
          {#if group.note}<p class="hint sheet-note">{group.note}</p>{/if}
          <ul>
            {#each group.refs as ref}
              <li class="parcel-row">
                <button
                  class:active={ref.key === selectedKey}
                  class:muted-row={ref.kind !== 'parcel'}
                  on:click={() => selectParcel(ref)}
                >
                  <span class="parcel-name">{ref.label}</span>
                  {#if ref.parcel.human_confirmed}<span class="badge">confirmed</span>{/if}
                  {#if ref.manual}<span class="ref-badge" title="You named this parcel; ROAM did not detect its identity.">manually named</span>{/if}
                  {#if ref.meta}<span class="parcel-meta">{ref.meta}</span>{/if}
                </button>
                {#if ref.parcelId && !ref.parcel.human_confirmed}
                  <button
                    class="delete-btn"
                    title="Delete this parcel entity (only possible before it's confirmed)"
                    on:click={() => deleteParcelEntity(ref)}
                  >✕</button>
                {/if}
              </li>
            {/each}
          </ul>
        {/each}

        {#if deleteError}<p class="hint error-hint">{deleteError}</p>{/if}

        {#if excludedParcels.length > 0}
          <h2 class="excluded-heading">Not offered by default ({excludedParcels.length})</h2>
          <p class="hint excluded-hint">
            Flagged as likely vicinity/locus-map duplicates, scattered fragments, or otherwise
            under-evidenced. Restore if this looks wrong, or delete it for good.
          </p>
          <ul>
            {#each excludedParcels as ref}
              <li class="excluded-item">
                <span class="excluded-label" title={ref.excludedReason ?? ''}>Page {ref.pageNumber} · {ref.label}</span>
                <button class="restore-btn" on:click={() => restoreExcludedParcel(ref)}>Restore</button>
                {#if ref.parcelId}
                  <button class="delete-btn" title="Delete for good" on:click={() => deleteParcelEntity(ref)}>✕</button>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      </aside>

      <main>
        {#if selected}
          <div class="toolbar">
            <span class="seed-note">
              seed: <strong>{seedSource}</strong>
              {#if seedSource === 'traverse'}
                (rough fit from vision-extracted calls — position/rotation/scale are guesses, correct
                by dragging)
              {:else if seedSource === 'none'}
                (no automatic shape available — draw from scratch)
              {/if}
              {#if !localTransform && !reading && selected.kind === 'parcel'}
                <span class="warn">— no survey calls were extracted for this parcel, so the outline is saved
                  but cannot be placed on the map.</span
                >
              {/if}
            </span>
            <button on:click={resetToSeed}>Reset to seed</button>
            <button on:click={addPointOnLongestEdge}>Add point</button>
            <button
              on:click={deleteSelectedVertex}
              disabled={selectedVertexIdx === null || vertices.length <= 3}
            >
              Delete selected vertex
            </button>
            <button
              on:click={confirmBoundary}
              disabled={vertices.length < 3 || selected.kind === 'reading' || (selected.kind === 'manual_new' && !manualName.trim())}
            >
              Confirm boundary
            </button>
            {#if saveStatus === 'saving'}<span>saving…</span>{/if}
            {#if saveStatus === 'saved' && savedState === 'waiting_for_document'}
              <span class="ok">saved ✓ — ROAM is still reading the document; your outline is placed on the
                map automatically when it finishes. <a href={`/workspace?doc=${encodeURIComponent(documentId.trim())}`}>Continue to map →</a></span
              >
            {:else if saveStatus === 'saved' && georeferenced}
              <span class="ok">saved ✓ — placed on the map.
                <a href={`/workspace?doc=${encodeURIComponent(documentId.trim())}`}>View on map →</a></span
              >
            {:else if saveStatus === 'saved'}
              <span class="ok">saved ✓ (pixel shape only — not projected)</span>
            {/if}
            {#if saveStatus === 'error'}<span class="error">save failed</span>{/if}
          </div>

          {#if selected.kind === 'reading'}
            <p class="hint pending-note">
              Reading parcel labels from this map… you can outline a parcel as soon as the names appear.
            </p>
          {:else if selected.kind === 'manual_new'}
            <div class="manual-name">
              <label>
                Parcel name
                <input type="text" bind:value={manualName} placeholder="e.g. Parcel 1" />
              </label>
              <span class="hint">
                You are naming this parcel yourself — ROAM did not detect its identity, and no parcel ID is inferred.
              </span>
            </div>
          {/if}

          <div class="stage">
            <img bind:this={cropImgEl} src={cropSrc} alt="source crop" on:load={onCropLoad} />
            {#if cropWidth > 0}
              <svg
                viewBox={`0 0 ${cropWidth} ${cropHeight}`}
                preserveAspectRatio="none"
                on:pointermove={(e) => onSvgMove(e, e.currentTarget)}
                on:pointerup={onSvgUp}
                on:pointerleave={onSvgUp}
              >
                {#each markers as m}
                  <circle cx={m.x} cy={m.y} r={cropWidth * 0.0045} class="roster-dot" class:active={m.active} />
                  <text x={m.x + cropWidth * 0.009} y={m.y + cropWidth * 0.004} class="roster-label" class:active={m.active} style={`font-size:${cropWidth * 0.013}px`}>{m.label}</text>
                {/each}
                <polygon points={polygonPoints(vertices)} class="boundary-poly" />
                {#each vertices as [x, y], i}
                  <line
                    x1={x}
                    y1={y}
                    x2={vertices[(i + 1) % vertices.length][0]}
                    y2={vertices[(i + 1) % vertices.length][1]}
                    class="edge-hit"
                    on:dblclick={(e) => addVertexOnEdge(i, e)}
                  />
                {/each}
                {#each vertices as [x, y], i}
                  <circle
                    cx={x}
                    cy={y}
                    r={cropWidth / 220 + 3}
                    class="vertex"
                    class:selected={i === selectedVertexIdx}
                    on:pointerdown={(e) => onVertexDown(i, e)}
                  />
                {/each}
              </svg>
            {/if}
          </div>
          <p class="hint">
            Drag a point to move it (only that vertex moves). Double-click an edge, or use "Add
            point", to add a vertex -- add as many as the shape needs, no 3/4-point limit. Click a
            vertex then press Delete/Backspace to remove it.
          </p>
        {/if}
      </main>
    </div>
  {:else if result}
    <p>No parcels with geometry found on this document.</p>
  {/if}
</div>

<style>
  .page {
    padding: 1.5rem;
    max-width: 1200px;
    margin: 0 auto;
    font-family: system-ui, sans-serif;
  }
  header h1 {
    font-size: 1.25rem;
    margin-bottom: 0.25rem;
  }
  .hint {
    color: #666;
    font-size: 0.85rem;
  }
  .load-row {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    margin: 1rem 0;
  }
  .load-row input {
    flex: 1;
    padding: 0.4rem 0.6rem;
    border: 1px solid #ccc;
    border-radius: 4px;
  }
  .error {
    color: #c0392b;
  }
  .ok {
    color: #2e7d32;
  }
  .warn {
    color: #b45f00;
  }
  .spacer {
    flex: 1;
  }
  .confirmed-count {
    font-size: 0.8rem;
    color: #555;
  }
  .primary-link {
    padding: 0.35rem 0.8rem;
    border-radius: 4px;
    background: #3477eb;
    color: #fff;
    font-size: 0.85rem;
    text-decoration: none;
  }
  .primary-link.disabled {
    opacity: 0.45;
    pointer-events: none;
  }
  .body {
    display: grid;
    grid-template-columns: 240px 1fr;
    gap: 1rem;
  }
  aside ul {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  aside button {
    width: 100%;
    text-align: left;
    padding: 0.4rem 0.5rem;
    border: 1px solid #ddd;
    background: #fafafa;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.85rem;
  }
  aside button.active {
    border-color: #3477eb;
    background: #eaf1ff;
  }
  .excluded-heading {
    margin-top: 1.25rem;
    font-size: 0.85rem;
    color: #777;
  }
  .excluded-hint {
    margin: 0.2rem 0 0.5rem 0;
    font-size: 0.75rem;
  }
  .excluded-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.4rem;
    padding: 0.3rem 0.5rem;
    font-size: 0.8rem;
    color: #888;
  }
  .excluded-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    cursor: help;
  }
  .excluded-label {
    flex: 1 1 auto;
    min-width: 0;
  }
  aside .restore-btn {
    width: auto;
  }
  .sheet-heading {
    margin: 0.9rem 0 0.25rem;
    font-size: 0.78rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #55534b;
  }
  .ref-badge {
    display: inline-block;
    margin-left: 0.4rem;
    padding: 0 0.35rem;
    border-radius: 4px;
    font-size: 0.68rem;
    font-weight: 500;
    text-transform: none;
    letter-spacing: 0;
    background: #ecebe4;
    color: #6b6a63;
  }
  .parcel-name {
    display: block;
    font-weight: 600;
  }
  .parcel-meta {
    display: block;
    margin-top: 0.1rem;
    font-size: 0.72rem;
    color: #6b6a63;
  }
  aside button.muted-row {
    color: #77756c;
    font-style: italic;
  }
  .manual-name {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.6rem;
    margin: 0.3rem 0 0.5rem;
    font-size: 0.85rem;
  }
  .manual-name input {
    margin-left: 0.4rem;
    padding: 0.25rem 0.5rem;
  }
  .roster-dot {
    fill: #1f6f4a;
    stroke: #fff;
    stroke-width: 2;
    pointer-events: none;
  }
  .roster-dot.active {
    fill: #3477eb;
  }
  .roster-label {
    fill: #1f6f4a;
    paint-order: stroke;
    stroke: #fff;
    stroke-width: 4px;
    font-weight: 600;
    pointer-events: none;
  }
  .roster-label.active {
    fill: #1d4fb0;
  }
  .other-heading {
    margin: 1.1rem 0 0.2rem;
    font-size: 0.9rem;
  }
  .reading-note,
  .pending-note {
    margin: 0.2rem 0 0.5rem;
    font-size: 0.78rem;
  }
  .sheet-note {
    margin: 0 0 0.3rem;
    font-size: 0.72rem;
  }
  .restore-btn {
    flex-shrink: 0;
    font-size: 0.75rem;
    padding: 0.15rem 0.5rem;
    border: 1px solid #ccc;
    border-radius: 4px;
    background: #fafafa;
    cursor: pointer;
  }
  .parcel-row {
    display: flex;
    align-items: center;
    gap: 0.25rem;
  }
  .parcel-row button:first-child {
    flex: 1;
  }
  .delete-btn {
    flex-shrink: 0;
    font-size: 0.75rem;
    width: 1.5rem;
    height: 1.5rem;
    line-height: 1;
    border: 1px solid #ccc;
    border-radius: 4px;
    background: #fafafa;
    color: #a33;
    cursor: pointer;
  }
  .delete-btn:hover {
    background: #fdeaea;
    border-color: #a33;
  }
  .error-hint {
    color: #a33;
  }
  .badge {
    margin-left: 0.4rem;
    font-size: 0.7rem;
    color: #2e7d32;
  }
  .toolbar {
    display: flex;
    gap: 0.6rem;
    align-items: center;
    flex-wrap: wrap;
    margin-bottom: 0.5rem;
    font-size: 0.85rem;
  }
  .seed-note {
    color: #555;
  }
  .stage {
    position: relative;
    max-width: 100%;
    border: 1px solid #ddd;
    line-height: 0;
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
  .boundary-poly {
    fill: rgba(52, 119, 235, 0.15);
    stroke: #3477eb;
    stroke-width: 2;
    vector-effect: non-scaling-stroke;
    pointer-events: none;
  }
  .edge-hit {
    stroke: transparent;
    stroke-width: 14;
    vector-effect: non-scaling-stroke;
    cursor: copy;
  }
  .vertex {
    fill: #fff;
    stroke: #3477eb;
    stroke-width: 2;
    vector-effect: non-scaling-stroke;
    cursor: grab;
  }
  .vertex.selected {
    fill: #ffb300;
    stroke: #b45f00;
  }
</style>
