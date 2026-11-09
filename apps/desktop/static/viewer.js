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
  constructor(canvas, { onRender, onPick, onFollowOff } = {}) {
    this.canvas = canvas; this.onPick = onPick; this.onFollowOff = onFollowOff; this.follow = false;
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

  setData(cloud, poses, opts = {}) {
    this.cloud = cloud; this.poses = poses;
    this.frameOnCloud = !!opts.frameOnCloud;
    // Scene frame: centre = median point, up = mean camera "up" (-y in COLMAP camera axes), forward = mean view dir.
    const n = cloud.count;
    const med = (k) => { const a = []; for (let i = k; i < 3 * n; i += 3 * Math.max(1, Math.floor(n / 4000))) a.push(cloud.xyz[i]); a.sort((x, y) => x - y); return a[a.length >> 1] ?? 0; };
    this.center = n ? [med(0), med(1), med(2)] : [0, 0, 0];
    this._center0 = this.center.slice(); this.roi = null; this._ov = null;
    let up = [0, 0, 0], fwd = [0, 0, 0];
    for (const p of poses) { const R = p.R; up = sub(up, [R[0][1], R[1][1], R[2][1]]); fwd = [fwd[0] + R[0][2], fwd[1] + R[1][2], fwd[2] + R[2][2]]; }
    up = poses.length ? norm(up) : [0, -1, 0];
    // A tilted camera (a drone looking down) makes its own "up" a poor level reference. When the scene is flat
    // (the thinnest spread of the points is well below the next one), level the map to that plane instead.
    this.levelled = false;
    if (n > 100) {
      const st = Math.max(1, Math.floor(n / 6000)), m = [0, 0, 0]; let k = 0;
      for (let i = 0; i < n; i += st) { m[0] += cloud.xyz[3 * i]; m[1] += cloud.xyz[3 * i + 1]; m[2] += cloud.xyz[3 * i + 2]; k++; }
      m[0] /= k; m[1] /= k; m[2] /= k;
      const S = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
      for (let i = 0; i < n; i += st) { const v = [cloud.xyz[3 * i] - m[0], cloud.xyz[3 * i + 1] - m[1], cloud.xyz[3 * i + 2] - m[2]]; for (let a = 0; a < 3; a++) for (let b = 0; b < 3; b++) S[a][b] += v[a] * v[b] / k; }
      const ev = jacobiEig(S);  // ascending variances
      const e0 = ev.vecs[0];
      if (ev.vals[0] < 0.25 * ev.vals[1] && Math.abs(dot(e0, up)) > Math.cos(70 * Math.PI / 180)) { up = dot(e0, up) > 0 ? e0 : e0.map((v) => -v); this.levelled = true; }
    }
    let f = sub(fwd, up.map((u) => u * dot(fwd, up)));
    if (Math.hypot(...f) < 1e-6) f = Math.abs(up[2]) < 0.9 ? cross(up, [0, 0, 1]) : cross(up, [1, 0, 0]);
    const z = norm(f.map((v) => -v)), x = norm(cross(up, z));
    this.align = [x, up, z]; this._align0 = this.align; this.follow = false; this._fvShown = null; this._tw = null;
    const d = [];
    for (let i = 0; i < n; i += Math.max(1, Math.floor(n / 4000))) d.push(Math.hypot(...sub([cloud.xyz[3 * i], cloud.xyz[3 * i + 1], cloud.xyz[3 * i + 2]], this.center)));
    d.sort((a, b) => a - b);
    // Frame the bulk of the points and the whole camera path (cameras often sit outside the point cloud).
    const camD = poses.map((p) => Math.hypot(...sub(p.C, this.center))).sort((a, b) => a - b);
    // typical point-to-nearest-camera distance (sampled) sets the area-of-interest radius
    this.camDist = [];
    for (let i = 0; i < n; i += Math.max(1, Math.floor(n / 3000))) {
      const q = [cloud.xyz[3 * i], cloud.xyz[3 * i + 1], cloud.xyz[3 * i + 2]];
      let best = Infinity; for (let k = 0; k < poses.length; k += Math.max(1, Math.floor(poses.length / 60))) best = Math.min(best, Math.hypot(...sub(q, poses[k].C)));
      this.camDist.push(best);
    }
    this.camDist.sort((a, b) => a - b);
    const camR = camD[Math.min(camD.length - 1, Math.floor(camD.length * 0.95))] || 0;  // p95: one stray camera must not shrink the scene
    const camMax = camD[camD.length - 1] || 0;
    // Two framings: the main observed region (90th percentile of point distances + the camera path) and the full
    // extent (every point). A few distant points must not decide the default view; they stay drawn either way.
    // Static-map layers frame the observed surfaces only (the camera path is an optional overlay there).
    this.radiusMain = (this.frameOnCloud ? d[Math.floor(d.length * 0.75)] : Math.max(d[Math.floor(d.length * 0.9)] || 0, camR * 1.1)) || 1;
    this.radiusFull = Math.max(d[d.length - 1] || 0, camMax * 1.1) || 1;
    this.radius = this.framing === "full" ? this.radiusFull : this.radiusMain;
    this.resetView();
  }

  clear() { this.cloud = null; this.poses = []; this.request(); }
  setBreaks(times) { this.breaks = new Set(times); this.request(); }
  setFraming(mode) {
    this.framing = mode;
    if (this.cloud) { this.radius = mode === "full" ? this.radiusFull : this.radiusMain; this.resetView(); }
  }
  // Flat (levelled) scenes such as aerial maps start from a drone-like angle along the camera direction: a low
  // 20-degree view of a flat map shows it edge-on, as a thin disc.
  _restoreFrame() { if (this._align0) this.align = this._align0; if (!this.roi?.on && this._center0) this.center = this._center0; }
  resetView() {
    this._restoreFrame();
    const flat = this.levelled;
    Object.assign(this.view, { yaw: 0, pitch: flat ? 0.75 : 0.35, dist: this.radius ? this.radius * (flat ? 1.7 : 2.6) : 3, panX: 0, panY: 0 }); this.request();
  }
  topView() { this._restoreFrame(); Object.assign(this.view, { pitch: 1.5, dist: this.radius ? this.radius * 2.4 : 3, panX: 0, panY: 0 }); this.request(); }
  // Follow: the view rides just behind the current camera, looking where it looked, so the map moves with the video.
  setFollow(on) {
    if (!on) return this.freeze();
    this.follow = true; this._fvShown = null; this._tw = null; this.request();
  }
  // Leave Follow without a jump: the free view starts exactly where the camera view was, orbiting the point it looked at.
  freeze() {
    if (!this.follow) return;
    const fv = this._fvShown || this._followView();
    this.follow = false; this._tw = null;
    if (fv) { this.align = fv.M; this.center = fv.cen; Object.assign(this.view, { yaw: 0, pitch: 0, dist: fv.dist, panX: 0, panY: 0 }); }
    this.request();
  }
  _followView(k = this.current) {
    const p = this.poses[Math.max(0, Math.min(this.poses.length - 1, k || 0))];
    if (!p) return null;
    const R = p.R, right = [R[0][0], R[1][0], R[2][0]], up = [-R[0][1], -R[1][1], -R[2][1]], fwd = [R[0][2], R[1][2], R[2][2]];
    const D = this.radius * 0.5, back = this.radius * 0.12, lift = this.radius * 0.04;
    const eye = [0, 1, 2].map((k) => p.C[k] - fwd[k] * back + up[k] * lift);
    return { M: [right, up, fwd.map((v) => -v)], dist: D, cen: [0, 1, 2].map((k) => eye[k] + fwd[k] * D) };
  }
  setVisibleUpTo(k) { this.visibleUpTo = k; this.request(); }
  setCurrent(k) {
    if (this.follow && this._fvShown && k !== this.current) this._tw = { from: this._fvShown, t0: performance.now(), dur: 180 };
    this.current = k; if (this.roi?.on) this._frameRoi(false); this.request(); }
  // Area of interest: only points near the current camera are drawn and framed; the overview shows the whole map.
  setRoi(on) {
    if (!this.cloud) return;
    if (!this.roi) {
      const d = this.camDist || [];
      this.roi = { r: Math.max(1e-6, (d[Math.floor(d.length * 0.5)] || this.radiusMain * 0.2) * 3) };
    }
    this.roi.on = on;
    if (on) this._frameRoi(true); else { this.center = this._center0; this.radius = this.framing === "full" ? this.radiusFull : this.radiusMain; this.resetView(); }
    this.request();
  }
  _frameRoi(reset) {
    const p = this.poses[Math.max(0, Math.min(this.poses.length - 1, this.current || 0))];
    if (!p) return;
    this.center = p.C.slice(); this.radius = this.roi.r;
    if (reset) this.resetView(); else this.request();
  }
  drawOverview(canvas) {
    if (!canvas || !this.cloud) return;
    const dpr = window.devicePixelRatio || 1, W = Math.max(1, Math.round(canvas.clientWidth * dpr)), H = Math.max(1, Math.round(canvas.clientHeight * dpr));
    if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
    const g = canvas.getContext("2d"); g.fillStyle = "rgba(11,14,18,.92)"; g.fillRect(0, 0, W, H);
    const [ax, , az] = this._align0 || this.align, c0 = this._center0 || this.center;
    if (!this._ov || this._ov.cloud !== this.cloud) {  // top-down projection of a decimated copy, computed once per map
      const { xyz, rgb, count } = this.cloud, step = Math.max(1, Math.floor(count / 60000)), P = [], C = [];
      for (let i = 0; i < count; i += step) { const q = [xyz[3 * i] - c0[0], xyz[3 * i + 1] - c0[1], xyz[3 * i + 2] - c0[2]]; P.push(dot(q, ax), dot(q, az)); C.push(rgb[3 * i], rgb[3 * i + 1], rgb[3 * i + 2]); }
      const cams = this.poses.map((p) => { const q = sub(p.C, c0); return [dot(q, ax), dot(q, az)]; });
      let ext = 1e-9; for (const [x, y] of cams) ext = Math.max(ext, Math.abs(x), Math.abs(y));
      this._ov = { cloud: this.cloud, P, C, cams, ext: Math.max(ext * 1.3, this.radiusMain) };
    }
    const o = this._ov, s = 0.48 * Math.min(W, H) / o.ext, cx = W / 2, cy = H / 2;
    for (let i = 0; i < o.P.length; i += 2) { g.fillStyle = `rgb(${o.C[1.5 * i]},${o.C[1.5 * i + 1]},${o.C[1.5 * i + 2]})`; g.fillRect(cx + s * o.P[i], cy - s * o.P[i + 1], dpr, dpr); }
    g.strokeStyle = "#4cc3ff"; g.lineWidth = dpr; g.beginPath();
    o.cams.forEach(([x, y], k) => (k ? g.lineTo(cx + s * x, cy - s * y) : g.moveTo(cx + s * x, cy - s * y))); g.stroke();
    o.s = s / dpr; o.cx = cx / dpr; o.cy = cy / dpr;  // CSS-pixel transform, for clicks
    const k0 = Math.max(0, Math.min(o.cams.length - 1, this.current || 0)), cur = o.cams[k0];
    if (cur) {
      const R = this.poses[k0].R, fw = [R[0][2], R[1][2], R[2][2]];
      let fx = dot(fw, ax), fy = dot(fw, az); const fl = Math.hypot(fx, fy) || 1; fx /= fl; fy /= fl;
      const X0 = cx + s * cur[0], Y0 = cy - s * cur[1], L = 22 * dpr, sp = 0.45;  // view direction wedge
      g.fillStyle = "rgba(255,176,32,.35)"; g.beginPath(); g.moveTo(X0, Y0);
      g.lineTo(X0 + L * (fx * Math.cos(sp) - fy * Math.sin(sp)), Y0 - L * (fx * Math.sin(sp) + fy * Math.cos(sp)));
      g.lineTo(X0 + L * (fx * Math.cos(-sp) - fy * Math.sin(-sp)), Y0 - L * (fx * Math.sin(-sp) + fy * Math.cos(-sp))); g.closePath(); g.fill();
      g.fillStyle = "#ffb020"; g.beginPath(); g.arc(X0, Y0, 3.5 * dpr, 0, 7); g.fill();
      if (this.roi?.on) { g.strokeStyle = "#ffb020"; g.beginPath(); g.arc(cx + s * cur[0], cy - s * cur[1], s * this.roi.r, 0, 7); g.stroke(); }
    }
  }
  setOverlays({ path, cameras }) {
    if (path !== undefined) this.showPath = path;
    if (cameras !== undefined) this.showCameras = cameras;
    this.request();
  }

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

    let fv = this.follow ? this._followView() : null;
    if (fv && this._tw) {  // glide from the previous camera view to this one
      const a = Math.min(1, (performance.now() - this._tw.t0) / this._tw.dur), e = a * a * (3 - 2 * a), F = this._tw.from;
      const L = (p, q) => p.map((v, i) => v + (q[i] - v) * e);
      const r0 = norm(L(F.M[0], fv.M[0])), r2 = norm(L(F.M[2], fv.M[2])), r1 = norm(cross(r2, r0));
      fv = { M: [r0, r1, r2], dist: F.dist + (fv.dist - F.dist) * e, cen: L(F.cen, fv.cen) };
      if (a < 1) requestAnimationFrame(() => this.request()); else this._tw = null;
    }
    this._fvShown = fv;
    this.canvas.dataset.follow = fv ? "on" : "off";
    const M = fv ? fv.M : this._matrix(), { panX, panY } = fv ? { panX: 0, panY: 0 } : this.view;
    const dist = fv ? fv.dist : this.view.dist, cen = fv ? fv.cen : this.center;
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
    const roiR2 = this.roi?.on ? this.roi.r * this.roi.r : Infinity;
    for (let i = 0; i < count; i++) {
      if (first[i] > this.visibleUpTo) continue;
      const x = xyz[3 * i] - cen[0], y = xyz[3 * i + 1] - cen[1], z = xyz[3 * i + 2] - cen[2];
      if (roiR2 !== Infinity && x * x + y * y + z * z > roiR2) continue;
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
    if (this.showPath !== false) {
      let started = false;
      for (let k = 0; k <= last; k++) {
        const q = project(this.poses[k].C); if (!q) { started = false; continue; }
        // never draw a line across a detected path jump: there is no geometric evidence linking the two sides
        if (started && this.breaks?.has(this.poses[k].timestamp_s)) started = false;
        if (started) ctx.lineTo(q[0], q[1]); else { ctx.moveTo(q[0], q[1]); started = true; }
      }
      ctx.stroke();
    }
    if (this.showCameras !== false) {
      const step = Math.max(1, Math.round(this.poses.length / 12));
      for (let k = 0; k <= last; k++) {
        if (k % step && k !== this.current) continue;
        this._frustum(project, this.poses[k], k === this.current ? "#ffb020" : "rgba(76,195,255,.75)", k === this.current ? 2.2 : 1);
      }
    }
    this.lastDrawn = drawn;
    if (this.overviewCanvas && !this.overviewCanvas.hidden) this.drawOverview(this.overviewCanvas);
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

  // click on the overview: jump to the nearest camera
  bindOverview(canvas) {
    this.overviewCanvas = canvas;
    if (canvas._bound) return; canvas._bound = true;
    canvas.addEventListener("click", (e) => {
      const o = this._ov; if (!o?.s) return;
      const r = canvas.getBoundingClientRect(), x = (e.clientX - r.left - o.cx) / o.s, y = -(e.clientY - r.top - o.cy) / o.s;
      let best = -1, bd = Infinity; o.cams.forEach(([a, b], k) => { const d = (a - x) ** 2 + (b - y) ** 2; if (d < bd) { bd = d; best = k; } });
      if (best >= 0) this.onPick?.(best);
    });
  }

  _bindInput() {
    const c = this.canvas; let drag = null;
    c.addEventListener("pointerdown", (e) => {
      if (this.follow) { this.freeze(); this.onFollowOff?.(); }  // grabbing the view during playback frees it
      drag = { x: e.clientX, y: e.clientY, pan: e.shiftKey || e.button === 2 }; c.setPointerCapture(e.pointerId); });
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
        "+": () => this.zoom(0.85), "=": () => this.zoom(0.85), "-": () => this.zoom(1.18), r: () => this.resetView(), R: () => this.resetView(), t: () => this.topView(), T: () => this.topView() }[e.key];
      if (k && this.cloud) { e.preventDefault(); e.stopPropagation(); k(); this.request(); }
    });
  }

  zoom(factor) { this.view.dist = Math.max(this.radius * 0.1, Math.min(this.radius * 30, this.view.dist * factor)); this.request(); }
}

// eigen-decomposition of a symmetric 3x3 matrix (Jacobi); vals ascending, vecs[i] the unit vector of vals[i]
function jacobiEig(A0) {
  const A = A0.map((r) => r.slice()), V = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
  for (let it = 0; it < 50; it++) {
    let p = 0, q = 1;
    for (const [i, j] of [[0, 1], [0, 2], [1, 2]]) if (Math.abs(A[i][j]) > Math.abs(A[p][q])) { p = i; q = j; }
    if (Math.abs(A[p][q]) < 1e-15) break;
    const th = 0.5 * Math.atan2(2 * A[p][q], A[q][q] - A[p][p]), c = Math.cos(th), s = Math.sin(th);
    for (let k = 0; k < 3; k++) { const akp = A[k][p], akq = A[k][q]; A[k][p] = c * akp - s * akq; A[k][q] = s * akp + c * akq; }
    for (let k = 0; k < 3; k++) { const apk = A[p][k], aqk = A[q][k]; A[p][k] = c * apk - s * aqk; A[q][k] = s * apk + c * aqk; }
    for (let k = 0; k < 3; k++) { const vkp = V[k][p], vkq = V[k][q]; V[k][p] = c * vkp - s * vkq; V[k][q] = s * vkp + c * vkq; }
  }
  const idx = [0, 1, 2].sort((a, b) => A[a][a] - A[b][b]);
  return { vals: idx.map((i) => A[i][i]), vecs: idx.map((i) => [V[0][i], V[1][i], V[2][i]]) };
}
