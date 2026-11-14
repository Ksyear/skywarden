// Clean screenshots of the console on real stored sessions (for the portfolio), plus the render times the app itself
// reported while each map was on screen.
// Usage: node tests/ui/portfolio_shots.mjs [--attach http://127.0.0.1:9222 | --browser <path>] [--dpr 2]
// Starts its own console server on a free port: stored sessions are linked into a temporary maps root and nothing is
// written to them (Measure picks two points but saves nothing); the live run writes into a temporary runs root.
// Writes outputs/evidence/portfolio/screens/*.png and screens.json.
import { spawn } from "node:child_process";
import { mkdtempSync, mkdirSync, symlinkSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { connectBrowser, freePort, sleep } from "./cdp.mjs";

const ROOT = resolve(import.meta.dirname, "../..");
const OUT = join(ROOT, "outputs/evidence/portfolio/screens");
const DPR = Number(process.argv.includes("--dpr") ? process.argv[process.argv.indexOf("--dpr") + 1] : 2);
const FSC = "mqg_fsc_campus_aerial", SUMIDA = "mqg_sumida_0025_0125", TENNIS = "yt_tennis_complex", TRACK = "tum_walking_xyz_nano_fixv3";
mkdirSync(OUT, { recursive: true });
const stored = (kind, name) => [join(ROOT, "outputs", kind, name), join(ROOT, "outputs/archive", kind, name)].find((p) => existsSync(p));

const mapsRoot = mkdtempSync(join(tmpdir(), "sda-shots-maps-")), tracksRoot = mkdtempSync(join(tmpdir(), "sda-shots-tracks-")), runsRoot = mkdtempSync(join(tmpdir(), "sda-shots-runs-"));
for (const s of [FSC, SUMIDA, TENNIS]) if (stored("maps", s)) symlinkSync(stored("maps", s), join(mapsRoot, s));
if (stored("tracks", TRACK)) symlinkSync(stored("tracks", TRACK), join(tracksRoot, TRACK));
const port = await freePort(), BASE = `http://127.0.0.1:${port}`;
const server = spawn(join(ROOT, ".venv/bin/python"), [join(ROOT, "apps/desktop/server.py"), "--port", String(port), "--maps-root", mapsRoot, "--tracks-root", tracksRoot, "--runs-root", runsRoot], { stdio: "ignore" });
for (let i = 0; i < 50; i++) { try { if ((await fetch(`${BASE}/api/health`)).ok) break; } catch {} await sleep(100); }

let B;
try { B = await connectBrowser({ width: 1600, height: 1000, dpr: DPR }); }
catch (e) { console.error(e.message); server.kill(); process.exit(2); }
const { evaluate, waitFor, send } = B;
const shots = [];
const text = (id) => evaluate(`document.getElementById(${JSON.stringify(id)}).textContent`);
const shot = async (name, note) => {
  await sleep(1300);  // let the status bar refresh its render times
  await B.shot(join(OUT, name));
  shots.push({ file: `outputs/evidence/portfolio/screens/${name}`, note, session: await text("session-label"), render: await text("m-render"), badges: await text("badges") });
  console.log(`${name}  ·  ${shots.at(-1).render}`);
};
const open = async (sid, layer = "sparse") => {
  await evaluate(`document.getElementById('mode-map').click()`);
  await evaluate(`(() => { document.getElementById('layer').value = '${layer}'; [...document.querySelectorAll('#sessions button')].find(b => b.querySelector('.sid').textContent === '${sid}')?.click(); })()`);
  return waitFor(`document.getElementById('session-label').textContent === '${sid}' && document.getElementById('map-empty').hidden && document.getElementById('layer').value === '${layer}'`, 120000);
};
// the view from just behind the camera at the current frame (what Follow shows during playback), left as a free view
const cameraView = () => evaluate(`(() => { const v = window.__skywarden.viewer; v.setFollow(true); v._draw({ silent: true }); v.freeze(); })()`);
const scrubTo = (frac) => evaluate(`(() => { const s = document.getElementById('scrub'); s.value = Math.round(${frac} * s.max); s.dispatchEvent(new Event('input')); })()`);

await send("Page.navigate", { url: BASE + "/" });
try {
  await waitFor(`document.getElementById('conn-badge').textContent === 'SERVER ONLINE' && document.querySelectorAll('#sessions button').length > 0`);
  if (await open(FSC, "dense_onpath")) {
    await scrubTo(0.45); await evaluate(`document.getElementById('reset-view').click()`);
    await shot("01_fsc_aerial_model_output.png", "FSC campus, aerial: model output (steady) on our SfM camera path, default drone-like view");
    await evaluate(`document.getElementById('top-view').click()`);
    await shot("02_fsc_aerial_top.png", "FSC campus, aerial, top view");
    await evaluate(`document.getElementById('reset-view').click()`);
  }
  if (await open(SUMIDA, "dense_onpath")) {
    await scrubTo(0.3); await sleep(500); await cameraView();
    await shot("03_sumida_model_output.png", "Sumida riverside walk: model output (steady), 1.37 M points, seen from just behind the camera");
    await open(SUMIDA, "dense");
    await scrubTo(0.3); await sleep(500); await cameraView();
    await shot("04_sumida_dense_verified.png", "Sumida: multi-view verified dense layer (the default map layer), same frame");
  }
  if (await open(TENNIS, "sparse")) {
    await evaluate(`(() => { for (const id of ['show-cams', 'show-path']) { const b = document.getElementById(id); if (b.getAttribute('aria-pressed') === 'true') b.click(); } document.getElementById('top-view').click(); })()`);
    await shot("05_tennis_scale_status.png", "Tennis complex (aerial): sparse map with its metric-scale status (measured from court dimensions, not yet verified)");
    await evaluate(`document.getElementById('measure').click()`); await sleep(400);
    const spots = await evaluate(`(() => { const c = document.getElementById('map-canvas'); return window.__skywarden.viewer.sampleScreenPoints(40).filter(([x, y]) => document.elementFromPoint(x, y) === c); })()`);
    if (spots.length >= 10) {
      // a long span: the two drawn sample points farthest apart on screen
      let pair = [spots[0], spots[1]], best = 0;
      for (const a of spots) for (const b of spots) { const d = Math.hypot(a[0] - b[0], a[1] - b[1]); if (d > best) { best = d; pair = [a, b]; } }
      for (const p of pair) {
        for (const type of ["mousePressed", "mouseReleased"]) await send("Input.dispatchMouseEvent", { type, x: p[0], y: p[1], button: "left", clickCount: 1 });
        await sleep(200);
      }
      await shot("06_tennis_measure.png", "Measure in use: two picked points, distance in map units and metres labelled unverified");
    }
    await evaluate(`document.getElementById('measure').click()`);
  }
  if (stored("tracks", TRACK)) {
    await evaluate(`document.getElementById('mode-track').click()`);
    await waitFor(`[...document.querySelectorAll('#t-runs button')].some(b => b.querySelector('.sid').textContent === '${TRACK}')`);
    await evaluate(`[...document.querySelectorAll('#t-runs button')].find(b => b.querySelector('.sid').textContent === '${TRACK}').click()`);
    await waitFor(`document.getElementById('t-empty').hidden`);
    const rec = await (await fetch(`${BASE}/api/tracks/${TRACK}/files/tracks.json`)).json();
    // the frame and track that stay LOCKED for the longest run afterwards (a representative target)
    let k0 = 0, tid = null, bestRun = -1;
    rec.frames.forEach((f, k) => { for (const t of f.tracks) { if (t.state !== "LOCKED") continue; let r = 0; while (rec.frames[k + r + 1]?.tracks.some((x) => x.id === t.id && x.state === "LOCKED")) r++; if (r > bestRun) { bestRun = r; k0 = k; tid = t.id; } } });
    k0 = Math.min(rec.frames.length - 1, k0 + Math.floor(bestRun / 2));
    await evaluate(`(() => { const s = document.getElementById('t-scrub'); s.value = ${k0}; s.dispatchEvent(new Event('input')); })()`);
    await sleep(400);
    await evaluate(`[...document.querySelectorAll('#t-tracks button')].find(b => b.textContent.includes('#${tid}'))?.click()`);
    await waitFor(`document.getElementById('r-tstate').textContent === 'LOCKED'`, 5000);
    await shot("07_tracking_target.png", "Tracking: recorded run, chosen target LOCKED, MOCK yaw command (never sent)");
  }
  const inputs = await (await fetch(`${BASE}/api/inputs`)).json(), vtest = inputs.find((i) => i.id.includes("vtest"));
  if (vtest) {
    await evaluate(`document.getElementById('mode-live').click()`);
    await waitFor(`[...document.getElementById('l-input').options].some(o => o.value === ${JSON.stringify(vtest.id)})`);
    await evaluate(`(() => { document.getElementById('l-input').value = ${JSON.stringify(vtest.id)}; document.getElementById('l-max').value = 20; document.getElementById('l-start').click(); })()`);
    await waitFor(`document.getElementById('l-state').textContent === 'LIVE'`, 30000);
    await sleep(3000);
    // choose a confirmed person as the target (paused, so the boxes on screen are the ones read from the API)
    for (let i = 0; i < 10; i++) {
      await evaluate(`document.getElementById('l-pause').click()`); await waitFor(`document.getElementById('l-state').textContent === 'PAUSED'`, 5000); await sleep(500);
      const runs = await (await fetch(`${BASE}/api/runs`)).json(), lt = await (await fetch(`${BASE}/api/runs/${runs[0].id}/latest`)).json();
      const tr = lt.row?.tracks?.filter((t) => t.state === "LOCKED").sort((a, b) => (b.bbox[3] - b.bbox[1]) - (a.bbox[3] - a.bbox[1]))[0];
      if (tr) {
        const pt = await evaluate(`(() => { const c = document.getElementById('l-canvas').getBoundingClientRect(), [W, H] = ${JSON.stringify(lt.image_wh)}, s = Math.min(c.width / W, c.height / H), b = ${JSON.stringify(tr.bbox)}; return [c.left + (c.width - W * s) / 2 + (b[0] + b[2]) / 2 * s, c.top + (c.height - H * s) / 2 + (b[1] + b[3]) / 2 * s]; })()`);
        for (const type of ["mousePressed", "mouseReleased"]) await send("Input.dispatchMouseEvent", { type, x: pt[0], y: pt[1], button: "left", clickCount: 1 });
        await evaluate(`document.getElementById('l-resume').click()`); break;
      }
      await evaluate(`document.getElementById('l-resume').click()`); await sleep(800);
    }
    await sleep(2500);
    await shot("08_live_run.png", "Live: video at its original time base through detection, tracking, VO and MOCK control");
    await evaluate(`document.getElementById('l-stop').click()`);
    await waitFor(`['SUCCEEDED','CANCELLED','INCOMPLETE'].includes(document.getElementById('l-state').textContent)`, 30000);
  }
} catch (e) { console.error("screenshots failed:", e.message); }
writeFileSync(join(ROOT, "outputs/evidence/portfolio/screens.json"), JSON.stringify({ date: new Date().toISOString(), browser: B.mode, dpr: DPR, viewport: [1600, 1000], shots }, null, 1));
await B.close(); server.kill();
