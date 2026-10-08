<script lang="ts">
  import { currentDocument } from '$lib/currentDocument';
  import { onMount } from 'svelte';
  import { page } from '$app/stores';

  const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';

  // The report model (app/services/report.py) and the reviewer's unsaved edits.
  let documentId = '';
  let report: any = null;
  let loading = false;
  let error = '';
  let saveState: 'idle' | 'saving' | 'saved' | 'error' = 'idle';
  let editor = '';
  let projectDraft: Record<string, string> = {};
  let parcelDraft: Record<string, Record<string, string>> = {};
  let openParcel: string | null = null;
  let figureStamp = Date.now();

  const PROJECT_FIELDS: [string, string][] = [
    ['title', 'Report title'],
    ['client', 'Client'],
    ['project_ref', 'Project reference'],
    ['prepared_by', 'Prepared by'],
    ['source_type', 'Source type'],
    ['county', 'County'],
    ['state', 'State']
  ];
  const REVIEW = [
    ['pending', 'Pending'],
    ['approved', 'Approved'],
    ['needs_field_check', 'Needs field check'],
    ['rejected', 'Rejected']
  ];
  const EXPORTS = [
    ['zip', 'Full deliverable package', 'ZIP — PDF report, GeoJSON, KML, shapefiles, CSVs, figures, metadata'],
    ['pdf', 'PDF report', 'Summary, maps, parcel schedule, calls, coordinates, QA'],
    ['geojson', 'GeoJSON', 'WGS 84, with attributes'],
    ['kml', 'KML', 'Google Earth'],
    ['shp', 'Shapefile (WGS 84)', 'Zipped .shp/.dbf/.prj'],
    ['shp-stateplane', 'Shapefile (state plane)', 'Zipped, NAD83 state-plane zone'],
    ['csv', 'Attribute table (CSV)', 'One row per parcel'],
    ['vertices-csv', 'Vertex coordinates (CSV)', 'Lat/lon, state plane, UTM']
  ];

  onMount(() => {
    documentId = currentDocument($page.url.searchParams.get('doc')) ?? '';
    try {
      editor = localStorage.getItem('roam-report-editor') ?? '';
    } catch {
      editor = '';
    }
    if (documentId) load();
  });

  async function load() {
    if (!documentId.trim()) return;
    loading = true;
    error = '';
    try {
      const res = await fetch(`${API_BASE}/documents/${documentId.trim()}/report`);
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? `HTTP ${res.status}`);
      report = await res.json();
      resetDrafts();
      figureStamp = Date.now();
    } catch (e: any) {
      error = e.message ?? String(e);
      report = null;
    } finally {
      loading = false;
    }
  }

  function resetDrafts() {
    projectDraft = Object.fromEntries(PROJECT_FIELDS.map(([k]) => [k, report.project[k] ?? '']));
    parcelDraft = Object.fromEntries(
      report.parcels.map((p: any) => [p.id, { label: p.label ?? '', apn: p.apn ?? '', review_status: p.review_status, notes: p.notes ?? '' }])
    );
  }

  $: dirty =
    !!report &&
    (PROJECT_FIELDS.some(([k]) => (projectDraft[k] ?? '') !== (report.project[k] ?? '')) ||
      report.parcels.some((p: any) =>
        ['label', 'apn', 'review_status', 'notes'].some((k) => (parcelDraft[p.id]?.[k] ?? '') !== (p[k] ?? ''))
      ));

  async function save() {
    saveState = 'saving';
    try {
      const project = Object.fromEntries(
        PROJECT_FIELDS.map(([k]) => [k, projectDraft[k]]).filter(([k, v]) => v !== (report.project[k] ?? ''))
      );
      const parcels: Record<string, Record<string, string>> = {};
      for (const p of report.parcels) {
        const changed = Object.fromEntries(
          Object.entries(parcelDraft[p.id]).filter(([k, v]) => v !== (p[k] ?? ''))
        );
        if (Object.keys(changed).length) parcels[p.id] = changed;
      }
      try {
        localStorage.setItem('roam-report-editor', editor);
      } catch {
        /* per-viewer convenience only */
      }
      const res = await fetch(`${API_BASE}/documents/${documentId.trim()}/report/edits`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project, parcels, editor: editor || null })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      report = await res.json();
      resetDrafts();
      figureStamp = Date.now();
      saveState = 'saved';
    } catch {
      saveState = 'error';
    }
  }

  function approveAll() {
    for (const p of report.parcels) parcelDraft[p.id].review_status = 'approved';
    parcelDraft = parcelDraft;
  }

  const exportHref = (fmt: string) => `${API_BASE}/documents/${documentId.trim()}/export/${fmt}`;
  const figure = (name: string) => `${API_BASE}/documents/${documentId.trim()}/report/figure/${name}?t=${figureStamp}`;
  const fmt = (v: number | null | undefined, d = 2) => (v == null ? '—' : v.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));
</script>

<div class="page">
  <header class="head">
    <div>
      <p class="kicker">Report</p>
      <h1>Final report &amp; export</h1>
      <p class="lede">
        Review the deliverable, correct names and IDs, record each parcel's review status, then export. Geometry is
        changed by re-confirming a boundary; everything here is logged.
      </p>
    </div>
  </header>

  <div class="loadbar">
    <input bind:value={documentId} placeholder="Document ID" on:keydown={(e) => e.key === 'Enter' && load()} />
    <button class="btn" on:click={load} disabled={loading}>{loading ? 'Loading…' : 'Load'}</button>
    {#if report}
      <a class="btn btn-ghost" href={`/workspace?doc=${encodeURIComponent(documentId.trim())}`}>← Back to map</a>
    {/if}
  </div>
  {#if error}<p class="error">{error}</p>{/if}

  {#if report}
    <section class="card exports">
      <div class="card-head">
        <h2>Export</h2>
        {#if dirty}<span class="warn">Unsaved edits — save first so the exports include them.</span>{/if}
      </div>
      <div class="export-grid">
        {#each EXPORTS as [fmtKey, label, sub]}
          {#if fmtKey !== 'shp-stateplane' || report.crs.state_plane}
            <a class="export" class:primary={fmtKey === 'zip'} class:disabled={dirty} href={dirty ? undefined : exportHref(fmtKey)} download>
              <strong>{label}</strong><span>{sub}</span>
            </a>
          {/if}
        {/each}
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>Project</h2>
        <div class="save">
          <input class="editor" bind:value={editor} placeholder="Your name (for the review log)" />
          <button class="btn primary" on:click={save} disabled={!dirty || saveState === 'saving'}>
            {saveState === 'saving' ? 'Saving…' : 'Save edits'}
          </button>
          {#if saveState === 'saved' && !dirty}<span class="ok">Saved ✓</span>{/if}
          {#if saveState === 'error'}<span class="error">Save failed</span>{/if}
        </div>
      </div>
      <div class="fields">
        {#each PROJECT_FIELDS as [key, label]}
          <label class:wide={key === 'title'}>
            <span>{label}</span>
            <input bind:value={projectDraft[key]} />
          </label>
        {/each}
      </div>
      <p class="meta">
        Source: {report.project.source_document} ({report.project.source_pages} pages) · Generated {report.generated_at.slice(0, 10)}
      </p>
    </section>

    <section class="stats">
      <div class="stat"><span>Parcels</span><strong>{report.summary.parcel_count}</strong></div>
      <div class="stat"><span>Location confirmed</span><strong>{report.summary.confirmed_locations} / {report.summary.parcel_count}</strong></div>
      <div class="stat"><span>Approved</span><strong>{report.summary.approved} / {report.summary.parcel_count}</strong></div>
      <div class="stat"><span>Total area</span><strong>{fmt(report.summary.total_area_acres, 3)} ac</strong>
        {#if report.summary.total_stated_acres}<em>stated {fmt(report.summary.total_stated_acres, 3)} ac</em>{/if}</div>
    </section>

    <section class="figures">
      <figure class="card">
        <img src={figure('location.png')} alt="Site location map" loading="lazy" />
        <figcaption>Figure 1 — Site location</figcaption>
      </figure>
      <figure class="card">
        <img src={figure('parcels.png')} alt="Parcels over imagery" loading="lazy" />
        <figcaption>Figure 2 — Parcels over imagery</figcaption>
      </figure>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>Parcel schedule</h2>
        <button class="btn btn-ghost btn-sm" on:click={approveAll}>Mark all approved</button>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Parcel</th><th>APN</th><th>Stated</th><th>Calculated</th><th>Diff</th><th>Placement</th><th>Location</th><th>Review</th><th>Notes</th><th></th>
            </tr>
          </thead>
          <tbody>
            {#each report.parcels as p (p.id)}
              <tr>
                <td><input bind:value={parcelDraft[p.id].label} /></td>
                <td><input bind:value={parcelDraft[p.id].apn} placeholder={p.parent_apn ? `parent ${p.parent_apn}` : ''} /></td>
                <td class="num">{p.stated_area_text ?? (p.stated_area_sqft ? `${fmt(p.stated_area_sqft, 0)} sq ft` : '—')}</td>
                <td class="num">{fmt(p.area_sqft, 0)} sq ft<br /><small>{fmt(p.area_acres, 4)} ac</small></td>
                <td class="num">{p.area_diff_pct == null ? '—' : `${fmt(p.area_diff_pct, 2)}%`}</td>
                <td>{p.placement_label}</td>
                <td><span class="pill" class:good={p.location_confirmed}>{p.location_confirmed ? 'Confirmed' : 'Approximate'}</span></td>
                <td>
                  <select bind:value={parcelDraft[p.id].review_status}>
                    {#each REVIEW as [v, l]}<option value={v}>{l}</option>{/each}
                  </select>
                </td>
                <td><input bind:value={parcelDraft[p.id].notes} placeholder="Reviewer note" /></td>
                <td><button class="btn btn-ghost btn-sm" on:click={() => (openParcel = openParcel === p.id ? null : p.id)}>{openParcel === p.id ? 'Hide' : 'Details'}</button></td>
              </tr>
              {#if openParcel === p.id}
                <tr class="detail">
                  <td colspan="10">
                    <div class="detail-grid">
                      <div>
                        <h3>Record calls</h3>
                        {#if p.calls.length}
                          <table class="mini">
                            <thead><tr><th>#</th><th>Bearing</th><th>Distance</th><th>Radius</th><th>Delta</th></tr></thead>
                            <tbody>
                              {#each p.calls as c}<tr><td>{c.course}</td><td>{c.bearing ?? ''}</td><td>{c.distance ?? ''}</td><td>{c.radius ?? ''}</td><td>{c.delta ?? ''}</td></tr>{/each}
                            </tbody>
                          </table>
                        {:else}<p class="muted">No calls were read for this parcel.</p>{/if}
                        <h3>Quality checks</h3>
                        <ul class="qa">
                          {#each p.qa as q}<li class:fail={!q.ok}>{q.ok ? '✓' : '!'} {q.check}</li>{/each}
                        </ul>
                      </div>
                      <div>
                        <h3>Vertex coordinates</h3>
                        <table class="mini">
                          <thead><tr><th>Pt</th><th>Lat</th><th>Lon</th>{#if report.crs.state_plane}<th>SP N</th><th>SP E</th>{/if}</tr></thead>
                          <tbody>
                            {#each p.vertices as v}
                              <tr><td>{v.point}</td><td>{v.lat.toFixed(7)}</td><td>{v.lon.toFixed(7)}</td>{#if report.crs.state_plane}<td>{v.sp_n.toFixed(2)}</td><td>{v.sp_e.toFixed(2)}</td>{/if}</tr>
                            {/each}
                          </tbody>
                        </table>
                        {#if p.placement_notes.length}
                          <h3>Placement notes</h3>
                          <ul class="notes">{#each p.placement_notes as n}<li>{n}</li>{/each}</ul>
                        {/if}
                      </div>
                    </div>
                  </td>
                </tr>
              {/if}
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <section class="card">
      <h2>Georeferencing</h2>
      <dl class="kv">
        <dt>Anchor</dt><dd>{report.location.anchor_method} — {report.location.anchor_source}</dd>
        {#if report.location.county_parcel_service}<dt>County records</dt><dd>{report.location.county_parcel_service}{report.location.parent_apn ? ` — subject APN ${report.location.parent_apn}` : ''}</dd>{/if}
        <dt>Coordinate systems</dt><dd>{report.crs.geographic}; {report.crs.utm}{report.crs.state_plane ? `; ${report.crs.state_plane}` : ''}</dd>
        <dt>Centre</dt><dd>{report.location.centre.lat}, {report.location.centre.lon}</dd>
      </dl>
      {#if report.location.neighbours.length}
        <h3>Adjoining parcels found in county records</h3>
        <div class="table-wrap">
          <table class="mini">
            <thead><tr><th>APN</th><th>Owner</th><th>Address</th></tr></thead>
            <tbody>{#each report.location.neighbours as n}<tr><td>{n.apn}</td><td>{n.owner ?? ''}</td><td>{n.address ?? ''}</td></tr>{/each}</tbody>
          </table>
        </div>
      {/if}
      <p class="statement">{report.accuracy_statement}</p>
    </section>

    {#if report.edit_log.length}
      <section class="card">
        <h2>Review log</h2>
        <div class="table-wrap">
          <table class="mini">
            <thead><tr><th>When (UTC)</th><th>By</th><th>Field</th><th>Value</th></tr></thead>
            <tbody>{#each [...report.edit_log].reverse() as e}<tr><td>{e.at.replace('T', ' ').slice(0, 19)}</td><td>{e.by ?? ''}</td><td>{e.field}</td><td>{e.value ?? ''}</td></tr>{/each}</tbody>
          </table>
        </div>
      </section>
    {/if}
  {/if}
</div>

<style>
  .page { display: flex; flex-direction: column; gap: 18px; }
  .kicker { color: var(--accent); font-size: 12px; letter-spacing: 0.12em; text-transform: uppercase; margin: 0; }
  h1 { margin: 4px 0 6px; font-size: 26px; }
  .lede { color: var(--muted); margin: 0; max-width: 760px; }
  .loadbar { display: flex; gap: 8px; flex-wrap: wrap; }
  .loadbar input { flex: 1 1 320px; padding: 9px 12px; border: 1px solid var(--line-soft); border-radius: 8px; background: var(--bg-elevated); color: inherit; }
  .card { background: var(--bg-elevated); border: 1px solid var(--line-soft); border-radius: 14px; padding: 18px 20px; }
  .card h2 { font-size: 16px; margin: 0 0 12px; }
  .card h3 { font-size: 13px; margin: 14px 0 6px; color: var(--muted); }
  .card-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
  .card-head h2 { margin: 0; }
  .btn { padding: 8px 14px; border-radius: 8px; border: 1px solid var(--line-soft); background: var(--bg-elevated); color: inherit; cursor: pointer; text-decoration: none; font: inherit; }
  .btn:disabled { opacity: 0.5; cursor: default; }
  .btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  .btn-ghost { background: transparent; }
  .btn-sm { padding: 5px 10px; font-size: 13px; }
  .export-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; }
  .export { display: flex; flex-direction: column; gap: 3px; padding: 12px 14px; border: 1px solid var(--line-soft); border-radius: 10px; text-decoration: none; color: inherit; }
  .export:hover { border-color: var(--accent); }
  .export span { font-size: 12px; color: var(--muted); }
  .export.primary { border-color: var(--accent); background: var(--accent-soft); }
  .export.disabled { opacity: 0.45; pointer-events: none; }
  .save { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .editor { padding: 7px 10px; border: 1px solid var(--line-soft); border-radius: 8px; background: transparent; color: inherit; min-width: 220px; }
  .fields { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px 14px; }
  .fields label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
  .fields label.wide { grid-column: 1 / -1; }
  .fields input, td input, td select { padding: 7px 9px; border: 1px solid var(--line-soft); border-radius: 7px; background: transparent; color: var(--fg, inherit); font: inherit; font-size: 13px; width: 100%; box-sizing: border-box; }
  .meta { font-size: 12px; color: var(--muted-dim); margin: 10px 0 0; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
  .stat { background: var(--bg-elevated); border: 1px solid var(--line-soft); border-radius: 14px; padding: 14px 16px; display: flex; flex-direction: column; gap: 4px; }
  .stat span { font-size: 12px; color: var(--muted); }
  .stat strong { font-size: 22px; }
  .stat em { font-size: 12px; color: var(--muted-dim); font-style: normal; }
  .figures { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }
  figure { margin: 0; padding: 10px; }
  figure img { width: 100%; border-radius: 8px; display: block; background: #222; aspect-ratio: 7 / 5; object-fit: cover; }
  figcaption { font-size: 12px; color: var(--muted); margin-top: 6px; }
  .table-wrap { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th { text-align: left; font-size: 12px; color: var(--muted); font-weight: 600; padding: 8px 6px; border-bottom: 1px solid var(--line-soft); white-space: nowrap; }
  td { padding: 7px 6px; border-bottom: 1px solid var(--line-soft); vertical-align: middle; }
  td.num { white-space: nowrap; }
  td small { color: var(--muted-dim); }
  .pill { font-size: 12px; padding: 3px 8px; border-radius: 99px; background: #fef3c7; color: #92400e; white-space: nowrap; }
  .pill.good { background: #dcfce7; color: #166534; }
  tr.detail td { background: var(--accent-soft); }
  .detail-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 18px; }
  table.mini { font-size: 12px; }
  table.mini td, table.mini th { padding: 4px 6px; }
  .qa, .notes { margin: 0; padding-left: 18px; font-size: 12px; }
  .qa li.fail { color: #b45309; }
  .kv { display: grid; grid-template-columns: 170px 1fr; gap: 6px 12px; font-size: 13px; margin: 0; }
  .kv dt { color: var(--muted); }
  .kv dd { margin: 0; }
  .statement { font-size: 12px; color: var(--muted); margin: 14px 0 0; }
  .muted { color: var(--muted); font-size: 12px; }
  .warn { color: #b45309; font-size: 13px; }
  .ok { color: #15803d; font-size: 13px; }
  .error { color: #b91c1c; font-size: 13px; }
  @media (max-width: 640px) {
    .kv { grid-template-columns: 1fr; }
  }
</style>
