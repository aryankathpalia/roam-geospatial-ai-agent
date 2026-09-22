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
      attributionControl: interactive,
      fadeAnimation: false,
      zoomAnimation: false
    });

    // Satellite imagery rather than the OSM street-vector style: a
    // parcel can sit on genuinely undeveloped rural land with almost
    // nothing drawn in OSM's vector tiles at high zoom (confirmed by
    // fetching an actual tile directly -- it really was mostly blank,
    // not a rendering bug), which reads as "broken" even though it's
    // accurately empty. Real imagery always has visible ground detail.
    L.tileLayer(
      'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      { attribution: 'Esri, Maxar, Earthstar Geographics' }
    ).addTo(map);

    // Settle the container's real size BEFORE the one-and-only fitBounds
    // call -- doing this after (or calling fitBounds more than once in
    // quick succession, which an earlier version of this component did)
    // makes Leaflet start loading tiles for one view, then abandon them
    // mid-fetch for the next, leaving broken/half-loaded tile fragments
    // instead of a clean render.
    map.invalidateSize(false);

    const layer = L.geoJSON(geojson, {
      style: { color, weight: 2.5, fillColor: color, fillOpacity: 0.14 }
    }).addTo(map);

    const bounds = layer.getBounds();
    if (bounds.isValid()) {
      map.fitBounds(bounds, { padding: [28, 28], animate: false });
    } else {
      map.setView([0, 0], 2);
    }
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
  background: #eeece6;
}

.result-map :global(.leaflet-control-attribution) {
  font-size: 9px;
}
</style>
