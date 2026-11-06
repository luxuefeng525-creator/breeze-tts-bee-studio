#!/usr/bin/env bash
# breeze-tts Streaming API 启动脚本
#
# 端口台账（权威口径，见 F:/TTS2/.workbuddy/memory/MEMORY.md）：
#   API 端口 = 7860 —— 已锁定，一旦启用永不变更，请勿修改下方 PORT。
#
# 用法:
#   bash start_api.sh [模型路径或 HF repo id] [额外参数...]
#   bash start_api.sh ../breeze-tts-2
#   bash start_api.sh BreezeBlue/breeze-tts-2 --revision main --fast-all
set -euo pipefail

MODEL="${1:-../breeze-tts-2}"
if [ "$#" -gt 0 ]; then shift; fi

HOST="0.0.0.0"
PORT=7860  # 固定端口，禁止变更（已锁定）

# 优先使用项目自带 venv
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -x "$SCRIPT_DIR/.venv/Scripts/python.exe" ]; then
  PYTHON="$SCRIPT_DIR/.venv/Scripts/python.exe"
elif [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
  PYTHON="$SCRIPT_DIR/.venv/bin/python"
else
  PYTHON="python"
fi

# huggingface.co 在本网络不可达，统一走 hf-mirror 镜像
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"  # 防显存碎片 OOM

# 8GB 显存下的稳态组合：只开 backbone decode 图（实测 6.7x），depth decoder 图需 triton 且撞 torch Windows 静态启动器 bug
export TORCHINDUCTOR_USE_STATIC_CUDA_LAUNCHER="${TORCHINDUCTOR_USE_STATIC_CUDA_LAUNCHER:-0}"
exec "$PYTHON" -m breeze_infer.api "$MODEL" --host "$HOST" --port "$PORT" --fast-backbone-decode "$@"
