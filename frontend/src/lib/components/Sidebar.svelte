<script lang="ts">
  import { page } from '$app/stores';
  import MessageSquare from 'lucide-svelte/icons/message-square';
  import Map from 'lucide-svelte/icons/map';

  export let collapsed = false;

  const navItems = [
    { name: 'Ask ROAM', href: '/', icon: MessageSquare },
    { name: 'Map', href: '/map', icon: Map }
  ];
</script>

<aside
  class="sidebar"
  data-tour="nav"
  class:collapsed={collapsed}
>
  <div class="sidebar-header">
    <div class="brand">
      <div class="logo-mark">R</div>
      {#if !collapsed}
        <div class="brand-text-wrap">
          <span class="brand-title">ROAM</span>
          <span class="brand-subtitle">Geospatial document intelligence</span>
        </div>
      {/if}
    </div>

    <button
      class="collapse-btn"
      on:click={() => (collapsed = !collapsed)}
    >
      {#if collapsed}→{:else}←{/if}
    </button>
  </div>

  <nav class="sidebar-nav">
    {#each navItems as item}
      <a
        href={item.href}
        class="nav-item"
        class:active={item.href === '/' ? $page.url.pathname === '/' : $page.url.pathname.startsWith(item.href)}
      >
        <span class="icon-wrap"><svelte:component this={item.icon} size={18} stroke-width={1.8} /></span>

        {#if !collapsed}
          <span>{item.name}</span>
        {/if}
      </a>
    {/each}
  </nav>

  <div class="sidebar-footer">
    {#if !collapsed}
      <span>ROAM skeleton</span>
    {/if}
  </div>
</aside>

<style>
.sidebar {
  margin: 16px;
  height: calc(100vh - 32px);
  background: rgba(255, 255, 255, 0.76);
  backdrop-filter: blur(12px);
  border: 1px solid rgba(17, 34, 40, 0.12);
  border-radius: 20px;
  display: flex;
  flex-direction: column;
  width: 260px;
  transition: width 0.25s ease, transform 0.25s ease;
  box-shadow: 0 18px 42px rgba(10, 30, 38, 0.16);
}

.sidebar.collapsed {
  width: 64px;
}

.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px;
  border-bottom: 1px solid rgba(17, 34, 40, 0.1);
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}

.logo-mark {
  width: 48px;
  height: 48px;
  border-radius: 15px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  font-weight: 800;
  color: #0d6f4a;
  background: linear-gradient(140deg, rgba(14, 164, 107, 0.2), rgba(32, 149, 232, 0.18));
}

.brand-text-wrap {
  display: flex;
  flex-direction: column;
  line-height: 1.2;
}

.brand-title {
  font-family: 'Space Grotesk', sans-serif;
  font-weight: 900;
  font-size: 1.20rem;
  letter-spacing: 0.02em;
  color: #14303a;
}

.brand-subtitle {
  font-size: 0.68rem;
  letter-spacing: 0.12em;
  opacity: 0.8;
}

.collapse-btn {
  padding: 5px 8px;
  border-radius: 10px;
  border: 1px solid rgba(17, 34, 40, 0.13);
  color: #29444f;
  background: rgba(255, 255, 255, 0.72);
  transition: all 0.2s ease;
}

.collapse-btn:hover {
  background: rgba(14, 164, 107, 0.12);
}

.sidebar-nav {
  flex: 1;
  padding: 12px 10px;
  overflow-y: auto;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  margin: 4px 10px;
  border-radius: 12px;
  text-decoration: none;
  color: #3e5e69;
  transition: all 0.2s ease;
}

.icon-wrap {
  display: inline-grid;
  place-items: center;
  color: #5f7178;
}

.nav-item:hover {
  background: rgba(17, 34, 40, 0.05);
  color: #112228;
  transform: translateX(2px);
}

.nav-item.active {
  background: linear-gradient(130deg, rgba(14, 164, 107, 0.2), rgba(32, 149, 232, 0.2));
  color: #0d6f4a;
  font-weight: 700;
  border: 1px solid rgba(14, 164, 107, 0.25);
}

.nav-item.active .icon-wrap {
  color: #0d6f4a;
}

.sidebar-footer {
  padding: 12px 14px;
  border-top: 1px solid rgba(17, 34, 40, 0.1);
  font-size: 0.74rem;
  color: #607179;
}

.sidebar.collapsed .nav-item {
  margin: 6px auto;
  padding: 10px;
  justify-content: center;
}

@media (max-width: 800px) {
  .sidebar {
    margin: 0;
    border-radius: 0;
    width: 100%;
    height: 72px;
    flex-direction: row;
    align-items: center;
    padding: 8px;
  }

  .sidebar.collapsed {
    width: 100%;
  }

  .sidebar-header {
    border: none;
    width: auto;
    padding: 6px;
  }

  .sidebar-nav {
    display: flex;
    gap: 4px;
    flex: 1;
    padding: 0 8px;
    overflow-x: auto;
    overflow-y: hidden;
  }

  .sidebar-footer {
    display: none;
  }
}
</style>
