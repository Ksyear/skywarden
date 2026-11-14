"""Record the Tello state stream to `telemetry.jsonl` (one parsed packet per line) for later use beside a map.

Receive-only: the recorder reads `TelloClient.latest_state()` and never sends anything, so it adds no path to the
vehicle. Each line keeps the client's monotonic receive time `t` (the clock the live run's timeline uses) plus the
fields that matter for a height cross-check (`h`, `tof` in cm, `baro` in m) plus attitude, speed and battery.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

KEEP = ("h", "tof", "baro", "yaw", "pitch", "roll", "vgx", "vgy", "vgz", "bat", "time")


class TelemetryRecorder:
    def __init__(self, client, path: Path, poll_s: float = 0.02, note: str = ""):
        self.client, self.path, self.poll_s = client, Path(path), poll_s
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_t: float | None = None
        self._lock = threading.Lock()  # a poll and stop() never interleave: file and samples always hold the same rows
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w")
        self._fh.write(json.dumps({"schema": "skywarden/telemetry@1", "clock": "time.monotonic of the receiving client",
                                   "units": {"h": "cm", "tof": "cm", "baro": "m"}, "simulated": bool(client.simulated),
                                   "note": note}) + "\n")

    def _poll(self) -> None:
        with self._lock:
            if self._fh.closed:
                return
            st = self.client.latest_state()
            if st is None or st.t == self._last_t:
                return
            self._last_t = st.t
            row = {"t": round(st.t, 4), **{k: st.data[k] for k in KEEP if k in st.data}}
            self._fh.write(json.dumps(row) + "\n")
            self.samples.append(row)

    def start(self) -> "TelemetryRecorder":
        def loop():
            while not self._stop.wait(self.poll_s):
                self._poll()
        self._thread = threading.Thread(target=loop, daemon=True, name="tello-telemetry")
        self._thread.start()
        return self

    def stop(self) -> list[dict]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        with self._lock:
            self._fh.close()
        return self.samples

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def read_telemetry(path: Path) -> list[dict]:
    lines = Path(path).read_text().splitlines()
    return [json.loads(ln) for ln in lines[1:] if ln.strip()]


def height_at(samples: list[dict], t: float, field: str = "h", max_gap_s: float = 0.25) -> float | None:
    """Telemetry height (metres) at time t, linear between the two packets around it; None if t is not bracketed by
    packets less than max_gap_s apart (no extrapolation)."""
    ts = [s["t"] for s in samples]
    for a, b in zip(samples, samples[1:]):
        if a["t"] <= t <= b["t"]:
            if b["t"] - a["t"] > max_gap_s:
                return None
            w = (t - a["t"]) / (b["t"] - a["t"]) if b["t"] > a["t"] else 0.0
            return ((1 - w) * a[field] + w * b[field]) / 100.0
    return None if not ts or t < ts[0] or t > ts[-1] else samples[-1][field] / 100.0
