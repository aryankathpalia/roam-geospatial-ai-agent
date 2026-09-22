<script lang="ts">
  import { page } from '$app/stores';

  const navItems = [
    { name: 'Workspace', href: '/workspace' }
  ];
</script>

<aside class="sidebar">
  <a href="/" class="brand">
    <span class="logo-mark">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
        <path d="M12 2 L21 7 V17 L12 22 L3 17 V7 Z" stroke="currentColor" stroke-width="1.6" />
        <circle cx="12" cy="12" r="2.4" fill="currentColor" />
      </svg>
    </span>
    <div class="brand-text">
      <span class="brand-title">ROAM</span>
      <span class="brand-subtitle">Document intelligence</span>
    </div>
  </a>

  <nav class="sidebar-nav">
    {#each navItems as item}
      <a
        href={item.href}
        class="nav-item"
        class:active={$page.url.pathname.startsWith(item.href)}
      >
        {item.name}
      </a>
    {/each}
  </nav>

  <div class="sidebar-footer">
    <a href="/" class="back-link">← Back to site</a>
  </div>
</aside>

<style>
.sidebar {
  width: 232px;
  flex: 0 0 auto;
  min-height: 100vh;
  background: var(--bg-elevated);
  border-right: 1px solid var(--line-soft);
  display: flex;
  flex-direction: column;
  padding: 20px 14px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  text-decoration: none;
  padding: 6px 8px 20px;
  border-bottom: 1px solid var(--line-soft);
  margin-bottom: 16px;
}

.logo-mark {
  width: 30px;
  height: 30px;
  border-radius: 9px;
  display: grid;
  place-items: center;
  color: var(--brand);
  background: var(--brand-soft);
  flex: 0 0 auto;
}

.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.2;
}

.brand-title {
  font-family: 'Space Grotesk', sans-serif;
  font-weight: 700;
  font-size: 0.98rem;
  color: var(--text);
}

.brand-subtitle {
  font-size: 0.66rem;
  color: var(--muted-dim);
}

.sidebar-nav {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.nav-item {
  padding: 9px 12px;
  border-radius: 9px;
  text-decoration: none;
  color: var(--muted);
  font-size: 0.87rem;
  font-weight: 500;
  transition: all 0.15s ease;
}

.nav-item:hover {
  background: rgba(255, 255, 255, 0.04);
  color: var(--text);
}

.nav-item.active {
  background: var(--brand-soft);
  color: var(--brand);
  font-weight: 700;
}

.sidebar-footer {
  padding-top: 12px;
  border-top: 1px solid var(--line-soft);
}

.back-link {
  font-size: 0.78rem;
  color: var(--muted-dim);
  text-decoration: none;
}

.back-link:hover {
  color: var(--muted);
}

@media (max-width: 800px) {
  .sidebar {
    width: 100%;
    min-height: auto;
    flex-direction: row;
    align-items: center;
    border-right: none;
    border-bottom: 1px solid var(--line-soft);
    padding: 12px 16px;
  }

  .brand {
    border-bottom: none;
    padding: 0;
    margin: 0 16px 0 0;
  }

  .sidebar-nav {
    flex-direction: row;
  }

  .sidebar-footer {
    display: none;
  }
}
</style>
