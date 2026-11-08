import { PointViewer, parsePly } from "./viewer.js";
import { initTracking } from "./tracking.js";

const $ = (id) => document.getElementById(id);
const STAGES = { SELECT_FRAMES: "Frames", EXTRACT_FEATURES: "Features", MATCH_FEATURES: "Matching", MAP: "Mapping", EXPORT: "Export" };
const TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"]);
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

let tracking = null;  // tracking workspace (tracking.js), created once the helpers below exist
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
}

async function healthLoop() {
  try {
    const h = await api("/api/health", { timeout: 3000 });
    $("armed").textContent = String(h.hardware_armed); $("dryrun").textContent = String(h.dry_run);
    if (h.hardware_armed !== false) { $("armed").style.color = "var(--error)"; }
    if (h.running_job && !state.jobSid) { state.jobSid = h.running_job; pollJob(); }  // reattach after reload/reconnect
    tracking.onHealth(h);
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
    const tone = s.state === "SUCCEEDED" ? (s.labels?.map_source === "SIMULATED" ? "sim" : "ok") : s.state === "RUNNING" || s.state === "STARTING" ? "warn" : "error";
    const st = badge(s.state === "SUCCEEDED" ? (s.labels?.map_source === "SIMULATED" ? "SIMULATED" : "READY") : s.state, tone);
    const meta = document.createElement("span"); meta.className = "meta";
    meta.textContent = s.state === "SUCCEEDED" ? `${s.registered} frames · ${s.points3D.toLocaleString()} pts · ${s.labels?.data_origin}` : (s.created_at || "").slice(0, 19).replace("T", " ");
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
  else if (s === "SUCCEEDED") showJobMsg("ok", "Map built", `${info.report.reconstruction.registered_images}/${info.report.reconstruction.selected_frames} frames registered. Loaded as ${info.report.labels.map_source}.`);
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
async function loadSession(sid) {
  const token = ++state.loadToken;
  stopPlay();
  setEmpty("Loading map…", `Reading ${sid} (PLY + trajectory).`, "");
  try {
    const base = `/api/sessions/${encodeURIComponent(sid)}`;
    const [info, traj, plyRes] = await Promise.all([api(base), api(`${base}/files/trajectory.json`), api(`${base}/files/sparse.ply`, { raw: true, timeout: 30000 })]);
    const cloud = parsePly(await plyRes.arrayBuffer());
    if (token !== state.loadToken) return;  // a newer load started meanwhile
    const report = info.report;
    if (cloud.count !== report.reconstruction.points3D) throw new Error(`PLY has ${cloud.count} points, report says ${report.reconstruction.points3D}`);
    const poses = traj.poses.map((p) => ({ ...p, R: p.T_world_camera.slice(0, 3).map((r) => r.slice(0, 3)), C: p.T_world_camera.slice(0, 3).map((r) => r[3]) }));
    state.loaded = { sid, report, poses, evaluation: info.evaluation, loadedAt: Date.now() };
    viewer.setData(cloud, poses);
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
  el.append(
    badge(L.map_source, sim ? "sim" : "real", sim ? "Generated from a synthetic render with known ground truth" : "Loaded from a finished reconstruction on disk"),
    badge(`${L.data_origin}`, sim ? "sim" : "real"),
    badge(`SCALE ${L.scale}`, "warn", "Monocular reconstruction: distances have no metric unit"),
    badge(L.nav_safety, "error", "Never use this map for obstacle avoidance or flight decisions"),
    badge(`CALIB ${state.loaded.report.calibration?.status ?? "?"}`, state.loaded.report.calibration?.status === "PROVIDED" ? "ok" : "warn"),
  );
  const chip = (t, cls = "") => Object.assign(document.createElement("span"), { className: `chip ${cls}`, textContent: t });
  tl.append(chip(sim ? "SIMULATED data — not a measurement" : "Replay of stored reconstruction — not live mapping", sim ? "" : ""));
  bl.append(chip("Relative units · scale unknown", "warn"), chip(`${L.geometry} · ${state.loaded.poses.length} cameras`));
}

function renderMetrics() {
  const r = state.loaded?.report.reconstruction, ev = state.loaded?.evaluation;
  $("m-reg").textContent = r ? `${r.registered_images}/${r.selected_frames} (${Math.round(r.registered_ratio * 100)}%)` : "—";
  $("m-pts").textContent = r ? r.points3D.toLocaleString() : "—";
  $("m-err").textContent = r ? `${r.mean_reprojection_error_px.toFixed(2)} px` : "—";
  $("m-ate").textContent = ev ? `${(ev.ate_rmse_m * 100).toFixed(1)} cm RMSE (Sim3-aligned to ground truth, ${ev.matched_poses} poses)` : "not evaluated (no ground truth)";
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
  const trackRunning = tracking?.trackJobRunning() ?? false;
  $("build").disabled = !online || running || trackRunning || !$("input-select").value;
  $("build").title = trackRunning ? "A tracking run is in progress; one job at a time" : "";
  $("cancel").disabled = !online || !running;
  if (!running) $("cancel-confirm").hidden = true;
  for (const id of ["prev", "play", "next", "scrub", "growth", "reset-view"]) $(id).disabled = !has;
  $("input-select").disabled = running; $("fps").disabled = running; $("max-frames").disabled = running; $("use-calib").disabled = running;
}

function statusTick() {
  const p50 = pct(state.renderMs, 0.5), p95 = pct(state.renderMs, 0.95);
  $("m-render").textContent = p50 == null ? "—" : `${p50.toFixed(1)}/${p95.toFixed(1)} ms (${viewer.lastDrawn?.toLocaleString() ?? 0} pts)`;
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
  const track = mode === "tracking";
  document.body.dataset.mode = track ? "tracking" : "mapping";
  $("ws-map").hidden = track; $("statusbar").hidden = track;
  $("ws-track").hidden = !track; $("statusbar-track").hidden = !track;
  $("badges").hidden = track; $("session-label").hidden = track;
  $("mode-map").setAttribute("aria-pressed", String(!track)); $("mode-track").setAttribute("aria-pressed", String(track));
  if (track) { stopPlay(); tracking.enter(); }
  if (location.hash !== (track ? "#tracking" : "")) history.replaceState(null, "", track ? "#tracking" : location.pathname);
  announce(track ? "Tracking workspace" : "Mapping workspace");
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
$("growth").addEventListener("click", () => {
  state.growth = !state.growth; $("growth").setAttribute("aria-pressed", String(state.growth));
  viewer.setVisibleUpTo(state.growth ? state.idx : Infinity);
});
$("mode-map").addEventListener("click", () => setMode("mapping"));
$("mode-track").addEventListener("click", () => setMode("tracking"));
document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, select, textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "m" || e.key === "M") return setMode("mapping");
  if (e.key === "t" || e.key === "T") return setMode("tracking");
  if (document.body.dataset.mode === "tracking") return tracking.onKey(e);
  if (e.key === "Escape" && !$("cancel-confirm").hidden) { $("cancel-confirm").hidden = true; return; }
  if (e.target === $("map-canvas")) return;  // canvas handles its own arrow/zoom keys
  if (e.key === " " && !e.target.closest("button")) { e.preventDefault(); togglePlay(); }
  else if (e.key === "ArrowLeft") { stopPlay(); setIndex(state.idx - 1); }
  else if (e.key === "ArrowRight") { stopPlay(); setIndex(state.idx + 1); }
  else if (e.key === "r" || e.key === "R") viewer.resetView();
  else if (e.key === "g" || e.key === "G") $("growth").click();
});

tracking = initTracking({ api, badge, announce, pct, isOnline: () => state.conn === "ONLINE", apiMs: () => state.apiMs,
  mapJobRunning: () => !!state.jobSid, onJobChange: () => renderControls() });
renderControls();
setMode(location.hash === "#tracking" ? "tracking" : "mapping");
healthLoop();
refreshSessions();
setInterval(statusTick, 1000);
setInterval(() => { if (state.conn === "ONLINE" && !state.jobSid) refreshSessions(); tracking.refreshRuns(); }, 15000);
