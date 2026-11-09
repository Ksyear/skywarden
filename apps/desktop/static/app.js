import { PointViewer, parsePly } from "./viewer.js";
import { initTracking } from "./tracking.js";
import { initLive } from "./live.js";

const $ = (id) => document.getElementById(id);
const STAGES = { SELECT_FRAMES: "Frames", EXTRACT_FEATURES: "Features", MATCH_FEATURES: "Matching", MAP: "Mapping", EXPORT: "Export" };
const TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"]);
const QUALITY_TONE = { GOOD: "ok", PARTIAL: "warn", POOR: "error", UNRATED: "offline" };
const QUALITY_HINT = {
  GOOD: "All map-quality checks pass for the declared input",
  PARTIAL: "Only part of the input was reconstructed, or the path has jumps / several separate models",
  POOR: "The reconstruction is not usable as a map of this input",
  UNRATED: "Built before map-quality checks; run `python -m mapping.cli rate <session>`",
};
const HINTS = {
  SELECT_FRAMES: "Check the input path/format, or record a steadier clip with more sideways motion.",
  EXTRACT_FEATURES: "Frames may be too dark or textureless; add light or texture.",
  MATCH_FEATURES: "Too little overlap between frames; raise the sample rate.",
  MAP: "No geometry could be initialised: avoid pure rotation, move the camera sideways, or try without the calibration file.",
  EXPORT: "Reconstruction too small to pass the quality gate; capture more overlapping frames.",
};

// ---- Single UI state object. Server-side job state is never copied here beyond the last poll result. ----
const state = {
  conn: "CONNECTING", failures: 0, lastContact: 0, apiMs: [],
  inputs: [], sessions: [],
  jobSid: null, jobInfo: null,
  loaded: null,      // {sid, report, poses, frames, evaluation, loadedAt}
  loadToken: 0,      // guards against an older load finishing after a newer one
  idx: 0, playing: false, growth: false,
  renderMs: [],
};

let tracking = null;
let live = null;  // tracking workspace (tracking.js), created once the helpers below exist
const viewer = new PointViewer($("map-canvas"), {
  onRender: (ms) => { state.renderMs.push(ms); if (state.renderMs.length > 120) state.renderMs.shift(); },
});

function pct(arr, p) { if (!arr.length) return null; const s = [...arr].sort((a, b) => a - b); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; }
function announce(text) { $("announcer").textContent = text; }
function badge(text, tone, title = "") { const b = document.createElement("span"); b.className = "badge"; b.dataset.tone = tone; b.textContent = text; if (title) b.title = title; return b; }
function fmtAge(s) { return s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : s < 172800 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} d`; }

async function api(path, opts = {}) {
  const t0 = performance.now();
  const ctrl = new AbortController(); const timer = setTimeout(() => ctrl.abort(), opts.timeout ?? 5000);
  try {
    const res = await fetch(path, { ...opts, signal: ctrl.signal, headers: opts.body ? { "Content-Type": "application/json" } : {} });
    state.apiMs.push(performance.now() - t0); if (state.apiMs.length > 60) state.apiMs.shift();
    markOnline();
    if (!res.ok) { let msg = res.statusText; try { msg = (await res.json()).error || msg; } catch {} throw Object.assign(new Error(msg), { status: res.status }); }
    return opts.raw ? res : res.json();
  } catch (e) {
    if (e.status === undefined) markFailure();   // network-level failure: server unreachable or timeout
    throw e;
  } finally { clearTimeout(timer); }
}

// ---------------- connection ----------------
function markOnline() { state.failures = 0; state.lastContact = Date.now(); setConn("ONLINE"); }
function markFailure() { state.failures++; setConn(state.failures >= 3 ? "DISCONNECTED" : "RECONNECTING"); }
function setConn(c) {
  if (state.conn === c) return;
  state.conn = c;
  const b = $("conn-badge");
  b.textContent = c === "ONLINE" ? "SERVER ONLINE" : c;
  b.dataset.tone = { ONLINE: "ok", RECONNECTING: "warn", DISCONNECTED: "error", CONNECTING: "offline" }[c];
  announce(`Server ${c.toLowerCase()}`);
  renderControls();
  tracking?.renderControls();
  live?.renderControls();
}

async function healthLoop() {
  try {
    const h = await api("/api/health", { timeout: 3000 });
    $("armed").textContent = String(h.hardware_armed); $("dryrun").textContent = String(h.dry_run);
    if (h.hardware_armed !== false) { $("armed").style.color = "var(--error)"; }
    if (h.running_job && !state.jobSid) { state.jobSid = h.running_job; pollJob(); }  // reattach after reload/reconnect
    tracking.onHealth(h);
    if (h.running_run_job) live.reattach(h.running_run_job);
    if (!state.inputs.length) await refreshInputs();
  } catch {}
  const delay = state.conn === "ONLINE" ? 2000 : Math.min(10000, 1000 * 2 ** state.failures);
  setTimeout(healthLoop, delay);
}

// ---------------- inputs & sessions ----------------
async function refreshInputs() {
  state.inputs = await api("/api/inputs");
  const sel = $("input-select"); const prev = sel.value; sel.replaceChildren();
  if (!state.inputs.length) { const o = new Option("No inputs found (put a video in assets/input/)", ""); o.disabled = true; sel.add(o); }
  for (const i of state.inputs) sel.add(new Option(`${i.label} · ${i.kind} · ${i.origin}`, i.id));
  if (prev && state.inputs.some((i) => i.id === prev)) sel.value = prev;
  describeInput(); renderControls();
}
function describeInput() {
  const i = state.inputs.find((x) => x.id === $("input-select").value);
  $("input-origin").textContent = i ? `${i.id}${i.camera_json ? " · calibration: " + i.camera_json.split("/").pop() : " · no calibration file (COLMAP will estimate)"}` : "";
}

async function refreshSessions() {
  try { state.sessions = await api("/api/sessions"); } catch { return; }
  const ul = $("sessions"); ul.replaceChildren();
  if (!state.sessions.length) { const li = document.createElement("li"); li.className = "muted"; li.textContent = "No stored maps yet."; ul.append(li); }
  for (const s of state.sessions) {
    const li = document.createElement("li"); const b = document.createElement("button");
    b.type = "button"; b.setAttribute("aria-current", String(state.loaded?.sid === s.id));
    const sid = document.createElement("span"); sid.className = "sid"; sid.textContent = s.id;
    // A finished build is labelled by its MAP QUALITY, never just "ready": PARTIAL / POOR / UNRATED are not normal completions.
    const ok = s.state === "SUCCEEDED", sim = s.labels?.map_source === "SIMULATED";
    const tone = ok ? QUALITY_TONE[s.quality] ?? "offline" : s.state === "RUNNING" || s.state === "STARTING" ? "warn" : "error";
    const st = badge(ok ? `${sim ? "SIM · " : ""}${s.quality}` : s.state, tone, ok ? QUALITY_HINT[s.quality] ?? "" : "");
    const meta = document.createElement("span"); meta.className = "meta";
    meta.textContent = s.state === "SUCCEEDED" ? `${s.registered} frames · ${s.points3D.toLocaleString()} pts${s.num_models > 1 ? ` · ${s.num_models} separate models` : ""} · ${s.labels?.data_origin}` : (s.created_at || "").slice(0, 19).replace("T", " ");
    b.append(sid, st, meta);
    b.disabled = s.state !== "SUCCEEDED";
    b.title = b.disabled ? "Only finished builds can be opened" : "Open this stored map";
    b.addEventListener("click", () => loadSession(s.id));
    li.append(b); ul.append(li);
  }
}

// ---------------- jobs ----------------
async function startBuild(ev) {
  ev.preventDefault();
  if (state.conn !== "ONLINE" || state.jobSid) return;
  const fps = parseFloat($("fps").value), maxFrames = parseInt($("max-frames").value, 10);
  if (!(fps >= 0.5 && fps <= 30) || !(maxFrames >= 3 && maxFrames <= 600)) { showJobMsg("error", "Invalid settings", "Sample rate must be 0.5–30 fps and max frames 3–600."); return; }
  $("build").disabled = true;
  try {
    const r = await api("/api/jobs", { method: "POST", body: JSON.stringify({ input_id: $("input-select").value, fps, max_frames: maxFrames, use_calibration: $("use-calib").checked }) });
    state.jobSid = r.session; state.jobInfo = null; announce("Build started"); tracking.renderControls();
    pollJob(); refreshSessions();
  } catch (e) { showJobMsg("error", "Could not start", e.message); }
  renderControls();
}

async function pollJob() {
  const sid = state.jobSid; if (!sid) return;
  try {
    const info = await api(`/api/sessions/${encodeURIComponent(sid)}`);
    if (sid !== state.jobSid) return;
    state.jobInfo = info; renderJob();
    if (TERMINAL.has(info.state)) {
      state.jobSid = null; renderControls(); tracking.renderControls(); refreshSessions();
      if (info.state === "SUCCEEDED") loadSession(sid);
      return;
    }
  } catch (e) { if (e.status === 404 && state.jobInfo === null) { /* child not started yet */ } }
  setTimeout(pollJob, state.conn === "ONLINE" ? 1000 : 3000);
}

function renderJob() {
  const info = state.jobInfo; if (!info) return;
  const st = info.status || {}, s = info.state;
  const jb = $("job-badge");
  jb.textContent = s === "RUNNING" && info.stale ? "STALE" : s;
  jb.dataset.tone = s === "SUCCEEDED" ? "ok" : s === "RUNNING" || s === "STARTING" ? (info.stale ? "warn" : "real") : s === "CANCELLED" ? "warn" : "error";
  const ol = $("steps"); ol.replaceChildren();
  const failedStage = info.report?.failure?.stage;
  Object.entries(STAGES).forEach(([key, label], i) => {
    let ds = "wait", txt = "WAITING";
    if (s === "SUCCEEDED" || i < st.stage_index) { ds = "done"; txt = "OK"; }
    else if (i === st.stage_index) {
      if (s === "RUNNING") { ds = "active"; txt = st.progress ? `${st.progress[0]}/${st.progress[1]}` : "RUNNING"; }
      else if (failedStage === key || s !== "SUCCEEDED") { ds = "failed"; txt = s; }
    }
    const li = document.createElement("li"); li.dataset.s = ds;
    li.append(Object.assign(document.createElement("span"), { textContent: label }), Object.assign(document.createElement("span"), { textContent: txt }));
    ol.append(li);
  });
  const frac = s === "SUCCEEDED" ? 1 : Math.max(0, (st.stage_index ?? 0) + (st.progress ? st.progress[0] / Math.max(1, st.progress[1]) : 0)) / 5;
  $("job-bar").style.width = `${Math.round(frac * 100)}%`;
  if (s === "RUNNING" || s === "STARTING") showJobMsg(info.stale ? "warn" : "", info.stale ? `No update for ${info.status_age_s} s` : "Building (CURRENT_SESSION)", st.message || "starting…");
  else if (s === "SUCCEEDED") {
    const q = info.report.quality || {}, r = info.report.reconstruction;
    const good = q.verdict === "GOOD";
    showJobMsg(good ? "ok" : "warn", good ? "Map built · quality GOOD" : `Build finished · map quality ${q.verdict ?? "UNRATED"}`,
      `${r.registered_images}/${r.selected_frames} frames in the largest of ${r.num_models} model(s).${good ? "" : ` Failed checks: ${(q.failed_checks || []).join(", ") || "not rated"}.`}`);
  }
  else if (s === "CANCELLED") showJobMsg("warn", "Build cancelled", "Partial frames are kept in the session folder. Start a new build when ready.");
  else {
    const f = info.report?.failure; const msg = f?.message || st.message || "The build process ended without a result.";
    showJobMsg("error", s === "INTERRUPTED" ? "Build interrupted" : `Failed at ${STAGES[f?.stage] || "unknown stage"}`, `${msg} ${HINTS[f?.stage] || "See process.log in the session folder."}`);
  }
}
function showJobMsg(tone, title, text) {
  const el = $("job-msg"); el.dataset.tone = tone; el.replaceChildren();
  const b = document.createElement("b"); b.textContent = title + ". "; el.append(b, document.createTextNode(text));
}

async function cancelJob() {
  $("cancel-confirm").hidden = true;
  if (!state.jobSid) return;
  try { await api(`/api/jobs/${encodeURIComponent(state.jobSid)}/cancel`, { method: "POST", body: "{}" }); announce("Cancel requested"); }
  catch (e) { showJobMsg("error", "Cancel failed", e.message); }
}

// ---------------- loading a stored map ----------------
async function loadSession(sid, modelRank = 0) {
  const token = ++state.loadToken;
  stopPlay();
  setEmpty("Loading map…", `Reading ${sid} (PLY + trajectory).`, "");
  try {
    const base = `/api/sessions/${encodeURIComponent(sid)}`;
    const info = await api(base);
    const models = info.report?.reconstruction?.models || null;  // per-model exports (report schema @2)
    const prefix = models && modelRank > 0 ? `models/${modelRank}/` : "";
    const pf = report_point_filter(info.report, modelRank);
    const densePaths = { dense: "dense", dense_exp: "dense_experimental", dense_static: "dense_static", dense_static_v2: "dense_static_v2", dense_static_v3: "dense_static_v3", dense_learned: "dense_learned", model_estimated: "model_estimated" };
    const denseReports = Object.fromEntries(await Promise.all(Object.entries(densePaths).map(async ([key, dir]) =>
      [key, modelRank === 0 ? await api(`${base}/files/${dir}/dense_report.json`).catch(() => null) : null])));
    let layer = $("layer").value;
    if ((layer === "filtered" && !pf) || (layer in densePaths && !denseReports[layer]?.dense_verified_points)) layer = "sparse";
    const useFiltered = layer === "filtered";
    const plyPath = layer in densePaths ? `${densePaths[layer]}/dense_verified.ply`
      : `${prefix}${useFiltered ? "sparse_filtered.ply" : "sparse.ply"}`;
    const [traj, plyRes, coverage] = await Promise.all([api(`${base}/files/${prefix}trajectory.json`),
      api(`${base}/files/${plyPath}`, { raw: true, timeout: 60000 }),
      api(`${base}/files/coverage.json`).catch(() => null)]);
    const cloud = parsePly(await plyRes.arrayBuffer());
    if (token !== state.loadToken) return;  // a newer load started meanwhile
    const report = info.report;
    const expected = denseReports[layer]?.dense_verified_points
      ?? (useFiltered ? pf.filtered : models ? models[modelRank].points3D : report.reconstruction.points3D);
    if (cloud.count !== expected) throw new Error(`PLY has ${cloud.count} points, report says ${expected}`);
    const poses = traj.poses.map((p) => ({ ...p, R: p.T_world_camera.slice(0, 3).map((r) => r.slice(0, 3)), C: p.T_world_camera.slice(0, 3).map((r) => r[3]) }));
    state.loaded = { sid, report, poses, evaluation: info.evaluation, loadedAt: Date.now(), modelRank, models, coverage,
      pointFilter: pf, filtered: !!useFiltered, layer, dense: denseReports.dense,
      denseExp: denseReports.dense_exp, denseStatic: denseReports.dense_static, denseStaticV2: denseReports.dense_static_v2, denseStaticV3: denseReports.dense_static_v3, denseLearned: denseReports.dense_learned, modelEstimated: denseReports.model_estimated };
    $("layer").value = layer;
    $("layer").options[1].disabled = !pf;
    for (const option of $("layer").options) {
      if (option.value in densePaths) option.disabled = !denseReports[option.value]?.dense_verified_points;
    }
    renderModelSelect();
    // Dense layers are static maps: frame the observed surfaces and hide path/cameras until asked for
    const staticView = layer.startsWith("dense");
    viewer.setData(cloud, poses, { frameOnCloud: staticView });
    setOverlays(!staticView, !staticView);
    viewer.setBreaks(modelRank === 0 && coverage ? (coverage.jumps_in_largest_model || []).map((j) => j.t_to) : []);
    $("map-empty").hidden = true;
    renderLabels(); renderMetrics(); refreshSessions();
    setIndex(0, true);
    renderControls();
    announce(`Loaded ${sid}: ${poses.length} cameras, ${cloud.count} points`);
  } catch (e) {
    if (token !== state.loadToken) return;
    state.loaded = null; viewer.clear(); renderLabels(); renderControls();
    setEmpty("Could not load map", e.message, "error");
  }
}
function setEmpty(title, text, tone) {
  const el = $("map-empty"); el.hidden = false; el.dataset.tone = tone;
  $("empty-title").textContent = title; $("empty-text").textContent = text;
}

function renderLabels() {
  const L = state.loaded?.report.labels, el = $("badges"); el.replaceChildren();
  $("session-label").textContent = state.loaded ? state.loaded.sid : "no map loaded";
  const tl = $("map-overlay-tl"), bl = $("map-overlay-bl"); tl.replaceChildren(); bl.replaceChildren();
  if (!L) return;
  const sim = L.map_source === "SIMULATED";
  const q = state.loaded.report.quality?.verdict ?? "UNRATED";
  el.append(
    badge(`QUALITY ${q}`, QUALITY_TONE[q] ?? "offline", `${QUALITY_HINT[q] ?? ""}${state.loaded.report.quality?.failed_checks?.length ? ` · failed: ${state.loaded.report.quality.failed_checks.join(", ")}` : ""}`),
    badge(L.map_source, sim ? "sim" : "real", sim ? "Generated from a synthetic render with known ground truth" : "Loaded from a finished reconstruction on disk"),
    badge(`${L.data_origin}`, sim ? "sim" : "real"),
    badge(`SCALE ${L.scale}`, "warn", "Monocular reconstruction: distances have no metric unit"),
    badge(L.nav_safety, "error", "Never use this map for obstacle avoidance or flight decisions"),
    badge(`CALIB ${state.loaded.report.calibration?.status ?? "?"}`, state.loaded.report.calibration?.status === "PROVIDED" ? "ok" : "warn"),
  );
  const chip = (t, cls = "") => Object.assign(document.createElement("span"), { className: `chip ${cls}`, textContent: t });
  tl.append(chip(sim ? "SIMULATED data — not a measurement" : "Replay of stored reconstruction — not live mapping", sim ? "" : ""));
  const layer = state.loaded.layer || "sparse";
  const layerChip = { sparse: "SPARSE POINTS (SfM) — feature points only, not walls or floors",
    filtered: "SPARSE POINTS, FILTERED — triangulation angle ≥ 1.5°, ≥ 3 views, low reprojection; raw points kept",
    dense: `DENSE · VERIFIED — ${denseMethod(state.loaded.dense)}; unobserved areas stay empty`,
    dense_exp: `DENSE · EXPERIMENTAL, NOT ADOPTED — ${denseMethod(state.loaded.denseExp)}; ${state.loaded.denseExp?.experimental?.reason ?? "failed the frozen acceptance"}`,
    dense_static: "DENSE · STATIC SCENE TRIAL — sky and person masks applied during matching and depth verification",
    dense_static_v2: `DENSE · STATIC SCENE v2, NOT ADOPTED — ${denseMethod(state.loaded.denseStaticV2)}; ${state.loaded.denseStaticV2?.experimental?.reason ?? ""}`,
    dense_static_v3: `DENSE · STATIC SCENE v3, ${state.loaded.denseStaticV3?.experimental?.status ?? "TRIAL"} — ${denseMethod(state.loaded.denseStaticV3)}; ${state.loaded.denseStaticV3?.experimental?.reason ?? ""}`,
    dense_learned: `DENSE + LEARNED DEPTH, ${state.loaded.denseLearned?.experimental?.status ?? "TRIAL"} — ${state.loaded.denseLearned?.experimental?.reason ?? "learned depth kept only where other frames and measured surfaces agree"}`,
    model_estimated: "MODEL ESTIMATED — learned depth that multi-view geometry did NOT confirm; a guess, never part of the map" }[layer];
  const shown = viewer.cloud?.count;
  const trial = layer === "dense_exp" || layer.startsWith("dense_static") || layer === "dense_learned" || layer === "model_estimated";
  tl.append(chip(`LAYER ▸ ${({ sparse: "SPARSE · RAW", filtered: "SPARSE · FILTERED", dense: "DENSE · VERIFIED", dense_exp: "DENSE · EXPERIMENTAL", dense_static: "DENSE · STATIC SCENE", dense_static_v2: "DENSE · STATIC SCENE v2", dense_static_v3: "DENSE · STATIC SCENE v3", dense_learned: "DENSE + LEARNED DEPTH", model_estimated: "MODEL ESTIMATED (NOT VERIFIED)" })[layer]}${shown != null ? ` · ${shown.toLocaleString()} pts in file` : ""}`, trial ? "layer warn" : "layer"), chip(layerChip, trial ? "warn" : ""));
  const M = state.loaded.models;
  if (q !== "GOOD" || (M && M.length > 1)) {
    const m = M ? M[state.loaded.modelRank] : null, n = state.loaded.report.reconstruction.selected_frames;
    tl.append(chip(M && M.length > 1 ? `PARTIAL RECONSTRUCTION — model ${state.loaded.modelRank + 1} of ${M.length} (${m.registered_images}/${n} frames); models have separate coordinate frames and are never merged`
                                    : `PARTIAL RECONSTRUCTION — quality ${q}`, "warn"));
  }
  const geom = { sparse: L.geometry, filtered: `${L.geometry} · FILTERED`, dense: "DENSE_VERIFIED_MVS", dense_exp: "DENSE_EXPERIMENTAL_MVS", dense_static: "DENSE_STATIC_MVS_TRIAL", dense_static_v2: "DENSE_STATIC_MVS_TRIAL_V2", dense_static_v3: "DENSE_STATIC_MVS_TRIAL_V3", dense_learned: "MVS + LEARNED_DEPTH_VERIFIED", model_estimated: "MODEL_ESTIMATED" }[layer] ?? L.geometry;
  bl.append(chip("Relative units · scale unknown", "warn"), chip(`${geom} · ${state.loaded.poses.length} cameras`));
  $("map-title").textContent = { sparse: "Sparse 3D map", filtered: "Sparse 3D map · filtered", dense: "Dense 3D map · verified", dense_exp: "Dense 3D map · experimental", dense_static: "Dense 3D map · static scene trial", dense_static_v2: "Dense 3D map · static scene v2 trial", dense_static_v3: "Dense 3D map · static scene v3 trial", dense_learned: "Dense 3D map · with learned depth", model_estimated: "Model estimate (not a map)" }[layer] ?? "3D map";
}

function renderMetrics() {
  const r = state.loaded?.report.reconstruction, ev = state.loaded?.evaluation;
  $("m-reg").textContent = r ? `${r.registered_images}/${r.selected_frames} (${Math.round(r.registered_ratio * 100)}%)` : "—";
  $("m-pts").textContent = r ? r.points3D.toLocaleString() : "—";
  $("m-err").textContent = r ? `${r.mean_reprojection_error_px.toFixed(2)} px` : "—";
  $("m-ate").textContent = ev ? `${(ev.ate_rmse_m * 100).toFixed(1)} cm RMSE (Sim3-aligned to ground truth, ${ev.matched_poses} poses)` : "not evaluated (no ground truth)";
}

function denseMethod(d) {
  // Which dense method produced the file on screen (older reports carry no config: fronto sweep + next-view check)
  const c = d?.config || {};
  const sweep = c.sweep === "fronto+ground" ? "fronto + ground-plane sweep" : "fronto-parallel sweep";
  const ver = c.verify === "multi" ? `checked against sparse points and every view within ±${c.verify_window ?? 8} (any contradiction drops a point)` : "checked against sparse points and the next view";
  const vox = c.voxel_near_scale && c.voxel_near_scale !== 1 ? ` · near voxel ×${c.voxel_near_scale}` : "";
  const masks = d?.static_masks ? (c.mask_cost === "valid_pixels" ? " · sky/person masks: cost over unmasked window pixels only" : " · sky/person masks: window erosion") : "";
  const extra = (c.neighbour_rule === "median_then_near_far" ? " · far-surface neighbours (±12)" : "") + (c.coarse_ground ? " · half-resolution floor fill" : "");
  return `${sweep}, ${ver}${vox}${masks}${extra}`;
}

function report_point_filter(report, rank) {
  const pf = report?.point_filter;
  return pf ? pf[String(rank)] ?? null : null;
}

// ---------------- separate models + coverage ----------------
function renderModelSelect() {
  const sel = $("model-select"), M = state.loaded?.models;
  sel.hidden = !(M && M.length > 1);
  if (sel.hidden) return;
  const n = state.loaded.report.reconstruction.selected_frames;
  sel.replaceChildren(...M.map((m, i) => new Option(`model ${i + 1} of ${M.length} · ${m.registered_images}/${n} frames${m.time_range_s ? ` · ${m.time_range_s[0].toFixed(1)}–${m.time_range_s[1].toFixed(1)} s` : ""}`, String(i))));
  sel.value = String(state.loaded.modelRank);
}
function drawCoverage() {
  const c = $("coverage"), g = c.getContext("2d"), dpr = window.devicePixelRatio || 1;
  const r = c.getBoundingClientRect(); c.width = Math.max(1, Math.round(r.width * dpr)); c.height = Math.max(1, Math.round(r.height * dpr));
  g.clearRect(0, 0, c.width, c.height);
  const cov = state.loaded?.coverage; $("coverage-wrap").hidden = !cov; if (!cov) return;
  const css = getComputedStyle(document.documentElement), v = (k) => css.getPropertyValue(k).trim();
  const F = cov.frames, n = F.length, w = c.width / n, rank = state.loaded.modelRank;
  F.forEach((f, i) => {
    g.fillStyle = f.models.includes(rank) ? v("--accent") : f.models.length ? v("--muted") : v("--error");
    g.globalAlpha = f.models.includes(rank) ? 1 : 0.55; g.fillRect(i * w, 0, Math.ceil(w), c.height * 0.7);
    if (i > 0 && f.shot !== F[i - 1].shot) { g.globalAlpha = 1; g.fillStyle = v("--text"); g.fillRect(i * w - dpr / 2, 0, dpr, c.height); }
  });
  g.globalAlpha = 1; g.fillStyle = v("--warn");
  if (rank === 0) for (const j of cov.jumps_in_largest_model || []) { const i = F.findIndex((f) => f.timestamp_s >= j.t_to); if (i >= 0) g.fillRect(i * w - 2 * dpr, c.height * 0.7, 4 * dpr, c.height * 0.3); }
  const p = state.loaded.poses[state.idx], cur = p ? F.findIndex((f) => f.file === p.image_name) : -1;
  if (cur >= 0) { g.fillStyle = v("--focus"); g.fillRect((cur + 0.5) * w - dpr, 0, 2 * dpr, c.height); }
  const missing = F.filter((f) => !f.models.length).length;
  $("coverage-meta").textContent = `${F.length - missing}/${F.length} selected frames in some model · ${F.filter((f) => f.models.includes(rank)).length} in this model · ${(cov.jumps_in_largest_model || []).length} path jumps · ${new Set(F.map((f) => f.shot)).size} shot(s)`;
}

// ---------------- replay ----------------
let frameToken = 0;
function setIndex(i, force = false) {
  const L = state.loaded; if (!L) return;
  i = Math.max(0, Math.min(L.poses.length - 1, i));
  if (i === state.idx && !force) return;
  state.idx = i;
  const p = L.poses[i];
  $("scrub").max = L.poses.length - 1; $("scrub").value = i;
  $("pose-count").textContent = `${i + 1} / ${L.poses.length}`;
  $("frame-meta").textContent = `source #${p.source_index}${p.timestamp_s != null ? ` · t=${p.timestamp_s.toFixed(3)} s` : ""} · ${p.num_points3D} pts`;
  viewer.setCurrent(i);
  viewer.setVisibleUpTo(state.growth ? i : Infinity);
  // Only the newest frame request matters: older ones are ignored when they arrive.
  const tok = ++frameToken, img = $("frame-img");
  const url = `/api/sessions/${encodeURIComponent(L.sid)}/files/frames/${p.image_name}`;
  return new Promise((resolve) => {
    const probe = new Image();
    probe.onload = () => { if (tok === frameToken) { img.src = url; img.hidden = false; $("frame-empty").hidden = true; } resolve(); };
    probe.onerror = () => { if (tok === frameToken) { img.hidden = true; $("frame-empty").hidden = false; $("frame-empty").textContent = `Frame ${p.image_name} is missing from the session folder.`; } resolve(); };
    probe.src = url;
    renderFilmstrip();
    drawCoverage();
  });
}
function renderFilmstrip() {
  const L = state.loaded, fs = $("filmstrip"); fs.replaceChildren(); if (!L) return;
  const from = Math.max(0, Math.min(state.idx - 3, L.poses.length - 7));
  for (let k = from; k < Math.min(L.poses.length, from + 7); k++) {
    const b = document.createElement("button"); b.type = "button"; b.setAttribute("aria-current", String(k === state.idx));
    b.setAttribute("aria-label", `Frame ${k + 1}`);
    const im = document.createElement("img"); im.loading = "lazy"; im.alt = ""; im.src = `/api/sessions/${encodeURIComponent(L.sid)}/files/frames/${L.poses[k].image_name}`;
    b.append(im); b.addEventListener("click", () => { stopPlay(); setIndex(k); }); fs.append(b);
  }
}
async function playLoop() {
  while (state.playing && state.loaded) {
    const t0 = performance.now();
    if (state.idx >= state.loaded.poses.length - 1) { stopPlay(); break; }
    await setIndex(state.idx + 1);         // waits for the image: no request queue builds up
    await new Promise((r) => setTimeout(r, Math.max(0, 200 - (performance.now() - t0))));
  }
}
function togglePlay() {
  if (!state.loaded) return;
  state.playing = !state.playing;
  if (state.playing && state.idx >= state.loaded.poses.length - 1) setIndex(0);
  $("play").setAttribute("aria-pressed", String(state.playing)); $("play").textContent = state.playing ? "❚❚" : "▶︎";
  $("play").setAttribute("aria-label", state.playing ? "Pause replay (Space)" : "Play replay (Space)");
  if (state.playing) playLoop();
}
function stopPlay() { if (state.playing) togglePlay(); }

// ---------------- controls & status bar ----------------
function renderControls() {
  const online = state.conn === "ONLINE", has = !!state.loaded, running = !!state.jobSid;
  const trackRunning = (tracking?.trackJobRunning() ?? false) || (live?.running() ?? false);
  $("build").disabled = !online || running || trackRunning || !$("input-select").value;
  $("build").title = trackRunning ? "A tracking run is in progress; one job at a time" : "";
  $("cancel").disabled = !online || !running;
  if (!running) $("cancel-confirm").hidden = true;
  for (const id of ["prev", "play", "next", "scrub", "growth", "reset-view", "framing", "show-path", "show-cams"]) $(id).disabled = !has;
  $("layer").disabled = !has;
  $("input-select").disabled = running; $("fps").disabled = running; $("max-frames").disabled = running; $("use-calib").disabled = running;
}

function statusTick() {
  const p50 = pct(state.renderMs, 0.5), p95 = pct(state.renderMs, 0.95);
  const pf = state.loaded?.pointFilter, dn = state.loaded?.dense;
  const counts = (pf ? ` · sparse raw ${pf.raw.toLocaleString()} / filtered ${pf.filtered.toLocaleString()}` : "")
    + (dn && dn.dense_verified_points ? ` · dense verified ${dn.dense_verified_points.toLocaleString()}` : "")
    + (state.loaded?.denseExp?.dense_verified_points ? ` · dense experimental ${state.loaded.denseExp.dense_verified_points.toLocaleString()}` : "")
    + (state.loaded ? ` · showing ${state.loaded.layer}` : "");
  $("m-render").textContent = p50 == null ? "—" : `${p50.toFixed(1)}/${p95.toFixed(1)} ms (${viewer.lastDrawn?.toLocaleString() ?? 0} of ${viewer.cloud?.count?.toLocaleString() ?? 0} pts drawn${counts})`;
  const a50 = pct(state.apiMs, 0.5), a95 = pct(state.apiMs, 0.95);
  $("m-api").textContent = a50 == null ? "—" : `${a50.toFixed(0)}/${a95.toFixed(0)} ms`;
  const age = $("m-age");
  if (state.conn !== "ONLINE") { age.className = "error"; age.textContent = `server unreachable · last contact ${state.lastContact ? fmtAge((Date.now() - state.lastContact) / 1000) + " ago" : "never"}`; }
  else if (state.loaded) { age.className = ""; const c = state.loaded.report.created_at; age.textContent = `stored map built ${fmtAge((Date.now() - Date.parse(c)) / 1000)} ago (${c.slice(0, 16).replace("T", " ")} UTC)`; }
  else { age.className = ""; age.textContent = "no data loaded"; }
  tracking.statusTick();
}

// ---------------- workspace mode ----------------
function setMode(mode) {
  const m = ["tracking", "live"].includes(mode) ? mode : "mapping";
  document.body.dataset.mode = m;
  $("ws-map").hidden = m !== "mapping"; $("statusbar").hidden = m !== "mapping";
  $("ws-track").hidden = m !== "tracking"; $("statusbar-track").hidden = m !== "tracking";
  $("ws-live").hidden = m !== "live"; $("statusbar-live").hidden = m !== "live";
  $("badges").hidden = m !== "mapping"; $("session-label").hidden = m !== "mapping";
  $("mode-map").setAttribute("aria-pressed", String(m === "mapping"));
  $("mode-track").setAttribute("aria-pressed", String(m === "tracking"));
  $("mode-live").setAttribute("aria-pressed", String(m === "live"));
  if (m !== "mapping") stopPlay();
  if (m === "tracking") tracking.enter();
  if (m === "live") live.enter();
  const hash = m === "mapping" ? "" : `#${m}`;
  if (location.hash !== hash) history.replaceState(null, "", hash || location.pathname);
  announce(`${m[0].toUpperCase()}${m.slice(1)} workspace`);
}

// ---------------- wiring ----------------
$("build-form").addEventListener("submit", startBuild);
$("input-select").addEventListener("change", () => { describeInput(); renderControls(); });
$("cancel").addEventListener("click", () => { $("cancel-confirm").hidden = false; $("cancel-yes").focus(); });
$("cancel-yes").addEventListener("click", cancelJob);
$("cancel-no").addEventListener("click", () => { $("cancel-confirm").hidden = true; $("cancel").focus(); });
$("refresh").addEventListener("click", () => { refreshSessions(); refreshInputs().catch(() => {}); });
$("prev").addEventListener("click", () => { stopPlay(); setIndex(state.idx - 1); });
$("next").addEventListener("click", () => { stopPlay(); setIndex(state.idx + 1); });
$("play").addEventListener("click", togglePlay);
$("scrub").addEventListener("input", (e) => { stopPlay(); setIndex(parseInt(e.target.value, 10)); });
$("reset-view").addEventListener("click", () => viewer.resetView());
$("layer").addEventListener("change", () => { if (state.loaded) loadSession(state.loaded.sid, state.loaded.modelRank); });
function setOverlays(path, cams) {
  $("show-path").setAttribute("aria-pressed", String(path)); $("show-cams").setAttribute("aria-pressed", String(cams));
  viewer.setOverlays({ path, cameras: cams });
}
$("show-path").addEventListener("click", () => setOverlays($("show-path").getAttribute("aria-pressed") !== "true", $("show-cams").getAttribute("aria-pressed") === "true"));
$("show-cams").addEventListener("click", () => setOverlays($("show-path").getAttribute("aria-pressed") === "true", $("show-cams").getAttribute("aria-pressed") !== "true"));
$("framing").addEventListener("click", () => {
  const full = $("framing").getAttribute("aria-pressed") !== "true";
  $("framing").setAttribute("aria-pressed", String(full)); viewer.setFraming(full ? "full" : "main");
});
$("model-select").addEventListener("change", (e) => { if (state.loaded) loadSession(state.loaded.sid, parseInt(e.target.value, 10)); });
window.addEventListener("resize", () => drawCoverage());
$("growth").addEventListener("click", () => {
  state.growth = !state.growth; $("growth").setAttribute("aria-pressed", String(state.growth));
  viewer.setVisibleUpTo(state.growth ? state.idx : Infinity);
});
$("mode-map").addEventListener("click", () => setMode("mapping"));
$("mode-track").addEventListener("click", () => setMode("tracking"));
$("mode-live").addEventListener("click", () => setMode("live"));
document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, select, textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "m" || e.key === "M") return setMode("mapping");
  if (e.key === "t" || e.key === "T") return setMode("tracking");
  if (e.key === "l" || e.key === "L") return setMode("live");
  if (document.body.dataset.mode === "live") return live.onKey(e);
  if (document.body.dataset.mode === "tracking") return tracking.onKey(e);
  if (e.key === "Escape" && !$("cancel-confirm").hidden) { $("cancel-confirm").hidden = true; return; }
  if (e.target === $("map-canvas")) return;  // canvas handles its own arrow/zoom keys
  if (e.key === " " && !e.target.closest("button")) { e.preventDefault(); togglePlay(); }
  else if (e.key === "ArrowLeft") { stopPlay(); setIndex(state.idx - 1); }
  else if (e.key === "ArrowRight") { stopPlay(); setIndex(state.idx + 1); }
  else if (e.key === "r" || e.key === "R") viewer.resetView();
  else if (e.key === "g" || e.key === "G") $("growth").click();
  else if (e.key === "p" || e.key === "P") $("show-path").click();
  else if (e.key === "c" || e.key === "C") $("show-cams").click();
});

tracking = initTracking({ api, badge, announce, pct, isOnline: () => state.conn === "ONLINE", apiMs: () => state.apiMs,
  mapJobRunning: () => !!state.jobSid || (live?.running() ?? false), onJobChange: () => renderControls() });
live = initLive({ api, badge, isOnline: () => state.conn === "ONLINE",
  otherJobRunning: () => !!state.jobSid || tracking.trackJobRunning(),
  onJobChange: () => { renderControls(); tracking.renderControls(); } });
renderControls();
setMode(location.hash.slice(1) || "mapping");
healthLoop();
refreshSessions();
setInterval(statusTick, 1000);
setInterval(() => { if (state.conn === "ONLINE" && !state.jobSid) refreshSessions(); tracking.refreshRuns(); }, 15000);
