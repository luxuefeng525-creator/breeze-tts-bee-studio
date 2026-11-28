#!/usr/bin/env python
"""把任意长度的参考音频规范成 GPT-SoVITS v3 能吃的 3~10 秒片段。

为什么需要：GPT-SoVITS 的 `TTS.py:926` 硬性要求参考音频在 3~10 秒
（`wav16k.shape[-1] > 160000 or < 48000` 直接抛 OSError），而本项目现有
克隆音色是 10.4 / 14.74 秒——原样送进去必然失败。

裁段只解决「时长」，不解决「文稿」：裁掉的片段说的话变了，原参考文稿
就与音频对不上，克隆会跑偏。所以调用方在裁段后必须重新转写（面板侧用
`panel._transcribe()`，命令行用同一条 ASR 链路）。本模块只做音频，
把「文稿重配」留给调用方，避免在这里偷偷加载语音模型。

用法（排障用）：
  python tools/sovits_ref.py probe <音频>
  python tools/sovits_ref.py crop  <源音频> <输出音频>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

# GPT-SoVITS 的上下限（秒）：上界留 0.1 秒余量，避免因重采样误差正好卡在 10.000
MIN_SEC = 3.0
MAX_SEC = 9.9
TARGET_SEC = 9.0
FRAME_SEC = 0.05          # 50 ms 一帧，足够分辨字与字之间的停顿


_T2S = None


def to_simplified(text: str) -> str:
    """whisper 的中文默认输出繁体。

    面板其它文稿（含用户逐字校对的参考文稿）全是简体，同一份数据出现两种
    字形会让后续比对与展示都对不上。缺 opencc 时原样返回，不阻断主流程。
    """
    global _T2S
    if not text:
        return text
    if _T2S is None:
        try:
            from opencc import OpenCC
            _T2S = OpenCC("t2s")
        except Exception:
            _T2S = False
    if _T2S is False:
        return text
    try:
        return _T2S.convert(text)
    except Exception:
        return text


def read_mono(path: str | Path) -> tuple[np.ndarray, int]:
    """读成单声道 float32。采样率保持原样，交给上游自己重采样。"""
    import soundfile as sf
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    return mono.astype(np.float32), int(sr)


def _frame_rms(y: np.ndarray, sr: int) -> np.ndarray:
    """逐帧 RMS 能量。帧长统一取 50 ms，末尾不足一帧的丢弃。"""
    n = max(1, int(sr * FRAME_SEC))
    frames = len(y) // n
    if frames == 0:
        return np.zeros(1, dtype=np.float32)
    return np.sqrt(np.mean(y[: frames * n].reshape(frames, n) ** 2, axis=1))


def pick_window(y: np.ndarray, sr: int, target_sec: float = TARGET_SEC) -> tuple[float, float]:
    """选「语音最密集」的 target_sec 窗口。

    判据是窗内语音帧数，而不是单纯能量最大——能量最大容易选到咳嗽、爆音或
    背景音乐的高潮段；语音帧数多说明这段一直在说话，更适合做音色参考。

    边界再向两侧各让 0.5 秒，在附近找能量最低的帧作为切点，避免正好切在
    半个字上（切字会让参考语义断裂，克隆相似度明显下降）。
    """
    rms = _frame_rms(y, sr)
    total = len(y) / sr
    if total <= target_sec:
        return 0.0, total

    # 语音阈值：相对最响帧 -25 dB，同时不低于绝对底噪的一个下限
    peak = float(rms.max()) if len(rms) else 0.0
    if peak <= 1e-6:
        return 0.0, min(total, target_sec)
    thr = max(peak * 10 ** (-25 / 20), 1e-5)
    voiced = (rms >= thr).astype(np.float32)

    win = max(1, int(round(target_sec / FRAME_SEC)))
    step = max(1, int(round(0.25 / FRAME_SEC)))
    if len(voiced) <= win:
        return 0.0, min(total, target_sec)

    # 前缀和求窗内语音帧数与能量和
    csum_v = np.concatenate([[0.0], np.cumsum(voiced)])
    csum_e = np.concatenate([[0.0], np.cumsum(rms)])
    best, best_key = 0, (-1.0, -1.0)
    for s in range(0, len(voiced) - win + 1, step):
        nv = csum_v[s + win] - csum_v[s]
        ne = csum_e[s + win] - csum_e[s]
        key = (float(nv), float(ne))
        if key > best_key:
            best_key, best = key, s

    start = best * FRAME_SEC
    end = start + target_sec
    # 边界对齐到静音：±0.5 秒内找能量最低帧
    pad = int(round(0.5 / FRAME_SEC))
    lo, hi = max(0, best - pad), min(len(rms) - 1, best + pad)
    if hi > lo:
        start = (lo + int(np.argmin(rms[lo : hi + 1]))) * FRAME_SEC
    lo_e = min(len(rms) - 1, best + win - pad)
    hi_e = min(len(rms) - 1, best + win + pad)
    if hi_e > lo_e:
        end = (lo_e + int(np.argmin(rms[lo_e : hi_e + 1]))) * FRAME_SEC
    if end - start < MIN_SEC:          # 对齐把窗口缩得太短 → 退回未对齐的窗口
        start, end = best * FRAME_SEC, best * FRAME_SEC + target_sec
    end = min(end, total)
    return round(start, 3), round(end, 3)


def analyze(path: str | Path) -> dict:
    """只报告，不写文件。用于判断这条参考音频能不能直接给 sidecar 用。"""
    y, sr = read_mono(path)
    dur = len(y) / sr
    info = {
        "path": str(path),
        "sample_rate": sr,
        "duration": round(dur, 3),
        "verdict": "ok" if MIN_SEC <= dur <= MAX_SEC else (
            "too_short" if dur < MIN_SEC else "too_long"),
    }
    if dur > MAX_SEC:
        s, e = pick_window(y, sr)
        info.update({"crop_start": s, "crop_end": e, "crop_duration": round(e - s, 3)})
    return info


def crop(src: str | Path, dst: str | Path) -> dict:
    """按最佳窗口裁一段并写成 24 kHz 单声道 PCM16 WAV（与项目口径一致）。"""
    import soundfile as sf

    y, sr = read_mono(src)
    dur = len(y) / sr
    if dur < MIN_SEC:
        raise ValueError(
            f"参考音频只有 {dur:.1f} 秒，短于 GPT-SoVITS 要求的 {MIN_SEC:g} 秒，"
            "裁不出来，请重新录一段 5~10 秒的干净人声")
    start, end = (0.0, dur) if dur <= MAX_SEC else pick_window(y, sr)
    seg = y[int(start * sr) : int(end * sr)]
    # 统一 24 kHz：与面板既有音频口径一致，省掉上游再重采样
    if sr != 24000:
        n = max(1, int(round(len(seg) * 24000 / sr)))
        x = np.linspace(0, len(seg) - 1, n, dtype=np.float64)
        seg = np.interp(x, np.arange(len(seg)), seg).astype(np.float32)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), seg, 24000, subtype="PCM_16")
    return {
        "src": str(src), "out": str(dst),
        "source_duration": round(dur, 3),
        "start": round(start, 3), "end": round(end, 3),
        "out_duration": round(len(seg) / 24000, 3),
        "cropped": dur > MAX_SEC,
    }


def prepare(src: str | Path, out_dir: str | Path, key: str,
            asr_fn=None) -> tuple[Path, str]:
    """把参考音频规范化成 3~10 秒，并按需重配文稿；结果按 key 缓存。

    asr_fn(path) -> str 由调用方注入：面板传 `_transcribe`，命令行传自己的
    ASR 封装。这样本模块不依赖任何语音模型，纯音频逻辑可以单测。

    返回 (可用音频路径, 文稿)。无需裁剪时返回原文件（文稿交回调用方自行取用）。
    """
    src = Path(src)
    out_dir = Path(out_dir)
    info = analyze(src)
    if info["verdict"] == "too_short":
        raise ValueError(
            f"参考音频只有 {info['duration']:.1f} 秒，短于 GPT-SoVITS 要求的 3 秒"
            "且内容补不出来，请重新录一段 5~10 秒的干净人声")

    if info["verdict"] == "ok":
        # 已在区间内：不裁也不重转，调用方手上那份用户逐字校对过的原稿更可信
        return src, ""

    dst = out_dir / f"{key}.wav"
    meta = out_dir / f"{key}.json"
    if dst.is_file() and meta.is_file():
        try:
            cached = json.loads(meta.read_text(encoding="utf-8"))
            if (cached.get("src") == str(src)
                    and cached.get("src_mtime") == src.stat().st_mtime):
                return dst, cached.get("prompt_text", "")
        except Exception:
            pass

    res = crop(src, dst)
    text = ""
    if asr_fn is not None:
        text = to_simplified(str(asr_fn(dst) or "")).strip()
    out_dir.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({
        "src": str(src), "src_mtime": src.stat().st_mtime,
        "window": [res["start"], res["end"]],
        "source_duration": res["source_duration"],
        "out_duration": res["out_duration"],
        "prompt_text": text,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return dst, text


def main() -> int:
    ap = argparse.ArgumentParser(description="参考音频规范化（3~10 秒）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("audio")
    c = sub.add_parser("crop")
    c.add_argument("src")
    c.add_argument("dst")
    args = ap.parse_args()

    if args.cmd == "probe":
        import json
        print(json.dumps(analyze(args.audio), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "crop":
        import json
        print(json.dumps(crop(args.src, args.dst), ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
