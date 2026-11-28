#!/usr/bin/env python
"""GPT-SoVITS v3 sidecar 生命周期管理（面板与守护共用的唯一入口）。

为什么单独一个脚本：本机 8 GB 显存装不下两个常驻 TTS。GPT-SoVITS 采用
「按需启动 + 用时让出 7860」的口径（雪峰 2026-11-28 确认的 A 方案）：
要跑 GPT-SoVITS 前先停掉 7860 的主力引擎并给守护写暂停标记，
用完再放开，由守护把 7860 拉回来。

子命令：
  status            探针 + 进程/端口情况 + 当前空闲显存
  start [--take-gpu]  启动；显存不足时按 --take-gpu 决定是否先停 7860
  stop              停掉 sidecar
  release           停 sidecar 并解除 7860 的暂停标记（交给守护恢复）
  tts               命令行合成一次（验收与排障用），输出 wav 路径

日志：F:/TTS2/logs/gpt-sovits-api.log；暂停标记：F:/TTS2/ops/logs/gpu-hold.json
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SOVITS_HOME = Path(r"E:\TTS2-runtime\gpt-sovits")
PY = SOVITS_HOME / "venv" / "Scripts" / "python.exe"
API_SCRIPT = "api_v2.py"
HOST = "127.0.0.1"
PORT = 9880
BASE = f"http://{HOST}:{PORT}"

LOGS = Path(r"F:\TTS2\logs")
API_LOG = LOGS / "gpt-sovits-api.log"
HOLD_FILE = Path(r"F:\TTS2\ops\logs\gpu-hold.json")

ADAPTER_LABEL = "adapter(7860)"
ADAPTER_PORT = 7860
# 低于这个空闲显存就不给启动：GPT-SoVITS v3 实测要数 GB，贴边启动只会 OOM 崩进程
MIN_FREE_MIB = 3500
HOLD_MINUTES = 60

DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


# ---------------------------------------------------------------- 探测与显存
def probe(timeout: float = 3.0) -> bool:
    """探针只看「服务在不在」：/api_v2 没有 health 路由，任何 HTTP 响应都算活着。"""
    try:
        with urllib.request.urlopen(f"{BASE}/tts", timeout=timeout) as r:
            r.read(1)
        return True
    except urllib.error.HTTPError:
        return True          # 有响应就是活着（缺参数会 400/422）
    except Exception:
        return False


def free_vram_mib() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="gbk", errors="replace", timeout=15,
        ).stdout
        # Windows 中文系统把 cmd 输出按 GBK 编码，utf-8 解码会抛 UnicodeDecodeError
        return int(out.splitlines()[0]) if out and out.splitlines()[0].strip().isdigit() else None
        return int(out[0]) if out and out[0].strip().isdigit() else None
    except Exception:
        return None


def port_pids(port: int) -> list[int]:
    """从 netstat 反查监听该端口的 PID。"""
    pids: list[int] = []
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, encoding="gbk", errors="replace", timeout=20).stdout
    except Exception:
        return pids
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].rsplit(":", 1)[-1] == str(port):
                try:
                    pids.append(int(parts[4]))
                except ValueError:
                    pass
    return sorted(set(pids))


def _kill(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True, timeout=20)


# ---------------------------------------------------------------- 暂停标记
def _read_hold() -> dict:
    try:
        return json.loads(HOLD_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def set_hold(minutes: int = HOLD_MINUTES) -> None:
    """让守护在 N 分钟内不要拉起 7860，否则我们刚腾出来的显存立刻被吃回去。"""
    data = _read_hold()
    data[ADAPTER_LABEL] = int(time.time()) + minutes * 60
    HOLD_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOLD_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"已暂停守护拉起 {ADAPTER_LABEL} {minutes} 分钟")


def clear_hold() -> None:
    data = _read_hold()
    if data.pop(ADAPTER_LABEL, None) is not None:
        HOLD_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"已解除 {ADAPTER_LABEL} 的暂停标记（守护会在下一分钟拉起它）")


def take_gpu() -> None:
    """A 方案核心：先让 7860 腾出显存，再给守护上锁防止它被立刻拉回来。"""
    killed = []
    for pid in port_pids(ADAPTER_PORT):
        try:
            _kill(pid)
            killed.append(pid)
        except Exception as exc:
            log(f"杀 {ADAPTER_PORT} 上的 pid={pid} 失败：{exc}")
    if killed:
        log(f"已停掉 7860 进程：{killed}")
    else:
        log("7860 上没有进程可停（可能本来就没在跑）")
    set_hold()


# ---------------------------------------------------------------- 生命周期
def start(take_gpu_first: bool = False, wait: float = 300.0) -> int:
    if probe():
        log("sidecar 已在运行，直接复用")
        return 0
    if not PY.is_file():
        log(f"找不到解释器：{PY} —— 请先装好 venv 与依赖")
        return 3
    free = free_vram_mib()
    log(f"当前空闲显存：{free} MiB")
    if free is not None and free < MIN_FREE_MIB:
        if not take_gpu_first:
            log(f"空闲显存只有 {free} MiB（<{MIN_FREE_MIB}），直接启动大概率 OOM。"
                "要么加 --take-gpu 先停 7860，要么改用 qwen3-tts")
            return 2
        take_gpu()
        time.sleep(8)      # 等驱动把显存还回来
        free = free_vram_mib()
        log(f"让出后空闲显存：{free} MiB")

    LOGS.mkdir(parents=True, exist_ok=True)
    log_fh = open(API_LOG, "ab")
    proc = subprocess.Popen(
        [str(PY), API_SCRIPT, "-a", HOST, "-p", str(PORT)],
        cwd=str(SOVITS_HOME), stdout=log_fh, stderr=subprocess.STDOUT,
        creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
    )
    log(f"已拉起 sidecar pid={proc.pid}，日志 {API_LOG}")
    deadline = time.time() + wait
    while time.time() < deadline:
        if probe():
            log("探针通过，服务就绪")
            return 0
        if proc.poll() is not None:
            log(f"进程已退出（code={proc.returncode}），原因见 {API_LOG}")
            return 4
        time.sleep(5)
    log(f"等待 {int(wait)}s 仍未就绪，请查 {API_LOG}")
    return 5


def stop() -> int:
    pids = port_pids(PORT)
    if not pids:
        log("sidecar 未在运行")
        return 0
    for pid in pids:
        _kill(pid)
    log(f"已停掉 sidecar：{pids}")
    return 0


def cmd_status() -> int:
    running = probe()
    pids = port_pids(PORT)
    free = free_vram_mib()
    hold = _read_hold().get(ADAPTER_LABEL, 0)
    left = max(0, int(hold - time.time()))
    print(json.dumps({
        "running": running, "pids": pids, "port": PORT,
        "free_vram_mib": free,
        "adapter_hold_seconds_left": left,
    }, ensure_ascii=False))
    return 0


VOICES_JSON = Path(r"F:\TTS2\breeze-tts\data\voices.json")
SOVITS_REF_DIR = Path(r"F:\TTS2\breeze-tts\data\sovits_refs")


def _find_voice(key: str) -> dict | None:
    """按 id 或名字在面板账本里找音色。命令行排障时用名字更顺手。"""
    try:
        items = json.loads(VOICES_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"读不到 {VOICES_JSON}：{exc}")
        return None
    for v in items:
        if v.get("id") == key:
            return v
    for v in items:
        if v.get("name") == key:
            return v
    return None


def _asr_text(path: Path) -> str:
    """复用面板同一条 ASR（faster-whisper CPU）把裁出来的片段重新转写。

    裁段后音频里说的话变了，原来的整段文稿就对不上；prompt_text 与音频
    不一致会直接拉低克隆相似度，所以这里必须重转而不是沿用原稿。
    """
    sys.path.insert(0, r"F:\TTS2\breeze-tts")
    import panel  # noqa: PLC0415  仅此路径需要，避免 status/start 也拖起 ASR 依赖

    return panel._transcribe(path)["text"].strip()


def prepare_ref(src: Path, voice_id: str, use_asr: bool = True) -> tuple[Path, str]:
    """把参考音频规范到 3~10 秒并配一份与之匹配的文稿。

    编排逻辑与面板共用 `sovits_ref.prepare`，这里只负责注入命令行侧的 ASR。
    返回 (可用音频路径, 文稿)。无需裁剪时返回原文件，文稿留空由调用方用原稿。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sovits_ref import prepare  # noqa: PLC0415

    asr = _asr_text if use_asr else None
    path, text = prepare(src, SOVITS_REF_DIR, voice_id or src.stem, asr_fn=asr)
    if path != src:
        log(f"已裁段并缓存：{path}")
        if text:
            log(f"重配文稿（{len(text)} 字）：{text}")
    return path, text


def cmd_tts(args: argparse.Namespace) -> int:
    ref_audio = args.ref_audio or ""
    prompt_text = args.prompt_text or ""
    voice_id = ""
    if args.voice:
        v = _find_voice(args.voice)
        if not v:
            log(f"账本里找不到音色「{args.voice}」（{VOICES_JSON}）")
            return 2
        voice_id = v["id"]
        rel = (v.get("ref_audio") or "").strip()
        if not rel:
            log(f"音色「{v.get('name')}」是设计型（没有参考音频），GPT-SoVITS 需要参考音频才能克隆")
            return 2
        ref_audio = str(Path(r"F:\TTS2\breeze-tts\data") / rel)
        if not prompt_text:
            prompt_text = (v.get("ref_text") or "").strip()
        log(f"音色：{v.get('name')} ({voice_id})")

    if not ref_audio:
        log("缺参考音频：用 --voice <id|名字> 或 --ref-audio <路径>")
        return 2
    src = Path(ref_audio)
    if not src.is_file():
        log(f"参考音频不存在：{src}")
        return 2

    try:
        ref_path, auto_text = prepare_ref(src, voice_id, use_asr=not args.no_asr)
    except ValueError as exc:
        log(str(exc))
        return 2
    if auto_text:
        prompt_text = auto_text          # 裁段后必须用与片段对齐的文稿
    if not prompt_text:
        log("缺参考文稿：请用 --prompt-text 指定，或加 --voice 让工具自动重配")
        return 2

    if not probe():
        log("sidecar 没在运行，先 start")
        return 3
    qs = {
        "text": args.text,
        "text_lang": args.text_lang,
        "ref_audio_path": str(ref_path),
        "prompt_text": prompt_text,
        "prompt_lang": args.prompt_lang,
        "media_type": "wav",
        "batch_size": "1",
    }
    url = f"{BASE}/tts?" + urllib.parse.urlencode(qs)
    started = time.time()
    try:
        with urllib.request.urlopen(url, timeout=args.timeout) as r:
            data = r.read()
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        body = e.read()[:400].decode("utf-8", "replace")
        log(f"HTTP {e.code}：{body}")
        return 4
    out = Path(args.out or (LOGS / "sovits-probe.wav"))
    out.write_bytes(data)
    log(f"已写 {out}（{len(data)} B，{ctype}，{time.time() - started:.1f}s）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="GPT-SoVITS sidecar 管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    s = sub.add_parser("start")
    s.add_argument("--take-gpu", action="store_true",
                   help="显存不足时先停 7860 并暂停守护拉起它（A 方案）")
    s.add_argument("--wait", type=float, default=300.0)
    sub.add_parser("stop")
    sub.add_parser("release")
    t = sub.add_parser("tts")
    t.add_argument("--text", required=True)
    t.add_argument("--voice", default="",
                   help="面板账本里的音色 id 或名字：自动取出参考音频并按需裁段/重配文稿")
    t.add_argument("--ref-audio", default="", help="参考音频绝对路径（与 --voice 二选一）")
    t.add_argument("--prompt-text", default="", help="参考音频里说的话（给了就不再自动转写）")
    t.add_argument("--no-asr", action="store_true",
                   help="裁段后不自动转写文稿（此时必须自带 --prompt-text，否则拒绝）")
    t.add_argument("--text-lang", default="zh")
    t.add_argument("--prompt-lang", default="zh")
    t.add_argument("--out", default="")
    t.add_argument("--timeout", type=float, default=600.0)
    args = ap.parse_args()

    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "start":
        return start(args.take_gpu, args.wait)
    if args.cmd == "stop":
        return stop()
    if args.cmd == "release":
        stop()
        clear_hold()
        return 0
    if args.cmd == "tts":
        return cmd_tts(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
