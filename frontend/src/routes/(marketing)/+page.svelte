<script lang="ts">
  import AccountButton from '$lib/components/AccountButton.svelte';
  import MapCarousel from '$lib/components/MapCarousel.svelte';
  import HeroAnimation from '$lib/components/HeroAnimation.svelte';
  import { user } from '$lib/auth';

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
      <a href="/workspace" class="btn btn-primary">Try a sample document</a>
      {#if $user}
        <a href="/workspace" class="btn btn-ghost hero-own">Upload your own document</a>
      {:else}
        <AccountButton text="continue_with" />
      {/if}
    </div>
    <div class="hero-chips">
      {#each capabilities as c}
        <span class="chip">{c}</span>
      {/each}
    </div>
  </div>

  <div class="hero-side">
    <HeroAnimation />
  </div>

  <div class="hero-visual">
    <MapCarousel />
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
          Every page is rendered and run through layout detection: text blocks, tables,
          seals and parcel-map drawings are located and OCR'd independently, so a dense
          survey plat doesn't drown out the deed text around it.
        </p>
      </div>
      <div class="story-visual scan-visual">
        <img src="/images/roam-detect.jpg" alt="A real survey plat with ROAM's layout detection: the parcel map, text blocks and the surveyor's seal boxed and labelled, and the four parcels traced" />
      </div>
    </div>

    <div class="story-row reverse">
      <div class="story-copy">
        <span class="story-index">02</span>
        <h3>Confirm the boundaries</h3>
        <p>
          ROAM lists every parcel it finds on the sheet with its stated area. You trace or
          adjust each outline in a fast editor, with corners that snap to neighbouring
          parcels, and see its area and perimeter update live against the printed figure.
        </p>
      </div>
      <div class="story-visual shot-visual">
        <img
          src="/images/roam-boundary.jpg"
          alt="ROAM's boundary editor on the same plat: the four parcels listed with their stated areas, parcel AB-1 traced on the drawing, and its computed area and perimeter"
        />
      </div>
    </div>

    <div class="story-row">
      <div class="story-copy">
        <span class="story-index">03</span>
        <h3>Reconstruct &amp; georeference</h3>
        <p>
          ROAM works out the drawing's scale and orientation, then places the parcels on the
          real map from the strongest evidence printed on the sheet. This plat carries a
          state-plane coordinate, and the placement is confirmed against the county's own
          parcel records.
        </p>
      </div>
      <div class="story-visual map-visual">
        <img
          src="/images/roam-placed.jpg"
          alt="The same four parcels placed on satellite imagery in Washoe County, Nevada, each in its own colour"
        />
        <span class="scan-tag">SATELLITE &middot; WASHOE COUNTY, NV &middot; PARCELS AB-1 TO AB-4</span>
      </div>
    </div>

    <div class="story-row reverse">
      <div class="story-copy">
        <span class="story-index">04</span>
        <h3>Validate and export</h3>
        <p>
          Every parcel is checked against the document's own stated area and the county's
          parcel records, and gets an honest status. The report collects the maps, the
          parcel schedule and the evidence, ready to export as PDF, GeoJSON, KML, shapefiles
          or CSV. This is the real result for the same plat, not a staged one.
        </p>
      </div>
      <div class="story-visual shot-visual">
        <img
          src="/images/roam-report.jpg"
          alt="ROAM's report for the same four parcels: all four locations confirmed, the site and parcel maps, and the parcel schedule comparing stated and calculated areas"
        />
      </div>
    </div>
  </div>
</section>

<section id="evidence" class="gallery">
  <div class="section-inner">
    <p class="kicker">Built for real documents</p>
    <h2>Historical, hand-drawn, or modern, the boundary calls are the same math.</h2>
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
      <p>Extract cadastral geometry with a documented, inspectable pipeline. No manual digitizing.</p>
      <a href="/workspace" class="btn btn-primary">Open Workspace →</a>
    </div>
  </div>
</section>

<style>
.hero {
  padding: 56px 28px 0;
  max-width: 1240px;
  margin: 0 auto;
  display: grid;
  grid-template-columns: minmax(0, 640px) minmax(0, 1fr);
  column-gap: 48px;
}

.hero-side {
  align-self: start;
  margin: 0 0 40px;
}

@media (max-width: 960px) {
  .hero {
    grid-template-columns: minmax(0, 1fr);
  }
  .hero-side {
    display: none;
  }
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
  grid-column: 1 / -1;
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
  /* No max-width here on purpose: the grid column itself is already
     the right reading width. A ch-based cap left unused space at
     whichever edge of the column faced away from the text's natural
     start, and since story-copy sits on the LEFT of the gap in rows
     1/3 but the RIGHT of the gap in rows 2/4 (reverse), that unused
     space landed on opposite sides -- making the visual gap to the
     image look bigger in one pair than the other for the same 56px
     grid gap. Filling the column removes the asymmetry. */
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

/* real app screenshots (steps 02 and 04): shown whole, at their own height */
.story-visual.shot-visual {
  height: auto;
  background: #f7f6f3;
}
.shot-visual img {
  display: block;
  width: 100%;
  height: auto;
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
  object-position: 50% 50%;
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
  top: 14px; /* the map's own scale bar sits bottom-left */
  font-family: var(--mono);
  font-size: 0.64rem;
  letter-spacing: 0.05em;
  color: #fff;
  background: rgba(0, 0, 0, 0.55);
  padding: 5px 9px;
  border-radius: 999px;
  backdrop-filter: blur(4px);
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
    url('/images/earth/vienna-detail.jpg');
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
  .hero-own {
    padding-block: 10px; /* the 1 px outline: same 44 px height as the filled button */
    background: var(--surface, #fff);
  }
  .hero-actions {
    align-items: center;
  }
</style>
