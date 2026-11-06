"""参考音频预处理与质量门。

纯 CPU，只依赖 numpy；WAV/MP3/FLAC 解码通过已有的 soundfile，不新增依赖。

处理链：
    读取/校验 -> 单声道 -> 可选重采样 -> 去首尾静音 -> 截取最佳窗口
    -> 响度归一化 -> 质量评估

阈值都是初始工程值，没有用标注数据校准。质量门默认只拦截“明显不可用”
（过短、全静音、严重削波、极低信噪比），其余给 warn，交给调用方决策。
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field

import numpy as np

__all__ = [
    "AudioIssue",
    "PreparedReference",
    "QualityReport",
    "RefPrepError",
    "assess_audio",
    "normalize_loudness",
    "prepare_reference",
    "prepare_wav_bytes",
    "resample_linear",
    "select_reference_window",
    "to_mono",
    "trim_silence",
    "write_wav_bytes",
]

EPS = 1e-12


class RefPrepError(ValueError):
    """带有稳定错误码的参考音频错误，便于 HTTP 层映射。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AudioIssue:
    code: str
    message: str
    severity: str = "warn"  # block / warn / info


@dataclass
class QualityReport:
    duration_s: float
    sample_rate: int
    channels: int
    peak_dbfs: float
    rms_dbfs: float
    clipped_ratio: float
    silence_ratio: float
    dc_offset: float
    snr_db: float | None
    spectral_instability: float | None = None
    issues: list[AudioIssue] = field(default_factory=list)
    score: int = 100

    @property
    def ready(self) -> bool:
        return not any(issue.severity == "block" for issue in self.issues)

    def as_dict(self) -> dict:
        return {
            "ready": self.ready,
            "score": self.score,
            "duration_s": round(self.duration_s, 3),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "peak_dbfs": round(self.peak_dbfs, 2),
            "rms_dbfs": round(self.rms_dbfs, 2),
            "clipped_ratio": round(self.clipped_ratio, 6),
            "silence_ratio": round(self.silence_ratio, 4),
            "dc_offset": round(self.dc_offset, 6),
            "snr_db": None if self.snr_db is None else round(self.snr_db, 2),
            "spectral_instability": (
                None
                if self.spectral_instability is None
                else round(self.spectral_instability, 4)
            ),
            "issues": [
                {"code": i.code, "message": i.message, "severity": i.severity}
                for i in self.issues
            ],
        }


@dataclass
class PreparedReference:
    audio: np.ndarray
    sample_rate: int
    duration_s: float
    source_duration_s: float
    quality: QualityReport
    window: tuple[float, float]
    operations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "sample_rate": self.sample_rate,
            "duration_s": round(self.duration_s, 3),
            "source_duration_s": round(self.source_duration_s, 3),
            "window": [round(self.window[0], 3), round(self.window[1], 3)],
            "operations": list(self.operations),
            "quality": self.quality.as_dict(),
        }


def _db(amplitude: float) -> float:
    return 20.0 * math.log10(max(float(amplitude), EPS))


def _as_mono(samples: np.ndarray) -> tuple[np.ndarray, int]:
    array = np.asarray(samples)
    if array.ndim == 1:
        return array.astype(np.float64, copy=False), 1
    if array.ndim == 2:
        channels = array.shape[1]
        if channels == 0:
            raise RefPrepError("empty_audio", "音频没有声道数据")
        if channels > 8:
            raise RefPrepError("too_many_channels", f"声道数 {channels} 超过上限 8")
        return array.astype(np.float64, copy=False).mean(axis=1), channels
    raise RefPrepError("bad_shape", f"音频维度应为 1 或 2，实际 {array.ndim}")


def _validate(samples: np.ndarray, sample_rate: int) -> None:
    if sample_rate <= 0:
        raise RefPrepError("bad_sample_rate", f"采样率必须为正，实际 {sample_rate}")
    if samples.size == 0:
        raise RefPrepError("empty_audio", "音频为空")
    if not np.all(np.isfinite(samples)):
        raise RefPrepError("non_finite", "音频包含 NaN 或 Inf，拒绝继续处理")


def to_mono(samples: np.ndarray) -> np.ndarray:
    """多声道下混为单声道 float32；已是单声道时返回副本。"""
    mono, _channels = _as_mono(samples)
    return mono.astype(np.float32)


def _frame_rms(mono: np.ndarray, sample_rate: int, frame_ms: float = 20.0) -> np.ndarray:
    size = max(1, int(sample_rate * frame_ms / 1000.0))
    if mono.size == 0:
        return np.zeros(1, dtype=np.float64)
    usable = (mono.size // size) * size
    frames = mono[:usable].reshape(-1, size)
    values = np.sqrt(np.mean(frames * frames, axis=1))
    if usable < mono.size:
        tail = mono[usable:]
        values = np.append(values, math.sqrt(float(np.mean(tail * tail))))
    return values


def _spectral_instability(mono: np.ndarray, sample_rate: int) -> float | None:
    """帧频谱质心的变异系数；只作为多人/强情绪的弱提示。"""
    n_fft = 1024 if sample_rate >= 16000 else 512
    if mono.size < n_fft * 4:
        return None
    window = np.hanning(n_fft)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)
    centroids: list[float] = []
    for start in range(0, mono.size - n_fft + 1, n_fft // 2):
        spectrum = np.abs(np.fft.rfft(mono[start : start + n_fft] * window))
        total = float(spectrum.sum())
        if total > 1e-9:
            centroids.append(float((spectrum * freqs).sum() / total))
    if len(centroids) < 4:
        return None
    mean = float(np.mean(centroids))
    if mean <= 0:
        return None
    return float(np.std(centroids) / mean)


def _spectral_snr(mono: np.ndarray, sample_rate: int) -> float | None:
    """用全段频谱的中位数当噪声底、95 分位当信号峰，估计信噪比。

    比“帧能量百分位”更能识别持续底噪：白噪声会把所有频点抬高，
    纯音/语音则只在少数谐波峰上有能量。
    """
    if mono.size < 2048:
        return None
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(mono.size))) ** 2
    freqs = np.fft.rfftfreq(mono.size, 1.0 / sample_rate)
    limit = min(7600.0, sample_rate / 2.0)
    band = spectrum[(freqs >= 80.0) & (freqs <= limit)]
    if band.size < 16:
        return None
    noise = float(np.median(band))
    peak = float(np.max(band))
    if peak <= EPS:
        return None
    noise = max(noise, peak * 1e-8)
    return float(min(60.0, 10.0 * math.log10(peak / noise)))


def assess_audio(
    samples: np.ndarray,
    sample_rate: int,
    *,
    min_s: float = 3.0,
    max_s: float = 15.0,
    clip_ratio_block: float = 0.001,
    silence_dbfs: float = -45.0,
    min_snr_db: float = 12.0,
) -> QualityReport:
    """评估参考音频是否适合克隆，返回可解释指标和问题列表。"""
    mono, channels = _as_mono(samples)
    _validate(mono, sample_rate)
    duration = mono.size / sample_rate
    peak = float(np.max(np.abs(mono)))
    rms = float(np.sqrt(np.mean(mono * mono)))
    clipped_ratio = float(np.mean(np.abs(mono) >= 0.999))
    dc_offset = float(np.mean(mono))

    frames = _frame_rms(mono, sample_rate)
    silence_ratio = float(np.mean(frames < 10 ** (silence_dbfs / 20.0)))
    snr_db = _spectral_snr(mono, sample_rate)

    instability = _spectral_instability(mono, sample_rate)

    issues: list[AudioIssue] = []
    if duration < min_s:
        issues.append(
            AudioIssue(
                "too_short",
                f"参考音频 {duration:.1f} 秒，短于下限 {min_s:.0f} 秒，克隆稳定性差",
                "block",
            )
        )
    elif duration > max_s:
        issues.append(
            AudioIssue(
                "too_long",
                f"参考音频 {duration:.1f} 秒，超过 {max_s:.0f} 秒，建议截取",
                "warn",
            )
        )
    if rms < 10 ** (-50 / 20.0) or silence_ratio >= 0.95:
        issues.append(AudioIssue("silent", "音频几乎全为静音或电平极低", "block"))
    elif rms < 10 ** (-35 / 20.0):
        issues.append(AudioIssue("too_quiet", "音频电平偏低，建议提高录音音量", "warn"))
    if clipped_ratio >= clip_ratio_block:
        issues.append(
            AudioIssue(
                "clipped",
                f"约 {clipped_ratio:.2%} 的采样点已削波，会引入失真",
                "block",
            )
        )
    elif clipped_ratio > 0.0001:
        issues.append(
            AudioIssue("near_clipping", f"约 {clipped_ratio:.2%} 的采样点接近满幅", "warn")
        )
    if abs(dc_offset) > 0.02:
        issues.append(
            AudioIssue("dc_offset", f"直流偏置 {dc_offset:+.3f}，预处理会去掉", "warn")
        )
    if snr_db is not None and snr_db < min_snr_db:
        issues.append(
            AudioIssue(
                "noisy",
                f"估计信噪比 {snr_db:.1f} dB，低于建议值 {min_snr_db:.0f} dB",
                "block" if snr_db < 6 else "warn",
            )
        )
    if instability is not None and instability > 0.75:
        issues.append(
            AudioIssue(
                "possible_multi_speaker",
                "频谱稳定性偏低，可能包含多人或强烈情绪；建议人工试听确认",
                "warn",
            )
        )
    if channels > 1:
        issues.append(AudioIssue("stereo_downmix", f"{channels} 声道将下混为单声道", "info"))

    score = 100
    for issue in issues:
        if issue.severity == "block":
            score -= 35
        elif issue.severity == "warn":
            score -= 8
    score = max(0, min(100, score))

    return QualityReport(
        duration_s=duration,
        sample_rate=sample_rate,
        channels=channels,
        peak_dbfs=_db(peak),
        rms_dbfs=_db(rms),
        clipped_ratio=clipped_ratio,
        silence_ratio=silence_ratio,
        dc_offset=dc_offset,
        snr_db=snr_db,
        spectral_instability=instability,
        issues=issues,
        score=score,
    )


def trim_silence(
    samples: np.ndarray,
    sample_rate: int,
    *,
    threshold_dbfs: float = -40.0,
    pad_ms: float = 100.0,
    min_voiced_s: float = 0.5,
) -> tuple[np.ndarray, tuple[float, float]]:
    """去掉首尾静音；返回 (裁剪后音频, (起始秒, 结束秒))。"""
    mono, _channels = _as_mono(samples)
    _validate(mono, sample_rate)
    if mono.size < max(1, int(sample_rate * 0.05)):
        return mono.astype(np.float32), (0.0, mono.size / sample_rate)
    frame_values = _frame_rms(mono, sample_rate)
    threshold = 10 ** (threshold_dbfs / 20.0)
    voiced = np.flatnonzero(frame_values >= threshold)
    if voiced.size == 0:
        return mono.astype(np.float32), (0.0, mono.size / sample_rate)
    frame_size = max(1, int(sample_rate * 0.02))
    pad = int(sample_rate * pad_ms / 1000.0)
    start = max(0, int(voiced[0]) * frame_size - pad)
    end = min(mono.size, (int(voiced[-1]) + 1) * frame_size + pad)
    if (end - start) < sample_rate * min_voiced_s:
        return mono.astype(np.float32), (0.0, mono.size / sample_rate)
    return (
        mono[start:end].astype(np.float32),
        (start / sample_rate, end / sample_rate),
    )


def select_reference_window(
    samples: np.ndarray,
    sample_rate: int,
    *,
    target_s: float = 12.0,
    min_s: float = 3.0,
    max_s: float = 15.0,
) -> tuple[int, int]:
    """在长音频里选能量最集中的窗口，返回 (start, end) 采样点下标。"""
    mono, _channels = _as_mono(samples)
    _validate(mono, sample_rate)
    if mono.size <= int(max_s * sample_rate):
        return 0, mono.size
    window = int(min(max_s, max(min_s, target_s)) * sample_rate)
    window = max(1, min(window, mono.size))
    hop = max(1, window // 4)
    best_start, best_score = 0, -1.0
    for start in range(0, mono.size - window + 1, hop):
        segment = mono[start : start + window]
        frames = _frame_rms(segment, sample_rate)
        if frames.size == 0:
            continue
        # 取能量较高的半数帧，避免一个尖峰决定结果
        top = np.sort(frames)[max(1, frames.size // 2) :]
        score = float(np.mean(top))
        score -= 10.0 * float(np.mean(np.abs(segment) >= 0.999))
        if score > best_score:
            best_start, best_score = start, score
    return best_start, min(mono.size, best_start + window)


def normalize_loudness(
    samples: np.ndarray,
    sample_rate: int,
    *,
    target_dbfs: float = -20.0,
    peak_ceiling_dbfs: float = -1.0,
    max_boost_db: float = 24.0,
) -> tuple[np.ndarray, float]:
    """按 RMS 归一到目标响度，并保证峰值不越过天花板；返回 (音频, 增益 dB)。"""
    mono, _channels = _as_mono(samples)
    _validate(mono, sample_rate)
    rms = float(np.sqrt(np.mean(mono * mono)))
    peak = float(np.max(np.abs(mono)))
    if rms <= EPS or peak <= EPS:
        return mono.astype(np.float32), 0.0
    target = 10 ** (target_dbfs / 20.0)
    ceiling = 10 ** (peak_ceiling_dbfs / 20.0)
    gain = target / rms
    if peak * gain > ceiling:
        gain = ceiling / peak
    gain_db = 20.0 * math.log10(gain) if gain > 0 else 0.0
    if gain_db > max_boost_db:
        gain = 10 ** (max_boost_db / 20.0)
        gain_db = max_boost_db
    out = np.clip(mono * gain, -ceiling, ceiling)
    return out.astype(np.float32), gain_db


def resample_linear(
    samples: np.ndarray,
    sample_rate: int,
    target_rate: int,
) -> np.ndarray:
    """线性插值重采样；降采样前做等权滑动平均抗混叠。

    这是没有 scipy 时的降级实现，适合语音参考，不追求音乐级质量。
    模型侧本身会用 soundfile 读入并按原采样率编码，默认不必强制重采样。
    """
    if target_rate <= 0:
        raise RefPrepError("bad_sample_rate", f"目标采样率必须为正，实际 {target_rate}")
    mono, _channels = _as_mono(samples)
    _validate(mono, sample_rate)
    if target_rate == sample_rate:
        return mono.astype(np.float32)
    source = mono.astype(np.float64)
    if target_rate < sample_rate:
        width = max(1, round(sample_rate / target_rate))
        if width > 1:
            pad = width // 2
            padded = np.pad(source, (pad, pad), mode="edge")
            cumulative = np.cumsum(np.insert(padded, 0, 0.0))
            source = (cumulative[width:] - cumulative[:-width]) / width
            source = source[: mono.size]
    new_length = round(mono.size * target_rate / sample_rate)
    if new_length <= 0:
        raise RefPrepError("empty_audio", "重采样后长度为 0")
    old_positions = np.arange(source.size, dtype=np.float64)
    new_positions = np.arange(new_length, dtype=np.float64) * sample_rate / target_rate
    new_positions = np.clip(new_positions, 0, max(0, source.size - 1))
    return np.interp(new_positions, old_positions, source).astype(np.float32)


def prepare_reference(
    samples: np.ndarray,
    sample_rate: int,
    *,
    target_sr: int | None = None,
    min_s: float = 3.0,
    max_s: float = 15.0,
    target_dbfs: float = -20.0,
    do_vad: bool = True,
    do_loudness: bool = True,
) -> PreparedReference:
    """完整预处理链；不抛错的情况下始终返回音频和质量报告。"""
    raw = np.asarray(samples)
    if raw.size == 0:
        raise RefPrepError("empty_audio", "音频为空")
    _validate(raw, sample_rate)
    mono, channels = _as_mono(raw)
    source_duration = mono.size / sample_rate
    operations: list[str] = []

    current = mono.astype(np.float32)
    if channels > 1:
        operations.append("downmix")
    current_rate = sample_rate
    if target_sr is not None and target_sr != sample_rate:
        current = resample_linear(current, current_rate, target_sr)
        current_rate = target_sr
        operations.append(f"resample:{sample_rate}->{target_sr}")

    if do_vad:
        trimmed, _span = trim_silence(current, current_rate)
        if trimmed.size != current.size:
            operations.append("trim_silence")
            current = trimmed

    if current.size > int(max_s * current_rate):
        start, end = select_reference_window(
            current, current_rate, target_s=min(12.0, max_s), min_s=min_s, max_s=max_s
        )
        if start != 0 or end != current.size:
            operations.append("select_window")
            current = current[start:end]

    if do_loudness:
        before = float(np.sqrt(np.mean(current * current)))
        current, gain_db = normalize_loudness(
            current, current_rate, target_dbfs=target_dbfs
        )
        after = float(np.sqrt(np.mean(current * current)))
        if abs(after - before) > EPS:
            operations.append(f"loudness:{gain_db:+.1f}dB")

    quality = assess_audio(current, current_rate, min_s=min_s, max_s=max_s)
    if channels > 1:
        # 质量报告要保留源声道数，提醒调用方做过下混
        quality.channels = channels
        if not any(issue.code == "stereo_downmix" for issue in quality.issues):
            quality.issues.append(
                AudioIssue("stereo_downmix", f"{channels} 声道已下混为单声道", "info")
            )
    if source_duration > max_s:
        quality.issues.append(
            AudioIssue(
                "trimmed_long",
                f"原始音频 {source_duration:.1f} 秒，已截取到 {current.size / current_rate:.1f} 秒",
                "info",
            )
        )
    return PreparedReference(
        audio=current.astype(np.float32),
        sample_rate=current_rate,
        duration_s=current.size / current_rate,
        source_duration_s=source_duration,
        quality=quality,
        window=(0.0, current.size / current_rate),
        operations=operations,
    )


def prepare_wav_bytes(data: bytes, **kwargs: object) -> PreparedReference:
    """解码上传的音频字节并预处理；解码失败抛 RefPrepError。"""
    if not data:
        raise RefPrepError("empty_audio", "上传内容为空")
    try:
        import soundfile as sf

        samples, sample_rate = sf.read(
            io.BytesIO(data), always_2d=True, dtype="float32"
        )
    except Exception as exc:
        raise RefPrepError("decode_failed", f"无法解码音频：{exc}") from exc
    return prepare_reference(samples, int(sample_rate), **kwargs)  # type: ignore[arg-type]


def write_wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    """把音频写成 16 bit PCM WAV 字节，供测试和接口复用。"""
    try:
        import soundfile as sf

        buffer = io.BytesIO()
        array = np.asarray(samples, dtype=np.float32)
        sf.write(buffer, array, sample_rate, format="WAV", subtype="PCM_16")
        return buffer.getvalue()
    except RefPrepError:
        raise
    except Exception as exc:
        raise RefPrepError("encode_failed", f"无法编码 WAV：{exc}") from exc

