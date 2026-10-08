<script lang="ts">
  import { onMount, onDestroy } from 'svelte';

  // Historical atlas sheets from the David Rumsey Map Collection, the same
  // kind of scanned cadastral maps ROAM reads. Cropped to their busiest area.
  const slides = [
    { src: '/images/carousel/washington-index.jpg', place: 'Washington, D.C.', sheet: 'Real estate atlas, index map' },
    { src: '/images/carousel/stapleton.jpg', place: 'Stapleton, Staten Island', sheet: 'Ward map, village of Edgewater' },
    { src: '/images/carousel/oil-creek.jpg', place: 'Oil Creek, Pennsylvania', sheet: 'Historical plan of Oil Creek' },
    { src: '/images/carousel/washington-plan.jpg', place: 'Washington, D.C.', sheet: 'Real estate atlas, plan 11' },
    { src: '/images/carousel/rock-creek.jpg', place: 'Rock Creek, Washington, D.C.', sheet: 'Real estate atlas, plan 1' },
    { src: '/images/carousel/newark.jpg', place: 'Newark, New Jersey', sheet: 'Property map, twelfth ward' }
  ];

  const SLIDE_MS = 6000;
  let current = 0;
  let paused = false;
  // bumped each time a slide comes in, so its pan restarts then and not
  // while it is fading out
  let runs = slides.map(() => 0);

  function show(i: number) {
    current = (i + slides.length) % slides.length;
    runs[current] += 1;
    runs = runs;
  }
  let timer: ReturnType<typeof setInterval> | undefined;

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
  class="carousel"
  style="--slide-ms: {SLIDE_MS}ms"
  role="region"
  aria-roledescription="carousel"
  aria-label="Historical survey maps"
  on:mouseenter={() => (paused = true)}
  on:mouseleave={() => (paused = false)}
>
  <div class="stack">
  <div class="deck" aria-hidden="true"></div>
  <div class="deck deck-2" aria-hidden="true"></div>

  <div class="frame">
    {#each slides as s, i}
      <div class="slide" class:active={i === current} class:even={i % 2 === 0} aria-hidden={i !== current}>
        {#key runs[i]}
          <img src={s.src} alt="{s.sheet}, {s.place}" loading={i === 0 ? 'eager' : 'lazy'} class:paused />
        {/key}
      </div>
    {/each}

    <div class="bars">
      {#each slides as s, i}
        <button
          class="bar"
          class:done={i < current}
          class:active={i === current}
          on:click={() => go(i)}
          aria-label="Show {s.place}"
        >
          {#key current}
            <span class:paused></span>
          {/key}
        </button>
      {/each}
    </div>

    <div class="caption">
      {#key current}
        <div class="caption-inner">
          <span class="place">{slides[current].place}</span>
          <span class="sheet">{slides[current].sheet}</span>
        </div>
      {/key}
      <span class="count">{String(current + 1).padStart(2, '0')} / {String(slides.length).padStart(2, '0')}</span>
    </div>
  </div>
  </div>

  <p class="credit">Atlas sheets from the David Rumsey Map Collection</p>
</div>

<style>
  .carousel {
    position: relative;
    width: 100%;
    max-width: 400px;
    margin: 0 auto;
  }

  /* two cards peeking out behind, like a stack of sheets */
  .stack {
    position: relative;
  }
  .deck {
    position: absolute;
    inset: 0;
    border-radius: 18px;
    background: #efe9dc;
    border: 1px solid var(--line);
    transform: rotate(4deg) translate(10px, 6px);
  }
  .deck-2 {
    background: #e6dfcf;
    transform: rotate(-3.5deg) translate(-10px, 8px);
  }

  .frame {
    position: relative;
    aspect-ratio: 4 / 5;
    border-radius: 18px;
    overflow: hidden;
    background: #ece5d6;
    border: 1px solid var(--line);
    box-shadow: 0 30px 60px -28px rgba(23, 22, 19, 0.45), 0 8px 20px -10px rgba(23, 22, 19, 0.18);
  }

  .slide {
    position: absolute;
    inset: 0;
    opacity: 0;
    transition: opacity 1.1s ease;
  }
  .slide.active {
    opacity: 1;
  }
  .slide img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
    transform-origin: 40% 40%;
    animation: pan-a calc(var(--slide-ms) + 1200ms) linear forwards;
  }
  .slide.even img {
    transform-origin: 60% 55%;
    animation-name: pan-b;
  }
  @keyframes pan-a {
    from { transform: scale(1.04) translate(0, 0); }
    to { transform: scale(1.16) translate(-2%, -1.5%); }
  }
  @keyframes pan-b {
    from { transform: scale(1.16) translate(1.5%, 1%); }
    to { transform: scale(1.04) translate(0, 0); }
  }

  /* soft shade so the caption and bars read on any map */
  .frame::after {
    content: '';
    position: absolute;
    z-index: 2;
    inset: 0;
    pointer-events: none;
    background:
      linear-gradient(to bottom, rgba(23, 22, 19, 0.28), transparent 18%),
      linear-gradient(to top, rgba(23, 22, 19, 0.55), transparent 34%);
  }

  .bars {
    position: absolute;
    z-index: 3;
    top: 14px;
    left: 14px;
    right: 14px;
    display: flex;
    gap: 5px;
  }
  .bar {
    flex: 1;
    height: 14px;
    padding: 6px 0;
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

  .caption {
    position: absolute;
    z-index: 3;
    left: 18px;
    right: 18px;
    bottom: 16px;
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 12px;
    color: #fff;
  }
  .caption-inner {
    display: flex;
    flex-direction: column;
    gap: 2px;
    animation: rise 0.6s ease both;
  }
  @keyframes rise {
    from { opacity: 0; transform: translateY(6px); }
  }
  .place {
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 700;
    font-size: 1.05rem;
    letter-spacing: 0.01em;
    text-shadow: 0 1px 8px rgba(0, 0, 0, 0.3);
  }
  .sheet {
    font-size: 0.78rem;
    opacity: 0.85;
  }
  .count {
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 0.72rem;
    opacity: 0.8;
    white-space: nowrap;
  }

  .credit {
    margin: 22px 0 0;
    text-align: center;
    font-size: 0.72rem;
    color: var(--muted);
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
