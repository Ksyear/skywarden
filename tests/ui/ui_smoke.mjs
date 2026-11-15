// End-to-end UI check of the console over the DevTools protocol.
// Usage: node tests/ui/ui_smoke.mjs [--attach http://127.0.0.1:9222 | --browser <path>]
//   Browser (tests/ui/cdp.mjs): the browser already open with --remote-debugging-port=9222 (one tab is opened there
//   and only that tab is closed at the end), else --browser / CHROME_PATH started headless, else exit 2 with a message.
// Starts its own console server on a free port with temporary roots, so builds made by the test never mix with real
// outputs. The core mapping checks use the fixture session in tests/fixtures/maps (in git); checks that need stored
// sessions or inputs from outputs/ (gitignored) run when those exist and are recorded as SKIP otherwise, so the check
// count never changes silently. Writes screenshots + results to outputs/evidence/ui/.
import { spawn } from "node:child_process";
import { mkdtempSync, mkdirSync, symlinkSync, writeFileSync, existsSync, cpSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { connectBrowser, freePort, sleep } from "./cdp.mjs";

const ROOT = resolve(import.meta.dirname, "../..");
const OUT = join(ROOT, "outputs/evidence/ui");
const FIXTURES = join(ROOT, "tests/fixtures");
const MQ_PARTIAL = "mq_bangkok_60s_after_mf300", MQ_DENSE = "mqg_sumida_0025_0125";
const REAL = "tum_fr1_desk_K4", SIM = "fixture_synthetic_room", TRACK = "tum_walking_xyz_nano", TRACK_AMB = "tum_walking_xyz_nano_fixv3";
mkdirSync(OUT, { recursive: true });

const results = []; const consoleErrors = []; const extensionErrors = [];
const check = (name, ok, detail = "") => { results.push({ name, status: ok ? "PASS" : "FAIL", detail }); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`); };
const skip = (names, why) => { for (const name of [names].flat()) { results.push({ name, status: "SKIP", detail: why }); console.log(`SKIP  ${name}  — ${why}`); } };

// ---------- server ----------
const mapsRoot = mkdtempSync(join(tmpdir(), "sda-ui-maps-"));
// stored sessions may have been moved out of the app's list into outputs/archive (see outputs/archive/manifest.json)
const stored = (kind, name) => [join(ROOT, "outputs", kind, name), join(ROOT, "outputs/archive", kind, name)].find((p) => existsSync(p));
const linked = (name, ...rest) => existsSync(join(mapsRoot, name, ...rest));
symlinkSync(join(FIXTURES, "maps", SIM), join(mapsRoot, SIM));
for (const s of [REAL, MQ_PARTIAL, MQ_DENSE]) if (stored("maps", s)) symlinkSync(stored("maps", s), join(mapsRoot, s));
// the Measure test writes scale.json, so it works on a copy (never on a symlinked session); the long name also checks
// that session names are not cut off in the list
const SIM_SCALE = `${SIM}_scale_test`;
cpSync(join(FIXTURES, "maps", SIM), join(mapsRoot, SIM_SCALE), { recursive: true, filter: (src) => !src.endsWith("scale.json") });
const tracksRoot = mkdtempSync(join(tmpdir(), "sda-ui-tracks-"));
const runsRoot = mkdtempSync(join(tmpdir(), "sda-ui-runs-"));
for (const t of [TRACK, TRACK_AMB]) if (stored("tracks", t)) symlinkSync(stored("tracks", t), join(tracksRoot, t));
const port = await freePort();
let server;
const startServer = () => { server = spawn(join(ROOT, ".venv/bin/python"), [join(ROOT, "apps/desktop/server.py"), "--port", String(port), "--maps-root", mapsRoot, "--tracks-root", tracksRoot, "--runs-root", runsRoot], { stdio: "ignore" }); };
startServer();
const BASE = `http://127.0.0.1:${port}`;
for (let i = 0; i < 50; i++) { try { if ((await fetch(`${BASE}/api/health`)).ok) break; } catch {} await sleep(100); }

// ---------- browser ----------
let B;
try { B = await connectBrowser({ width: 1600, height: 1000 }); }
catch (e) { console.error(e.message); server.kill(); process.exit(2); }
const { send, evaluate, waitFor } = B;
B.onEvent((msg) => {
  // With an attached browser the user's own profile is used, so extensions can log errors into the page; those are
  // counted separately and do not fail the console check.
  const fromExtension = (d) => /chrome-extension:\/\//.test(JSON.stringify(d?.stackTrace || d?.url || "")) || /Receiving end does not exist/.test(JSON.stringify(d));
  if (msg.method === "Runtime.exceptionThrown") { const d = msg.params.exceptionDetails; (fromExtension(d) ? extensionErrors : consoleErrors).push(d.exception?.description || d.text); }
  if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
    const t = msg.params.args.map((a) => a.value ?? a.description).join(" ");
    (fromExtension(msg.params) ? extensionErrors : consoleErrors).push(t);
  }
});
const shot = (name) => B.shot(join(OUT, name));
const key = async (k, code = k) => { for (const type of ["keyDown", "keyUp"]) await send("Input.dispatchKeyEvent", { type, key: k, code, windowsVirtualKeyCode: { ArrowRight: 39, ArrowLeft: 37, Tab: 9, " ": 32 }[k] || 0 }); };
const text = (id) => evaluate(`document.getElementById(${JSON.stringify(id)}).textContent`);
const clickSession = (sid) => evaluate(`[...document.querySelectorAll('#sessions button')].find(b => b.querySelector('.sid').textContent === ${JSON.stringify(sid)})?.click() ?? 'missing'`);
const setLayer = (v) => evaluate(`(() => { const s = document.getElementById('layer'); s.value = '${v}'; s.dispatchEvent(new Event('change')); })()`);
const clickAt = async ([x, y]) => { for (const type of ["mousePressed", "mouseReleased"]) await send("Input.dispatchMouseEvent", { type, x, y, button: "left", clickCount: 1 }); await sleep(150); };
// in-page POST: same origin and JSON content type, like the console itself
const pagePost = (path, body) => evaluate(`fetch(${JSON.stringify(path)}, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: ${JSON.stringify(JSON.stringify(body))} }).then(r => r.json())`);
// rectangles of two elements overlap (both visible)
const overlap = (a, b) => evaluate(`(() => { const A = ${a}, B = ${b}; if (!A || !B || A.hidden || B.hidden) return false; const r = A.getBoundingClientRect(), s = B.getBoundingClientRect();
  return r.width > 0 && s.width > 0 && r.left < s.right && s.left < r.right && r.top < s.bottom && s.top < r.bottom; })()`);

await send("Page.navigate", { url: BASE + "/" });

try {
  // 1. connection + empty state
  check("connects to local server", await waitFor(`document.getElementById('conn-badge').textContent === 'SERVER ONLINE'`));
  check("shows empty state before any map", await evaluate(`!document.getElementById('map-empty').hidden && document.getElementById('empty-title').textContent === 'No map loaded'`));
  check("HARDWARE_ARMED shown false", (await text("armed")) === "false");
  check("replay controls disabled without map", await evaluate(`document.getElementById('play').disabled && document.getElementById('scrub').disabled`));
  check("map renderer is WebGL (2D canvas only as fallback)", await evaluate(`window.__skywarden.viewer.renderer === 'webgl' && document.getElementById('map-canvas').dataset.renderer === 'webgl'`), await evaluate(`window.__skywarden.viewer.renderer`));
  await waitFor(`document.querySelectorAll('#sessions button').length >= 2`);
  check("long session names are not cut off in the stored-maps list", await evaluate(`(() => { const s = [...document.querySelectorAll('#sessions .sid')].find(e => e.textContent === '${SIM_SCALE}'); return !!s && s.scrollWidth <= s.clientWidth + 1; })()`));
  await shot("01_empty_state.png");

  // 2. load the real stored map (or the fixture when no stored session is available)
  const FIRST = stored("maps", REAL) ? REAL : SIM;
  await clickSession(FIRST);
  check(`stored map loads (${FIRST})`, await waitFor(`document.getElementById('map-empty').hidden && document.getElementById('m-pts').textContent !== '—'`));
  const badges = await text("badges");
  if (FIRST === REAL) {
    check("labels: PUBLIC_DATASET, SCALE RELATIVE, NOT_NAVIGATION_SAFE, calibration; stored reconstruction named in the map",
      ["SCALE RELATIVE", "NOT_NAVIGATION_SAFE", "PUBLIC_DATASET", "CALIB PROVIDED"].every((b) => badges.includes(b)) && (await text("map-overlay-tl")).includes("Replay of stored reconstruction"), badges);
  } else skip("labels: PUBLIC_DATASET, SCALE RELATIVE, NOT_NAVIGATION_SAFE, calibration; stored reconstruction named in the map", `${REAL} not in outputs/`);
  const report = await (await fetch(`${BASE}/api/sessions/${FIRST}`)).json();
  const pts = (await text("m-pts")).replace(/,/g, "");
  check("UI point count equals map_report points3D", Number(pts) === report.report.reconstruction.points3D, `${pts} vs ${report.report.reconstruction.points3D}`);
  check("viewer drew points", await waitFor(`/WebGL \\([\\d,]+ of [\\d,]+ pts drawn\\)/.test(document.getElementById('m-render').textContent)`, 5000), await text("m-render"));
  check("camera frame image shown", await waitFor(`!document.getElementById('frame-img').hidden && document.getElementById('frame-img').naturalWidth > 0`));
  // the GPU drew something where the viewer says a point is (read back from the WebGL canvas)
  const probe = await evaluate(`(() => { const v = window.__skywarden.viewer, s = v.sampleScreenPoints(8); return s.map(p => v.probePixel(p[0], p[1])).filter(c => !v.isBackground(c)).length; })()`);
  check("WebGL canvas has the points the viewer projects (pixel read-back)", probe >= 6, `${probe} of 8 sampled point positions non-background`);
  await sleep(400);
  await shot("02_real_map_loaded.png");

  // 3. replay: keyboard stepping + growth mode
  await evaluate(`document.body.focus()`);
  const nPoses = report.report.reconstruction.registered_images;
  const steps = Math.min(12, nPoses - 2);
  for (let i = 0; i < steps; i++) await key("ArrowRight");
  check("arrow keys step through poses", (await text("pose-count")).startsWith(`${steps + 1} /`), await text("pose-count"));
  await evaluate(`document.getElementById('growth').click()`);
  await sleep(1300);  // status bar refreshes once per second
  const shownGrowth = await evaluate(`(() => { const m = document.getElementById('m-render').textContent.match(/\\(([\\d,]+) of /); return m ? Number(m[1].replace(/,/g,'')) : -1 })()`);
  check("map growth hides points not yet observed", shownGrowth > 0 && shownGrowth < report.report.reconstruction.points3D, `${shownGrowth} drawn of ${report.report.reconstruction.points3D}`);
  await evaluate(`document.getElementById('growth').click()`);
  await evaluate(`(() => { const s = document.getElementById('scrub'); s.value = 0; s.dispatchEvent(new Event('input')); })()`);
  await key(" ");
  await sleep(1300);
  await key(" ");
  const afterPlay = await text("pose-count");
  check("Space plays and pauses replay", !afterPlay.startsWith("1 /"), afterPlay);
  await shot("03_replay_growth.png");

  // 3b. map quality is shown instead of "ready"; overview, Follow
  check("session list never says READY", await evaluate(`![...document.querySelectorAll('#sessions .badge')].some(b => b.textContent === 'READY')`));
  check("map shows its quality verdict", (await text("badges")).includes("QUALITY GOOD"), await text("badges"));
  check("sparse-points caption shown", (await text("map-overlay-tl")).includes("SPARSE POINTS"));
  check("overview map shown in the corner for a loaded map", await evaluate(`!document.getElementById('map-overview').hidden`));
  check("overview map does not cover the bottom-left captions", !(await overlap(`document.getElementById('map-overview')`, `document.getElementById('map-overlay-bl')`)));
  check("Follow is on by default", await evaluate(`document.getElementById('follow').getAttribute('aria-pressed') === 'true'`));
  await evaluate(`(() => { const s = document.getElementById('scrub'); s.value = 1; s.dispatchEvent(new Event('input')); })()`);
  await evaluate(`document.getElementById('play').click()`); await sleep(700);
  check("map rides with the camera while the video plays", await evaluate(`document.getElementById('map-canvas').dataset.follow === 'on'`));
  await shot("03h2_follow_camera.png");
  await evaluate(`document.getElementById('play').click()`); await sleep(600);
  check("pausing hands the map to the user (free view)", await evaluate(`document.getElementById('map-canvas').dataset.follow === 'off' && document.getElementById('play').getAttribute('aria-pressed') === 'false'`));
  await evaluate(`document.getElementById('reset-view').click()`);

  const PARTIAL_CHECKS = ["partial map loads with QUALITY PARTIAL/POOR", "partial map is labelled PARTIAL RECONSTRUCTION", "model selector lists every separate model", "coverage strip shows registered/missing frames", "second model loads in its own frame"];
  if (linked(MQ_PARTIAL)) {
    await clickSession(MQ_PARTIAL);
    check(PARTIAL_CHECKS[0], await waitFor(`document.getElementById('map-empty').hidden && /QUALITY (PARTIAL|POOR)/.test(document.getElementById('badges').textContent)`, 20000), await text("badges"));
    check(PARTIAL_CHECKS[1], (await text("map-overlay-tl")).includes("PARTIAL RECONSTRUCTION"), await text("map-overlay-tl"));
    const nModels = await evaluate(`document.getElementById('model-select').hidden ? 0 : document.getElementById('model-select').options.length`);
    const rep = await (await fetch(`${BASE}/api/sessions/${MQ_PARTIAL}`)).json();
    check(PARTIAL_CHECKS[2], nModels === rep.report.reconstruction.num_models && nModels > 1, `${nModels} vs ${rep.report.reconstruction.num_models}`);
    check(PARTIAL_CHECKS[3], await waitFor(`!document.getElementById('coverage-wrap').hidden && document.getElementById('coverage-meta').textContent.includes('selected frames')`), await text("coverage-meta"));
    await shot("03b_partial_map_model1.png");
    if (nModels > 1) {
      await evaluate(`(() => { const s = document.getElementById('model-select'); s.value = '1'; s.dispatchEvent(new Event('change')); })()`);
      check(PARTIAL_CHECKS[4], await waitFor(`document.getElementById('map-overlay-tl').textContent.includes('model 2 of')`, 20000), await text("map-overlay-tl"));
      await shot("03c_partial_map_model2.png");
    } else skip(PARTIAL_CHECKS[4], "only one model");
  } else skip(PARTIAL_CHECKS, `${MQ_PARTIAL} not in outputs/`);

  // 3c. dense layer: separate, labelled, path/cameras off by default (fixture has a small verified dense layer)
  const DENSE_SID = linked(MQ_DENSE, "dense", "dense_verified.ply") ? MQ_DENSE : SIM;
  await clickSession(DENSE_SID);
  await waitFor(`document.getElementById('session-label').textContent === '${DENSE_SID}' && document.getElementById('map-empty').hidden && document.getElementById('badges').textContent.includes('QUALITY GOOD')`, 20000);
  if (DENSE_SID === MQ_DENSE) check("map built without a calibration file is marked UNCALIBRATED", (await text("badges")).includes("UNCALIBRATED"), await text("badges"));
  else skip("map built without a calibration file is marked UNCALIBRATED", `${MQ_DENSE} not in outputs/ (the fixture has a calibration file)`);
  check("dense layer offered only when verified dense exists", await evaluate(`!document.getElementById('layer').options[2].disabled`));
  await setLayer("dense");
  check("dense layer loads with its own caption", await waitFor(`document.getElementById('map-overlay-tl').textContent.includes('DENSE · VERIFIED')`, 60000), await text("map-overlay-tl"));
  await sleep(1500);
  check("status bar reports raw / dense counts and the layer shown", /dense verified [\d,]+ · showing dense/.test(await text("m-layers")), await text("m-layers"));
  check("dense layer hides path and cameras by default", await evaluate(`document.getElementById('show-path').getAttribute('aria-pressed') === 'false' && document.getElementById('show-cams').getAttribute('aria-pressed') === 'false'`));
  await evaluate(`document.getElementById('show-path').click()`);
  check("path toggle turns the trajectory back on", await evaluate(`document.getElementById('show-path').getAttribute('aria-pressed') === 'true'`));
  await evaluate(`document.getElementById('show-path').click()`);
  check("viewport names the layer on screen with its point count", /LAYER ▸ DENSE · VERIFIED · [\d,]+ pts in file/.test(await text("map-overlay-tl")), await text("map-overlay-tl"));
  check("long layer description is clamped to two lines, full text kept", await evaluate(`(() => { const c = document.querySelector('#map-overlay-tl .chip.caption'); return !!c && c.title.length > 0 && c.textContent === c.title && c.getBoundingClientRect().height < 3.2 * parseFloat(getComputedStyle(c).lineHeight); })()`));
  await shot("03d_dense_layer.png");
  const LEARNED_CHECKS = ["learned-depth layer is a labelled trial", "state lane shows learned depth on the frame time axis", "Nearby view shows the overview of the whole map", "model-estimated layer is not selected by default"];
  if (DENSE_SID === MQ_DENSE && linked(MQ_DENSE, "dense_learned", "dense_verified.ply")) {
    await setLayer("dense_learned");
    check(LEARNED_CHECKS[0], await waitFor(`document.getElementById('map-overlay-tl').textContent.includes('DENSE + LEARNED DEPTH, TRIAL')`, 90000), await text("map-overlay-tl"));
    await sleep(1500);
    check(LEARNED_CHECKS[1], await waitFor(`!document.getElementById('state-lane').hidden && document.getElementById('state-meta').textContent.includes('learned depth at t=')`, 10000), await text("state-meta"));
    await evaluate(`document.getElementById('roi').click()`); await sleep(800);
    check(LEARNED_CHECKS[2], await evaluate(`document.getElementById('roi').getAttribute('aria-pressed') === 'true' && !document.getElementById('map-overview').hidden`));
    await shot("03h_learned_nearby_overview.png");
    await evaluate(`document.getElementById('roi').click()`);
    check(LEARNED_CHECKS[3], await evaluate(`document.getElementById('layer').value !== 'model_estimated'`));
  } else skip(LEARNED_CHECKS, `${MQ_DENSE}/dense_learned not in outputs/ (archived)`);
  if (DENSE_SID === MQ_DENSE && linked(MQ_DENSE, "dense_onpath", "dense_verified.ply")) {
    await setLayer("dense_onpath");
    check("model-output layer is labelled as model output, not a verified map", await waitFor(`document.getElementById('map-overlay-tl').textContent.includes('MODEL OUTPUT, STEADY') && document.getElementById('map-overlay-tl').textContent.includes('LAYER ▸ MODEL OUTPUT · STEADY')`, 90000), await text("map-overlay-tl"));
    await sleep(1500);
    check("1.37M-point layer drawn with WebGL", /WebGL \([\d,]+ of 1,367,234 pts drawn\)/.test(await text("m-render")), await text("m-render"));
    await shot("03i_model_output_steady.png");
  } else skip(["model-output layer is labelled as model output, not a verified map", "1.37M-point layer drawn with WebGL"], `${MQ_DENSE}/dense_onpath not in outputs/`);
  await setLayer("sparse");
  await waitFor(`document.getElementById('map-overlay-tl').textContent.includes('SPARSE POINTS (SfM)')`, 20000);

  // 4. simulated map is labelled as such
  await clickSession(SIM);
  check("synthetic map labelled SIMULATED", await waitFor(`document.getElementById('session-label').textContent === '${SIM}' && document.getElementById('badges').textContent.includes('SIMULATED') && document.getElementById('map-overlay-tl').textContent.includes('SIMULATED')`));
  await sleep(300);
  await shot("04_simulated_map.png");

  // 4b. Measure tool: two points -> distance; a tape length as scale, two independent checks -> verified
  await clickSession(SIM_SCALE);
  check("scale chip says no metric scale before any measurement", await waitFor(`document.getElementById('session-label').textContent === '${SIM_SCALE}' && document.getElementById('map-overlay-bl').textContent.includes('미터 스케일 없음 · 상대 단위')`, 20000), await text("map-overlay-bl"));
  for (const id of ["show-path", "show-cams"]) await evaluate(`document.getElementById('${id}').getAttribute('aria-pressed') === 'true' && document.getElementById('${id}').click()`);
  await evaluate(`document.getElementById('measure').click()`);
  check("Measure toggles the measure box", await evaluate(`document.getElementById('measure').getAttribute('aria-pressed') === 'true' && !document.getElementById('measure-box').hidden`));
  await sleep(400);
  check("Measure box does not cover the map captions", !(await overlap(`document.getElementById('measure-box')`, `document.getElementById('map-overlay-tl')`)));
  // screen positions of drawn points (from the viewer's own projection), where the map canvas is on top; taken again
  // before every pair, as a user clicks what is on screen now
  const sampleSpots = () => evaluate(`(() => { const c = document.getElementById('map-canvas'); return window.__skywarden.viewer.sampleScreenPoints(24).filter(([x, y]) => document.elementFromPoint(x, y) === c); })()`).then((s) => s.length >= 8 ? [0.05, 0.95, 0.2, 0.8, 0.3, 0.7, 0.4, 0.6].map((q) => s[Math.floor(q * (s.length - 1))]) : []);
  let spots = await sampleSpots();
  const stageRect = () => evaluate(`JSON.stringify(document.getElementById('map-canvas').getBoundingClientRect())`);
  const stage0 = await stageRect();
  const pickPair = async (a, b) => { const now = await sampleSpots(); if (now.length === 8) spots = now; await clickAt(spots[a]); await clickAt(spots[b]); return text("measure-text"); };
  const setTrue = (v) => evaluate(`(() => { const i = document.getElementById('measure-true'); i.value = '${v}'; i.dispatchEvent(new Event('input')); })()`);
  check("found drawn points to click", spots.length === 8, JSON.stringify(spots));
  if (spots.length === 8) {
    // the picked point is drawn, and it projects within the pick radius of the click
    const pk = await evaluate(`(() => { const v = window.__skywarden.viewer, [x, y] = ${JSON.stringify(spots[0])}, p = v.pickPoint(x, y); return p && { inPrefix: p.index < v._shown.count, d: Math.hypot(p.client[0] - x, p.client[1] - y) }; })()`);
    check("pick returns a drawn point within 8 px of the click", !!pk && pk.inPrefix && pk.d <= 8.01, JSON.stringify(pk));
    const t1 = await pickPair(0, 1);
    check("two clicks show the distance in map units", /^[\d.]+ 지도 단위$/.test(t1), t1);
    await setTrue(1.5);
    check("tape-length buttons enabled with two points and a length", await evaluate(`!document.getElementById('measure-fit').disabled && !document.getElementById('measure-verify').disabled`));
    await evaluate(`document.getElementById('measure-fit').click()`);
    check("using it as scale: chip shows measured, unverified 0/2", await waitFor(`document.getElementById('map-overlay-bl').textContent.includes('미터 스케일: 측정됨 · 미검증 (검증 길이 0/2 통과)')`, 5000), await text("map-overlay-bl"));
    check("header badge says the metres are unverified", (await text("badges")).includes("SCALE RELATIVE → m (unverified)"), await text("badges"));
    for (const [a, b] of [[2, 3], [4, 5]]) {
      const t2 = await pickPair(a, b);
      const m = t2.match(/≈ ([\d.]+) m \(미검증\)/);
      check(`metres shown with the unverified label (pair ${a},${b})`, !!m, t2);
      if (m) { await setTrue(m[1]); await evaluate(`document.getElementById('measure-verify').click()`); await sleep(500); }
    }
    check("two independent checks within 5 %: chip says verified", await waitFor(`document.getElementById('map-overlay-bl').textContent.includes('미터 스케일: 측정으로 확인됨 (줄자 두 점, 관측 1개, 최대 오차')`, 5000), await text("map-overlay-bl"));
    check("header badge adds → m (verified); NOT_NAVIGATION_SAFE stays", (await text("badges")).includes("SCALE RELATIVE → m (verified)") && (await text("badges")).includes("NOT_NAVIGATION_SAFE"), await text("badges"));
    const sj = await (await fetch(`${BASE}/api/sessions/${SIM_SCALE}/files/scale.json`)).json();
    check("scale.json on disk is MEASURED_VERIFIED with 1 fit + 2 checks", sj.status === "MEASURED_VERIFIED" && sj.observations.length === 1 && sj.verification.checks.length === 2, `${sj.status} obs=${sj.observations?.length} checks=${sj.verification?.checks?.length}`);
    const t3 = await pickPair(6, 7);
    check("distance after verification carries the verified label", /≈ [\d.]+ m \(측정으로 확인됨\)/.test(t3), t3);
    check("the map does not move when the scale labels change (top bar stays one line)", (await stageRect()) === stage0 && await evaluate(`document.querySelector('.topbar').getBoundingClientRect().height < 56`), `${stage0} → ${await stageRect()}`);
    await sleep(300);
    await shot("measure_tool.png");
  }
  await evaluate(`document.getElementById('measure').click()`);
  check("Measure off hides the box", await evaluate(`document.getElementById('measure-box').hidden`));

  // 4c. level of detail on a large cloud (made in the page, 1.2M points around the fixture's cameras): a zoomed-out
  // view draws an even sample; zooming back in draws every point; picks stay inside the drawn sample
  const lod = await evaluate(`(() => {
    const v = window.__skywarden.viewer, n = 1200000, xyz = new Float32Array(3 * n), rgb = new Uint8Array(3 * n), first = new Uint32Array(n), c = v._center0, R = v.radiusMain;
    let s = 7; const rnd = () => (s = (s * 16807) % 2147483647) / 2147483647;
    for (let i = 0; i < n; i++) { xyz[3*i] = c[0] + (rnd() - .5) * 2 * R; xyz[3*i+1] = c[1] + (rnd() - .5) * 2 * R; xyz[3*i+2] = c[2] + (rnd() - .5) * 0.2 * R; rgb.fill(180 + (i % 60), 3*i, 3*i+3); }
    v.setData({ count: n, xyz, rgb, first }, v.poses, { frameOnCloud: true });
    v._draw({ silent: true, noSync: true }); const atDefault = v._shown.count;
    v.view.dist *= 25; v._draw({ silent: true, noSync: true }); const far = v._shown.count;
    const pick = v.sampleScreenPoints(4).map(([x, y]) => v.pickPoint(x, y)).filter(Boolean).every((p) => p.index < v._shown.count);
    v.view.dist /= 200; v._draw({ silent: true, noSync: true }); const close = v._shown.count;
    return { n, atDefault, far, close, pick };
  })()`);
  check("LOD: zoomed far out draws an even sample, close up draws every point", lod.far < lod.n && lod.far <= lod.atDefault && lod.close === lod.n && lod.pick, JSON.stringify(lod));
  await clickSession(SIM);
  await waitFor(`document.getElementById('session-label').textContent === '${SIM}' && document.getElementById('map-empty').hidden`);

  // 4d. 2D fallback (no WebGL): same map, same labels, picking works
  await send("Page.navigate", { url: BASE + "/?renderer=2d" });
  await waitFor(`document.getElementById('conn-badge')?.textContent === 'SERVER ONLINE'`);
  await waitFor(`document.querySelectorAll('#sessions button').length >= 2`);
  await clickSession(SIM);
  check("2D fallback renderer draws the map", await waitFor(`window.__skywarden.viewer.renderer === '2d' && /2D canvas \\([\\d,]+ of [\\d,]+ pts drawn\\)/.test(document.getElementById('m-render').textContent)`, 10000), await text("m-render"));
  const pick2d = await evaluate(`(() => { const v = window.__skywarden.viewer, s = v.sampleScreenPoints(8); return s.length ? !!v.pickPoint(s[3][0], s[3][1]) && !v.isBackground(v.probePixel(s[3][0], s[3][1])) : false; })()`);
  check("2D fallback: a drawn point can be picked", pick2d);
  await send("Page.navigate", { url: BASE + "/" });
  await waitFor(`document.getElementById('conn-badge')?.textContent === 'SERVER ONLINE'`);

  // 5. build from the UI (synthetic input, fast), then cancel a second build
  const BUILD_CHECKS = ["Build enabled for a valid input", "build shows progress while running", "build finishes and auto-loads", "Build disabled while a job runs", "cancel asks for confirmation", "cancelled build ends as CANCELLED"];
  await waitFor(`document.getElementById('input-select').options.length > 0`, 5000);
  const inputs = await (await fetch(`${BASE}/api/inputs`)).json();
  const synthIn = inputs.find((i) => i.id.includes("synthetic")), longIn = inputs.find((i) => i.id.includes("tum"));
  if (synthIn && longIn) {
    await evaluate(`(() => { const s = document.getElementById('input-select'); s.value = ${JSON.stringify(synthIn.id)}; s.dispatchEvent(new Event('change')); })()`);
    check(BUILD_CHECKS[0], await evaluate(`!document.getElementById('build').disabled && document.getElementById('build-form').checkValidity()`));
    await evaluate(`document.getElementById('build').click()`);
    check(BUILD_CHECKS[1], await waitFor(`['RUNNING','STARTING'].includes(document.getElementById('job-badge').textContent) || document.getElementById('job-badge').textContent === 'SUCCEEDED'`, 5000));
    check(BUILD_CHECKS[2], await waitFor(`document.getElementById('job-badge').textContent === 'SUCCEEDED' && document.getElementById('session-label').textContent.startsWith('room_seed7-')`, 60000), await text("job-msg"));
    await shot("05_build_succeeded.png");
    await evaluate(`(() => { const s = document.getElementById('input-select'); s.value = ${JSON.stringify(longIn.id)}; s.dispatchEvent(new Event('change')); })()`);
    await evaluate(`document.getElementById('build').click()`);
    await waitFor(`document.getElementById('job-badge').textContent === 'RUNNING'`, 10000);
    check(BUILD_CHECKS[3], await evaluate(`document.getElementById('build').disabled`));
    await evaluate(`document.getElementById('cancel').click()`);
    check(BUILD_CHECKS[4], await evaluate(`!document.getElementById('cancel-confirm').hidden`));
    await evaluate(`document.getElementById('cancel-yes').click()`);
    check(BUILD_CHECKS[5], await waitFor(`document.getElementById('job-badge').textContent === 'CANCELLED'`, 30000), await text("job-msg"));
    await shot("06_build_cancelled.png");
  } else skip(BUILD_CHECKS, "synthetic / TUM inputs not present (outputs/synthetic, data/public)");

  // 6. focus visibility, keyboard reach of the map toolbar
  await evaluate(`document.getElementById('build').disabled = false; document.getElementById('build').focus()`);
  await key("Tab");
  const outline = await evaluate(`getComputedStyle(document.activeElement).outlineStyle + ' ' + document.activeElement.id`);
  check("keyboard focus has visible outline", outline.startsWith("solid"), outline);
  check("map toolbar is a labelled toolbar with grouped buttons", await evaluate(`(() => { const t = document.querySelector('.toolbar[role=toolbar]'); return !!t && t.getAttribute('aria-label') === 'Map view' && t.querySelectorAll('[role=group][aria-label]').length === 3; })()`));
  check("top bar fits on one line at 1600 px", await evaluate(`document.querySelector('.topbar').getBoundingClientRect().height < 56`), `${await evaluate(`document.querySelector('.topbar').getBoundingClientRect().height`)} px`);

  // 7. narrow window: controls stay reachable
  for (const [w, h] of [[1100, 800], [700, 900]]) {
    await B.viewport(w, h, 1);
    await sleep(400);
    const vis = await evaluate(`(() => { const r = document.getElementById('build').getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth; })()`);
    const noHScroll = await evaluate(`document.documentElement.scrollWidth <= innerWidth + 1`);
    check(`at ${w}x${h}: Build button visible without scrolling, no horizontal overflow`, vis && noHScroll, `visible=${vis} noHScroll=${noHScroll}`);
    await shot(`07_narrow_${w}.png`);
  }
  await B.viewport(1600, 1000, 1);

  // 8. tracking workspace
  await evaluate(`document.getElementById('mode-track').click()`);
  check("Tracking tab shows the tracking workspace", await waitFor(`!document.getElementById('ws-track').hidden && document.getElementById('ws-map').hidden && location.hash === '#tracking'`));
  check("tracking empty state before a run is loaded", await evaluate(`!document.getElementById('t-empty').hidden && document.getElementById('t-play').disabled`));
  check("detector list offers the verified local model", await waitFor(`[...document.getElementById('t-model').options].some(o => o.value === 'yolox_nano' && !o.disabled)`));
  await shot("09_tracking_empty.png");
  const TRACK_CHECKS = ["recorded run loads with REAL_MODEL label", "accuracy shown as undefined without ground truth", "UI frame count equals tracks.json", "key 1 selects the first track as target and shows LOCKED", "virtual command labelled MOCK and yaw within limit", "after the target is no longer detected the UI shows STOP", "key 0 clears the target", "clicking a box selects that person", "Space plays the recorded run"];
  if (stored("tracks", TRACK)) {
    await waitFor(`[...document.querySelectorAll('#t-runs button')].some(b => b.querySelector('.sid').textContent === '${TRACK}')`);
    await evaluate(`[...document.querySelectorAll('#t-runs button')].find(b => b.querySelector('.sid').textContent === '${TRACK}').click()`);
    check(TRACK_CHECKS[0], await waitFor(`document.getElementById('t-empty').hidden && document.getElementById('t-badges').textContent.includes('REAL_MODEL') && document.getElementById('t-badges').textContent.includes('yolox_nano')`), await text("t-badges"));
    check(TRACK_CHECKS[1], (await text("tm-acc")).includes("undefined"), await text("tm-acc"));
    check("association mode shown for the recorded run", /^assoc (strict|margin)/.test(await text("tm-assoc")), await text("tm-assoc"));
    const rec = await (await fetch(`${BASE}/api/tracks/${TRACK}/files/tracks.json`)).json();
    check(TRACK_CHECKS[2], (await text("t-count")).includes(`/ ${rec.frames.length}`), await text("t-count"));
    // jump to the first frame whose lowest-id LOCKED track is also the first entry of the list (key 1)
    const k0 = rec.frames.findIndex((f) => { const s = [...f.tracks].sort((a, b) => a.id - b.id); return s.length && s[0].state === "LOCKED"; });
    await evaluate(`(() => { const s = document.getElementById('t-scrub'); s.value = ${k0}; s.dispatchEvent(new Event('input')); })()`);
    await waitFor(`document.getElementById('t-count').textContent.startsWith('${k0 + 1} /')`);
    const tid = [...rec.frames[k0].tracks].sort((a, b) => a.id - b.id)[0].id;
    await evaluate(`document.body.focus()`);
    await key("1", "Digit1");
    check(TRACK_CHECKS[3], await waitFor(`document.getElementById('r-tstate').textContent === 'LOCKED' && document.getElementById('r-tid').textContent === '#${tid}'`), await text("r-tstate"));
    check(TRACK_CHECKS[4], await evaluate(`(() => { const m = document.getElementById('r-yaw').textContent.match(/yaw ([+-]?\\d+) \\/ ±(\\d+)/); return !!m && Math.abs(+m[1]) <= +m[2] && document.getElementById('r-cmd').textContent.includes('MOCK'); })()`), await text("r-yaw"));
    await shot("10_tracking_target_locked.png");
    const ctl = await pagePost(`/api/tracks/${TRACK}/control`, { target_id: tid, select_k: k0 });
    const kp = ctl.frames.findIndex((r, i) => i > k0 && r.target_state !== "LOCKED");
    if (kp > 0) {
      await evaluate(`(() => { const s = document.getElementById('t-scrub'); s.value = ${kp}; s.dispatchEvent(new Event('input')); })()`);
      const want = ctl.frames[kp].target_state;
      check(TRACK_CHECKS[5], await waitFor(`document.getElementById('r-tstate').textContent === '${want}' && document.getElementById('r-stop').textContent.startsWith('STOP') && document.getElementById('r-yaw').textContent.startsWith('yaw 0')`), `${want}: ${await text("r-tstate")} · ${await text("r-stop")}`);
      await shot("11_tracking_target_not_locked.png");
    } else skip(TRACK_CHECKS[5], "target never leaves LOCKED");
    await key("0", "Digit0");
    check(TRACK_CHECKS[6], await waitFor(`document.getElementById('r-tstate').textContent === 'NO TARGET'`));
    await evaluate(`(() => { const s = document.getElementById('t-scrub'); s.value = ${k0}; s.dispatchEvent(new Event('input')); })()`);
    await sleep(300);
    const pt = await evaluate(`(() => { const c = document.getElementById('t-canvas').getBoundingClientRect(); const [W, H] = [${rec.image_wh[0]}, ${rec.image_wh[1]}]; const s = Math.min(c.width / W, c.height / H); const ox = (c.width - W * s) / 2, oy = (c.height - H * s) / 2; const b = ${JSON.stringify([...rec.frames[k0].tracks].sort((a, b) => a.id - b.id)[0].bbox)}; return [c.left + ox + (b[0] + b[2]) / 2 * s, c.top + oy + (b[1] + b[3]) / 2 * s]; })()`);
    for (const type of ["mousePressed", "mouseReleased"]) await send("Input.dispatchMouseEvent", { type, x: pt[0], y: pt[1], button: "left", clickCount: 1 });
    check(TRACK_CHECKS[7], await waitFor(`document.getElementById('r-tid').textContent === '#${tid}'`), await text("r-tid"));
    await evaluate(`document.getElementById('t-raw').click()`);
    await key(" "); await sleep(1200); await key(" ");
    check(TRACK_CHECKS[8], !(await text("t-count")).startsWith(`${k0 + 1} /`), await text("t-count"));
    await shot("12_tracking_playback_raw.png");
    await key("0", "Digit0");
  } else skip(TRACK_CHECKS, `${TRACK} not in outputs/`);

  // ambiguous target: recorded with the conservative tracker; the console must show AMBIGUOUS and STOP
  const AMB_CHECKS = ["run with ambiguous frames loads", "recorded run contains a LOCKED track that later turns AMBIGUOUS", "AMBIGUOUS target shows STOP · TARGET_AMBIGUOUS and yaw 0", "track list marks the AMBIGUOUS track"];
  if (stored("tracks", TRACK_AMB)) {
    await evaluate(`[...document.querySelectorAll('#t-runs button')].find(b => b.querySelector('.sid').textContent === '${TRACK_AMB}')?.click()`);
    check(AMB_CHECKS[0], await waitFor(`document.getElementById('t-empty').hidden && document.getElementById('t-count').textContent.startsWith('1 /')`, 15000));
    const recA = await (await fetch(`${BASE}/api/tracks/${TRACK_AMB}/files/tracks.json`)).json();
    let pick = null;
    for (const f of recA.frames) { for (const tr of f.tracks) { if (tr.state !== "LOCKED") continue; const later = recA.frames.find((g) => g.k > f.k && g.tracks.some((x) => x.id === tr.id && x.state === "AMBIGUOUS")); if (later) { pick = { k: f.k, id: tr.id, ka: later.k }; break; } } if (pick) break; }
    check(AMB_CHECKS[1], !!pick, JSON.stringify(pick));
    if (pick) {
      await evaluate(`(() => { const s = document.getElementById('t-scrub'); s.value = ${pick.k}; s.dispatchEvent(new Event('input')); })()`);
      await waitFor(`document.getElementById('t-count').textContent.startsWith('${pick.k + 1} /')`);
      await sleep(200);
      const rank = [...recA.frames[pick.k].tracks].sort((a, b) => a.id - b.id).findIndex((x) => x.id === pick.id);
      await evaluate(`document.querySelectorAll('#t-tracks button')[${rank}].click()`);
      await waitFor(`document.getElementById('r-tid').textContent === '#${pick.id}' && document.getElementById('r-tstate').textContent === 'LOCKED'`);
      await evaluate(`(() => { const s = document.getElementById('t-scrub'); s.value = ${pick.ka}; s.dispatchEvent(new Event('input')); })()`);
      check(AMB_CHECKS[2], await waitFor(`document.getElementById('r-tstate').textContent === 'AMBIGUOUS' && document.getElementById('r-stop').textContent.includes('TARGET_AMBIGUOUS') && document.getElementById('r-yaw').textContent.startsWith('yaw 0')`), `${await text("r-tstate")} · ${await text("r-stop")}`);
      check(AMB_CHECKS[3], await evaluate(`[...document.querySelectorAll('#t-tracks button')].some(b => b.textContent.includes('AMBIGUOUS'))`));
      await shot("16_tracking_ambiguous_stop.png");
      await key("0", "Digit0");
    } else skip(AMB_CHECKS.slice(2), "no LOCKED → AMBIGUOUS track");
  } else skip(AMB_CHECKS, `${TRACK_AMB} not in outputs/`);

  // run the real detector from the UI on the pedestrian clip, then cancel a longer run
  const vtest = inputs.find((i) => i.id.includes("vtest"));
  const TRUN_CHECKS = ["tracking run shows progress", "Build map disabled while a tracking run is active (one job at a time)", "tracking run finishes and auto-loads", "tracking cancel asks for confirmation", "cancelled tracking run ends as CANCELLED"];
  if (vtest) {
    await evaluate(`(() => { const s = document.getElementById('t-input'); s.value = ${JSON.stringify(vtest.id)}; s.dispatchEvent(new Event('change')); document.getElementById('t-max').value = 40; })()`);
    await evaluate(`document.getElementById('t-run').click()`);
    check(TRUN_CHECKS[0], await waitFor(`['RUNNING','STARTING','SUCCEEDED'].includes(document.getElementById('t-job-badge').textContent)`, 5000));
    check(TRUN_CHECKS[1], await waitFor(`document.getElementById('build').disabled || document.getElementById('t-job-badge').textContent === 'SUCCEEDED'`, 3000));
    check(TRUN_CHECKS[2], await waitFor(`document.getElementById('t-job-badge').textContent === 'SUCCEEDED' && document.getElementById('t-count').textContent.includes('/ 40')`, 60000), await text("t-job-msg"));
    await shot("13_tracking_run_succeeded.png");
    await evaluate(`document.getElementById('t-max').value = 3000; document.getElementById('t-run').click()`);
    await waitFor(`document.getElementById('t-job-badge').textContent === 'RUNNING'`, 10000);
    await evaluate(`document.getElementById('t-cancel').click()`);
    check(TRUN_CHECKS[3], await evaluate(`!document.getElementById('t-cancel-confirm').hidden`));
    await evaluate(`document.getElementById('t-cancel-yes').click()`);
    check(TRUN_CHECKS[4], await waitFor(`document.getElementById('t-job-badge').textContent === 'CANCELLED'`, 30000), await text("t-job-msg"));
    await shot("14_tracking_cancelled.png");
  } else skip(TRUN_CHECKS, "vtest input not present (data/public/opencv_vtest)");
  for (const [w, h] of [[1100, 800], [700, 900]]) {
    await B.viewport(w, h, 1);
    await sleep(400);
    check(`tracking at ${w}x${h}: no horizontal overflow`, await evaluate(`document.documentElement.scrollWidth <= innerWidth + 1`));
    await shot(`15_tracking_narrow_${w}.png`);
  }
  await B.viewport(1600, 1000, 1);
  await evaluate(`window.__beforeReload = 1`);  // gone once the new document has replaced the old one
  await send("Page.reload");
  check("reload keeps the tracking workspace (#tracking)", await waitFor(`!window.__beforeReload && document.readyState === 'complete' && document.getElementById('conn-badge')?.textContent === 'SERVER ONLINE' && !document.getElementById('ws-track').hidden`));
  await evaluate(`document.getElementById('mode-map').click()`);

  // 8b. live run: start, frame + boxes, pause/resume, select a target by clicking, stop, verified, replay
  await evaluate(`document.getElementById('mode-live').click()`);
  check("Live tab shows the live workspace", await waitFor(`!document.getElementById('ws-live').hidden && location.hash === '#live'`));
  const LIVE_CHECKS = ["live run shows processed frames", "live status bar shows fps, latency and drops", "map position is shown as not available (no 3D person, no map registration)", "pause reaches the worker (PAUSED)", "resume returns to LIVE", "clicking a LOCKED box selects it as the target", "stopped run finishes with verified files", "VO without a calibration file is marked UNCALIBRATED", "finished live run is listed for replay", "live run replays as OPEN_LOOP_REPLAY"];
  if (vtest) {
    await waitFor(`[...document.getElementById('l-input').options].some(o => o.value.includes('vtest'))`);
    await evaluate(`(() => { const s = document.getElementById('l-input'); s.value = [...s.options].find(o => o.value.includes('vtest')).value; document.getElementById('l-max').value = 14; })()`);
    await evaluate(`document.getElementById('l-start').click()`);
    check(LIVE_CHECKS[0], await waitFor(`document.getElementById('l-empty').hidden && document.getElementById('l-state').textContent === 'LIVE'`, 30000), await text("l-state"));
    check(LIVE_CHECKS[1], await waitFor(`/fps/.test(document.getElementById('lm-fps').textContent) && /ms/.test(document.getElementById('lm-lat').textContent) && / of /.test(document.getElementById('lm-drop').textContent)`, 5000), `${await text("lm-fps")} · ${await text("lm-lat")} · ${await text("lm-drop")}`);
    check(LIVE_CHECKS[2], (await text("l-vometa")).includes("NOT_AVAILABLE"));
    await evaluate(`document.getElementById('l-pause').click()`);
    check(LIVE_CHECKS[3], await waitFor(`document.getElementById('l-state').textContent === 'PAUSED'`, 5000), await text("l-state"));
    await evaluate(`document.getElementById('l-resume').click()`);
    check(LIVE_CHECKS[4], await waitFor(`document.getElementById('l-state').textContent === 'LIVE'`, 5000), await text("l-state"));
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
      } else { await evaluate(`document.getElementById('l-resume').click()`); await sleep(700); }
    }
    check(LIVE_CHECKS[5], picked != null && await waitFor(`document.getElementById('l-tid').textContent !== '' || document.getElementById('l-events').textContent.includes('SELECT')`, 8000), `picked #${picked} · ${await text("l-tid")} · ${await text("l-events")}`);
    await shot("17_live_run.png");
    await evaluate(`document.getElementById('l-resume').click()`);
    await sleep(800);
    await evaluate(`document.getElementById('l-stop').click()`);
    check(LIVE_CHECKS[6], await waitFor(`document.getElementById('l-state').textContent === 'SUCCEEDED' && document.getElementById('l-msg').textContent.includes('verified')`, 30000), await text("l-msg"));
    check(LIVE_CHECKS[7], await evaluate(`!document.getElementById('l-vocalib').hidden && document.getElementById('l-vocalib').textContent === 'UNCALIBRATED'`), await evaluate(`document.getElementById('l-vocalib').title`));
    await shot("18_live_finished.png");
    await evaluate(`document.getElementById('mode-track').click()`);
    await evaluate(`document.getElementById('t-refresh').click()`);
    check(LIVE_CHECKS[8], await waitFor(`[...document.querySelectorAll('#t-runs .meta')].some(m => m.textContent.startsWith('LIVE RUN'))`, 10000));
    await evaluate(`[...document.querySelectorAll('#t-runs button')].find(b => b.querySelector('.meta').textContent.startsWith('LIVE RUN')).click()`);
    check(LIVE_CHECKS[9], await waitFor(`document.getElementById('t-badges').textContent.includes('OPEN_LOOP_REPLAY')`, 15000), await text("t-badges"));
    await evaluate(`document.getElementById('mode-live').click()`);
  } else skip(LIVE_CHECKS, "vtest input not present (data/public/opencv_vtest)");

  // 8c. Live state badges driven by a scripted run (the page's fetch answers /api/runs/scripted_run/* and announces
  // it as the running run in /api/health); the worker itself is covered by tests/test_live_run.py
  await evaluate(`(() => {
    const real = window.fetch, S = window.__script = { on: true, t0: performance.now(), row: 1, mode: 'LIVE', state: 'RUNNING', report: null };
    window.__realFetch = real;
    const json = (o) => new Response(JSON.stringify(o), { status: 200, headers: { 'Content-Type': 'application/json' } });
    window.fetch = async (url, opts) => {
      const u = String(url);
      if (u.startsWith('/api/health')) { const h = await (await real(url, opts)).json(); if (S.on) h.running_run_job = 'scripted_run'; return json(h); }
      if (u.startsWith('/api/runs/scripted_run/vo')) return json({ segment: null, points: [] });
      if (u.startsWith('/api/runs/scripted_run/latest')) {
        if (S.mode !== 'FROZEN') S.row++;
        const row = { type: 'frame', row: S.row, run_t: S.row * 0.1, media_t: S.row * 0.1, latency_ms: 40, tracks: [], target_id: null, yaw: 0, stop_reasons: ['NO_TARGET'], vo_state: 'INITIALIZING', vo_segment: 0, events: [] };
        return json({ state: { state: S.state, status: { input_state: S.mode === 'FROZEN' ? 'LIVE' : S.mode, recent_fps: 10, latency_ms_p95_recent: 50, dropped_total: 0, captured_total: S.row, epoch: 0 }, report: S.report }, row, image_wh: [640, 480], labels: {}, server_time: Date.now() / 1000 });
      }
      return real(url, opts);
    };
  })()`);
  const lstate = () => text("l-state");
  check("Live: a running run shows LIVE", await waitFor(`document.getElementById('l-state').textContent === 'LIVE'`, 8000), await lstate());
  await evaluate(`window.__script.mode = 'FROZEN'`);  // rows stop arriving while the input says LIVE
  check("Live: no new processed frame for 0.5 s shows STALE_DATA", await waitFor(`document.getElementById('l-state').textContent === 'STALE_DATA'`, 5000), await lstate());
  await shot("19_live_stale_data.png");
  await evaluate(`window.__script.mode = 'STALLED'`);
  check("Live: a stalled input shows STALLED", await waitFor(`document.getElementById('l-state').textContent === 'STALLED'`, 5000), await lstate());
  for (const [st, report, want] of [["FAILED", { failure: { message: "scripted failure" } }, "FAILED"], ["INCOMPLETE", {}, "INCOMPLETE"], ["CANCELLED", {}, "CANCELLED"]]) {
    await evaluate(`Object.assign(window.__script, { on: true, mode: 'LIVE', state: 'RUNNING', report: null })`);
    await waitFor(`document.getElementById('l-state').textContent === 'LIVE'`, 8000);
    await evaluate(`Object.assign(window.__script, { on: false, state: '${st}', report: ${JSON.stringify(report)} })`);
    check(`Live: a run that ends ${st} shows ${want}`, await waitFor(`document.getElementById('l-state').textContent === '${want}'`, 8000), `${await lstate()} · ${await text("l-msg")}`);
  }
  await shot("20_live_cancelled.png");
  await evaluate(`window.fetch = window.__realFetch; window.__script.on = false`);
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
  const count = (s) => results.filter((r) => r.status === s).length;
  const summary = { browser: B.mode, date: new Date().toISOString(), port, results, consoleErrors, extensionErrors,
    pass: count("PASS"), fail: count("FAIL"), skip: count("SKIP") };
  writeFileSync(join(OUT, "ui_smoke.json"), JSON.stringify(summary, null, 1));
  console.log(`\n${summary.pass} passed, ${summary.fail} failed, ${summary.skip} skipped → ${join(OUT, "ui_smoke.json")}`);
  await B.close();
  server.kill();
  process.exit(summary.fail ? 1 : 0);
}
