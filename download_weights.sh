#!/usr/bin/env bash
# 直接用 curl 从 hf-mirror 下载 Breeze TTS 2 权重（不依赖 huggingface_hub）
set -uo pipefail
BASE="https://hf-mirror.com/BreezeBlue/Breeze-TTS-2/resolve/main"
DEST="${1:-F:/TTS2/breeze-tts-2}"
mkdir -p "$DEST/audio_tokenizer"

FILES=(
  "config.json"
  "generation_config.json"
  "model.safetensors.index.json"
  "special_tokens_map.json"
  "tokenizer.json"
  "tokenizer_config.json"
  "audio_tokenizer/config.json"
  "audio_tokenizer/configuration.json"
  "audio_tokenizer/preprocessor_config.json"
  "model-00001-of-00002.safetensors"
  "model-00002-of-00002.safetensors"
  "audio_tokenizer/model.safetensors"
)

for f in "${FILES[@]}"; do
  out="$DEST/$f"
  if [ -f "$out" ] && [ -s "$out" ]; then
    echo "SKIP (exists): $f"
    continue
  fi
  echo "GET $f"
  curl -L -C - --retry 8 --retry-delay 3 --connect-timeout 30 \
    -o "$out" "$BASE/$f" || echo "FAIL $f"
  echo "  -> $(stat -c%s "$out" 2>/dev/null || echo 0) bytes"
done
echo "ALL_DONE"
