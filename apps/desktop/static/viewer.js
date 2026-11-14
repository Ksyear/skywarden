// Dependency-free point-cloud viewer. Points are drawn with WebGL (one draw call, GPU depth test); without WebGL the
// same projection runs as z-buffered 2D canvas splats. Trajectory, frustums and measurements are drawn on a 2D layer
// on top. Renders only when something changed (requestAnimationFrame coalesces bursts of input).
//
// Level of detail: the points are put in a fixed random order once per map, so any prefix of the arrays is an even
// sample of the whole cloud. Each frame draws the prefix that the map's size on screen needs (all of it when the
// map fills the view); while the view moves the prefix is shorter, and the full prefix is drawn once it settles.
// Point picking only looks at the prefix that is on screen, so it can only return a point the user can see.

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
  if (!offs.x || !offs.y || !offs.z) throw new Error("PLY has no x/y/z");
  const dv = new DataView(buffer, headerEnd);
  const xyz = new Float32Array(count * 3), rgb = new Uint8Array(count * 3), first = new Uint32Array(count);
  const reader = (f) => ({ float: (b) => dv.getFloat32(b + f.o, true), uchar: (b) => dv.getUint8(b + f.o),
    uint: (b) => dv.getUint32(b + f.o, true), int: (b) => dv.getInt32(b + f.o, true), double: (b) => dv.getFloat64(b + f.o, true) })[f.t];
  const fx = offs.x, fy = offs.y, fz = offs.z;
  if (fx.t === "float" && fy.t === "float" && fz.t === "float") {  // the usual layout: read the floats directly
    for (let i = 0, b = 0; i < count; i++, b += stride) {
      xyz[3 * i] = dv.getFloat32(b + fx.o, true); xyz[3 * i + 1] = dv.getFloat32(b + fy.o, true); xyz[3 * i + 2] = dv.getFloat32(b + fz.o, true);
    }
  } else {
    const gx = reader(fx), gy = reader(fy), gz = reader(fz);
    for (let i = 0, b = 0; i < count; i++, b += stride) { xyz[3 * i] = gx(b); xyz[3 * i + 1] = gy(b); xyz[3 * i + 2] = gz(b); }
  }
  if (offs.red && offs.green && offs.blue) {
    const { red: r, green: g, blue: bl } = offs;
    if (r.t === "uchar" && g.t === "uchar" && bl.t === "uchar") {
      for (let i = 0, b = 0; i < count; i++, b += stride) { rgb[3 * i] = bytes[headerEnd + b + r.o]; rgb[3 * i + 1] = bytes[headerEnd + b + g.o]; rgb[3 * i + 2] = bytes[headerEnd + b + bl.o]; }
    } else {
      const gr = reader(r), gg = reader(g), gb = reader(bl);
      for (let i = 0, b = 0; i < count; i++, b += stride) { rgb[3 * i] = gr(b); rgb[3 * i + 1] = gg(b); rgb[3 * i + 2] = gb(b); }
    }
  } else rgb.fill(200);
  if (offs.first_seen) { const gf = reader(offs.first_seen); for (let i = 0, b = 0; i < count; i++, b += stride) first[i] = gf(b); }
  return { count, xyz, rgb, first };
}

const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const norm = (a) => { const l = Math.hypot(...a) || 1; return [a[0] / l, a[1] / l, a[2] / l]; };
const mul = (A, B) => A.map((r) => [0, 1, 2].map((j) => r[0] * B[0][j] + r[1] * B[1][j] + r[2] * B[2][j]));
const apply = (M, v) => [dot(M[0], v), dot(M[1], v), dot(M[2], v)];
const BG = [11, 14, 18];  // #0b0e12, the map background in both renderers
const reduceMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

// Points needed on screen per point footprint (area / size²): the full map is drawn when it fills the view; a map that
// is small on screen (zoomed out) or a view that is moving draws an even sample. The 2D path pays per point in JS.
const LOD = { webgl: { idle: 32, moving: 12, min: 600000 }, "2d": { idle: 10, moving: 3, min: 150000 } };
const SETTLE_MS = 160;
const MOVING_LOD_ABOVE_MS = 14;  // the lighter moving sample is used only when an idle frame takes longer than this

const VS = `
attribute vec3 a_pos; attribute vec3 a_col; attribute float a_first;
uniform mat3 u_M; uniform vec3 u_cen; uniform vec2 u_pan; uniform vec2 u_scale;
uniform float u_dist; uniform float u_near; uniform float u_far; uniform float u_upTo; uniform float u_roiR2; uniform float u_size;
varying vec3 v_col;
void main() {
  vec3 q = a_pos - u_cen;
  if (a_first > u_upTo || dot(q, q) > u_roiR2) { gl_Position = vec4(2.0, 2.0, 2.0, 1.0); gl_PointSize = 0.0; return; }
  vec3 v = q * u_M;  // u_M holds the row-major view matrix, so q * u_M = M q
  float depth = u_dist - v.z;
  float zn = (u_far + u_near) / (u_far - u_near) - 2.0 * u_far * u_near / ((u_far - u_near) * depth);
  gl_Position = vec4(u_scale.x * (v.x + u_pan.x), u_scale.y * (v.y + u_pan.y), zn * depth, depth);
  gl_PointSize = u_size;
  v_col = a_col;
}`;
const FS = `precision mediump float; varying vec3 v_col; void main() { gl_FragColor = vec4(v_col, 1.0); }`;

export class PointViewer {
  constructor(canvas, { onRender, onPick, onFollowOff, onPoint, glCanvas, renderer } = {}) {
    this.canvas = canvas; this.onPick = onPick; this.onFollowOff = onFollowOff; this.onPoint = onPoint; this.follow = false;
    this.pickMode = false; this.measure = null;  // measure: {pts: [[x,y,z], ...], text} drawn on top of the map
    this.onRender = onRender;
    this.cloud = null; this.poses = [];
    this.visibleUpTo = Infinity; this.current = -1;
    this.pointSize = 2;
    this.view = { yaw: 0, pitch: 0.35, dist: 3, panX: 0, panY: 0 };
    this._pending = false; this._moveUntil = 0; this._shown = { count: 0 };
    this.glCanvas = glCanvas || null; this.gl = null;
    if (glCanvas && renderer !== "2d") this._initGL(glCanvas);
    this.renderer = this.gl ? "webgl" : "2d";
    if (this.glCanvas) this.glCanvas.hidden = !this.gl;
    this.ctx = canvas.getContext("2d");
    canvas.dataset.renderer = this.renderer;
    new ResizeObserver(() => this.request()).observe(canvas);
    this._bindInput();
  }

  // ---------------- WebGL ----------------
  _initGL(c) {
    const opts = { antialias: false, alpha: false, depth: true, premultipliedAlpha: false, preserveDrawingBuffer: false, powerPreference: "high-performance" };
    let gl = null;
    try { gl = c.getContext("webgl2", opts) || c.getContext("webgl", opts); } catch { gl = null; }
    if (!gl) return;
    const shader = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s)); return s; };
    try {
      const p = gl.createProgram();
      gl.attachShader(p, shader(gl.VERTEX_SHADER, VS)); gl.attachShader(p, shader(gl.FRAGMENT_SHADER, FS)); gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
      const u = {}; for (const n of ["M", "cen", "pan", "scale", "dist", "near", "far", "upTo", "roiR2", "size"]) u[n] = gl.getUniformLocation(p, `u_${n}`);
      const a = { pos: gl.getAttribLocation(p, "a_pos"), col: gl.getAttribLocation(p, "a_col"), first: gl.getAttribLocation(p, "a_first") };
      this._g = { p, u, a, buf: null, maxSize: gl.getParameter(gl.ALIASED_POINT_SIZE_RANGE)[1] || 64 };
      this.gl = gl;
    } catch (e) { console.warn("WebGL unavailable, using the 2D renderer:", e.message); this.gl = null; return; }
    c.addEventListener("webglcontextlost", (e) => {  // GPU reset: keep working with the 2D renderer
      e.preventDefault(); this.gl = null; this._g = null; this.renderer = "2d"; c.hidden = true;
      this.canvas.dataset.renderer = "2d"; this.request();
    });
  }
  _uploadGL() {
    const gl = this.gl, g = this._g; if (!gl) return;
    if (g.buf) for (const b of Object.values(g.buf)) gl.deleteBuffer(b);
    g.buf = null; if (!this.cloud) return;
    const { xyz, rgb, first, count } = this.cloud;
    let maxFirst = 0; for (let i = 0; i < count; i++) if (first[i] > maxFirst) maxFirst = first[i];
    const firstArr = maxFirst < 65535 ? Uint16Array.from(first) : Float32Array.from(first);
    const mk = (data) => { const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW); return b; };
    g.buf = { pos: mk(xyz), col: mk(rgb), first: mk(firstArr) }; g.firstType = firstArr instanceof Uint16Array ? gl.UNSIGNED_SHORT : gl.FLOAT;
  }
  _drawGL(vp, W, H, count, size) {
    const gl = this.gl, g = this._g, c = this.glCanvas;
    if (c.width !== W || c.height !== H) { c.width = W; c.height = H; }
    gl.viewport(0, 0, W, H);
    gl.clearColor(BG[0] / 255, BG[1] / 255, BG[2] / 255, 1); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    if (!g.buf || !count) return;
    gl.useProgram(g.p); gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LESS);
    const attr = (loc, buf, n, type, normalized) => { gl.bindBuffer(gl.ARRAY_BUFFER, buf); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, n, type, normalized, 0, 0); };
    attr(g.a.pos, g.buf.pos, 3, gl.FLOAT, false); attr(g.a.col, g.buf.col, 3, gl.UNSIGNED_BYTE, true); attr(g.a.first, g.buf.first, 1, g.firstType, false);
    const { M, panX, panY, dist, cen, f, near, far } = vp, u = g.u;
    gl.uniformMatrix3fv(u.M, false, new Float32Array(M.flat()));
    gl.uniform3f(u.cen, cen[0], cen[1], cen[2]); gl.uniform2f(u.pan, panX, panY); gl.uniform2f(u.scale, 2 * f / W, 2 * f / H);
    gl.uniform1f(u.dist, dist); gl.uniform1f(u.near, near); gl.uniform1f(u.far, far);
    gl.uniform1f(u.upTo, Number.isFinite(this.visibleUpTo) ? this.visibleUpTo : 3e38);
    gl.uniform1f(u.roiR2, this.roi?.on ? this.roi.r * this.roi.r : 3e38);
    gl.uniform1f(u.size, Math.min(size, g.maxSize));
    gl.drawArrays(gl.POINTS, 0, count);
  }

  setData(cloud, poses, opts = {}) {
    this.cloud = cloud; this.poses = poses;
    this.frameOnCloud = !!opts.frameOnCloud;
    // Scene frame: centre = median point, up = mean camera "up" (-y in COLMAP camera axes), forward = mean view dir.
    const n = cloud.count;
    const med = (k) => { const a = []; for (let i = k; i < 3 * n; i += 3 * Math.max(1, Math.floor(n / 4000))) a.push(cloud.xyz[i]); a.sort((x, y) => x - y); return a[a.length >> 1] ?? 0; };
    this.center = n ? [med(0), med(1), med(2)] : [0, 0, 0];
    this._center0 = this.center.slice(); this.roi = null; this._ov = null; this._visCache = null;
    let ext = 0; for (let i = 0; i < n; i++) { const x = cloud.xyz[3 * i] - this.center[0], y = cloud.xyz[3 * i + 1] - this.center[1], z = cloud.xyz[3 * i + 2] - this.center[2]; const d = x * x + y * y + z * z; if (d > ext) ext = d; }
    this._extent = Math.sqrt(ext);  // farthest point from the centre: sets the far clipping plane
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
    // fixed random order: every prefix is an even sample (LOD). Done after the framing statistics above, which sample
    // the file order, so the default view is the same as before.
    if (n > 200000) shuffleCloud(cloud);
    this._uploadGL();
    this.resetView();
  }

  clear() { this.cloud = null; this.poses = []; this._uploadGL(); this.request(); }
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
    // glide between camera views (about 180 ms); with reduced motion the view jumps
    if (this.follow && this._fvShown && k !== this.current && !reduceMotion()) this._tw = { from: this._fvShown, t0: performance.now(), dur: 180 };
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
    const g = canvas.getContext("2d");
    const [ax, , az] = this._align0 || this.align, c0 = this._center0 || this.center;
    if (!this._ov || this._ov.cloud !== this.cloud) {  // top-down projection of a decimated copy, computed once per map
      const { xyz, rgb, count } = this.cloud, step = Math.max(1, Math.floor(count / 60000)), P = [], C = [];
      for (let i = 0; i < count; i += step) { const q = [xyz[3 * i] - c0[0], xyz[3 * i + 1] - c0[1], xyz[3 * i + 2] - c0[2]]; P.push(dot(q, ax), dot(q, az)); C.push(rgb[3 * i], rgb[3 * i + 1], rgb[3 * i + 2]); }
      const cams = this.poses.map((p) => { const q = sub(p.C, c0); return [dot(q, ax), dot(q, az)]; });
      let ext = 1e-9; for (const [x, y] of cams) ext = Math.max(ext, Math.abs(x), Math.abs(y));
      this._ov = { cloud: this.cloud, P, C, cams, ext: Math.max(ext * 1.3, this.radiusMain), bitmap: null };
    }
    const o = this._ov, s = 0.48 * Math.min(W, H) / o.ext, cx = W / 2, cy = H / 2;
    if (!o.bitmap || o.bitmap.width !== W || o.bitmap.height !== H) {  // the point layer is drawn once per map and size
      const img = new ImageData(W, H), px = new Uint32Array(img.data.buffer), r = Math.max(1, Math.round(dpr));
      px.fill(0xeb000000 | (BG[2] << 16) | (BG[1] << 8) | BG[0]);
      for (let i = 0; i < o.P.length; i += 2) {
        const X = Math.round(cx + s * o.P[i]), Y = Math.round(cy - s * o.P[i + 1]); if (X < 0 || Y < 0 || X > W - r || Y > H - r) continue;
        const col = 0xff000000 | (o.C[1.5 * i + 2] << 16) | (o.C[1.5 * i + 1] << 8) | o.C[1.5 * i];
        for (let dy = 0; dy < r; dy++) for (let dx = 0; dx < r; dx++) px[(Y + dy) * W + X + dx] = col;
      }
      o.bitmap = img;
    }
    g.putImageData(o.bitmap, 0, 0);
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
  // The view is moving (drag, wheel, keys): draw a lighter sample now and the full one once input settles.
  _moved() {
    this._moveUntil = performance.now() + SETTLE_MS;
    clearTimeout(this._settle); this._settle = setTimeout(() => this.request(), SETTLE_MS + 10);
    this.request();
  }

  _matrix() {
    const { yaw, pitch } = this.view;
    const cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
    const Ry = [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]];
    const Rx = [[1, 0, 0], [0, cp, -sp], [0, sp, cp]];
    return mul(mul(Rx, Ry), this.align);
  }

  // How many points (a prefix of the shuffled arrays) this view needs, and their size in device pixels.
  _budget(vp, W, H, moving) {
    const n = this.cloud.count, base = Math.max(1, Math.round(this.pointSize * (window.devicePixelRatio || 1)));
    const lod = LOD[this.renderer];
    const R = this.roi?.on ? this.roi.r : this.radius, rPx = vp.f * R / Math.max(vp.near, vp.dist);
    const area = Math.max(1, Math.PI * rPx * rPx);             // the map's footprint on screen (may exceed the view)
    const onScreen = Math.min(area, W * H);
    // a GPU that draws the idle sample within a frame keeps it while the view moves (the moving sample thins sparse
    // areas visibly); slower devices and the 2D path drop to the moving sample
    const thin = moving && (this.renderer === "2d" || (this._idleMs ?? 0) > MOVING_LOD_ABOVE_MS);
    const want = (thin ? lod.moving : lod.idle) * onScreen / (base * base) * (area / onScreen);
    const count = Math.min(n, Math.max(Math.min(n, lod.min), Math.round(want)));
    // dense layers close up: an even sample leaves gaps between points, so grow them (up to 2x) to keep surfaces
    // closed. Sparse maps are feature points, not surfaces, and keep their size.
    const spacing = Math.sqrt(area / Math.max(1, count));
    const size = n < 200000 ? base : Math.max(base, Math.min(2 * base, Math.round(spacing)));
    return { count, size };
  }

  _draw(opts = {}) {
    const t0 = performance.now();
    const c = this.canvas, dpr = window.devicePixelRatio || 1;
    const W = Math.max(1, Math.round(c.clientWidth * dpr)), H = Math.max(1, Math.round(c.clientHeight * dpr));
    if (c.width !== W || c.height !== H) { c.width = W; c.height = H; }
    const ctx = this.ctx;
    if (!this.cloud) {
      if (this.gl) { ctx.clearRect(0, 0, W, H); this._drawGL(null, W, H, 0, 1); }
      else { ctx.fillStyle = "#0b0e12"; ctx.fillRect(0, 0, W, H); }
      this._shown = { count: 0 }; return;
    }

    let fv = this.follow ? this._followView() : null;
    if (fv && this._tw) {  // glide from the previous camera view to this one
      const a = Math.min(1, (performance.now() - this._tw.t0) / this._tw.dur), e = a * a * (3 - 2 * a), F = this._tw.from;
      const L = (p, q) => p.map((v, i) => v + (q[i] - v) * e);
      const r0 = norm(L(F.M[0], fv.M[0])), r2 = norm(L(F.M[2], fv.M[2])), r1 = norm(cross(r2, r0));
      fv = { M: [r0, r1, r2], dist: F.dist + (fv.dist - F.dist) * e, cen: L(F.cen, fv.cen) };
      if (a < 1) requestAnimationFrame(() => this.request()); else this._tw = null;  // the last glide frame is a full one
    }
    this._fvShown = fv;
    this.canvas.dataset.follow = fv ? "on" : "off";
    const vp = this._viewParams(fv, W, H);
    const moving = opts.full ? false : opts.moving ?? (!!this._tw || performance.now() < this._moveUntil);
    const { count, size } = opts.full ? { count: this.cloud.count, size: Math.max(1, Math.round(this.pointSize * dpr)) } : this._budget(vp, W, H, moving);
    const { M, panX, panY, dist, cen, f, cx, cy, near } = vp;
    const project = (p) => {
      const v = apply(M, sub(p, cen)); const depth = dist - v[2];
      if (depth <= near) return null;
      return [cx + f * (v[0] + panX) / depth, cy - f * (v[1] + panY) / depth, depth];
    };

    let drawn;
    if (this.gl) { this._drawGL(vp, W, H, count, size); ctx.clearRect(0, 0, W, H); drawn = count; }
    else drawn = this._draw2D(vp, W, H, count, size);
    this._shown = { count, size, moving };

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
    if (this.measure?.pts?.length) this._drawMeasure(project, dpr);
    this.lastDrawn = drawn;
    if (this.overviewCanvas && !this.overviewCanvas.hidden && !opts.noOverview) this.drawOverview(this.overviewCanvas);
    // WebGL work is queued, not done, when drawArrays returns: wait for it so the time is the real frame time
    if (this.gl && !opts.noSync) { const px = new Uint8Array(4); this.gl.readPixels(0, 0, 1, 1, this.gl.RGBA, this.gl.UNSIGNED_BYTE, px); }
    const ms = performance.now() - t0;
    if (!moving && !opts.full && !opts.noSync) this._idleMs = this._idleMs == null ? ms : 0.7 * this._idleMs + 0.3 * ms;
    if (!opts.silent) this.onRender?.(ms, drawn);
    return ms;
  }

  // 2D fallback: z-buffered square splats into an ImageData (the original renderer, with the LOD prefix).
  _draw2D(vp, W, H, count, s) {
    const ctx = this.ctx, { M, panX, panY, dist, cen, f, cx, cy, near } = vp;
    const img = ctx.createImageData(W, H), px = new Uint32Array(img.data.buffer);
    const zbuf = new Float32Array(W * H).fill(Infinity);
    px.fill(0xff000000 | (BG[2] << 16) | (BG[1] << 8) | BG[0]);
    const { xyz, rgb, first } = this.cloud;
    const m = M.flat();
    let drawn = 0;
    const roiR2 = this.roi?.on ? this.roi.r * this.roi.r : Infinity;
    for (let i = 0; i < count; i++) {
      if (first[i] > this.visibleUpTo) continue;
      const x = xyz[3 * i] - cen[0], y = xyz[3 * i + 1] - cen[1], z = xyz[3 * i + 2] - cen[2];
      if (roiR2 !== Infinity && x * x + y * y + z * z > roiR2) continue;
      const vz = m[6] * x + m[7] * y + m[8] * z, depth = dist - vz;
      if (depth <= near) continue;
      const sx = Math.round(cx + f * (m[0] * x + m[1] * y + m[2] * z + panX) / depth - s / 2);
      const sy = Math.round(cy - f * (m[3] * x + m[4] * y + m[5] * z + panY) / depth - s / 2);
      if (sx < 0 || sy < 0 || sx >= W - s || sy >= H - s) continue;
      const col = 0xff000000 | (rgb[3 * i + 2] << 16) | (rgb[3 * i + 1] << 8) | rgb[3 * i];
      for (let dy = 0; dy < s; dy++) for (let dx = 0; dx < s; dx++) {
        const k = (sy + dy) * W + sx + dx;
        if (depth < zbuf[k]) { zbuf[k] = depth; px[k] = col; }
      }
      drawn++;
    }
    ctx.putImageData(img, 0, 0);
    return drawn;
  }

  // The projection used by _draw (free view, or the camera view while following); W, H in device pixels.
  _viewParams(fv, W, H) {
    const M = fv ? fv.M : this._matrix(), { panX, panY } = fv ? { panX: 0, panY: 0 } : this.view;
    const cen = fv ? fv.cen : this.center, dist = fv ? fv.dist : this.view.dist, c0 = this._center0 || cen;
    const far = dist + (this._extent || this.radiusFull || 1) + Math.hypot(...sub(cen, c0)) + 1e-6;
    return { M, panX, panY, dist, cen, f: 0.9 * Math.min(W, H), cx: W / 2, cy: H / 2, near: this.radius * 0.02, far };
  }

  // Points currently drawn (after the growth and Nearby filters) out of the on-screen prefix; counted on demand.
  visibleCount() {
    if (!this.cloud) return 0;
    if (!this.gl) return this.lastDrawn ?? 0;
    const n = this._shown.count || 0, roi = this.roi?.on ? `${this.roi.r}|${this.center.join(",")}` : "", key = `${n}|${this.visibleUpTo}|${roi}`;
    if (this._visCache?.key === key) return this._visCache.v;
    const { xyz, first } = this.cloud, r2 = this.roi?.on ? this.roi.r * this.roi.r : Infinity, c = this.center;
    let v = 0;
    for (let i = 0; i < n; i++) {
      if (first[i] > this.visibleUpTo) continue;
      if (r2 !== Infinity) { const x = xyz[3 * i] - c[0], y = xyz[3 * i + 1] - c[1], z = xyz[3 * i + 2] - c[2]; if (x * x + y * y + z * z > r2) continue; }
      v++;
    }
    this._visCache = { key, v }; return v;
  }

  // 3D point under the pointer: among the points drawn within `radiusPx` (CSS px) of the click, the nearest to the
  // viewer (what the depth test shows there). Only the prefix on screen is searched, so a picked point is always one
  // that is drawn. tol_units = the pick radius at that depth, in map units.
  pickPoint(clientX, clientY, radiusPx = 8) {
    if (!this.cloud) return null;
    const c = this.canvas, dpr = window.devicePixelRatio || 1, r = c.getBoundingClientRect();
    const W = c.width, H = c.height, px = (clientX - r.left) * dpr, py = (clientY - r.top) * dpr, rad = radiusPx * dpr;
    const { M, panX, panY, dist, cen, f, cx, cy, near } = this._viewParams(this.follow ? this._fvShown : null, W, H);
    const m = M.flat(), { xyz, first } = this.cloud, count = Math.min(this.cloud.count, this._shown.count || this.cloud.count);
    const roiR2 = this.roi?.on ? this.roi.r * this.roi.r : Infinity;
    let best = -1, bestDepth = Infinity;
    for (let i = 0; i < count; i++) {
      if (first[i] > this.visibleUpTo) continue;
      const x = xyz[3 * i] - cen[0], y = xyz[3 * i + 1] - cen[1], z = xyz[3 * i + 2] - cen[2];
      if (roiR2 !== Infinity && x * x + y * y + z * z > roiR2) continue;
      const depth = dist - (m[6] * x + m[7] * y + m[8] * z);
      if (depth <= near || depth >= bestDepth) continue;
      const sx = cx + f * (m[0] * x + m[1] * y + m[2] * z + panX) / depth, sy = cy - f * (m[3] * x + m[4] * y + m[5] * z + panY) / depth;
      if ((sx - px) ** 2 + (sy - py) ** 2 <= rad * rad) { best = i; bestDepth = depth; }
    }
    if (best < 0) return null;
    const p = [xyz[3 * best], xyz[3 * best + 1], xyz[3 * best + 2]], q = apply(M, sub(p, cen)), dq = dist - q[2];
    return { index: best, xyz: p, tol_units: rad * bestDepth / f,
      client: [r.left + (cx + f * (q[0] + panX) / dq) / dpr, r.top + (cy - f * (q[1] + panY) / dq) / dpr] };
  }

  // For tests: client coordinates of up to k drawn points spread across the view (sorted by x), and the colour the
  // renderer put at a client position (read back right after a synchronous redraw).
  sampleScreenPoints(k = 8) {
    if (!this.cloud) return [];
    const c = this.canvas, dpr = window.devicePixelRatio || 1, r = c.getBoundingClientRect();
    const vp = this._viewParams(this.follow ? this._fvShown : null, c.width, c.height), m = vp.M.flat();
    const { xyz, first } = this.cloud, n = Math.min(this.cloud.count, this._shown.count || 0), hits = [];
    for (let i = 0; i < n; i += Math.max(1, Math.floor(n / 20000))) {
      if (first[i] > this.visibleUpTo) continue;
      const x = xyz[3 * i] - vp.cen[0], y = xyz[3 * i + 1] - vp.cen[1], z = xyz[3 * i + 2] - vp.cen[2], depth = vp.dist - (m[6] * x + m[7] * y + m[8] * z);
      if (depth <= vp.near) continue;
      const sx = vp.cx + vp.f * (m[0] * x + m[1] * y + m[2] * z + vp.panX) / depth, sy = vp.cy - vp.f * (m[3] * x + m[4] * y + m[5] * z + vp.panY) / depth;
      if (sx < 16 * dpr || sy < 16 * dpr || sx > c.width - 16 * dpr || sy > c.height - 16 * dpr) continue;
      hits.push([r.left + sx / dpr, r.top + sy / dpr]);
    }
    hits.sort((a, b) => a[0] - b[0]);
    return hits.length < 2 * k ? [] : Array.from({ length: k }, (_, j) => hits[Math.floor((j + 0.5) / k * (hits.length - 1))]);
  }
  probePixel(clientX, clientY) {
    const c = this.canvas, dpr = window.devicePixelRatio || 1, r = c.getBoundingClientRect();
    const x = Math.round((clientX - r.left) * dpr), y = Math.round((clientY - r.top) * dpr);
    this._draw({ noSync: true, silent: true });
    if (this.gl) { const p = new Uint8Array(4); this.gl.readPixels(x, c.height - 1 - y, 1, 1, this.gl.RGBA, this.gl.UNSIGNED_BYTE, p); return [...p.slice(0, 3)]; }
    return [...this.ctx.getImageData(x, y, 1, 1).data.slice(0, 3)];
  }
  isBackground(rgb) { return Math.abs(rgb[0] - BG[0]) + Math.abs(rgb[1] - BG[1]) + Math.abs(rgb[2] - BG[2]) <= 3; }

  // Frame-time benchmark: `frames` synchronous redraws while the view turns; full = every point (no LOD).
  benchmark({ frames = 60, full = false, moving = false } = {}) {
    if (!this.cloud) return null;
    const yaw0 = this.view.yaw, times = [];
    this._draw({ full, moving, silent: true });  // warm-up (buffers, shader, caches)
    for (let i = 0; i < frames; i++) { this.view.yaw = yaw0 + 0.02 * (i + 1); times.push(this._draw({ full, moving, silent: true })); }
    this.view.yaw = yaw0; this.request();
    times.sort((a, b) => a - b);
    const q = (p) => times[Math.min(times.length - 1, Math.floor(p * times.length))];
    return { renderer: this.renderer, points_in_file: this.cloud.count, points_drawn: this._shown.count, point_size_px: this._shown.size,
      lod: full ? "off (every point)" : moving ? "moving" : "idle", frames, p50_ms: +q(0.5).toFixed(2), p95_ms: +q(0.95).toFixed(2),
      canvas_px: [this.canvas.width, this.canvas.height], dpr: window.devicePixelRatio || 1 };
  }

  setMeasure(m) { this.measure = m; this.request(); }
  _drawMeasure(project, dpr) {
    const ctx = this.ctx, q = this.measure.pts.map(project);
    ctx.lineWidth = 2 * dpr; ctx.strokeStyle = "#ffe14d"; ctx.fillStyle = "#ffe14d";
    if (q.length === 2 && q[0] && q[1]) { ctx.beginPath(); ctx.moveTo(q[0][0], q[0][1]); ctx.lineTo(q[1][0], q[1][1]); ctx.stroke(); }
    ctx.font = `600 ${12 * dpr}px system-ui, sans-serif`;
    q.forEach((p, k) => { if (!p) return; ctx.beginPath(); ctx.arc(p[0], p[1], 5 * dpr, 0, 7); ctx.stroke(); ctx.fillText(String(k + 1), p[0] + 7 * dpr, p[1] - 7 * dpr); });
    if (q.length === 2 && q[0] && q[1] && this.measure.text) {
      const mx = (q[0][0] + q[1][0]) / 2, my = (q[0][1] + q[1][1]) / 2 - 10 * dpr, w = ctx.measureText(this.measure.text).width;
      ctx.fillStyle = "rgba(11,14,18,.85)"; ctx.fillRect(mx - w / 2 - 6 * dpr, my - 14 * dpr, w + 12 * dpr, 20 * dpr);
      ctx.fillStyle = "#ffe14d"; ctx.textAlign = "center"; ctx.fillText(this.measure.text, mx, my); ctx.textAlign = "start";
    }
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
      drag = { x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY, pan: e.shiftKey || e.button === 2 }; c.setPointerCapture(e.pointerId); });
    c.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y; drag.x = e.clientX; drag.y = e.clientY;
      if (!dx && !dy) return;
      if (drag.pan) { const k = this.view.dist / (0.9 * Math.min(c.clientWidth, c.clientHeight)); this.view.panX += dx * k; this.view.panY -= dy * k; }
      else { this.view.yaw += dx * 0.008; this.view.pitch = Math.max(-1.5, Math.min(1.5, this.view.pitch + dy * 0.008)); }
      this._moved();
    });
    const end = () => { drag = null; };
    // In pick mode a click (no drag) picks a 3D point; dragging still orbits the view.
    c.addEventListener("pointerup", (e) => {
      const click = drag && !drag.pan && Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0) < 4;
      end();
      if (click && this.pickMode) this.onPoint?.(this.pickPoint(e.clientX, e.clientY));
    }); c.addEventListener("pointercancel", end);
    c.addEventListener("contextmenu", (e) => e.preventDefault());
    c.addEventListener("wheel", (e) => { e.preventDefault(); this.zoom(Math.exp(e.deltaY * 0.001)); }, { passive: false });
    c.addEventListener("keydown", (e) => {
      const k = { ArrowLeft: () => (this.view.yaw -= 0.1), ArrowRight: () => (this.view.yaw += 0.1),
        ArrowUp: () => (this.view.pitch = Math.max(-1.5, this.view.pitch - 0.1)), ArrowDown: () => (this.view.pitch = Math.min(1.5, this.view.pitch + 0.1)),
        "+": () => this.zoom(0.85), "=": () => this.zoom(0.85), "-": () => this.zoom(1.18), r: () => this.resetView(), R: () => this.resetView(), t: () => this.topView(), T: () => this.topView() }[e.key];
      if (k && this.cloud) { e.preventDefault(); e.stopPropagation(); k(); this._moved(); }
    });
  }

  zoom(factor) { this.view.dist = Math.max(this.radius * 0.1, Math.min(this.radius * 30, this.view.dist * factor)); this._moved(); }
}

// Fisher-Yates over the three point arrays together, with a fixed seed so the order (and the LOD sample) is repeatable.
function shuffleCloud(cloud) {
  const { xyz, rgb, first, count } = cloud;
  let s = 0x9e3779b9;
  const rnd = () => { s |= 0; s = (s + 0x6d2b79f5) | 0; let t = Math.imul(s ^ (s >>> 15), 1 | s); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  for (let i = count - 1; i > 0; i--) {
    const j = Math.floor(rnd() * (i + 1));
    for (let a = 0; a < 3; a++) {
      const x = xyz[3 * i + a]; xyz[3 * i + a] = xyz[3 * j + a]; xyz[3 * j + a] = x;
      const c = rgb[3 * i + a]; rgb[3 * i + a] = rgb[3 * j + a]; rgb[3 * j + a] = c;
    }
    const f = first[i]; first[i] = first[j]; first[j] = f;
  }
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
