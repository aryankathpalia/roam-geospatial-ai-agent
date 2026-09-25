<script lang="ts">
  import { onDestroy } from 'svelte';

  const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';

  type Phase = 'idle' | 'uploading' | 'error' | 'done';

  let phase: Phase = 'idle';
  let errorMessage = '';
  let dragOver = false;
  let fileInput: HTMLInputElement;

  let result: any = null;
  let usingSample = false;

  let map: any;
  let mapEl: HTMLDivElement;
  let L: any;
  let layerGroup: any;

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
  $: parcelRegions = result
    ? (result.pages ?? []).flatMap((p: any) =>
        (p.regions ?? []).flatMap((region: any, i: number) =>
          (region.parcels ?? []).map((parcel: any, j: number) => ({
            page: p.page_number,
            i: `${i}-${j}`,
            parcel
          }))
        )
      )
    : [];

  async function ensureLeaflet() {
    if (!L) {
      L = await import('leaflet');
    }
  }

  async function renderMap() {
    await ensureLeaflet();
    if (!map) {
      map = L.map(mapEl, { zoomControl: true, fadeAnimation: false, zoomAnimation: false });
      // Satellite imagery, not OSM's street-vector style -- a parcel
      // can sit on undeveloped rural land with almost nothing drawn at
      // high zoom in the vector style, which reads as "broken" even
      // when it's rendering correctly. Imagery always has ground detail.
      L.tileLayer(
        'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        { attribution: 'Esri, Maxar, Earthstar Geographics' }
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

    const bounds: any[] = [];

    for (const { page, i, parcel } of parcelRegions) {
      if (!parcel.boundary_geojson_wgs84) continue; // nothing to draw -- see card for why

      const valid = parcel.spatial_validation?.valid;
      const color = valid ? '#2f7a4f' : '#c53b3b';
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

      const b = layer.getBounds();
      if (b.isValid()) bounds.push(b);
    }

    if (bounds.length) {
      let combined = bounds[0];
      for (const b of bounds.slice(1)) combined = combined.extend(b);
      map.fitBounds(combined, { padding: [40, 40], animate: false });
    } else {
      map.setView([20, 0], 2);
    }
  }

  function selectRegion(key: string) {
    selectedKey = selectedKey === key ? null : key;
    renderMap();
  }

  async function loadSample() {
    errorMessage = '';
    const res = await fetch('/sample-data/sample-result.json');
    result = await res.json();
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
      result = data.result;
      phase = 'done';
      queueMicrotask(renderMap);
    } catch (err: any) {
      errorMessage =
        err?.message?.includes('Failed to fetch') || err?.name === 'TypeError'
          ? `Could not reach the ROAM backend at ${API_BASE}. It may be offline right now — try the sample result below instead.`
          : `Processing failed: ${err.message}`;
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
    phase = 'idle';
    result = null;
    errorMessage = '';
    selectedKey = null;
  }

  onDestroy(() => {
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

  {#if phase !== 'done'}
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
          <p>Processing document — OCR, layout detection, vision extraction and georeferencing all run server-side. This can take a while for large scans.</p>
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
        <button class="link-btn" on:click={loadSample}>View a sample result instead →</button>
      </div>
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

      <div class="results-grid">
        <div class="map-panel panel">
          <div bind:this={mapEl} class="map-container"></div>
        </div>

        <div class="region-list">
          {#if parcelRegions.length === 0}
            <div class="panel empty-state">
              <p>No georeferenced parcel geometry in this result yet — vision extraction may not have found boundary calls on this document's ParcelMap regions.</p>
            </div>
          {/if}

          {#each parcelRegions as { page, i, parcel } (regionKey(page, i))}
            {@const key = regionKey(page, i)}
            {@const v = parcel.spatial_validation}
            <button
              class="region-card panel"
              class:selected={selectedKey === key}
              on:click={() => selectRegion(key)}
            >
              <div class="region-head">
                <span class="region-title">
                  {parcel.vision_geometry?.parcel_label || `Page ${page} parcel`}
                </span>
                {#if v}
                  <span class="pill {v.valid ? 'low' : 'high'}">
                    {v.valid ? 'Valid' : 'Needs review'}
                  </span>
                {:else if parcel.extraction_note || parcel.georeference_error}
                  <span class="pill high">No geometry</span>
                {/if}
              </div>

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
          {/each}
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
}

.map-container {
  height: 560px;
  border-radius: 12px;
  overflow: hidden;
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
  padding: 14px 16px;
  cursor: pointer;
  background: var(--surface);
  transition: border-color 0.15s ease, background 0.15s ease;
}

.region-card.selected {
  border-color: rgba(52, 224, 161, 0.5);
  background: var(--accent-soft);
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

@media (max-width: 980px) {
  .results-grid {
    grid-template-columns: 1fr;
  }
  .results-summary {
    grid-template-columns: 1fr;
  }
}
</style>
