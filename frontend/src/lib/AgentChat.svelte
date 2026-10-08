<script lang="ts">
  // The AI placement review panel. Each request is a "run": the user's words, the checks the agent makes
  // as they happen (streamed from POST /documents/{id}/agent/chat/stream), its finding, and any proposed
  // fix -- previewed on the map by the parent (`preview` event), applied or discarded here. Runs are kept
  // per document in this browser (a convenience; proposals themselves live on the server).
  import { createEventDispatcher, onDestroy, tick } from 'svelte';

  export let apiBase: string;
  export let documentId: string;
  export let focus: { page: number; label: string | null } | null = null;
  export let previewId: string | null = null;

  type Step = { id: string; label: string; summary?: string; ok?: boolean; image?: string; running: boolean };
  type Proposal = {
    id: string; kind: 'move' | 'stated_area'; status: string; page_number: number; reason: string;
    east_m?: number; north_m?: number; label?: string; stated_area?: string; from_user_instruction?: boolean;
    check?: { kind: 'imagery' | 'county_parcels'; features?: string[]; hugging_pct?: number; overlap_pct?: number; corroborated?: boolean } | null;
  };
  type Run = {
    request: string; scope: string; steps: Step[]; thinking: boolean;
    reply?: string; proposals?: Proposal[]; error?: string; done: boolean; started: number; seconds?: number;
  };

  const dispatch = createEventDispatcher<{ preview: Proposal | null; applied: Proposal; close: void }>();
  const storeKey = () => `roam.agentRuns.${documentId}`;

  let runs: Run[] = load();
  let draft = '';
  let busy = false;
  let now = Date.now();
  let clock: ReturnType<typeof setInterval> | null = null;
  let bodyEl: HTMLDivElement;
  let acting: string | null = null;
  let enlarged: string | null = null;
  let abort: AbortController | null = null;

  $: scope = focus ? `Page ${focus.page}${focus.label ? ` · ${focus.label}` : ''}` : 'Whole document';
  $: current = runs[runs.length - 1] ?? null;
  $: earlier = runs.slice(0, -1);

  function load(): Run[] {
    try {
      return (JSON.parse(localStorage.getItem(storeKey()) ?? '[]') as Run[]).filter((r) => r.done);
    } catch {
      return [];
    }
  }
  function save() {
    try {
      // images stay out of storage: they are large and only useful while the run is fresh
      const slim = runs.filter((r) => r.done).slice(-10).map((r) => ({ ...r, steps: r.steps.map(({ image, ...s }) => s) }));
      localStorage.setItem(storeKey(), JSON.stringify(slim));
    } catch {
      // storage unavailable -- the panel still works for this visit
    }
  }

  const quick = ['It is a few metres off', 'It sits on the wrong side of the road', 'Is this in the right place?'];

  async function scrollDown() {
    await tick();
    bodyEl?.scrollTo({ top: bodyEl.scrollHeight, behavior: 'smooth' });
  }

  function update(fn: (r: Run) => void) {
    const r = runs[runs.length - 1];
    fn(r);
    runs = runs;
  }

  async function start(text = draft) {
    const request = text.trim();
    if (!request || busy) return;
    const history = runs.flatMap((r) => [
      { role: 'user', content: r.request },
      ...(r.reply ? [{ role: 'assistant', content: r.reply }] : [])
    ]);
    runs = [...runs, { request, scope, steps: [], thinking: true, done: false, started: Date.now() }];
    draft = '';
    busy = true;
    clock = setInterval(() => (now = Date.now()), 1000);
    dispatch('preview', null);
    scrollDown();
    abort = new AbortController();
    try {
      const res = await fetch(`${apiBase}/documents/${documentId}/agent/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: request, history, page_number: focus?.page ?? null, label: focus?.label ?? null }),
        signal: abort.signal
      });
      if (!res.ok || !res.body) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail ?? `${res.status} ${res.statusText}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let nl: number;
        while ((nl = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, nl).trim();
          buffer = buffer.slice(nl + 1);
          if (line) handle(JSON.parse(line));
        }
      }
      if (!runs[runs.length - 1].done) update((r) => (r.error = 'The review stopped without an answer.'));
    } catch (err) {
      if (!(err instanceof DOMException && err.name === 'AbortError')) {
        update((r) => (r.error = err instanceof Error ? err.message : 'The AI review did not answer.'));
      } else {
        update((r) => (r.error = 'Stopped.'));
      }
    } finally {
      update((r) => {
        r.done = true;
        r.thinking = false;
        r.steps.forEach((s) => (s.running = false));
        r.seconds = Math.round((Date.now() - r.started) / 1000);
      });
      busy = false;
      abort = null;
      if (clock) clearInterval(clock);
      clock = null;
      save();
      scrollDown();
    }
  }

  function handle(ev: any) {
    if (ev.type === 'thinking') update((r) => (r.thinking = true));
    else if (ev.type === 'step_start') update((r) => {
      r.thinking = false;
      r.steps.push({ id: ev.id, label: ev.label, running: true });
    });
    else if (ev.type === 'step_done') update((r) => {
      const s = r.steps.find((x) => x.id === ev.id);
      if (s) Object.assign(s, { summary: ev.summary, ok: ev.ok, image: ev.image, running: false });
    });
    else if (ev.type === 'done') {
      update((r) => {
        r.reply = ev.reply;
        r.proposals = ev.proposals ?? [];
        r.thinking = false;
        r.done = true;
      });
      const firstMove = (ev.proposals ?? []).find((p: Proposal) => p.kind === 'move');
      if (firstMove) dispatch('preview', firstMove);
    } else if (ev.type === 'error') update((r) => (r.error = ev.message));
    scrollDown();
  }

  function setStatus(id: string, status: string) {
    runs = runs.map((r) => ({ ...r, proposals: r.proposals?.map((p) => (p.id === id ? { ...p, status } : p)) }));
    save();
  }

  async function act(p: Proposal, action: 'apply' | 'discard') {
    acting = p.id;
    try {
      const res = await fetch(`${apiBase}/documents/${documentId}/agent/proposals/${p.id}/${action}`, { method: 'POST' });
      const body = await res.json().catch(() => null);
      if (!res.ok && res.status !== 409) throw new Error(body?.detail ?? `${res.status} ${res.statusText}`);
      setStatus(p.id, res.ok ? body.proposal.status : (body?.detail ?? '').replace('Proposal already ', '') || 'applied');
      if (previewId === p.id) dispatch('preview', null);
      if (action === 'apply' && res.ok) dispatch('applied', p);
    } catch (err) {
      update((r) => (r.error = err instanceof Error ? err.message : 'Could not update the proposal.'));
    } finally {
      acting = null;
    }
  }

  function clearAll() {
    runs = [];
    save();
    dispatch('preview', null);
  }

  function direction(e = 0, n = 0): string {
    const parts = [
      Math.abs(n) >= 0.5 ? `${Math.abs(Math.round(n))} m ${n >= 0 ? 'north' : 'south'}` : '',
      Math.abs(e) >= 0.5 ? `${Math.abs(Math.round(e))} m ${e >= 0 ? 'east' : 'west'}` : ''
    ].filter(Boolean);
    return parts.join(', ') || 'no move';
  }

  function evidence(p: Proposal): string {
    const c = p.check;
    if (!c) return p.from_user_instruction ? 'As you instructed — not checked against the imagery' : '';
    if (c.kind === 'imagery') return `Checked on the satellite imagery: lines up with ${c.features?.join(', ')}`;
    return `Checked against the county parcels: ${c.hugging_pct}% of the outline against a neighbour, ${c.overlap_pct}% overlap${c.corroborated ? ' (corroborated)' : ''}`;
  }

  function render(text: string): string {
    const esc = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return esc.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br>');
  }

  onDestroy(() => {
    if (clock) clearInterval(clock);
    abort?.abort();
  });
</script>

<section class="agent panel" aria-label="AI placement review">
  <header>
    <div class="title">
      <span class="spark" aria-hidden="true">✦</span>
      <div>
        <strong>AI placement review</strong>
        <span class="sub">{scope}</span>
      </div>
    </div>
    <div class="head-actions">
      {#if runs.length && !busy}<button class="link" on:click={clearAll}>Clear</button>{/if}
      <button class="close" aria-label="Close" on:click={() => dispatch('close')}>×</button>
    </div>
  </header>

  <div class="body" bind:this={bodyEl}>
    {#each earlier as r}
      <details class="past">
        <summary>“{r.request}” <span>· {r.steps.length} checks{r.proposals?.length ? ' · proposed a fix' : ''}</span></summary>
        {#if r.reply}<div class="reply">{@html render(r.reply)}</div>{/if}
      </details>
    {/each}

    {#if !current}
      <p class="intro">Tell the AI what looks wrong. It reads the plat, compares it with the satellite imagery and any county records, shows each check it makes, and proposes a fix you can preview and apply.</p>
    {:else}
      <div class="run">
        <p class="request"><span>You asked</span>“{current.request}”</p>

        <ol class="steps">
          {#each current.steps as s (s.id)}
            <li class:running={s.running} class:fail={s.ok === false}>
              <span class="dot" aria-hidden="true">{#if s.running}<i class="spin"></i>{:else if s.ok === false}!{:else}✓{/if}</span>
              <div class="step-text">
                <span class="label">{s.running ? `${s.label}…` : s.summary ?? s.label}</span>
                {#if s.image}
                  <button class="shot" title="Enlarge" on:click={() => (enlarged = s.image ?? null)}>
                    <img src={`data:image/jpeg;base64,${s.image}`} alt="Satellite imagery with the outlines and the features the AI matched" />
                  </button>
                {/if}
              </div>
            </li>
          {/each}
          {#if !current.done && current.thinking}
            <li class="running thinking">
              <span class="dot" aria-hidden="true"><i class="spin"></i></span>
              <span class="label">{current.steps.length ? 'Deciding the next check…' : 'Reading your request…'}</span>
            </li>
          {/if}
        </ol>

        {#if !current.done}
          <p class="elapsed">{Math.round((now - current.started) / 1000)}s · free AI models take 1–4 minutes
            <button class="link" on:click={() => abort?.abort()}>Stop</button></p>
        {/if}

        {#if current.error}<p class="err">{current.error}</p>{/if}

        {#if current.reply}
          <div class="finding">
            <span class="tag">Finding</span>
            <div class="reply">{@html render(current.reply)}</div>
          </div>
        {/if}

        {#each current.proposals ?? [] as p (p.id)}
          <div class="fix" class:done={p.status !== 'pending'} class:previewing={previewId === p.id}>
            <span class="tag">Proposed fix</span>
            <p class="what">
              {#if p.kind === 'move'}Move page {p.page_number}'s parcels {direction(p.east_m, p.north_m)}
              {:else}Set {p.label}'s printed area to “{p.stated_area}”{/if}
            </p>
            {#if evidence(p)}<p class="evidence">{evidence(p)}</p>{/if}
            {#if p.status === 'pending'}
              <div class="row">
                <button class="primary" disabled={acting === p.id} on:click={() => act(p, 'apply')}>Apply</button>
                {#if p.kind === 'move'}
                  <button class="ghost" on:click={() => dispatch('preview', previewId === p.id ? null : p)}>
                    {previewId === p.id ? 'Hide preview' : 'Preview on map'}
                  </button>
                {/if}
                <button class="ghost" disabled={acting === p.id} on:click={() => act(p, 'discard')}>Discard</button>
              </div>
              {#if previewId === p.id}<p class="hint">Dashed outline on the map = where it would go.</p>{/if}
            {:else}
              <p class="evidence">{p.status === 'applied' ? '✓ Applied' + (p.kind === 'move' ? ' · “Reset position” on the parcel card undoes it' : '') : 'Discarded'}</p>
            {/if}
          </div>
        {/each}
      </div>
    {/if}
  </div>

  <form class="composer" on:submit|preventDefault={() => start()}>
    {#if !current}
      <div class="chips">
        {#each quick as q}<button type="button" on:click={() => start(q)}>{q}</button>{/each}
      </div>
    {/if}
    <div class="input-row">
      <textarea
        rows="2"
        bind:value={draft}
        placeholder={current ? 'Follow up, e.g. “it should be 10 m further west”' : 'Describe the problem, e.g. “the parcels should be west of the road”'}
        disabled={busy}
        on:keydown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            start();
          }
        }}
      ></textarea>
      <button class="primary" type="submit" disabled={busy || !draft.trim()}>{current ? 'Send' : 'Review'}</button>
    </div>
  </form>
</section>

{#if enlarged}
  <button class="lightbox" aria-label="Close image" on:click={() => (enlarged = null)}>
    <img src={`data:image/jpeg;base64,${enlarged}`} alt="Satellite imagery checked by the AI" />
  </button>
{/if}

<style>
  .agent {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 14px 14px 12px;
    border: 1px solid #c7d2fe;
    background: #fff;
    max-height: 78vh;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
  }
  .title {
    display: flex;
    gap: 10px;
    align-items: center;
  }
  .spark {
    display: grid;
    place-items: center;
    width: 28px;
    height: 28px;
    border-radius: 8px;
    background: #eef2ff;
    color: #4f46e5;
  }
  .title strong {
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
    gap: 8px;
  }
  .close {
    border: 0;
    background: none;
    font-size: 20px;
    line-height: 1;
    cursor: pointer;
    color: #9ca3af;
  }
  .link {
    border: 0;
    background: none;
    padding: 0;
    color: #4f46e5;
    font: inherit;
    font-size: 12px;
    cursor: pointer;
  }
  .body {
    flex: 1;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-height: 80px;
  }
  .intro {
    margin: 0;
    font-size: 13px;
    line-height: 1.5;
    color: #4b5563;
  }
  .past {
    font-size: 12.5px;
    color: #4b5563;
    border-bottom: 1px solid #f1f1f4;
    padding-bottom: 6px;
  }
  .past summary {
    cursor: pointer;
  }
  .past summary span {
    color: #9ca3af;
  }
  .request {
    margin: 0 0 8px;
    font-size: 13px;
    color: #111827;
  }
  .request span {
    display: block;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #9ca3af;
    margin-bottom: 2px;
  }
  .steps {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
    position: relative;
  }
  .steps li {
    display: grid;
    grid-template-columns: 20px 1fr;
    gap: 8px;
    padding: 5px 0;
    font-size: 12.5px;
    line-height: 1.45;
    color: #374151;
  }
  .steps li.running .label {
    color: #4f46e5;
  }
  .steps li.fail .label {
    color: #b45309;
  }
  .dot {
    display: grid;
    place-items: center;
    width: 18px;
    height: 18px;
    margin-top: 1px;
    border-radius: 50%;
    background: #ecfdf5;
    color: #059669;
    font-size: 11px;
    font-weight: 700;
  }
  li.running .dot {
    background: #eef2ff;
  }
  li.fail .dot {
    background: #fffbeb;
    color: #b45309;
  }
  .spin {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    border: 2px solid #c7d2fe;
    border-top-color: #4f46e5;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin {
    to { transform: rotate(360deg); }
  }
  .shot {
    display: block;
    margin-top: 6px;
    padding: 0;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    overflow: hidden;
    cursor: zoom-in;
    background: none;
    width: 100%;
    max-width: 260px;
  }
  .shot img {
    display: block;
    width: 100%;
  }
  .elapsed {
    margin: 4px 0 0 28px;
    font-size: 11.5px;
    color: #9ca3af;
  }
  .err {
    margin: 6px 0 0;
    padding: 8px 10px;
    border-radius: 8px;
    background: #fef2f2;
    color: #b91c1c;
    font-size: 12.5px;
  }
  .finding {
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px solid #f1f1f4;
  }
  .reply {
    font-size: 13px;
    line-height: 1.5;
    color: #1f2937;
  }
  .tag {
    display: block;
    margin-bottom: 3px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: #6b7280;
  }
  .fix {
    margin-top: 10px;
    padding: 12px;
    border-radius: 10px;
    border: 1px solid #a5b4fc;
    background: #eef2ff;
  }
  .fix .tag {
    color: #4f46e5;
  }
  .fix.previewing {
    box-shadow: 0 0 0 3px #e0e7ff;
  }
  .fix.done {
    border-color: #e5e7eb;
    background: #f9fafb;
  }
  .what {
    margin: 2px 0 4px;
    font-size: 14px;
    font-weight: 600;
    color: #1e1b4b;
  }
  .evidence,
  .hint {
    margin: 0 0 8px;
    font-size: 12px;
    color: #4b5563;
  }
  .hint {
    margin: 6px 0 0;
    color: #6366f1;
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
    padding: 7px 14px;
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
    opacity: 0.5;
    cursor: default;
  }
  .composer {
    display: flex;
    flex-direction: column;
    gap: 8px;
    border-top: 1px solid #f1f1f4;
    padding-top: 10px;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .chips button {
    font: inherit;
    font-size: 12px;
    padding: 5px 10px;
    border-radius: 999px;
    border: 1px solid #e0e7ff;
    background: #f5f7ff;
    color: #3730a3;
    cursor: pointer;
  }
  .input-row {
    display: flex;
    gap: 8px;
    align-items: flex-end;
  }
  .input-row textarea {
    flex: 1;
    font: inherit;
    font-size: 13px;
    resize: none;
    padding: 8px 10px;
    border-radius: 8px;
    border: 1px solid #d1d5db;
    background: #fff;
    color: inherit;
  }
  .lightbox {
    position: fixed;
    inset: 0;
    z-index: 2000;
    display: grid;
    place-items: center;
    padding: 24px;
    border: 0;
    background: rgba(17, 24, 39, 0.75);
    cursor: zoom-out;
  }
  .lightbox img {
    max-width: min(92vw, 900px);
    max-height: 90vh;
    border-radius: 10px;
  }
</style>
