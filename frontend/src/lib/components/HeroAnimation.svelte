<script lang="ts">
  // The landing hero's right-hand animation, one 10 s loop of what ROAM does:
  // a scanned plat is read, the parcel is traced, it drops onto the map
  // layers, and it gets verified. Everything is drawn flat (a 240 x 240
  // sheet) and tilted into an isometric view with one matrix.
  const ISO = (ty: number) => `matrix(0.866 0.5 -0.866 0.5 260 ${ty})`;

  const parcel: [number, number][] = [
    [70, 62],
    [176, 48],
    [194, 150],
    [122, 188],
    [52, 138]
  ];
  const parcelPoints = parcel.map((p) => p.join(',')).join(' ');
  const grid = [30, 60, 90, 120, 150, 180, 210];
</script>

<div class="hero-anim" aria-hidden="true">
  <svg viewBox="0 0 520 500" role="presentation">
    <defs>
      <linearGradient id="ha-top" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#fff3e8" />
        <stop offset="0.55" stop-color="#fbcfa9" />
        <stop offset="1" stop-color="#f4a36b" />
      </linearGradient>
      <linearGradient id="ha-bottom" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#f6a46a" />
        <stop offset="1" stop-color="#d9581f" />
      </linearGradient>
      <linearGradient id="ha-rim" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#e98a52" />
        <stop offset="1" stop-color="#b9471a" />
      </linearGradient>
      <linearGradient id="ha-paper" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#ffffff" />
        <stop offset="1" stop-color="#f6f1e7" />
      </linearGradient>
      <linearGradient id="ha-parcel" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#f08a4b" stop-opacity="0.38" />
        <stop offset="1" stop-color="#d9581f" stop-opacity="0.22" />
      </linearGradient>
      <linearGradient id="ha-scan" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#d9581f" stop-opacity="0" />
        <stop offset="1" stop-color="#d9581f" stop-opacity="0.16" />
      </linearGradient>
      <filter id="ha-soft" x="-30%" y="-30%" width="160%" height="160%">
        <feGaussianBlur stdDeviation="16" />
      </filter>
      <filter id="ha-glow" x="-10%" y="-200%" width="120%" height="500%">
        <feGaussianBlur stdDeviation="2.2" />
      </filter>
      <filter id="ha-card" x="-30%" y="-40%" width="160%" height="200%">
        <feDropShadow dx="0" dy="6" stdDeviation="8" flood-color="#171613" flood-opacity="0.14" />
      </filter>
    </defs>

    <!-- ground shadow -->
    <ellipse cx="260" cy="396" rx="190" ry="78" fill="#171613" opacity="0.10" filter="url(#ha-soft)" />

    <g class="float">
      <!-- bottom layer: terrain -->
      <g transform={ISO(258)}>
        <rect width="240" height="240" rx="12" fill="url(#ha-rim)" />
      </g>
      <g transform={ISO(253)}>
        <rect width="240" height="240" rx="12" fill="url(#ha-bottom)" />
        <g fill="none" stroke="#fff4ea" stroke-opacity="0.45" stroke-width="0.9">
          <path d="M14 52 C64 26 118 78 168 46 S226 66 234 58" vector-effect="non-scaling-stroke" />
          <path d="M8 96 C58 70 112 120 162 92 S222 104 236 98" vector-effect="non-scaling-stroke" />
          <path d="M8 140 C62 112 116 162 166 134 S224 146 236 140" vector-effect="non-scaling-stroke" />
          <path d="M10 186 C70 156 120 206 174 176 S226 190 234 184" vector-effect="non-scaling-stroke" />
        </g>
      </g>

      <!-- top layer: the map -->
      <g transform={ISO(232)}>
        <rect width="240" height="240" rx="12" fill="#e7a074" opacity="0.9" />
      </g>
      <g transform={ISO(228)}>
        <rect width="240" height="240" rx="12" fill="url(#ha-top)" opacity="0.96" />
        <g stroke="#ffffff" stroke-opacity="0.55" stroke-width="0.7">
          {#each grid as v}
            <line x1={v} y1="4" x2={v} y2="236" vector-effect="non-scaling-stroke" />
            <line x1="4" y1={v} x2="236" y2={v} vector-effect="non-scaling-stroke" />
          {/each}
        </g>
        <path d="M0 22 C80 32 150 12 240 26" fill="none" stroke="#ffffff" stroke-width="3" stroke-linecap="round" vector-effect="non-scaling-stroke" />
        <path d="M214 0 C204 90 226 160 212 240" fill="none" stroke="#ffffff" stroke-width="2.4" stroke-linecap="round" vector-effect="non-scaling-stroke" />
        <rect width="240" height="240" rx="12" fill="none" stroke="#ffffff" stroke-opacity="0.8" stroke-width="1" vector-effect="non-scaling-stroke" />
      </g>

      <!-- guides from the sheet down to the map while the parcel drops -->
      <g class="guides" stroke="#d9581f" stroke-width="1" stroke-dasharray="3 4" opacity="0">
        <line x1="52" y1="140" x2="52" y2="348" />
        <line x1="468" y1="140" x2="468" y2="348" />
      </g>

      <!-- the scanned plat sheet -->
      <g class="sheet">
        <g transform={ISO(20)}>
          <rect width="240" height="240" rx="4" fill="url(#ha-paper)" stroke="#d9d2c3" stroke-width="0.8" vector-effect="non-scaling-stroke" />
          <rect x="8" y="8" width="224" height="224" rx="2" fill="none" stroke="#e4ded1" stroke-width="0.6" vector-effect="non-scaling-stroke" />
          <g stroke="#bdb5a4" stroke-width="0.7" stroke-dasharray="3 3" fill="none">
            <line x1="176" y1="48" x2="180" y2="10" vector-effect="non-scaling-stroke" />
            <line x1="194" y1="150" x2="230" y2="158" vector-effect="non-scaling-stroke" />
            <line x1="52" y1="138" x2="10" y2="132" vector-effect="non-scaling-stroke" />
            <line x1="122" y1="188" x2="128" y2="230" vector-effect="non-scaling-stroke" />
            <line x1="70" y1="62" x2="22" y2="12" vector-effect="non-scaling-stroke" />
          </g>
          <polygon points={parcelPoints} fill="none" stroke="#4a463c" stroke-width="1" vector-effect="non-scaling-stroke" />
          <!-- bearings, distances and the area label -->
          <g fill="#a39c8b">
            <rect x="106" y="42" width="38" height="2.2" rx="1.1" />
            <rect x="188" y="94" width="28" height="2.2" rx="1.1" />
            <rect x="150" y="174" width="26" height="2.2" rx="1.1" />
            <rect x="72" y="166" width="24" height="2.2" rx="1.1" />
            <rect x="34" y="94" width="24" height="2.2" rx="1.1" />
          </g>
          <g fill="#6f695c">
            <rect x="102" y="112" width="40" height="3" rx="1.5" />
            <rect x="110" y="120" width="24" height="2.2" rx="1.1" />
          </g>
          <!-- north arrow and title block -->
          <path d="M28 196 l6 -16 l6 16 l-6 -4 z" fill="#8d8676" />
          <rect x="170" y="198" width="58" height="30" rx="2" fill="none" stroke="#cfc8b8" stroke-width="0.7" vector-effect="non-scaling-stroke" />
          <g fill="#cfc8b8">
            <rect x="176" y="204" width="40" height="2" rx="1" />
            <rect x="176" y="211" width="30" height="2" rx="1" />
            <rect x="176" y="218" width="36" height="2" rx="1" />
          </g>
          <!-- scanner sweep -->
          <g class="scan">
            <rect x="0" y="-30" width="240" height="30" fill="url(#ha-scan)" />
            <line x1="0" y1="0" x2="240" y2="0" stroke="#ff8a4c" stroke-width="3" filter="url(#ha-glow)" vector-effect="non-scaling-stroke" />
            <line x1="0" y1="0" x2="240" y2="0" stroke="#d9581f" stroke-width="1" vector-effect="non-scaling-stroke" />
          </g>
        </g>
      </g>

      <!-- the traced parcel: drawn on the sheet, then dropped onto the map -->
      <g class="drop">
        <g transform={ISO(20)}>
          <polygon class="fill" points={parcelPoints} fill="url(#ha-parcel)" />
          <polygon class="trace" points={parcelPoints} pathLength="1" fill="none" stroke="#d9581f" stroke-width="1.8" stroke-linejoin="round" vector-effect="non-scaling-stroke" />
        </g>
        {#each parcel as [x, y], i}
          <circle
            class="corner"
            style="animation-delay: {i * 0.16}s"
            cx={260 + (x - y) * 0.866}
            cy={20 + (x + y) * 0.5}
            r="3.4"
            fill="#ffffff"
            stroke="#d9581f"
            stroke-width="1.5"
          />
        {/each}
      </g>

      <!-- result card -->
      <g class="result">
        <circle class="ring" cx="266" cy="348" r="6" fill="none" stroke="#2f9e5b" stroke-width="1.5" />
        <circle cx="266" cy="348" r="4.5" fill="#2f9e5b" stroke="#ffffff" stroke-width="1.5" />
        <line x1="270" y1="344" x2="300" y2="310" stroke="#2f9e5b" stroke-width="1" />
        <g filter="url(#ha-card)">
          <rect x="300" y="276" width="150" height="50" rx="9" fill="#ffffff" />
        </g>
        <rect x="300" y="276" width="150" height="50" rx="9" fill="none" stroke="#171613" stroke-opacity="0.08" />
        <circle cx="318" cy="294" r="7" fill="#2f9e5b" />
        <path d="M314.6 294.2 l2.4 2.4 l4.4 -4.8" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
        <text x="331" y="298" class="card-title">Verified</text>
        <text x="312" y="316" class="card-sub">Closure 1:12,400 · 2 sources</text>
      </g>
    </g>
  </svg>

  <div class="status">
    <span class="dot"></span>
    <span class="steps">
      <span class="s1">Reading the plat</span>
      <span class="s2">Tracing the boundary</span>
      <span class="s3">Placing on the map</span>
      <span class="s4">Location verified</span>
    </span>
  </div>
</div>

<style>
  .hero-anim {
    position: relative;
    width: 100%;
    max-width: 480px;
    margin: 0 auto;
  }
  svg {
    display: block;
    width: 100%;
    height: auto;
    overflow: visible;
  }
  .card-title {
    font: 700 12.5px 'Space Grotesk', system-ui, sans-serif;
    fill: #171613;
    letter-spacing: 0.01em;
  }
  .card-sub {
    font: 500 10px 'Manrope', system-ui, sans-serif;
    fill: #6b6a63;
  }

  /* one shared 10 s loop; each part keys its own window of it */
  .sheet,
  .scan,
  .drop,
  .trace,
  .fill,
  .corner,
  .guides,
  .result,
  .ring,
  .steps span,
  .dot {
    animation-duration: 10s;
    animation-iteration-count: infinite;
    animation-timing-function: cubic-bezier(0.45, 0, 0.2, 1);
  }

  .float {
    animation: float 6s ease-in-out infinite;
  }
  @keyframes float {
    0%, 100% { transform: translateY(0); }
    50% { transform: translateY(-5px); }
  }

  .sheet { animation-name: sheet; }
  @keyframes sheet {
    0% { opacity: 0; transform: translateY(-10px); }
    7%, 52% { opacity: 1; transform: translateY(0); }
    66%, 92% { opacity: 0.16; transform: translateY(0); }
    100% { opacity: 0; transform: translateY(0); }
  }

  .scan { animation-name: scan; animation-timing-function: linear; }
  @keyframes scan {
    0%, 7% { transform: translateY(0); opacity: 0; }
    9% { opacity: 1; }
    27% { transform: translateY(240px); opacity: 1; }
    30%, 100% { transform: translateY(240px); opacity: 0; }
  }

  .trace { stroke-dasharray: 1; animation-name: trace; }
  @keyframes trace {
    0%, 29% { stroke-dashoffset: 1; opacity: 1; }
    45%, 94% { stroke-dashoffset: 0; opacity: 1; }
    100% { stroke-dashoffset: 0; opacity: 0; }
  }

  .fill { animation-name: fill; }
  @keyframes fill {
    0%, 43% { opacity: 0; }
    51%, 94% { opacity: 1; }
    100% { opacity: 0; }
  }

  .corner {
    transform-box: fill-box;
    transform-origin: center;
    animation-name: corner;
  }
  @keyframes corner {
    0%, 30% { transform: scale(0); opacity: 0; }
    33% { transform: scale(1.3); opacity: 1; }
    36%, 94% { transform: scale(1); opacity: 1; }
    100% { transform: scale(1); opacity: 0; }
  }

  .drop { animation-name: drop; }
  @keyframes drop {
    0%, 53% { transform: translateY(0); }
    67%, 100% { transform: translateY(208px); }
  }

  .guides { animation-name: guides; }
  @keyframes guides {
    0%, 50% { opacity: 0; }
    56%, 64% { opacity: 0.45; }
    72%, 100% { opacity: 0; }
  }

  .result { animation-name: result; }
  @keyframes result {
    0%, 69% { opacity: 0; transform: translateY(6px); }
    75%, 93% { opacity: 1; transform: translateY(0); }
    100% { opacity: 0; transform: translateY(0); }
  }

  .ring {
    transform-box: fill-box;
    transform-origin: center;
    animation-name: ring;
    animation-timing-function: ease-out;
  }
  @keyframes ring {
    0%, 70% { transform: scale(1); opacity: 0; }
    72% { opacity: 0.9; }
    84%, 100% { transform: scale(3.2); opacity: 0; }
  }

  .status {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 9px;
    height: 22px;
    margin-top: 6px;
  }
  .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent);
    animation-name: dot;
  }
  @keyframes dot {
    0%, 69% { background: #d9581f; box-shadow: 0 0 0 3px rgba(217, 88, 31, 0.15); }
    73%, 100% { background: #2f9e5b; box-shadow: 0 0 0 3px rgba(47, 158, 91, 0.15); }
  }
  .steps {
    position: relative;
    width: 170px;
    height: 16px;
  }
  .steps span {
    position: absolute;
    left: 0;
    top: 0;
    white-space: nowrap;
    opacity: 0;
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .s1 { animation-name: s1; }
  .s2 { animation-name: s2; }
  .s3 { animation-name: s3; }
  .s4 { animation-name: s4; }
  @keyframes s1 { 0%, 3% { opacity: 0; } 7%, 25% { opacity: 1; } 29%, 100% { opacity: 0; } }
  @keyframes s2 { 0%, 29% { opacity: 0; } 32%, 49% { opacity: 1; } 53%, 100% { opacity: 0; } }
  @keyframes s3 { 0%, 53% { opacity: 0; } 56%, 66% { opacity: 1; } 70%, 100% { opacity: 0; } }
  @keyframes s4 { 0%, 70% { opacity: 0; } 74%, 92% { opacity: 1; } 98%, 100% { opacity: 0; } }

  /* no motion: show the finished state */
  @media (prefers-reduced-motion: reduce) {
    .float, .sheet, .scan, .drop, .trace, .fill, .corner, .guides, .result, .ring, .steps span, .dot {
      animation: none;
    }
    .sheet { opacity: 0.16; }
    .scan, .ring, .guides, .s1, .s2, .s3 { opacity: 0; }
    .drop { transform: translateY(208px); }
    .s4 { opacity: 1; }
    .dot { background: #2f9e5b; }
  }
</style>
