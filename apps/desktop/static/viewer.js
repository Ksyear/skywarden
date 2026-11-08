// Dependency-free point-cloud viewer: z-buffered 2D canvas splats + trajectory/frustum overlay.
// Renders only when something changed (requestAnimationFrame coalesces bursts of input).

export function parsePly(buffer) {
  const bytes = new Uint8Array(buffer);
  const marker = "end_header\n";
  const head = new TextDecoder("latin1").decode(bytes.subarray(0, Math.min(bytes.length, 4096)));
  if (!head.startsWith("ply\n") || head.indexOf(marker) < 0) throw new Error("PLY header not found");
  const headerEnd = head.indexOf(marker) + marker.length;
  const header = new TextDecoder().decode(bytes.subarray(0, headerEnd)).split("\n");
  if (!header.includes("format binary_little_endian 1.0")) throw new Error("PLY must be binary_little_endian");
  const sizes = { float: 4, uchar: 1, uint: 4, int: 4, double: 8 };
  let count = 0; const props = [];
  for (const line of header) {
    const p = line.trim().split(/\s+/);
    if (p[0] === "element" && p[1] === "vertex") count = parseInt(p[2], 10);
    else if (p[0] === "property") {
      if (!(p[1] in sizes)) throw new Error(`unsupported PLY property type ${p[1]}`);
      props.push({ type: p[1], name: p[2] });
    }
  }
  let stride = 0; const offs = {};
  for (const p of props) { offs[p.name] = { o: stride, t: p.type }; stride += sizes[p.type]; }
  if (buffer.byteLength - headerEnd !== count * stride) throw new Error("PLY body size does not match header");
  const dv = new DataView(buffer, headerEnd);
  const xyz = new Float32Array(count * 3), rgb = new Uint8Array(count * 3), first = new Uint32Array(count);
  const get = (base, f) => f.t === "float" ? dv.getFloat32(base + f.o, true) : f.t === "uchar" ? dv.getUint8(base + f.o)
    : f.t === "uint" ? dv.getUint32(base + f.o, true) : f.t === "int" ? dv.getInt32(base + f.o, true) : dv.getFloat64(base + f.o, true);
  for (let i = 0; i < count; i++) {
    const b = i * stride;
    xyz[3 * i] = get(b, offs.x); xyz[3 * i + 1] = get(b, offs.y); xyz[3 * i + 2] = get(b, offs.z);
    if (offs.red) { rgb[3 * i] = get(b, offs.red); rgb[3 * i + 1] = get(b, offs.green); rgb[3 * i + 2] = get(b, offs.blue); }
    else rgb.fill(200, 3 * i, 3 * i + 3);
    first[i] = offs.first_seen ? get(b, offs.first_seen) : 0;
  }
  return { count, xyz, rgb, first };
}

const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const norm = (a) => { const l = Math.hypot(...a) || 1; return [a[0] / l, a[1] / l, a[2] / l]; };
const mul = (A, B) => A.map((r) => [0, 1, 2].map((j) => r[0] * B[0][j] + r[1] * B[1][j] + r[2] * B[2][j]));
const apply = (M, v) => [dot(M[0], v), dot(M[1], v), dot(M[2], v)];

export class PointViewer {
  constructor(canvas, { onRender } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.onRender = onRender;
    this.cloud = null; this.poses = [];
    this.visibleUpTo = Infinity; this.current = -1;
    this.pointSize = 2;
    this.view = { yaw: 0, pitch: 0.35, dist: 3, panX: 0, panY: 0 };
    this._pending = false;
    new ResizeObserver(() => this.request()).observe(canvas);
    this._bindInput();
  }

  setData(cloud, poses) {
    this.cloud = cloud; this.poses = poses;
    // Scene frame: centre = median point, up = mean camera "up" (-y in COLMAP camera axes), forward = mean view dir.
    const n = cloud.count;
    const med = (k) => { const a = []; for (let i = k; i < 3 * n; i += 3 * Math.max(1, Math.floor(n / 4000))) a.push(cloud.xyz[i]); a.sort((x, y) => x - y); return a[a.length >> 1] ?? 0; };
    this.center = n ? [med(0), med(1), med(2)] : [0, 0, 0];
    let up = [0, 0, 0], fwd = [0, 0, 0];
    for (const p of poses) { const R = p.R; up = sub(up, [R[0][1], R[1][1], R[2][1]]); fwd = [fwd[0] + R[0][2], fwd[1] + R[1][2], fwd[2] + R[2][2]]; }
    up = poses.length ? norm(up) : [0, -1, 0];
    let f = sub(fwd, up.map((u) => u * dot(fwd, up)));
    if (Math.hypot(...f) < 1e-6) f = Math.abs(up[2]) < 0.9 ? cross(up, [0, 0, 1]) : cross(up, [1, 0, 0]);
    const z = norm(f.map((v) => -v)), x = norm(cross(up, z));
    this.align = [x, up, z];
    const d = [];
    for (let i = 0; i < n; i += Math.max(1, Math.floor(n / 4000))) d.push(Math.hypot(...sub([cloud.xyz[3 * i], cloud.xyz[3 * i + 1], cloud.xyz[3 * i + 2]], this.center)));
    d.sort((a, b) => a - b);
    // Frame the bulk of the points and the whole camera path (cameras often sit outside the point cloud).
    const camR = poses.reduce((m, p) => Math.max(m, Math.hypot(...sub(p.C, this.center))), 0);
    this.radius = Math.max(d[Math.floor(d.length * 0.9)] || 0, camR * 1.1) || 1;
    this.resetView();
  }

  clear() { this.cloud = null; this.poses = []; this.request(); }
  resetView() { Object.assign(this.view, { yaw: 0, pitch: 0.35, dist: this.radius ? this.radius * 2.6 : 3, panX: 0, panY: 0 }); this.request(); }
  setVisibleUpTo(k) { this.visibleUpTo = k; this.request(); }
  setCurrent(k) { this.current = k; this.request(); }

  request() {
    if (this._pending) return;
    this._pending = true;
    requestAnimationFrame(() => { this._pending = false; this._draw(); });
  }

  _matrix() {
    const { yaw, pitch } = this.view;
    const cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
    const Ry = [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]];
    const Rx = [[1, 0, 0], [0, cp, -sp], [0, sp, cp]];
    return mul(mul(Rx, Ry), this.align);
  }

  _draw() {
    const t0 = performance.now();
    const c = this.canvas, dpr = window.devicePixelRatio || 1;
    const W = Math.max(1, Math.round(c.clientWidth * dpr)), H = Math.max(1, Math.round(c.clientHeight * dpr));
    if (c.width !== W || c.height !== H) { c.width = W; c.height = H; }
    const ctx = this.ctx;
    ctx.fillStyle = "#0b0e12"; ctx.fillRect(0, 0, W, H);
    if (!this.cloud) return;

    const M = this._matrix(), { dist, panX, panY } = this.view, cen = this.center;
    const f = 0.9 * Math.min(W, H), cx = W / 2, cy = H / 2, near = this.radius * 0.02;
    const project = (p) => {
      const v = apply(M, sub(p, cen)); const depth = dist - v[2];
      if (depth <= near) return null;
      return [cx + f * (v[0] + panX) / depth, cy - f * (v[1] + panY) / depth, depth];
    };

    const img = ctx.createImageData(W, H), px = new Uint32Array(img.data.buffer);
    const zbuf = new Float32Array(W * H).fill(Infinity);
    px.fill(0xff120e0b); // ABGR of #0b0e12
    const { xyz, rgb, first, count } = this.cloud;
    const m = M.flat(), s = Math.max(1, Math.round(this.pointSize * dpr));
    let drawn = 0;
    for (let i = 0; i < count; i++) {
      if (first[i] > this.visibleUpTo) continue;
      const x = xyz[3 * i] - cen[0], y = xyz[3 * i + 1] - cen[1], z = xyz[3 * i + 2] - cen[2];
      const vz = m[6] * x + m[7] * y + m[8] * z, depth = dist - vz;
      if (depth <= near) continue;
      const sx = Math.round(cx + f * (m[0] * x + m[1] * y + m[2] * z + panX) / depth);
      const sy = Math.round(cy - f * (m[3] * x + m[4] * y + m[5] * z + panY) / depth);
      if (sx < 0 || sy < 0 || sx >= W - s || sy >= H - s) continue;
      const col = 0xff000000 | (rgb[3 * i + 2] << 16) | (rgb[3 * i + 1] << 8) | rgb[3 * i];
      for (let dy = 0; dy < s; dy++) for (let dx = 0; dx < s; dx++) {
        const k = (sy + dy) * W + sx + dx;
        if (depth < zbuf[k]) { zbuf[k] = depth; px[k] = col; }
      }
      drawn++;
    }
    ctx.putImageData(img, 0, 0);

    // Trajectory and camera frustums (drawn on top: they are annotations, not geometry).
    const last = Math.min(this.poses.length - 1, this.visibleUpTo);
    ctx.lineWidth = 1.5 * dpr; ctx.strokeStyle = "#4cc3ff"; ctx.beginPath();
    let started = false;
    for (let k = 0; k <= last; k++) {
      const q = project(this.poses[k].C); if (!q) { started = false; continue; }
      if (started) ctx.lineTo(q[0], q[1]); else { ctx.moveTo(q[0], q[1]); started = true; }
    }
    ctx.stroke();
    const step = Math.max(1, Math.round(this.poses.length / 12));
    for (let k = 0; k <= last; k++) {
      if (k % step && k !== this.current) continue;
      this._frustum(project, this.poses[k], k === this.current ? "#ffb020" : "rgba(76,195,255,.75)", k === this.current ? 2.2 : 1);
    }
    this.lastDrawn = drawn;
    this.onRender?.(performance.now() - t0, drawn);
  }

  _frustum(project, pose, color, scale) {
    const ctx = this.ctx, s = this.radius * 0.06 * scale, R = pose.R, C = pose.C;
    const corner = (u, v) => [0, 1, 2].map((r) => C[r] + s * (R[r][0] * u + R[r][1] * v + R[r][2] * 1));
    const pts = [C, corner(-0.6, -0.45), corner(0.6, -0.45), corner(0.6, 0.45), corner(-0.6, 0.45)].map(project);
    if (pts.some((p) => !p)) return;
    ctx.strokeStyle = color; ctx.lineWidth = (scale > 1 ? 2 : 1) * (window.devicePixelRatio || 1);
    ctx.beginPath();
    for (let i = 1; i <= 4; i++) { ctx.moveTo(pts[0][0], pts[0][1]); ctx.lineTo(pts[i][0], pts[i][1]); }
    ctx.moveTo(pts[1][0], pts[1][1]); for (const i of [2, 3, 4, 1]) ctx.lineTo(pts[i][0], pts[i][1]);
    ctx.stroke();
  }

  _bindInput() {
    const c = this.canvas; let drag = null;
    c.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY, pan: e.shiftKey || e.button === 2 }; c.setPointerCapture(e.pointerId); });
    c.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y; drag.x = e.clientX; drag.y = e.clientY;
      if (drag.pan) { const k = this.view.dist / (0.9 * Math.min(c.clientWidth, c.clientHeight)); this.view.panX += dx * k; this.view.panY -= dy * k; }
      else { this.view.yaw += dx * 0.008; this.view.pitch = Math.max(-1.5, Math.min(1.5, this.view.pitch + dy * 0.008)); }
      this.request();
    });
    const end = () => { drag = null; };
    c.addEventListener("pointerup", end); c.addEventListener("pointercancel", end);
    c.addEventListener("contextmenu", (e) => e.preventDefault());
    c.addEventListener("wheel", (e) => { e.preventDefault(); this.zoom(Math.exp(e.deltaY * 0.001)); }, { passive: false });
    c.addEventListener("keydown", (e) => {
      const k = { ArrowLeft: () => (this.view.yaw -= 0.1), ArrowRight: () => (this.view.yaw += 0.1),
        ArrowUp: () => (this.view.pitch = Math.max(-1.5, this.view.pitch - 0.1)), ArrowDown: () => (this.view.pitch = Math.min(1.5, this.view.pitch + 0.1)),
        "+": () => this.zoom(0.85), "=": () => this.zoom(0.85), "-": () => this.zoom(1.18), r: () => this.resetView(), R: () => this.resetView() }[e.key];
      if (k && this.cloud) { e.preventDefault(); e.stopPropagation(); k(); this.request(); }
    });
  }

  zoom(factor) { this.view.dist = Math.max(this.radius * 0.1, Math.min(this.radius * 30, this.view.dist * factor)); this.request(); }
}
