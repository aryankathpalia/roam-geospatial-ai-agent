<script lang="ts">
  import { onMount, onDestroy } from 'svelte';

  // A full-width banner of historical atlas sheets from the David Rumsey Map
  // Collection: the kind of scanned cadastral maps ROAM reads. Each sheet
  // crossfades in with a slow pan.
  type Slide = { src: string; place: string; sheet: string };
  export let slides: Slide[] = [
    { src: '/images/maps/portland.jpg', place: 'Portland, Maine', sheet: 'Plate map of the city wards' },
    { src: '/images/maps/washington-index.jpg', place: 'Washington, D.C.', sheet: 'Real estate atlas, index map' },
    { src: '/images/maps/oil-creek.jpg', place: 'Oil Creek, Pennsylvania', sheet: 'Historical plan of Oil Creek' },
    { src: '/images/maps/peaks-island.jpg', place: 'Peaks Island, Maine', sheet: 'Atlas plate, Peaks Island' },
    { src: '/images/maps/washington-plan.jpg', place: 'Washington, D.C.', sheet: 'Real estate atlas, plan 11' },
    { src: '/images/maps/newark.jpg', place: 'Newark, New Jersey', sheet: 'Property map, twelfth ward' }
  ];

  const SLIDE_MS = 6500;
  let current = 0;
  let paused = false;
  let timer: ReturnType<typeof setInterval> | undefined;
  // bumped each time a slide comes in, so its pan restarts then and not
  // while it is fading out
  let runs = slides.map(() => 0);

  function show(i: number) {
    current = (i + slides.length) % slides.length;
    runs[current] += 1;
    runs = runs;
  }

  function go(i: number) {
    show(i);
    restart();
  }

  function restart() {
    clearInterval(timer);
    timer = setInterval(() => {
      if (!paused) show(current + 1);
    }, SLIDE_MS);
  }

  onMount(() => {
    // warm the cache so every crossfade lands on a loaded image
    slides.slice(1).forEach((s) => (new Image().src = s.src));
    restart();
  });
  onDestroy(() => clearInterval(timer));
</script>

<div
  class="frame"
  style="--slide-ms: {SLIDE_MS}ms"
  role="region"
  aria-roledescription="carousel"
  aria-label="Historical survey maps"
  on:mouseenter={() => (paused = true)}
  on:mouseleave={() => (paused = false)}
>
  {#each slides as s, i}
    <div class="slide" class:active={i === current} class:even={i % 2 === 0} aria-hidden={i !== current}>
      {#key runs[i]}
        <img src={s.src} alt="{s.sheet}, {s.place}" loading={i === 0 ? 'eager' : 'lazy'} class:paused />
      {/key}
    </div>
  {/each}

  <div class="caption">
    {#key current}
      <div class="caption-inner">
        <span class="place">{slides[current].place}</span>
        <span class="sheet">{slides[current].sheet} · David Rumsey Map Collection</span>
      </div>
    {/key}
    <div class="controls">
      <div class="bars">
        {#each slides as s, i}
          <button class="bar" class:done={i < current} class:active={i === current} on:click={() => go(i)} aria-label="Show {s.place}">
            {#key current}
              <span class:paused></span>
            {/key}
          </button>
        {/each}
      </div>
      <span class="count">{String(current + 1).padStart(2, '0')} / {String(slides.length).padStart(2, '0')}</span>
    </div>
  </div>
</div>

<style>
  .frame {
    position: relative;
    height: clamp(280px, 40vw, 460px);
    border-radius: 16px;
    overflow: hidden;
    background: #ece5d6;
    border: 1px solid var(--line);
    box-shadow: var(--shadow-float);
  }

  .slide {
    position: absolute;
    inset: 0;
    opacity: 0;
    transition: opacity 1.2s ease;
  }
  .slide.active {
    opacity: 1;
  }
  .slide img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
    transform-origin: 35% 45%;
    animation: pan-a calc(var(--slide-ms) + 1300ms) linear forwards;
  }
  .slide.even img {
    transform-origin: 65% 55%;
    animation-name: pan-b;
  }
  @keyframes pan-a {
    from { transform: scale(1.02); }
    to { transform: scale(1.1) translate(-1.5%, -1%); }
  }
  @keyframes pan-b {
    from { transform: scale(1.1) translate(1.5%, 1%); }
    to { transform: scale(1.02); }
  }

  /* soft shade so the caption reads on any map */
  .frame::after {
    content: '';
    position: absolute;
    z-index: 2;
    inset: 0;
    pointer-events: none;
    background: linear-gradient(to top, rgba(23, 22, 19, 0.6), transparent 38%);
  }

  .caption {
    position: absolute;
    z-index: 3;
    left: 28px;
    right: 28px;
    bottom: 22px;
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 24px;
    color: #fff;
  }
  .caption-inner {
    display: flex;
    flex-direction: column;
    gap: 3px;
    animation: rise 0.6s ease both;
  }
  @keyframes rise {
    from { opacity: 0; transform: translateY(6px); }
  }
  .place {
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 700;
    font-size: 1.25rem;
    text-shadow: 0 1px 8px rgba(0, 0, 0, 0.3);
  }
  .sheet {
    font-size: 0.8rem;
    opacity: 0.85;
  }

  .controls {
    display: flex;
    align-items: center;
    gap: 14px;
  }
  .bars {
    display: flex;
    gap: 5px;
  }
  .bar {
    width: 34px;
    padding: 8px 0;
    border: 0;
    background: none;
    cursor: pointer;
  }
  .bar::before,
  .bar span {
    display: block;
    height: 2.5px;
    border-radius: 2px;
  }
  .bar::before {
    content: '';
    background: rgba(255, 255, 255, 0.35);
  }
  .bar span {
    margin-top: -2.5px;
    width: 0;
    background: #fff;
  }
  .bar.done span {
    width: 100%;
  }
  .bar.active span {
    animation: fill var(--slide-ms) linear forwards;
  }
  @keyframes fill {
    to { width: 100%; }
  }
  .paused {
    animation-play-state: paused !important;
  }
  .count {
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 0.74rem;
    opacity: 0.8;
    white-space: nowrap;
  }

  @media (max-width: 640px) {
    .caption {
      left: 16px;
      right: 16px;
      bottom: 14px;
      flex-direction: column;
      align-items: flex-start;
      gap: 8px;
    }
    .bar {
      width: 22px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .slide img {
      animation: none;
      transform: none;
    }
    .slide {
      transition: none;
    }
  }
</style>
