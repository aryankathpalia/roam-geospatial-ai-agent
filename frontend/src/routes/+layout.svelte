<script lang="ts">
  import '../app.css';
  import { browser } from '$app/environment';
  import { installAuth } from '$lib/auth';

  // during setup, not onMount: child pages mount (and start fetching) before this layout's onMount runs
  if (browser) installAuth();
</script>

<svelte:head>
  <title>ROAM — Geospatial Document Intelligence</title>
  <meta
    name="description"
    content="ROAM turns scanned deeds, plats and survey documents into verified, georeferenced GIS data."
  />
</svelte:head>

<slot />
