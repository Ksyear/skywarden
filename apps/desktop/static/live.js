// Live workspace: start a live run (video at its original time base, or a webcam), watch the latest processed frame
// with boxes, target state, MOCK yaw and STOP reasons, VO state/path, and control it (pause, resume, seek, stop,
// select target). The worker owns all processing; this page only polls /api/runs/<id>/latest and sends commands.
const $ = (id) => document.getElementById(id);
const STALE_AFTER_MS = 500;
const TONE = { LOCKED: "ok", PREDICTING: "warn", AMBIGUOUS: "ambiguous", LOST: "error", SEARCHING: "offline" };

export function initLive(ctx) {
  const L = { sid: null, state: null, row: null, wh: null, lastRowT: null, lastRowSeen: 0, image: null, hits: [],
    polling: false, inputs: [], duration: null, events: [] };

  async function refreshInputs() {
    try { L.inputs = await ctx.api("/api/inputs"); } catch { return; }
    const sel = $("l-input"), prev = sel.value; sel.replaceChildren();
    for (const i of L.inputs.filter((i) => i.kind === "video")) sel.add(new Option(`${i.label} · ${i.origin}`, i.id));
    sel.add(new Option("webcam:0 (local camera, if present)", "webcam:0"));
    if (prev) sel.value = prev; else { const u = L.inputs.find((i) => /utrecht/.test(i.id)); if (u) sel.value = u.id; }
    render();
  }

  function msg(tone, title, text) {
    const el = $("l-msg"); el.dataset.tone = tone; el.replaceChildren();
    el.append(Object.assign(document.createElement("b"), { textContent: title + ". " }), document.createTextNode(text));
  }

  async function start(ev) {
    ev.preventDefault();
    if (!ctx.isOnline() || L.polling) return;
    $("l-start").disabled = true;
    try {
      const r = await ctx.api("/api/run-jobs", { method: "POST", body: JSON.stringify({ input_id: $("l-input").value, rate: $("l-rate").value, max_seconds: parseFloat($("l-max").value) || undefined }) });
      L.sid = r.session; L.row = null; L.events = []; L.lastRowT = null; L.duration = null; showCalib(null);
      msg("", "Starting", `${L.sid}: loading the detector and opening the input…`);
      ctx.onJobChange(); poll();
    } catch (e) { msg("error", "Could not start", e.message); }
    render();
  }

  async function command(op, args = {}) {
    if (!L.sid) return;
    try { await ctx.api(`/api/runs/${encodeURIComponent(L.sid)}/control`, { method: "POST", body: JSON.stringify({ op, args }) }); }
    catch (e) { msg("error", `Command ${op} failed`, e.message); }
  }

  async function poll() {
    if (L.polling) return;
    L.polling = true;
    while (L.sid) {
      try {
        const r = await ctx.api(`/api/runs/${encodeURIComponent(L.sid)}/latest`, { timeout: 3000 });
        L.state = r.state; L.wh = r.image_wh || L.wh;
        const st = r.state.status || {};
        if (st.progress && st.progress[1]) L.duration = st.progress[1];
        if (r.row && r.row.row !== L.row?.row) {
          if (!L.row) msg("", "Running", `${L.sid}: frames are processed at the media time base; older frames are dropped and counted when processing falls behind.`);
          L.row = r.row; L.lastRowSeen = performance.now();
          for (const e of r.row.events || []) L.events.unshift({ t: r.row.run_t, ...e });
          await loadImage();
        }
        if (st.last_event && !L.events.some((e) => e.type === st.last_event.type && e.run_t === st.last_event.run_t)) L.events.unshift({ t: st.last_event.run_t, ...st.last_event });
        L.events = L.events.slice(0, 12);
        render();
        if (["SUCCEEDED", "FAILED", "CANCELLED", "INCOMPLETE", "INTERRUPTED"].includes(r.state.state)) { finish(r.state); break; }
      } catch {}
      if (Math.floor(performance.now() / 1000) % 2 === 0) loadVo();
      await new Promise((res) => setTimeout(res, ctx.isOnline() ? 150 : 1500));
    }
    L.polling = false;
  }

  // VO focal length: without a calibration file it is estimated (fx = 0.9 × width), and the VO path is not reliable.
  // The run report says which; a running run does not expose it yet, so the badge appears when the run ends.
  function showCalib(intr) {
    const b = $("l-vocalib"), est = intr?.source === "ESTIMATED";
    b.hidden = !est; b.title = est ? `UNCALIBRATED — VO path not reliable: ${intr.note || "focal length estimated"}` : "";
  }

  function finish(state) {
    const s = state.state, rep = state.report || {}, ver = state.status?.verify || {};
    showCalib(rep.intrinsics);
    if (s === "SUCCEEDED") msg("ok", "Run finished", `${rep.counts?.processed ?? "?"} frames processed, ${rep.counts?.dropped ?? 0} dropped (${rep.end_reason}). Files verified (rows, sha256, frames). Open it in Tracking → Recorded runs for an OPEN_LOOP replay.`);
    else if (s === "INCOMPLETE") msg("error", "Run incomplete", `Saved files do not check out: ${(ver.problems || []).slice(0, 2).join("; ")}. Not shown as finished.`);
    else if (s === "CANCELLED") msg("warn", "Run cancelled", "Stopped by the operator before the end.");
    else msg("error", `Run ${s.toLowerCase()}`, rep.failure?.message || state.status?.message || "The worker ended without a result.");
    L.sid = null; ctx.onJobChange(); render(); loadVo();
  }

  async function loadImage() {
    if (!L.sid) return;
    const url = `/api/runs/${encodeURIComponent(L.sid)}/files/frames/latest.jpg?r=${L.row.row}`;
    await new Promise((res) => { const im = new Image(); im.onload = () => { L.image = im; draw(); res(); }; im.onerror = res; im.src = url; });
  }

  async function loadVo() {
    const sid = L.sid || L.lastSid; if (!sid) return;
    try { L.vo = await ctx.api(`/api/runs/${encodeURIComponent(sid)}/vo`); drawVo(); } catch {}
  }

  function stale() {
    const live = L.state?.status?.input_state === "LIVE" && L.state?.state === "RUNNING";
    return live && L.lastRowSeen && performance.now() - L.lastRowSeen > STALE_AFTER_MS;
  }

  function draw() {
    const c = $("l-canvas"), g = c.getContext("2d"), r = c.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    c.width = Math.max(1, Math.round(r.width * dpr)); c.height = Math.max(1, Math.round(r.height * dpr));
    g.clearRect(0, 0, c.width, c.height); L.hits = [];
    if (!L.image || !L.wh) return;
    const [W, H] = L.wh, s = Math.min(c.width / W, c.height / H), ox = (c.width - W * s) / 2, oy = (c.height - H * s) / 2;
    g.drawImage(L.image, ox, oy, W * s, H * s);
    g.save(); g.beginPath(); g.rect(ox, oy, W * s, H * s); g.clip();  // boxes and labels stay inside the image
    const css = getComputedStyle(document.documentElement), v = (k) => css.getPropertyValue(k).trim();
    g.font = `600 ${12 * dpr}px ui-monospace, Menlo, monospace`; g.textBaseline = "bottom";
    const placed = [];  // label boxes already drawn: a label that would overlap one goes under its box instead
    const hitsLabel = (r) => placed.some((q) => r.x < q.x + q.w && q.x < r.x + r.w && r.y < q.y + q.h && q.y < r.y + r.h);
    for (const tr of L.row?.tracks || []) {
      const [x1, y1, x2, y2] = tr.bbox, x = ox + x1 * s, y = oy + y1 * s, w = (x2 - x1) * s, h = (y2 - y1) * s;
      const isT = L.row.target_id === tr.id;
      const col = tr.state === "AMBIGUOUS" ? v("--ambiguous") : isT ? v("--accent") : tr.state === "PREDICTING" ? v("--warn") : tr.state === "LOCKED" ? v("--text") : v("--offline");
      g.strokeStyle = col; g.lineWidth = (isT ? 3 : 1.5) * dpr; g.setLineDash(tr.state === "LOCKED" ? [] : [6 * dpr, 4 * dpr]);
      g.strokeRect(x, y, w, h); g.setLineDash([]);
      const label = `${isT ? "TARGET " : ""}#${tr.id} ${tr.state}`, lw = g.measureText(label).width + 6 * dpr, lh = 15 * dpr;
      let ly = y > 16 * dpr ? y - 2 * dpr : y + 16 * dpr;
      if (hitsLabel({ x, y: ly - lh, w: lw, h: lh })) ly = y + h + lh;
      if (!hitsLabel({ x, y: ly - lh, w: lw, h: lh })) { placed.push({ x, y: ly - lh, w: lw, h: lh }); g.fillStyle = col; g.fillText(label, x + 3 * dpr, ly); }
      if (tr.state === "LOCKED") L.hits.push({ id: tr.id, x: x / dpr, y: y / dpr, w: w / dpr, h: h / dpr });
    }
    g.restore();
    if (stale()) { g.fillStyle = "rgba(0,0,0,.45)"; g.fillRect(0, 0, c.width, c.height); g.fillStyle = v("--warn"); g.font = `700 ${22 * dpr}px system-ui`; g.fillText("STALE_DATA — no new processed frame", 20 * dpr, 40 * dpr); }
  }

  function drawVo() {
    const c = $("l-vo"), g = c.getContext("2d"), r = c.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    c.width = Math.max(1, Math.round(r.width * dpr)); c.height = Math.max(1, Math.round(r.height * dpr));
    g.clearRect(0, 0, c.width, c.height);
    const pts = L.vo?.points || [];
    if (pts.length < 2) { $("l-vometa").textContent = "VO: no pose yet in this segment (needs parallax). Map position: NOT_AVAILABLE."; return; }
    const xs = pts.map((p) => p[1]), zs = pts.map((p) => p[3]);
    const mnx = Math.min(...xs), mxx = Math.max(...xs), mnz = Math.min(...zs), mxz = Math.max(...zs);
    const sc = Math.min((c.width - 20 * dpr) / Math.max(1e-6, mxx - mnx), (c.height - 20 * dpr) / Math.max(1e-6, mxz - mnz));
    const css = getComputedStyle(document.documentElement);
    g.strokeStyle = css.getPropertyValue("--accent").trim(); g.lineWidth = 2 * dpr; g.beginPath();
    pts.forEach((p, i) => { const x = 10 * dpr + (p[1] - mnx) * sc, y = c.height - 10 * dpr - (p[3] - mnz) * sc; i ? g.lineTo(x, y) : g.moveTo(x, y); });
    g.stroke();
    // current position, so a camera that has not moved still shows where the path is
    const lp = pts[pts.length - 1], still = Math.max(mxx - mnx, mxz - mnz) < 1e-6;
    g.fillStyle = css.getPropertyValue("--warn").trim(); g.beginPath();
    g.arc(still ? c.width / 2 : 10 * dpr + (lp[1] - mnx) * sc, still ? c.height / 2 : c.height - 10 * dpr - (lp[3] - mnz) * sc, 4 * dpr, 0, 7); g.fill();
    if (still) { g.fillStyle = css.getPropertyValue("--muted").trim(); g.font = `${12 * dpr}px system-ui`; g.textAlign = "center"; g.fillText("camera has not moved in this segment", c.width / 2, c.height / 2 + 22 * dpr); g.textAlign = "start"; }
    $("l-vometa").textContent = `VO segment ${L.vo.segment} (epoch ${L.vo.epoch}): ${pts.length} poses, top view x/z, relative scale, no loop closure. Map position: NOT_AVAILABLE.`;
  }

  function render() {
    const online = ctx.isOnline(), st = L.state?.status || {}, row = L.row, running = !!L.sid;
    $("l-start").disabled = !online || running || ctx.otherJobRunning() || !$("l-input").value;
    for (const id of ["l-pause", "l-resume", "l-stop", "l-seek", "l-clear"]) $(id).disabled = !online || !running;
    $("l-empty").hidden = !!L.image;
    const s = $("l-state");
    const label = !running && !L.state ? "NO RUN" : stale() ? "STALE_DATA" : !online && running ? "DISCONNECTED" : running ? (st.input_state || L.state?.state || "STARTING") : L.state?.state;
    s.textContent = label; s.dataset.tone = { LIVE: "ok", PAUSED: "warn", STALLED: "warn", STALE_DATA: "stale", DISCONNECTED: "error", SUCCEEDED: "ok", FAILED: "error", INCOMPLETE: "error", CANCELLED: "warn", EOF: "offline" }[label] || "offline";
    $("lm-input").textContent = st.input_state || "—";
    $("lm-fps").textContent = st.recent_fps != null ? `${st.recent_fps.toFixed(1)} fps` : "—";
    $("lm-lat").textContent = st.latency_ms_p95_recent != null ? `${st.latency_ms_p95_recent.toFixed(0)} ms` : "—";
    $("lm-drop").textContent = st.dropped_total != null ? `${st.dropped_total} of ${st.captured_total}` : "—";
    $("lm-epoch").textContent = st.epoch ?? "—";
    $("lm-age").textContent = L.lastRowSeen ? `${((performance.now() - L.lastRowSeen) / 1000).toFixed(1)} s since last processed frame` : "—";
    if (row) {
      const ts = row.target_state || "SEARCHING";
      $("l-tstate").textContent = row.target_id == null ? "NO TARGET" : ts; $("l-tstate").dataset.tone = row.target_id == null ? "offline" : TONE[ts] || "offline";
      const tid = row.target_id ?? st.target_id;  // a selection made while paused shows before the next frame
      $("l-tid").textContent = tid == null ? "" : `#${tid}`;
      $("l-yaw").textContent = `yaw ${row.yaw > 0 ? "+" : ""}${row.yaw} / ±40`;
      const f = (row.yaw || 0) / 80; $("l-yawfill").style.left = `${50 + Math.min(0, f) * 100}%`; $("l-yawfill").style.width = `${Math.abs(f) * 100}%`;
      $("l-stop-reasons").textContent = row.stop_reasons?.length ? `STOP · ${row.stop_reasons.join(", ")}` : "Centring the target (MOCK command).";
      $("l-vostate").textContent = `${row.vo_state || "—"} · seg ${row.vo_segment ?? "—"}`;
      $("l-vostate").dataset.tone = { TRACKING: "ok", LOW_CONFIDENCE: "warn", LOST: "error", INITIALIZING: "offline" }[row.vo_state] || "offline";
      $("l-time").textContent = `media ${row.media_t?.toFixed(2)} s${L.duration ? ` / ${L.duration.toFixed(1)}` : ""} · run ${row.run_t?.toFixed(1)} s · latency ${row.latency_ms?.toFixed(0)} ms`;
      if (L.duration) { $("l-seek").max = L.duration; if (document.activeElement !== $("l-seek")) $("l-seek").value = row.media_t || 0; }
    }
    const ul = $("l-events"); ul.replaceChildren();
    if (!L.events.length) ul.append(Object.assign(document.createElement("li"), { className: "muted", textContent: "No events yet." }));
    for (const e of L.events) ul.append(Object.assign(document.createElement("li"), { className: "muted", textContent: `${(e.t ?? e.run_t ?? 0).toFixed?.(1) ?? e.t} s · ${e.type}${e.track_id != null ? ` #${e.track_id}` : ""}${e.to != null ? ` → ${e.to}` : ""}${e.reason ? ` (${e.reason})` : ""}` }));
    const bd = $("l-badges"); bd.replaceChildren();
    if (L.row || L.state) bd.append(ctx.badge("MOCK · NOT SENT", "sim"), ctx.badge("VO (relative)", "warn"), ctx.badge("MAP POSITION N/A", "offline"));
    draw();
  }

  $("l-form").addEventListener("submit", start);
  $("l-pause").addEventListener("click", () => command("pause"));
  $("l-resume").addEventListener("click", () => command("resume"));
  $("l-stop").addEventListener("click", () => command("stop"));
  $("l-clear").addEventListener("click", () => command("clear"));
  $("l-seek").addEventListener("change", (e) => command("seek", { media_t: parseFloat(e.target.value) }));
  $("l-canvas").addEventListener("click", (e) => {
    const r = e.currentTarget.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    const hit = L.hits.filter((h) => x >= h.x && x <= h.x + h.w && y >= h.y && y <= h.y + h.h).sort((a, b) => a.w * a.h - b.w * b.h)[0];
    if (hit) command("select", { track_id: hit.id });
  });
  setInterval(() => { if (L.sid) render(); }, 250);

  let started = false;
  return {
    enter() { if (!started) { started = true; refreshInputs(); } requestAnimationFrame(render); },
    reattach(sid) { if (sid && !L.sid) { L.sid = sid; poll(); } },
    running: () => !!L.sid,
    onKey(e) { if (e.key === " " && L.sid) { e.preventDefault(); command(L.state?.status?.input_state === "PAUSED" ? "resume" : "pause"); } },
    renderControls: render,
  };
}
