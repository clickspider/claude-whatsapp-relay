#!/bin/bash
# Download the local whisper.cpp speech-to-text model (about 550 MB). Voice notes never leave your computer.
# Usage: scripts/get-model.sh [model-name]   (default: large-v3-turbo-q5_0)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="${1:-large-v3-turbo-q5_0}"
[[ "$NAME" =~ ^[a-z0-9.-]+(_[a-z0-9]+)?$ ]] || { echo "Bad model name"; exit 1; }
mkdir -p "$ROOT/models"
OUT="$ROOT/models/ggml-$NAME.bin"
[ -f "$OUT" ] && { echo "Already there: $OUT"; exit 0; }
URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-$NAME.bin"
echo "Downloading $URL"
curl -fL --proto '=https' -o "$OUT.part" "$URL"
mv "$OUT.part" "$OUT"
echo "Saved $OUT. If you chose another model, set paths.whisper_model in relay.config.toml."
