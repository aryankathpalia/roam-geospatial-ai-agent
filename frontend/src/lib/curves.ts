// Curved edges for the manual boundary drawing.
//
// An edge i runs from vertex i to vertex i+1. A curved edge carries WAYPOINTS: points the user places
// ON the curve, and the edge is a smooth (Catmull-Rom) spline through its two corners and every
// waypoint, in order. One waypoint bends an edge once; each further waypoint adds another bend, so an
// S-shaped road frontage or a long arc that wanders is just a few more points.
//
// Waypoints are stored in the edge's own frame -- (a, b): a fraction of the chord along it, b a fraction
// of the chord length perpendicular to it -- so the curve keeps its proportions when either corner is
// dragged or the crop is rescaled.

export type Pt = [number, number];
export type Curves = Record<number, Pt[]>;

const STRAIGHT = 0.003; // a lone waypoint with |b| below this reads as a straight edge
const MAX_WAYPOINTS = 24;

const sub = (a: Pt, b: Pt): Pt => [a[0] - b[0], a[1] - b[1]];

export function fromChordFrame(p0: Pt, p1: Pt, [a, b]: Pt): Pt {
  const dx = p1[0] - p0[0];
  const dy = p1[1] - p0[1];
  return [p0[0] + a * dx - b * dy, p0[1] + a * dy + b * dx];
}

export function toChordFrame(p0: Pt, p1: Pt, c: Pt): Pt {
  const [dx, dy] = sub(p1, p0);
  const len2 = dx * dx + dy * dy || 1;
  const [rx, ry] = sub(c, p0);
  return [(rx * dx + ry * dy) / len2, (-rx * dy + ry * dx) / len2];
}

// Saved drawings from before waypoints stored ONE quadratic control point [a, b] per edge; the curve's
// midpoint (its apex) is the single waypoint that reproduces it closely.
export function migrateCurves(raw: unknown): Curves {
  const out: Curves = {};
  if (!raw || typeof raw !== 'object') return out;
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (!Array.isArray(v) || v.length === 0) continue;
    if (typeof v[0] === 'number') {
      const [a, b] = v as number[];
      out[Number(k)] = [[0.5 * a + 0.25, 0.5 * b]];
    } else {
      out[Number(k)] = (v as Pt[]).map(([a, b]) => [a, b] as Pt);
    }
  }
  return out;
}

// Corners and waypoints of edge i, in order, in crop pixels.
function anchors(p0: Pt, p1: Pt, ws: Pt[] | undefined): Pt[] {
  return [p0, ...(ws ?? []).map((w) => fromChordFrame(p0, p1, w)), p1];
}

type Cubic = [Pt, Pt, Pt, Pt];

// Catmull-Rom through the anchors, as one cubic Bezier per span.
function cubics(pts: Pt[]): Cubic[] {
  const out: Cubic[] = [];
  for (let i = 0; i < pts.length - 1; i++) {
    const before = pts[i - 1] ?? pts[i];
    const after = pts[i + 2] ?? pts[i + 1];
    out.push([
      pts[i],
      [pts[i][0] + (pts[i + 1][0] - before[0]) / 6, pts[i][1] + (pts[i + 1][1] - before[1]) / 6],
      [pts[i + 1][0] - (after[0] - pts[i][0]) / 6, pts[i + 1][1] - (after[1] - pts[i][1]) / 6],
      pts[i + 1]
    ]);
  }
  return out;
}

function cubicAt([p0, c1, c2, p1]: Cubic, t: number): Pt {
  const u = 1 - t;
  const w0 = u * u * u;
  const w1 = 3 * u * u * t;
  const w2 = 3 * u * t * t;
  const w3 = t * t * t;
  return [w0 * p0[0] + w1 * c1[0] + w2 * c2[0] + w3 * p1[0], w0 * p0[1] + w1 * c1[1] + w2 * c2[1] + w3 * p1[1]];
}

// The edge as a dense polyline (about one sample per 12 crop px, 6 to 40 per span): used for the
// stored polygon, hit-testing and the edge's midpoint.
function sample(p0: Pt, p1: Pt, ws: Pt[] | undefined): Pt[] {
  if (!ws || ws.length === 0) return [p0, p1];
  const pts: Pt[] = [p0];
  for (const seg of cubics(anchors(p0, p1, ws))) {
    const len = Math.hypot(seg[1][0] - seg[0][0], seg[1][1] - seg[0][1]) +
      Math.hypot(seg[2][0] - seg[1][0], seg[2][1] - seg[1][1]) +
      Math.hypot(seg[3][0] - seg[2][0], seg[3][1] - seg[2][1]);
    const n = Math.max(6, Math.min(40, Math.round(len / 12)));
    for (let k = 1; k <= n; k++) pts.push(cubicAt(seg, k / n));
  }
  return pts;
}

function segmentPath(p0: Pt, p1: Pt, ws: Pt[] | undefined): string {
  if (!ws || ws.length === 0) return ` L ${p1[0]} ${p1[1]}`;
  return cubics(anchors(p0, p1, ws))
    .map(([, c1, c2, e]) => ` C ${c1[0]} ${c1[1]} ${c2[0]} ${c2[1]} ${e[0]} ${e[1]}`)
    .join('');
}

// SVG path for one edge / for the whole outline.
export function edgePath(vertices: Pt[], curves: Curves, i: number): string {
  const p0 = vertices[i];
  const p1 = vertices[(i + 1) % vertices.length];
  return `M ${p0[0]} ${p0[1]}` + segmentPath(p0, p1, curves[i]);
}

export function outlinePath(vertices: Pt[], curves: Curves): string {
  if (vertices.length === 0) return '';
  let d = `M ${vertices[0][0]} ${vertices[0][1]}`;
  for (let i = 0; i < vertices.length; i++) {
    d += segmentPath(vertices[i], vertices[(i + 1) % vertices.length], curves[i]);
  }
  return d + ' Z';
}

// The vertex list the backend gets: every corner, plus points sampled along each curved edge, so the
// stored polygon (its area, its ring on the map) follows the curve. `cornerIndices` marks which
// entries are the user's own corners.
export function flatten(vertices: Pt[], curves: Curves): { points: Pt[]; cornerIndices: number[] } {
  const points: Pt[] = [];
  const cornerIndices: number[] = [];
  for (let i = 0; i < vertices.length; i++) {
    cornerIndices.push(points.length);
    const dense = sample(vertices[i], vertices[(i + 1) % vertices.length], curves[i]);
    points.push(...dense.slice(0, -1)); // the next edge starts at this edge's end
  }
  return { points, cornerIndices };
}

// Absolute positions of an edge's waypoints (for the drag handles).
export function waypointPoints(vertices: Pt[], curves: Curves, i: number): Pt[] {
  const p0 = vertices[i];
  const p1 = vertices[(i + 1) % vertices.length];
  return (curves[i] ?? []).map((w) => fromChordFrame(p0, p1, w));
}

// Where the "select this edge" handle sits: halfway along the drawn edge.
export function midpointOf(p0: Pt, p1: Pt, ws: Pt[] | undefined): Pt {
  const dense = sample(p0, p1, ws);
  const lens: number[] = [0];
  for (let k = 1; k < dense.length; k++) {
    lens.push(lens[k - 1] + Math.hypot(dense[k][0] - dense[k - 1][0], dense[k][1] - dense[k - 1][1]));
  }
  const half = lens[lens.length - 1] / 2;
  for (let k = 1; k < dense.length; k++) {
    if (lens[k] >= half) {
      const f = (half - lens[k - 1]) / (lens[k] - lens[k - 1] || 1);
      return [dense[k - 1][0] + f * (dense[k][0] - dense[k - 1][0]), dense[k - 1][1] + f * (dense[k][1] - dense[k - 1][1])];
    }
  }
  return dense[dense.length - 1];
}

function nearestOnEdge(p0: Pt, p1: Pt, ws: Pt[] | undefined, p: Pt): Pt {
  let best = p0;
  let bestD = Infinity;
  const dense = sample(p0, p1, ws);
  for (let k = 0; k < dense.length - 1; k++) {
    // nearest point on each little segment, so a coarse sampling still lands exactly on the line
    const [x0, y0] = dense[k];
    const [dx, dy] = sub(dense[k + 1], dense[k]);
    const t = Math.max(0, Math.min(1, ((p[0] - x0) * dx + (p[1] - y0) * dy) / (dx * dx + dy * dy || 1)));
    const q: Pt = [x0 + t * dx, y0 + t * dy];
    const d = (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2;
    if (d < bestD) {
      bestD = d;
      best = q;
    }
  }
  return best;
}

function tidy(ws: Pt[]): Pt[] | undefined {
  if (ws.length === 0) return undefined;
  if (ws.length === 1 && Math.abs(ws[0][1]) < STRAIGHT) return undefined;
  return ws;
}

function withEdge(curves: Curves, i: number, ws: Pt[] | undefined): Curves {
  const next = { ...curves };
  if (ws) next[i] = ws;
  else delete next[i];
  return next;
}

// Adds a waypoint on edge i at the point of the edge nearest to `p`, keeping the waypoints ordered
// along the chord. Returns the new curves and the new waypoint's index.
export function addWaypoint(vertices: Pt[], curves: Curves, i: number, p: Pt): { curves: Curves; index: number } {
  const p0 = vertices[i];
  const p1 = vertices[(i + 1) % vertices.length];
  const existing = curves[i] ?? [];
  if (existing.length >= MAX_WAYPOINTS) return { curves, index: -1 };
  const w = toChordFrame(p0, p1, nearestOnEdge(p0, p1, existing, p));
  const ws = [...existing, w].sort((x, y) => x[0] - y[0]);
  return { curves: withEdge(curves, i, ws), index: ws.indexOf(w) };
}

// Drags waypoint k of edge i to `p` (no snapping to the old curve: the user is bending it).
export function moveWaypoint(vertices: Pt[], curves: Curves, i: number, k: number, p: Pt): Curves {
  const ws = [...(curves[i] ?? [])];
  if (!ws[k]) return curves;
  ws[k] = toChordFrame(vertices[i], vertices[(i + 1) % vertices.length], p);
  return withEdge(curves, i, ws);
}

export function removeWaypoint(curves: Curves, i: number, k: number): Curves {
  return withEdge(curves, i, tidy((curves[i] ?? []).filter((_, j) => j !== k)));
}

// A lone waypoint dragged back onto the chord is a straight edge again.
export function settle(curves: Curves, i: number): Curves {
  const ws = curves[i];
  return ws ? withEdge(curves, i, tidy(ws)) : curves;
}

// Edge index bookkeeping when vertices change. Edge k starts at vertex k.
//
// Inserting `vertex` (a point ON edge `edge`) splits it in two: waypoints before the vertex go to the
// first half, the rest to the second, each re-expressed in its new chord.
export function curvesAfterInsert(vertices: Pt[], curves: Curves, edge: number, vertex: Pt): Curves {
  const p0 = vertices[edge];
  const p1 = vertices[(edge + 1) % vertices.length];
  const out: Curves = {};
  for (const [k, v] of Object.entries(curves)) {
    const i = Number(k);
    if (i < edge) out[i] = v;
    else if (i > edge) out[i + 1] = v;
  }
  const ws = curves[edge];
  if (ws && ws.length) {
    const split = toChordFrame(p0, p1, vertex)[0];
    const abs = ws.map((w) => ({ a: w[0], p: fromChordFrame(p0, p1, w) }));
    const left = tidy(abs.filter((w) => w.a < split).map((w) => toChordFrame(p0, vertex, w.p)));
    const right = tidy(abs.filter((w) => w.a >= split).map((w) => toChordFrame(vertex, p1, w.p)));
    if (left) out[edge] = left;
    if (right) out[edge + 1] = right;
  }
  return out;
}

// Removing vertex v merges edges v-1 and v into one straight edge.
export function curvesAfterDelete(curves: Curves, v: number, vertexCount: number): Curves {
  const out: Curves = {};
  const before = (v - 1 + vertexCount) % vertexCount;
  for (const [k, c] of Object.entries(curves)) {
    const i = Number(k);
    if (i === v || i === before) continue;
    out[i > v ? i - 1 : i] = c;
  }
  return out;
}

// The point of edge i nearest `p`: where a new vertex goes when the user double-clicks a curved edge.
export function pointOnEdge(vertices: Pt[], curves: Curves, i: number, p: Pt): Pt {
  return nearestOnEdge(vertices[i], vertices[(i + 1) % vertices.length], curves[i], p);
}
