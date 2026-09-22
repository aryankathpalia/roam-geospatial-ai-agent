<script lang="ts">
  import { onMount, onDestroy } from 'svelte';

  export let geojson: any;
  export let color: string = '#d9581f';
  export let interactive: boolean = false;

  let mapEl: HTMLDivElement;
  let map: any;

  onMount(async () => {
    const L = await import('leaflet');

    map = L.map(mapEl, {
      zoomControl: interactive,
      dragging: interactive,
      scrollWheelZoom: false,
      doubleClickZoom: interactive,
      boxZoom: interactive,
      keyboard: false,
      attributionControl: interactive
    }).setView([0, 0], 2);

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap contributors'
    }).addTo(map);

    const layer = L.geoJSON(geojson, {
      style: { color, weight: 2.5, fillColor: color, fillOpacity: 0.14 }
    }).addTo(map);

    const bounds = layer.getBounds();
    if (bounds.isValid()) {
      map.fitBounds(bounds, { padding: [28, 28] });
    }

    requestAnimationFrame(() => map.invalidateSize());
    setTimeout(() => {
      map.invalidateSize();
      const b = layer.getBounds();
      if (b.isValid()) map.fitBounds(b, { padding: [28, 28] });
    }, 250);
  });

  onDestroy(() => {
    map?.remove();
  });
</script>

<div bind:this={mapEl} class="result-map"></div>

<style>
.result-map {
  width: 100%;
  height: 100%;
}

.result-map :global(.leaflet-control-attribution) {
  font-size: 9px;
}
</style>
