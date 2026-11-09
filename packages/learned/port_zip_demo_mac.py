"""Make the LingBot-Map demo from the user's checkout run on a Mac (Apple GPU / MPS), unchanged otherwise.

The checkout's demo.py assumes an NVIDIA GPU: it picks "cuda" or falls back to CPU, loads the checkpoint straight
onto the device, and runs under a CUDA-only autocast. This script copies demo.py and applies a small, listed set of
edits (each one must match exactly, so a different checkout fails loudly instead of being silently mis-edited):
  1. device: Apple GPU (mps) when available
  2. checkpoint read to CPU first, then moved (avoids a second 4.6 GB copy on the GPU)
  3. float64 / complex128 buffers cast to float32 / complex64 (the Apple GPU has neither)
  4. inference dtype on mps: bfloat16 for the trunk, heads stay float32 (as on the CUDA PC); MAC_DTYPE=float32 to override
  5. autocast on the device actually used
  6. after each frame the Apple GPU's cached blocks are returned (the cache grows by concatenation)
  7. outputs converted to float32 on the way to the CPU (NumPy has no bfloat16)
  8. --save_npz to keep the predictions (for comparison), --no_viewer to skip the browser viewer
The model, its settings, the frames and the viewer are the checkout's own.

python -m learned.port_zip_demo_mac --src outputs/tools/lingbot_map_src/lingbot-map --out outputs/tools/lingbot_map_mac
"""
from __future__ import annotations

import argparse
from pathlib import Path

EDITS = [
    ('    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")',
     '    device = torch.device("cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu"))'),
    ('        ckpt = torch.load(args.model_path, map_location=device, weights_only=False)',
     '        ckpt = torch.load(args.model_path, map_location="cpu", weights_only=False)'),
    ('    return model.to(device).eval()',
     '''    if args.model_path:
        del ckpt, state_dict
    if device.type == "mps":  # no float64 / complex128 on the Apple GPU
        for m in model.modules():
            for k, v in list(vars(m).items()):
                if isinstance(v, torch.Tensor) and v.dtype in (torch.float64, torch.complex128):
                    setattr(m, k, v.to(torch.complex64 if v.is_complex() else torch.float32))
            for k, v in list(m._buffers.items()):
                if v is not None and v.dtype in (torch.float64, torch.complex128):
                    m._buffers[k] = v.to(torch.complex64 if v.is_complex() else torch.float32)
        # the key/value cache grows by concatenation every frame; the Apple GPU allocator keeps every old block,
        # so return them after each frame (otherwise a 47-frame pass fills 16 GB by frame 15)
        _fwd = model.forward
        def _forward_then_free(*a, **k):
            out = _fwd(*a, **k)
            torch.mps.empty_cache()
            if os.environ.get("MAC_MEMLOG"):
                print(f"[mem] alloc={torch.mps.current_allocated_memory()/1e9:.2f} GB driver={torch.mps.driver_allocated_memory()/1e9:.2f} GB", flush=True)
            return out
        model.forward = _forward_then_free
    return model.to(device).eval()'''),
    ('''        dtype = torch.bfloat16 if torch.cuda.get_device_capability()[0] >= 8 else torch.float16
    else:
        dtype = torch.float32''',
     '''        dtype = torch.bfloat16 if torch.cuda.get_device_capability()[0] >= 8 else torch.float16
    elif device.type == "mps":
        dtype = getattr(torch, os.environ.get("MAC_DTYPE", "bfloat16"))
    else:
        dtype = torch.float32'''),
    ('k, predictions[k].to("cpu", non_blocking=True)',
     'k, (lambda t: t.float() if t.is_floating_point() and t.dtype != torch.float32 else t)(predictions[k].to("cpu"))'),
    ('torch.amp.autocast("cuda", dtype=dtype)', 'torch.amp.autocast(images.device.type, dtype=dtype, enabled=dtype != torch.float32)'),
    ('    parser.add_argument("--port", type=int, default=8080)',
     '''    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--save_npz", type=str, default=None, help="(Mac port) save predictions for comparison")
    parser.add_argument("--no_viewer", action="store_true", help="(Mac port) do not start the browser viewer")'''),
    ('''    # ── Visualize ────────────────────────────────────────────────────────────
''',
     '''    if args.save_npz:
        vis = prepare_for_visualization(predictions, images_cpu)
        np.savez(args.save_npz, **{k: v for k, v in vis.items() if isinstance(v, np.ndarray)}, paths=np.array(paths))
        print(f"Saved predictions to {args.save_npz}")
    if args.no_viewer:
        return

    # ── Visualize ────────────────────────────────────────────────────────────
'''),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="learned.port_zip_demo_mac")
    ap.add_argument("--src", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    s = (Path(a.src) / "demo.py").read_text()
    for old, new in EDITS:
        n = s.count(old)
        if n == 0:
            raise SystemExit(f"edit not found in this checkout: {old[:70]!r}")
        s = s.replace(old, new)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    (out / "demo_mac.py").write_text(s)
    print(f"wrote {out / 'demo_mac.py'} ({len(EDITS)} edits)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
