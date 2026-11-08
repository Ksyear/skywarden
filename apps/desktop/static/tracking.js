// Tracking workspace: run the local person detector on a clip, replay the recorded tracks with boxes, choose a
// target, and show the MOCK yaw command the controller would have produced (computed by the server, never sent).
const $ = (id) => document.getElementById(id);
const TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"]);
const TSTATE_TONE = { LOCKED: "ok", PREDICTING: "warn", AMBIGUOUS: "ambiguous", LOST: "error", SEARCHING: "offline" };
const STATE_HINT = {
  SEARCHING: "Target chosen but not confirmed in this frame yet.",
  LOCKED: "Target detected in this frame. The controller turns to keep it centred.",
  PREDICTING: "Target not detected; position is predicted (up to 2 s). Command is STOP until it is seen again.",
  AMBIGUOUS: "Another person is inside the target's gate, so motion alone cannot tell who is who. Command is STOP; the target is not refreshed and re-lock is not guaranteed.",
  LOST: "Target lost. The selector never switches to another person on its own — choose again.",
};

export function initTracking(ctx) {
  const T = {
    inputs: [], models: [], runs: [],
    jobSid: null, jobInfo: null,
    run: null, loadToken: 0,     // run = {sid, report, frames, wh}
    k: 0, playing: false,
    target: null, replay: null, replayToken: 0,
    showRaw: false, hits: [], images: new Map(),
  };
  let colors = null;
  const css = () => {
    const s = getComputedStyle(document.documentElement);
    const v = (n) => s.getPropertyValue(n).trim();
    colors = { accent: v("--accent"), ok: v("--ok"), warn: v("--warn"), error: v("--error"), muted: v("--muted"),
      offline: v("--offline"), text: v("--text"), bg: v("--bg"), border: v("--border-strong"), panel2: v("--panel-2"), sim: v("--sim"),
      ambiguous: v("--ambiguous") };
  };

  // ---------------- lists ----------------
  async function refreshInputs() {
    try {
      [T.inputs, T.models] = await Promise.all([ctx.api("/api/inputs"), ctx.api("/api/models")]);
    } catch { return; }
    const sel = $("t-input"), prev = sel.value; sel.replaceChildren();
    if (!T.inputs.length) { const o = new Option("No inputs found (put a video in assets/input/)", ""); o.disabled = true; sel.add(o); }
    for (const i of T.inputs) sel.add(new Option(`${i.label} · ${i.kind} · ${i.origin}`, i.id));
    const pick = prev && T.inputs.some((i) => i.id === prev) ? prev : T.inputs.find((i) => /walking/.test(i.id))?.id;
    if (pick) sel.value = pick;
    const ms = $("t-model"), pm = ms.value; ms.replaceChildren();
    for (const m of T.models) {
      const o = new Option(`${m.name} · ${m.input_size}px · ${m.license}${m.available ? "" : " · weights missing"}`, m.name);
      o.disabled = !m.available; ms.add(o);
    }
    if (pm) ms.value = pm;
    describeInput(); renderControls();
  }
  function describeInput() {
    const i = T.inputs.find((x) => x.id === $("t-input").value);
    $("t-input-origin").textContent = i ? `${i.id} · ${i.origin}` : "";
  }

  async function refreshRuns() {
    try {
      const [tr, lv] = await Promise.all([ctx.api("/api/tracks"), ctx.api("/api/runs").catch(() => [])]);
      T.runs = [...tr.map((r) => ({ ...r, kind: "tracks" })), ...lv.map((r) => ({ ...r, kind: "runs" }))];
    } catch { return; }
    const ul = $("t-runs"); ul.replaceChildren();
    if (!T.runs.length) { const li = document.createElement("li"); li.className = "muted"; li.textContent = "No recorded runs yet."; ul.append(li); }
    for (const r of T.runs) {
      const li = document.createElement("li"), b = document.createElement("button");
      b.type = "button"; b.setAttribute("aria-current", String(T.run?.sid === r.id));
      const sid = Object.assign(document.createElement("span"), { className: "sid", textContent: r.id });
      const ok = r.state === "SUCCEEDED";
      const st = ctx.badge(ok ? "READY" : r.state, ok ? "real" : r.state === "RUNNING" || r.state === "STARTING" ? "warn" : r.state === "CANCELLED" ? "warn" : "error");
      const meta = Object.assign(document.createElement("span"), { className: "meta" });
      meta.textContent = ok ? `${r.frames} frames · ${r.tracks_confirmed} tracks · ${r.model} · ${r.labels?.data_origin}` : (r.created_at || "").slice(0, 19).replace("T", " ");
      b.append(sid, st, meta);
      b.disabled = !ok; b.title = ok ? "Open this recorded run" : "Only finished runs can be opened";
      if (r.kind === "runs") meta.textContent = `LIVE RUN · ${meta.textContent}`;
      b.addEventListener("click", () => loadRun(r.id, r.kind));
      li.append(b); ul.append(li);
    }
  }

  // ---------------- jobs ----------------
  async function startRun(ev) {
    ev.preventDefault();
    if (!ctx.isOnline() || T.jobSid) return;
    const fps = parseFloat($("t-fps").value), max = parseInt($("t-max").value, 10);
    if (!(fps >= 1 && fps <= 30) || !(max >= 10 && max <= 3000)) { msg("error", "Invalid settings", "Sample rate must be 1–30 fps and max frames 10–3000."); return; }
    $("t-run").disabled = true;
    try {
      const r = await ctx.api("/api/track-jobs", { method: "POST", body: JSON.stringify({ input_id: $("t-input").value, model: $("t-model").value, fps, max_frames: max }) });
      T.jobSid = r.session; T.jobInfo = null; ctx.announce("Tracking run started"); ctx.onJobChange();
      poll(); refreshRuns();
    } catch (e) { msg("error", "Could not start", e.message); }
    renderControls();
  }
  function reattach(sid) { if (sid && !T.jobSid) { T.jobSid = sid; poll(); renderControls(); ctx.onJobChange(); } }

  async function poll() {
    const sid = T.jobSid; if (!sid) return;
    try {
      const info = await ctx.api(`/api/tracks/${encodeURIComponent(sid)}`);
      if (sid !== T.jobSid) return;
      T.jobInfo = info; renderJob();
      if (TERMINAL.has(info.state)) {
        T.jobSid = null; renderControls(); ctx.onJobChange(); refreshRuns();
        if (info.state === "SUCCEEDED") loadRun(sid);
        return;
      }
    } catch {}
    setTimeout(poll, ctx.isOnline() ? 700 : 3000);
  }

  function renderJob() {
    const info = T.jobInfo; if (!info) return;
    const st = info.status || {}, s = info.state, b = $("t-job-badge");
    b.textContent = s === "RUNNING" && info.stale ? "STALE" : s;
    b.dataset.tone = s === "SUCCEEDED" ? "ok" : s === "RUNNING" || s === "STARTING" ? (info.stale ? "warn" : "real") : s === "CANCELLED" ? "warn" : "error";
    const n = (st.stages || []).length || 3;
    const frac = s === "SUCCEEDED" ? 1 : Math.max(0, (st.stage_index ?? 0) + (st.progress ? st.progress[0] / Math.max(1, st.progress[1]) : 0)) / n;
    $("t-job-bar").style.width = `${Math.round(frac * 100)}%`;
    if (s === "RUNNING" || s === "STARTING") msg(info.stale ? "warn" : "", info.stale ? `No update for ${info.status_age_s} s` : "Detecting + tracking", st.message || "starting…");
    else if (s === "SUCCEEDED") msg("ok", "Run finished", `${info.report.frames_processed} frames, ${info.report.tracks.tracks_confirmed} confirmed tracks.`);
    else if (s === "CANCELLED") msg("warn", "Run cancelled", "Nothing was kept for replay. Start a new run when ready.");
    else msg("error", s === "INTERRUPTED" ? "Run interrupted" : "Run failed", `${info.report?.failure?.message || st.message || "The process ended without a result."}`);
  }
  function msg(tone, title, text) {
    const el = $("t-job-msg"); el.dataset.tone = tone; el.replaceChildren();
    el.append(Object.assign(document.createElement("b"), { textContent: title + ". " }), document.createTextNode(text));
  }
  async function cancelRun() {
    $("t-cancel-confirm").hidden = true;
    if (!T.jobSid) return;
    try { await ctx.api(`/api/track-jobs/${encodeURIComponent(T.jobSid)}/cancel`, { method: "POST", body: "{}" }); ctx.announce("Cancel requested"); }
    catch (e) { msg("error", "Cancel failed", e.message); }
  }

  // ---------------- loading a run ----------------
  const baseOf = (sid, kind) => `/api/${kind === "runs" ? "runs" : "tracks"}/${encodeURIComponent(sid)}`;
  async function loadRun(sid, kind = "tracks") {
    const token = ++T.loadToken;
    stopPlay(); clearTarget(false);
    setEmpty("Loading run…", `Reading ${sid} (tracks + report).`, "");
    try {
      const base = baseOf(sid, kind);
      const [info, rec] = await Promise.all([ctx.api(base), ctx.api(`${base}/files/tracks.json`, { timeout: 30000 })]);
      if (token !== T.loadToken) return;
      if (info.state !== "SUCCEEDED") throw new Error(`run is ${info.state}`);
      const expected = kind === "runs" ? info.report.counts?.processed : info.report.frames_processed;
      if (!rec.frames?.length || rec.frames.length !== expected) throw new Error(`tracks.json has ${rec.frames?.length ?? 0} frames, report says ${expected}`);
      T.run = { sid, kind, report: normaliseReport(info.report, rec, kind), frames: rec.frames, wh: rec.image_wh };
      T.k = 0;  // the previous run may have been longer: never index past this run's frames
      T.images.clear();
      $("t-empty").hidden = true;
      renderLabels(); renderMetrics(); refreshRuns(); renderControls();
      await setFrame(0, true);
      drawTimeline();
      ctx.announce(`Loaded tracking run ${sid}: ${rec.frames.length} frames`);
    } catch (e) {
      if (token !== T.loadToken) return;
      T.run = null; renderLabels(); renderMetrics(); renderControls(); draw(); drawTimeline();
      setEmpty("Could not load run", e.message, "error");
    }
  }
  function setEmpty(title, text, tone) {
    const el = $("t-empty"); el.hidden = false; el.dataset.tone = tone;
    $("t-empty-title").textContent = title; $("t-empty-text").textContent = text;
  }
  function renderLabels() {
    const el = $("t-badges"), ov = $("t-overlay"); el.replaceChildren(); ov.replaceChildren();
    const R = T.run?.report; if (!R) return;
    const d = R.detector;
    el.append(
      ctx.badge(R.labels.detector_source, "real", `${d.name} · ${d.license} · ${d.runtime} · sha256 ${d.sha256.slice(0, 12)}…`),
      ctx.badge(d.name, "real"),
      ctx.badge(R.labels.data_origin, R.labels.data_origin === "SYNTHETIC" ? "sim" : "real"),
      ctx.badge(T.run.kind === "runs" ? "LIVE RUN · OPEN_LOOP_REPLAY" : "RECORDED CLIP", "warn",
        T.run.kind === "runs" ? "Replay of a recorded live run; commands are recomputed open-loop (the video did not react)" : "Replay of a recorded run, not a live camera"),
    );
    const chip = (t, cls = "") => Object.assign(document.createElement("span"), { className: `chip ${cls}`, textContent: t });
    ov.append(chip(`Replay of recorded clip · ${d.name} on CPU · no identity re-ID`));
  }
  function renderMetrics() {
    const R = T.run?.report, tm = R?.timing;
    const f = (a, b, u = " ms") => `${a}/${b}${u}`;
    $("tm-frames").textContent = R ? `${R.frames_processed} (${R.clip_duration_s.toFixed(1)} s clip)` : "—";
    $("tm-det").textContent = tm ? f(tm.detect_ms_p50, tm.detect_ms_p95) : "—";
    $("tm-trk").textContent = tm ? f(tm.track_ms_p50, tm.track_ms_p95) : "—";
    $("tm-fps").textContent = tm ? `${tm.processing_fps} fps (detect+track)` : "—";
    $("tm-tracks").textContent = R ? `${R.tracks.tracks_confirmed} of ${R.tracks.track_ids_created} ids` : "—";
    $("tm-acc").textContent = R ? (R.accuracy.MOTA == null ? "undefined (no ground truth)" : `${R.accuracy.MOTA}/${R.accuracy.IDF1}`) : "—";
  }

  // A live run's report has a different shape; map it onto the fields the panel shows (nothing invented).
  function normaliseReport(r, rec, kind) {
    if (kind !== "runs") return r;
    const lt = r.latency_ms || {}, dt = r.detect_ms || {};
    return { ...r, frames_processed: rec.frames.length, clip_duration_s: r.media_seconds ?? 0,
      labels: { ...(r.labels || {}), detector_source: r.labels?.detector_source || "REAL_MODEL", data_origin: r.labels?.data_origin || "LIVE_RUN" },
      detector: { name: r.detector?.name || "?", license: r.detector?.license || "", runtime: r.detector?.runtime || "", sha256: r.detector?.sha256 || "" },
      timing: { detect_ms_p50: dt.p50, detect_ms_p95: dt.p95, track_ms_p50: "—", track_ms_p95: "—", processing_fps: r.processed_fps },
      tracks: { tracks_confirmed: "—", track_ids_created: "—" },
      accuracy: { MOTA: null }, live: { latency_ms: lt, dropped: r.counts?.dropped } };
  }

  // ---------------- frames ----------------
  function frameImage(k) {
    const f = T.run.frames[k], url = `${baseOf(T.run.sid, T.run.kind)}/files/${f.image}`;
    if (T.images.has(url)) return T.images.get(url);
    const p = new Promise((resolve) => { const im = new Image(); im.onload = () => resolve(im); im.onerror = () => resolve(null); im.src = url; });
    T.images.set(url, p);
    if (T.images.size > 40) T.images.delete(T.images.keys().next().value);
    return p;
  }
  let frameTok = 0, curImg = null;
  async function setFrame(k, force = false) {
    if (!T.run) return;
    k = Math.max(0, Math.min(T.run.frames.length - 1, k));
    if (k === T.k && !force) return;
    T.k = k;
    $("t-scrub").max = T.run.frames.length - 1; $("t-scrub").value = k;
    const f = T.run.frames[k];
    $("t-count").textContent = `${k + 1} / ${T.run.frames.length} · t=${f.t.toFixed(2)} s`;
    renderTrackList(); renderReadouts(); drawTimeline();
    const tok = ++frameTok, im = await frameImage(k);
    if (k + 1 < T.run.frames.length) frameImage(k + 1);  // prefetch
    if (tok !== frameTok) return;
    curImg = im; draw();
  }

  function selectable(tr) { return tr.state === "LOCKED"; }
  function frameTracks() { return T.run ? [...T.run.frames[T.k].tracks].sort((a, b) => a.id - b.id) : []; }

  function renderTrackList() {
    const ul = $("t-tracks"); ul.replaceChildren();
    const trs = frameTracks();
    $("t-tcount").textContent = T.run ? `${trs.length} track${trs.length === 1 ? "" : "s"} · ${T.run.frames[T.k].detections.length} detections` : "—";
    if (!T.run) { ul.append(Object.assign(document.createElement("li"), { className: "muted", textContent: "Load a run to see tracks." })); return; }
    if (!trs.length) { ul.append(Object.assign(document.createElement("li"), { className: "muted", textContent: "No people tracked in this frame." })); return; }
    trs.forEach((tr, i) => {
      const li = document.createElement("li"), b = document.createElement("button");
      b.type = "button"; b.setAttribute("aria-pressed", String(T.target?.id === tr.id));
      const key = Object.assign(document.createElement("span"), { className: "key", textContent: i < 9 ? String(i + 1) : "" });
      const id = Object.assign(document.createElement("span"), { className: "tid", textContent: `#${tr.id}` });
      const st = ctx.badge(tr.state, { LOCKED: "ok", PREDICTING: "warn", AMBIGUOUS: "ambiguous" }[tr.state] || "offline");
      const meta = Object.assign(document.createElement("span"), { className: "meta", textContent: `σ ${tr.sigma_px.toFixed(0)} px · ${tr.hits} hits` });
      b.append(key, id, st, meta);
      b.disabled = !selectable(tr) || !ctx.isOnline();
      b.title = selectable(tr) ? `Choose #${tr.id} as target from this frame${i < 9 ? ` (key ${i + 1})` : ""}` : "Only confirmed (LOCKED) tracks can be chosen";
      b.addEventListener("click", () => selectTarget(tr.id));
      li.append(b); ul.append(li);
    });
  }

  // ---------------- drawing ----------------
  function fitCanvas(c) {
    const r = c.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    return dpr;
  }
  function draw() {
    const c = $("t-canvas"), g = c.getContext("2d"), dpr = fitCanvas(c);
    g.clearRect(0, 0, c.width, c.height); T.hits = [];
    if (!T.run || !curImg) return;
    if (!colors) css();
    const [W, H] = T.run.wh, s = Math.min(c.width / W, c.height / H), ox = (c.width - W * s) / 2, oy = (c.height - H * s) / 2;
    g.drawImage(curImg, ox, oy, W * s, H * s);
    const f = T.run.frames[T.k];
    const box = (b) => [ox + b[0] * s, oy + b[1] * s, (b[2] - b[0]) * s, (b[3] - b[1]) * s];
    if (T.showRaw) {
      g.strokeStyle = colors.sim; g.lineWidth = 1 * dpr; g.setLineDash([]);
      for (const d of f.detections) { const [x, y, w, h] = box(d); g.strokeRect(x, y, w, h); }
    }
    g.font = `600 ${12 * dpr}px ui-monospace, Menlo, monospace`; g.textBaseline = "bottom";
    for (const tr of f.tracks) {
      const [x, y, w, h] = box(tr.bbox), isT = T.target?.id === tr.id;
      const col = tr.state === "AMBIGUOUS" ? colors.ambiguous : isT ? colors.accent : tr.state === "PREDICTING" ? colors.warn : tr.state === "LOCKED" ? colors.text : colors.offline;
      g.strokeStyle = col; g.lineWidth = (isT ? 3 : 1.5) * dpr;
      g.setLineDash(tr.state === "PREDICTING" || tr.state === "AMBIGUOUS" ? [6 * dpr, 4 * dpr] : tr.state === "TENTATIVE" ? [2 * dpr, 3 * dpr] : []);
      g.strokeRect(x, y, w, h);
      g.setLineDash([]);
      const label = `${isT ? "TARGET " : ""}#${tr.id} ${tr.state}`, tw = g.measureText(label).width + 8 * dpr, th = 16 * dpr;
      const ly = y > th ? y : y + th;  // above the box, or inside its top edge when the box touches the frame top
      g.fillStyle = colors.bg; g.globalAlpha = 0.8; g.fillRect(x, ly - th, tw, th); g.globalAlpha = 1;
      g.fillStyle = col; g.fillText(label, x + 4 * dpr, ly - 2 * dpr);
      if (selectable(tr)) T.hits.push({ id: tr.id, x: x / dpr, y: y / dpr, w: w / dpr, h: h / dpr });
    }
    // image centre line: what the yaw controller aims for
    g.strokeStyle = colors.muted; g.globalAlpha = 0.35; g.lineWidth = 1 * dpr; g.setLineDash([4 * dpr, 6 * dpr]);
    g.beginPath(); g.moveTo(ox + W * s / 2, oy); g.lineTo(ox + W * s / 2, oy + H * s); g.stroke(); g.setLineDash([]); g.globalAlpha = 1;
  }

  function drawTimeline() {
    const c = $("t-timeline"), g = c.getContext("2d"), dpr = fitCanvas(c);
    g.clearRect(0, 0, c.width, c.height);
    if (!T.run) return;
    if (!colors) css();
    const n = T.run.frames.length, W = c.width, H = c.height, bw = W / n, band = H * 0.45;
    const rows = T.replay?.frames;
    for (let k = 0; k < n; k++) {
      let col;
      if (rows) col = { LOCKED: colors.ok, PREDICTING: colors.warn, AMBIGUOUS: colors.ambiguous, LOST: colors.error, SEARCHING: colors.border }[rows[k].target_state];
      else { const locked = T.run.frames[k].tracks.filter((t) => t.state === "LOCKED").length; col = locked ? colors.muted : colors.border; g.globalAlpha = locked ? Math.min(1, 0.3 + 0.2 * locked) : 1; }
      g.fillStyle = col; g.fillRect(k * bw, 0, Math.ceil(bw), band); g.globalAlpha = 1;
    }
    // yaw command trace in the lower band, zero in the middle, ±max_yaw at the edges
    const mid = band + (H - band) / 2, amp = (H - band) / 2 - 3 * dpr;
    g.strokeStyle = colors.border; g.lineWidth = 1; g.beginPath(); g.moveTo(0, mid); g.lineTo(W, mid); g.stroke();
    if (rows) {
      const lim = T.replay.limits.max_yaw_rc;
      g.strokeStyle = colors.accent; g.lineWidth = 1.5 * dpr; g.beginPath();
      rows.forEach((r, k) => { const x = (k + 0.5) * bw, y = mid - (r.yaw / lim) * amp; k ? g.lineTo(x, y) : g.moveTo(x, y); });
      g.stroke();
      const sx = (T.replay.select_k + 0.5) * bw;
      g.fillStyle = colors.accent; g.beginPath(); g.moveTo(sx - 5 * dpr, 0); g.lineTo(sx + 5 * dpr, 0); g.lineTo(sx, 7 * dpr); g.fill();
    }
    g.fillStyle = colors.text; g.fillRect((T.k + 0.5) * bw - dpr, 0, 2 * dpr, H);
  }

  // ---------------- target + virtual command ----------------
  async function selectTarget(id) {
    if (!T.run || !ctx.isOnline()) return;
    const tr = T.run.frames[T.k].tracks.find((t) => t.id === id);
    if (!tr || !selectable(tr)) return;
    const tok = ++T.replayToken;
    T.target = { id, select_k: T.k }; T.replay = null;
    renderTrackList(); renderReadouts(); draw();
    try {
      const r = await ctx.api(`${baseOf(T.run.sid, T.run.kind)}/${T.run.kind === "runs" ? "replay" : "control"}`, { method: "POST", body: JSON.stringify({ target_id: id, select_k: T.k }) });
      if (tok !== T.replayToken) return;
      T.replay = r; ctx.announce(`Target #${id} chosen at frame ${T.k + 1}`);
    } catch (e) {
      if (tok !== T.replayToken) return;
      T.target = null; $("r-stop").textContent = `Could not compute the command replay: ${e.message}`; $("r-stop").dataset.tone = "warn";
    }
    renderTrackList(); renderReadouts(); draw(); drawTimeline();
  }
  function clearTarget(redraw = true) {
    T.replayToken++; T.target = null; T.replay = null;
    if (redraw) { renderTrackList(); renderReadouts(); draw(); drawTimeline(); ctx.announce("Target cleared"); }
  }

  function renderReadouts() {
    const st = $("r-tstate"), yaw = $("r-yaw"), stop = $("r-stop"), fill = $("r-yawfill");
    $("r-clear").hidden = !T.target;
    stop.dataset.tone = "";
    const none = (text) => { st.textContent = "NO TARGET"; st.dataset.tone = "offline"; $("r-tid").textContent = ""; yaw.textContent = "—"; fill.style.left = "50%"; fill.style.width = "0"; stop.textContent = text; };
    if (!T.run) return none("Load a run to compute commands.");
    if (!T.target) { $("r-thint").textContent = "Click a box or press 1–9 to choose who to keep centred. The choice never switches to another person on its own."; return none("No target selected · command STOP (yaw 0)."); }
    $("r-tid").textContent = `#${T.target.id}`;
    if (!T.replay) { st.textContent = "COMPUTING"; st.dataset.tone = "warn"; yaw.textContent = "…"; stop.textContent = "Asking the server to replay the controller…"; return; }
    const row = T.replay.frames[T.k], lim = T.replay.limits.max_yaw_rc;
    st.textContent = row.target_state; st.dataset.tone = TSTATE_TONE[row.target_state];
    $("r-thint").textContent = T.k < T.replay.select_k ? `Chosen at frame ${T.replay.select_k + 1}; earlier frames had no target.` : STATE_HINT[row.target_state];
    yaw.textContent = `yaw ${row.yaw > 0 ? "+" : ""}${row.yaw} / ±${lim}${row.yaw > 0 ? " · turn right" : row.yaw < 0 ? " · turn left" : ""}`;
    const frac = row.yaw / lim / 2;
    fill.style.left = `${50 + Math.min(0, frac) * 100}%`; fill.style.width = `${Math.abs(frac) * 100}%`;
    if (row.stop.length) { stop.textContent = `STOP · ${row.stop.join(", ")}`; stop.dataset.tone = "warn"; }
    else stop.textContent = `Centring target · offset ${row.error_norm > 0 ? "+" : ""}${row.error_norm} (−1 left edge … +1 right edge)`;
  }

  // ---------------- playback ----------------
  async function playLoop() {
    while (T.playing && T.run) {
      if (T.k >= T.run.frames.length - 1) { stopPlay(); break; }
      const t0 = performance.now(), dt = (T.run.frames[T.k + 1].t - T.run.frames[T.k].t) * 1000;
      await setFrame(T.k + 1);
      await new Promise((r) => setTimeout(r, Math.max(0, Math.min(500, dt) - (performance.now() - t0))));
    }
  }
  function togglePlay() {
    if (!T.run) return;
    T.playing = !T.playing;
    if (T.playing && T.k >= T.run.frames.length - 1) setFrame(0);
    const b = $("t-play"); b.setAttribute("aria-pressed", String(T.playing)); b.textContent = T.playing ? "❚❚" : "▶︎";
    b.setAttribute("aria-label", T.playing ? "Pause (Space)" : "Play (Space)");
    if (T.playing) playLoop();
  }
  function stopPlay() { if (T.playing) togglePlay(); }

  // ---------------- controls ----------------
  function renderControls() {
    const online = ctx.isOnline(), has = !!T.run, running = !!T.jobSid;
    $("t-run").disabled = !online || running || ctx.mapJobRunning() || !$("t-input").value || !$("t-model").value;
    $("t-run").title = ctx.mapJobRunning() ? "A map build is running; one job at a time" : "";
    $("t-cancel").disabled = !online || !running;
    if (!running) $("t-cancel-confirm").hidden = true;
    for (const id of ["t-prev", "t-play", "t-next", "t-scrub"]) $(id).disabled = !has;
    for (const id of ["t-input", "t-model", "t-fps", "t-max"]) $(id).disabled = running;
    $("r-clear").disabled = !online;
    if (has) renderTrackList();
  }

  // ---------------- wiring ----------------
  $("t-form").addEventListener("submit", startRun);
  $("t-input").addEventListener("change", () => { describeInput(); renderControls(); });
  $("t-cancel").addEventListener("click", () => { $("t-cancel-confirm").hidden = false; $("t-cancel-yes").focus(); });
  $("t-cancel-yes").addEventListener("click", cancelRun);
  $("t-cancel-no").addEventListener("click", () => { $("t-cancel-confirm").hidden = true; $("t-cancel").focus(); });
  $("t-refresh").addEventListener("click", () => { refreshRuns(); refreshInputs(); });
  $("t-prev").addEventListener("click", () => { stopPlay(); setFrame(T.k - 1); });
  $("t-next").addEventListener("click", () => { stopPlay(); setFrame(T.k + 1); });
  $("t-play").addEventListener("click", togglePlay);
  $("t-scrub").addEventListener("input", (e) => { stopPlay(); setFrame(parseInt(e.target.value, 10)); });
  $("t-raw").addEventListener("change", (e) => { T.showRaw = e.target.checked; draw(); });
  $("r-clear").addEventListener("click", () => clearTarget());
  $("t-canvas").addEventListener("click", (e) => {
    const r = e.currentTarget.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    const hit = T.hits.filter((h) => x >= h.x && x <= h.x + h.w && y >= h.y && y <= h.y + h.h).sort((a, b) => a.w * a.h - b.w * b.h)[0];
    if (hit) selectTarget(hit.id);
  });
  $("t-timeline").addEventListener("click", (e) => {
    if (!T.run) return;
    const r = e.currentTarget.getBoundingClientRect();
    stopPlay(); setFrame(Math.floor((e.clientX - r.left) / r.width * T.run.frames.length));
  });
  new ResizeObserver(() => { draw(); drawTimeline(); }).observe($("t-stage"));

  function onKey(e) {
    if (e.target.closest("input, select, textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "Escape" && !$("t-cancel-confirm").hidden) { $("t-cancel-confirm").hidden = true; return; }
    if (e.key === " " && !e.target.closest("button")) { e.preventDefault(); togglePlay(); }
    else if (e.key === "ArrowLeft") { stopPlay(); setFrame(T.k - 1); }
    else if (e.key === "ArrowRight") { stopPlay(); setFrame(T.k + 1); }
    else if (e.key === "0") clearTarget();
    else if (/^[1-9]$/.test(e.key)) { const tr = frameTracks()[parseInt(e.key, 10) - 1]; if (tr) selectTarget(tr.id); }
  }

  let started = false;
  return {
    onKey, renderControls, reattach,
    enter() { if (!started) { started = true; refreshInputs(); refreshRuns(); } requestAnimationFrame(() => { draw(); drawTimeline(); }); },
    onHealth(h) {
      document.querySelectorAll(".armed-mirror").forEach((el) => { el.textContent = String(h.hardware_armed); if (h.hardware_armed !== false) el.style.color = "var(--error)"; });
      document.querySelectorAll(".dryrun-mirror").forEach((el) => { el.textContent = String(h.dry_run); });
      reattach(h.running_track_job);
      if (started && !T.inputs.length) refreshInputs();
    },
    statusTick() {
      const a50 = ctx.pct(ctx.apiMs(), 0.5), a95 = ctx.pct(ctx.apiMs(), 0.95);
      $("tm-api").textContent = a50 == null ? "—" : `${a50.toFixed(0)}/${a95.toFixed(0)} ms`;
    },
    refreshRuns: () => { if (started && ctx.isOnline() && !T.jobSid) refreshRuns(); },
    trackJobRunning: () => !!T.jobSid,
  };
}
