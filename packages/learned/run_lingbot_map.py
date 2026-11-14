"""Run LingBot-Map (Apache-2.0, external, installed in outputs/tools/venv_learned) on a session's frames.

Executed with the separate learned-model interpreter, never the project .venv:
  outputs/tools/venv_learned/bin/python packages/learned/run_lingbot_map.py \
      --frames outputs/maps/<session>/frames --model outputs/tools/lingbot_map_src/lingbot-map/models/lingbot-map.pt \
      --out outputs/experiments/learned_depth/lingbot_<session> [--t-range 4 14 --frames-json outputs/maps/<session>/frames.json]

Writes raw model outputs (model's own coordinate frame and scale): predictions.npz with depth [S,H,W],
depth_conf [S,H,W], c2w [S,3,4], intrinsic [S,3,3], plus names.json and run_report.json. Alignment to our SfM poses
and the geometric checks happen in the project environment (mapping.learned_align / mapping.learned_fuse).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")  # ops MPS lacks fall back to CPU instead of failing

import numpy as np
import torch

from lingbot_map.models.gct_stream import GCTStream
from lingbot_map.utils.load_fn import load_and_preprocess_images
from lingbot_map.utils.pose_enc import pose_encoding_to_extri_intri


def _mem(stage: str, log: list) -> None:
    """Process peak RSS, MPS allocations, macOS memory pressure and swap at a pipeline stage."""
    import resource, subprocess
    row = {"stage": stage, "max_rss_gb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9, 2)}
    if torch.backends.mps.is_available():
        row["mps_current_gb"] = round(torch.mps.current_allocated_memory() / 1e9, 2)
        row["mps_driver_gb"] = round(torch.mps.driver_allocated_memory() / 1e9, 2)
    try:
        q = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True, timeout=10).stdout
        row["system_free_pct"] = int(q.strip().splitlines()[-1].split(":")[-1].strip().rstrip("%"))
        sw = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=10).stdout.split()
        row["swap_used_mb"] = float(sw[sw.index("used") + 2].rstrip("M"))
    except Exception:
        pass
    log.append(row)
    print("MEM", row, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", required=True)
    ap.add_argument("--frames-json")
    ap.add_argument("--t-range", type=float, nargs=2)
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", default="mps" if torch.backends.mps.is_available() else "cpu")
    ap.add_argument("--image-size", type=int, default=518)
    ap.add_argument("--num-scale-frames", type=int, default=8)
    ap.add_argument("--dtype", default="bfloat16", choices=["bfloat16", "float32"],
                    help="trunk dtype on the Apple GPU; heads stay float32 (as the demo does on CUDA). bfloat16 is 10-40x faster here")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    names = sorted(p.name for p in Path(a.frames).glob("*.jpg"))
    if a.frames_json and a.t_range:
        t = {f["file"]: f["timestamp_s"] for f in json.loads(Path(a.frames_json).read_text())}
        names = [n for n in names if a.t_range[0] <= t[n] <= a.t_range[1]]
    paths = [str(Path(a.frames) / n) for n in names]
    mem: list = []
    _mem("start", mem)
    t0 = time.monotonic()
    images = load_and_preprocess_images(paths, mode="crop", image_size=a.image_size, patch_size=14)
    dev = torch.device(a.device)
    model = GCTStream(img_size=a.image_size, patch_size=14, enable_3d_rope=True, max_frame_num=1024,
                      kv_cache_sliding_window=64, kv_cache_scale_frames=a.num_scale_frames,
                      kv_cache_cross_frame_special=True, kv_cache_include_scale_frames=True, use_sdpa=True,
                      camera_num_iterations=4)
    ckpt = torch.load(a.model, map_location="cpu", weights_only=False)
    missing, unexpected = model.load_state_dict(ckpt.get("model", ckpt), strict=False)
    del ckpt  # the state dict was copied into the model; keeping the 4 GB checkpoint doubles host memory
    import gc
    gc.collect()
    _mem("model_loaded_cpu", mem)
    # MPS has no float64 / complex128: rotary-embedding frequency tables are created in float64 and moved lazily in forward
    for m in model.modules():
        for k, v in list(vars(m).items()):
            if isinstance(v, torch.Tensor) and v.dtype in (torch.float64, torch.complex128):
                setattr(m, k, v.to(torch.complex64 if v.is_complex() else torch.float32))
        for k, v in list(m._buffers.items()):
            if v is not None and v.dtype in (torch.float64, torch.complex128):
                m._buffers[k] = v.to(torch.complex64 if v.is_complex() else torch.float32)
    model = model.to(dev).eval()
    dtype = getattr(torch, a.dtype)
    if dtype != torch.float32:
        model.aggregator = model.aggregator.to(dtype=dtype)
    if dev.type == "mps":  # the key/value cache grows by concatenation; return freed blocks after every frame
        _fwd = model.forward
        def _fwd_free(*x, **k):
            o = _fwd(*x, **k); torch.mps.empty_cache(); return o
        model.forward = _fwd_free
    _mem("model_on_device", mem)
    t_load = time.monotonic() - t0
    t1 = time.monotonic()
    with torch.no_grad(), torch.amp.autocast(dev.type, dtype=dtype, enabled=dtype != torch.float32):
        pred = model.inference_streaming(images.to(dev), num_scale_frames=a.num_scale_frames, keyframe_interval=1,
                                         output_device=torch.device("cpu"))
    t_inf = time.monotonic() - t1
    _mem("inference_done", mem)
    # outputs are already on the CPU: drop the model and return the MPS cache before post-processing and saving
    del model
    import gc
    gc.collect()
    if dev.type == "mps":
        torch.mps.empty_cache()
    _mem("mps_released", mem)
    # The decoded pose is camera-to-world with OpenCV axes (upstream docs corrected 2026-10-14: "no pose inversion is
    # performed during decoding"); older demo code inverted it for its viewer. Saved as-is; the project-side alignment
    # also checks the inverse and records which convention matches our SfM path.
    c2w, intr = pose_encoding_to_extri_intri(pred["pose_enc"], images.shape[-2:])
    sq = lambda x: x[0] if x.ndim and x.shape[0] == 1 else x  # noqa: E731
    depth = sq(pred["depth"].float().cpu()).numpy()[..., 0]
    conf = sq(pred["depth_conf"].float().cpu()).numpy()
    np.savez_compressed(out / "predictions.npz", depth=depth.astype(np.float32), depth_conf=conf.astype(np.float32),
                        c2w=sq(c2w.float().cpu()).numpy(), intrinsic=sq(intr.float().cpu()).numpy())
    (out / "names.json").write_text(json.dumps(names))
    h = hashlib.sha256()
    with open(a.model, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 24), b""):
            h.update(chunk)
    import lingbot_map, subprocess
    src = Path(lingbot_map.__file__).resolve().parents[1]
    commit = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    rep = {"model": "LingBot-Map (GCTStream, streaming, SDPA attention)", "code_path": str(src), "code_commit": commit, "licence": "Apache-2.0 (code and released weights)",
           "weights": str(a.model), "weights_sha256": h.hexdigest(), "device": a.device, "torch": torch.__version__,
           "frames": len(names), "input_hw": list(images.shape[-2:]), "num_scale_frames": a.num_scale_frames,
           "missing_keys": len(missing), "unexpected_keys": len(unexpected),
           "seconds_load": round(t_load, 1), "seconds_inference": round(t_inf, 1),
           "seconds_per_frame": round(t_inf / max(1, len(names)), 3),
           "mps_high_watermark_ratio": os.environ.get("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "default"), "memory": mem}
    _mem("saved", mem)
    rep["memory"] = mem
    rep["peak_rss_gb"] = max(r["max_rss_gb"] for r in mem)
    rep["peak_mps_driver_gb"] = max(r.get("mps_driver_gb", 0) for r in mem)
    (out / "run_report.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
