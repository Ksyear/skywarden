#!/usr/bin/env bash
# Download the pinned YOLOX person-detector weights (Apache-2.0) into data/models/yolox and verify sha256.
# Usage: scripts/fetch_models.sh [yolox_nano|yolox_tiny ...]   (default: yolox_nano yolox_tiny)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/data/models/yolox"
mkdir -p "$DIR"
BASE="https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0"
sha_for() { case "$1" in
  yolox_nano) echo c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d ;;
  yolox_tiny) echo 427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7 ;;
  *) echo "unknown model $1" >&2; exit 2 ;; esac; }
for m in "${@:-yolox_nano yolox_tiny}"; do for name in $m; do
  want="$(sha_for "$name")"; f="$DIR/$name.onnx"
  if [[ -f "$f" ]] && [[ "$(shasum -a 256 "$f" | cut -d' ' -f1)" == "$want" ]]; then echo "ok   $name (already present)"; continue; fi
  curl -fL --retry 2 -o "$f.part" "$BASE/$name.onnx"
  got="$(shasum -a 256 "$f.part" | cut -d' ' -f1)"
  [[ "$got" == "$want" ]] || { rm -f "$f.part"; echo "sha256 mismatch for $name: $got" >&2; exit 1; }
  mv "$f.part" "$f"; echo "ok   $name ($got)"
done; done
