<script lang="ts">
  // Signed out: a "Continue with Google" button in ROAM's own button style (same size, corners and spacing as
  // the buttons beside it), with Google's real sign-in button laid invisibly over it -- the click still goes
  // through Google Identity Services. Signed in: the account and a sign-out link.
  import { onMount, tick } from 'svelte';
  import { renderGoogleButton, signOut, user } from '$lib/auth';

  export let text: 'continue_with' | 'signin_with' = 'continue_with';
  export let compact = false;

  let wrap: HTMLDivElement;
  let slot: HTMLDivElement;
  let error: string | null = null;
  let ready = false;

  $: label = text === 'signin_with' ? 'Sign in with Google' : 'Continue with Google';

  // Rendered once per signed-out spell, never from a reactive statement: Google's button updates the DOM,
  // and re-rendering on every update looped forever (the tab froze).
  let drawn = false;
  async function draw() {
    await tick();
    if (drawn || !slot || !wrap) return;
    drawn = true;
    error = await renderGoogleButton(slot, { text, width: Math.min(400, Math.max(200, Math.round(wrap.offsetWidth))) });
    ready = !error;
  }
  onMount(() =>
    user.subscribe((u) => {
      drawn = false; // a fresh button element appears after signing out
      if (!u) draw();
    })
  );
</script>

{#if $user}
  <div class="account" class:compact>
    {#if $user.picture}<img src={$user.picture} alt="" referrerpolicy="no-referrer" />{/if}
    {#if !compact}<span class="who">{$user.name}</span>{/if}
    <button class="out" on:click={signOut}>Sign out</button>
  </div>
{:else}
  <div class="gwrap" class:ready bind:this={wrap}>
    <span class="btn btn-ghost gface" aria-hidden="true">
      <svg width="18" height="18" viewBox="0 0 48 48">
        <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z" />
        <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z" />
        <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z" />
        <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z" />
      </svg>
      {label}
    </span>
    <div class="goverlay" bind:this={slot}></div>
  </div>
  {#if error}<p class="err">{error}</p>{/if}
{/if}

<style>
  .gwrap {
    position: relative;
    display: inline-flex;
  }
  .gface {
    background: var(--surface, #fff);
    white-space: nowrap;
    padding-block: 10px; /* the 1 px outline: same 44 px height as the filled button beside it */
  }
  .gwrap:hover .gface {
    transform: translateY(-1px);
    background: var(--surface-muted, #f4f2ee);
    border-color: rgba(23, 22, 19, 0.18);
  }
  .gwrap:not(.ready) .gface {
    opacity: 0.6;
  }
  /* Google's own button, stretched over ours and invisible: it receives the click */
  .goverlay {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    opacity: 0.0001;
    border-radius: 9px;
  }
  .goverlay > :global(div) {
    transform: scale(1.02, 1.2); /* Google's 40 px button over our 44 px one: cover the edges too */
  }
  .account {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 4px 6px 4px 4px;
    border: 1px solid var(--border, #e5e2dc);
    border-radius: 999px;
    background: #fff;
    font-size: 13px;
  }
  .account img {
    width: 28px;
    height: 28px;
    border-radius: 50%;
  }
  .who {
    max-width: 160px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .out {
    border: 0;
    background: none;
    font: inherit;
    font-size: 12px;
    color: #6b7280;
    cursor: pointer;
  }
  .err {
    margin: 4px 0 0;
    font-size: 12px;
    color: #b45309;
  }
</style>
