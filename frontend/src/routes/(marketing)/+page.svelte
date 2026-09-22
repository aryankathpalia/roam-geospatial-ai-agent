<script lang="ts">
  const stages = [
    {
      step: '01',
      title: 'Understand',
      body: 'Every page is rendered and run through layout detection — text blocks, tables, seals and parcel-map drawings are located and OCR’d independently.'
    },
    {
      step: '02',
      title: 'Extract',
      body: 'Parcel-map regions are escalated to a vision model that reads bearing and distance calls, tie points and basis-of-bearings straight off the drawing.'
    },
    {
      step: '03',
      title: 'Reconstruct',
      body: 'The boundary traverse is walked corner-to-corner into real geometry, then projected onto true WGS84 coordinates using a geocoded anchor from the document itself.'
    },
    {
      step: '04',
      title: 'Validate',
      body: 'Closure precision, self-intersection and the document’s own stated acreage are cross-checked — every parcel ships with an honest confidence signal, not a guess.'
    }
  ];

  const capabilities = [
    'Any quadrant bearing format',
    'Closure-based QA, not a black box',
    'Free-text address geocoding, no fixed zone',
    'GeoJSON out, ready for any GIS stack'
  ];
</script>

<section class="hero">
  <div class="hero-inner">
    <div class="hero-copy fade-up">
      <p class="kicker">ROAM &middot; Reasoning-Oriented Agent for Maps</p>
      <h1>Turn scanned maps into<br /><span class="accent">verified spatial data.</span></h1>
      <p class="lede">
        Upload a scanned deed, survey plat or legacy cadastral map. ROAM reads the boundary
        calls, reconstructs the parcel geometry, places it on the real map, and tells you exactly
        how well it closes.
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

    <div class="hero-visual fade-up">
      <div class="scan-frame">
        <img src="/images/hero-survey.jpg" alt="1881 survey map of property in the Twelfth Ward of the City of New York, with parcel boundaries outlined" />
        <span class="scan-tag">SOURCE SCAN &middot; 1881 SURVEY PLAT</span>
      </div>

      <div class="result-card">
        <div class="result-head">
          <span class="status-dot"></span>
          <span>PARCEL EXTRACTED &middot; LIVE PIPELINE RUN</span>
        </div>
        <dl>
          <div><dt>Closure precision</dt><dd class="mono">1:2.8k</dd></div>
          <div><dt>Boundary closure</dt><dd class="mono">325.3 ft</dd></div>
          <div><dt>Output CRS</dt><dd class="mono">WGS84</dd></div>
        </dl>
        <span class="result-note">from an actual ROAM run — see /workspace</span>
      </div>
    </div>
  </div>
</section>

<section id="pipeline" class="pipeline">
  <div class="section-inner">
    <p class="kicker">The pipeline</p>
    <h2>Four real stages. No manual tracing.</h2>
    <div class="stage-grid">
      {#each stages as s}
        <div class="stage-card">
          <span class="stage-num mono">{s.step}</span>
          <h3>{s.title}</h3>
          <p>{s.body}</p>
        </div>
      {/each}
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
  padding: 64px 28px 40px;
}

.hero-inner {
  max-width: 1240px;
  margin: 0 auto;
  display: grid;
  grid-template-columns: 1.05fr 0.95fr;
  gap: 56px;
  align-items: center;
}

.kicker {
  margin: 0 0 14px;
  text-transform: uppercase;
  font-size: 0.72rem;
  letter-spacing: 0.14em;
  font-weight: 700;
  color: var(--brand);
}

h1 {
  margin: 0 0 20px;
  font-size: clamp(2.1rem, 4vw, 3.1rem);
  line-height: 1.08;
  color: var(--text);
}

.accent {
  background: linear-gradient(120deg, var(--brand), #7be8c8);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}

.lede {
  margin: 0 0 28px;
  color: var(--muted);
  font-size: 1.02rem;
  line-height: 1.6;
  max-width: 46ch;
}

.hero-actions {
  display: flex;
  gap: 14px;
  margin-bottom: 30px;
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
  position: relative;
}

.scan-frame {
  position: relative;
  border-radius: 18px;
  overflow: hidden;
  border: 1px solid var(--line);
  box-shadow: var(--shadow-float);
}

.scan-frame img {
  display: block;
  width: 100%;
  height: 380px;
  object-fit: cover;
  filter: saturate(0.92) contrast(1.02);
}

.scan-tag {
  position: absolute;
  left: 14px;
  bottom: 14px;
  font-family: var(--mono);
  font-size: 0.66rem;
  letter-spacing: 0.06em;
  color: #cfe9df;
  background: rgba(8, 11, 10, 0.72);
  border: 1px solid rgba(255, 255, 255, 0.14);
  padding: 5px 9px;
  border-radius: 999px;
  backdrop-filter: blur(6px);
}

.result-card {
  position: absolute;
  right: -18px;
  bottom: -34px;
  width: 262px;
  background: var(--surface-strong);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 16px 18px;
  box-shadow: var(--shadow-float);
}

.result-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.64rem;
  letter-spacing: 0.06em;
  color: var(--brand);
  font-weight: 700;
  margin-bottom: 12px;
}

.result-card dl {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.result-card dl > div {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}

.result-card dt {
  font-size: 0.78rem;
  color: var(--muted);
}

.result-card dd {
  margin: 0;
  font-size: 0.86rem;
  color: var(--text);
  font-weight: 600;
}

.result-note {
  display: block;
  margin-top: 12px;
  font-size: 0.68rem;
  color: var(--muted-dim);
}

.pipeline,
.gallery {
  padding: 76px 28px;
  border-top: 1px solid var(--line-soft);
}

.section-inner {
  max-width: 1240px;
  margin: 0 auto;
}

.pipeline h2,
.gallery h2 {
  margin: 0 0 40px;
  font-size: clamp(1.5rem, 2.4vw, 2rem);
  max-width: 22ch;
}

.stage-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 18px;
}

.stage-card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 22px 20px;
}

.stage-num {
  color: var(--brand);
  font-size: 0.78rem;
  font-weight: 600;
}

.stage-card h3 {
  margin: 10px 0 8px;
  font-size: 1.05rem;
}

.stage-card p {
  margin: 0;
  color: var(--muted);
  font-size: 0.87rem;
  line-height: 1.55;
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
  filter: saturate(0.9);
  transition: filter 0.2s ease, transform 0.2s ease;
}

.gallery-grid img:hover {
  filter: saturate(1.05);
  transform: translateY(-2px);
}

.gallery-caption {
  margin: 16px 2px 0;
  font-size: 0.76rem;
  color: var(--muted-dim);
}

.cta {
  padding: 30px 28px 90px;
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

@media (max-width: 960px) {
  .hero-inner {
    grid-template-columns: 1fr;
  }
  .stage-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  .gallery-grid {
    grid-template-columns: 1fr;
  }
  .result-card {
    position: static;
    width: auto;
    margin-top: 16px;
  }
}
</style>
