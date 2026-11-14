// Browser connection for the UI scripts (smoke test, render benchmark, screenshots) over the DevTools protocol.
//
// Order: 1. --attach <url> if given, else the browser already listening on http://127.0.0.1:9222 — the script opens
//           ONE tab there and closes only that tab at the end; no browser process or window is started.
//        2. --browser <path> or the CHROME_PATH environment variable: a headless Chromium-family browser with a
//           temporary profile, started by the script and killed at the end.
//        3. Otherwise: exit with a message saying how to provide one. A browser is never started on its own.
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import net from "node:net";

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const freePort = () => new Promise((r) => { const s = net.createServer(); s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => r(p)); }); });
const arg = (name) => { const i = process.argv.indexOf(name); return i > 0 ? process.argv[i + 1] : null; };

async function newTab(base) {
  const t = await (await fetch(`${base}/json/new?about:blank`, { method: "PUT", signal: AbortSignal.timeout(3000) })).json();
  return { wsUrl: t.webSocketDebuggerUrl, tabId: t.id };
}

export async function connectBrowser({ width = 1600, height = 1000, dpr = 1 } = {}) {
  const attach = arg("--attach") ?? "http://127.0.0.1:9222", explicitAttach = process.argv.includes("--attach");
  const browserPath = arg("--browser") ?? process.env.CHROME_PATH ?? null;
  let wsUrl = null, tabId = null, proc = null, base = null, mode;
  const forceBrowser = process.argv.includes("--browser") && !explicitAttach;  // --browser skips the attach attempt
  if (!forceBrowser) {
    try { ({ wsUrl, tabId } = await newTab(attach)); base = attach; mode = `attached to the open browser at ${attach}`; }
    catch (e) { if (explicitAttach) throw new Error(`cannot attach to ${attach}: ${e.message}. Start the browser with --remote-debugging-port=9222.`); }
  }
  if (!wsUrl) {
    if (!browserPath) throw new Error(`no browser: nothing listens on ${attach} and CHROME_PATH is not set. Either start your browser with --remote-debugging-port=9222, or set CHROME_PATH to a Chromium-family browser binary (it is then started headless with a temporary profile).`);
    const port = await freePort(), profile = mkdtempSync(join(tmpdir(), "sda-ui-profile-"));
    proc = spawn(browserPath, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, "--no-first-run", "--no-default-browser-check", `--window-size=${width},${height}`, "about:blank"], { stdio: "ignore" });
    base = `http://127.0.0.1:${port}`; mode = `headless ${browserPath}`;
    for (let i = 0; i < 200 && !wsUrl; i++) {
      try { const t = await (await fetch(`${base}/json/list`)).json(); wsUrl = t.find((x) => x.type === "page")?.webSocketDebuggerUrl; } catch {}
      if (!wsUrl) await sleep(150);
    }
    if (!wsUrl) { proc.kill("SIGKILL"); throw new Error("the browser did not expose a DevTools page"); }
  }
  const ws = new WebSocket(wsUrl);
  await new Promise((r, j) => { ws.addEventListener("open", r); ws.addEventListener("error", j); });
  let seq = 0; const pending = new Map(), listeners = [];
  ws.addEventListener("message", (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
    if (msg.method) for (const f of listeners) f(msg);
  });
  const send = (method, params = {}) => new Promise((r) => { const id = ++seq; pending.set(id, r); ws.send(JSON.stringify({ id, method, params })); });
  const evaluate = async (expr) => {
    const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text);
    return r.result?.result?.value;
  };
  const waitFor = async (expr, ms = 15000) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { try { if (await evaluate(expr)) return true; } catch {} await sleep(150); } return false; };
  const shot = async (path, clip) => { const r = await send("Page.captureScreenshot", { format: "png", ...(clip ? { clip: { ...clip, scale: 1 } } : {}) }); writeFileSync(path, Buffer.from(r.result.data, "base64")); };
  const viewport = (w, h, scale = dpr) => send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: scale, mobile: false });
  await send("Page.enable"); await send("Runtime.enable");
  // An attached tab in an existing window can be a background tab, and Chromium throttles background timers; keep it
  // in front for the duration of the script. It is closed at the end.
  if (tabId) await send("Page.bringToFront");
  await viewport(width, height);
  const close = async () => {
    try { ws.close(); } catch {}
    if (tabId) { try { await fetch(`${base}/json/close/${tabId}`); } catch {} }  // close only our tab
    if (proc) proc.kill("SIGKILL");
  };
  return { send, evaluate, waitFor, shot, viewport, close, mode, onEvent: (f) => listeners.push(f) };
}
