<script lang="ts">
  import AppSidebar from '$lib/components/AppSidebar.svelte';
</script>

<div class="app-shell">
  <AppSidebar />
  <main class="app-main">
    <div class="page-wrap">
      <slot />
    </div>
  </main>
</div>
