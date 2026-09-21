<script lang="ts">
  import Sidebar from '$lib/components/Sidebar.svelte';
  import '../app.css';

  let sidebarCollapsed = false;
</script>

<svelte:head>
  <title>ROAM</title>
</svelte:head>

<div class="app-shell">
  <Sidebar bind:collapsed={sidebarCollapsed} />

  <main class="app-main">
    <div class="page-wrap">
      <slot />
    </div>
  </main>
</div>
