<script lang="ts">
  // Chat with the placement-review agent (POST /documents/{id}/agent/chat). The agent only PROPOSES fixes;
  // each proposal is previewed on the map by the parent (`preview` event) and applied or discarded here.
  // The conversation is kept per document in this browser (a convenience; the server keeps proposals).
  import { createEventDispatcher, onDestroy, tick } from 'svelte';

  export let apiBase: string;
  export let documentId: string;
  export let focus: { page: number; label: string | null } | null = null;
  export let previewId: string | null = null;

  type Step = { tool: string; summary: string };
  type Proposal = {
    id: string; kind: 'move' | 'stated_area'; status: string; page_number: number; reason: string;
    east_m?: number; north_m?: number; label?: string; stated_area?: string;
    score?: { outline_hugging_a_neighbour_pct: number; overlap_with_neighbours_pct: number };
  };
  type Msg = { role: 'user' | 'assistant' | 'error'; content: string; steps?: Step[]; proposals?: Proposal[]; model?: string };

  const dispatch = createEventDispatcher<{ preview: Proposal | null; applied: Proposal; close: void }>();
  const storeKey = () => `roam.agentChat.${documentId}`;

  let messages: Msg[] = load();
  let draft = '';
  let busy = false;
  let elapsed = 0;
  let timer: ReturnType<typeof setInterval> | null = null;
  let listEl: HTMLDivElement;
  let acting: string | null = null;

  function load(): Msg[] {
    try {
      return JSON.parse(localStorage.getItem(storeKey()) ?? '[]');
    } catch {
      return [];
    }
  }
  function save() {
    try {
      localStorage.setItem(storeKey(), JSON.stringify(messages.slice(-40)));
    } catch {
      // storage unavailable -- the chat still works for this visit
    }
  }

  const suggestions = [
    'Is this sheet in the right place? If not, why?',
    'The parcels are about 20 m off from where they should be',
    'The printed area on this parcel looks misread'
  ];

  async function scrollDown() {
    await tick();
    listEl?.scrollTo({ top: listEl.scrollHeight, behavior: 'smooth' });
  }

  async function send(text = draft) {
    const message = text.trim();
    if (!message || busy) return;
    const history = messages
      .filter((m) => m.role === 'user' || m.role === 'assistant')
      .map((m) => ({ role: m.role, content: m.content }));
    messages = [...messages, { role: 'user', content: message }];
    draft = '';
    busy = true;
    elapsed = 0;
    timer = setInterval(() => (elapsed += 1), 1000);
    save();
    scrollDown();
    try {
      const res = await fetch(`${apiBase}/documents/${documentId}/agent/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, history, page_number: focus?.page ?? null, label: focus?.label ?? null })
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) throw new Error(body?.detail ?? `${res.status} ${res.statusText}`);
      messages = [...messages, {
        role: 'assistant', content: body.reply || '(no answer)', steps: body.steps, proposals: body.proposals, model: body.model
      }];
      const firstMove = (body.proposals ?? []).find((p: Proposal) => p.kind === 'move');
      if (firstMove) dispatch('preview', firstMove);
    } catch (err) {
      messages = [...messages, { role: 'error', content: err instanceof Error ? err.message : 'The AI agent did not answer.' }];
    } finally {
      busy = false;
      if (timer) clearInterval(timer);
      timer = null;
      save();
      scrollDown();
    }
  }

  function setStatus(id: string, status: string) {
    messages = messages.map((m) => ({
      ...m, proposals: m.proposals?.map((p) => (p.id === id ? { ...p, status } : p))
    }));
    save();
  }

  async function act(p: Proposal, action: 'apply' | 'discard') {
    acting = p.id;
    try {
      const res = await fetch(`${apiBase}/documents/${documentId}/agent/proposals/${p.id}/${action}`, { method: 'POST' });
      const body = await res.json().catch(() => null);
      if (!res.ok && res.status !== 409) throw new Error(body?.detail ?? `${res.status} ${res.statusText}`);
      const status = res.status === 409 ? (body?.detail ?? '').replace('Proposal already ', '') || 'applied' : body.proposal.status;
      setStatus(p.id, status);
      if (previewId === p.id) dispatch('preview', null);
      if (action === 'apply' && res.ok) dispatch('applied', p);
    } catch (err) {
      messages = [...messages, { role: 'error', content: err instanceof Error ? err.message : 'Could not update the proposal.' }];
      save();
    } finally {
      acting = null;
    }
  }

  function clearChat() {
    messages = [];
    save();
    dispatch('preview', null);
  }

  function describe(p: Proposal): string {
    if (p.kind === 'move') {
      const e = p.east_m ?? 0;
      const n = p.north_m ?? 0;
      const dist = Math.round(Math.hypot(e, n));
      const dir = `${Math.abs(Math.round(n))} m ${n >= 0 ? 'north' : 'south'}, ${Math.abs(Math.round(e))} m ${e >= 0 ? 'east' : 'west'}`;
      return `Move page ${p.page_number}'s parcels ${dist} m (${dir})`;
    }
    return `Set ${p.label}'s printed area to “${p.stated_area}”`;
  }

  // Bold (**x**) and line breaks only: the agent answers in short plain text.
  function render(text: string): string {
    const esc = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return esc.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br>');
  }

  onDestroy(() => {
    if (timer) clearInterval(timer);
  });
</script>

<section class="agent panel" aria-label="AI placement reviewer">
  <header>
    <div>
      <strong>AI placement review</strong>
      <span class="sub">{focus ? `Page ${focus.page}${focus.label ? ` · ${focus.label}` : ''}` : 'Whole document'}</span>
    </div>
    <div class="head-actions">
      {#if messages.length}<button class="link" on:click={clearChat} disabled={busy}>Clear</button>{/if}
      <button class="close" aria-label="Close" on:click={() => dispatch('close')}>×</button>
    </div>
  </header>

  <div class="list" bind:this={listEl}>
    {#if !messages.length}
      <p class="intro">
        Describe what looks wrong, in your own words. The AI checks the printed coordinates, the county parcel
        records and the drawing, explains what it finds, and may propose a fix. Nothing changes until you apply it.
      </p>
      <div class="suggest">
        {#each suggestions as s}
          <button on:click={() => send(s)}>{s}</button>
        {/each}
      </div>
    {/if}

    {#each messages as m, mi (mi)}
      <div class="msg {m.role}">
        {#if m.role === 'assistant'}
          {#if m.steps?.length}
            <details class="steps">
              <summary>Checked {m.steps.length} thing{m.steps.length === 1 ? '' : 's'}</summary>
              <ol>{#each m.steps as s}<li>{s.summary}</li>{/each}</ol>
            </details>
          {/if}
          <div class="text">{@html render(m.content)}</div>
          {#each m.proposals ?? [] as p (p.id)}
            <div class="proposal" class:done={p.status !== 'pending'} class:previewing={previewId === p.id}>
              <span class="tag">Proposed fix</span>
              <p class="what">{describe(p)}</p>
              {#if p.kind === 'move' && p.score}
                <p class="fit">Fit at the new spot: {p.score.outline_hugging_a_neighbour_pct}% of the outline against a neighbouring parcel, {p.score.overlap_with_neighbours_pct}% overlap</p>
              {/if}
              {#if p.status === 'pending'}
                <div class="row">
                  {#if p.kind === 'move'}
                    <button class="ghost" on:click={() => dispatch('preview', previewId === p.id ? null : p)}>
                      {previewId === p.id ? 'Hide preview' : 'Show on map'}
                    </button>
                  {/if}
                  <button class="primary" disabled={acting === p.id} on:click={() => act(p, 'apply')}>Apply</button>
                  <button class="ghost" disabled={acting === p.id} on:click={() => act(p, 'discard')}>Discard</button>
                </div>
              {:else}
                <p class="status">{p.status === 'applied' ? '✓ Applied' + (p.kind === 'move' ? ' — “Reset position” on the card undoes it' : '') : 'Discarded'}</p>
              {/if}
            </div>
          {/each}
        {:else}
          <div class="text">{m.content}</div>
        {/if}
      </div>
    {/each}

    {#if busy}
      <div class="msg assistant working">
        <span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>
        Investigating… {elapsed}s
        <small>Checking coordinates, county records and the drawing. On the free AI tier this takes 1–4 minutes.</small>
      </div>
    {/if}
  </div>

  <form class="composer" on:submit|preventDefault={() => send()}>
    <textarea
      rows="2"
      bind:value={draft}
      placeholder="e.g. The parcels should be across the street, next to the cul-de-sac"
      disabled={busy}
      on:keydown={(e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          send();
        }
      }}
    ></textarea>
    <button class="primary" type="submit" disabled={busy || !draft.trim()}>Send</button>
  </form>
</section>

<style>
  .agent {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 14px;
    border: 1px solid #c7d2fe;
    background: #fbfbff;
    max-height: 74vh;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 8px;
  }
  header strong {
    display: block;
    font-size: 14px;
  }
  .sub {
    font-size: 12px;
    color: #6b7280;
  }
  .head-actions {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .close {
    border: 0;
    background: none;
    font-size: 20px;
    line-height: 1;
    cursor: pointer;
    color: #6b7280;
  }
  .link {
    border: 0;
    background: none;
    color: #4f46e5;
    font-size: 12px;
    cursor: pointer;
  }
  .list {
    flex: 1;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-height: 120px;
  }
  .intro {
    font-size: 13px;
    color: #4b5563;
    margin: 0;
  }
  .suggest {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .suggest button {
    text-align: left;
    font: inherit;
    font-size: 12.5px;
    padding: 7px 10px;
    border-radius: 8px;
    border: 1px solid #e0e7ff;
    background: #fff;
    cursor: pointer;
    color: #3730a3;
  }
  .msg {
    font-size: 13px;
    line-height: 1.45;
  }
  .msg.user .text {
    margin-left: 24px;
    padding: 8px 10px;
    border-radius: 10px;
    background: #4f46e5;
    color: #fff;
    white-space: pre-wrap;
  }
  .msg.assistant .text {
    padding: 2px 0;
    color: #1f2937;
  }
  .msg.error .text {
    padding: 8px 10px;
    border-radius: 8px;
    background: #fef2f2;
    color: #b91c1c;
  }
  .steps {
    font-size: 12px;
    color: #6b7280;
    margin-bottom: 4px;
  }
  .steps ol {
    margin: 4px 0 0 16px;
    padding: 0;
  }
  .proposal {
    margin-top: 8px;
    padding: 10px;
    border-radius: 10px;
    border: 1px dashed #6366f1;
    background: #eef2ff;
  }
  .proposal.previewing {
    border-style: solid;
    box-shadow: 0 0 0 2px #c7d2fe;
  }
  .proposal.done {
    border-color: #d1d5db;
    background: #f9fafb;
  }
  .tag {
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: #4f46e5;
  }
  .what {
    margin: 4px 0;
    font-weight: 600;
  }
  .fit,
  .status {
    margin: 0 0 6px;
    font-size: 12px;
    color: #4b5563;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  button.primary,
  button.ghost {
    font: inherit;
    font-size: 12.5px;
    padding: 6px 12px;
    border-radius: 8px;
    cursor: pointer;
  }
  button.primary {
    border: 1px solid #4f46e5;
    background: #4f46e5;
    color: #fff;
  }
  button.ghost {
    border: 1px solid #d1d5db;
    background: #fff;
    color: #374151;
  }
  button:disabled {
    opacity: 0.55;
    cursor: default;
  }
  .working {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    color: #4f46e5;
  }
  .working small {
    flex-basis: 100%;
    color: #6b7280;
  }
  .dots {
    display: inline-flex;
    gap: 3px;
  }
  .dots i {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #6366f1;
    animation: blink 1.2s infinite ease-in-out;
  }
  .dots i:nth-child(2) {
    animation-delay: 0.2s;
  }
  .dots i:nth-child(3) {
    animation-delay: 0.4s;
  }
  @keyframes blink {
    0%, 80%, 100% { opacity: 0.25; }
    40% { opacity: 1; }
  }
  .composer {
    display: flex;
    gap: 8px;
    align-items: flex-end;
  }
  .composer textarea {
    flex: 1;
    font: inherit;
    font-size: 13px;
    resize: vertical;
    padding: 8px 10px;
    border-radius: 8px;
    border: 1px solid #d1d5db;
    background: #fff;
    color: inherit;
  }
</style>
