<script lang="ts">
  import { onMount, onDestroy } from 'svelte';

  // Reusable Leaflet map foundation. No document/boundary is loaded by
  // default yet — this will render whatever GeoJSON geometry the future
  // spatial validation pipeline produces.

  let mapEl: HTMLDivElement;
  let map: any;

  onMount(async () => {
    const L = await import('leaflet');

    map = L.map(mapEl).setView([20.5937, 78.9629], 5);

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap contributors'
    }).addTo(map);

    // The container's final size isn't settled until after layout, so
    // Leaflet's initial size measurement can be stale — force a recheck.
    requestAnimationFrame(() => map.invalidateSize());
  });

  onDestroy(() => {
    map?.remove();
  });
</script>

<svelte:head>
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
  />
</svelte:head>

<section class="panel p-6 md:p-8">
  <p class="hero-kicker">Map</p>
  <h1>ROAM Map</h1>
  <p class="subtitle">
    Reconstructed geometry and spatial validation results will render here
    once the document intelligence pipeline is implemented.
  </p>

  <div bind:this={mapEl} class="map-container"></div>
</section>

<style>
.hero-kicker {
  margin: 0;
  text-transform: uppercase;
  font-size: 0.74rem;
  letter-spacing: 0.1em;
  font-weight: 700;
  color: #0d6f4a;
}
h1 { margin: 8px 0 4px; }
.subtitle {
  margin: 0 0 16px;
  color: #5a6d75;
}

.map-container {
  height: 520px;
  border-radius: 16px;
  overflow: hidden;
  border: 1px solid rgba(17, 34, 40, 0.12);
}
</style>
