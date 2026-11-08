# ENV AUDIT — measured on 2026-11-08 21:21 KST

Source: `outputs/evidence/m0/env.txt` (raw capture by Opus 5.5). Values below are copied from that file; nothing is assumed.

## Hardware and OS

| Item | Measured value |
|---|---|
| Chip | Apple M6 |
| Physical memory | 17,179,869,184 bytes = 16 GiB |
| Architecture | arm64 (kernel `xnu-13432.41.4~25/RELEASE_ARM64_T8152`) |
| macOS | 27.2, build 26B123, Darwin 27.2.0 |
| Project volume | `/Volumes/HIBACHI` on `/dev/disk6s1`: 954 GiB total, 622 GiB used, 331 GiB free (66 %) |

Correction to earlier documents: `docs/IMPLEMENTATION_PLAN.md` assumed "M4 Pro, 24 GB". The measured host is an Apple M6 with 16 GiB. Memory-sensitive settings (SIFT thread count, frame cap, image long side) should be judged against 16 GiB. pycolmap's warning about per-thread RAM for CPU SIFT appears at the top of every build log; at 640x480 and 53 frames it was harmless.

## Toolchain

| Tool | Version | Note |
|---|---|---|
| Python | 3.13.16 | Project `.venv` created with `python3 -m venv` |
| pycolmap | 4.2.1 | Wheel installed in `.venv`; CPU SIFT extraction/matching; no CUDA, no dense MVS |
| OpenCV (`cv2`) | 5.0.0 | |
| numpy | 2.5.4 | |
| scipy | 1.18.1 | |
| pytest | 9.2.0 | From `outputs/evidence/tests/pytest-20261108-212122.log` |
| ffmpeg | 9.0.2 | |
| node | v24.21.0 | Used for `tests/ui/ui_smoke.mjs` |
| cmake | 4.4.4 | Present, not needed for iteration 1 |
| Apple clang | 21.0.0 (clang-2100.4.1.1) | Present, not needed for iteration 1 |
| codex-cli | 0.165.0 | Used only to generate the two design images in `assets/design/` (see `docs/DESIGN_ASSETS.md`) |
| colmap CLI | not installed | pycolmap used instead; `brew install colmap` was not requested |
| uv | mise shim present but no version configured | Not changed; the venv was created with `python3 -m venv` instead |
| Browser for UI tests | Dia (`/Applications/Dia.app/Contents/MacOS/Dia`) | From `outputs/evidence/ui/ui_smoke.json`; headless over the DevTools protocol |

## Runtime defaults in scripts

`scripts/run_demo.sh` and `scripts/run_tracking_sim.sh` export `HARDWARE_ARMED=false DRY_RUN=true` before launching anything. Default console port is 8780 (`apps/desktop/server.py --port`), chosen because 8765 was occupied by another local program on this machine.

## Not measured

GPU/Metal capability, battery/thermal state, and camera devices were not captured. No webcam was used in this iteration.
