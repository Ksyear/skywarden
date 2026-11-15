"""One-time export of the OSNet x0.25 person re-identification network to ONNX for OpenCV DNN (CPU).

Runs in a SEPARATE virtual environment that has torch + onnx (never the project .venv, which stays torch-free):
    python3 -m venv outputs/tools/venv_reid_export
    outputs/tools/venv_reid_export/bin/pip install torch==2.15.0 onnx onnxscript
    outputs/tools/venv_reid_export/bin/python scripts/export_reid_onnx.py

Inputs (downloaded to outputs/tools/reid_export/, both pinned by SHA-256):
  * osnet.py — the OSNet architecture from deep-person-reid (MIT License, Copyright (c) 2018 Kaiyang Zhou) at a
    fixed commit. It is downloaded and imported, not copied into this repository.
  * osnet_x0_25 weights trained on Market-1501 (deep-person-reid model zoo). Market-1501 is distributed for
    non-commercial research use; see data/models/reid/SOURCE.md. The weights are never committed.
Output: data/models/reid/osnet_x0_25_market1501.onnx (gitignored). Input 1x3x256x128 RGB, ImageNet-normalised;
output 1x512 feature (eval mode). The script prints the ONNX SHA-256, which packages/vision/reid.py pins.
The network is loaded with torch.load(weights_only=True): tensors only, no pickled code is executed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "outputs" / "tools" / "reid_export"
OUT = ROOT / "data" / "models" / "reid" / "osnet_x0_25_market1501.onnx"
COMMIT = "f8cd150fdf77e8d9e1ed143b7f308c2c609ded50"
ARCH_URL = f"https://raw.githubusercontent.com/KaiyangZhou/deep-person-reid/{COMMIT}/torchreid/models/osnet.py"
ARCH_SHA = "c7c1c29187d6330f859c91da229271531920464c7011aec13842a086b2263cae"
WEIGHTS_URL = "https://drive.google.com/uc?export=download&id=1z1UghYvOTtjx7kEoRfmqSMu-z62J6MAj"
WEIGHTS_SHA = "c0ff09177a21417a19bc73bbf5af4eb5d2b097c2074ed35e67819ee6cd93612c"


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def fetch(url: str, dst: Path, want: str) -> Path:
    if not dst.is_file() or sha256(dst) != want:
        with urllib.request.urlopen(url, timeout=60) as r:
            dst.write_bytes(r.read())
    got = sha256(dst)
    if got != want:
        dst.unlink()
        raise SystemExit(f"sha256 mismatch for {dst.name}: {got}")
    return dst


def main() -> int:
    import numpy as np
    import torch

    WORK.mkdir(parents=True, exist_ok=True)
    arch = fetch(ARCH_URL, WORK / "osnet.py", ARCH_SHA)
    wts = fetch(WEIGHTS_URL, WORK / "osnet_x0_25_market1501.pth", WEIGHTS_SHA)
    spec = importlib.util.spec_from_file_location("osnet_ref", arch)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sd = torch.load(wts, map_location="cpu", weights_only=True)
    sd = sd.get("state_dict", sd)
    sd = {k.removeprefix("module."): v for k, v in sd.items()}
    num_classes = sd["classifier.weight"].shape[0]
    net = mod.osnet_x0_25(num_classes=num_classes, pretrained=False)
    missing, unexpected = net.load_state_dict(sd, strict=True), None
    net.eval()
    x = torch.randn(1, 3, 256, 128)
    with torch.no_grad():
        ref = net(x).numpy()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(net, (x,), str(OUT), input_names=["images"], output_names=["features"], opset_version=17, dynamic_axes={"images": {0: "n"}, "features": {0: "n"}},
                      dynamo=False)
    info = {"onnx": str(OUT.relative_to(ROOT)), "onnx_sha256": sha256(OUT), "bytes": OUT.stat().st_size,
            "arch_commit": COMMIT, "arch_sha256": ARCH_SHA, "weights_sha256": WEIGHTS_SHA,
            "num_classes_head_dropped_in_eval": int(num_classes), "feature_dim": int(ref.shape[1]),
            "torch": torch.__version__, "load_state_dict": str(missing), "unexpected": unexpected,
            "ref_feature_head": [round(float(v), 6) for v in ref[0, :4]], "ref_input_seed": "torch.randn default",
            "ref_feature_norm": round(float(np.linalg.norm(ref)), 6)}
    np.save(WORK / "ref_input.npy", x.numpy())
    np.save(WORK / "ref_output.npy", ref)
    print(json.dumps(info, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
