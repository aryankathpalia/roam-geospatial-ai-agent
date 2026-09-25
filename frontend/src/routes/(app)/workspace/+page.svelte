<script lang="ts">
  import { onDestroy, onMount } from 'svelte';

  const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';
  const LAST_DOCUMENT_KEY = 'roam:lastDocumentId';

  type Phase = 'idle' | 'uploading' | 'error' | 'done';

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
      lastFitBounds = combined;
      map.fitBounds(combined, { padding: [40, 40], animate: false });
    } else {
      lastFitBounds = null;
      map.setView([20, 0], 2);
    }
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
  // (idea 1), plus a per-parcel re-extract button (idea 2, scoped
  // down -- see the small-crop re-read rationale in the backend
  // endpoint's docstring). Neither is available on the static sample
  // result, since there's no real backend document behind it.
  // ---------------------------------------------------------------

  let editingKey: string | null = null;
  let editCalls: { bearing: string; distance: string }[] = [];
  let recomputing = false;
  let recomputeError = '';

  let reextracting: string | null = null;
  let reextractError = '';
  let suggestions: Record<string, any> = {};

  function callsToEdit(parcel: any) {
    const source = parcel.resolved_boundary_calls ?? parcel.vision_geometry?.boundary_calls ?? [];
    return source.map((c: any) => ({ bearing: c.bearing ?? '', distance: c.distance ?? '' }));
  }

  function startEdit(entry: any) {
    const key = regionKey(entry.page, entry.i);
    if (editingKey === key) {
      editingKey = null;
      return;
    }
    editingKey = key;
    editCalls = callsToEdit(entry.parcel);
    recomputeError = '';
  }

  function addCallRow() {
    editCalls = [...editCalls, { bearing: '', distance: '' }];
  }

  function removeCallRow(idx: number) {
    editCalls = editCalls.filter((_, i) => i !== idx);
  }

  async function submitRecompute(entry: any) {
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
          boundary_calls: editCalls.filter((c) => c.bearing.trim() || c.distance.trim())
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
      editingKey = null;
      queueMicrotask(renderMap);
    } catch (err: any) {
      recomputeError = `Recompute failed: ${err.message}`;
    } finally {
      recomputing = false;
    }
  }

  async function submitReextract(entry: any) {
    if (!documentId) return;
    const key = regionKey(entry.page, entry.i);
    reextracting = key;
    reextractError = '';
    try {
      const res = await fetch(`${API_BASE}/documents/${documentId}/reextract`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          page_number: entry.page,
          region_index: entry.regionIndex,
          parcel_index: entry.parcelIndex
        })
      });
      if (!res.ok) {
        const body = await res.text();
        throw new Error(`${res.status}: ${body.slice(0, 200)}`);
      }
      const data = await res.json();
      suggestions = { ...suggestions, [key]: data.vision_geometry };
    } catch (err: any) {
      reextractError = `Re-extraction failed: ${err.message}`;
    } finally {
      reextracting = null;
    }
  }

  function acceptSuggestion(entry: any) {
    const key = regionKey(entry.page, entry.i);
    const suggestion = suggestions[key];
    if (!suggestion) return;
    editingKey = key;
    editCalls = (suggestion.boundary_calls ?? []).map((c: any) => ({
      bearing: c.bearing ?? '',
      distance: c.distance ?? ''
    }));
    const rest = { ...suggestions };
    delete rest[key];
    suggestions = rest;
  }

  function dismissSuggestion(entry: any) {
    const key = regionKey(entry.page, entry.i);
    const rest = { ...suggestions };
    delete rest[key];
    suggestions = rest;
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
      rememberDocumentId(data.document_id ?? null);
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
    documentId = null;
    errorMessage = '';
    selectedKey = null;
    editingKey = null;
    suggestions = {};
    showAllCategories = false;
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
        {#if lastDocumentId}
          <button class="link-btn" on:click={resumeLastDocument} disabled={resuming}>
            {resuming ? 'Resuming…' : 'Resume last document →'}
          </button>
        {/if}
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

          {#each parcelRegions as entry (regionKey(entry.page, entry.i))}
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

              {#if selectedKey === key && documentId}
                <div class="review-tools">
                  <div class="review-tools-row">
                    <button class="btn btn-ghost btn-sm" on:click={() => startEdit(entry)}>
                      {editingKey === key ? 'Cancel edit' : 'Edit calls'}
                    </button>
                    <button
                      class="btn btn-ghost btn-sm"
                      disabled={reextracting === key}
                      on:click={() => submitReextract(entry)}
                    >
                      {reextracting === key ? 'Re-extracting…' : 'Re-extract this parcel'}
                    </button>
                  </div>

                  {#if reextractError}
                    <p class="review-error">{reextractError}</p>
                  {/if}

                  {#if suggestions[key]}
                    <div class="suggestion-box">
                      <p class="suggestion-title">
                        New reading found {suggestions[key].boundary_calls?.length ?? 0} call(s)
                        {suggestions[key].stated_area_acres ? ` · ${suggestions[key].stated_area_acres} ac stated` : ''}
                      </p>
                      <ul class="suggestion-calls">
                        {#each suggestions[key].boundary_calls ?? [] as c}
                          <li class="mono">{c.bearing} — {c.distance}</li>
                        {/each}
                      </ul>
                      <div class="review-tools-row">
                        <button class="btn btn-primary btn-sm" on:click={() => acceptSuggestion(entry)}>
                          Load into table
                        </button>
                        <button class="btn btn-ghost btn-sm" on:click={() => dismissSuggestion(entry)}>
                          Dismiss
                        </button>
                      </div>
                    </div>
                  {/if}

                  {#if editingKey === key}
                    <div class="call-editor">
                      <table class="call-table">
                        <thead>
                          <tr><th>Bearing</th><th>Distance</th><th></th></tr>
                        </thead>
                        <tbody>
                          {#each editCalls as call, idx}
                            <tr>
                              <td><input class="mono" bind:value={call.bearing} placeholder="N 45°00'00&quot; W" /></td>
                              <td><input class="mono" bind:value={call.distance} placeholder="100.00'" /></td>
                              <td><button class="row-remove" on:click={() => removeCallRow(idx)} aria-label="Remove call">×</button></td>
                            </tr>
                          {/each}
                        </tbody>
                      </table>
                      <div class="review-tools-row">
                        <button class="btn btn-ghost btn-sm" on:click={addCallRow}>+ Add call</button>
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

.suggestion-box {
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--accent-soft);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.suggestion-title {
  margin: 0;
  font-size: 0.78rem;
  font-weight: 600;
}

.suggestion-calls {
  margin: 0;
  padding-left: 16px;
  font-size: 0.76rem;
  line-height: 1.5;
}

.call-editor {
  display: flex;
  flex-direction: column;
  gap: 10px;
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

@media (max-width: 980px) {
  .results-grid {
    grid-template-columns: 1fr;
  }
  .results-summary {
    grid-template-columns: 1fr;
  }
}
</style>
