<script lang="ts">
  import { onMount } from 'svelte';

  let sample: any = null;
  let region: any = null;
  let previewPath = '';
  let previewPoints: [number, number][] = [];

  // Mirrors app/services/geometry.py's quadrant-bearing-to-azimuth and
  // traverse-walking logic in JS, so the small polygon sketch next to
  // the boundary_calls table is drawn from the SAME real data, not a
  // decorative stand-in shape.
  function bearingToAzimuth(bearing: string): number | null {
    const m = bearing
      .replace('�', '°')
      .match(/([NSns])\s*(\d+(?:\.\d+)?)\s*[°*ov]?\s*(?:(\d+(?:\.\d+)?)['′]?\s*)?(?:(\d+(?:\.\d+)?)["″]?\s*)?([EWew])/);
    if (!m) return null;
    const angle = parseFloat(m[2]) + (parseFloat(m[3]) || 0) / 60 + (parseFloat(m[4]) || 0) / 3600;
    const ns = m[1].toUpperCase();
    const ew = m[5].toUpperCase();
    if (ns === 'N' && ew === 'E') return angle;
    if (ns === 'S' && ew === 'E') return 180 - angle;
    if (ns === 'S' && ew === 'W') return 180 + angle;
    if (ns === 'N' && ew === 'W') return 360 - angle;
    return null;
  }

  function walkTraverse(calls: { bearing: string; distance: string }[]): [number, number][] {
    let x = 0;
    let y = 0;
    const pts: [number, number][] = [[0, 0]];
    for (const call of calls) {
      const az = bearingToAzimuth(call.bearing);
      const dist = parseFloat((call.distance || '').replace(/[^\d.]/g, ''));
      if (az === null || isNaN(dist)) continue;
      const rad = (az * Math.PI) / 180;
      x += dist * Math.sin(rad);
      y += dist * Math.cos(rad);
      pts.push([x, y]);
    }
    return pts;
  }

  function pointsToSvgPath(pts: [number, number][], size = 160, pad = 22): string {
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const spanX = maxX - minX || 1;
    const spanY = maxY - minY || 1;
    const scale = Math.min((size - pad * 2) / spanX, (size - pad * 2) / spanY);
    return pts
      .map(([x, y], i) => {
        const sx = pad + (x - minX) * scale;
        const sy = size - (pad + (y - minY) * scale); // flip: north (+y) goes up on screen
        return `${i === 0 ? 'M' : 'L'}${sx.toFixed(1)},${sy.toFixed(1)}`;
      })
      .join(' ');
  }

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
    if (region) {
      previewPoints = walkTraverse(region.vision_geometry.boundary_calls);
      previewPath = pointsToSvgPath(previewPoints);
    }
  });

  const capabilities = [
    'Any quadrant bearing format',
    'Closure-based QA, not a black box',
    'Free-text address geocoding',
    'GeoJSON out, ready for any GIS stack'
  ];

  // A conservative gauge scale for the precision-ratio bar -- see
  // app/services/spatial_validation.py's own _MIN_ACCEPTABLE_PRECISION_RATIO
  // comment for why 1:2000 is used as a generic, loose minimum rather
  // than a claimed regulatory threshold.
  const GAUGE_MAX = 2500;
  const GAUGE_THRESHOLD = 2000;
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
        <div class="detect-box" style="left:50.5%; top:58.8%; width:21%; height:31.7%;">
          <span>ParcelMap</span>
        </div>
        <div class="detect-box" style="left:10.5%; top:9.5%; width:25%; height:14%;">
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
          <div class="calls-body">
            <div class="calls-table">
              {#if region}
                {#each region.vision_geometry.boundary_calls as call, i}
                  <div class="call-row mono">
                    <span class="call-vertex">{i + 1}</span>
                    <span class="call-bearing">{call.bearing.replace('�', '°')}</span>
                    <span class="muted call-distance">{call.distance}</span>
                  </div>
                {/each}
              {:else}
                <div class="calls-loading skeleton" style="height:200px"></div>
              {/if}
            </div>
            <div class="calls-sketch">
              {#if previewPath}
                <svg viewBox="0 0 160 160" width="140" height="140">
                  <path
                    d={previewPath}
                    fill="rgba(217,88,31,0.12)"
                    stroke="#d9581f"
                    stroke-width="2"
                    stroke-linejoin="round"
                  />
                  {#each previewPoints as p, i}
                    {@const xs = previewPoints.map((pt) => pt[0])}
                    {@const ys = previewPoints.map((pt) => pt[1])}
                    {@const minX = Math.min(...xs)}
                    {@const maxX = Math.max(...xs)}
                    {@const minY = Math.min(...ys)}
                    {@const maxY = Math.max(...ys)}
                    {@const scale = Math.min(116 / (maxX - minX || 1), 116 / (maxY - minY || 1))}
                    {@const sx = 22 + (p[0] - minX) * scale}
                    {@const sy = 160 - (22 + (p[1] - minY) * scale)}
                    {#if i < previewPoints.length - 1 || previewPoints.length === 1}
                      <circle cx={sx} cy={sy} r="2.6" fill="#d9581f" />
                    {/if}
                  {/each}
                </svg>
                <span class="calls-sketch-label">walked traverse</span>
              {:else}
                <div class="skeleton" style="width:140px;height:140px;border-radius:8px"></div>
              {/if}
            </div>
          </div>
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
        <img
          src="/images/earth/khorinsky-russia.jpg"
          alt="Satellite imagery of river valleys, ridgelines and scattered clouds over the Khorinsky district, Russia"
        />
        <span class="scan-tag">SATELLITE &middot; KHORINSKY DISTRICT</span>
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
      <div class="story-visual earth-wash">
        <div class="validate-panel panel">
          {#if region}
            {@const v = region.spatial_validation}
            <div class="validate-head">
              <span class="pill {v.valid ? 'ok' : 'high'}">{v.valid ? 'Valid' : 'Needs review'}</span>
              <span class="mono small muted">{region.vision_geometry.parcel_label}</span>
            </div>

            <div class="gauge">
              <div class="gauge-row">
                <span class="gauge-label">Closure precision</span>
                <span class="mono gauge-value">{v.precision_ratio ? `1:${v.precision_ratio}` : '—'}</span>
              </div>
              <div class="gauge-track">
                <div
                  class="gauge-fill"
                  class:fail={!v.valid}
                  style="width:{Math.min(100, ((v.precision_ratio || 0) / GAUGE_MAX) * 100)}%"
                ></div>
                <div class="gauge-threshold" style="left:{(GAUGE_THRESHOLD / GAUGE_MAX) * 100}%"></div>
              </div>
              <div class="gauge-scale">
                <span>1:0</span>
                <span>1:{GAUGE_THRESHOLD} min</span>
              </div>
            </div>

            <dl class="validate-grid">
              <div><dt>Closure</dt><dd class="mono">{region.boundary_geojson_wgs84.properties.closure_error_ft} ft</dd></div>
              <div><dt>Perimeter</dt><dd class="mono">{v.perimeter_ft} ft</dd></div>
              <div><dt>Area</dt><dd class="mono">{v.area_acres ? `${v.area_acres} ac` : '—'}</dd></div>
            </dl>

            {#if v.issues?.length}
              <ul class="validate-issues">
                {#each v.issues as issue}
                  <li>{issue}</li>
                {/each}
              </ul>
            {/if}
          {:else}
            <div class="skeleton" style="height:200px"></div>
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
  <div class="section-inner cta-frame">
    <div class="cta-box panel-strong">
      <h2>From paper deed to living GIS layer.</h2>
      <p>Extract cadastral geometry with a documented, inspectable pipeline — no manual digitizing.</p>
      <a href="/workspace" class="btn btn-primary">Open Workspace →</a>
    </div>
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
  height: 320px;
  position: relative;
  display: flex;
  align-items: stretch;
}

.scan-visual img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
  /* Keeps the two annotated targets (title text ~13.5-23%, the lot-grid
     block ~47-68.5% of the source image) inside the visible crop window
     at this box's aspect ratio -- recompute this if the image or the
     detect-box positions below change. */
  object-position: 50% 22%;
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
  position: relative;
}

.map-visual img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.map-visual .scan-tag {
  position: absolute;
  left: 14px;
  bottom: 14px;
  font-family: var(--mono);
  font-size: 0.64rem;
  letter-spacing: 0.05em;
  color: #fff;
  background: rgba(0, 0, 0, 0.55);
  padding: 5px 9px;
  border-radius: 999px;
  backdrop-filter: blur(4px);
}

.calls-panel,
.validate-panel {
  padding: 20px 22px;
  height: 100%;
  width: 100%;
  overflow-y: auto;
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

.calls-body {
  display: flex;
  gap: 20px;
  align-items: center;
}

.calls-table {
  flex: 1;
  min-width: 0;
}

.call-row {
  display: grid;
  grid-template-columns: 20px 1fr auto;
  align-items: center;
  gap: 10px;
  padding: 7px 0;
  font-size: 0.84rem;
  border-bottom: 1px solid var(--line-soft);
}

.call-row:last-child {
  border-bottom: none;
}

.call-vertex {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--surface-muted);
  color: var(--muted);
  font-size: 0.66rem;
  display: grid;
  place-items: center;
}

.call-distance {
  text-align: right;
}

.muted {
  color: var(--muted);
}

.calls-sketch {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding-left: 20px;
  border-left: 1px solid var(--line-soft);
}

.calls-sketch-label {
  font-size: 0.62rem;
  color: var(--muted-dim);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.validate-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 18px;
}

.gauge {
  margin-bottom: 18px;
}

.gauge-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 7px;
}

.gauge-label {
  font-size: 0.78rem;
  color: var(--muted);
}

.gauge-value {
  font-size: 0.92rem;
  font-weight: 700;
}

.gauge-track {
  position: relative;
  height: 8px;
  border-radius: 999px;
  background: var(--surface-muted);
  overflow: visible;
}

.gauge-fill {
  height: 100%;
  border-radius: 999px;
  background: var(--ok);
  min-width: 3px;
}

.gauge-fill.fail {
  background: var(--danger);
}

.gauge-threshold {
  position: absolute;
  top: -3px;
  width: 2px;
  height: 14px;
  background: var(--ink);
  opacity: 0.35;
}

.gauge-scale {
  display: flex;
  justify-content: space-between;
  margin-top: 6px;
  font-size: 0.66rem;
  color: var(--muted-dim);
}

.validate-grid {
  margin: 0 0 14px;
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
  padding-top: 14px;
  border-top: 1px solid var(--line-soft);
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

.validate-issues {
  margin: 0;
  padding: 14px 0 0 18px;
  border-top: 1px solid var(--line-soft);
  font-size: 0.8rem;
  color: var(--muted);
  line-height: 1.5;
}

.validate-issues li + li {
  margin-top: 6px;
}

.earth-wash {
  background-image: linear-gradient(rgba(247, 246, 243, 0.88), rgba(247, 246, 243, 0.88)), url('/images/earth/amazon-river.jpg');
  background-size: cover;
  background-position: center;
  display: flex;
  align-items: stretch;
  padding: 14px;
  width: 100%;
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

.cta-frame {
  border-radius: 18px;
  overflow: hidden;
  border: 1px solid var(--line);
  padding: 64px 28px;
  display: flex;
  justify-content: center;
  background-image: linear-gradient(rgba(8, 10, 8, 0.42), rgba(8, 10, 8, 0.5)),
    url('/images/earth/holla-bend-arkansas.jpg');
  background-size: cover;
  background-position: center;
}

.cta-box {
  padding: 40px 44px;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  max-width: 480px;
  background: var(--surface);
}

.cta-box h2 {
  margin: 0;
  font-size: clamp(1.4rem, 2.2vw, 1.8rem);
}

.cta-box p {
  margin: 0 0 10px;
  color: var(--muted);
  max-width: 42ch;
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
