@echo off
REM breeze-tts 快速模式启动脚本（2026-11-06 定稿）
REM
REM 为什么要这个脚本：
REM   --fast-depth-decoder 的 CUDA Graph 捕获本身不需要 torch.compile。
REM   在 Windows + torch 2.9 上 torch.compile 走 triton 会抛 OverflowError，
REM   所以必须用 BREEZE_DEPTH_GRAPH_NO_COMPILE=1：关掉编译、只做捕获。
REM   少了这一步服务起不来；加上它，RTF 从 10.2 降到 4.8~5.9。
REM
REM 实测（2.72s 音频，同一脚本同一文本，3 次）：
REM   仅 --fast-backbone-decode   TTFA 1778~1890ms  RTF 10.15~10.28
REM   + 深度图（本脚本）           TTFA 1026~1647ms  RTF  4.81~5.37
REM   约 2.1 倍，首包延迟减半。

setlocal
cd /d %~dp0

set HF_ENDPOINT=https://hf-mirror.com
set BREEZE_DEPTH_GRAPH_NO_COMPILE=1

set PORT=7860
if not "%~1"=="" set PORT=%~1

".venv\Scripts\python.exe" -m breeze_infer.api ..\breeze-tts-2 ^
  --port %PORT% ^
  --fast-backbone-decode ^
  --fast-depth-decoder

endlocal
