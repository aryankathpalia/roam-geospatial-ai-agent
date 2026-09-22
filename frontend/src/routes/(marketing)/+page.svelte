<script lang="ts">
  import { onMount } from 'svelte';
  import ResultMap from '$lib/components/ResultMap.svelte';

  let sample: any = null;
  let region: any = null;

  onMount(async () => {
    const res = await fetch('/sample-data/sample-result.json');
    sample = await res.json();
    // Page 9's "Parcel 2" -- a real, multi-sided extracted boundary from
    // an actual live ROAM run, used throughout this page instead of any
    // placeholder numbers.
    for (const page of sample.pages) {
      const found = page.regions.find((r: any) => r.boundary_geojson_wgs84);
      if (found) {
        region = found;
        break;
      }
    }
  });

  const capabilities = [
    'Any quadrant bearing format',
    'Closure-based QA, not a black box',
    'Free-text address geocoding',
    'GeoJSON out, ready for any GIS stack'
  ];
</script>

<section class="hero">
  <div class="hero-inner">
    <p class="kicker">ROAM &middot; Reasoning-Oriented Agent for Maps</p>
    <h1>Turn scanned maps into<br />verified spatial data.</h1>
    <p class="lede">
      Upload a scanned deed, survey plat or legacy cadastral map. ROAM reads the boundary
      calls, reconstructs the parcel geometry, places it on the real map, and tells you
      exactly how well it closes.
    </p>
    <div class="hero-actions">
      <a href="/workspace" class="btn btn-primary">Open Workspace</a>
      <a href="#pipeline" class="btn btn-ghost">See how it works</a>
    </div>
    <div class="hero-chips">
      {#each capabilities as c}
        <span class="chip">{c}</span>
      {/each}
    </div>
  </div>

  <div class="hero-visual">
    <img src="/images/hero-survey.jpg" alt="1903 plate map of Portland, Maine, showing colored ward and district boundaries" />
  </div>
</section>

<section id="pipeline" class="story">
  <div class="section-inner">
    <p class="kicker">The pipeline</p>
    <h2>From a flat scan to a validated boundary.</h2>

    <div class="story-row">
      <div class="story-copy">
        <span class="story-index">01</span>
        <h3>Read any scan</h3>
        <p>
          Every page is rendered and run through layout detection — text blocks, tables,
          seals and parcel-map drawings are located and OCR'd independently, so a dense
          survey plat doesn't drown out the deed text around it.
        </p>
      </div>
      <div class="story-visual scan-visual">
        <img src="/images/gallery-baist-dc.jpg" alt="Detected regions on a scanned real-estate atlas plate" />
        <div class="detect-box" style="left:8%; top:38%; width:26%; height:20%;">
          <span>ParcelMap</span>
        </div>
        <div class="detect-box" style="left:58%; top:10%; width:20%; height:12%;">
          <span>Text</span>
        </div>
      </div>
    </div>

    <div class="story-row reverse">
      <div class="story-copy">
        <span class="story-index">02</span>
        <h3>Extract the boundary calls</h3>
        <p>
          A vision model reads bearing-and-distance calls, tie points and basis-of-bearings
          straight off the drawing — filtering out reference citations and interior
          dimensions that aren't part of the actual boundary.
        </p>
      </div>
      <div class="story-visual">
        <div class="calls-panel panel">
          <div class="calls-head">
            <span class="dot-label">boundary_calls</span>
            {#if region}<span class="mono small">{region.vision_geometry.parcel_label}</span>{/if}
          </div>
          {#if region}
            {#each region.vision_geometry.boundary_calls.slice(0, 5) as call}
              <div class="call-row mono">
                <span>{call.bearing.replace('�', '°')}</span>
                <span class="muted">{call.distance}</span>
              </div>
            {/each}
          {:else}
            <div class="calls-loading skeleton" style="height:120px"></div>
          {/if}
        </div>
      </div>
    </div>

    <div class="story-row">
      <div class="story-copy">
        <span class="story-index">03</span>
        <h3>Reconstruct &amp; georeference</h3>
        <p>
          The traverse is walked corner-to-corner into real geometry, then projected onto
          true WGS84 coordinates using an address geocoded straight out of the document —
          no fixed coordinate zone assumed.
        </p>
      </div>
      <div class="story-visual map-visual">
        {#if region}
          <ResultMap geojson={region.boundary_geojson_wgs84} color="#d9581f" />
        {:else}
          <div class="skeleton" style="height:100%"></div>
        {/if}
      </div>
    </div>

    <div class="story-row reverse">
      <div class="story-copy">
        <span class="story-index">04</span>
        <h3>Validate automatically</h3>
        <p>
          Closure precision, self-intersection and the document's own stated acreage are
          cross-checked. Every parcel ships with an honest signal — including when it
          doesn't pass, like the real example on the right.
        </p>
      </div>
      <div class="story-visual">
        <div class="validate-panel panel">
          {#if region}
            {@const v = region.spatial_validation}
            <div class="validate-head">
              <span class="pill {v.valid ? 'ok' : 'high'}">{v.valid ? 'Valid' : 'Needs review'}</span>
            </div>
            <dl class="validate-grid">
              <div><dt>Precision</dt><dd class="mono">{v.precision_ratio ? `1:${v.precision_ratio}` : '—'}</dd></div>
              <div><dt>Closure</dt><dd class="mono">{region.boundary_geojson_wgs84.properties.closure_error_ft} ft</dd></div>
              <div><dt>Area</dt><dd class="mono">{v.area_acres ? `${v.area_acres} ac` : '—'}</dd></div>
            </dl>
            {#if v.issues?.length}
              <p class="validate-issue">{v.issues[0]}</p>
            {/if}
          {:else}
            <div class="skeleton" style="height:120px"></div>
          {/if}
        </div>
      </div>
    </div>
  </div>
</section>

<section id="evidence" class="gallery">
  <div class="section-inner">
    <p class="kicker">Built for real documents</p>
    <h2>Historical, hand-drawn, or modern — the boundary calls are the same math.</h2>
    <div class="gallery-grid">
      <img src="/images/gallery-baist-dc.jpg" alt="1907 Baist's Real Estate Atlas of Washington, D.C." />
      <img src="/images/gallery-stapleton.jpg" alt="Historical atlas map of Stapleton, Staten Island" />
      <img src="/images/gallery-3.jpg" alt="Historical cadastral survey map" />
    </div>
    <p class="gallery-caption">Maps: David Rumsey Map Collection, used for reference/testing.</p>
  </div>
</section>

<section id="technology" class="cta">
  <div class="section-inner cta-inner panel-strong">
    <h2>From paper deed to living GIS layer.</h2>
    <p>Extract cadastral geometry with a documented, inspectable pipeline — no manual digitizing.</p>
    <a href="/workspace" class="btn btn-primary">Open Workspace →</a>
  </div>
</section>

<style>
.hero {
  padding: 56px 28px 0;
  max-width: 1240px;
  margin: 0 auto;
}

.hero-inner {
  max-width: 640px;
  margin-bottom: 40px;
}

.kicker {
  margin: 0 0 14px;
  text-transform: uppercase;
  font-size: 0.72rem;
  letter-spacing: 0.12em;
  font-weight: 700;
  color: var(--accent);
}

h1 {
  margin: 0 0 20px;
  font-size: clamp(2.1rem, 4vw, 3.1rem);
  line-height: 1.08;
}

.lede {
  margin: 0 0 28px;
  color: var(--muted);
  font-size: 1.02rem;
  line-height: 1.6;
  max-width: 52ch;
}

.hero-actions {
  display: flex;
  gap: 14px;
  margin-bottom: 26px;
}

.hero-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 9px;
}

.chip {
  font-size: 0.76rem;
  color: var(--muted);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 5px 11px;
}

.hero-visual {
  border-radius: 16px;
  overflow: hidden;
  border: 1px solid var(--line);
  box-shadow: var(--shadow-float);
}

.hero-visual img {
  display: block;
  width: 100%;
  height: clamp(280px, 44vw, 460px);
  object-fit: cover;
  object-position: 20% 30%;
}

.story {
  padding: 90px 28px;
  border-top: 1px solid var(--line-soft);
}

.section-inner {
  max-width: 1240px;
  margin: 0 auto;
}

.story h2 {
  margin: 0 0 64px;
  font-size: clamp(1.5rem, 2.4vw, 2rem);
  max-width: 26ch;
}

.story-row {
  display: grid;
  grid-template-columns: 0.85fr 1.15fr;
  gap: 56px;
  align-items: center;
  margin-bottom: 76px;
}

.story-row:last-child {
  margin-bottom: 0;
}

.story-row.reverse {
  grid-template-columns: 1.15fr 0.85fr;
}

.story-row.reverse .story-copy {
  order: 2;
}

.story-index {
  font-family: var(--mono);
  color: var(--accent);
  font-size: 0.8rem;
  font-weight: 600;
}

.story-copy h3 {
  margin: 10px 0 12px;
  font-size: 1.4rem;
}

.story-copy p {
  margin: 0;
  color: var(--muted);
  line-height: 1.65;
  max-width: 42ch;
}

.story-visual {
  border-radius: 14px;
  border: 1px solid var(--line);
  overflow: hidden;
  background: var(--surface);
  box-shadow: var(--shadow-soft);
  min-height: 260px;
  position: relative;
}

.scan-visual img {
  display: block;
  width: 100%;
  height: 260px;
  object-fit: cover;
}

.detect-box {
  position: absolute;
  border: 1.5px solid var(--accent);
  background: rgba(217, 88, 31, 0.08);
  border-radius: 3px;
}

.detect-box span {
  position: absolute;
  top: -20px;
  left: -1.5px;
  font-family: var(--mono);
  font-size: 0.62rem;
  background: var(--accent);
  color: #fff;
  padding: 2px 6px;
  border-radius: 3px;
  white-space: nowrap;
}

.map-visual {
  height: 300px;
}

.calls-panel,
.validate-panel {
  padding: 20px 22px;
  height: 100%;
}

.calls-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--line-soft);
}

.dot-label {
  font-size: 0.72rem;
  color: var(--muted-dim);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.small {
  font-size: 0.78rem;
}

.call-row {
  display: flex;
  justify-content: space-between;
  padding: 8px 0;
  font-size: 0.86rem;
  border-bottom: 1px solid var(--line-soft);
}

.call-row:last-child {
  border-bottom: none;
}

.muted {
  color: var(--muted);
}

.validate-head {
  margin-bottom: 16px;
}

.validate-grid {
  margin: 0 0 14px;
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}

.validate-grid dt {
  font-size: 0.68rem;
  color: var(--muted-dim);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.validate-grid dd {
  margin: 3px 0 0;
  font-size: 0.92rem;
  font-weight: 600;
}

.validate-issue {
  margin: 0;
  padding-top: 14px;
  border-top: 1px solid var(--line-soft);
  font-size: 0.82rem;
  color: var(--muted);
  line-height: 1.5;
}

.gallery {
  padding: 90px 28px;
  border-top: 1px solid var(--line-soft);
}

.gallery h2 {
  margin: 0 0 40px;
  font-size: clamp(1.5rem, 2.4vw, 2rem);
  max-width: 22ch;
}

.gallery-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
}

.gallery-grid img {
  width: 100%;
  height: 220px;
  object-fit: cover;
  border-radius: 12px;
  border: 1px solid var(--line);
  transition: transform 0.2s ease;
}

.gallery-grid img:hover {
  transform: translateY(-2px);
}

.gallery-caption {
  margin: 16px 2px 0;
  font-size: 0.76rem;
  color: var(--muted-dim);
}

.cta {
  padding: 30px 28px 100px;
}

.cta-inner {
  padding: 56px 40px;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
}

.cta-inner h2 {
  margin: 0;
  font-size: clamp(1.5rem, 2.6vw, 2.1rem);
}

.cta-inner p {
  margin: 0 0 10px;
  color: var(--muted);
  max-width: 46ch;
}

@media (max-width: 900px) {
  .story-row,
  .story-row.reverse {
    grid-template-columns: 1fr;
  }
  .story-row.reverse .story-copy {
    order: 0;
  }
  .gallery-grid {
    grid-template-columns: 1fr;
  }
}
</style>
