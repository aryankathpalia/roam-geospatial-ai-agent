<script lang="ts">
  import { onMount, onDestroy } from 'svelte';

  // The landing hero's right-hand animation, on a real ROAM result: the
  // four-parcel Washoe County plat (sample b4c6b8bf, page 12). The scan is
  // read, the parcels are traced on it, then they lift off the paper and
  // land where ROAM placed them on the satellite image. Geometry below is
  // the stored result, in a 240 x 240 flat frame for each image.
  const parcels = [
    { label: 'Parcel 1', acres: 9.98, colour: '#ff2d55',
      plat: [[12.9, 184.1], [117.9, 56.1], [147.0, 127.3], [35.4, 180.4], [22.3, 193.6]],
      map: [[45.4, 165.9], [118.1, 75.7], [138.9, 125.3], [61.2, 163.1], [52.1, 172.5]] },
    { label: 'Parcel 2', acres: 10.33, colour: '#ffcc00',
      plat: [[117.8, 55.8], [130.6, 40.0], [227.1, 42.4], [226.6, 78.8], [147.2, 127.6]],
      map: [[118.0, 75.5], [126.8, 64.4], [194.4, 65.5], [194.2, 90.9], [139.0, 125.5]] },
    { label: 'Parcel 3', acres: 10.1, colour: '#00e5ff',
      plat: [[147.2, 127.4], [226.5, 79.4], [224.7, 200.0], [183.6, 198.9]],
      map: [[139.0, 125.3], [194.2, 91.3], [193.7, 175.7], [164.9, 175.2]] },
    { label: 'Parcel 4', acres: 10.07, colour: '#7cff4f',
      plat: [[35.5, 180.1], [147.1, 127.9], [183.8, 198.9], [24.4, 195.4], [22.3, 193.4]],
      map: [[61.3, 162.9], [138.9, 125.7], [165.0, 175.2], [53.6, 173.6], [52.1, 172.3]] }
  ];
  const totalAcres = parcels.reduce((s, p) => s + p.acres, 0).toFixed(2);

  const SHEET_Y = 8; // where the flat frames sit, in screen units
  const MAP_Y = 196;
  const LOOP = 12; // seconds

  // flat (x, y) on a plane at height ty -> isometric screen point
  const iso = (x: number, y: number, ty: number) => [260 + (x - y) * 0.866, ty + (x + y) * 0.5];
  const ISO = (ty: number) => `matrix(0.866 0.5 -0.866 0.5 260 ${ty})`;

  const clamp = (v: number) => Math.min(1, Math.max(0, v));
  const ramp = (t: number, a: number, b: number) => clamp((t - a) / (b - a));
  const ease = (v: number) => (v < 0.5 ? 4 * v * v * v : 1 - Math.pow(-2 * v + 2, 3) / 2);
  const win = (t: number, a: number, b: number, fade = 0.35) => Math.min(ramp(t, a, a + fade), 1 - ramp(t, b - fade, b));

  let t = 0;
  let raf = 0;
  let still = false;

  onMount(() => {
    still = matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (still) {
      t = 10;
      return;
    }
    const start = performance.now();
    const tick = (now: number) => {
      t = ((now - start) / 1000) % LOOP;
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
  });
  onDestroy(() => cancelAnimationFrame(raf));

  // ---- the timeline
  $: fadeAll = 1 - ramp(t, 11.3, 11.95);
  $: sheetIn = ease(ramp(t, 0, 0.8));
  $: sheetOpacity = sheetIn * (1 - 0.85 * ramp(t, 6.0, 7.2));
  $: scanY = ramp(t, 0.8, 2.6) * 240;
  $: scanOn = win(t, 0.8, 2.75, 0.15);
  $: boxDraw = ramp(t, 1.5, 2.4);
  $: boxOn = 1 - ramp(t, 5.6, 6.2);
  $: lift = ease(ramp(t, 6.0, 7.9));
  $: shapes = parcels.map((p, i) => {
    const trace = ramp(t, 2.7 + i * 0.45, 3.6 + i * 0.45);
    const ty = SHEET_Y + (MAP_Y - SHEET_Y) * lift;
    const pts = p.plat.map(([x, y], k) => {
      const [mx, my] = p.map[k];
      return iso(x + (mx - x) * lift, y + (my - y) * lift, ty);
    });
    const cx = pts.reduce((s, q) => s + q[0], 0) / pts.length;
    const cy = pts.reduce((s, q) => s + q[1], 0) / pts.length;
    return {
      ...p,
      points: pts.map((q) => q.join(',')).join(' '),
      pts,
      cx,
      cy,
      trace,
      fill: ramp(t, 3.4 + i * 0.45, 4.0 + i * 0.45),
      dots: ramp(t, 3.4 + i * 0.45, 3.7 + i * 0.45)
    };
  });
  $: landed = ramp(t, 7.7, 8.2);
  $: pulse = ramp(t, 8.0, 9.4);
  $: steps = [
    { n: '01', name: 'Read', on: t < 2.7 },
    { n: '02', name: 'Trace', on: t >= 2.7 && t < 6.0 },
    { n: '03', name: 'Place', on: t >= 6.0 && t < 8.0 },
    { n: '04', name: 'Check', on: t >= 8.0 }
  ];
</script>

<div class="hero-anim" aria-hidden="true" style="opacity: {fadeAll}">
  <svg viewBox="30 0 460 452" role="presentation">
    <defs>
      <clipPath id="ha-flat"><rect width="240" height="240" rx="10" /></clipPath>
      <linearGradient id="ha-scan" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#d9581f" stop-opacity="0" />
        <stop offset="1" stop-color="#d9581f" stop-opacity="0.22" />
      </linearGradient>
      <filter id="ha-soft" x="-30%" y="-30%" width="160%" height="160%">
        <feGaussianBlur stdDeviation="14" />
      </filter>
      <filter id="ha-glow" x="-10%" y="-300%" width="120%" height="700%">
        <feGaussianBlur stdDeviation="2.4" />
      </filter>
    </defs>

    <!-- ground shadow -->
    <ellipse cx="260" cy="{MAP_Y + 138}" rx="196" ry="70" fill="#171613" opacity="0.13" filter="url(#ha-soft)" />

    <!-- the satellite tile -->
    <g transform={ISO(MAP_Y + 5)}>
      <rect width="240" height="240" rx="10" fill="#4a463d" />
    </g>
    <g transform={ISO(MAP_Y)}>
      <image href="/images/hero/sat.jpg" width="240" height="240" clip-path="url(#ha-flat)" preserveAspectRatio="xMidYMid slice" />
      <rect width="240" height="240" rx="10" fill="#171613" opacity={0.22 * (1 - landed)} />
      <rect width="240" height="240" rx="10" fill="none" stroke="#ffffff" stroke-opacity="0.55" stroke-width="1" vector-effect="non-scaling-stroke" />
    </g>

    <!-- the scanned plat -->
    <g style="opacity: {sheetOpacity}; transform: translateY({(1 - sheetIn) * -12}px)">
      <g transform={ISO(SHEET_Y + 5)}>
        <rect width="240" height="240" rx="10" fill="#d8d1c2" />
      </g>
      <g transform={ISO(SHEET_Y)}>
        <rect width="240" height="240" rx="10" fill="#fffdf8" />
        <image href="/images/hero/plat.jpg" width="240" height="240" clip-path="url(#ha-flat)" />
        <rect width="240" height="240" rx="10" fill="none" stroke="#cfc7b6" stroke-width="0.8" vector-effect="non-scaling-stroke" />
        <!-- layout detection: the ParcelMap region -->
        <rect
          x="8" y="13" width="224" height="219" rx="3"
          fill="#d9581f" fill-opacity={0.05 * boxDraw * boxOn}
          stroke="#d9581f" stroke-width="1.6" vector-effect="non-scaling-stroke"
          pathLength="1" stroke-dasharray="1" stroke-dashoffset={1 - boxDraw} opacity={boxOn}
        />
        <!-- scanner sweep -->
        <g style="opacity: {scanOn}" transform="translate(0 {scanY})" clip-path="url(#ha-flat)">
          <rect x="0" y="-34" width="240" height="34" fill="url(#ha-scan)" />
          <line x1="0" y1="0" x2="240" y2="0" stroke="#ff8a4c" stroke-width="3.5" filter="url(#ha-glow)" vector-effect="non-scaling-stroke" />
          <line x1="0" y1="0" x2="240" y2="0" stroke="#d9581f" stroke-width="1.2" vector-effect="non-scaling-stroke" />
        </g>
      </g>
    </g>

    <!-- the parcels: traced on the plat, then lifted onto the map -->
    {#each shapes as s}
      <polygon points={s.points} fill={s.colour} fill-opacity={0.3 * s.fill} />
      <polygon
        points={s.points} fill="none" stroke={s.colour} stroke-width="2" stroke-linejoin="round"
        pathLength="1" stroke-dasharray="1" stroke-dashoffset={1 - s.trace} opacity={s.trace > 0 ? 1 : 0}
      />
      {#each s.pts as [x, y]}
        <circle cx={x} cy={y} r={2.6 * s.dots} fill="#fff" stroke={s.colour} stroke-width="1.4" />
      {/each}
    {/each}

    <!-- landing pulse -->
    {#if pulse > 0 && pulse < 1}
      <ellipse cx="260" cy="{MAP_Y + 120}" rx={60 + 150 * pulse} ry={(60 + 150 * pulse) * 0.5}
        fill="none" stroke="#ffffff" stroke-width="1.5" opacity={0.7 * (1 - pulse)} />
    {/if}
  </svg>

  <!-- floating cards, like the app's own panels -->
  <div class="card c1" style="opacity: {win(t, 1.9, 5.8)}; transform: translateY({(1 - ramp(t, 1.9, 2.3)) * 8}px)">
    <span class="k">Layout detection</span>
    <span class="v"><i class="sw" style="background:#d9581f"></i>ParcelMap · 0.97</span>
  </div>
  <div class="card c2" style="opacity: {win(t, 4.3, 6.5)}; transform: translateY({(1 - ramp(t, 4.3, 4.7)) * 8}px)">
    <span class="k">Boundaries traced</span>
    <span class="v">4 parcels · {totalAcres} ac</span>
    <span class="rows">
      {#each parcels as p}
        <span><i class="sw" style="background:{p.colour}"></i>{p.label}<b>{p.acres.toFixed(2)} ac</b></span>
      {/each}
    </span>
  </div>
  <div class="card c3" style="opacity: {win(t, 8.1, 11.4)}; transform: translateY({(1 - ramp(t, 8.1, 8.5)) * 8}px)">
    <span class="k">Placed on the map</span>
    <span class="v"><i class="dot"></i>Fitted to county parcels</span>
    <span class="sub">Washoe County, NV · closure 0.00 ft</span>
  </div>

  <ol class="steps">
    {#each steps as s}
      <li class:on={s.on}><span>{s.n}</span>{s.name}</li>
    {/each}
  </ol>
</div>

<style>
  .hero-anim {
    position: relative;
    width: 100%;
    max-width: 400px;
    margin: 0 auto;
  }
  svg {
    display: block;
    width: 100%;
    height: auto;
    overflow: visible;
  }

  .card {
    position: absolute;
    display: flex;
    flex-direction: column;
    gap: 3px;
    padding: 9px 12px;
    border-radius: 10px;
    background: rgba(255, 255, 255, 0.9);
    backdrop-filter: blur(8px);
    border: 1px solid rgba(23, 22, 19, 0.08);
    box-shadow: 0 12px 28px -12px rgba(23, 22, 19, 0.35);
    white-space: nowrap;
    pointer-events: none;
  }
  .c1 { left: -17%; top: 8%; }
  .c2 { left: -19%; top: 52%; }
  .c3 { right: -14%; top: 8%; }
  .k {
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .v {
    display: flex;
    align-items: center;
    gap: 6px;
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 700;
    font-size: 0.86rem;
    color: var(--ink);
  }
  .sub {
    font-size: 0.7rem;
    color: var(--muted);
  }
  .rows {
    display: grid;
    gap: 2px;
    margin-top: 3px;
    font-size: 0.7rem;
    color: var(--muted);
  }
  .rows span {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .rows b {
    margin-left: auto;
    padding-left: 14px;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-weight: 500;
    color: var(--ink);
  }
  .sw {
    width: 8px;
    height: 8px;
    border-radius: 2px;
    flex: none;
  }
  .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #2f9e5b;
    box-shadow: 0 0 0 3px rgba(47, 158, 91, 0.18);
  }

  .steps {
    display: flex;
    justify-content: center;
    gap: 6px;
    margin: 4px 0 0;
    padding: 0;
    list-style: none;
  }
  .steps li {
    display: flex;
    align-items: center;
    gap: 5px;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 0.7rem;
    font-weight: 600;
    color: var(--muted);
    border: 1px solid transparent;
    transition: all 0.35s ease;
  }
  .steps li span {
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 0.64rem;
    opacity: 0.6;
  }
  .steps li.on {
    color: var(--ink);
    background: #fff;
    border-color: var(--line);
    box-shadow: 0 4px 12px -6px rgba(23, 22, 19, 0.25);
  }
  .steps li.on span {
    color: var(--accent);
    opacity: 1;
  }
</style>
