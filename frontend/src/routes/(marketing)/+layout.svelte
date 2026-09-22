<script lang="ts">
  let mobileOpen = false;
</script>

<div class="marketing-shell">
  <header class="nav">
    <div class="nav-inner">
      <a href="/" class="brand">
        <span class="logo-mark">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
            <path
              d="M12 2 L21 7 V17 L12 22 L3 17 V7 Z"
              stroke="currentColor"
              stroke-width="1.6"
              fill="none"
            />
            <circle cx="12" cy="12" r="2.6" fill="currentColor" />
          </svg>
        </span>
        <span class="brand-name">ROAM</span>
      </a>

      <nav class="nav-links">
        <a href="#pipeline">Pipeline</a>
        <a href="#evidence">Evidence</a>
        <a href="#technology">Technology</a>
      </nav>

      <div class="nav-actions">
        <a href="/workspace" class="btn btn-primary">Open Workspace</a>
      </div>

      <button class="nav-burger" on:click={() => (mobileOpen = !mobileOpen)} aria-label="Menu">
        <span></span><span></span><span></span>
      </button>
    </div>

    {#if mobileOpen}
      <div class="nav-mobile">
        <a href="#pipeline" on:click={() => (mobileOpen = false)}>Pipeline</a>
        <a href="#evidence" on:click={() => (mobileOpen = false)}>Evidence</a>
        <a href="#technology" on:click={() => (mobileOpen = false)}>Technology</a>
        <a href="/workspace" class="btn btn-primary">Open Workspace</a>
      </div>
    {/if}
  </header>

  <main>
    <slot />
  </main>

  <footer class="site-footer">
    <div class="footer-inner">
      <div class="footer-brand">
        <span class="brand-name">ROAM</span>
        <span class="footer-tag">Reasoning-Oriented Agent for Maps</span>
      </div>
      <div class="footer-links">
        <a href="/workspace">Workspace</a>
        <a href="https://github.com/aryankathpalia/roam-geospatial-ai-agent" target="_blank" rel="noreferrer">GitHub</a>
      </div>
    </div>
  </footer>
</div>

<style>
.marketing-shell {
  min-height: 100vh;
  background: var(--bg);
}

.nav {
  position: sticky;
  top: 0;
  z-index: 40;
  background: rgba(247, 246, 243, 0.86);
  backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--line-soft);
}

.nav-inner {
  max-width: 1240px;
  margin: 0 auto;
  padding: 16px 28px;
  display: flex;
  align-items: center;
  gap: 32px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  text-decoration: none;
  margin-right: auto;
}

.logo-mark {
  width: 36px;
  height: 36px;
  border-radius: 9px;
  display: grid;
  place-items: center;
  color: var(--ink);
  background: var(--surface-muted);
}

.brand-name {
  font-family: 'Space Grotesk', sans-serif;
  font-weight: 700;
  font-size: 1.4rem;
  letter-spacing: 0.02em;
  color: var(--text);
}

.nav-links {
  display: flex;
  gap: 28px;
}

.nav-links a {
  text-decoration: none;
  color: var(--muted);
  font-size: 0.88rem;
  font-weight: 500;
  transition: color 0.15s ease;
}

.nav-links a:hover {
  color: var(--text);
}

.nav-burger {
  display: none;
  flex-direction: column;
  gap: 4px;
  background: none;
  border: none;
  padding: 6px;
}

.nav-burger span {
  width: 20px;
  height: 1.6px;
  background: var(--text);
}

.nav-mobile {
  display: none;
}

@media (max-width: 760px) {
  .nav-links,
  .nav-actions {
    display: none;
  }
  .nav-burger {
    display: flex;
  }
  .nav-mobile {
    display: flex;
    flex-direction: column;
    gap: 14px;
    padding: 16px 24px 22px;
    border-top: 1px solid var(--line-soft);
  }
  .nav-mobile a:not(.btn) {
    text-decoration: none;
    color: var(--muted);
    font-size: 0.92rem;
  }
}

.site-footer {
  border-top: 1px solid var(--line-soft);
  padding: 36px 28px;
}

.footer-inner {
  max-width: 1240px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
}

.footer-brand {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.footer-tag {
  font-size: 0.76rem;
  color: var(--muted-dim);
}

.footer-links {
  display: flex;
  gap: 22px;
}

.footer-links a {
  text-decoration: none;
  color: var(--muted);
  font-size: 0.84rem;
}

.footer-links a:hover {
  color: var(--text);
}
</style>
