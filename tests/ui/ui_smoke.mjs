// End-to-end UI check in a headless Chromium browser (Dia by default) over the DevTools protocol.
// Usage: node tests/ui/ui_smoke.mjs [--attach http://127.0.0.1:9222 | --browser /Applications/Dia.app/Contents/MacOS/Dia]
//   --attach  use the Dia window that is already open (Dia started with --remote-debugging-port=9222): the test opens
//             ONE tab there and closes only that tab at the end; no new browser process or window is started.
// Starts its own console server on a free port with a temporary maps root (stored sessions are symlinked in),
// so builds made by the test never mix with real outputs. Writes screenshots + results to outputs/evidence/ui/.
import { spawn } from "node:child_process";
import { mkdtempSync, mkdirSync, symlinkSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import net from "node:net";

const ROOT = resolve(import.meta.dirname, "../..");
const OUT = join(ROOT, "outputs/evidence/ui");
const argBrowser = process.argv.indexOf("--browser");
const BROWSER = argBrowser > 0 ? process.argv[argBrowser + 1] : "/Applications/Dia.app/Contents/MacOS/Dia";
const argAttach = process.argv.indexOf("--attach");
const ATTACH = argAttach > 0 ? process.argv[argAttach + 1] : null;
const MQ_PARTIAL = "mq_bangkok_60s_after_mf300", MQ_DENSE = "mqg_sumida_0025_0125";
const REAL = "tum_fr1_desk_K4", SIM = "synthetic_room_seed7", TRACK = "tum_walking_xyz_nano", TRACK_AMB = "tum_walking_xyz_nano_fixv3";
mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const freePort = () => new Promise((r) => { const s = net.createServer(); s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => r(p)); }); });
const results = []; const consoleErrors = []; const extensionErrors = [];
const check = (name, ok, detail = "") => { results.push({ name, status: ok ? "PASS" : "FAIL", detail }); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`); };

// ---------- server ----------
const mapsRoot = mkdtempSync(join(tmpdir(), "sda-ui-maps-"));
for (const s of [REAL, SIM, MQ_PARTIAL, MQ_DENSE]) if (existsSync(join(ROOT, "outputs/maps", s))) symlinkSync(join(ROOT, "outputs/maps", s), join(mapsRoot, s));
const tracksRoot = mkdtempSync(join(tmpdir(), "sda-ui-tracks-"));
const runsRoot = mkdtempSync(join(tmpdir(), "sda-ui-runs-"));
for (const t of [TRACK, TRACK_AMB]) if (existsSync(join(ROOT, "outputs/tracks", t))) symlinkSync(join(ROOT, "outputs/tracks", t), join(tracksRoot, t));
const port = await freePort();
let server;
const startServer = () => { server = spawn(join(ROOT, ".venv/bin/python"), [join(ROOT, "apps/desktop/server.py"), "--port", String(port), "--maps-root", mapsRoot, "--tracks-root", tracksRoot, "--runs-root", runsRoot], { stdio: "ignore" }); };
startServer();
const BASE = `http://127.0.0.1:${port}`;
for (let i = 0; i < 50; i++) { try { if ((await fetch(`${BASE}/api/health`)).ok) break; } catch {} await sleep(100); }

// ---------- browser ----------
let browser = null, wsUrl, attachedTabId = null;
if (ATTACH) {
  try {
    const t = await (await fetch(`${ATTACH}/json/new?about:blank`, { method: "PUT" })).json();
    wsUrl = t.webSocketDebuggerUrl; attachedTabId = t.id;
  } catch (e) { console.error(`cannot attach to ${ATTACH}: ${e.message}. Start Dia with --remote-debugging-port=9222.`); server.kill(); process.exit(2); }
}
const cdpPort = ATTACH ? null : await freePort();
if (!ATTACH) {
  const profile = mkdtempSync(join(tmpdir(), "sda-ui-profile-"));
  browser = spawn(BROWSER, [`--headless=new`, `--remote-debugging-port=${cdpPort}`, `--user-data-dir=${profile}`, "--no-first-run", "--no-default-browser-check", "--window-size=1600,1000", "about:blank"], { stdio: "ignore" });
}
for (let i = 0; i < 200 && !wsUrl; i++) {
  try {
    const t = await (await fetch(`http://127.0.0.1:${cdpPort}/json/list`)).json();
    wsUrl = t.find((x) => x.type === "page")?.webSocketDebuggerUrl;
    if (!wsUrl && i > 20) wsUrl = (await (await fetch(`http://127.0.0.1:${cdpPort}/json/new?about:blank`, { method: "PUT" })).json()).webSocketDebuggerUrl;
  } catch {}
  if (!wsUrl) await sleep(150);
}
if (!wsUrl) { console.error("browser did not expose a DevTools page"); process.exit(2); }
const ws = new WebSocket(wsUrl);
await new Promise((r) => ws.addEventListener("open", r));
let seq = 0; const pending = new Map();
ws.addEventListener("message", (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  // In --attach mode the user's own profile is used, so browser extensions can log errors into the page; those are
  // counted separately and do not fail the console check.
  const fromExtension = (d) => /chrome-extension:\/\//.test(JSON.stringify(d?.stackTrace || d?.url || "")) || /Receiving end does not exist/.test(JSON.stringify(d));
  if (msg.method === "Runtime.exceptionThrown") {
    const d = msg.params.exceptionDetails;
    (fromExtension(d) ? extensionErrors : consoleErrors).push(d.exception?.description || d.text);
  }
  if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
    const text = msg.params.args.map((a) => a.value ?? a.description).join(" ");
    (fromExtension(msg.params) ? extensionErrors : consoleErrors).push(text);
  }
});
const send = (method, params = {}) => new Promise((r) => { const id = ++seq; pending.set(id, r); ws.send(JSON.stringify({ id, method, params })); });
const evaluate = async (expr) => { const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true }); if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description); return r.result?.result?.value; };
const waitFor = async (expr, ms = 15000) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { try { if (await evaluate(expr)) return true; } catch {} await sleep(150); } return false; };
const shot = async (name) => { const r = await send("Page.captureScreenshot", { format: "png" }); writeFileSync(join(OUT, name), Buffer.from(r.result.data, "base64")); };
const key = async (k, code = k) => { for (const type of ["keyDown", "keyUp"]) await send("Input.dispatchKeyEvent", { type, key: k, code, windowsVirtualKeyCode: { ArrowRight: 39, ArrowLeft: 37, Tab: 9, " ": 32 }[k] || 0 }); };
const text = (id) => evaluate(`document.getElementById(${JSON.stringify(id)}).textContent`);
const clickSession = (sid) => evaluate(`[...document.querySelectorAll('#sessions button')].find(b => b.querySelector('.sid').textContent === ${JSON.stringify(sid)})?.click() ?? 'missing'`);

await send("Page.enable"); await send("Runtime.enable");
// An attached tab in an existing window would be a background tab, and Chromium throttles its timers (the console's
// health polling) after a few minutes; keep it in front for the duration of the test. It is closed at the end.
if (ATTACH) await send("Page.bringToFront");
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: BASE + "/" });

try {
  // 1. connection + empty state
  check("connects to local server", await waitFor(`document.getElementById('conn-badge').textContent === 'SERVER ONLINE'`));
  check("shows empty state before any map", await evaluate(`!document.getElementById('map-empty').hidden && document.getElementById('empty-title').textContent === 'No map loaded'`));
  check("HARDWARE_ARMED shown false", (await text("armed")) === "false");
  check("replay controls disabled without map", await evaluate(`document.getElementById('play').disabled && document.getElementById('scrub').disabled`));
  await waitFor(`document.querySelectorAll('#sessions button').length >= 2`);
  await shot("01_empty_state.png");

  // 2. load the real stored map
  await clickSession(REAL);
  check("real map loads", await waitFor(`document.getElementById('map-empty').hidden && document.getElementById('m-pts').textContent !== '—'`));
  const badges = await text("badges");
  check("labels: STORED_RECONSTRUCTION, RELATIVE, NOT_NAVIGATION_SAFE, PUBLIC_DATASET", ["STORED_RECONSTRUCTION", "SCALE RELATIVE", "NOT_NAVIGATION_SAFE", "PUBLIC_DATASET"].every((b) => badges.includes(b)), badges);
  const report = await (await fetch(`${BASE}/api/sessions/${REAL}`)).json();
  const pts = (await text("m-pts")).replace(/,/g, "");
  check("UI point count equals map_report points3D", Number(pts) === report.report.reconstruction.points3D, `${pts} vs ${report.report.reconstruction.points3D}`);
  check("viewer drew points", await waitFor(`document.getElementById('m-render').textContent.includes('pts')`, 5000), await text("m-render"));
  check("camera frame image shown", await waitFor(`!document.getElementById('frame-img').hidden && document.getElementById('frame-img').naturalWidth > 0`));
  await sleep(400);
  await shot("02_real_map_loaded.png");

  // 3. replay: keyboard stepping + growth mode
  await evaluate(`document.body.focus()`);
  for (let i = 0; i < 12; i++) await key("ArrowRight");
  check("arrow keys step through poses", (await text("pose-count")).startsWith("13 /"), await text("pose-count"));
  await evaluate(`document.getElementById('growth').click()`);
  await sleep(1300);  // status bar refreshes once per second
  const shownGrowth = await evaluate(`(() => { const m = document.getElementById('m-render').textContent.match(/\\(([\\d,]+) of /); return m ? Number(m[1].replace(/,/g,'')) : -1 })()`);
  check("map growth hides points not yet observed", shownGrowth > 0 && shownGrowth < report.report.reconstruction.points3D, `${shownGrowth} drawn of ${report.report.reconstruction.points3D}`);
  await key(" ");
  await sleep(1300);
  await key(" ");
  const afterPlay = await text("pose-count");
  check("Space plays and pauses replay", !afterPlay.startsWith("13 /"), afterPlay);
  await shot("03_replay_growth.png");

  // 3b. map quality is shown instead of "ready"; a partial map says so and exposes its models and coverage
  check("session list never says READY", await evaluate(`![...document.querySelectorAll('#sessions .badge')].some(b => b.textContent === 'READY')`));
  check("real map shows its quality verdict", (await text("badges")).includes("QUALITY GOOD"), await text("badges"));
  check("sparse-points caption shown", (await text("map-overlay-tl")).includes("SPARSE POINTS"));
  if (existsSync(join(ROOT, "outputs/maps", MQ_PARTIAL))) {
    await clickSession(MQ_PARTIAL);
    check("partial map loads with QUALITY PARTIAL/POOR", await waitFor(`document.getElementById('map-empty').hidden && /QUALITY (PARTIAL|POOR)/.test(document.getElementById('badges').textContent)`, 20000), await text("badges"));
    check("partial map is labelled PARTIAL RECONSTRUCTION", (await text("map-overlay-tl")).includes("PARTIAL RECONSTRUCTION"), await text("map-overlay-tl"));
    const nModels = await evaluate(`document.getElementById('model-select').hidden ? 0 : document.getElementById('model-select').options.length`);
    const rep = await (await fetch(`${BASE}/api/sessions/${MQ_PARTIAL}`)).json();
    check("model selector lists every separate model", nModels === rep.report.reconstruction.num_models && nModels > 1, `${nModels} vs ${rep.report.reconstruction.num_models}`);
    check("coverage strip shows registered/missing frames", await waitFor(`!document.getElementById('coverage-wrap').hidden && document.getElementById('coverage-meta').textContent.includes('selected frames')`), await text("coverage-meta"));
    await shot("03b_partial_map_model1.png");
    if (nModels > 1) {
      await evaluate(`(() => { const s = document.getElementById('model-select'); s.value = '1'; s.dispatchEvent(new Event('change')); })()`);
      check("second model loads in its own frame", await waitFor(`document.getElementById('map-overlay-tl').textContent.includes('model 2 of')`, 20000), await text("map-overlay-tl"));
      await shot("03c_partial_map_model2.png");
    }
  }

  // 3c. dense layer on a GOOD map: separate, labelled, more points than the sparse layer
  if (existsSync(join(ROOT, "outputs/maps", MQ_DENSE, "dense", "dense_verified.ply"))) {
    await clickSession(MQ_DENSE);
    await waitFor(`document.getElementById('map-empty').hidden && document.getElementById('badges').textContent.includes('QUALITY GOOD')`, 20000);
    check("dense layer offered only when verified dense exists", await evaluate(`!document.getElementById('layer').options[2].disabled`));
    await evaluate(`(() => { const s = document.getElementById('layer'); s.value = 'dense'; s.dispatchEvent(new Event('change')); })()`);
    check("dense layer loads with its own caption", await waitFor(`document.getElementById('map-overlay-tl').textContent.includes('DENSE · VERIFIED')`, 60000), await text("map-overlay-tl"));
    await sleep(1500);
    check("status bar reports raw / filtered / dense counts", /sparse raw [\d,]+ \/ filtered [\d,]+ · dense verified [\d,]+/.test(await text("m-render")), await text("m-render"));
    check("dense layer hides path and cameras by default", await evaluate(`document.getElementById('show-path').getAttribute('aria-pressed') === 'false' && document.getElementById('show-cams').getAttribute('aria-pressed') === 'false'`));
    await evaluate(`document.getElementById('show-path').click()`);
    check("path toggle turns the trajectory back on", await evaluate(`document.getElementById('show-path').getAttribute('aria-pressed') === 'true'`));
    await evaluate(`document.getElementById('show-path').click()`);
    check("viewport names the layer on screen with its point count", /LAYER ▸ DENSE · VERIFIED · [\d,]+ pts in file/.test(await text("map-overlay-tl")), await text("map-overlay-tl"));
    await shot("03d_dense_layer.png");
    if (existsSync(join(ROOT, "outputs/maps", MQ_DENSE, "dense_experimental", "dense_verified.ply"))) {
      check("experimental dense layer offered separately", await evaluate(`!document.getElementById('layer').options[3].disabled`));
      await evaluate(`(() => { const s = document.getElementById('layer'); s.value = 'dense_exp'; s.dispatchEvent(new Event('change')); })()`);
      check("experimental dense layer is labelled NOT ADOPTED", await waitFor(`document.getElementById('map-overlay-tl').textContent.includes('DENSE · EXPERIMENTAL, NOT ADOPTED') && document.getElementById('map-overlay-tl').textContent.includes('LAYER ▸ DENSE · EXPERIMENTAL')`, 60000), await text("map-overlay-tl"));
      await sleep(1500);
      check("status bar reports the experimental count separately", /dense experimental [\d,]+ · showing dense_exp/.test(await text("m-render")), await text("m-render"));
      check("panel title follows the layer", (await text("map-title")) === "Dense 3D map · experimental", await text("map-title"));
      await shot("03e_dense_experimental_layer.png");
    }
    if (existsSync(join(ROOT, "outputs/maps", MQ_DENSE, "dense_static_v2", "dense_verified.ply"))) {
      await evaluate(`(() => { const s = document.getElementById('layer'); s.value = 'dense_static_v2'; s.dispatchEvent(new Event('change')); })()`);
      check("static map v2 layer is labelled as a trial, not adopted", await waitFor(`document.getElementById('map-overlay-tl').textContent.includes('DENSE · STATIC SCENE v2, NOT ADOPTED')`, 60000), await text("map-overlay-tl"));
      await sleep(1500);
      check("static map v2: path and cameras off, title follows", await evaluate(`document.getElementById('show-path').getAttribute('aria-pressed') === 'false' && document.getElementById('show-cams').getAttribute('aria-pressed') === 'false' && document.getElementById('map-title').textContent === 'Dense 3D map · static scene v2 trial'`));
      check("static map v2: status bar reports the file's point count and the drawn count", /[\d,]+ of 1,530,092 pts drawn · .*showing dense_static_v2/.test(await text("m-render")), await text("m-render"));
      await shot("03f_static_map_v2.png");
    }
    if (existsSync(join(ROOT, "outputs/maps", MQ_DENSE, "dense_static_v3", "dense_verified.ply"))) {
      await evaluate(`(() => { const s = document.getElementById('layer'); s.value = 'dense_static_v3'; s.dispatchEvent(new Event('change')); })()`);
      check("static map v3 layer is labelled NOT ADOPTED with its method", await waitFor(`document.getElementById('map-overlay-tl').textContent.includes('DENSE · STATIC SCENE v3, NOT ADOPTED') && document.getElementById('map-overlay-tl').textContent.includes('far-surface neighbours')`, 60000), await text("map-overlay-tl"));
      await sleep(1500);
      await shot("03g_static_map_v3.png");
    }
    await evaluate(`(() => { const s = document.getElementById('layer'); s.value = 'sparse'; s.dispatchEvent(new Event('change')); })()`);
    await waitFor(`document.getElementById('map-overlay-tl').textContent.includes('SPARSE POINTS (SfM)')`, 20000);
  }

  // 4. simulated map is labelled as such
  await clickSession(SIM);
  check("synthetic map labelled SIMULATED", await waitFor(`document.getElementById('badges').textContent.includes('SIMULATED') && document.getElementById('map-overlay-tl').textContent.includes('SIMULATED')`));
  await sleep(300);
  await shot("04_simulated_map.png");

  // 5. build from the UI (synthetic input, fast), then cancel a second build
  await evaluate(`(() => { const s = document.getElementById('input-select'); s.value = [...s.options].find(o => o.value.includes('synthetic')).value; s.dispatchEvent(new Event('change')); })()`);
  check("Build enabled for a valid input", await evaluate(`!document.getElementById('build').disabled && document.getElementById('build-form').checkValidity()`));
  await evaluate(`document.getElementById('build').click()`);
  check("build shows progress while running", await waitFor(`['RUNNING','STARTING'].includes(document.getElementById('job-badge').textContent) || document.getElementById('job-badge').textContent === 'SUCCEEDED'`, 5000));
  check("build finishes and auto-loads", await waitFor(`document.getElementById('job-badge').textContent === 'SUCCEEDED' && document.getElementById('session-label').textContent.startsWith('room_seed7-')`, 60000), await text("job-msg"));
  await shot("05_build_succeeded.png");

  await evaluate(`(() => { const s = document.getElementById('input-select'); s.value = [...s.options].find(o => o.value.includes('tum')).value; s.dispatchEvent(new Event('change')); })()`);
  await evaluate(`document.getElementById('build').click()`);
  await waitFor(`document.getElementById('job-badge').textContent === 'RUNNING'`, 10000);
  check("Build disabled while a job runs", await evaluate(`document.getElementById('build').disabled`));
  await evaluate(`document.getElementById('cancel').click()`);
  check("cancel asks for confirmation", await evaluate(`!document.getElementById('cancel-confirm').hidden`));
  await evaluate(`document.getElementById('cancel-yes').click()`);
  check("cancelled build ends as CANCELLED", await waitFor(`document.getElementById('job-badge').textContent === 'CANCELLED'`, 30000), await text("job-msg"));
  await shot("06_build_cancelled.png");

  // 6. focus visibility
  await evaluate(`document.getElementById('build').disabled = false; document.getElementById('build').focus()`);
  await key("Tab");
  const outline = await evaluate(`getComputedStyle(document.activeElement).outlineStyle + ' ' + document.activeElement.id`);
  check("keyboard focus has visible outline", outline.startsWith("solid"), outline);

  // 7. narrow window: controls stay reachable
  for (const [w, h] of [[1100, 800], [700, 900]]) {
    await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile: false });
    await sleep(400);
    const vis = await evaluate(`(() => { const r = document.getElementById('build').getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth; })()`);
    const noHScroll = await evaluate(`document.documentElement.scrollWidth <= innerWidth + 1`);
    check(`at ${w}x${h}: Build button visible without scrolling, no horizontal overflow`, vis && noHScroll, `visible=${vis} noHScroll=${noHScroll}`);
    await shot(`07_narrow_${w}.png`);
  }
  await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });

  // 8. tracking workspace
  await evaluate(`document.getElementById('mode-track').click()`);
  check("Tracking tab shows the tracking workspace", await waitFor(`!document.getElementById('ws-track').hidden && document.getElementById('ws-map').hidden && location.hash === '#tracking'`));
  check("tracking empty state before a run is loaded", await evaluate(`!document.getElementById('t-empty').hidden && document.getElementById('t-play').disabled`));
  check("detector list offers the verified local model", await waitFor(`[...document.getElementById('t-model').options].some(o => o.value === 'yolox_nano' && !o.disabled)`));
  await waitFor(`[...document.querySelectorAll('#t-runs button')].some(b => b.querySelector('.sid').textContent === '${TRACK}')`);
  await shot("09_tracking_empty.png");
  await evaluate(`[...document.querySelectorAll('#t-runs button')].find(b => b.querySelector('.sid').textContent === '${TRACK}').click()`);
  check("recorded run loads with REAL_MODEL label", await waitFor(`document.getElementById('t-empty').hidden && document.getElementById('t-badges').textContent.includes('REAL_MODEL') && document.getElementById('t-badges').textContent.includes('yolox_nano')`), await text("t-badges"));
  check("accuracy shown as undefined without ground truth", (await text("tm-acc")).includes("undefined"), await text("tm-acc"));
  const rec = await (await fetch(`${BASE}/api/tracks/${TRACK}/files/tracks.json`)).json();
  check("UI frame count equals tracks.json", (await text("t-count")).includes(`/ ${rec.frames.length}`), await text("t-count"));
  // jump to the first frame whose lowest-id LOCKED track is also the first entry of the list (key 1)
  const k0 = rec.frames.findIndex((f) => { const s = [...f.tracks].sort((a, b) => a.id - b.id); return s.length && s[0].state === "LOCKED"; });
  await evaluate(`(() => { const s = document.getElementById('t-scrub'); s.value = ${k0}; s.dispatchEvent(new Event('input')); })()`);
  await waitFor(`document.getElementById('t-count').textContent.startsWith('${k0 + 1} /')`);
  const tid = [...rec.frames[k0].tracks].sort((a, b) => a.id - b.id)[0].id;
  await evaluate(`document.body.focus()`);
  await key("1", "Digit1");
  check("key 1 selects the first track as target and shows LOCKED", await waitFor(`document.getElementById('r-tstate').textContent === 'LOCKED' && document.getElementById('r-tid').textContent === '#${tid}'`), await text("r-tstate"));
  check("virtual command labelled MOCK and yaw within limit", await evaluate(`(() => { const m = document.getElementById('r-yaw').textContent.match(/yaw ([+-]?\\d+) \\/ ±(\\d+)/); return !!m && Math.abs(+m[1]) <= +m[2] && document.getElementById('r-cmd').textContent.includes('MOCK'); })()`), await text("r-yaw"));
  await shot("10_tracking_target_locked.png");
  const ctl = await (await fetch(`${BASE}/api/tracks/${TRACK}/control`, { method: "POST", body: JSON.stringify({ target_id: tid, select_k: k0 }) })).json();
  const kp = ctl.frames.findIndex((r, i) => i > k0 && r.target_state !== "LOCKED");
  if (kp > 0) {
    await evaluate(`(() => { const s = document.getElementById('t-scrub'); s.value = ${kp}; s.dispatchEvent(new Event('input')); })()`);
    const want = ctl.frames[kp].target_state;
    check(`after the target is no longer detected the UI shows ${want} and STOP`, await waitFor(`document.getElementById('r-tstate').textContent === '${want}' && document.getElementById('r-stop').textContent.startsWith('STOP') && document.getElementById('r-yaw').textContent.startsWith('yaw 0')`), `${await text("r-tstate")} · ${await text("r-stop")}`);
    await shot("11_tracking_target_not_locked.png");
  }
  await key("0", "Digit0");
  check("key 0 clears the target", await waitFor(`document.getElementById('r-tstate').textContent === 'NO TARGET'`));
  // canvas click selects the box under the pointer
  await evaluate(`(() => { const s = document.getElementById('t-scrub'); s.value = ${k0}; s.dispatchEvent(new Event('input')); })()`);
  await sleep(300);
  const pt = await evaluate(`(() => { const c = document.getElementById('t-canvas').getBoundingClientRect(); const [W, H] = [${rec.image_wh[0]}, ${rec.image_wh[1]}]; const s = Math.min(c.width / W, c.height / H); const ox = (c.width - W * s) / 2, oy = (c.height - H * s) / 2; const b = ${JSON.stringify([...rec.frames[k0].tracks].sort((a, b) => a.id - b.id)[0].bbox)}; return [c.left + ox + (b[0] + b[2]) / 2 * s, c.top + oy + (b[1] + b[3]) / 2 * s]; })()`);
  for (const type of ["mousePressed", "mouseReleased"]) await send("Input.dispatchMouseEvent", { type, x: pt[0], y: pt[1], button: "left", clickCount: 1 });
  check("clicking a box selects that person", await waitFor(`document.getElementById('r-tid').textContent === '#${tid}'`), await text("r-tid"));
  await evaluate(`document.getElementById('t-raw').click()`);
  await key(" "); await sleep(1200); await key(" ");
  check("Space plays the recorded run", !(await text("t-count")).startsWith(`${k0 + 1} /`), await text("t-count"));
  await shot("12_tracking_playback_raw.png");

  // ambiguous target: recorded with the conservative tracker; the console must show AMBIGUOUS and STOP
  await key("0", "Digit0");
  await evaluate(`[...document.querySelectorAll('#t-runs button')].find(b => b.querySelector('.sid').textContent === '${TRACK_AMB}')?.click()`);
  check("run with ambiguous frames loads", await waitFor(`document.getElementById('t-empty').hidden && document.getElementById('t-count').textContent.startsWith('1 /')`, 15000));
  const recA = await (await fetch(`${BASE}/api/tracks/${TRACK_AMB}/files/tracks.json`)).json();
  // first (k_sel, id) where id is LOCKED and the same id later becomes AMBIGUOUS
  let pick = null;
  for (const f of recA.frames) { for (const tr of f.tracks) { if (tr.state !== "LOCKED") continue; const later = recA.frames.find((g) => g.k > f.k && g.tracks.some((x) => x.id === tr.id && x.state === "AMBIGUOUS")); if (later) { pick = { k: f.k, id: tr.id, ka: later.k }; break; } } if (pick) break; }
  check("recorded run contains a LOCKED track that later turns AMBIGUOUS", !!pick, JSON.stringify(pick));
  if (pick) {
    await evaluate(`(() => { const s = document.getElementById('t-scrub'); s.value = ${pick.k}; s.dispatchEvent(new Event('input')); })()`);
    await waitFor(`document.getElementById('t-count').textContent.startsWith('${pick.k + 1} /')`);
    await sleep(200);
    const rank = [...recA.frames[pick.k].tracks].sort((a, b) => a.id - b.id).findIndex((x) => x.id === pick.id);
    await evaluate(`document.querySelectorAll('#t-tracks button')[${rank}].click()`);
    await waitFor(`document.getElementById('r-tid').textContent === '#${pick.id}' && document.getElementById('r-tstate').textContent === 'LOCKED'`);
    await evaluate(`(() => { const s = document.getElementById('t-scrub'); s.value = ${pick.ka}; s.dispatchEvent(new Event('input')); })()`);
    check("AMBIGUOUS target shows STOP · TARGET_AMBIGUOUS and yaw 0", await waitFor(`document.getElementById('r-tstate').textContent === 'AMBIGUOUS' && document.getElementById('r-stop').textContent.includes('TARGET_AMBIGUOUS') && document.getElementById('r-yaw').textContent.startsWith('yaw 0')`), `${await text("r-tstate")} · ${await text("r-stop")}`);
    check("track list marks the AMBIGUOUS track", await evaluate(`[...document.querySelectorAll('#t-tracks button')].some(b => b.textContent.includes('AMBIGUOUS'))`));
    await shot("16_tracking_ambiguous_stop.png");
    await key("0", "Digit0");
  }

  // run the real detector from the UI on the pedestrian clip, then cancel a longer run
  await evaluate(`(() => { const s = document.getElementById('t-input'); s.value = [...s.options].find(o => o.value.includes('vtest')).value; s.dispatchEvent(new Event('change')); document.getElementById('t-max').value = 40; })()`);
  await evaluate(`document.getElementById('t-run').click()`);
  check("tracking run shows progress", await waitFor(`['RUNNING','STARTING','SUCCEEDED'].includes(document.getElementById('t-job-badge').textContent)`, 5000));
  check("Build map disabled while a tracking run is active (one job at a time)", await waitFor(`document.getElementById('build').disabled || document.getElementById('t-job-badge').textContent === 'SUCCEEDED'`, 3000));
  check("tracking run finishes and auto-loads", await waitFor(`document.getElementById('t-job-badge').textContent === 'SUCCEEDED' && document.getElementById('t-count').textContent.includes('/ 40')`, 60000), await text("t-job-msg"));
  await shot("13_tracking_run_succeeded.png");
  await evaluate(`document.getElementById('t-max').value = 3000; document.getElementById('t-run').click()`);
  await waitFor(`document.getElementById('t-job-badge').textContent === 'RUNNING'`, 10000);
  await evaluate(`document.getElementById('t-cancel').click()`);
  check("tracking cancel asks for confirmation", await evaluate(`!document.getElementById('t-cancel-confirm').hidden`));
  await evaluate(`document.getElementById('t-cancel-yes').click()`);
  check("cancelled tracking run ends as CANCELLED", await waitFor(`document.getElementById('t-job-badge').textContent === 'CANCELLED'`, 30000), await text("t-job-msg"));
  await shot("14_tracking_cancelled.png");
  for (const [w, h] of [[1100, 800], [700, 900]]) {
    await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile: false });
    await sleep(400);
    const noHScroll = await evaluate(`document.documentElement.scrollWidth <= innerWidth + 1`);
    check(`tracking at ${w}x${h}: no horizontal overflow`, noHScroll);
    await shot(`15_tracking_narrow_${w}.png`);
  }
  await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
  await evaluate(`window.__beforeReload = 1`);  // gone once the new document has replaced the old one
  await send("Page.reload");
  check("reload keeps the tracking workspace (#tracking)", await waitFor(`!window.__beforeReload && document.readyState === 'complete' && document.getElementById('conn-badge')?.textContent === 'SERVER ONLINE' && !document.getElementById('ws-track').hidden`));
  await evaluate(`document.getElementById('mode-map').click()`);

  // 8b. live run: start, frame + boxes, pause/resume, select a target by clicking, stop, verified, replay
  await evaluate(`document.getElementById('mode-live').click()`);
  check("Live tab shows the live workspace", await waitFor(`!document.getElementById('ws-live').hidden && location.hash === '#live'`));
  await waitFor(`[...document.getElementById('l-input').options].some(o => o.value.includes('vtest'))`);
  await evaluate(`(() => { const s = document.getElementById('l-input'); s.value = [...s.options].find(o => o.value.includes('vtest')).value; document.getElementById('l-max').value = 14; })()`);
  await evaluate(`document.getElementById('l-start').click()`);
  check("live run shows processed frames", await waitFor(`document.getElementById('l-empty').hidden && document.getElementById('l-state').textContent === 'LIVE'`, 30000), await text("l-state"));
  check("live status bar shows fps, latency and drops", await waitFor(`/fps/.test(document.getElementById('lm-fps').textContent) && /ms/.test(document.getElementById('lm-lat').textContent) && / of /.test(document.getElementById('lm-drop').textContent)`, 5000), `${await text("lm-fps")} · ${await text("lm-lat")} · ${await text("lm-drop")}`);
  check("map position is shown as not available (no 3D person, no map registration)", (await text("l-vometa")).includes("NOT_AVAILABLE"));
  await evaluate(`document.getElementById('l-pause').click()`);
  check("pause reaches the worker (PAUSED)", await waitFor(`document.getElementById('l-state').textContent === 'PAUSED'`, 5000), await text("l-state"));
  await evaluate(`document.getElementById('l-resume').click()`);
  check("resume returns to LIVE", await waitFor(`document.getElementById('l-state').textContent === 'LIVE'`, 5000), await text("l-state"));
  // click the centre of a LOCKED box from the newest row
  // pause first so the frame on screen and the frame read from the API are the same one, then click a LOCKED box
  let picked = null;
  for (let i = 0; i < 12 && !picked; i++) {
    await evaluate(`document.getElementById('l-pause').click()`);
    await waitFor(`document.getElementById('l-state').textContent === 'PAUSED'`, 5000);
    await sleep(600);
    const runs = await (await fetch(`${BASE}/api/runs`)).json();
    const lt = runs.length ? await (await fetch(`${BASE}/api/runs/${runs[0].id}/latest`)).json() : null;
    const tr = lt?.row?.tracks?.find((t) => t.state === "LOCKED");
    if (tr) {
      const pt = await evaluate(`(() => { const c = document.getElementById('l-canvas').getBoundingClientRect(); const [W, H] = ${JSON.stringify(lt.image_wh)}; const s = Math.min(c.width / W, c.height / H); const ox = (c.width - W * s) / 2, oy = (c.height - H * s) / 2; const b = ${JSON.stringify(tr.bbox)}; return [c.left + ox + (b[0] + b[2]) / 2 * s, c.top + oy + (b[1] + b[3]) / 2 * s]; })()`);
      for (const type of ["mousePressed", "mouseReleased"]) await send("Input.dispatchMouseEvent", { type, x: pt[0], y: pt[1], button: "left", clickCount: 1 });
      picked = tr.id;
    } else {
      await evaluate(`document.getElementById('l-resume').click()`);
      await sleep(700);
    }
  }
  check("clicking a LOCKED box selects it as the target", picked != null && await waitFor(`document.getElementById('l-tid').textContent !== '' || document.getElementById('l-events').textContent.includes('SELECT')`, 8000), `picked #${picked} · ${await text("l-tid")} · ${await text("l-events")}`);
  await shot("17_live_run.png");
  await evaluate(`document.getElementById('l-resume').click()`);
  await sleep(800);
  await evaluate(`document.getElementById('l-stop').click()`);
  check("stopped run finishes with verified files", await waitFor(`document.getElementById('l-state').textContent === 'SUCCEEDED' && document.getElementById('l-msg').textContent.includes('verified')`, 30000), await text("l-msg"));
  await shot("18_live_finished.png");
  await evaluate(`document.getElementById('mode-track').click()`);
  await evaluate(`document.getElementById('t-refresh').click()`);
  check("finished live run is listed for replay", await waitFor(`[...document.querySelectorAll('#t-runs .meta')].some(m => m.textContent.startsWith('LIVE RUN'))`, 10000));
  await evaluate(`[...document.querySelectorAll('#t-runs button')].find(b => b.querySelector('.meta').textContent.startsWith('LIVE RUN')).click()`);
  check("live run replays as OPEN_LOOP_REPLAY", await waitFor(`document.getElementById('t-badges').textContent.includes('OPEN_LOOP_REPLAY')`, 15000), await text("t-badges"));
  await evaluate(`document.getElementById('mode-map').click()`);

  // 9. server loss and reconnect
  server.kill("SIGTERM");
  check("server loss shown as DISCONNECTED", await waitFor(`document.getElementById('conn-badge').textContent === 'DISCONNECTED'`, 20000));
  check("Build disabled while disconnected", await evaluate(`document.getElementById('build').disabled`));
  check("Run tracking disabled while disconnected", await evaluate(`document.getElementById('t-run').disabled`));
  check("Live start disabled while disconnected", await evaluate(`document.getElementById('l-start').disabled`));
  await shot("08_disconnected.png");
  startServer();
  check("reconnects automatically", await waitFor(`document.getElementById('conn-badge').textContent === 'SERVER ONLINE'`, 20000));
} catch (e) {
  check("ui script ran without exception", false, String(e));
} finally {
  check("no JS errors in console", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" | "));
  const summary = { browser: ATTACH ? `attached to existing Dia at ${ATTACH}` : BROWSER, date: new Date().toISOString(), port, results, consoleErrors, extensionErrors,
    pass: results.filter((r) => r.status === "PASS").length, fail: results.filter((r) => r.status === "FAIL").length };
  writeFileSync(join(OUT, "ui_smoke.json"), JSON.stringify(summary, null, 1));
  console.log(`\n${summary.pass} passed, ${summary.fail} failed → ${join(OUT, "ui_smoke.json")}`);
  ws.close();
  if (attachedTabId) { try { await fetch(`${ATTACH}/json/close/${attachedTabId}`); } catch {} }  // close only our tab
  else browser.kill("SIGKILL");
  server.kill();
  process.exit(summary.fail ? 1 : 0);
}
