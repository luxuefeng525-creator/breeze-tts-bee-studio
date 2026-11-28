#!/usr/bin/env python
"""BreezeTTS Studio — breeze-tts Web 控制面板（stdlib 单文件，零第三方依赖）。

端口台账（权威口径见 F:/TTS2/.workbuddy/memory/MEMORY.md，启用后冻结）：
  面板 7861（本文件）| 合成 API 7860（breeze_infer.api，不改不扰）

用法:
  python panel.py            # 监听 0.0.0.0:7861
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import re
import sqlite3
import sys
import struct
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, unquote, urlencode
from urllib.request import Request, urlopen
import http.client as _httpclient

# 直接 `python panel.py` 运行时模块名是 __main__，而 script_routes 里 `import panel`
# 会再加载一份——锁、DATA、_JOBS 全被复制成两份，并发保护形同虚设。
# 把它登记成 panel，让两种导入路径拿到同一个模块对象。
sys.modules.setdefault("panel", sys.modules[__name__])

# ---- 端口台账（锁定，禁止修改默认值）----
# 默认值就是台账口径；允许用环境变量覆写**仅为**回滚演练/并行金丝雀，
# 否则新旧版本必须抢同一个端口，回滚就只能先停后起（期间服务中断）。
PANEL_PORT = int(os.environ.get("BREEZE_PANEL_PORT") or 7861)
API_BASE = os.environ.get("BREEZE_API_BASE") or "http://127.0.0.1:7860"  # 端口已冻结

ROOT = Path(__file__).resolve().parent
# 默认跟着代码走；允许覆写是为了让并行演练实例能读到同一份数据副本，
# 否则金丝雀实例看到的是空库，回滚演练验证不到真实音色与历史。
DATA = Path(os.environ.get("BREEZE_DATA") or (ROOT / "data")).resolve()
REFS = DATA / "refs"
AUDIOS = DATA / "audio"
# 每个账本文件保留的历史版本数：写盘前自动留档，供误删/整批改写后回滚
_HIST_KEEP = 20
for d in (DATA, REFS, AUDIOS):
    d.mkdir(parents=True, exist_ok=True)

class _ValidationError(Exception):
    """读改写过程中发现数据不合法：不写入，由外层翻成 400。"""


def _resolve_audio(rel: str) -> Path | None:
    """把 /api/audio/ 后的相对路径解析成 AUDIOS 下的真实文件，越界返回 None。

    只按 "/" 切分是不够的：Windows 上反斜杠同样分隔目录，盘符更是直接
    跳出整个目录树（实测 joinpath("C:\\Windows\\win.ini") 可读）。
    因此这里既拒绝可疑字符，也对最终路径做 is_relative_to 校验。
    """
    if not rel or "\\" in rel or ":" in rel or "\x00" in rel:
        return None
    parts = [seg for seg in rel.split("/") if seg]
    if not parts or len(parts) > 3 or any(seg in (".", "..") for seg in parts):
        return None
    if any(seg != _safe_name(seg) for seg in parts):
        return None
    try:
        base = AUDIOS.resolve()
        target = base.joinpath(*parts).resolve()
    except (OSError, ValueError):
        return None
    if target != base and not target.is_relative_to(base):
        return None
    return target


def _post_upstream(api_path: str, body: bytes, content_type: str, timeout: int = 600,
                   headers_out: dict | None = None):
    """POST 到推理 API，返回 (status, body, ctype)。错误已翻译成人话。

    headers_out 非空时把上游响应头写入该字典。Qwen 适配层用
    X-Adapter-Note 说明静默降级（例如克隆音色忽略指令），必须让 UI 看见，
    而不是像早期版本那样只取 body 就把头丢掉。
    """
    req = Request(API_BASE + api_path, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    try:
        with urlopen(req, timeout=timeout) as resp:
            if headers_out is not None:
                headers_out.update({
                    str(k).lower(): resp.headers.get(k)
                    for k in resp.headers.keys()
                })
            return resp.status, resp.read(), resp.headers.get("Content-Type",
                                                              "application/octet-stream")
    except HTTPError as e:
        detail = e.read()
        try:
            detail = json.loads(detail).get("detail", detail[:200])
        except Exception:
            detail = (detail.decode("utf-8", "replace")[:200]
                      if isinstance(detail, bytes) else str(detail)[:200])
        return e.code, json.dumps({"detail": _friendly_error(detail)},
                                  ensure_ascii=False).encode(), "application/json"
    except URLError as e:
        return 502, json.dumps({"detail": f"API 7860 不可达: {e.reason}"}).encode(),             "application/json"


def _post_with_retry(api_path: str, body: bytes, content_type: str,
                     timeout: int = 600,
                     headers_out: dict | None = None) -> tuple[int, bytes]:
    """排队 + 409 自动重试。剧本逐行合成也要走这里，否则会跟单条请求撞车。"""
    code, raw = 502, b"{}"
    with _INFER_LOCK:
        for attempt in range(60):
            code, raw, _ct = _post_upstream(api_path, body, content_type,
                                            timeout, headers_out)
            if code != 409:
                return code, raw
            if attempt < 59:
                time.sleep(3.0)
    return code, raw


# ---------------------------------------------------------------------------
# GPT-SoVITS sidecar（可选第二引擎）
# ---------------------------------------------------------------------------
# 本机 8 GB 显存装不下两个常驻 TTS：sidecar 按需启动，启动前先让 7860 让出显存
# （口径见 tools/gpt_sovits_sidecar.py）。这里只负责「按音色路由 + 统一产物格式」，
# 使前端、历史记录、导出与 7860 链路完全一致。
SOVITS_BASE = (os.environ.get("BREEZE_SOVITS_BASE") or "http://127.0.0.1:9880").rstrip("/")
SOVITS_REF_DIR = DATA / "sovits_refs"
# v3 是扩散式解码，步数直接决定耗时：32 步（上游默认）约 3.5 s/3 秒音频，
# 16 步约 2.9 s，听感差异很小。要更高保真可以用环境变量调回 32。
SOVITS_SAMPLE_STEPS = int(os.environ.get("BREEZE_SOVITS_STEPS") or 16)
# cache_dit（DBCache）是上游对 V3/V4/V5 DiT 的官方加速：缓存前 Fn 块的残差，
# 显著减少扩散前向次数、听感近一致。默认 f16；要更稳/更快用 f8，关掉用 off。
SOVITS_CACHE_DIT = (os.environ.get("BREEZE_SOVITS_CACHE_DIT") or "f16").strip()

# 裁段实现与命令行工具共用一份，避免两处逻辑漂移
_TOOLS_DIR = ROOT / "tools"
if _TOOLS_DIR.is_dir() and str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

ENGINE_QWEN = "qwen3-tts"
ENGINE_SOVITS = "gpt-sovits"


def voice_engine(voice) -> str:
    """音色指定的合成引擎。缺省 qwen3-tts —— 老数据完全不受影响。"""
    raw = str((voice or {}).get("engine") or "").strip().lower()
    return ENGINE_SOVITS if raw == ENGINE_SOVITS else ENGINE_QWEN


def _sovits_probe(timeout: float = 2.0) -> bool:
    """探针：/tts 缺参数会回 4xx，只要有响应就说明服务活着（与 CLI 口径一致）。"""
    try:
        with urlopen(SOVITS_BASE + "/tts", timeout=timeout) as resp:
            resp.read(1)
        return True
    except HTTPError:
        return True
    except Exception:
        return False


def _sovits_unavailable() -> str:
    return ("GPT-SoVITS 服务未启动或不可达（" + SOVITS_BASE + "）。"
            "本机只有 8 GB 显存，它与主力引擎不能同时常驻：请先在命令行运行 "
            "tools\\gpt_sovits_sidecar.py start --take-gpu 启动它，合成完会自动让回。")


def _sovits_ref_for(voice) -> tuple[Path, str]:
    """取/生成该音色可用的参考音频与文稿。

    超过 10 秒的参考音频会被裁成语音最密集的片段，并用 ASR 重配文稿
    （v3 不支持无文稿模式，见上游 TTS.py:1275）。
    """
    from sovits_ref import prepare as _prepare  # noqa: PLC0415  延迟导入

    rel = (voice.get("ref_audio") or "").strip()
    src = DATA / rel if rel else None
    if not src or not src.is_file():
        raise ValueError(
            f"音色「{voice.get('name', '')}」没有可用的参考音频文件。"
            "GPT-SoVITS 靠参考音频克隆，请先上传一段 5~10 秒的人声")
    path, text = _prepare(src, SOVITS_REF_DIR, voice["id"],
                          asr_fn=lambda p: _transcribe(p)["text"])
    # 用户逐字校对过的原稿比 ASR 转写更可信：裁段后参考文稿优先用音色自带 ref_text，
    # 避免 ASR 转写失败（乱码）污染克隆音色（实测 10.4s 音频 ASR 出「现在最潜艳的…」乱码）。
    # ASR 结果只作兜底，ref_text 缺失时才会落到它。
    user_text = (voice.get("ref_text") or "").strip()
    if user_text:
        text = user_text
    if not text:
        raise ValueError(
            f"音色「{voice.get('name', '')}」缺少参考文稿："
            "请点编辑把参考音频里说的话一字不差补上")
    return path, text


def _sovits_synthesize(text: str, voice, timeout: int = 900) -> tuple[int, bytes]:
    """调 sidecar 合成，返回 (200, wav_bytes) 或 (错误码, 可直接回前端的 JSON)。"""
    text = (text or "").strip()
    if not text:
        return 400, json.dumps({"detail": "text required"}, ensure_ascii=False).encode()
    if not _sovits_probe():
        return 503, json.dumps({"detail": _sovits_unavailable()},
                               ensure_ascii=False).encode()
    try:
        ref_path, prompt_text = _sovits_ref_for(voice)
    except ValueError as exc:
        return 400, json.dumps({"detail": str(exc)}, ensure_ascii=False).encode()

    qs = urlencode({
        "text": text, "text_lang": "zh",
        "ref_audio_path": str(ref_path), "prompt_text": prompt_text,
        "prompt_lang": "zh", "media_type": "wav",
        "batch_size": "1", "speed_factor": "1.0",
        "sample_steps": str(SOVITS_SAMPLE_STEPS),
        "cache_dit": SOVITS_CACHE_DIT,
    })
    req = Request(SOVITS_BASE + "/tts?" + qs, method="GET")
    # 与 7860 共用一把锁：两条链路都只允许单并发，撞车只会两边一起卡死
    with _INFER_LOCK:
        try:
            with urlopen(req, timeout=timeout) as resp:
                return 200, resp.read()
        except HTTPError as exc:
            body = exc.read()[:400]
            try:
                parsed = json.loads(body)
                msg = parsed.get("Exception") or parsed.get("message") or str(parsed)
            except Exception:
                msg = body.decode("utf-8", "replace")
            return exc.code, json.dumps(
                {"detail": f"GPT-SoVITS 合成失败：{msg}"}, ensure_ascii=False).encode()
        except Exception as exc:
            return 502, json.dumps(
                {"detail": f"GPT-SoVITS 请求异常：{exc}"}, ensure_ascii=False).encode()


def _wav_to_pcm(wav: bytes) -> bytes:
    """把 sidecar 返回的 WAV 解成 24 kHz / 单声道 s16le，与 7860 链路同口径。"""
    import io
    import numpy as np
    import soundfile as sf

    data, sr = sf.read(io.BytesIO(wav), dtype="float32", always_2d=True)
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    if sr != SAMPLE_RATE and len(mono):
        n = max(1, int(round(len(mono) * SAMPLE_RATE / sr)))
        x = np.linspace(0, len(mono) - 1, n, dtype=np.float64)
        mono = np.interp(x, np.arange(len(mono)), mono).astype(np.float32)
    return (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


# ---- 一键开关：面板内直接启停 sidecar ----
# 启动要 60~90 秒（停 7860 → 等驱动回收显存 → 加载模型），不能让 HTTP 请求干等，
# 所以做成「立即返回 starting + 前端轮询状态」。实现直接复用 CLI 的同一套逻辑，
# 避免面板与命令行两份启停代码各修各的。
_SOVITS_SWITCH_LOCK = threading.Lock()
_SOVITS_SWITCH = {"state": "idle", "detail": "", "ts": 0.0}


def _sovits_switch_set(state: str, detail: str = "") -> None:
    with _SOVITS_SWITCH_LOCK:
        _SOVITS_SWITCH.update(state=state, detail=detail, ts=time.time())


def _sovits_switch_status() -> dict:
    with _SOVITS_SWITCH_LOCK:
        snap = dict(_SOVITS_SWITCH)
    alive = _sovits_probe(timeout=1.5)
    # 以探针为准：进程可能被外部杀掉，或手工在命令行起过。
    # starting / warming 是「还在准备」，不能被探针提前改判成 running。
    if snap["state"] in ("starting", "warming"):
        snap["running"] = alive
        snap["free_vram_mib"] = _sovits_free_vram()
        return snap
    if not alive and snap["state"] == "running":
        snap["state"], snap["detail"] = "idle", "服务已不在运行"
    elif alive and snap["state"] != "running":
        snap["state"] = "running"
    snap["running"] = alive
    snap["free_vram_mib"] = _sovits_free_vram()
    return snap


def _sovits_free_vram() -> int | None:
    """给前端显示「还差多少显存」用；拿不到就返回 None，不阻断。"""
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="gbk", errors="replace", timeout=15).stdout.strip().splitlines()
        return int(out[0]) if out and out[0].strip().isdigit() else None
    except Exception:
        return None


def _sovits_warmup() -> None:
    """首次合成要额外 60 秒做图捕获与编译，用户会以为卡死。

    sidecar 启动后先用一条既有音色跑一句极短的话，把这份一次性开销提前吃掉，
    之后用户真正合成时就是 3 秒级。失败不影响服务可用性，只记日志。
    """
    try:
        voices = _load("voices.json")
        cand = [v for v in voices if (v.get("ref_audio") or "").strip()]
        # 优先预热已经标了 gpt-sovits 的音色：那才是用户马上要用的
        cand.sort(key=lambda v: 0 if (v.get("engine") or "") == ENGINE_SOVITS else 1)
        if not cand:
            return
        code, _ = _sovits_synthesize("预热。", cand[0], timeout=300)
        sys.stderr.write(f"[panel] sovits 预热结束 code={code}\n")
    except Exception as exc:
        sys.stderr.write(f"[panel] sovits 预热失败（不影响使用）: {exc!r}\n")


def _sovits_switch_start(take_gpu: bool = True) -> dict:
    with _SOVITS_SWITCH_LOCK:
        if _SOVITS_SWITCH["state"] in ("starting", "warming"):
            return {"state": _SOVITS_SWITCH["state"], "detail": "已经在启动中，请稍候"}
    _sovits_switch_set("starting",
                       "正在让出显存并加载模型，约 60~90 秒；期间主力引擎会暂时停止")

    def _run():
        try:
            import gpt_sovits_sidecar as S  # noqa: PLC0415  tools/ 已在 sys.path
            code = S.start(take_gpu_first=take_gpu, wait=600)
            if code == 0:
                # 预热同样持有推理锁。此时若报「已就绪」，用户点合成会干等一分钟，
                # 看起来像卡死——必须先明确告诉他在预热。
                _sovits_switch_set("warming", "正在预热模型（约 1 分钟），完成后即可合成")
                _sovits_warmup()
                _sovits_switch_set("running", "服务已就绪，可以合成")
            elif code == 2:
                _sovits_switch_set(
                    "error", "显存不足且未允许让出主力引擎，请重试并允许「让出显存」")
            else:
                _sovits_switch_set(
                    "error", f"启动失败（退出码 {code}），详见 F:\\TTS2\\logs\\gpt-sovits-api.log")
        except Exception as exc:
            _sovits_switch_set("error", f"启动异常：{exc}")

    threading.Thread(target=_run, daemon=True, name="sovits-switch").start()
    return _sovits_switch_status()


def _sovits_switch_stop() -> dict:
    try:
        import gpt_sovits_sidecar as S  # noqa: PLC0415
        S.stop()
        S.clear_hold()
        _sovits_switch_set("idle", "已停止；主力引擎会在 1 分钟内由守护自动拉起")
    except Exception as exc:
        _sovits_switch_set("error", f"停止失败：{exc}")
    return _sovits_switch_status()


def _header_get(headers: dict, name: str) -> str:
    """HTTP 头名大小写不敏感；uvicorn 会统一发送小写头名。"""
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value or "")
    return ""


def _engine_note(headers: dict) -> str:
    """还原上游 percent-encode 过的降级说明（HTTP 头不支持非 latin-1 字符）。"""
    return unquote(_header_get(headers, "X-Adapter-Note"))


# ---------------------------------------------------------------------------
# 参考音频转写（克隆页自动填文稿）
# ---------------------------------------------------------------------------
# faster-whisper 跑在面板进程里：只在「上传参考音频」时调用一次，
# 10–15 秒音频在 CPU 上约几秒，不抢 7860 的显存（8 GB 卡上 TTS 已经够挤）。
_ASR_MODEL = None
_ASR_LOCK = threading.Lock()
ASR_DIR = Path(os.environ.get("TTS2_ASR_DIR") or (ROOT.parent / "asr-models"))


def _asr_model():
    """懒加载 whisper 模型（首次约 3 秒装载，模型文件放 asr-models/）。"""
    global _ASR_MODEL
    if _ASR_MODEL is not None:
        return _ASR_MODEL
    with _ASR_LOCK:
        if _ASR_MODEL is not None:
            return _ASR_MODEL
        # 顺序很重要：本机 c10.dll 对加载次序敏感，torch 必须先于 ctranslate2，
        # 否则 import 阶段直接 OSError 1114（Windows DLL 初始化失败）
        import torch  # noqa: F401
        from faster_whisper import WhisperModel

        local = ASR_DIR / "faster-whisper-small"
        target = str(local) if local.is_dir() else os.environ.get("ASR_MODEL", "small")
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        _ASR_MODEL = WhisperModel(
            target,
            device=os.environ.get("ASR_DEVICE", "cpu"),
            compute_type=os.environ.get("ASR_COMPUTE", "int8"),
            download_root=str(ASR_DIR),
        )
        return _ASR_MODEL


def _load_16k(path: Path):
    """读成 16 kHz 单声道 float32。

    faster-whisper 默认走 PyAV 解码，而本机 av 19 的 `open()` 已经不接受
    `metadata_errors` 参数（1.2.1 还在传），直接 TypeError。项目本来就用
    soundfile 处理音频，这里绕开 PyAV：读出来自己线性插值到 16 k——
    ASR 对重采样精度不敏感，不值得为它再引一个 resampy。
    """
    import numpy as np
    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    if sr == 16000:
        return mono
    n = max(1, int(round(len(mono) * 16000 / sr)))
    x = np.linspace(0, len(mono) - 1, n, dtype=np.float64)
    return np.interp(x, np.arange(len(mono)), mono).astype(np.float32)


def _transcribe(path: Path) -> dict:
    """把一段参考音频转成文字。模型对短音频的标点略稀，交给用户校对。"""
    model = _asr_model()
    started = time.time()
    audio = _load_16k(path)
    segments, info = model.transcribe(audio, language="zh",
                                      beam_size=5, vad_filter=True)
    text = "".join(s.text for s in segments).strip()
    if not text:
        # VAD 会把「带片头音乐 / 过短」的整段判成非人声直接返回空；
        # 这时关掉 VAD 再来一遍，宁可多识别出噪声也不能让文稿一片空白
        segments, info = model.transcribe(audio, language="zh",
                                          beam_size=5, vad_filter=False)
        text = "".join(s.text for s in segments).strip()
    return {
        "text": text,
        "language": info.language,
        "duration": round(float(info.duration), 2),
        "seconds": round(time.time() - started, 1),
    }


def _safe_int(value, default: int) -> int:
    """把表单里的字符串转 int，非法值回落到默认。

    上游已经出完音频之后才在构造响应时抛 ValueError，会直接断连：
    GPU 白跑一次，用户什么也看不到。
    """
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


SAMPLE_RATE = 24000
_LOCK = threading.RLock()  # 可重入：读改写块可嵌套 _save
_INFER_LOCK = threading.Lock()  # 上游单并发：面板内部串行排队，避免 409 already running


def _friendly_error(msg: str) -> str:
    """把上游英文报错翻译成可执行的人话。"""
    low = str(msg).lower()
    if "already running" in low:
        return "另一条合成正在进行中，请等它完成后重试（面板每次只跑一条，多任务会自动排队）"
    if "does not define a negative prompt" in low:
        return "该音色不支持指令强度（CFG）：请保持 cfg=1，或改用「导向音色」来控制语气"
    if "out of memory" in low or ("cuda" in low and "memory" in low):
        return "GPU 显存不足：请缩短文本、减少批量条数，或关闭其他占用 GPU 的程序"
    if "ref_audio and ref_text" in low or "ref_audio_path and ref_text" in low:
        return "参考音频和文字稿必须成对提供（缺一不可）"
    return str(msg)[:300]


# ---------- W2-3 质量管线 ----------
def _dict_path() -> Path:
    """按调用时求值：测试把 DATA 指向临时目录后，词典也要跟着走。

    原先在 import 期绑定，测试只会重定向 DATA/REFS/AUDIOS，词典仍写生产
    data/dictionary.json——跑一次测试就往用户词典里塞条目。
    """
    return DATA / "dictionary.json"


def _load_dict():
    """读取发音词典（缺失或损坏都返回空词典，不阻塞服务）。"""
    try:
        from tts_pipeline.pron_dict import PronunciationDict
        return PronunciationDict.load(_dict_path())
    except Exception:
        class _Empty:
            entries: list = []

            def apply(self, text, scope="global"):
                return text, []

            # 调用方还用了这两个：tts_pipeline 不可用时不能抛 AttributeError
            # （那会把连接直接断掉，用户看不到任何提示）。
            def to_list(self):
                return []

            def remove(self, word, scope="global"):
                return False
        return _Empty()


def _save_dict(d) -> None:
    d.save(_dict_path())


def _apply_text_pipeline(text: str, norm_flag: str, scope: str = "global"):
    """文本规范化 + 发音词典。

    norm_flag:
      "" / "1" → 不改写文本（"1" 仅表示注入普通话指令，由合成端处理）
      "2"      → 走真实规范化管线
    返回 (新文本, 变更列表[{before, after, kind}])
    """
    if norm_flag != "2" or not text:
        return text, []
    changes: list[dict[str, str]] = []
    out = text
    try:
        from tts_pipeline.text_norm import normalize_text
        # 产品是中文优先（PRD A1）：只要出现汉字、或完全没有拉丁字母
        # （例如裸金额 "¥1,299.50"），就按中文读，不要误判成英文。
        import re as _re
        has_cjk = bool(_re.search(r"[一-鿿]", out))
        has_latin_word = bool(_re.search(r"[A-Za-z]{2,}", out))
        lang = "zh" if (has_cjk or not has_latin_word) else "en"
        result = normalize_text(out, lang=lang)
        out = result.text
        for c in result.changes:
            changes.append({"before": c.before, "after": c.after, "kind": c.kind})
    except Exception as exc:
        # 不能静默吞掉：规范化抛异常时文本会原样下发，用户只会觉得"读了怪"，
        # 而开发和排查都看不到任何信号。把失败写进变更清单让它可见。
        changes.append({"before": "", "after": "",
                        "kind": "normalize-failed", "error": str(exc)[:120]})
    try:
        d = _load_dict()
        if d.entries:
            out, dict_hits = d.apply(out, scope)
            for h in dict_hits:
                changes.append({"before": h.get("original", ""),
                                "after": h.get("replacement", ""),
                                "kind": "dict-" + h.get("kind", "replace")})
    except Exception as exc:
        # 词典失败原来被静默吞掉：用户专门为纠音维护的词表不生效，
        # 他只会听到错的读音，界面上毫无提示。同一函数里 normalize 的失败
        # 是写进 changes 的，这里照同样的口径处理。
        changes.append({"before": "", "after": "",
                        "kind": "dict-failed", "error": str(exc)[:120]})
    return out, changes


# ---------- 持久化（W2-4: 原子写 + 损坏恢复） ----------
# 损坏且无法恢复的文件：写保护名单。
# 以前 _load 解析失败就返回 []，于是「界面上什么都没有」；而下一次任意写入
# 会把这个空列表落盘，把用户全部数据覆盖掉——这才是最致命的一步。
# 现在：先留证据（.corrupt）→ 尝试从 .bak 恢复 → 恢复不了就禁止写入该文件。
_DATA_CORRUPT: set[str] = set()


class _DataCorrupt(RuntimeError):
    """状态文件损坏且没有可用备份：必须让调用方知道。"""


def _log(msg: str) -> None:
    """写一行带时间戳的日志到 stderr（面板的 stdout 是 HTTP 响应体，不能用）。"""
    import sys
    import time as _t
    try:
        sys.stderr.write(f"[{_t.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _load(name: str) -> list:
    p = DATA / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            # R1: 损坏时保留备份，不静默丢弃
            import shutil
            corrupt = p.with_suffix(p.suffix + ".corrupt")
            try:
                shutil.copy2(p, corrupt)
            except Exception:
                pass
            # 新增：能从上一版 .bak 恢复就直接恢复，用户无感
            bak = p.with_suffix(p.suffix + ".bak")
            if bak.is_file():
                try:
                    data = json.loads(bak.read_text(encoding="utf-8"))
                    p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
                    _DATA_CORRUPT.discard(name)
                    _log(f"[data] {name} 损坏，已从 {bak.name} 恢复：{exc}")
                    return data
                except Exception:
                    pass
            # 恢复不了：标记写保护，别让下一次写入把原始数据盖掉
            _DATA_CORRUPT.add(name)
            _log(f"[data] {name} 损坏且无可用备份（{exc}）；"
                 "已保留 .corrupt 副本并禁止写入该文件")
            return []
    return []


def _save(name: str, items: list) -> None:
    with _LOCK:
        target = DATA / name
        if name in _DATA_CORRUPT:
            # 损坏且没有备份：宁可不写，也不把空数据盖回用户的文件上
            raise _DataCorrupt(
                f"{name} 已损坏且没有可用备份，为避免覆盖原始数据已拒绝写入")
        # 写前留一份上一版：写坏了还能回滚（只保留一份，够应急）
        if target.is_file():
            try:
                import shutil
                shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))
                # 带时间戳的历史版本：只靠单个 .bak 的话，一次误删/整批改写就永久追不回
                # （2026-11-27 的现网事故：voices.json 里 8 条音色被一次写操作抹掉）。
                hist_dir = DATA / "_ledger_history" / Path(name).stem
                hist_dir.mkdir(parents=True, exist_ok=True)
                # 精确到微秒：同一秒内连续写盘（批量删角色等）用秒级戳会互相覆盖，
                # 反而丢掉中间版本。再补一个自增后缀彻底避免同名。
                stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                dest = hist_dir / f"{stamp}.json"
                seq = 1
                while dest.exists():
                    dest = hist_dir / f"{stamp}_{seq}.json"
                    seq += 1
                shutil.copy2(target, dest)
                olds = sorted(hist_dir.glob("*.json"))
                for p in olds[:-_HIST_KEEP]:
                    with contextlib.suppress(OSError):
                        p.unlink()
            except Exception:
                pass
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 原子替换：Windows 上 os.replace 是原子操作
        import os
        os.replace(str(tmp), str(target))


class _file_lock:
    """跨进程文件锁（lockfile + O_EXCL 自旋）。

    面板进程与任何其他进程（例如测试进程、外部脚本）同时写同一份 JSON 时，
    没有它就会出现「后写覆盖先写、丢条目」。用 O_CREAT|O_EXCL 原子创建锁文件。
    """

    def __init__(self, name: str, timeout: float = 10.0):
        self.path = DATA / (name + ".lock")
        self.timeout = timeout
        self._fd = None
        self._degraded = False

    STALE_AFTER = 60.0   # 写 JSON 只需毫秒级；60 秒还没释放说明持有者已经没了
    RELEASED = b"released"

    def _is_stale(self) -> bool:
        """锁只可能来自死进程时返回 True。

        用锁龄判断而不是探测 PID：Windows 上 os.kill(pid, 0) 会直接
        TerminateProcess 杀掉对方，拿来做存活检查是事故。
        """
        try:
            return (time.time() - self.path.stat().st_mtime) > self.STALE_AFTER
        except OSError:
            return False

    def _try_clear(self) -> str:
        """尽力清掉锁文件，返回 "unlinked" / "marked" / "failed"。

        某些环境（批量删除保护、只读目录、杀软占用）会让 unlink 抛非 OSError，
        之前这里只捕获 OSError，异常一路冒到请求处理里把连接打断，还会把锁
        永久留在盘上，之后所有写入都会等超时。

        返回值必须区分「真的删掉了」和「只写了已释放标记」：后者文件仍在，
        下一轮还会撞 FileExistsError，若当成成功就会在这里空转不 sleep。
        """
        try:
            self.path.unlink()
            return "unlinked"
        except Exception:
            pass
        try:
            self.path.write_bytes(self.RELEASED)
            return "marked"
        except Exception:
            return "failed"

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                # 已释放的标记文件等于没有持有者，可以接管
                try:
                    if self.path.read_bytes().strip() == self.RELEASED:
                        result = self._try_clear()
                        if result == "unlinked":
                            continue
                        if result == "marked":
                            # 删不掉但能写：退化为非独占获取，宁可互斥弱一点，
                            # 也不要让请求在这里永久空转。
                            sys.stderr.write(
                                f"[panel] 锁无法删除，退化获取: {self.path.name}\u000a")
                            self._degraded = True
                            self._fd = os.open(str(self.path),
                                               os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
                            os.write(self._fd, str(os.getpid()).encode())
                            return self
                except Exception:
                    pass
                if time.time() > deadline:
                    if self._is_stale() and self._try_clear() == "unlinked":
                        continue
                    raise TimeoutError(
                        f"等待 {self.path.name} 超时，可能有其他进程正在写入")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, tb):
        import os
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        # 释放失败不能让请求崩掉：这里兜底成写「已释放」标记，
        # 下个持锁者看到标记会直接接管。
        if self._try_clear() == "failed":
            try:
                sys.stderr.write("[panel] 释放锁失败: " + self.path.name + "\n")
            except Exception:
                pass
        return False


HISTORY_LIMIT = 200   # 历史保留条数上限（只增不减的数组会让每次写入重写整个文件）


def _append_history(item: dict) -> list:
    """追加一条历史，并裁掉超出上限的老条目。

    history.json 原来只增不减：每合成一条都要全量读-改-写整个数组
    （139 条就是 51 KB，几千条时每次写 1.8 MB，写放大是 O(N²)）。
    留最近 200 条足够回看，超出的从记录里去掉（音频文件本身仍留在磁盘上）。
    """
    def _mut(items):
        items.append(item)
        if len(items) > HISTORY_LIMIT:
            del items[:len(items) - HISTORY_LIMIT]
    return _update_json("history.json", _mut)


def _update_json(name: str, mutator) -> list:
    """读-改-写原子操作（R2 修复）：进程内锁 + 跨进程文件锁，整个 RMW 不被打断。

    所有写入都必须走这里。裸 `_load` → 改 → `_save` 在 ThreadingHTTPServer 下
    会让两个并发请求各自读一份再整体覆盖回去，丢掉对方的改动。
    """
    with _LOCK:
        with _file_lock(name):
            items = _load(name)
            result = mutator(items)
            try:
                _save(name, items)  # _LOCK 是 RLock，这里重入安全；写盘只在 _save 一处
            except _DataCorrupt as exc:
                # 数据文件坏了：拒绝写入，但不能把异常抛到 socket 层
                # （那会让浏览器看到连接被掐断，而不是一条能读懂的错误）。
                # 原文件仍在磁盘上（.corrupt 副本也在），用户可以手动救。
                _log(f"[data] 写入被拒绝：{exc}")
                raise
            return result if result is not None else items


# ---------- 工具 ----------
def pcm_to_wav(pcm: bytes, normalize: bool = True) -> bytes:
    """PCM(s16le mono 24k) 封装为 WAV；默认响度归一化（只增益不衰减，峰值限幅 97%）。"""
    if normalize and len(pcm) >= 4:
        import array

        usable = len(pcm) - (len(pcm) % 2)
        a = array.array("h")
        a.frombytes(pcm[:usable])
        if len(a) > 32:
            rms = (sum(x * x for x in a) / len(a)) ** 0.5
            peak = max(abs(x) for x in a)
            target_rms = 0.2 * 32767  # ≈ -14 dBFS，正常对白响度
            if rms > 32:
                gain = target_rms / rms
                # 允许峰值到 120% 后硬限幅：换响度，语音瞬态轻微软削可接受
                if peak > 0 and (32767 * 1.2) / peak < gain:
                    gain = (32767 * 1.2) / peak
                if gain > 1.0:
                    a = array.array(
                        "h",
                        (max(-32767, min(32767, int(x * gain))) for x in a),
                    )
                    pcm = a.tobytes() + pcm[usable:]
    n = len(pcm)
    return (
        struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + n, b"WAVE",
            b"fmt ", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16,
            b"data", n,
        )
        + pcm
    )


def parse_multipart(body: bytes, content_type: str) -> tuple[dict, dict]:
    """极简 multipart/form-data 解析（Python3.13 无 cgi 模块）。"""
    m = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not m:
        return {}, {}
    boundary = b"--" + m.group(1).encode()
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in body.split(boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        head, sep, payload = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        headers = head.decode("utf-8", "replace")
        name_m = re.search(r'name="([^"]+)"', headers)
        if not name_m:
            continue
        name = name_m.group(1)
        fn_m = re.search(r'filename="([^"]*)"', headers)
        if fn_m:
            files[name] = (fn_m.group(1), payload)
        else:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


def resample_pcm(pcm: bytes, speed: float) -> bytes:
    """线性插值变速（speed>1 加快）。1.0 原样返回。"""
    if abs(speed - 1.0) < 1e-6 or len(pcm) < 4:
        return pcm
    import array
    usable = len(pcm) - (len(pcm) % 2)
    a = array.array("h")
    a.frombytes(pcm[:usable])
    n = len(a)
    if n < 2:
        return pcm
    out_len = max(2, int(n / speed))
    out = array.array("h", bytes(2 * out_len))
    step = speed
    for i in range(out_len):
        pos = i * step
        i0 = int(pos)
        if i0 >= n - 1:
            out[i] = a[n - 1]
        else:
            frac = pos - i0
            out[i] = int(a[i0] * (1 - frac) + a[i0 + 1] * frac)
    return out.tobytes()


def wav_duration(data: bytes) -> float:
    """标准 WAV 时长（秒）；非 WAV 返回 -1。"""
    if len(data) < 44 or data[:4] != b"RIFF":
        return -1.0
    pos, rate, ch, bits, dsize = 12, 0, 0, 0, 0
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        csz = struct.unpack_from("<I", data, pos + 4)[0]
        if cid == b"fmt ":
            _, ch, rate, _, _, bits = struct.unpack_from("<HHIIHH", data, pos + 8)
        elif cid == b"data":
            dsize = min(csz, len(data) - pos - 8)
            break
        pos += 8 + csz + (csz & 1)
    if not (rate and ch and bits):
        return -1.0
    return dsize / (rate * ch * bits / 8)


# 参考音频上限：50 MB / 600 秒。超了不让进解码，避免请求被拖死。
REF_MAX_BYTES = 50 * 1024 * 1024
REF_MAX_SECONDS = 600.0
REF_TARGET_SR = 24000


def normalize_ref_audio(data: bytes) -> tuple[bytes, float]:
    """把上传的参考音频归一成 24 kHz / 单声道 / PCM16 WAV，返回 (字节, 时长秒)。

    上游引擎只认标准 WAV，而用户常传 mp3/m4a。这里统一解码重编码：
    解码失败（损坏、伪装扩展名、libsndfile 不支持该容器）抛 ValueError，
    由调用方翻成 400，避免把坏文件写进 refs/ 让合成阶段才炸。
    """
    if len(data) > REF_MAX_BYTES:
        raise ValueError(f"文件过大（{len(data)/1024/1024:.1f} MB > {REF_MAX_BYTES//1024//1024} MB）")
    try:
        import io
        import numpy as np
        import soundfile as sf
        from tts_pipeline.ref_prep import to_mono, resample_linear, write_wav_bytes

        samples, sr = sf.read(io.BytesIO(data), always_2d=True, dtype="float32")
        if samples.size == 0:
            raise ValueError("音频内容为空")
        mono = to_mono(np.asarray(samples))
        if int(sr) != REF_TARGET_SR:
            mono = resample_linear(mono, int(sr), REF_TARGET_SR)
        dur = len(mono) / float(REF_TARGET_SR)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"音频无法解码：{str(exc)[:120]}") from exc
    if dur > REF_MAX_SECONDS:
        raise ValueError(f"参考音频过长（{dur:.0f}s > {REF_MAX_SECONDS:.0f}s）")
    out = write_wav_bytes(mono, REF_TARGET_SR)
    if not out or out[:4] != b"RIFF" or out[8:12] != b"WAVE":
        raise ValueError("转码结果异常，请换一个文件重试")
    return out, round(dur, 3)



def trim_wav_bytes(data: bytes, max_sec: float = 15.0) -> bytes:
    """把 WAV 裁到前 max_sec 秒（重建标准头）。非 WAV 原样返回。"""
    if len(data) < 44 or data[:4] != b"RIFF":
        return data
    pos, rate, ch, bits, dstart, dsize = 12, 0, 0, 0, 0, 0
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        csz = struct.unpack_from("<I", data, pos + 4)[0]
        if cid == b"fmt ":
            _, ch, rate, _, _, bits = struct.unpack_from("<HHIIHH", data, pos + 8)
        elif cid == b"data":
            dstart, dsize = pos + 8, min(csz, len(data) - pos - 8)
            break
        pos += 8 + csz + (csz & 1)
    if not (rate and ch and bits and dstart):
        return data
    frame = max(1, ch * bits // 8)
    keep = min(int(max_sec * rate * ch * bits / 8) // frame * frame, dsize)
    raw = data[dstart:dstart + keep]
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(raw), b"WAVE",
        b"fmt ", 16, 1, ch, rate, rate * ch * bits // 8, frame, bits,
        b"data", len(raw),
    ) + raw


# 占位符文稿（历史遗留：填了但其实没填）——视同缺文稿
_PLACEHOLDER_REF_TEXT = (
    "参考文字稿", "参考文案", "参考稿", "占位", "placeholder",
    "test", "测试文字稿", "待补充", "todo",
)


def _is_placeholder_ref_text(text: str) -> bool:
    """判断参考文稿是否只是占位符（不是真实转写）。"""
    t = (text or "").strip().lower()
    if not t:
        return True
    return any(t == p or t.startswith(p) for p in _PLACEHOLDER_REF_TEXT)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name)[:80]


def _purge_rel_file(rel: str) -> bool:
    """删 data/ 下的单个文件，越界一律拒绝。

    路径来自 JSON 账本，理论上可信；但文件名与角色 id 都可能被塞进 URL，
    所以仍按「resolve 后必须落在 DATA 内」来判，宁可少删也不越界。
    """
    rel = str(rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return False
    try:
        base = DATA.resolve()
        p = (base / rel).resolve()
        if p != base and base not in p.parents:
            return False
        if p.is_file():
            p.unlink()
            return True
    except OSError:
        return False
    return False


def _purge_role_ref_dir(rid: str) -> int:
    """删角色的参考图目录 data/refs/characters/<rid>/，返回删除的文件数。"""
    rid = str(rid or "").strip()
    if not rid or "/" in rid or "\\" in rid or rid in (".", ".."):
        return 0
    d = DATA / "refs" / "characters" / rid
    if not d.is_dir():
        return 0
    n = 0
    try:
        for f in d.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
        d.rmdir()
    except OSError:
        pass
    return n


def _sniff_image_ext(data: bytes) -> str:
    """按文件头认图片类型，不信前端给的扩展名。认不出返回空串。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


# ---------- 面板 HTML ----------
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BreezeTTS Studio</title>
<style>
  :root{
    --bg:#0A0E27; --bg2:#0D1226; --panel:#141A2E; --panel2:#0E1326;
    --line:#262E4A; --line2:#2A3050;
    --text:#F8FAFC; --muted:#9CA8C4; --dim:#64748B;
    --primary:#60A5FA; --primary-d:#3B82F6; --primary-soft:rgba(96,165,250,.14);
    --cta:#F97316; --cta-d:#EA580C; --cta-glow:rgba(249,115,22,.35);
    --ok:#34D399; --ok-soft:rgba(52,211,153,.12);
    --danger:#F87171; --danger-soft:rgba(248,113,113,.12);
    --warn:#FBBF24;
    --radius:14px; --radius-s:10px;
    --shadow:0 10px 30px rgba(2,6,23,.45);
    --font:"Fira Sans","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
    --mono:"Fira Code",Consolas,"Courier New",monospace;
  }
  *{box-sizing:border-box; margin:0; padding:0}
  html{font-size:16px}
  body{
    background:
      radial-gradient(1100px 500px at 85% -10%, rgba(59,130,246,.10), transparent 60%),
      radial-gradient(900px 420px at -10% 110%, rgba(249,115,22,.07), transparent 55%),
      var(--bg);
    color:var(--text); min-height:100vh; font-family:var(--font); line-height:1.6;
    -webkit-font-smoothing:antialiased;
    display:flex; flex-direction:column; height:100vh; overflow:hidden;
  }
  code,.mono{font-family:var(--mono)}
  ::selection{background:rgba(96,165,250,.35)}
  ::-webkit-scrollbar{width:9px; height:9px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:#2A3050; border-radius:8px; border:2px solid var(--panel)}
  ::-webkit-scrollbar-thumb:hover{background:#3B82F6}

  button{font:inherit; cursor:pointer; border:none; border-radius:var(--radius-s);
    transition:background-color .2s, border-color .2s, color .2s, box-shadow .2s, opacity .2s}
  button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible{
    outline:2px solid var(--primary); outline-offset:2px}
  input, select, textarea{
    font:inherit; color:var(--text); background:var(--panel2);
    border:1px solid var(--line); border-radius:var(--radius-s);
    padding:11px 13px; width:100%; transition:border-color .2s, box-shadow .2s}
  input::placeholder, textarea::placeholder{color:var(--dim)}
  input:hover, select:hover, textarea:hover{border-color:#34406B}
  input:focus, select:focus, textarea:focus{
    border-color:var(--primary); box-shadow:0 0 0 3px rgba(96,165,250,.16); outline:none}
  textarea{resize:vertical; min-height:132px; line-height:1.7}
  label{display:block; font-size:.78rem; font-weight:600; letter-spacing:.06em;
    text-transform:uppercase; color:var(--muted); margin:12px 0 6px}

  /* ---------- header ---------- */
  header{
    display:flex; align-items:center; gap:14px; padding:13px 24px;
    flex-shrink:0; position:sticky; top:0; z-index:30;
    background:rgba(10,14,39,.86); backdrop-filter:blur(14px);
    border-bottom:1px solid var(--line);
  }
  .brand{display:flex; align-items:center; gap:11px}
  .brand-mark{
    width:38px; height:38px; border-radius:11px; display:flex; align-items:center; justify-content:center;
    background:linear-gradient(135deg,#3B82F6,#7C3AED); box-shadow:0 6px 18px rgba(59,130,246,.4)}
  .brand-mark svg{width:20px; height:20px}
  .brand h1{font-size:1.12rem; font-weight:700; letter-spacing:.01em}
  .brand h1 .grad{
    background:linear-gradient(90deg,#60A5FA,#A78BFA);
    -webkit-background-clip:text; background-clip:text; color:transparent}
  .pill{
    display:inline-flex; align-items:center; gap:8px; padding:7px 14px;
    border-radius:999px; border:1px solid var(--line); background:rgba(14,19,38,.8);
    font-size:.8rem; color:var(--muted)}
  .pill.mono{font-family:var(--mono); font-size:.74rem}
  .dot{width:8px; height:8px; border-radius:50%; background:var(--dim); flex-shrink:0}
  .dot.on{background:var(--ok); box-shadow:0 0 8px rgba(52,211,153,.7)}
  .dot.off{background:var(--danger); box-shadow:0 0 8px rgba(248,113,113,.6)}
  .spacer{flex:1}
  .mode-switch{
    display:flex; border:1px solid var(--line); border-radius:12px; overflow:hidden;
    background:var(--panel2)}
  .mode-switch button{
    min-height:40px; padding:8px 18px; background:transparent; color:var(--muted); font-size:.88rem;
    border-radius:0}
  .mode-switch button+button{border-left:1px solid var(--line)}
  .mode-switch button:hover{color:var(--text)}
  .mode-switch button.active{background:var(--primary-d); color:#fff; font-weight:600}

  /* ---------- layout ---------- */
  main{
    display:grid; grid-template-columns:312px minmax(0,1fr) 336px; gap:18px;
    padding:20px 24px 24px; max-width:1620px; margin:0 auto;
    flex:1; min-height:0; overflow:hidden}
  .col{display:flex; flex-direction:column; gap:18px; min-width:0; min-height:0; overflow:hidden}
  .col.center{overflow-y:auto}
  .card{
    background:linear-gradient(165deg, var(--panel), #11152A 70%);
    border:1px solid var(--line); border-radius:var(--radius);
    padding:18px; box-shadow:var(--shadow);
    display:flex; flex-direction:column; min-height:0}
  .col>.card{flex:1 1 0}
  .card h2{
    font-size:.92rem; font-weight:700; letter-spacing:.02em; margin-bottom:14px;
    display:flex; align-items:center; gap:9px; justify-content:space-between}
  .card h2 .sub{font-size:.74rem; color:var(--dim); font-weight:500; letter-spacing:.03em}
  .card h2 .h2-left{display:flex; align-items:center; gap:9px}
  .card h2 svg{width:17px; height:17px; color:var(--primary)}

  /* ---------- buttons ---------- */
  .btn{
    min-height:44px; padding:10px 16px; background:var(--panel2); color:var(--text);
    border:1px solid var(--line2); font-size:.9rem; font-weight:500;
    display:inline-flex; align-items:center; justify-content:center; gap:8px}
  .btn:hover{border-color:var(--primary); color:var(--primary); background:var(--primary-soft)}
  .btn-cta{
    background:linear-gradient(135deg, #F97316, #EA580C); color:#fff; font-weight:700;
    min-width:158px; min-height:48px; font-size:.98rem;
    box-shadow:0 6px 20px var(--cta-glow)}
  .btn-cta:hover{background:linear-gradient(135deg,#FB8B3C,#F0630C); color:#fff;
    box-shadow:0 8px 26px rgba(249,115,22,.5); transform:translateY(-1px)}
  .btn-cta[disabled]{opacity:.55; cursor:not-allowed; transform:none; box-shadow:none}
  .btn-danger{color:var(--danger); border-color:rgba(248,113,113,.35)}
  .btn-danger:hover{border-color:var(--danger); background:var(--danger-soft); color:var(--danger)}
  .icon{width:18px; height:18px; flex-shrink:0}
  .iconbtn{
    width:34px; height:34px; display:inline-flex; align-items:center; justify-content:center;
    background:transparent; color:var(--dim); border:1px solid transparent; border-radius:9px; flex-shrink:0}
  .iconbtn:hover{color:var(--primary); border-color:var(--line2); background:var(--primary-soft)}
  .iconbtn.del:hover{color:var(--danger); border-color:rgba(248,113,113,.4); background:var(--danger-soft)}

  /* ---------- lists ---------- */
  .list{display:flex; flex-direction:column; gap:8px; flex:1 1 auto; min-height:0; overflow-y:auto; padding-right:2px}
  .item{
    display:flex; align-items:center; gap:9px; padding:11px 12px; border-radius:12px;
    border:1px solid var(--line); background:var(--panel2); cursor:pointer;
    transition:border-color .2s, background-color .2s}
  .item:hover{border-color:var(--primary); background:rgba(96,165,250,.07)}
  .item.active{border-color:var(--primary); background:var(--primary-soft);
    box-shadow:inset 0 0 0 1px rgba(96,165,250,.35)}
  .item .name{font-size:.88rem; font-weight:600}
  .item .meta{font-size:.71rem; color:var(--dim); margin-top:1px}
  .item .grow{flex:1; min-width:0}
  .item .name,.item .meta{white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
  .tag{
    font-size:.66rem; font-weight:700; letter-spacing:.05em; padding:3px 9px;
    border-radius:999px; border:1px solid var(--line); color:var(--muted); flex-shrink:0}
  .tag.design{color:#A78BFA; border-color:rgba(167,139,250,.4); background:rgba(167,139,250,.1)}
  .tag.clone{color:var(--ok); border-color:rgba(52,211,153,.4); background:var(--ok-soft)}
  .tag.direction{color:var(--cta); border-color:rgba(249,115,22,.4); background:rgba(249,115,22,.1)}
  .empty{
    color:var(--dim); font-size:.84rem; padding:18px 10px; text-align:center;
    border:1px dashed var(--line); border-radius:12px}

  /* ---------- chips ---------- */
  .chips{display:flex; gap:8px; flex-wrap:wrap; margin:2px 0 4px; min-height:40px; align-items:center}
  .chip{
    padding:8px 14px; border-radius:999px; border:1px solid var(--line2);
    background:var(--panel2); font-size:.82rem; cursor:pointer; min-height:38px;
    display:inline-flex; align-items:center; gap:6px; transition:border-color .2s, color .2s, background-color .2s}
  .chip:hover{border-color:var(--primary); color:var(--primary)}
  .chip.active{background:var(--primary-soft); border-color:var(--primary); color:var(--primary); font-weight:600}
  .chip.flat{cursor:default; opacity:.75}

  /* ---------- status ---------- */
  .status{
    font-size:.86rem; min-height:1.5em; margin-top:12px;
    padding:10px 14px; border-radius:var(--radius-s); border:1px solid transparent;
    display:flex; align-items:center; gap:9px}
  .status:empty{display:none}
  .status.ok{color:var(--ok); background:var(--ok-soft); border-color:rgba(52,211,153,.25)}
  .status.err{color:var(--danger); background:var(--danger-soft); border-color:rgba(248,113,113,.3)}
  .status.busy{color:var(--primary); background:var(--primary-soft); border-color:rgba(96,165,250,.28)}
  .spinner{
    width:15px; height:15px; border-radius:50%; flex-shrink:0;
    border:2px solid rgba(96,165,250,.25); border-top-color:var(--primary);
    animation:spin .8s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  @media (prefers-reduced-motion:reduce){
    *{transition:none !important; animation:none !important}
    .spinner{border-top-color:var(--primary); border-right-color:var(--primary)}
  }
  .hint{font-size:.76rem; color:var(--dim); margin-top:6px; line-height:1.55}
  .hint code{color:var(--muted); background:var(--panel2); padding:1px 6px; border-radius:6px; font-size:.72rem}

  /* ---------- player ---------- */
  .player{
    display:flex; align-items:center; gap:12px; margin-top:14px; flex-wrap:wrap}
  audio{width:100%; min-height:44px; filter:saturate(.92)}
  .player-box{
    margin-top:14px; padding:12px; border-radius:12px;
    background:var(--panel2); border:1px solid var(--line)}
  .progress-track{
    height:4px; border-radius:4px; background:var(--line); overflow:hidden; margin-top:12px}
  .progress-track .bar{
    height:100%; width:0; border-radius:4px;
    background:linear-gradient(90deg, var(--primary), #A78BFA);
    transition:width .5s ease}

  /* ---------- history ---------- */
  .hist{display:flex; flex-direction:column; gap:8px; flex:1 1 auto; min-height:0; overflow-y:auto; padding-right:2px}
  .hist .item{cursor:default}
  .hist .item.fresh{border-color:var(--ok); background:var(--ok-soft)}
  .hist .play{color:var(--primary)}

  /* ---------- dialogs ---------- */
  dialog{
    border:1px solid var(--line2); border-radius:16px; background:var(--panel);
    color:var(--text); width:min(540px, 92vw); padding:22px; box-shadow:0 24px 80px rgba(2,6,23,.7)}
  dialog::backdrop{background:rgba(4,6,16,.72); backdrop-filter:blur(3px)}
  dialog h3{margin-bottom:4px; font-size:1.08rem; font-weight:700}
  .dlg-actions{display:flex; gap:10px; justify-content:flex-end; margin-top:18px}
  .filelabel{
    display:flex; align-items:center; gap:10px; padding:13px;
    border:1px dashed var(--line2); border-radius:var(--radius-s);
    cursor:pointer; color:var(--muted); font-size:.85rem; min-height:46px;
    transition:border-color .2s, color .2s}
  .filelabel:hover{border-color:var(--primary); color:var(--primary)}
  .filelabel input{display:none}
  .row{display:flex; gap:12px; flex-wrap:wrap}
  .row>*{flex:1; min-width:130px}

  @media (max-width:1220px){
    main{grid-template-columns:1fr; height:auto; overflow:visible}
    .col{overflow:visible}
    .list,.hist{max-height:320px}
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="brand-mark" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round">
        <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/>
      </svg>
    </div>
    <h1>Breeze<span class="grad">TTS</span> Studio</h1>
  </div>
  <div class="pill"><span class="dot" id="apiDot"></span><span id="apiText">连接中…</span></div>
  <div class="pill mono" id="modelInfo" title="model_digest">—</div>
  <div class="spacer"></div>
  <div class="mode-switch" role="tablist" aria-label="合成模式">
    <button id="modeSingle" class="active" role="tab" aria-selected="true">单条合成</button>
    <button id="modeBatch" role="tab" aria-selected="false">批量合成</button>
  </div>
</header>

<main>
  <!-- 左：音色库 + 角色库 -->
  <div class="col">
    <section class="card">
      <h2>
        <span class="h2-left">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg>
          音色库
        </span>
        <span class="sub" id="voiceCount">0 个</span>
      </h2>
      <div class="row" style="margin-bottom:12px">
        <button class="btn" onclick="openVoiceDlg('design')" title="用自然语言描述创造全新音色">+ 设计</button>
        <button class="btn" onclick="openVoiceDlg('clone')" title="上传自己的声音直接克隆">+ 克隆</button>
        <button class="btn" onclick="openVoiceDlg('direction')" title="克隆声音并控制语气">+ 导向</button>
      </div>
      <div class="list" id="voiceList"><div class="empty">暂无音色<br>点上方按钮创建第一个</div></div>
    </section>
    <section class="card">
      <h2>
        <span class="h2-left">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
          角色库
        </span>
        <span class="sub" id="roleCount">0 个</span>
      </h2>
      <button class="btn" style="width:100%;margin-bottom:12px" onclick="openRoleDlg(null)">+ 新建角色</button>
      <div class="list" id="roleList"><div class="empty">暂无角色<br>角色 = 名字 + 音色 + 默认参数</div></div>
    </section>
  </div>

  <!-- 中：工作台 -->
  <div class="col center">
    <section class="card">
      <h2>
        <span class="h2-left">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
          合成工作台
        </span>
      </h2>
      <div class="chips" id="activeChips"></div>
      <div class="player-box" id="instrPreviewWrap" style="margin:0 0 6px;padding:10px 13px">
        <div class="hint" style="margin:0 0 4px">本次将下发的指令（合成前可核对，杜绝「莫名带方言」）：</div>
        <div class="mono" id="instrPreview" style="font-size:.78rem;line-height:1.7;color:var(--muted)">（无指令：默认/纯克隆音色）</div>
      </div>

      <label for="emotionSelect">情感 / 语气（可选，让声音带情绪与风格）</label>
      <select id="emotionSelect" onchange="onEmotionChange()">
        <option value="" data-instr="">平静自然（默认）</option>
        <optgroup label="情感">
          <option value="em_gentle" data-instr="语气温柔亲切，情感细腻">温柔亲切</option>
          <option value="em_happy" data-instr="语气开心愉悦，充满活力">开心愉悦</option>
          <option value="em_excited" data-instr="语气激动热情，感染力强">激动热情</option>
          <option value="em_sad" data-instr="语气悲伤低沉，略带伤感">悲伤低沉</option>
          <option value="em_angry" data-instr="语气愤怒有力，掷地有声">愤怒有力</option>
        </optgroup>
        <optgroup label="风格">
          <option value="st_story" data-instr="像讲故事一样娓娓道来，节奏自然，有画面感">讲故事</option>
          <option value="st_news" data-instr="标准新闻播音腔，字正腔圆，客观庄重">新闻播音</option>
          <option value="st_recite" data-instr="字正腔圆，清晰咬字，抑扬顿挫，节奏从容">朗诵</option>
          <option value="st_classic" data-instr="朗诵古文，抑扬顿挫，节奏从容，字字清晰，古韵悠然">古文朗诵</option>
        </optgroup>
      </select>
      <div class="hint" id="emotionHint">情感与风格由自然语言指令驱动；克隆音色选择后自动解锁指令强度</div>

      <label for="accentSelect">口音 / 方言（可选，随文本语言生效）</label>
      <select id="accentSelect" onchange="onAccentChange()">
        <option value="" data-instr="">无口音（默认）</option>
        <optgroup label="中国方言">
          <option value="cn_guizhou" data-instr="用浓重的贵州方言说话，明显的贵州方言语调、咬字和用词，腔调要重">贵州话</option>
          <option value="cn_dongbei" data-instr="用浓重的东北方言说话，豪爽直率，明显的东北语调起伏和用词">东北话</option>
          <option value="cn_sichuan" data-instr="用浓重的四川方言说话，明显的川渝语调和韵味，腔调要重">四川话</option>
          <option value="cn_yue" data-instr="用浓重的粤语说话，地道广东话，明显的粤语声调和用词">粤语（广东话）</option>
          <option value="cn_henan" data-instr="用浓重的河南方言说话，明显的中原官话韵味和语调">河南话</option>
          <option value="cn_shaanxi" data-instr="用浓重的陕西方言说话，明显的关中话韵味和豪迈语调">陕西话</option>
          <option value="cn_shanghai" data-instr="用浓重的上海话（吴语）说话，明显的吴侬软语语调">上海话</option>
          <option value="cn_beijing" data-instr="用浓重的北京话说话，明显的儿化音和松弛京腔">北京话</option>
          <option value="cn_taiwan" data-instr="用明显的台湾腔说话，软糯亲切，句尾带轻柔语气词">台湾腔</option>
          <option value="cn_hunan" data-instr="用浓重的湖南方言说话，明显的湖南口音语调和用词">湖南话</option>
          <option value="cn_shandong" data-instr="用浓重的山东方言说话，明显的憨厚敦实语调">山东话</option>
          <option value="cn_minnan" data-instr="用浓重的闽南语说话，明显的闽台方言声调和用词">闽南话</option>
        </optgroup>
        <optgroup label="全球口音">
          <option value="en_us" data-instr="Speak with a General American accent">美式英语</option>
          <option value="en_gb" data-instr="Speak with a British Received Pronunciation accent">英式英语</option>
          <option value="en_southern" data-instr="Speak with a warm American Southern drawl">美国南方口音</option>
          <option value="en_scottish" data-instr="Speak with a Scottish accent">苏格兰口音</option>
          <option value="en_indian" data-instr="Speak with an Indian English accent">印度英语</option>
          <option value="en_australian" data-instr="Speak with an Australian accent">澳洲英语</option>
          <option value="en_newyork" data-instr="Speak with a New York accent">纽约口音</option>
          <option value="en_french" data-instr="Speak English with a French accent">法式口音英语</option>
          <option value="en_japanese" data-instr="Speak English with a Japanese accent">日式口音英语</option>
          <option value="en_spanish" data-instr="Speak English with a Spanish accent">西式口音英语</option>
        </optgroup>
      </select>
      <div class="hint" id="accentHint">口音由自然语言指令驱动；「克隆音色 + 口音」自动变为导向合成（可用指令强度）。支持的表演标记：<code>[笑]</code> <code>[咳嗽]</code> <code>[清嗓子]</code> <code>[叹气]</code></div>

      <label for="synthText">合成文本</label>
      <textarea id="synthText" placeholder="输入要合成的文本。表演标记：[笑] [咳嗽] [清嗓子] [叹气]；停顿标记：[停顿0.5] 插入半秒静音"></textarea>
      <textarea id="batchText" placeholder="每行一条文本，逐行批量合成（建议先用 2-3 行试跑，GPU 显存有限）" style="display:none"></textarea>
      <div class="hint" id="lenHint"></div>

      <div class="row" style="margin-top:4px">
        <div><label for="cfgScale">指令强度 cfg_scale</label>
          <input id="cfgScale" type="number" min="1" max="8" step="0.5" value="4">
          <div class="hint" id="cfgHint"></div></div>
        <div><label for="seed">随机种子 seed</label>
          <input id="seed" type="number" value="42"></div>
        <div><label for="speedSel">语速</label>
          <select id="speedSel">
            <option value="0.75">0.75× 慢速</option>
            <option value="0.85">0.85×</option>
            <option value="1" selected>1.0× 正常</option>
            <option value="1.15">1.15×</option>
            <option value="1.3">1.3×</option>
            <option value="1.5">1.5× 快速</option>
          </select></div>
        <div id="maxTokensWrap" style="display:none"><label for="maxTokens">最大生成 token</label>
          <input id="maxTokens" type="number" min="50" max="1500" step="50" value="600"></div>
      </div>

      <div class="row" style="margin-top:4px">
        <div><label for="langSel">文本语种</label>
          <select id="langSel">
            <option value="">Auto 自动识别（推荐）</option>
            <option value="Chinese">中文</option>
            <option value="English">English</option>
            <option value="Japanese">日本語</option>
            <option value="Korean">한국어</option>
            <option value="German">Deutsch</option>
            <option value="French">Français</option>
            <option value="Russian">Русский</option>
            <option value="Portuguese">Português</option>
            <option value="Spanish">Español</option>
            <option value="Italian">Italiano</option>
          </select></div>
        <div><label for="instructPolicySel">克隆音色遇到情绪 / 方言指令时</label>
          <select id="instructPolicySel">
            <option value="voice">保留克隆音色（指令不生效）</option>
            <option value="instruct">让指令生效（改用原生预设音色）</option>
          </select>
          <div class="hint">Qwen3-TTS 的克隆模型不接受指令；选「让指令生效」会换用 CustomVoice 预设音色承载方言与情绪。</div></div>
      </div>

      <div class="player" id="playerBox">
        <button class="btn btn-cta" id="synthBtn" onclick="synthesize()">
          <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
          开始合成
        </button>
        <button class="btn" id="downloadBtn" onclick="downloadCurrent()" style="display:none">
          <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          下载 WAV
        </button>
      </div>

      <div class="status" id="synthStatus"></div>
      <div class="progress-track" id="progTrack" style="display:none"><div class="bar" id="progBar" style="animation:none;width:0%"></div></div>
      <div class="hint" id="segProgress"></div>
      <div class="hint" id="lastInstrWrap" style="display:none">本次指令：<span class="mono" id="lastInstr"></span></div>
      <div class="hint" id="engineNoteWrap" style="display:none;color:var(--warn,#b8860b)">引擎提示：<span id="engineNote"></span></div>

      <div class="player-box" id="playerBoxWrap" style="display:none">
        <audio id="player" controls></audio>
      </div>
    </section>
  </div>

  <!-- 右：历史 -->
  <div class="col">
    <section class="card">
      <h2>
        <span class="h2-left">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>
          合成历史
        </span>
        <span class="sub" id="histCount">0 条</span>
      </h2>
      <div class="hist" id="histList"><div class="empty">还没有合成记录<br>合成结果会自动保存在这里</div></div>
    </section>
  </div>
</main>

<!-- 音色编辑弹窗 -->
<dialog id="voiceDlg">
  <h3 id="voiceDlgTitle">新建音色</h3>
  <div class="hint" id="voiceDlgHint"></div>
  <input type="hidden" id="voiceId">
  <input type="hidden" id="voiceType">
  <label for="voiceName">音色名称</label>
  <input id="voiceName" placeholder="如：温柔小雅">
  <div id="voiceInstrWrap">
    <label for="voiceInstr">声音描述 / 方向指令（自然语言）</label>
    <textarea id="voiceInstr" placeholder="一位温柔自信的年轻女性，声音清晰，语气亲切，表达轻快而富有感染力。"></textarea>
  </div>
  <div id="voiceRefWrap" style="display:none">
    <label>参考音频（自己的声音，干净无噪音，几秒即可）</label>
    <label class="filelabel">
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
      <span id="voiceRefName">选择 wav 文件…</span>
      <input type="file" id="voiceRef" accept=".wav,.mp3,.flac,audio/*">
    </label>
    <label for="voiceRefText">参考音频的准确文字稿（必填，一字不差）</label>
    <textarea id="voiceRefText" placeholder="参考音频里说的完整内容，包括重复的部分"></textarea>
    <div class="hint">克隆音色不需要指令；「导向音色」在克隆基础上再加语气指令。</div>
    <div class="hint" id="voiceRefDur"></div>
    <button class="btn" id="voiceTrimBtn" style="display:none;margin-top:8px" onclick="trimRef()">裁剪参考音频到前 15 秒（提速）</button>
  </div>
  <div class="row">
    <div><label for="voiceCfg">默认 cfg_scale</label>
      <input id="voiceCfg" type="number" min="1" max="8" step="0.5" value="4"></div>
  </div>
  <div class="status err" id="voiceDlgErr" style="display:none"></div>
  <div class="dlg-actions">
    <button class="btn" onclick="voiceDlg.close()">取消</button>
    <button class="btn btn-cta" onclick="saveVoice()">保存音色</button>
  </div>
</dialog>

<!-- 角色编辑弹窗 -->
<dialog id="roleDlg">
  <h3 id="roleDlgTitle">新建角色</h3>
  <input type="hidden" id="roleId">
  <label for="roleName">角色名称</label>
  <input id="roleName" placeholder="如：故事旁白-小雅">
  <label for="roleDesc">角色简介</label>
  <input id="roleDesc" placeholder="这个人设负责什么场景">
  <label for="roleArch">原型（男女老少）</label>
  <select id="roleArch">
    <option value="custom">自定义</option>
    <option value="narrator">旁白</option>
    <option value="neutral">通用人声</option>
    <option value="elder_m">老年男</option>
    <option value="elder_f">老年女</option>
    <option value="teen_m">少年男</option>
    <option value="teen_f">少年女</option>
    <option value="adult_m">成年男</option>
    <option value="adult_f">成年女</option>
  </select>
  <label>视频参考图（可选，绑定到本角色）</label>
  <div class="ref-row" style="display:flex;gap:10px;align-items:center">
    <img id="roleRefThumb" alt="" style="display:none;width:56px;height:56px;object-fit:cover;border-radius:8px;border:1px solid var(--line)">
    <input id="roleRefFile" type="file" accept="image/png,image/jpeg,image/webp">
  </div>
  <label class="consent-line" style="display:flex;gap:8px;align-items:flex-start;font-size:12px;color:var(--muted)"><input id="roleRefConsent" type="checkbox" style="margin-top:2px">
    我确认拥有这张肖像的使用授权；云端出片时会把它上传给第三方模型服务</label>
  <div style="margin-top:6px">
    <button class="btn" type="button" onclick="uploadRoleRef()">上传参考图</button>
  </div>
  <div class="status err" id="roleRefErr" style="display:none"></div>
  <label for="roleVoice">绑定音色</label>
  <select id="roleVoice"></select>
  <div class="row">
    <div><label for="roleCfg">默认 cfg_scale</label>
      <input id="roleCfg" type="number" min="1" max="8" step="0.5" value="4"></div>
    <div><label for="roleSeed">默认 seed</label>
      <input id="roleSeed" type="number" value="42"></div>
  </div>
  <div class="status err" id="roleDlgErr" style="display:none"></div>
  <div class="dlg-actions">
    <button class="btn" onclick="roleDlg.close()">取消</button>
    <button class="btn btn-cta" onclick="saveRole()">保存角色</button>
  </div>
</dialog>

<script>
const $ = (id) => document.getElementById(id);
const state = {
  voices: [], roles: [], history: [],
  voiceId: null, roleId: null, mode: "single",
  lastWavUrl: null, lastWavName: null, refFile: null,
  accent: "", accentLabel: "", histOffset: 0,
  emotion: "", emotionLabel: "", segPlaying: 0,
  segDefs: [], segFiles: [], failedIdx: [], engineNote: "",
};

const TYPE_LABEL = { design:"设计", clone:"克隆", direction:"导向" };
const ARCH_LABEL = {
  narrator:"旁白", neutral:"通用人声", custom:"",
  elder_m:"老年男", elder_f:"老年女", teen_m:"少年男", teen_f:"少年女",
  adult_m:"成年男", adult_f:"成年女",
};

function api(path, opts) {
  return fetch(path, opts).then(async (r) => {
    if (!r.ok) {
      let msg = r.statusText;
      try { msg = (await r.json()).detail || msg; } catch {}
      throw new Error(msg);
    }
    return r.json();
  });
}

function friendlyErr(msg) {
  const s = String(msg || "");
  if (/already running/i.test(s)) return "另一条合成正在进行，请等它完成后重试（多任务会自动排队）";
  if (/negative prompt/i.test(s)) return "该音色不支持指令强度（CFG），保持 cfg=1 或改用「导向音色」";
  if (/out of memory|cuda/i.test(s)) return "GPU 显存不足：请缩短文本或关闭占用 GPU 的程序";
  if (/不可达|refused|Failed to fetch/i.test(s)) return "合成服务（7860）未响应，请确认 API 已启动";
  return s;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;",
  }[c]));
}

function setStatus(msg, kind) {
  const el = $("synthStatus");
  if (!msg) { el.textContent = ""; el.className = "status"; return; }
  const spin = kind === "busy" ? '<span class="spinner"></span>' : "";
  el.innerHTML = spin + "<span>" + escapeHtml(msg) + "</span>";
  el.className = "status " + (kind || "ok");
}

/* ---------- 状态栏 ---------- */
async function refreshModel() {
  try {
    const m = await api("/api/model");
    $("apiDot").className = "dot on";
    $("apiText").textContent = "API 在线";
    $("modelInfo").textContent = (m.model_digest||"").slice(0,8) + " · " + m.frame_rate + " fps · ≤" + m.max_new_tokens + " tok";
    $("modelInfo").title = "model_digest: " + (m.model_digest||"");
  } catch (e) {
    $("apiDot").className = "dot off";
    $("apiText").textContent = "API 离线（7860）";
    $("modelInfo").textContent = "—";
  }
}

/* ---------- 音色 ---------- */
async function refreshVoices() {
  state.voices = await api("/api/voices");
  $("voiceCount").textContent = state.voices.length + " 个";
  const box = $("voiceList");
  if (!state.voices.length) {
    box.innerHTML = '<div class="empty">暂无音色<br>点上方按钮创建第一个</div>';
    return;
  }
  const vShown = state.voices.slice(0, 10);
  const vMore = state.voices.length > 10
    ? '<div class="hint" style="text-align:center">仅显示前 10 个（共 ' + state.voices.length + '）</div>' : "";
  box.innerHTML = vShown.map((v) => `
    <div class="item ${state.voiceId===v.id?"active":""}" onclick="pickVoice('${v.id}')" role="button" tabindex="0">
      <div class="grow"><div class="name">${escapeHtml(v.name)}</div>
      <div class="meta">${escapeHtml((v.instruction||v.ref_text||"").slice(0,26))}</div></div>
      <span class="tag ${v.type}">${TYPE_LABEL[v.type]||v.type}</span>
      <button class="iconbtn" title="编辑音色" onclick="event.stopPropagation();editVoice('${v.id}')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>
      </button>
      <button class="iconbtn del" title="删除音色" onclick="event.stopPropagation();deleteVoice('${v.id}')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
      </button>
    </div>`).join("") + vMore;
}

function pickVoice(id) {
  state.voiceId = state.voiceId === id ? null : id;
  state.roleId = null;
  syncCfgByVoice();
  renderChips(); refreshVoices(); refreshRoles();
  updateInstrPreview();
}

function syncCfgByVoice() {
  const v = state.voices.find((x) => x.id === state.voiceId);
  const el = $("cfgScale"), hint = $("cfgHint");
  const hasExtra = !!extraInstruction();
  if (v && v.type === "clone" && !hasExtra) {
    el.value = 1;
    hint.textContent = "克隆默认走标准普通话（cfg=1 已够用）；选「情感」可加强语气，选「口音」切换方言";
  } else if (v && v.type === "clone" && hasExtra) {
    if (parseFloat(el.value) === 1) el.value = 4;
    hint.textContent = "克隆 + 情感/口音 = 导向合成，可自由调节指令强度";
  } else {
    hint.textContent = "";
  }
}

function onAccentChange() {
  const sel = $("accentSelect");
  const opt = sel.options[sel.selectedIndex];
  state.accent = opt.dataset.instr || "";
  state.accentLabel = state.accent ? opt.text : "";
  $("accentHint").textContent = state.accent
    ? "口音指令（将下发给模型）：" + state.accent
    : "口音由自然语言指令驱动；「克隆音色 + 口音」自动变为导向合成（可用指令强度）。支持的表演标记：[笑] [咳嗽] [清嗓子] [叹气]";
  syncCfgByVoice();
  renderChips();
  updateInstrPreview();
}

function clearAccent() {
  $("accentSelect").value = "";
  onAccentChange();
}

function onEmotionChange() {
  const sel = $("emotionSelect");
  const opt = sel.options[sel.selectedIndex];
  state.emotion = opt.dataset.instr || "";
  state.emotionLabel = state.emotion ? opt.text : "";
  $("emotionHint").textContent = state.emotion
    ? "情感指令（将下发给模型）：" + state.emotion
    : "情感与风格由自然语言指令驱动；克隆音色选择后自动解锁指令强度";
  syncCfgByVoice();
  renderChips();
  updateInstrPreview();
}

function clearEmotion() {
  $("emotionSelect").value = "";
  onEmotionChange();
}

function extraInstruction() {
  return [state.emotion, state.accent].filter(Boolean).join("，");
}

const NORM_TEXT = "用标准普通话，发音标准，字正腔圆，吐字清晰";

function wantNorm() {
  // 克隆/默认音色默认走标准普通话；显式选方言口音则按口音来
  const v = state.voices.find((x) => x.id === state.voiceId);
  const isCloneish = !v || v.type === "clone";
  return isCloneish && !state.accent;
}

function updateInstrPreview() {
  // 只显示真正会下发的内容；角色简介仅备注不下发
  const parts = [];
  const v = state.voices.find((x) => x.id === state.voiceId);
  const vIns = v && v.instruction && v.instruction.trim() ? v.instruction.trim() : "";
  if (wantNorm() && !vIns) parts.push("【发音规范】" + NORM_TEXT);
  if (vIns) parts.push("【音色指令】" + vIns);
  if (state.emotion) parts.push("【情感】" + state.emotion);
  if (state.accent) parts.push("【口音】" + state.accent);
  const r = state.roles.find((x) => x.id === state.roleId);
  const note = r ? "　〔角色 " + r.name + " 备注：仅标识，不下发〕" : "";
  const el = $("instrPreview");
  el.textContent = (parts.length ? parts.join(" ｜ ") : "（无指令，纯克隆原始口音）") + note;
}

async function trimRef() {
  const id = $("voiceId").value;
  if (!id) return;
  try {
    const res = await api("/api/voices/" + id + "/trim", { method: "POST", body: "" });
    $("voiceRefDur").textContent = `已裁剪到 ${res.ref_duration.toFixed(1)} 秒，合成会明显提速`;
    $("voiceTrimBtn").style.display = "none";
    await refreshVoices();
  } catch (e) { dlgErr("裁剪失败：" + friendlyErr(e.message)); }
}

function editVoice(id) {
  const v = state.voices.find((x) => x.id === id);
  if (!v) return;
  state.refFile = null;
  dlgErr("");
  $("voiceId").value = v.id;
  $("voiceType").value = v.type;
  $("voiceName").value = v.name;
  $("voiceInstr").value = v.instruction || "";
  $("voiceRefText").value = v.ref_text || "";
  $("voiceRef").value = "";
  $("voiceRefName").textContent = v.ref_audio
    ? "已上传：" + v.ref_audio.split("/").pop() + "（重新选择可替换）"
    : "选择 wav 文件…";
  const rdur = v.ref_duration || 0;
  $("voiceRefDur").textContent = rdur > 0
    ? `参考音频时长 ${rdur.toFixed(1)} 秒` + (rdur > 15 ? " —— 过长会显著拖慢每次合成，建议裁到 15 秒内" : "（推荐 5-15 秒）")
    : "";
  $("voiceTrimBtn").style.display = (rdur > 15 && v.ref_audio) ? "" : "none";
  $("voiceCfg").value = v.cfg_scale ?? 4;
  const isRef = v.type !== "design";
  $("voiceRefWrap").style.display = isRef ? "" : "none";
  $("voiceInstrWrap").style.display = (v.type === "clone") ? "none" : "";
  $("voiceDlgTitle").textContent = "编辑音色：" + v.name;
  $("voiceDlgHint").textContent = "修改后点保存即生效；不重新选文件则保留原参考音频";
  $("voiceDlg").showModal();
}

function openVoiceDlg(type) {
  state.refFile = null;
  dlgErr("");
  $("voiceId").value = ""; $("voiceType").value = type;
  $("voiceName").value = ""; $("voiceInstr").value = "";
  $("voiceRefText").value = ""; $("voiceRef").value = "";
  $("voiceRefName").textContent = "选择 wav 文件…";
  $("voiceCfg").value = 4;
  const isRef = type !== "design";
  $("voiceRefWrap").style.display = isRef ? "" : "none";
  $("voiceInstrWrap").style.display = (type === "clone") ? "none" : "";
  $("voiceDlgTitle").textContent =
    type === "design" ? "设计音色（Voice Design）" :
    type === "clone" ? "克隆音色（Voice Clone）" : "导向音色（Voice Direction）";
  $("voiceDlgHint").textContent =
    type === "design" ? "用一句自然语言描述创造一个全新音色，无需参考音频。" :
    type === "clone" ? "上传自己的声音 + 准确文稿，复刻音色。" :
    "上传参考音频克隆音色，再用指令控制语气、语速与情绪。";
  $("voiceDlg").showModal();
}

$("voiceRef")?.addEventListener("change", (e) => {
  state.refFile = e.target.files[0] || null;
  $("voiceRefName").textContent = state.refFile ? state.refFile.name : "选择 wav 文件…";
});

function dlgErr(msg) {
  const el = $("voiceDlgErr");
  if (msg) { el.textContent = msg; el.style.display = ""; }
  else { el.textContent = ""; el.style.display = "none"; }
}

async function saveVoice() {
  const id = $("voiceId").value;
  const type = $("voiceType").value;
  const name = $("voiceName").value.trim();
  dlgErr("");
  if (!name) { dlgErr("请填写音色名称"); return; }
  if (type !== "design") {
    if (!id && !state.refFile) { dlgErr("请上传参考音频（自己的声音，几秒即可）"); return; }
    if (!$("voiceRefText").value.trim()) {
      dlgErr("请填写「参考音频的准确文字稿」——与音频里说的话一字不差，这是克隆的必要条件"); return;
    }
  }
  const body = {
    name, type,
    instruction: $("voiceInstr").value.trim(),
    ref_text: $("voiceRefText").value.trim(),
    cfg_scale: parseFloat($("voiceCfg").value) || 4,
  };
  try {
    let voice;
    if (id) {
      voice = await api("/api/voices/" + id, {
        method: "PUT", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
    } else {
      voice = await api("/api/voices", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
    }
    if (state.refFile && type !== "design") {
      const fd = new FormData();
      fd.append("ref_audio", state.refFile);
      await api("/api/voices/" + voice.id + "/ref", { method: "POST", body: fd });
    }
    $("voiceDlg").close();
    setStatus("音色已保存：" + name, "ok");
    await refreshVoices(); renderChips();
  } catch (e) { dlgErr("保存失败：" + friendlyErr(e.message)); }
}

async function deleteVoice(id) {
  if (!confirm("删除该音色？")) return;
  try {
    await api("/api/voices/" + id, { method: "DELETE" });
    if (state.voiceId === id) state.voiceId = null;
    await refreshVoices(); renderChips();
  } catch (e) { setStatus("删除失败：" + friendlyErr(e.message), "err"); }
}

/* ---------- 角色 ---------- */
async function refreshRoles() {
  state.roles = await api("/api/roles");
  $("roleCount").textContent = state.roles.length + " 个";
  const box = $("roleList");
  if (!state.roles.length) {
    box.innerHTML = '<div class="empty">暂无角色<br>角色 = 名字 + 音色 + 默认参数</div>'; return;
  }
  box.innerHTML = state.roles.map((r) => {
    const arch = ARCH_LABEL[r.archetype] || "";
    const face = r.ref_image
      ? `<img src="${escapeHtml(r.ref_image)}" alt="" style="width:34px;height:34px;object-fit:cover;border-radius:8px;margin-right:9px;flex:none">`
      : "";
    return `
    <div class="item ${state.roleId===r.id?"active":""}" onclick="pickRole('${r.id}')" role="button" tabindex="0">
      ${face}
      <div class="grow"><div class="name">${escapeHtml(r.name)}${arch ? ` <span style="color:var(--primary);font-weight:400;font-size:11px">${escapeHtml(arch)}</span>` : ""}</div>
      <div class="meta">${escapeHtml(r.description||voiceNameOf(r.voice_id))}</div></div>
      <button class="iconbtn" title="编辑角色" onclick="event.stopPropagation();openRoleDlg('${r.id}')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>
      </button>
      <button class="iconbtn del" title="删除角色" onclick="event.stopPropagation();deleteRole('${r.id}')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
      </button>
    </div>`;
  }).join("");
}

function voiceNameOf(id) {
  const v = state.voices.find((x) => x.id === id);
  return v ? "音色：" + v.name : "";
}

function pickRole(id) {
  state.roleId = state.roleId === id ? null : id;
  if (state.roleId) {
    const r = state.roles.find((x) => x.id === id);
    if (r) {
      state.voiceId = r.voice_id || null;
      if (r.defaults) {
        $("cfgScale").value = r.defaults.cfg_scale ?? 4;
        $("seed").value = r.defaults.seed ?? 42;
      }
    }
  }
  syncCfgByVoice();
  renderChips(); refreshRoles(); refreshVoices();
  updateInstrPreview();
}

function openRoleDlg(id) {
  $("roleId").value = id || "";
  const r = id ? state.roles.find((x) => x.id === id) : null;
  $("roleDlgTitle").textContent = r ? "编辑角色" : "新建角色";
  $("roleName").value = r ? r.name : "";
  $("roleDesc").value = r ? (r.description || "") : "";
  $("roleArch").value = (r && r.archetype) || "custom";
  const thumb = $("roleRefThumb");
  if (r && r.ref_image) { thumb.src = r.ref_image + "?t=" + Date.now(); thumb.style.display = ""; }
  else { thumb.removeAttribute("src"); thumb.style.display = "none"; }
  $("roleRefFile").value = "";
  $("roleRefConsent").checked = false;
  $("roleRefErr").style.display = "none";
  $("roleCfg").value = r?.defaults?.cfg_scale ?? 4;
  $("roleSeed").value = r?.defaults?.seed ?? 42;
  $("roleVoice").innerHTML = state.voices.map((v) =>
    `<option value="${v.id}">${escapeHtml(v.name)}（${TYPE_LABEL[v.type]||v.type}）</option>`).join("");
  if (r?.voice_id) $("roleVoice").value = r.voice_id;
  $("roleDlgErr").style.display = "none";
  $("roleDlg").showModal();
}

async function saveRole() {
  const id = $("roleId").value;
  const name = $("roleName").value.trim();
  const err = $("roleDlgErr");
  err.style.display = "none";
  if (!name) { err.textContent = "请填写角色名称"; err.style.display = ""; return; }
  const body = {
    name,
    description: $("roleDesc").value.trim(),
    archetype: $("roleArch").value || "custom",
    voice_id: $("roleVoice").value || null,
    defaults: {
      cfg_scale: parseFloat($("roleCfg").value) || 4,
      seed: parseInt($("roleSeed").value) || 42,
    },
  };
  try {
    if (id) {
      await api("/api/roles/" + id, { method: "PUT", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
    } else {
      await api("/api/roles", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
    }
    $("roleDlg").close();
    setStatus("角色已保存：" + name, "ok");
    await refreshRoles(); renderChips();
  } catch (e) { err.textContent = "保存失败：" + friendlyErr(e.message); err.style.display = ""; }
}

async function uploadRoleRef() {
  const id = $("roleId").value;
  const err = $("roleRefErr");
  err.style.display = "none";
  if (!id) { err.textContent = "请先保存角色，再上传参考图"; err.style.display = ""; return; }
  const f = $("roleRefFile").files[0];
  if (!f) { err.textContent = "请选择一张图片"; err.style.display = ""; return; }
  if (!$("roleRefConsent").checked) { err.textContent = "请先勾选肖像授权确认"; err.style.display = ""; return; }
  const fd = new FormData();
  fd.append("role_id", id);
  fd.append("consent", "true");
  fd.append("file", f);
  try {
    const r = await fetch("/api/refs/upload", { method: "POST", body: fd });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || j.error || String(r.status));
    const thumb = $("roleRefThumb");
    thumb.src = j.ref_image + "?t=" + Date.now();
    thumb.style.display = "";
    $("roleRefFile").value = "";
    $("roleRefConsent").checked = false;
    setStatus("参考图已上传", "ok");
    await refreshRoles();
  } catch (e) { err.textContent = "上传失败：" + friendlyErr(e.message); err.style.display = ""; }
}

async function deleteRole(id) {
  if (!confirm("删除该角色？")) return;
  try {
    await api("/api/roles/" + id, { method: "DELETE" });
    if (state.roleId === id) state.roleId = null;
    await refreshRoles(); renderChips();
  } catch (e) { setStatus("删除失败：" + friendlyErr(e.message), "err"); }
}

/* ---------- 工作台 ---------- */
function renderChips() {
  const box = $("activeChips");
  const v = state.voices.find((x) => x.id === state.voiceId);
  const r = state.roles.find((x) => x.id === state.roleId);
  const parts = [];
  if (r) parts.push(`<span class="chip active" onclick="pickRole('${r.id}')" title="点击取消">角色：${escapeHtml(r.name)} ✕</span>`);
  if (v) parts.push(`<span class="chip active" onclick="pickVoice('${v.id}')" title="点击取消">音色：${escapeHtml(v.name)} ✕</span>`);
  if (state.emotionLabel) parts.push(`<span class="chip active" onclick="clearEmotion()" title="点击移除">情感：${escapeHtml(state.emotionLabel)} ✕</span>`);
  if (state.accentLabel) parts.push(`<span class="chip active" onclick="clearAccent()" title="点击移除">口音：${escapeHtml(state.accentLabel)} ✕</span>`);
  if (!parts.length) parts.push('<span class="chip flat">未选择角色/音色（用默认音色合成）</span>');
  box.innerHTML = parts.join("");
}

function estimateSeconds(chars) {
  return Math.max(12, Math.round(15 + chars * 2.0));
}

function updateLenHint() {
  const el = $("synthText"), hint = $("lenHint");
  const n = (el.value || "").replace(/\s/g, "").length;
  if (state.mode === "single" && n > 120) {
    const est = estimateSeconds(n);
    hint.textContent = `长文本（${n} 字）：本机显存有限，预计需要 ${Math.ceil(est/60)} 分钟左右，请耐心等待进度提示；追求交互速度可按句号分段合成`;
  } else if (state.mode === "single" && n > 0) {
    const est = estimateSeconds(n);
    hint.textContent = est > 60 ? `预计约 ${Math.ceil(est/60)} 分钟` : `预计约 ${est} 秒`;
  } else {
    hint.textContent = "";
  }
}
$("synthText")?.addEventListener("input", updateLenHint);

function splitText(t) {
  // 返回 [{text, gap}]；支持 [停顿N] / [pause:N] 标记生成段间静音
  const out = [];
  const re = /\[停顿\s*([\d.]+)\s*\]|\[pause\s*:\s*([\d.]+)\s*\]/gi;
  const chunks = [];
  let last = 0, m;
  while ((m = re.exec(t)) !== null) {
    chunks.push({ seg: t.slice(last, m.index), gap: parseFloat(m[1] || m[2] || 0.5) });
    last = re.lastIndex;
  }
  chunks.push({ seg: t.slice(last), gap: 0.25 });
  for (const c of chunks) {
    let cur = "";
    for (const ch of c.seg) {
      cur += ch;
      if ("。！？；!?;\n".includes(ch) && cur.trim().length >= 2) {
        out.push({ text: cur.trim(), gap: 0.25 }); cur = "";
      } else if (cur.length >= 55) {
        out.push({ text: cur.trim(), gap: 0.25 }); cur = "";
      }
    }
    if (cur.trim()) out.push({ text: cur.trim(), gap: c.gap });
    else if (out.length && c.gap > 0.25) out[out.length - 1].gap = c.gap;
  }
  return out;
}

function renderSegProgress(i, n, text) {
  const pct = Math.round((i / n) * 100);
  const bar = $("progBar");
  if (bar) bar.style.width = pct + "%";
  $("segProgress").textContent = n > 1
    ? `第 ${Math.min(i + 1, n)}/${n} 段 · 正在念：「${(text || "").slice(0, 16)}${(text || "").length > 16 ? "…" : ""}」 · 总进度 ${pct}%`
    : "";
}

function markSegDone(i, n) {
  const bar = $("progBar");
  if (bar) bar.style.width = Math.round(((i + 1) / n) * 100) + "%";
}

async function runSegments(idxs, segs, segFiles, t0) {
  const n = segs.length;
  const failed = [];
  for (const i of idxs) {
    const seg = segs[i];
    renderSegProgress(i, n, seg.text);
    try {
      const fd = new FormData();
      fd.append("text", seg.text);
      const extra = extraInstruction();
      if (extra) fd.append("accent", extra);
      if (wantNorm()) fd.append("norm", "1");
      if (state.voiceId) fd.append("voice_id", state.voiceId);
      if (state.roleId) fd.append("role_id", state.roleId);
      fd.append("cfg_scale", $("cfgScale").value || 4);
      fd.append("seed", $("seed").value || 42);
      fd.append("speed", $("speedSel").value || 1);
      const _lang = $("langSel").value || "";
      if (_lang) fd.append("language", _lang);
      const _policy = $("instructPolicySel").value || "voice";
      if (_policy && _policy !== "voice") fd.append("instruct_policy", _policy);
      if (n > 1) fd.append("no_history", "1");
      const res = await api("/api/synthesize", { method: "POST", body: fd });
      if (res.engine_note) state.engineNote = res.engine_note;
      segFiles[i] = (res.wav_url || "").split("/").pop();
      const player = $("player");
      const idle = player.paused || player.ended || !player.src;
      player.src = res.wav_url;
      $("playerBoxWrap").style.display = "";
      if (idle) player.play().catch(() => {});
      state.lastWavUrl = res.wav_url;
      state.lastWavName = (res.wav_url || "").split("/").pop();
      $("downloadBtn").style.display = "";
      markSegDone(i, n);
    } catch (e) {
      failed.push(i);
    }
  }
  return failed;
}

async function synthesize() {
  const text = state.mode === "single" ? $("synthText").value.trim() : $("batchText").value.trim();
  if (!text) { setStatus("请输入合成文本", "err"); return; }
  const btn = $("synthBtn");
  btn.disabled = true;
  $("playerBoxWrap").style.display = "none";
  $("player").pause();
  $("downloadBtn").style.display = "none";
  $("segProgress").textContent = "";

  const useSeg = state.mode === "single" && (text.replace(/\s/g, "").length > 60 || /\[停顿|\[pause/i.test(text));
  const segs = useSeg ? splitText(text) : [{ text, gap: 0.25 }];
  const multi = segs.length > 1;
  state.segDefs = segs;
  state.segFiles = new Array(segs.length).fill(null);
  state.failedIdx = [];
  state.engineNote = "";

  const chars = text.replace(/\s/g, "").length;
  const est = estimateSeconds(chars);
  const t0 = Date.now();
  $("progTrack").style.display = "";
  $("progBar").style.width = "0%";
  const timer = setInterval(() => {
    const el = ((Date.now() - t0) / 1000) | 0;
    const segNote = multi ? `第 ${Math.min(state.segPlaying + 1, segs.length)}/${segs.length} 段 · ` : "";
    setStatus(`合成中…${segNote}已用 ${el}s / 预计约 ${est > 90 ? Math.ceil(est / 60) + " 分钟" : est + " 秒"}（${multi ? "分段渐进，边合成边出稿" : "结果会自动播放"}）`, "busy");
  }, 1000);

  try {
    const idxs = segs.map((_, i) => i);
    let failed = await runSegments(idxs, segs, state.segFiles, t0);
    state.failedIdx = failed;
    const done = segs.length - failed.length;
    const secs = ((Date.now() - t0) / 1000).toFixed(1);

    if (done === 0) {
      setStatus("合成失败：全部分段未成功，请检查 API 状态后重试", "err");
      return;
    }

    // 合并已完成段（失败段跳过）
    const files = [];
    const gaps = [];
    for (let i = 0; i < segs.length; i++) {
      if (state.segFiles[i]) {
        files.push(state.segFiles[i]);
        gaps.push(segs[i].gap);
      }
    }
    let finalUrl = null, finalDur = 0;
    if (files.length > 1) {
      setStatus(`正在合并 ${files.length} 段…`, "busy");
      const merged = await api("/api/concat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          files, gaps, text: text.slice(0, 120),
          voice_id: state.voiceId || null, role_id: state.roleId || null,
          speed: $("speedSel").value || 1,
        }),
      });
      finalUrl = merged.wav_url;
      finalDur = merged.duration;
      state.lastWavUrl = merged.wav_url;
      state.lastWavName = (merged.wav_url || "").split("/").pop();
      $("player").src = merged.wav_url;
      $("player").play().catch(() => {});
    } else {
      finalUrl = state.lastWavUrl;
      finalDur = 0;
    }
    const cfgNote = ($("cfgScale").value || 4) == 1 ? " · cfg=1" : ` · cfg=${$("cfgScale").value}`;
    const failNote = failed.length ? ` · ⚠ ${failed.length} 段失败` : "";
    setStatus(`合成完成：${(finalDur || 0).toFixed(2)}s · ${done}/${segs.length} 段 · 耗时 ${secs}s${cfgNote}${failNote}`, failed.length ? "err" : "ok");
    $("segProgress").textContent = failed.length
      ? `失败段：${failed.map((i) => "「" + segs[i].text.slice(0, 10) + "…」").join("、")} · 点「重试失败段」补齐`
      : "";
    if (failed.length) {
      const rb = document.createElement("button");
      rb.className = "btn";
      rb.textContent = `重试失败段（${failed.length}）`;
      rb.style.marginTop = "8px";
      rb.onclick = async () => {
        rb.disabled = true;
        rb.textContent = "重试中…";
        const f2 = await runSegments(state.failedIdx, state.segDefs, state.segFiles, t0);
        state.failedIdx = f2;
        rb.textContent = f2.length ? `仍有 ${f2.length} 段失败` : "重试完成 ✓";
        await refreshHistory();
      };
      $("segProgress").appendChild(rb);
    }
    const insEl = $("lastInstr");
    insEl.textContent = extraInstruction() || "（无指令：默认/纯克隆音色）";
    $("lastInstrWrap").style.display = "";
    const noteEl = $("engineNoteWrap");
    if (state.engineNote) {
      $("engineNote").textContent = state.engineNote;
      noteEl.style.display = "";
    } else {
      noteEl.style.display = "none";
    }
    await refreshHistory();
  } catch (e) {
    setStatus("合成失败：" + friendlyErr(e.message), "err");
  } finally {
    clearInterval(timer);
    $("progTrack").style.display = "none";
    btn.disabled = false;
    updateLenHint();
  }
}

function downloadCurrent() {
  if (!state.lastWavUrl) return;
  const a = document.createElement("a");
  a.href = state.lastWavUrl;
  a.download = state.lastWavName || "synth.wav";
  a.click();
}

/* ---------- 历史 ---------- */
const HIST_PAGE = 10;

function histNav(offset) {
  state.histOffset = Math.max(0, offset);
  renderHistPage();
}

async function refreshHistory(freshId) {
  state.history = await api("/api/history");
  $("histCount").textContent = state.history.length + " 条";
  if (state.histOffset === 0 || state.histOffset >= state.history.length) state.histOffset = 0;
  renderHistPage(freshId);
}

function renderHistPage(freshId) {
  const box = $("histList");
  const total = state.history.length;
  if (!total) {
    box.innerHTML = '<div class="empty">还没有合成记录<br>合成结果会自动保存在这里</div>'; return;
  }
  const end = total - state.histOffset;
  const start = Math.max(0, end - HIST_PAGE);
  const slice = state.history.slice(start, end).reverse();
  const navTop = state.histOffset > 0
    ? '<button class="btn" style="width:100%;min-height:36px" onclick="histNav(' + (state.histOffset - HIST_PAGE) + ')">← 回到最新</button>' : "";
  const navBottom = start > 0
    ? '<button class="btn" style="width:100%;min-height:36px" onclick="histNav(' + (state.histOffset + HIST_PAGE) + ')">查看更早 ' + Math.min(HIST_PAGE, start) + ' 条（共 ' + total + '）</button>'
    : (total > HIST_PAGE ? '<div class="hint" style="text-align:center">已到最早记录（共 ' + total + ' 条）</div>' : "");
  box.innerHTML = navTop + slice.map((h) => `
    <div class="item ${h.id===freshId?"fresh":""}">
      <div class="grow">
        <div class="name">${escapeHtml((h.text||"[批量]").slice(0,24))}</div>
        <div class="meta">${(h.duration||0).toFixed(1)}s · ${escapeHtml((h.created_at||"").slice(5,16))}</div>
      </div>
      <button class="iconbtn play" title="播放" onclick="playHist('${h.wav_url}')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
      </button>
      <button class="iconbtn" title="下载" onclick="dlHist('${h.wav_url}')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
      </button>
      <button class="iconbtn del" title="删除" onclick="delHist('${h.id}')">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
      </button>
    </div>`).join("") + navBottom;
}

function playHist(url) {
  $("player").src = url;
  $("playerBoxWrap").style.display = "";
  $("player").play().catch(() => {});
}
function dlHist(url) {
  const a = document.createElement("a");
  a.href = url; a.download = url.split("/").pop(); a.click();
}
async function delHist(id) {
  try {
    await api("/api/history/" + id, { method: "DELETE" });
    await refreshHistory();
  } catch (e) { setStatus("删除失败：" + friendlyErr(e.message), "err"); }
}

/* ---------- 模式切换 ---------- */
$("modeSingle").addEventListener("click", () => setMode("single"));
$("modeBatch").addEventListener("click", () => setMode("batch"));
function setMode(m) {
  state.mode = m;
  $("modeSingle").classList.toggle("active", m === "single");
  $("modeBatch").classList.toggle("active", m === "batch");
  $("synthText").style.display = m === "single" ? "" : "none";
  $("batchText").style.display = m === "batch" ? "" : "none";
  $("maxTokensWrap").style.display = m === "batch" ? "" : "none";
  updateLenHint();
}

/* ---------- 快捷键 ---------- */
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    if (!$("synthBtn").disabled) synthesize();
  }
});

/* ---------- 启动 ---------- */
(async function init() {
  setMode("single");
  await Promise.all([refreshModel(), refreshVoices().catch(()=>[]), refreshRoles().catch(()=>[]), refreshHistory().catch(()=>[])]);
  renderChips();
  updateLenHint();
  updateInstrPreview();
  setInterval(refreshModel, 15000);
})();
</script>
</body>
</html>
"""


def _local_video_busy(max_age_sec: int = 1800) -> bool:
    """本地出片（ComfyUI）是否正在占用显卡。

    8GB 卡做不到「语音常驻 + 视频推理」并存：出片前我们主动请语音卸卡，
    如果这时候再进来一个合成请求，语音会把模型重新装回显存，把正在跑的
    出片顶爆（OOM，白等几分钟）。所以出片期间直接拒绝合成类请求，
    并说清原因，而不是让人等到一半才失败。
    """
    db = DATA / "bee_tasks.db"
    if not db.is_file():
        return False
    try:
        with contextlib.closing(sqlite3.connect(db, timeout=3)) as con:
            rows = con.execute(
                "SELECT updated_at FROM video_jobs "
                "WHERE provider='comfyui' AND status IN ('queued','running')"
            ).fetchall()
    except Exception:
        return False
    now = datetime.datetime.now()
    for (ts,) in rows:
        try:
            started = datetime.datetime.fromisoformat(str(ts))
        except Exception:
            continue
        if (now - started).total_seconds() <= max_age_sec:
            return True
    return False


# ---------- HTTP Handler ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "BreezeTTSPanel/1.0"

    # ---- helpers ----
    def _json(self, obj, code: int = 200) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"detail": msg}, code)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _serve_file(self, path: Path, ctype: str) -> None:
        if not path.is_file():
            self._err("not found", 404)
            return
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        # 音频原来给的是 max-age=3600：同一行的文件名（line_001.wav）在重录后
        # 不变，浏览器一小时内会继续播旧音频——用户听到的还是上一版。
        # 改成 no-cache：每次都问一次服务器，本地网络开销可以忽略。
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(raw)

    def _serve_video(self, rel: str) -> None:
        """提供 data/videos/ 下的成片。

        不能用 _serve_file：整段读进内存对几十 MB 的视频太重，而且 <video>
        拖动进度条要靠 Range 请求，一次性 200 全量响应会让浏览器不能 seek。
        """
        if not rel or "\\" in rel or ":" in rel or "\x00" in rel or "/" in rel:
            self._err("视频路径不合法", 400)
            return
        if rel != _safe_name(rel) or not rel.lower().endswith((".mp4", ".webm", ".mkv")):
            self._err("视频路径不合法", 400)
            return
        target = (DATA / "videos" / rel).resolve()
        base = (DATA / "videos").resolve()
        if not target.is_file() or not target.is_relative_to(base):
            self._err("not found", 404)
            return
        size = target.stat().st_size
        ctype = "video/mp4" if rel.lower().endswith(".mp4") else "video/webm"
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            try:
                a, _, b = rng[len("bytes="):].partition("-")
                if a:
                    start = int(a)
                if b:
                    end = int(b)
            except ValueError:
                start, end = 0, size - 1
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        length = end - start + 1
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with open(target, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _proxy_stream(self, api_path: str, body: bytes, content_type: str, timeout: int = 600,
                      save_as: str = ""):
        """流式透传：按块转发上游 PCM，不整段缓冲（W2-2 S1 修复）。

        save_as 非空时把整段 PCM 落成 WAV（文件名由前端指定），
        供前端分段渐进合成后交给 /api/concat 合并（W2-6）。
        """
        import urllib.parse as _up
        parsed = _up.urlparse(API_BASE)
        conn = None
        resp = None
        try:
            # 上游单并发：撞车（409）时排队重试，而不是直接失败
            with _INFER_LOCK:
                for attempt in range(60):
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
                    conn = _httpclient.HTTPConnection(
                        parsed.hostname, parsed.port, timeout=timeout)
                    conn.request("POST", api_path, body=body,
                                 headers={"Content-Type": content_type})
                    resp = conn.getresponse()
                    if resp.status != 409:
                        break
                    if attempt < 59:
                        time.sleep(2.0)
                if resp is None:
                    raise RuntimeError("no upstream response")
            if resp.status != 200:
                detail = resp.read()
                try:
                    detail = json.loads(detail).get("detail", detail[:200])
                except Exception:
                    detail = detail.decode("utf-8", "replace")[:200] if isinstance(detail, bytes) else str(detail)[:200]
                self.send_response(resp.status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                err = json.dumps({"detail": _friendly_error(detail)}, ensure_ascii=False).encode()
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return
            # 200: stream chunks to browser
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("X-Sample-Rate", resp.getheader("X-Sample-Rate", "24000"))
            self.send_header("X-Sample-Format", resp.getheader("X-Sample-Format", "s16le"))
            _note = resp.getheader("X-Adapter-Note", "")
            if _note:
                # 降级原因必须能到前端，不能像早期版本那样静默吞掉
                self.send_header("X-Adapter-Note", _note)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            collected = bytearray() if save_as else None
            while True:
                # 必须用 read1：read(n) 在 chunked 响应上会阻塞攒满 n 字节
                # 才返回（等于又加了一层缓冲，首包被推迟、总耗时反而变长）。
                chunk = resp.read1(8192)
                if not chunk:
                    break
                if collected is not None:
                    collected += chunk
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            # W2-7: 必须先落盘再发结束标记——否则客户端收到流结束的瞬间
            # 文件还没写完，紧接着调 /api/concat 会读不到（竞态）。
            save_error = None
            if collected is not None and collected:
                try:
                    safe = _safe_name(save_as)
                    if not safe.lower().endswith(".wav"):
                        safe += ".wav"
                    (AUDIOS / safe).write_bytes(
                        pcm_to_wav(bytes(collected), normalize=False))
                except Exception as exc:
                    save_error = exc
                    sys.stderr.write(f"[panel] 分段落盘失败 {save_as!r}: {exc!r}\n")
            if save_error is not None:
                # 不能发成功终止块：客户端会立刻调 /api/concat 然后拿不到文件，
                # 整段音频静默丢失。发一个非法 chunk 让浏览器判定本次传输失败，
                # 前端按"该段失败"处理并允许重试。
                self.wfile.write(b"!!save-failed\r\n")
                self.wfile.flush()
                return
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception as exc:
            # 静默会让浏览器只看到 network error，日志毫无线索
            sys.stderr.write(f"[panel] 流式合成中断: {exc!r}\n")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _stream_pcm(self, pcm: bytes, save_as: str = "") -> None:
        """把已生成好的整段 PCM 按 chunked 假流式发出去。

        GPT-SoVITS 是一次性返回整段 WAV（官方 streaming_mode 也只是内部切段），
        所以先拿整段再分块写。响应头与 `_proxy_stream` 对齐，前端无需区分引擎。
        """
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("X-Sample-Rate", str(SAMPLE_RATE))
            self.send_header("X-Sample-Format", "s16le")
            self.send_header("X-Adapter-Route", ENGINE_SOVITS)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            step = 8192
            for i in range(0, len(pcm), step):
                chunk = pcm[i:i + step]
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            save_error = None
            if save_as:
                try:
                    safe = _safe_name(save_as)
                    if not safe.lower().endswith(".wav"):
                        safe += ".wav"
                    (AUDIOS / safe).write_bytes(pcm_to_wav(pcm, normalize=False))
                except Exception as exc:
                    save_error = exc
                    sys.stderr.write(f"[panel] sovits 分段落盘失败 {save_as!r}: {exc!r}\n")
            if save_error is not None:
                # 与 7860 链路同口径：不能让客户端以为成功却没文件
                self.wfile.write(b"!!save-failed\r\n")
                self.wfile.flush()
                return
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception as exc:
            sys.stderr.write(f"[panel] sovits 流式写出中断: {exc!r}\n")

    def _infer_with_retry(self, api_path: str, body: bytes, content_type: str,
                          timeout: int = 600, headers_out: dict | None = None):
        """上游单并发：锁内排队 + 409 撞车自动重试（跨进程/外部客户端撞车也能消化）。"""
        return _post_with_retry(api_path, body, content_type, timeout, headers_out)

    def log_message(self, fmt: str, *args) -> None:  # 安静日志
        pass

    def handle_one_request(self) -> None:
        """状态文件损坏时给一条能读懂的 500，而不是把连接掐断。

        以前 _save 抛错会一路冒到 socket 层，浏览器只看到「Failed to fetch」，
        用户完全不知道发生了什么。现在返回 JSON 错误，前端能弹出来。
        """
        try:
            super().handle_one_request()
        except _DataCorrupt as exc:
            _log(f"[http] 数据文件损坏，写入被拒绝：{exc}")
            try:
                body = json.dumps({"detail": str(exc)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                pass

    # ---- GET ----
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        # 剧本台路由（独立模块；惰性导入避免与 panel 互相 import）
        from script_routes import handle as _script_handle
        if _script_handle(self, "GET", path, b"", ""):
            return
        # Bee Studio 视频/成本路由（惰性导入）
        try:
            from api.router import handle as _bee_handle
            if _bee_handle(self, "GET", path, b"", ""):
                return
        except Exception:
            pass

        if path == "/":
            # 首页回 TTS 制声台：方言 / 声音克隆 / 角色自定义都在 web/index.html。
            # 视频工作台在 /bee-studio.html，两边顶栏互链。
            # query 必须原样带走 —— bee-studio 顶栏的「克隆 / 音色库 / 剧本台」
            # 是 /?screen=xxx，丢了 query 会永远停在默认屏（2026-11-26 回归）。
            _qs = urlparse(self.path).query
            self.send_response(302)
            self.send_header("Location", "/index.html" + (("?" + _qs) if _qs else ""))
            self.end_headers()
            return
        if path == "/index.html":
            _web_index = ROOT / "web" / "index.html"
            if _web_index.is_file():
                raw = _web_index.read_bytes()
            else:
                raw = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        # W2-1: serve extracted static files from web/
        if path in ("/styles.css", "/app.js", "/app.css"):
            fpath = ROOT / "web" / path.lstrip("/")
            if fpath.is_file():
                ext_ctype = {".css": "text/css", ".js": "application/javascript"}[fpath.suffix]
                data = fpath.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ext_ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
        # W2-1b: serve any .html/.css/.js/.svg/.ico from web/ (for bee-studio.html etc.)
        _web_exts = {".html", ".css", ".js", ".svg", ".ico", ".woff2", ".png", ".jpg"}
        _web_ctype = {".html": "text/html", ".css": "text/css", ".js": "application/javascript", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".woff2": "font/woff2", ".png": "image/png", ".jpg": "image/jpeg"}
        _fname = path.lstrip("/")
        _fpath = ROOT / "web" / _fname
        if _fname and _fpath.is_file() and _fpath.suffix in _web_exts:
            data = _fpath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", _web_ctype[_fpath.suffix] + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        # W2-3c: 发音词典读取
        if path == "/api/dict":
            self._json(_load_dict().to_list())
            return
        # W2-3c: 朗读预览（原文 → 规范化 → 命中项）
        if path == "/api/norm/preview":
            return
        if path == "/api/voices":
            self._json(_load("voices.json"))
            return
        if path == "/api/roles":
            roles = _load("roles.json")
            # 专属角色带上所属剧本标题：角色库里可能同时存在多个「旁白」，
            # 只给 script_id 用户分不清谁是谁
            titles = {s.get("id"): s.get("title", "") for s in _load("scripts.json")}
            for r in roles:
                # 老角色没有这个键：统一补出来，消费方才能直接按它判断归属
                r.setdefault("script_id", "")
                sid = str(r.get("script_id") or "")
                r["script_title"] = titles.get(sid, "") if sid else ""
            self._json(roles)
            return
        if path == "/api/history":
            items = _load("history.json")
            for h in items:
                h.setdefault("wav_url", "/api/audio/" + Path(h.get("wav", "")).name)
            self._json(items)
            return
        if path == "/api/model":
            try:
                with urlopen(API_BASE + "/v1/model", timeout=10) as resp:
                    self._json(json.loads(resp.read()))
            except Exception as e:
                self._err(f"API 7860 不可达: {e}", 502)
            return
        if path == "/api/status":
            # 运维可见性：现在「在跑什么、排队几个、引擎在不在、数据文件坏没坏」
            # 全都要靠猜。这个端点一次给全，故障排查时先看它。
            self._json(_status_snapshot())
            return
        if path == "/api/engine/sovits":
            self._json(_sovits_switch_status())
            return
        if path.startswith("/api/audio/"):
            target = _resolve_audio(path[len("/api/audio/"):])   # 模块级函数，不是方法
            if target is None:
                self._err("音频路径不合法", 400)
                return
            self._serve_file(target, "audio/wav")
            return
        if path.startswith("/api/refs/"):
            # 参考图挂在 data/refs/characters/<role_id>/<hash>.png，
            # 音色参考仍是 data/refs/<file>.wav：必须支持子路径。
            # 只允许在 REFS 目录内，挡掉 .. 与绝对路径穿越。
            import mimetypes
            rel = unquote(path[len("/api/refs/"):])
            base = REFS.resolve()
            target = (base / rel).resolve()
            if target != base and base not in target.parents:
                self._err("路径不合法", 400)
                return
            if not target.is_file():
                self._err("not found", 404)
                return
            self._serve_file(target, mimetypes.guess_type(str(target))[0]
                             or "application/octet-stream")
            return
        if path.startswith("/api/videos/"):
            self._serve_video(path[len("/api/videos/"):])
            return
        if path.startswith("/api/images/"):
            # SD1.5 出图落在 data/images/<date>-<id>.png，只接受纯文件名
            rel = unquote(path[len("/api/images/"):])
            if not rel or "/" in rel or "\\" in rel or ":" in rel \
                    or rel != _safe_name(rel) or not rel.lower().endswith(".png"):
                self._err("图片路径不合法", 400)
                return
            base = (DATA / "images").resolve()
            target = (base / rel).resolve()
            if not target.is_file() or not target.is_relative_to(base):
                self._err("not found", 404)
                return
            self._serve_file(target, "image/png")
            return
        self._err("not found", 404)

    # ---- POST ----
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        ctype = self.headers.get("Content-Type", "")
        body = self._read_body()
        # 本地出片独占显卡期间，合成类请求一律先拦下：放进去只会把出片顶爆
        if path.startswith("/api/synthesize") or path.endswith("/synth") or path.endswith("/produce"):
            if _local_video_busy():
                self._err("本地出片正在使用显卡（约 3-8 分钟）。出片期间合成会把它挤崩，"
                          "请等出片完成后再合成；要边配音边出片请改用云端 provider。", 409)
                return
        # 剧本台路由（独立模块；惰性导入避免与 panel 互相 import）
        from script_routes import handle as _script_handle
        if _script_handle(self, "POST", path, body, ctype):
            return
        # Bee Studio 视频/成本路由
        try:
            from api.router import handle as _bee_handle
            if _bee_handle(self, "POST", path, body, ctype):
                return
        except Exception:
            pass


        # ---- 角色参考图上传（R-01 存储 + R-04 肖像授权）----
        if path == "/api/refs/upload":
            fields, files = parse_multipart(body, ctype)
            rid = str(fields.get("role_id") or "").strip()
            consent = str(fields.get("consent") or "").strip().lower()
            if not re.fullmatch(r"r_[A-Za-z0-9_-]{1,40}", rid):
                self._err("role_id 不合法", 400)
                return
            # 法务边界：没有明确授权不落盘。前端勾选框不是装饰，
            # 这里必须再校验一次，否则直接打接口就能绕过。
            if consent not in ("1", "true", "yes", "on"):
                self._err("请先勾选确认：你拥有该肖像的使用授权", 400)
                return
            up = files.get("file")
            if not up:
                self._err("缺少参考图文件", 400)
                return
            _fname, data = up
            if len(data) > 10 * 1024 * 1024:
                self._err("参考图不能超过 10MB", 400)
                return
            ext = _sniff_image_ext(data)
            if not ext:
                self._err("只接受 PNG / JPEG / WebP 图片", 400)
                return
            role = next((r for r in _load("roles.json") if r.get("id") == rid), None)
            if not role:
                self._err("角色不存在，请刷新后重试", 404)
                return
            import hashlib
            digest = hashlib.sha1(data).hexdigest()[:16]
            folder = REFS / "characters" / rid
            folder.mkdir(parents=True, exist_ok=True)
            # 覆盖语义：同一角色只留一张参考图，先清旧图再落新图
            for old in folder.iterdir():
                if old.is_file():
                    try:
                        old.unlink()
                    except OSError:
                        pass
            fname = f"{digest}.{ext}"
            (folder / fname).write_bytes(data)
            ref_url = f"/api/refs/characters/{rid}/{fname}"

            def _apply(items, _rid=rid, _url=ref_url):
                for r in items:
                    if r.get("id") == _rid:
                        r["ref_image"] = _url
                        r["ref_consent_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        r["ref_consent_ver"] = "v1"
                        return {"found": r}
                return {"found": None}

            _update_json("roles.json", _apply)
            self._json({"ok": True, "ref_image": ref_url})
            return

        # ---- W2-3c: 朗读预览 ----
        if path == "/api/norm/preview":
            try:
                d = json.loads(body or b"{}")
            except Exception:
                return self._err("请求体必须是 JSON")
            if not isinstance(d, dict):
                return self._err("请求体必须是 JSON 对象")
            text = str(d.get("text") or "")
            scope = str(d.get("voice_id") or "")
            instruction = str(d.get("instruction") or "").strip()
            accent = str(d.get("accent") or "").strip()
            emotion = str(d.get("emotion") or "").strip()
            normalized, changes = _apply_text_pipeline(text, "2", scope)
            # 预览"最终下发指令"的拼装逻辑（与合成端一致）
            parts = [x for x in (instruction, emotion, accent) if x]
            final_ins = "，".join(parts)
            cfg = float(d.get("cfg_scale") or 4)
            voice = None
            if scope:
                for v in _load("voices.json"):
                    if v["id"] == scope:
                        voice = v
                        break
            if voice and voice.get("instruction") and voice["instruction"] not in parts:
                final_ins = "，".join([voice["instruction"]] + parts) if parts else voice["instruction"]
            if not final_ins:
                final_ins = "用标准普通话，发音标准，字正腔圆，吐字清晰"
                if accent or emotion:
                    final_ins = ""
            if not final_ins:
                cfg = 1.0
            self._json({
                "original": text,
                "normalized": normalized,
                "final_text": normalized,
                "changes": changes,
                "instruction": final_ins,
                "cfg": cfg,
                "dict_count": len(_load_dict().entries),
            })
            return

        # ---- W2-3c: 发音词典增删 ----
        if path == "/api/dict":
            try:
                d = json.loads(body or b"{}")
            except Exception:
                return self._err("请求体必须是 JSON")
            if not isinstance(d, dict):
                return self._err("请求体必须是 JSON 对象")
            word = str(d.get("word") or "").strip()
            reading = str(d.get("reading") or "").strip()
            scope = str(d.get("scope") or "global").strip() or "global"
            if not word:
                return self._err("请填写要纠正的词")
            if not reading:
                return self._err("请填写正确的读法")
            dictionary = _load_dict()
            try:
                entry = dictionary.add({"word": word, "reading": reading, "scope": scope})
            except ValueError as exc:
                return self._err(str(exc))
            _save_dict(dictionary)
            self._json({"ok": True, "entry": entry.to_dict()})
            return

        if path == "/api/engine/sovits/start":
            take_gpu = True
            if body:
                try:
                    take_gpu = bool(json.loads(body).get("take_gpu", True))
                except Exception:
                    pass          # body 非法就按默认（让出显存）处理，不因此拒绝启动
            self._json(_sovits_switch_start(take_gpu))
            return
        if path == "/api/engine/sovits/stop":
            self._json(_sovits_switch_stop())
            return

        if path == "/api/voices":
            try:
                d = json.loads(body or b"{}")
            except Exception:
                return self._err("invalid json")
            if not d.get("name"):
                return self._err("name required")
            vtype = d.get("type", "design")
            if vtype not in ("design", "clone", "direction"):
                return self._err("type must be design|clone|direction")
            # W2-7: 创建阶段允许文稿为空（草稿态）——正确流程是
            # 上传 → 质检 → 校对文稿 → 保存；强制校验放在 PUT（定稿）阶段。
            # 原型音色（F9）：只给 archetype 时用原型描述预填指令，
            # 让「按原型建音色」一次 POST 就能落库，不必前端拼文案。
            arch = str(d.get("archetype") or "").strip()
            instruction = str(d.get("instruction") or "").strip()
            if arch:
                from tts_pipeline.script import ARCHETYPES
                if arch not in ARCHETYPES:
                    # 拼错的原型会建出一个「没有音色描述」的空壳音色，
                    # 用户以为选了少年女，实际听到的是默认发音人。
                    return self._err(f"不认识的原型「{arch}」")
                if not instruction:
                    instruction = (ARCHETYPES[arch].get("instruction") or "").strip()
            _engine = str(d.get("engine") or ENGINE_QWEN).strip().lower()
            if _engine not in (ENGINE_QWEN, ENGINE_SOVITS):
                return self._err("engine 只能是 qwen3-tts 或 gpt-sovits")
            # 设计型靠文字描述造声，没有参考音频；GPT-SoVITS 是克隆引擎，存进去也合不出声
            if vtype == "design" and _engine == ENGINE_SOVITS:
                return self._err(
                    "设计型音色没有参考音频，GPT-SoVITS 无法克隆："
                    "请改用 Qwen3-TTS，或把类型改成「克隆」并上传参考音频")
            voice = {
                "id": _new_id("v"),
                "name": d["name"],
                "type": vtype,
                "instruction": instruction,
                "archetype": arch,
                "engine": _engine,
                "ref_audio": None,
                "ref_text": d.get("ref_text", ""),
                "cfg_scale": float(d.get("cfg_scale", 4)),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            # 原子追加：并发建音色时不能各自读一份再整体覆盖回去
            _update_json("voices.json", lambda it: it.append(voice))
            self._json(voice, 201)
            return

        if path.startswith("/api/voices/") and path.endswith("/ref"):
            vid = path.split("/")[3]
            fields, files = parse_multipart(body, ctype)
            f = files.get("ref_audio")
            if not f:
                return self._err("ref_audio file required")
            fname, data = f
            ext = Path(fname).suffix.lower() or ".wav"
            if ext not in (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".webm"):
                return self._err("unsupported audio format")
            # 统一归一成标准 WAV（24k/单声道/PCM16）：非 WAV 或采样参数不一致都重编码。
            # 解码失败/超限直接 400，绝不把坏文件写进 refs/ 让合成阶段才炸
            # （现网案例：上传 mp3 成功但每次合成都被上游判 400 ref_audio must be RIFF/WAVE）。
            try:
                wav_bytes, dur = normalize_ref_audio(data)
            except ValueError as exc:
                return self._err(str(exc), 400)
            safe = _safe_name(f"{vid}_{int(time.time())}.wav")
            (REFS / safe).write_bytes(wav_bytes)

            def _set_ref(items, _vid=vid, _safe=safe, _dur=dur):
                for v in items:
                    if v["id"] == _vid:
                        _old = str(v.get("ref_audio") or "")
                        v["ref_audio"] = f"refs/{_safe}"
                        v["ref_duration"] = _dur
                        return _old
                return ""

            old_rel = _update_json("voices.json", _set_ref)
            # 旧参考音频若已无人引用就删掉，避免重复上传在 refs/ 里堆孤儿文件
            if old_rel and not any(
                    str(v.get("ref_audio") or "") == old_rel
                    for v in _load("voices.json")):
                _purge_rel_file(old_rel)

            # W2-3c: 参考音频质检（可选，失败不阻断上传）
            quality = None
            try:
                from tts_pipeline.ref_prep import prepare_wav_bytes, RefPrepError
                prepared = prepare_wav_bytes(data, target_sr=24000, max_s=60.0)
                quality = prepared.quality.as_dict()
                quality["prepared_duration_s"] = round(prepared.duration_s, 3)
                quality["window"] = [round(prepared.window[0], 3),
                                     round(prepared.window[1], 3)]
                quality["operations"] = list(prepared.operations)
            except Exception as exc:
                code = getattr(exc, "code", "")
                quality = {"ready": True, "score": None,
                           "issues": [{"code": code or "assess_failed",
                                       "message": str(exc)[:160],
                                       "severity": "warn"}]}
            self._json({"ok": True, "ref_audio": f"refs/{safe}",
                        "ref_duration": dur, "quality": quality})
            return

        if path.startswith("/api/voices/") and path.endswith("/trim"):
            vid = path.split("/")[3]
            # 定位只读一次；音频文件与 JSON 是两份独立数据，先处理文件，
            # 再对 JSON 做原子更新，避免把耗时 IO 关进锁里。
            items_now = _load("voices.json")
            target = next((v for v in items_now if v["id"] == vid), None)
            if not target or not target.get("ref_audio"):
                return self._err("该音色没有参考音频可裁剪", 404)
            rp = DATA / target["ref_audio"]
            if not rp.is_file():
                return self._err("参考音频文件缺失，请重新上传", 404)
            trimmed = trim_wav_bytes(rp.read_bytes(), 15.0)
            rp.write_bytes(trimmed)
            dur = wav_duration(trimmed)

            def _set_dur(items, _vid=vid, _dur=dur):
                for v in items:
                    if v["id"] == _vid:
                        v["ref_duration"] = _dur

            _update_json("voices.json", _set_dur)
            self._json({"ok": True, "ref_duration": dur})
            return

        # 参考音频转写：克隆页上传后自动填文稿，省掉全产品最费手的一步
        if path == "/api/asr":
            try:
                d = json.loads(body or b"{}")
            except Exception:
                return self._err("invalid json")
            vid = str(d.get("voice_id") or "").strip()
            rec = next((v for v in _load("voices.json") if v.get("id") == vid), None)
            if not rec:
                return self._err("音色不存在", 404)
            rel = str(rec.get("ref_audio") or "")
            src = (DATA / rel) if rel else None
            if not src or not src.is_file():
                return self._err("先上传参考音频，再自动转写文稿")
            try:
                out = _transcribe(src)
            except Exception as exc:
                return self._err(f"转写失败：{type(exc).__name__}: {exc}"[:200], 500)
            return self._json(out)

        if path == "/api/roles/batch":
            # 批量编辑角色：一次改一批（默认情绪 / 语速 / 口音 / 音色 / 简介）。
            # 角色一多（30+），一个一个点开改是纯体力活。
            # 一次锁 + 一次写盘，别让前端发 N 个 PUT（中间态会互相覆盖）。
            try:
                d = json.loads(body or b"{}")
            except Exception:
                return self._err("invalid json")
            ids = {str(x) for x in (d.get("ids") or [])}
            patch = d.get("patch") or {}
            if not ids:
                return self._err("没选中任何角色")
            if not isinstance(patch, dict) or not patch:
                return self._err("没有要修改的内容")
            allowed = ("default_emotion", "speed", "accent", "voice_id",
                       "description", "archetype")
            use = {k: patch[k] for k in allowed if k in patch}
            if not use:
                return self._err("没有可批量修改的字段")
            if "speed" in use:
                try:
                    use["speed"] = min(2.0, max(0.5, float(use["speed"])))
                except (TypeError, ValueError):
                    return self._err("语速必须是 0.5~2 之间的数字")
            if "voice_id" in use and use["voice_id"]:
                if not any(v["id"] == use["voice_id"]
                           for v in _load("voices.json")):
                    return self._err("指定的音色不存在")

            def _batch(items, _ids=ids, _use=use):
                hit = 0
                for r in items:
                    if r["id"] in _ids:
                        r.update(_use)
                        hit += 1
                return {"found": hit}

            res = _update_json("roles.json", _batch)
            n = int(res.get("found") or 0)
            if not n:
                return self._err("选中的角色都不存在了，请刷新后重试")
            self._json({"updated": n, "ids": sorted(ids)})
            return

        if path == "/api/roles":
            try:
                d = json.loads(body or b"{}")
            except Exception:
                return self._err("invalid json")
            if not d.get("name"):
                return self._err("name required")
            # 剧本按名字找角色：同名角色会让后建的那个静默顶掉前一个，
            # 作者以为在改"雪峰"，实际合成用的是另一个人的音色。
            wanted = str(d["name"]).strip()
            # 归属剧本必须真实存在：否则会建出谁也认领不了的孤儿角色，
            # 角色库里只能显示成「已删剧本」
            scope_id = str(d.get("script_id") or "").strip()
            if scope_id and not any(s.get("id") == scope_id
                                    for s in _load("scripts.json")):
                return self._err("指定的剧本不存在")
            try:
                speed = float(d.get("speed") or 1.0)
            except (TypeError, ValueError):
                speed = 1.0
            # 原型必须是白名单里的键：拼错会建出一个「没有原型」的空壳角色，
            # 用户以为选了老年女，实际卡片什么都不显示。
            arch = str(d.get("archetype") or "custom").strip()
            from tts_pipeline.script import ARCHETYPES as _ARCH
            if arch not in _ARCH:
                return self._err(f"不认识的原型「{arch}」")
            role = {
                "id": _new_id("r"),
                "name": wanted,
                "description": d.get("description", ""),
                "voice_id": d.get("voice_id"),
                # 剧本台依赖这两个字段：原型决定音色描述，默认情绪决定该角色的基调
                "archetype": arch,
                # 空串 = 未设置：剧本台据此走「自动推断」。
                # 若在这里填「中性」，会让所有无标签行都恒为中性，
                # 标点与关键词推断永远不触发。
                "default_emotion": d.get("default_emotion") or "",
                # 角色的语速与口音：同一句话由不同角色念出来，快慢与腔调也该不同
                "speed": min(2.0, max(0.5, speed)),
                "accent": str(d.get("accent") or "").strip(),
                "defaults": d.get("defaults") or {"cfg_scale": 4, "seed": 42},
                # 专属角色归属的剧本 id；空 = 全局角色。同名角色可以共存
                # （不同剧本各一份），但同一个剧本/全局内不允许重名
                "script_id": scope_id,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            # 查重放进 mutator：先查后写会让两个并发请求都通过检查，
            # 最后写进两条同名角色——正是这次要根治的问题。
            def _add(items, _role=role):
                for r in items:
                    if (str(r.get("name") or "").strip() == _role["name"]
                            and str(r.get("script_id") or "") == _role["script_id"]):
                        where = "这个剧本里" if _role["script_id"] else ""
                        raise _ValidationError(
                            f"{where}已经有同名角色「{_role['name']}」了，"
                            "换一个名字，或直接编辑那个角色")
                items.append(_role)

            try:
                _update_json("roles.json", _add)
            except _ValidationError as exc:
                return self._err(str(exc))
            self._json(role, 201)
            return

        if path == "/api/synthesize/stream":
            # W2-2: 流式合成，按块转发 PCM
            fields, files = parse_multipart(body, ctype)
            api_fields = {k: v for k, v in fields.items()}
            voice_id = api_fields.get("voice_id", "")
            role_id = api_fields.get("role_id", "")
            voice = None
            if voice_id:
                for v in _load("voices.json"):
                    if v["id"] == voice_id:
                        voice = v
                        break
                if voice is None:
                    return self._err(f"指定的音色不存在（{voice_id}），请重新选择")
            if role_id and not voice:
                role_rec = next((r for r in _load("roles.json")
                                 if r["id"] == role_id), None)
                if role_rec is None:
                    return self._err("指定的角色已不存在，请重新选择")
                # 角色没绑音色时不能静默回落到默认发音人：用户以为在听「雪峰」，
                # 实际听到的是通用音色，表现就是"角色不能生成语音"。
                if not (role_rec.get("voice_id") or "").strip():
                    return self._err(
                        f"角色「{role_rec.get('name', '')}」还没绑定音色："
                        "点该角色的编辑按钮选一个音色，或取消角色改用默认发音人")
                for v in _load("voices.json"):
                    if v["id"] == role_rec["voice_id"]:
                        voice = v
                        break
                # 绑定关系失效必须明说：静默回落会念出别的声音，用户无从判断。
                # 两条合成路径口径统一（REQ-20261128-01）。
                if voice is None:
                    return self._err(
                        f"角色「{role_rec.get('name', '')}」绑定的音色"
                        f"（{role_rec['voice_id']}）已不存在，可能刚被删除："
                        "请重新为该角色绑定音色，或取消角色改用默认发音人")
            # W2-3c: 文本规范化 + 发音词典（norm=2）——只应调用一次
            _scope = api_fields.get("voice_id", "") or ""
            _new_text, _text_changes = _apply_text_pipeline(
                api_fields.get("text", ""), api_fields.get("norm", ""), _scope)
            if _new_text:
                api_fields["text"] = _new_text
            ins = (api_fields.get("instruction") or "").strip()
            accent = (api_fields.get("accent") or "").strip()
            emotion = (api_fields.get("emotion") or "").strip()
            extra_parts = []
            if voice and voice.get("instruction"):
                extra_parts.append(voice["instruction"])
            if emotion:
                extra_parts.append(emotion)
            if accent:
                extra_parts.append(accent)
            # merged 只能包含"额外"指令；为空时不能退化成 ins 本身，
            # 否则下面会拼成「沉稳旁白，沉稳旁白」，模型会多生成数十秒音频。
            merged = "，".join(extra_parts)
            if merged:
                api_fields["instruction"] = (ins + "，" + merged) if ins else merged
            norm_flag = api_fields.get("norm", "")
            has_accent = bool(accent)
            if norm_flag == "1" and not has_accent and not ins:
                api_fields["instruction"] = "用标准普通话，发音标准，字正腔圆，吐字清晰"
            cfg_used = api_fields.get("cfg_scale", "4")
            if not (api_fields.get("instruction") or "").strip():
                api_fields["cfg_scale"] = "1"
                cfg_used = "1"
            elif voice and voice.get("type") == "clone" and not has_accent and not emotion:
                api_fields["cfg_scale"] = "1"
                cfg_used = "1"
            if voice:
                # 设计型音色没有参考音频：它残留的 ref_text 若发给上游，
                # 会被直接判 ref_audio and ref_text must be paired，整条合成打挂。
                _vtxt = ("" if (voice.get("type") or "").strip() == "design"
                         else (voice.get("ref_text") or "").strip())
                if _vtxt and not api_fields.get("ref_text"):
                    api_fields["ref_text"] = _vtxt
                if voice.get("ref_audio"):
                    rp = DATA / voice["ref_audio"]
                    if rp.is_file():
                        files["ref_audio"] = (rp.name, rp.read_bytes())
            # save_as 是面板自己的参数，不能透给上游：必须先摘掉再序列化。
            _save_as = (api_fields.get("save_as") or "").strip()
            api_fields.pop("save_as", None)
            if voice_engine(voice) == ENGINE_SOVITS:
                _code, _payload = _sovits_synthesize(api_fields.get("text", ""), voice)
                if _code != 200:
                    self.send_response(_code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(_payload)))
                    self.end_headers()
                    self.wfile.write(_payload)
                    return
                self._stream_pcm(_wav_to_pcm(_payload), _save_as)
                return
            out_body, out_ctype = _remultipart(api_fields, files)
            self._proxy_stream("/v1/audio/speech", out_body, out_ctype,
                               save_as=_save_as)
            return

        if path == "/api/synthesize":
            fields, files = parse_multipart(body, ctype)
            text = (fields.get("text") or "").strip()
            if not text:
                return self._err("text required")
            # 组装上游参数
            api_fields = {
                "text": text,
                "cfg_scale": fields.get("cfg_scale", "4"),
                "seed": fields.get("seed", "42"),
            }
            # Qwen 适配层新增字段：显式语种、原生预设音色、克隆/指令冲突策略。
            # 留空时上游分别回落到 Auto / 自动推断 / 保音色，旧前端零改动可用。
            for _k in ("language", "speaker", "instruct_policy"):
                _v = (fields.get(_k) or "").strip()
                if _v:
                    api_fields[_k] = _v
            # W2-3c: 文本规范化 + 发音词典（norm=2）——此前这条路径漏了接线，
            # 导致 norm=2 只在预览和流式端点生效。
            _scope = fields.get("voice_id", "") or ""
            _new_text, _text_changes = _apply_text_pipeline(
                text, fields.get("norm", ""), _scope)
            if _new_text:
                api_fields["text"] = _new_text
            ins = fields.get("instruction", "")
            if ins:
                api_fields["instruction"] = ins
            ref_text = fields.get("ref_text", "")
            if ref_text:
                api_fields["ref_text"] = ref_text
            # 从音色/角色取参数
            voices = {v["id"]: v for v in _load("voices.json")}
            roles = {r["id"]: r for r in _load("roles.json")}
            role = roles.get(fields.get("role_id", ""))
            # 指定了角色却查不到：可能刚被删掉，同样不能静默回落
            if fields.get("role_id") and role is None:
                return self._err("指定的角色已不存在，请重新选择")
            # 显式指定了 voice_id 就以它为准；只选了角色而角色没绑音色则明确拒绝，
            # 否则会安静地用默认发音人，用户无从判断角色到底生效没有。
            if role is not None and not fields.get("voice_id") \
                    and not (role.get("voice_id") or "").strip():
                return self._err(
                    f"角色「{role.get('name', '')}」还没绑定音色："
                    "点该角色的编辑按钮选一个音色，或取消角色改用默认发音人")
            voice = None
            vid = fields.get("voice_id") or (role or {}).get("voice_id")
            if vid:
                voice = voices.get(vid)
            if fields.get("voice_id") and voice is None:
                return self._err(
                    f"指定的音色不存在（{fields.get('voice_id')}），请重新选择")
            # 角色绑定的音色已失效（被删/改 id）：与流式路径口径统一，必须明说而不是静默回落
            if vid and voice is None and not fields.get("voice_id"):
                return self._err(
                    f"角色「{(role or {}).get('name', '')}」绑定的音色（{vid}）"
                    "已不存在，可能刚被删除：请重新为该角色绑定音色，"
                    "或取消角色改用默认发音人")
            if voice:
                _vref = voice.get("ref_audio")
                _vtxt = (voice.get("ref_text") or "").strip()
                _vtype = (voice.get("type") or "").strip()
                # 设计型音色靠 instruction 描述，本来就没有参考音频。
                # 它身上残留的 ref_text 是历史脏数据：既不该拿去做「成对」校验，
                # 也不能发给上游——上游只收到 ref_text 没有 ref_audio 会直接
                # 报 ref_audio and ref_text must be paired，把合成整条打挂。
                if _vtype == "design":
                    _vtxt = ""
                if bool(_vref) != bool(_vtxt):
                    missing = "文字稿" if _vref else "参考音频"
                    return self._err(f"音色「{voice.get('name', '')}」缺{missing}（参考音频与文字稿必须成对），请点该音色的编辑按钮补齐后再合成")
                if _vref and _is_placeholder_ref_text(_vtxt):
                    # W2-7b: 占位符文稿 = 没填。放行会让模型学错对齐，
                    # 结果是"克隆不像"，而系统还不报错——必须点名拦截。
                    return self._err(
                        f"音色「{voice.get('name', '')}」的参考文稿还是占位符「{_vtxt}」。"
                        "请点该音色的编辑按钮，把参考音频里说的话一字不差地写进去，"
                        "否则克隆出来的声音会对不上，相似度会明显下降。")
                if _vref and not (DATA / _vref).is_file():
                    return self._err(f"音色「{voice.get('name', '')}」的参考音频文件缺失，请点编辑重新上传")
                if voice.get("instruction") and not ins:
                    api_fields["instruction"] = voice["instruction"]
                if _vtxt and not ref_text:
                    api_fields["ref_text"] = _vtxt
                if voice.get("cfg_scale") and "cfg_scale" not in fields:
                    api_fields["cfg_scale"] = str(voice["cfg_scale"])
                if voice.get("ref_audio"):
                    rp = DATA / voice["ref_audio"]
                    if rp.is_file():
                        files["ref_audio"] = (rp.name, rp.read_bytes())
            # 默认普通话规范：克隆/默认音色且未显式选口音时注入（norm=1 由前端决策）
            if (fields.get("norm") or "") == "1":
                _ins0 = (api_fields.get("instruction") or "").strip()
                _acc0 = (fields.get("accent") or "").strip()
                if not _ins0 and not _acc0:
                    api_fields["instruction"] = "用标准普通话，发音标准，字正腔圆，吐字清晰"
            # CFG 规则：无指令模板（tts_plain / ref_clone_tata）无 negative prompt，cfg 必须=1
            accent = (fields.get("accent") or "").strip()
            if accent:
                ins = (api_fields.get("instruction") or "").strip()
                api_fields["instruction"] = f"{ins}，{accent}" if ins else accent
            cfg_used = api_fields.get("cfg_scale", "4")
            if not (api_fields.get("instruction") or "").strip():
                api_fields["cfg_scale"] = "1"
                cfg_used = "1"
            # 重新封 multipart 发给 7860（上游单并发，加锁排队 + 409 自动重试）
            # 取回上游响应头：X-Adapter-Note 承载静默降级原因，必须回显给 UI。
            out_body, out_ctype = _remultipart(api_fields, files)
            _up_hdrs: dict = {}
            if voice_engine(voice) == ENGINE_SOVITS:
                code, raw = _sovits_synthesize(api_fields.get("text", ""), voice)
                if code == 200:
                    raw = _wav_to_pcm(raw)
            else:
                code, raw = self._infer_with_retry("/v1/audio/speech", out_body, out_ctype,
                                                   headers_out=_up_hdrs)
            if code != 200:
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except Exception:
                    pass
                return
            try:
                _sp = float(fields.get("speed") or 1.0)
            except ValueError:
                _sp = 1.0
            _sp = min(2.0, max(0.5, _sp))
            raw = resample_pcm(raw, _sp)
            wav = pcm_to_wav(raw)
            hid = _new_id("h")
            fname = f"{time.strftime('%Y%m%d_%H%M%S')}_{hid}.wav"
            (AUDIOS / fname).write_bytes(wav)
            if (fields.get("no_history") or "") != "1":
                _append_history({
                    "id": hid,
                    "role_id": fields.get("role_id"),
                    "voice_id": vid,
                    "text": text,
                    "params": {"cfg_scale": api_fields.get("cfg_scale"),
                               "seed": api_fields.get("seed")},
                    "wav": f"audio/{fname}",
                    "duration": len(raw) / (SAMPLE_RATE * 2),
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
            self._json({
                "id": hid,
                "wav_url": f"/api/audio/{fname}",
                "duration": len(raw) / (SAMPLE_RATE * 2),
                "cfg_used": _safe_int(cfg_used, 1),
                "instruction_used": (api_fields.get("instruction") or "").strip(),
                "text_used": api_fields.get("text", ""),
                "engine_note": _engine_note(_up_hdrs),
                "engine_route": _header_get(_up_hdrs, "X-Adapter-Route"),
            })
            return

        if path == "/api/synthesize/batch":
            fields, files = parse_multipart(body, ctype)
            try:
                texts = json.loads(fields.get("texts") or "[]")
            except Exception:
                return self._err("texts must be JSON array")
            if not isinstance(texts, list) or not texts:
                return self._err("texts required")
            # W2-3c: 批量逐条规范化
            _bscope = fields.get("voice_id", "") or ""
            if (fields.get("norm") or "") == "2":
                _normed = []
                for _t in texts:
                    _nt, _ = _apply_text_pipeline(_t, "2", _bscope)
                    _normed.append(_nt)
                texts = _normed
            api_fields = {
                "texts": json.dumps(texts, ensure_ascii=False),
                "cfg_scale": fields.get("cfg_scale", "4"),
                "seed": fields.get("seed", "42"),
                "max_new_tokens": fields.get("max_new_tokens", "600"),
            }
            for _k in ("language", "speaker", "instruct_policy"):
                _v = (fields.get(_k) or "").strip()
                if _v:
                    api_fields[_k] = _v
            voices = {v["id"]: v for v in _load("voices.json")}
            roles = {r["id"]: r for r in _load("roles.json")}
            role = roles.get(fields.get("role_id", ""))
            # 指定了角色却查不到：可能刚被删掉，同样不能静默回落
            if fields.get("role_id") and role is None:
                return self._err("指定的角色已不存在，请重新选择")
            # 显式指定了 voice_id 就以它为准；只选了角色而角色没绑音色则明确拒绝，
            # 否则会安静地用默认发音人，用户无从判断角色到底生效没有。
            if role is not None and not fields.get("voice_id") \
                    and not (role.get("voice_id") or "").strip():
                return self._err(
                    f"角色「{role.get('name', '')}」还没绑定音色："
                    "点该角色的编辑按钮选一个音色，或取消角色改用默认发音人")
            voice = None
            vid = fields.get("voice_id") or (role or {}).get("voice_id")
            if vid:
                voice = voices.get(vid)
            if fields.get("voice_id") and voice is None:
                return self._err(
                    f"指定的音色不存在（{fields.get('voice_id')}），请重新选择")
            # 角色绑定的音色已失效（被删/改 id）：与流式路径口径统一，必须明说而不是静默回落
            if vid and voice is None and not fields.get("voice_id"):
                return self._err(
                    f"角色「{(role or {}).get('name', '')}」绑定的音色（{vid}）"
                    "已不存在，可能刚被删除：请重新为该角色绑定音色，"
                    "或取消角色改用默认发音人")
            if voice:
                _vref = voice.get("ref_audio")
                _vtxt = (voice.get("ref_text") or "").strip()
                _vtype = (voice.get("type") or "").strip()
                # 设计型音色靠 instruction 描述，本来就没有参考音频。
                # 它身上残留的 ref_text 是历史脏数据：既不该拿去做「成对」校验，
                # 也不能发给上游——上游只收到 ref_text 没有 ref_audio 会直接
                # 报 ref_audio and ref_text must be paired，把合成整条打挂。
                if _vtype == "design":
                    _vtxt = ""
                if bool(_vref) != bool(_vtxt):
                    missing = "文字稿" if _vref else "参考音频"
                    return self._err(f"音色「{voice.get('name', '')}」缺{missing}（参考音频与文字稿必须成对），请点该音色的编辑按钮补齐后再合成")
                if _vref and _is_placeholder_ref_text(_vtxt):
                    # W2-7b: 占位符文稿 = 没填。放行会让模型学错对齐，
                    # 结果是"克隆不像"，而系统还不报错——必须点名拦截。
                    return self._err(
                        f"音色「{voice.get('name', '')}」的参考文稿还是占位符「{_vtxt}」。"
                        "请点该音色的编辑按钮，把参考音频里说的话一字不差地写进去，"
                        "否则克隆出来的声音会对不上，相似度会明显下降。")
                if _vref and not (DATA / _vref).is_file():
                    return self._err(f"音色「{voice.get('name', '')}」的参考音频文件缺失，请点编辑重新上传")
                if voice.get("instruction"):
                    api_fields["instruction"] = voice["instruction"]
                if _vtxt:
                    api_fields["ref_text"] = _vtxt
                if voice.get("ref_audio"):
                    rp = DATA / voice["ref_audio"]
                    if rp.is_file():
                        files["ref_audio"] = (rp.name, rp.read_bytes())
            if (fields.get("norm") or "") == "1":
                _ins0 = (api_fields.get("instruction") or "").strip()
                _acc0 = (fields.get("accent") or "").strip()
                if not _ins0 and not _acc0:
                    api_fields["instruction"] = "用标准普通话，发音标准，字正腔圆，吐字清晰"
            accent = (fields.get("accent") or "").strip()
            if accent:
                ins = (api_fields.get("instruction") or "").strip()
                api_fields["instruction"] = f"{ins}，{accent}" if ins else accent
            cfg_used = api_fields.get("cfg_scale", "4")
            if not (api_fields.get("instruction") or "").strip():
                api_fields["cfg_scale"] = "1"
                cfg_used = "1"
            out_body, out_ctype = _remultipart(api_fields, files)
            _up_hdrs: dict = {}
            if voice_engine(voice) == ENGINE_SOVITS:
                # sidecar 没有批量接口：逐条合成再拼成一条，语义与上游批量一致
                _parts = []
                for _t in texts:
                    _c, _p = _sovits_synthesize(_t, voice)
                    if _c != 200:
                        self.send_response(_c)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(_p)))
                        self.end_headers()
                        self.wfile.write(_p)
                        return
                    _parts.append(_wav_to_pcm(_p))
                code, raw = 200, b"".join(_parts)
            else:
                code, raw = self._infer_with_retry("/v1/audio/speech/batch", out_body, out_ctype,
                                                   headers_out=_up_hdrs)
            if code != 200:
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            # 分段落盘（X-Segment-Bytes 在上游响应头；这里用整段+近似均分兜底）
            wav = pcm_to_wav(raw)
            hid = _new_id("h")
            fname = f"{time.strftime('%Y%m%d_%H%M%S')}_{hid}_batch.wav"
            (AUDIOS / fname).write_bytes(wav)
            _append_history({
                "id": hid,
                "role_id": fields.get("role_id"),
                "voice_id": vid,
                "text": f"[批量 {len(texts)} 条] " + " / ".join(t[:12] for t in texts[:3]),
                "params": {"cfg_scale": api_fields.get("cfg_scale"), "seed": api_fields.get("seed"), "count": len(texts)},
                "wav": f"audio/{fname}",
                "duration": len(raw) / (SAMPLE_RATE * 2),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            self._json({
                "id": hid,
                "wav_url": f"/api/audio/{fname}",
                "duration": len(raw) / (SAMPLE_RATE * 2),
                "segments": [len(raw)],
                "count": len(texts),
                "cfg_used": _safe_int(cfg_used, 1),
                "instruction_used": (api_fields.get("instruction") or "").strip(),
                "engine_note": _engine_note(_up_hdrs),
                "engine_route": _header_get(_up_hdrs, "X-Adapter-Route"),
            })
            return

        if path == "/api/concat":
            try:
                d = json.loads(body or b"{}")
            except Exception:
                return self._err("invalid json")
            fnames = d.get("files") or []
            if not fnames:
                return self._err("files required")
            gaps = d.get("gaps") or []
            pcm_parts = []
            for i, fn in enumerate(fnames):
                fp = AUDIOS / _safe_name(str(fn))
                if not fp.is_file():
                    return self._err(f"分段文件不存在: {fn}", 404)
                raw = fp.read_bytes()
                pcm_parts.append(raw[44:] if raw[:4] == b"RIFF" else raw)
                if i < len(fnames) - 1:
                    try:
                        gap = float(gaps[i]) if i < len(gaps) else 0.25
                    except (TypeError, ValueError):
                        gap = 0.25
                    gap = min(5.0, max(0.0, gap))
                    if gap > 0:
                        pcm_parts.append(b"\x00\x00" * int(SAMPLE_RATE * gap))
            pcm = b"".join(pcm_parts)
            try:
                _sp = float(d.get("speed") or 1.0)
            except (TypeError, ValueError):
                _sp = 1.0
            pcm = resample_pcm(pcm, min(2.0, max(0.5, _sp)))
            wav = pcm_to_wav(pcm, normalize=True)
            hid = _new_id("h")
            fname = f"{time.strftime('%Y%m%d_%H%M%S')}_{hid}_merged.wav"
            (AUDIOS / fname).write_bytes(wav)
            dur = len(pcm) / (SAMPLE_RATE * 2)
            _append_history({
                "id": hid,
                "role_id": d.get("role_id"),
                "voice_id": d.get("voice_id"),
                "text": (d.get("text") or "[分段合并]")[:200],
                "params": {"merged_segments": len(fnames)},
                "wav": f"audio/{fname}",
                "duration": dur,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            self._json({"id": hid, "wav_url": f"/api/audio/{fname}", "duration": dur, "segments": len(fnames)})
            return

        self._err("not found", 404)

    # ---- PUT ----
    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        body = self._read_body()
        # 剧本台路由（独立模块；惰性导入避免与 panel 互相 import）
        from script_routes import handle as _script_handle
        if _script_handle(self, "PUT", path, body, ""):
            return

        try:
            d = json.loads(body or b"{}")
        except Exception:
            return self._err("invalid json")

        if path.startswith("/api/voices/"):
            vid = path.rsplit("/", 1)[-1]
            def _apply(items, _vid=vid):
                for v in items:
                    if v["id"] == _vid:
                        for k in ("name", "type", "instruction", "ref_text", "archetype"):
                            if k in d:
                                v[k] = d[k]
                        if "engine" in d:
                            _e = str(d.get("engine") or ENGINE_QWEN).strip().lower()
                            if _e not in (ENGINE_QWEN, ENGINE_SOVITS):
                                raise _ValidationError("engine 只能是 qwen3-tts 或 gpt-sovits")
                            # 组合校验放在最终值上：本次可能同时改了 type 与 engine
                            if str(v.get("type")) == "design" and _e == ENGINE_SOVITS:
                                raise _ValidationError(
                                    "设计型音色没有参考音频，GPT-SoVITS 无法克隆："
                                    "请改用 Qwen3-TTS，或把类型改成「克隆」并上传参考音频")
                            v["engine"] = _e
                        if "cfg_scale" in d:
                            v["cfg_scale"] = float(d["cfg_scale"])
                        if v.get("type") in ("clone", "direction") and not (v.get("ref_text") or "").strip():
                            # 抛异常 = 本次不落盘（half-written 记录不会留在文件里）
                            raise _ValidationError(
                                "还不能保存：克隆/导向音色必须先填写「参考音频的准确文字稿」，与音频内容一字不差")
                        return {"found": v}
                return {"found": None}

            try:
                res = _update_json("voices.json", _apply)
            except _ValidationError as exc:
                return self._err(str(exc))
            if not res.get("found"):
                return self._err("voice not found", 404)
            return self._json(res["found"])

        if path.startswith("/api/roles/"):
            rid = path.rsplit("/", 1)[-1]
            wanted = str(d.get("name") or "").strip() if "name" in d else None
            if wanted is not None and not wanted:
                return self._err("角色名不能为空")

            def _apply(items, _rid=rid, _name=wanted):
                for r in items:
                    if r["id"] == _rid:
                        same_scope = str(r.get("script_id") or "")
                        # 改名查重也放在锁内，否则两个并发改名都能通过。
                        # 只在同一个归属内比：别的剧本的「旁白」不该拦这次改名
                        if _name and any(x["id"] != _rid
                                         and str(x.get("name") or "").strip() == _name
                                         and str(x.get("script_id") or "") == same_scope
                                         for x in items):
                            raise _ValidationError(f"已经有同名角色「{_name}」了")
                        for k in ("description", "voice_id", "defaults",
                                  "archetype", "default_emotion", "accent"):
                            if k in d:
                                if k == "archetype":
                                    from tts_pipeline.script import ARCHETYPES as _ARCH
                                    if str(d[k] or "custom") not in _ARCH:
                                        raise _ValidationError(f"不认识的原型「{d[k]}」")
                                r[k] = d[k]
                        # 参考图只允许清空：非空路径只能由 /api/refs/upload 写，
                        # 否则 PUT 能绕过肖像授权校验直接指向任意图片。
                        if "ref_image" in d and not str(d.get("ref_image") or "").strip():
                            r["ref_image"] = ""
                            r["ref_consent_at"] = ""
                            r["ref_consent_ver"] = ""
                        if _name:
                            r["name"] = _name
                        if "speed" in d:
                            try:
                                r["speed"] = min(2.0, max(0.5, float(d["speed"])))
                            except (TypeError, ValueError):
                                pass
                        return {"found": r}
                return {"found": None}

            try:
                res = _update_json("roles.json", _apply)
            except _ValidationError as exc:
                return self._err(str(exc))
            if not res.get("found"):
                return self._err("role not found", 404)
            return self._json(res["found"])

        self._err("not found", 404)

    # ---- DELETE ----
    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        # 剧本台路由（独立模块；惰性导入避免与 panel 互相 import）
        from script_routes import handle as _script_handle
        if _script_handle(self, "DELETE", path, b"", ""):
            return
        # Bee Studio DELETE 路由（模型 registry 删除等）
        try:
            from api.router import handle as _bee_handle
            if _bee_handle(self, "DELETE", path, b"", ""):
                return
        except Exception:
            pass

        # W2-3c: 删除词典条目
        if path.startswith("/api/dict/"):
            # urlparse 不解码百分号编码，中文词条必须先 unquote
            # （unquote 已在模块顶部导入；此处不能再局部 import，
            #   否则整个函数内 unquote 会被当成局部变量，前面调用直接 UnboundLocalError）
            word = _safe_name(unquote(path.rsplit("/", 1)[-1]))
            scope = "global"
            query = urlparse(self.path).query
            if query:
                from urllib.parse import parse_qs
                scope = (parse_qs(query).get("scope") or ["global"])[0]
            dictionary = _load_dict()
            if not dictionary.remove(word, scope):
                return self._err("词典里没有这一条", 404)
            _save_dict(dictionary)
            self._json({"ok": True})
            return
        # ---- DELETE /api/refs/<相对路径>：单独删一张参考图/参考音频 ----
        if path.startswith("/api/refs/"):
            rel = "refs/" + unquote(path[len("/api/refs/"):])
            if not _purge_rel_file(rel):
                return self._err("参考文件不存在或路径非法", 404)
            # 文件删了、账本还指着它就是悬空引用：把引用字段一起清空
            url = "/api/" + rel

            def _clear_voice(items):
                for v in items:
                    if (v.get("ref_audio") or "").strip().lstrip("/") == rel:
                        v["ref_audio"] = ""
                        v["ref_text"] = ""

            def _clear_role(items):
                for r in items:
                    if (r.get("ref_image") or "").strip() == url:
                        r["ref_image"] = ""
                        # 授权跟着图走：图删了还留着 consent，会留下
                        # 「没有图却有授权」的脏账本
                        r["ref_consent_at"] = ""
                        r["ref_consent_ver"] = ""

            _update_json("voices.json", _clear_voice)
            _update_json("roles.json", _clear_role)
            return self._json({"ok": True})
        if path.startswith("/api/voices/"):
            vid = path.rsplit("/", 1)[-1]
            removed: dict = {}

            def _drop(items, _vid=vid):
                for i, v in enumerate(items):
                    if v["id"] == _vid:
                        removed.update(v)
                        del items[i]
                        return {"hit": True}
                return {"hit": False}

            res = _update_json("voices.json", _drop)
            if not res["hit"]:
                return self._err("voice not found", 404)
            # 只删账本会让参考音频永久留在 data/refs 下（实测已积 18 个孤儿）
            purged = _purge_rel_file(removed.get("ref_audio", ""))
            return self._json({"ok": True, "purged_ref": purged})
        if path.startswith("/api/roles/"):
            rid = path.rsplit("/", 1)[-1]
            def _drop(items, _rid=rid):
                hit = any(r["id"] == _rid for r in items)
                if hit:
                    items[:] = [r for r in items if r["id"] != _rid]
                return {"hit": hit}

            res = _update_json("roles.json", _drop)
            if not res["hit"]:
                return self._err("role not found", 404)
            # 角色没了，参考图目录留着就是永远删不掉的人脸照片
            purged_imgs = _purge_role_ref_dir(rid)
            # 剧本 cast 里留着这个 id 会变成悬空引用：解析时找不到角色、
            # 界面也不报错，用户只会觉得「这一行突然没声音了」
            cleaned, warning = 0, ""
            try:
                from script_routes import drop_role_from_casts
                cleaned = drop_role_from_casts(rid)
            except Exception as exc:
                # 角色已经删掉了，这里失败没法回滚；但要如实告诉用户
                # 「剧本里的引用没清干净」，否则他只会在下次打开剧本时
                # 看到某个角色莫名其妙消失了
                warning = f"角色已删除，但清理剧本引用失败：{exc}"
            return self._json({"ok": True, "cleaned": cleaned, "warning": warning,
                               "purged_ref_images": purged_imgs})
        if path.startswith("/api/history/"):
            hid = path.rsplit("/", 1)[-1]
            items = _load("history.json")
            target = next((h for h in items if h["id"] == hid), None)
            if not target:
                return self._err("history not found", 404)
            wp = DATA / target.get("wav", "")
            if wp.is_file():
                try:
                    wp.unlink()
                except OSError:
                    pass
            def _drop_hist(items, _hid=hid):
                items[:] = [h for h in items if h["id"] != _hid]

            _update_json("history.json", _drop_hist)
            return self._json({"ok": True})
        self._err("not found", 404)


def _remultipart(fields: dict, files: dict) -> tuple[bytes, str]:
    """把 fields+files 重新封装为 multipart/form-data 请求体（发给 7860）。"""
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for k, v in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
        )
    for k, (fname, data) in files.items():
        parts.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
            + data
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """这个端口上是不是已经有东西在听。

    Windows 上 socket 默认带 SO_REUSEADDR，两个进程可以「同时」绑定同一个
    端口（后起的那个甚至会把前一个的连接抢走一半）。结果就是面板出现双实例：
    任务状态在 A 进程、文件写盘在 B 进程，前端随机拿到一边，表现为
    「Failed to fetch」「脚本一会儿有一会儿没有」。所以起服务前先探一下。
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.5)
        return s.connect_ex((host, port)) == 0


def _status_snapshot() -> dict:
    """运维快照：引擎、任务、队列、数据文件健康度，一次给全。

    以前这些信息散在各处（引擎在 7860 的 /v1/model、任务在 script_routes
    的内存字典里、数据文件健康只在写失败时才知道），排障时只能靠猜。
    """
    snap: dict = {"panel_port": PANEL_PORT, "engine": {}, "jobs": [],
                  "queue": 0, "data_files": {}, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    # 引擎（7860）
    try:
        with urlopen(API_BASE + "/v1/model", timeout=5) as resp:
            snap["engine"] = json.loads(resp.read())
            snap["engine"]["reachable"] = True
    except Exception as e:
        snap["engine"] = {"reachable": False, "error": str(e)[:200]}
    # 任务与队列（内存态，面板重启即失）
    try:
        from script_routes import _JOBS, _JOB_QUEUE, _JOBS_LOCK, _job_view
        with _JOBS_LOCK:
            snap["jobs"] = [
                {k: v for k, v in _job_view(j).items() if k != "rows"}
                for j in _JOBS.values()
            ]
            snap["queue"] = len(_JOB_QUEUE)
    except Exception as e:
        snap["jobs_error"] = str(e)[:200]
    # 数据文件健康
    for name in ("scripts.json", "roles.json", "voices.json",
                 "history.json", "assets.json"):
        p = DATA / name
        info = {"exists": p.is_file()}
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                info["count"] = len(data) if isinstance(data, list) else -1
                info["ok"] = True
            except Exception as e:
                info["ok"] = False
                info["error"] = str(e)[:120]
        info["corrupt"] = name in _DATA_CORRUPT
        snap["data_files"][name] = info
    return snap


def _startup_selfcheck() -> None:
    """启动时校验状态文件：坏了就从 .bak 恢复，实在不行明确报出来。

    不校验的话，一个损坏的 scripts.json 会让面板「正常启动但空空如也」，
    用户以为剧本没了，实际是文件坏了——而旧逻辑下一次写入还会把空数据盖回去。
    """
    for name in ("scripts.json", "roles.json", "voices.json",
                 "history.json", "assets.json"):
        p = DATA / name
        if not p.is_file():
            continue
        try:
            json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            _log(f"[data] 启动自愈：{name} 损坏（{exc}）")
            _load(name)   # _load 会尝试从 .bak 恢复，并标记写保护


def main() -> None:
    if _port_in_use(PANEL_PORT):
        # 已有实例在服务就别再起一个：端口是本工作区的唯一口径，
        # 双实例只会让状态分裂。让守护脚本去收敛旧进程。
        print(f"BreezeTTS Studio panel: 端口 {PANEL_PORT} 已被占用，"
              "已有实例在服务，本次不重复启动", flush=True)
        raise SystemExit(0)
    _startup_selfcheck()
    # 轮询线程活在进程内，面板一重启就没了；GET /api/video/poll 只读 DB，
    # 不补回线程的话任务会永远停在 running（界面一直转圈）。
    try:
        from api.router import resume_pending_video_jobs
        resume_pending_video_jobs()
    except Exception:
        pass
    server = ThreadingHTTPServer(("0.0.0.0", PANEL_PORT), Handler)
    print(f"BreezeTTS Studio panel: http://127.0.0.1:{PANEL_PORT}  (proxy -> {API_BASE}, port locked)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
