// Map viewer frame time, before and after the WebGL renderer, on the same layers and the same canvas.
// Usage: node tests/ui/render_bench.mjs [--attach http://127.0.0.1:9222 | --browser <path>] [--rev 2677744] [--frames 40]
//   before = apps/desktop/static/viewer.js at --rev (the last 2D-only viewer), after = the working-tree viewer.js
//   (WebGL; LOD off = every point, LOD idle / moving as in the app; plus its 2D fallback).
// Serves the two viewer modules, a bench page and the layer files itself (read-only, no console server); the browser
// connection follows tests/ui/cdp.mjs. Each frame is a synchronous redraw while the view turns by 0.02 rad; WebGL
// frames include a 1-pixel read-back, so they count the GPU work, not only the time to queue it.
// Writes outputs/evidence/portfolio/render_bench.json.
import http from "node:http";
import { execFileSync } from "node:child_process";
import { readFileSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { join, resolve, normalize } from "node:path";
import { connectBrowser, freePort } from "./cdp.mjs";

const ROOT = resolve(import.meta.dirname, "../..");
const OUT = join(ROOT, "outputs/evidence/portfolio");
const arg = (n, d) => { const i = process.argv.indexOf(n); return i > 0 ? process.argv[i + 1] : d; };
const REV = arg("--rev", "2677744"), FRAMES = Number(arg("--frames", 40));
const LAYERS = [["mqg_fsc_campus_aerial", "dense_onpath"], ["mqg_sumida_0025_0125", "dense_onpath"]];
const CSS = [680, 760];  // about the map panel of the console at 1600x1000
mkdirSync(OUT, { recursive: true });

const oldViewer = execFileSync("git", ["-C", ROOT, "show", `${REV}:apps/desktop/static/viewer.js`]);
const newViewer = readFileSync(join(ROOT, "apps/desktop/static/viewer.js"));
const mapFile = (rel) => {
  const p = normalize(rel); if (p.startsWith("..") || !/\.(ply|json)$/.test(p)) return null;
  return [join(ROOT, "outputs/maps", p), join(ROOT, "outputs/archive/maps", p)].find((f) => existsSync(f)) ?? null;
};
const PAGE = `<!doctype html><meta charset="utf-8"><title>render bench</title>
<style>body{margin:0;background:#0f1216} .stage{position:relative;width:${CSS[0]}px;height:${CSS[1]}px} .stage canvas{position:absolute;inset:0;width:100%;height:100%}
.ov{position:absolute;right:12px;bottom:12px;width:240px;height:180px}</style><div id="host"></div>`;

const port = await freePort();
const srv = http.createServer((req, res) => {
  const u = new URL(req.url, "http://x");
  const send = (body, type) => { res.writeHead(200, { "Content-Type": type, "Cache-Control": "no-store" }); res.end(body); };
  if (u.pathname === "/") return send(PAGE, "text/html");
  if (u.pathname === "/old/viewer.js") return send(oldViewer, "text/javascript");
  if (u.pathname === "/new/viewer.js") return send(newViewer, "text/javascript");
  if (u.pathname.startsWith("/maps/")) { const f = mapFile(decodeURIComponent(u.pathname.slice(6))); if (f) return send(readFileSync(f), "application/octet-stream"); }
  res.writeHead(404); res.end();
}).listen(port, "127.0.0.1");

let B;
try { B = await connectBrowser({ width: 1600, height: 1000 }); }
catch (e) { console.error(e.message); srv.close(); process.exit(2); }

const runs = [];
try {
  for (const dpr of [1, 2]) {
    await B.viewport(1600, 1000, dpr);
    await B.send("Page.navigate", { url: `http://127.0.0.1:${port}/` });
    await B.waitFor(`document.readyState === 'complete' && !!document.getElementById('host')`);
    for (const [sid, layer] of LAYERS) {
      if (!mapFile(`${sid}/${layer}/dense_verified.ply`)) { runs.push({ session: sid, layer, dpr, status: "NOT_RUN", reason: "layer not in outputs/" }); continue; }
      const r = await B.evaluate(`(async () => {
        const O = await import('/old/viewer.js'), N = await import('/new/viewer.js?' + Date.now());
        const traj = await (await fetch('/maps/${sid}/trajectory.json')).json();
        const buf = await (await fetch('/maps/${sid}/${layer}/dense_verified.ply')).arrayBuffer();
        const poses = traj.poses.map((p) => ({ ...p, R: p.T_world_camera.slice(0, 3).map((r) => r.slice(0, 3)), C: p.T_world_camera.slice(0, 3).map((r) => r[3]) }));
        const host = document.getElementById('host');
        const stage = () => { host.replaceChildren(); const s = document.createElement('div'); s.className = 'stage'; host.append(s);
          const mk = (cls) => { const c = document.createElement('canvas'); if (cls) c.className = cls; s.append(c); return c; };
          return { gl: mk(), cv: mk(), ov: mk('ov') }; };
        const stats = (t) => { t.sort((a, b) => a - b); const q = (p) => t[Math.min(t.length - 1, Math.floor(p * t.length))]; return { p50_ms: +q(0.5).toFixed(2), p95_ms: +q(0.95).toFixed(2) }; };
        const out = {};
        { // before: the 2D-only viewer, timed the way its status bar timed it (the whole _draw, overview included)
          const { cv, ov } = stage(), cloud = O.parsePly(buf.slice(0)), v = new O.PointViewer(cv, {});
          v.setData(cloud, poses, { frameOnCloud: true }); v.setOverlays({ path: false, cameras: false }); v.bindOverview(ov);
          v._draw(); const t = [];
          for (let i = 0; i < ${FRAMES}; i++) { v.view.yaw += 0.02; const t0 = performance.now(); v._draw(); t.push(performance.now() - t0); }
          out.before_2d = { renderer: '2d (rev ${REV})', points_in_file: cloud.count, points_drawn: v.lastDrawn, ...stats(t), canvas_px: [cv.width, cv.height] };
        }
        let gpu = null;
        { const { gl, cv, ov } = stage(), cloud = N.parsePly(buf.slice(0)), v = new N.PointViewer(cv, { glCanvas: gl });
          v.setData(cloud, poses, { frameOnCloud: true }); v.setOverlays({ path: false, cameras: false }); v.bindOverview(ov);
          const g = v.gl, ext = g && g.getExtension('WEBGL_debug_renderer_info'); gpu = g ? (ext ? g.getParameter(ext.UNMASKED_RENDERER_WEBGL) : g.getParameter(g.RENDERER)) : null;
          out.after_webgl_full = v.benchmark({ frames: ${FRAMES}, full: true });
          out.after_webgl_idle = v.benchmark({ frames: ${FRAMES} });
          out.after_webgl_moving = v.benchmark({ frames: ${FRAMES}, moving: true });
          g?.getExtension('WEBGL_lose_context')?.loseContext();
        }
        { const { cv, ov } = stage(), cloud = N.parsePly(buf.slice(0)), v = new N.PointViewer(cv, { renderer: '2d' });
          v.setData(cloud, poses, { frameOnCloud: true }); v.setOverlays({ path: false, cameras: false }); v.bindOverview(ov);
          out.after_2d_fallback_full = v.benchmark({ frames: ${FRAMES}, full: true });
          out.after_2d_fallback_idle = v.benchmark({ frames: ${FRAMES} });
          out.after_2d_fallback_moving = v.benchmark({ frames: ${FRAMES}, moving: true });
        }
        host.replaceChildren();
        return { gpu, ua: navigator.userAgent, out };
      })()`);
      runs.push({ session: sid, layer, dpr, css_px: CSS, frames: FRAMES, gpu: r.gpu, user_agent: r.ua, ...r.out });
      const o = r.out, row = (k) => `${k.padEnd(24)} ${String(o[k].points_drawn).padStart(9)} drawn  p50 ${String(o[k].p50_ms).padStart(7)} ms  p95 ${String(o[k].p95_ms).padStart(7)} ms`;
      console.log(`\n${sid}/${layer} · ${o.before_2d.points_in_file.toLocaleString()} pts · dpr ${dpr} · ${CSS.join("x")} css px · ${r.gpu}`);
      for (const k of Object.keys(o)) console.log(row(k));
    }
  }
} catch (e) { console.error("bench failed:", e.message); runs.push({ status: "FAILED", error: String(e) }); }
const summary = { date: new Date().toISOString(), browser: B.mode, rev_before: REV, method: "synchronous redraws while the view turns 0.02 rad per frame; WebGL frames end with a 1-pixel readPixels so GPU work is included; the before viewer is timed over its whole _draw (overview included), as its status bar did", runs };
writeFileSync(join(OUT, "render_bench.json"), JSON.stringify(summary, null, 1));
console.log(`\n→ ${join(OUT, "render_bench.json")}`);
await B.close(); srv.close();
process.exit(runs.some((r) => r.status === "FAILED") ? 1 : 0);
