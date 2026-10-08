<script lang="ts">
  // Signed out: Google's own "Continue with Google" button. Signed in: the account and a sign-out link.
  import { onMount, tick } from 'svelte';
  import { renderGoogleButton, signOut, user } from '$lib/auth';

  export let text: 'continue_with' | 'signin_with' | 'signup_with' = 'continue_with';
  export let width = 240;
  export let compact = false;

  let slot: HTMLDivElement;
  let error: string | null = null;

  async function draw() {
    await tick();
    if (slot && !$user) error = await renderGoogleButton(slot, { text, width });
  }
  onMount(draw);
  $: if (!$user && slot) draw();
</script>

{#if $user}
  <div class="account" class:compact>
    {#if $user.picture}<img src={$user.picture} alt="" referrerpolicy="no-referrer" />{/if}
    {#if !compact}<span class="who">{$user.name}</span>{/if}
    <button class="out" on:click={signOut}>Sign out</button>
  </div>
{:else}
  <div class="gbtn" bind:this={slot}></div>
  {#if error}<p class="err">{error}</p>{/if}
{/if}

<style>
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
  .gbtn {
    min-height: 40px;
  }
  .err {
    margin: 4px 0 0;
    font-size: 12px;
    color: #b45309;
  }
</style>
