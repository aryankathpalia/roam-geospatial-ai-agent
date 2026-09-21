<section class="landing-handoff panel p-8 md:p-10">
  <p class="kicker">ROAM</p>
  <h1>ROAM — coming soon</h1>
  <p>
    Reasoning-Oriented Agent for Maps — a geospatial document intelligence
    and spatial validation agent. The document upload UI and agent loop are
    not wired up in this skeleton yet.
  </p>
</section>

<style>
.landing-handoff {
  min-height: 52vh;
  display: grid;
  place-content: center;
  text-align: center;
  gap: 10px;
  background:
    linear-gradient(125deg, rgba(14, 164, 107, 0.16), rgba(32, 149, 232, 0.12)),
    rgba(255, 255, 255, 0.7);
}

.kicker {
  margin: 0;
  text-transform: uppercase;
  font-size: 0.74rem;
  letter-spacing: 0.1em;
  font-weight: 700;
  color: #0d6f4a;
}

h1 {
  margin: 0;
  font-size: clamp(1.6rem, 2.4vw, 2.7rem);
}

p {
  margin: 0;
  color: #5a6d75;
}
</style>
