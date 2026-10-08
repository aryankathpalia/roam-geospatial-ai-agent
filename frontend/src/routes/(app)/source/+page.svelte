<script lang="ts">
  // Source document: every page of the uploaded PDF, tagged with the role ROAM gave it, in a large viewer
  // that can show the regions layout detection found on the page. Opens the tab's current document.
  import { onDestroy, onMount } from 'svelte';
  import { page as pageStore } from '$app/stores';
  import { API_BASE } from '$lib/auth';
  import { currentDocument, setCurrentDocument } from '$lib/currentDocument';

  type Region = { class: string; bbox: [number, number, number, number]; confidence?: number };
  type Page = { page_number: number; regions?: Region[]; sheet?: { role?: string; reason?: string } };

  const ROLE: Record<string, { label: string; tone: string }> = {
    target_parcel_map: { label: 'Target parcel map', tone: 'target' },
    reference_survey: { label: 'Reference survey', tone: 'map' },
    other_parcel_drawing: { label: 'Other drawing', tone: 'map' },
    location_or_aerial_map: { label: 'Location / aerial map', tone: 'map' },
    not_a_map: { label: 'Not a map', tone: 'text' }
  };
  const CLASS_COLOURS: Record<string, string> = {
    ParcelMap: '#e8590c',
    Text: '#1c7ed6',
    Table: '#2b8a3e',
    Seal: '#9c36b5',
    Picture: '#e67700',
    ScannedPrintout: '#495057'
  };

  let documentId = '';
  let pages: Page[] = [];
  let loading = false;
  let error = '';
  let filter: 'all' | 'maps' | 'text' = 'all';

  let open: Page | null = null;
  let showRegions = true;
  let natural = { w: 0, h: 0 };
  let zoom = 1;
  let pan = { x: 0, y: 0 };
  let drag: { x: number; y: number; px: number; py: number } | null = null;

  function roleOf(p: Page): { label: string; tone: string } {
    const r = p.sheet?.role;
    if (r && ROLE[r]) return ROLE[r];
    const classes = (p.regions ?? []).map((x) => x.class);
    if (classes.includes('ParcelMap')) return { label: 'Map (not judged)', tone: 'map' };
    if (classes.filter((c) => c === 'Table').length >= 2) return { label: 'Tables', tone: 'text' };
    return { label: 'Text', tone: 'text' };
  }
  const isMap = (p: Page) => roleOf(p).tone !== 'text';

  $: shown = pages.filter((p) => filter === 'all' || (filter === 'maps' ? isMap(p) : !isMap(p)));
  $: mapCount = pages.filter(isMap).length;
  $: counts = (open?.regions ?? []).reduce<Record<string, number>>((acc, r) => ((acc[r.class] = (acc[r.class] ?? 0) + 1), acc), {});

  async function load() {
    if (!documentId.trim()) return;
    loading = true;
    error = '';
    pages = [];
    try {
      const res = await fetch(`${API_BASE}/documents/${documentId.trim()}`);
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const data = await res.json();
      pages = data.result?.pages ?? [];
      setCurrentDocument(documentId.trim());
    } catch (e) {
      error = e instanceof Error ? `Could not load the document: ${e.message}` : 'Could not load the document.';
    } finally {
      loading = false;
    }
  }

  function openPage(p: Page) {
    open = p;
    natural = { w: 0, h: 0 };
    zoom = 1;
    pan = { x: 0, y: 0 };
  }
  function step(dir: number) {
    if (!open) return;
    const i = shown.findIndex((p) => p.page_number === open!.page_number);
    const next = shown[i + dir];
    if (next) openPage(next);
  }
  function onKey(e: KeyboardEvent) {
    if (!open) return;
    if (e.key === 'Escape') open = null;
    else if (e.key === 'ArrowRight') step(1);
    else if (e.key === 'ArrowLeft') step(-1);
  }
  function onWheel(e: WheelEvent) {
    e.preventDefault();
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const cx = e.clientX - rect.left - rect.width / 2;
    const cy = e.clientY - rect.top - rect.height / 2;
    const next = Math.min(8, Math.max(1, zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
    pan = next === 1 ? { x: 0, y: 0 } : { x: cx - ((cx - pan.x) * next) / zoom, y: cy - ((cy - pan.y) * next) / zoom };
    zoom = next;
  }
  function startDrag(e: PointerEvent) {
    if (zoom === 1) return;
    drag = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y };
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  }
  function moveDrag(e: PointerEvent) {
    if (drag) pan = { x: drag.px + e.clientX - drag.x, y: drag.py + e.clientY - drag.y };
  }

  onMount(() => {
    documentId = currentDocument($pageStore.url.searchParams.get('doc')) ?? '';
    if (documentId) load();
    window.addEventListener('keydown', onKey);
  });
  onDestroy(() => {
    if (typeof window !== 'undefined') window.removeEventListener('keydown', onKey);
  });
</script>

<svelte:head><title>Source document · ROAM</title></svelte:head>

<section class="head">
  <p class="kicker">Source document</p>
  <h1>The scanned document</h1>
  <p class="lede">Every page as uploaded, with the role ROAM gave it. Open a page to see the regions layout detection found on it.</p>
</section>

<form class="loader" on:submit|preventDefault={load}>
  <input class="mono" bind:value={documentId} placeholder="Document id" aria-label="Document id" />
  <button class="btn btn-ghost" type="submit" disabled={loading}>{loading ? 'Loading…' : 'Load'}</button>
  {#if pages.length}
    <a class="btn btn-primary" href={`${API_BASE}/documents/${documentId.trim()}/original.pdf`} target="_blank" rel="noreferrer">Download original PDF</a>
  {/if}
</form>
{#if error}<p class="error">{error}</p>{/if}

{#if pages.length}
  <div class="toolbar">
    <div class="filters" role="tablist">
      <button class:active={filter === 'all'} on:click={() => (filter = 'all')}>All pages <span>{pages.length}</span></button>
      <button class:active={filter === 'maps'} on:click={() => (filter = 'maps')}>Maps <span>{mapCount}</span></button>
      <button class:active={filter === 'text'} on:click={() => (filter = 'text')}>Text & tables <span>{pages.length - mapCount}</span></button>
    </div>
  </div>

  <div class="grid">
    {#each shown as p (p.page_number)}
      {@const role = roleOf(p)}
      <button class="card" on:click={() => openPage(p)}>
        <div class="thumb">
          <img src={`${API_BASE}/documents/${documentId.trim()}/pages/${p.page_number}/thumb.jpg`} alt={`Page ${p.page_number}`} />
        </div>
        <div class="meta">
          <span class="num">Page {p.page_number}</span>
          <span class="tag {role.tone}">{role.label}</span>
        </div>
      </button>
    {/each}
  </div>
{:else if !loading && !error}
  <p class="empty">Open a document from the workspace (or a sample) and it shows here.</p>
{/if}

{#if open}
  {@const role = roleOf(open)}
  <div class="viewer" role="dialog" aria-modal="true" aria-label={`Page ${open.page_number}`}>
    <header>
      <div class="vt">
        <strong>Page {open.page_number}</strong>
        <span class="tag {role.tone}">{role.label}</span>
        {#if open.sheet?.reason}<span class="reason">{open.sheet.reason}</span>{/if}
      </div>
      <div class="va">
        <label class="toggle"><input type="checkbox" bind:checked={showRegions} /> Show what ROAM detected</label>
        <button class="btn btn-ghost btn-sm" on:click={() => step(-1)} aria-label="Previous page">←</button>
        <button class="btn btn-ghost btn-sm" on:click={() => step(1)} aria-label="Next page">→</button>
        <button class="btn btn-ghost btn-sm" on:click={() => (zoom = 1, pan = { x: 0, y: 0 })}>Fit</button>
        <button class="btn btn-primary btn-sm" on:click={() => (open = null)}>Close</button>
      </div>
    </header>
    {#if showRegions && Object.keys(counts).length}
      <div class="legend">
        {#each Object.entries(counts) as [cls, n]}
          <span><i style={`background:${CLASS_COLOURS[cls] ?? '#868e96'}`}></i>{cls === 'ParcelMap' ? 'Parcel map' : cls === 'ScannedPrintout' ? 'Scanned printout' : cls} · {n}</span>
        {/each}
      </div>
    {/if}
    <!-- svelte-ignore a11y-no-static-element-interactions -->
    <div class="stage" class:grab={zoom > 1} on:wheel={onWheel} on:pointerdown={startDrag} on:pointermove={moveDrag} on:pointerup={() => (drag = null)}>
      <div class="canvas" style={`transform: translate(${pan.x}px, ${pan.y}px) scale(${zoom})`}>
        <img
          src={`${API_BASE}/documents/${documentId.trim()}/pages/${open.page_number}.png`}
          alt={`Page ${open.page_number}, full size`}
          draggable="false"
          on:load={(e) => {
            const im = e.currentTarget as HTMLImageElement;
            natural = { w: im.naturalWidth, h: im.naturalHeight };
          }}
        />
        {#if showRegions && natural.w}
          <svg viewBox={`0 0 ${natural.w} ${natural.h}`} preserveAspectRatio="none" aria-hidden="true">
            {#each open.regions ?? [] as r}
              {@const c = CLASS_COLOURS[r.class] ?? '#868e96'}
              <rect x={r.bbox[0]} y={r.bbox[1]} width={r.bbox[2]} height={r.bbox[3]} fill={c} fill-opacity="0.08" stroke={c} stroke-width={Math.max(2, natural.w / 600)} />
            {/each}
          </svg>
        {/if}
      </div>
    </div>
    <p class="hint">Scroll to zoom · drag to move · ← → for the previous / next page · Esc to close</p>
  </div>
{/if}

<style>
  .head {
    margin-bottom: 18px;
  }
  .kicker {
    margin: 0 0 6px;
    font-size: 12px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--accent, #d9480f);
  }
  h1 {
    margin: 0 0 8px;
  }
  .lede {
    margin: 0;
    color: var(--text-muted, #6b7280);
    max-width: 760px;
  }
  .loader {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin: 18px 0;
  }
  .loader input {
    flex: 1;
    min-width: 260px;
    padding: 11px 14px;
    border-radius: 9px;
    border: 1px solid var(--line, #e5e2dc);
    background: var(--surface, #fff);
    font-size: 0.95rem;
    color: inherit;
  }
  .error {
    color: #c92a2a;
  }
  .empty {
    color: var(--text-muted, #6b7280);
  }
  .toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
  }
  .filters {
    display: inline-flex;
    gap: 4px;
    padding: 4px;
    border-radius: 10px;
    border: 1px solid var(--line, #e5e2dc);
    background: var(--surface, #fff);
  }
  .filters button {
    border: 0;
    background: none;
    padding: 7px 12px;
    border-radius: 7px;
    font: inherit;
    font-size: 0.85rem;
    color: var(--text-muted, #6b7280);
    cursor: pointer;
  }
  .filters button span {
    margin-left: 4px;
    opacity: 0.7;
  }
  .filters button.active {
    background: var(--surface-muted, #f1efea);
    color: var(--text, #171613);
    font-weight: 600;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 14px;
  }
  .card {
    display: flex;
    flex-direction: column;
    padding: 0;
    overflow: hidden;
    border-radius: 12px;
    border: 1px solid var(--line, #e5e2dc);
    background: var(--surface, #fff);
    cursor: pointer;
    font: inherit;
    color: inherit;
    text-align: left;
    transition: transform 0.12s ease, box-shadow 0.12s ease;
  }
  .card:hover {
    transform: translateY(-2px);
    box-shadow: 0 8px 22px rgba(0, 0, 0, 0.08);
  }
  .thumb {
    aspect-ratio: 3 / 4;
    background: #f3f1ec;
    border-bottom: 1px solid var(--line, #e5e2dc);
    overflow: hidden;
  }
  .thumb img {
    width: 100%;
    height: 100%;
    object-fit: contain;
  }
  .meta {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 10px 12px 12px;
  }
  .num {
    font-weight: 600;
    font-size: 0.9rem;
  }
  .tag {
    align-self: flex-start;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 600;
    background: #f1f3f5;
    color: #495057;
  }
  .tag.target {
    background: #fff4e6;
    color: #d9480f;
  }
  .tag.map {
    background: #e7f5ff;
    color: #1864ab;
  }
  .viewer {
    position: fixed;
    inset: 0;
    z-index: 1500;
    display: flex;
    flex-direction: column;
    background: rgba(20, 20, 18, 0.98);
    color: #f1f3f5;
  }
  .viewer header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 12px 18px;
  }
  .vt {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }
  .reason {
    font-size: 0.82rem;
    color: #adb5bd;
    max-width: 640px;
  }
  .va {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .viewer :global(.btn-ghost) {
    color: #f1f3f5;
    border-color: rgba(255, 255, 255, 0.25);
  }
  .toggle {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    margin-right: 8px;
    font-size: 0.85rem;
  }
  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 14px;
    padding: 0 18px 8px;
    font-size: 0.8rem;
    color: #ced4da;
  }
  .legend i {
    display: inline-block;
    width: 10px;
    height: 10px;
    margin-right: 6px;
    border-radius: 2px;
  }
  .stage {
    position: relative;
    flex: 1;
    overflow: hidden;
    display: grid;
    place-items: center;
    padding: 8px;
  }
  .stage.grab {
    cursor: grab;
  }
  .canvas {
    position: relative;
    max-width: 100%;
    max-height: 100%;
    transform-origin: center center;
  }
  .canvas img {
    display: block;
    max-width: calc(100vw - 32px);
    max-height: calc(100vh - 150px);
    background: #fff;
    user-select: none;
  }
  .canvas svg {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
  }
  .hint {
    margin: 0;
    padding: 6px 18px 12px;
    font-size: 0.78rem;
    color: #868e96;
    text-align: center;
  }
</style>
