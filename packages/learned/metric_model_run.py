"""Runs one metric-depth model on a prepared frame set, chunk by chunk, inside that model's own environment.

The model is an external estimator: this script only feeds it frames (already resized to a grid whose sides are
multiples of 14, with the matching pinhole K) and stores what it says. Nothing here writes to a map.
Inputs come from `learned.metric_model_trial prepare` (job.json); outputs per frame:
  depth_<stem>.npy  z-depth along the optical axis, in the model's own units (metres if the model is metric)
  conf_<stem>.npy   the model's own confidence (scale differs per model)
  mask_<stem>.npy   the model's own recommended validity mask (uint8)
and run_report.json (model, weights hash, device, per-chunk time and memory, predicted camera-to-world poses).

Supported models: mapanything (optional intrinsics / our poses as input), pi3x, da3metric (monocular),
da3nested (multi-view, metric). Run with the model environment's python, e.g.
  outputs/tools/venv_mapanything/bin/python packages/learned/metric_model_run.py <job.json>
"""
from __future__ import annotations

import hashlib
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

MIN_FREE_PCT = 35      # a chunk starts only when macOS reports at least this much free memory (A1 protocol)
FREE_WAIT_S = 600


def free_pct() -> int | None:
    try:
        return int(subprocess.run(["sysctl", "-n", "kern.memorystatus_level"], capture_output=True, text=True).stdout.strip())
    except Exception:
        return None


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 24), b""):
            h.update(b)
    return h.hexdigest()


def load_rgb(path: str) -> np.ndarray:
    import cv2
    return cv2.cvtColor(cv2.imread(path), cv2.COLOR_BGR2RGB)


def _mps_mem(torch):
    if torch.backends.mps.is_available():
        return {"mps_current_gb": round(torch.mps.current_allocated_memory() / 2**30, 3),
                "mps_driver_gb": round(torch.mps.driver_allocated_memory() / 2**30, 3)}
    return {}


class MapAnythingRunner:
    def __init__(self, job, dev, torch):
        import mapanything.utils.inference as mi
        from mapanything.models import MapAnything
        self.torch, self.dev, self.job = torch, dev, job
        # the postprocess recovers pinhole K from the predicted rays with a least-squares bmm whose inner dimension is
        # every pixel; on MPS that one call took minutes per frame (sampled), on CPU it is instant -> run it on CPU
        orig = mi.recover_pinhole_intrinsics_from_ray_directions

        def k_on_cpu(ray_directions, *a, **kw):
            r = orig(ray_directions.detach().float().cpu(), *a, **kw)
            return r.to(ray_directions.device) if torch.is_tensor(r) else r
        mi.recover_pinhole_intrinsics_from_ray_directions = k_on_cpu
        self.model = MapAnything.from_pretrained(job["weights"]).to(dev).eval()

    def run(self, frames):
        torch = self.torch
        from mapanything.utils.image import preprocess_inputs
        W, H = self.job["grid"]
        cond = self.job["condition"]
        views = []
        for f in frames:
            v = {"img": torch.from_numpy(load_rgb(f["path"]))}
            if "intrinsics" in cond:
                v["intrinsics"] = torch.tensor(f["K"], dtype=torch.float32)
            if "poses" in cond:
                v["camera_poses"] = torch.tensor(f["c2w"], dtype=torch.float32)
                v["is_metric_scale"] = torch.tensor([False])
            views.append(v)
        views = preprocess_inputs(views, resize_mode="fixed_size", size=(W, H))
        amp = self.job.get("dtype", "fp32")
        preds = self.model.infer(views, memory_efficient_inference=True, use_amp=amp != "fp32", amp_dtype=amp if amp != "fp32" else "bf16",
                                 apply_mask=True, mask_edges=True)
        out = []
        for p in preds:
            out.append({"depth": p["depth_z"][0, ..., 0].float().cpu().numpy(), "conf": p["conf"][0].float().cpu().numpy(),
                        "mask": p["mask"][0, ..., 0].cpu().numpy().astype(np.uint8),
                        "c2w": p["camera_poses"][0].float().cpu().numpy(),
                        "K_pred": p["intrinsics"][0].float().cpu().numpy(),
                        "metric_scaling_factor": float(p["metric_scaling_factor"].reshape(-1)[0]) if "metric_scaling_factor" in p else None})
        return out


class Pi3XRunner:
    def __init__(self, job, dev, torch):
        from safetensors.torch import load_file
        from pi3.models.pi3x import Pi3X
        self.torch, self.dev, self.job = torch, dev, job
        cond = job["condition"] != "none"
        m = Pi3X(use_multimodal=cond).eval()
        missing = m.load_state_dict(load_file(str(Path(job["weights"]) / "model.safetensors")), strict=False)
        self.load_note = {"missing_keys": len(missing.missing_keys), "unexpected_keys": len(missing.unexpected_keys)}
        if not cond:
            m.disable_multimodal()
        self.model = m.to(dev)

    def run(self, frames):
        torch = self.torch
        from pi3.utils.geometry import depth_normal_edge
        imgs = np.stack([load_rgb(f["path"]) for f in frames]).astype(np.float32) / 255.0
        x = torch.from_numpy(imgs).permute(0, 3, 1, 2)[None].to(self.dev)
        kw = {}
        if "intrinsics" in self.job["condition"]:
            kw["intrinsics"] = torch.tensor(np.stack([f["K"] for f in frames]), dtype=torch.float32)[None].to(self.dev)
        if "poses" in self.job["condition"]:
            kw["poses"] = torch.tensor(np.stack([f["c2w"] for f in frames]), dtype=torch.float32)[None].to(self.dev)
        amp = self.job.get("dtype", "fp32")
        with torch.no_grad(), torch.autocast(device_type=self.dev.type, dtype=torch.bfloat16, enabled=amp == "bf16"):
            res = self.model(imgs=x, **kw)
        lp = res["local_points"].float()
        conf = torch.sigmoid(res["conf"][..., 0].float())
        mask = (conf > 0.1) & ~depth_normal_edge(lp, rtol=0.03, mask=conf > 0.1)  # the model's own example rule
        out = []
        for i in range(len(frames)):
            out.append({"depth": lp[0, i, ..., 2].cpu().numpy(), "conf": conf[0, i].cpu().numpy(), "mask": mask[0, i].cpu().numpy().astype(np.uint8),
                        "c2w": res["camera_poses"][0, i].float().cpu().numpy(), "metric_scaling_factor": float(res["metric"].reshape(-1)[0])})
        return out


class DA3Runner:
    def __init__(self, job, dev, torch):
        import types
        # pycolmap is only used by the package's COLMAP exporter; its bundled OpenMP runtime clashes with torch's
        sys.modules.setdefault("pycolmap", types.ModuleType("pycolmap"))
        from depth_anything_3.api import DepthAnything3
        self.torch, self.dev, self.job = torch, dev, job

        def fwd(self_, image, extrinsics=None, intrinsics=None, export_feat_layers=None, infer_gs=False, use_ray_pose=False, ref_view_strategy="saddle_balanced"):
            with torch.no_grad():  # fp32 on MPS (the default forward enables CUDA autocast)
                return self_.model(image, extrinsics, intrinsics, export_feat_layers, infer_gs, use_ray_pose, ref_view_strategy)
        DepthAnything3.forward = fwd
        self.model = DepthAnything3.from_pretrained(job["weights"]).to(dev).eval()

    def run(self, frames):
        W, H = self.job["grid"]
        imgs = [load_rgb(f["path"]) for f in frames]
        kw = {}
        if "poses" in self.job["condition"]:
            kw = {"extrinsics": np.stack([np.linalg.inv(np.array(f["c2w"])) for f in frames]).astype(np.float32),
                  "intrinsics": np.stack([f["K"] for f in frames]).astype(np.float32)}
        pred = self.model.inference(imgs, process_res=max(W, H), process_res_method="upper_bound_resize", **kw)
        depth = np.asarray(pred.depth, np.float32)
        if depth.shape[1:] != (H, W):
            raise RuntimeError(f"model returned depth {depth.shape[1:]} for grid {(H, W)}")
        conf = np.asarray(pred.conf, np.float32) if getattr(pred, "conf", None) is not None else np.ones_like(depth)
        sky = np.asarray(pred.sky) if getattr(pred, "sky", None) is not None else None
        out = []
        for i, f in enumerate(frames):
            d = depth[i]
            if self.job["model"] == "da3metric":  # README: metric_depth = focal * net_output / 300, focal in pixels at this resolution
                K = np.array(f["K"]); d = d * 0.5 * (K[0, 0] + K[1, 1]) / 300.0
            m = np.isfinite(d) & (d > 0)
            if sky is not None:
                m &= ~(sky[i] > 0.5) if sky[i].dtype != bool else ~sky[i]
            if conf is not None and self.job["model"] != "da3metric":
                m &= conf[i] >= np.percentile(conf[i], 40)  # the exporter's default confidence cut (40th percentile)
            ext = getattr(pred, "extrinsics", None)
            c2w = None
            if ext is not None:
                E = np.eye(4); E[:3, :4] = np.asarray(ext[i])[:3, :4]; c2w = np.linalg.inv(E)
            out.append({"depth": d, "conf": conf[i], "mask": m.astype(np.uint8), "c2w": c2w})
        return out


RUNNERS = {"mapanything": MapAnythingRunner, "pi3x": Pi3XRunner, "da3metric": DA3Runner, "da3nested": DA3Runner}


def main(argv=None) -> int:
    import faulthandler
    import signal
    faulthandler.register(signal.SIGUSR1)  # `kill -USR1 <pid>` prints where a slow run is
    job = json.loads(Path((argv or sys.argv[1:])[0]).read_text())
    out = Path(job["out"]); out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    import torch
    dev = torch.device(job.get("device", "mps"))
    rep = {"model": job["model"], "condition": job["condition"], "weights": job["weights"], "weights_rev": job.get("weights_rev"),
           "code_commit": job.get("code_commit"), "device": str(dev), "torch": torch.__version__, "dtype": job.get("dtype", "fp32"),
           "grid": job["grid"], "chunk": job["chunk"], "frames": len(job["frames"]), "chunks": [], "poses_c2w": {}, "status": "RUNNING"}
    wf = sorted(Path(job["weights"]).glob("*.safetensors"))
    rep["weights_sha256"] = {p.name: sha256(p) for p in wf}
    t0 = time.time()
    runner = RUNNERS[job["model"]](job, dev, torch)
    rep["seconds_load"] = round(time.time() - t0, 2); rep["load_note"] = getattr(runner, "load_note", None)
    rep["memory_after_load"] = {"free_pct": free_pct(), **_mps_mem(torch)}
    fr = job["frames"]; n = job["chunk"]
    for c0 in range(0, len(fr), n):
        part = fr[c0:c0 + n]
        waited = 0
        while (free_pct() or 100) < MIN_FREE_PCT and waited < FREE_WAIT_S:
            time.sleep(10); waited += 10
        row = {"first": part[0]["name"], "last": part[-1]["name"], "frames": len(part), "free_pct_start": free_pct(), "waited_s": waited}
        t1 = time.time()
        try:
            res = runner.run(part)
            if dev.type == "mps":
                torch.mps.synchronize()
        except Exception as e:  # recorded with the original message; the next chunk still runs
            row |= {"status": "ERROR", "error": f"{type(e).__name__}: {e}"[:2000], "seconds": round(time.time() - t1, 2)}
            rep["chunks"].append(row); print(json.dumps(row), flush=True)
            if dev.type == "mps":
                torch.mps.empty_cache()
            continue
        row |= {"status": "DONE", "seconds": round(time.time() - t1, 2), "s_per_frame": round((time.time() - t1) / len(part), 3),
                "peak_rss_gb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**30, 3), **_mps_mem(torch), "free_pct_end": free_pct()}
        msf = [r.get("metric_scaling_factor") for r in res if r.get("metric_scaling_factor") is not None]
        if msf:
            row["metric_scaling_factor"] = msf[0]
        for f, r in zip(part, res):
            st = Path(f["name"]).stem
            np.save(out / f"depth_{st}.npy", r["depth"].astype(np.float32)); np.save(out / f"conf_{st}.npy", r["conf"].astype(np.float32))
            np.save(out / f"mask_{st}.npy", r["mask"].astype(np.uint8))
            if r.get("c2w") is not None:
                rep["poses_c2w"][f["name"]] = {"chunk": len(rep["chunks"]), "T": np.asarray(r["c2w"], float).tolist()}
            if r.get("K_pred") is not None:
                rep.setdefault("K_pred", {})[f["name"]] = np.asarray(r["K_pred"], float).tolist()
        rep["chunks"].append(row); print(json.dumps(row), flush=True)
        del res
        if dev.type == "mps":
            torch.mps.empty_cache()
        (out / "run_report.json").write_text(json.dumps(rep, indent=1))
    done = [c for c in rep["chunks"] if c["status"] == "DONE"]
    rep["status"] = "DONE" if len(done) == len(rep["chunks"]) else ("PARTIAL" if done else "ERROR")
    rep["seconds_total"] = round(time.time() - t0, 2)
    rep["s_per_frame_median"] = float(np.median([c["s_per_frame"] for c in done])) if done else None
    rep["peak_rss_gb"] = max((c["peak_rss_gb"] for c in done), default=None)
    rep["peak_mps_driver_gb"] = max((c.get("mps_driver_gb", 0) for c in done), default=None)
    (out / "run_report.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps({k: rep[k] for k in ("model", "condition", "status", "seconds_total", "s_per_frame_median", "peak_rss_gb", "peak_mps_driver_gb")}))
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main())
