<script lang="ts">
  import '../app.css';
  import { browser } from '$app/environment';
  import { installAuth, API_BASE } from '$lib/auth';

  // during setup, not onMount: child pages mount (and start fetching) before this layout's onMount runs
  if (browser) {
    installAuth();
    // Wake the backend the moment any page loads: it sleeps when idle and takes 15-20 s to start,
    // so by the time a visitor opens a sample it is usually ready.
    fetch(`${API_BASE}/auth/config`).catch(() => {});
  }
</script>

<svelte:head>
  <title>ROAM | Geospatial Document Intelligence</title>
  <meta
    name="description"
    content="ROAM turns scanned deeds, plats and survey documents into verified, georeferenced GIS data."
  />
</svelte:head>

<slot />
