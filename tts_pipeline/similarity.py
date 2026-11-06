"""轻量说话人相似度评分。

本机没有说话人验证模型，这里用“逐帧归一化的对数梅尔谱统计量”作为音色代理
特征，再算余弦相似度。它能反映频谱包络和音色差异，但不是神经说话人嵌入：

- 分数适合同一设备、同一语言下的相对比较和回归报警；
- 默认阈值 0.72 没有用标注数据校准，不能当作绝对“像/不像”结论；
- 跨设备、跨语种、强情绪时偏差会明显变大；
- 如果以后接入真实 speaker embedding，用 ``compare_embeddings`` 替换即可。

纯 CPU、仅依赖 numpy，不加载模型、不访问网络。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np

__all__ = [
    "SimilarityError",
    "SimilarityResult",
    "SpeakerEmbedding",
    "compare",
    "compare_embeddings",
    "cosine_similarity",
    "extract_embedding",
    "rank_candidates",
]

EPS = 1e-10
METHOD = "mfcc-stats-v1"


class SimilarityError(ValueError):
    """音频或嵌入不满足计算条件。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SpeakerEmbedding:
    vector: np.ndarray
    sample_rate: int
    frames: int
    duration_s: float
    method: str = METHOD

    def as_dict(self) -> dict:
        return {
            "dim": int(self.vector.size),
            "sample_rate": self.sample_rate,
            "frames": self.frames,
            "duration_s": round(self.duration_s, 3),
            "method": self.method,
        }


@dataclass(frozen=True)
class SimilarityResult:
    score: float
    threshold: float
    passed: bool
    method: str = METHOD
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "threshold": self.threshold,
            "passed": self.passed,
            "method": self.method,
            "warnings": list(self.warnings),
        }


def _as_mono(samples: np.ndarray) -> np.ndarray:
    array = np.asarray(samples)
    if array.ndim == 1:
        return array.astype(np.float64, copy=False)
    if array.ndim == 2:
        if array.shape[1] == 0:
            raise SimilarityError("empty_audio", "音频没有声道数据")
        return array.astype(np.float64, copy=False).mean(axis=1)
    raise SimilarityError("bad_shape", f"音频维度应为 1 或 2，实际 {array.ndim}")


def _hz_to_mel(freq: float) -> float:
    return 2595.0 * math.log10(1.0 + freq / 700.0)


def _mel_to_hz(mel: float) -> float:
    return 700.0 * (10 ** (mel / 2595.0) - 1.0)


@lru_cache(maxsize=8)
def _mel_filterbank(
    sample_rate: int, n_fft: int, n_mels: int, fmin: float, fmax: float
) -> np.ndarray:
    mel_points = np.linspace(
        _hz_to_mel(fmin), _hz_to_mel(fmax), n_mels + 2
    )
    hz_points = np.array([_mel_to_hz(float(m)) for m in mel_points])
    bins = np.floor((n_fft + 1) * hz_points / sample_rate).astype(int)
    bins = np.clip(bins, 0, n_fft // 2)
    bank = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float64)
    for index in range(1, n_mels + 1):
        left, center, right = bins[index - 1], bins[index], bins[index + 1]
        if center <= left:
            center = left + 1
        if right <= center:
            right = center + 1
        center = min(center, n_fft // 2)
        right = min(right, n_fft // 2)
        if center > left:
            bank[index - 1, left:center] = (
                np.arange(left, center) - left
            ) / float(center - left)
        if right > center:
            bank[index - 1, center:right] = (
                right - np.arange(center, right)
            ) / float(right - center)
    return bank


def _dct_basis(n_mels: int, n_out: int) -> np.ndarray:
    k = np.arange(n_out)[:, None]
    n = np.arange(n_mels)[None, :]
    basis = np.cos(np.pi * k * (2 * n + 1) / (2.0 * n_mels))
    basis *= math.sqrt(2.0 / n_mels)
    basis[0, :] /= math.sqrt(2.0)
    return basis


def extract_embedding(
    samples: np.ndarray,
    sample_rate: int,
    *,
    min_duration_s: float = 0.3,
) -> SpeakerEmbedding:
    """提取音色代理嵌入；维度 42（20 个 MFCC 的均值/标准差 + 2 个频谱统计），L2 归一化。"""
    if sample_rate <= 0:
        raise SimilarityError("bad_sample_rate", f"采样率必须为正，实际 {sample_rate}")
    mono = _as_mono(samples)
    if mono.size == 0:
        raise SimilarityError("empty_audio", "音频为空")
    if not np.all(np.isfinite(mono)):
        raise SimilarityError("non_finite", "音频包含 NaN 或 Inf")
    duration = mono.size / sample_rate
    if duration < min_duration_s:
        raise SimilarityError(
            "too_short",
            f"音频 {duration:.2f} 秒，短于相似度计算下限 {min_duration_s:.1f} 秒",
        )
    rms = float(np.sqrt(np.mean(mono * mono)))
    if rms <= 1e-5:
        raise SimilarityError("silent", "音频电平过低，无法计算相似度")

    frame_len = max(1, int(sample_rate * 0.025))
    hop = max(1, int(sample_rate * 0.010))
    n_fft = 1
    while n_fft < frame_len:
        n_fft *= 2

    if mono.size < frame_len:
        mono = np.pad(mono, (0, frame_len - mono.size))
    n_frames = 1 + (mono.size - frame_len) // hop
    frames = np.lib.stride_tricks.as_strided(
        mono,
        shape=(n_frames, frame_len),
        strides=(mono.strides[0] * hop, mono.strides[0]),
        writeable=False,
    ).copy()

    emphasized = np.concatenate(
        (frames[:, :1], frames[:, 1:] - 0.97 * frames[:, :-1]), axis=1
    )
    windowed = emphasized * np.hamming(frame_len)
    power = np.abs(np.fft.rfft(windowed, n=n_fft, axis=1)) ** 2

    fmax = min(sample_rate / 2.0, 7600.0)
    bank = _mel_filterbank(sample_rate, n_fft, 40, 60.0, fmax)
    mel = np.maximum(power @ bank.T, 1e-10)
    log_mel = np.log(mel)

    # 逐帧去均值/方差，消掉录音增益和整体频谱倾斜
    normalized = (log_mel - log_mel.mean(axis=1, keepdims=True)) / (
        log_mel.std(axis=1, keepdims=True) + 1e-6
    )

    n_mfcc = 21
    basis = _dct_basis(40, n_mfcc)
    mfcc = normalized @ basis.T
    mfcc = mfcc[:, 1:]  # 丢掉 c0，避免响度主导

    frame_rms = np.sqrt(np.mean(frames * frames, axis=1))
    threshold = max(float(np.percentile(frame_rms, 25)), float(frame_rms.max()) * 1e-4)
    voiced = frame_rms >= threshold
    if int(voiced.sum()) < 5:
        voiced = np.ones_like(frame_rms, dtype=bool)

    selected = mfcc[voiced]
    mean = selected.mean(axis=0)
    std = selected.std(axis=0)

    freqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)
    spectrum_sum = power[voiced].sum(axis=1) + EPS
    centroid = (power[voiced] * freqs).sum(axis=1) / spectrum_sum
    centroid = centroid / (sample_rate / 2.0)
    bandwidth = np.sqrt(
        (power[voiced] * (freqs[None, :] - centroid[:, None] * sample_rate / 2.0) ** 2).sum(
            axis=1
        )
        / spectrum_sum
    ) / (sample_rate / 2.0)

    vector = np.concatenate(
        [mean, std, [float(centroid.mean()), float(bandwidth.mean())]]
    ).astype(np.float64)
    norm = float(np.linalg.norm(vector))
    if norm <= EPS:
        raise SimilarityError("degenerate_embedding", "嵌入向量为 0，无法比较")
    vector /= norm
    return SpeakerEmbedding(
        vector=vector,
        sample_rate=sample_rate,
        frames=int(voiced.sum()),
        duration_s=duration,
    )


def cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
    """两个一维向量的余弦相似度，范围 [-1, 1]。"""
    a = np.asarray(left, dtype=np.float64).reshape(-1)
    b = np.asarray(right, dtype=np.float64).reshape(-1)
    if a.size == 0 or b.size == 0:
        raise SimilarityError("empty_embedding", "嵌入向量为空")
    if a.size != b.size:
        raise SimilarityError(
            "dim_mismatch", f"嵌入维度不一致：{a.size} vs {b.size}"
        )
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator <= EPS:
        raise SimilarityError("degenerate_embedding", "嵌入向量范数为 0")
    return float(np.clip(np.dot(a, b) / denominator, -1.0, 1.0))


def compare_embeddings(
    reference: np.ndarray,
    candidate: np.ndarray,
    *,
    threshold: float = 0.72,
    short_reference: bool = False,
) -> SimilarityResult:
    """比较两个已提取的嵌入。"""
    if not -1.0 <= threshold <= 1.0:
        raise SimilarityError("bad_threshold", f"阈值应在 [-1, 1]，实际 {threshold}")
    score = cosine_similarity(reference, candidate)
    warnings = [
        "MFCC 统计量是音色代理特征，不是神经说话人嵌入",
        f"阈值 {threshold:.2f} 未经标注数据校准，只建议做相对比较",
    ]
    if short_reference:
        warnings.append("参考音频短于 3 秒，分数波动较大")
    return SimilarityResult(
        score=score,
        threshold=threshold,
        passed=score >= threshold,
        warnings=tuple(warnings),
    )


def compare(
    reference: np.ndarray,
    candidate: np.ndarray,
    sample_rate: int,
    *,
    threshold: float = 0.72,
) -> SimilarityResult:
    """从两段音频直接计算相似度。"""
    ref = extract_embedding(reference, sample_rate)
    cand = extract_embedding(candidate, sample_rate)
    return compare_embeddings(
        ref.vector,
        cand.vector,
        threshold=threshold,
        short_reference=ref.duration_s < 3.0,
    )


def rank_candidates(
    reference: np.ndarray,
    candidates: list[tuple[str, np.ndarray]],
    sample_rate: int,
    *,
    threshold: float = 0.72,
) -> list[dict]:
    """对多个候选按相似度降序排序；空候选返回空列表。"""
    if not candidates:
        return []
    ref = extract_embedding(reference, sample_rate)
    results: list[dict] = []
    for name, audio in candidates:
        try:
            cand = extract_embedding(audio, sample_rate)
            result = compare_embeddings(
                ref.vector,
                cand.vector,
                threshold=threshold,
                short_reference=ref.duration_s < 3.0,
            )
            results.append({"name": name, **result.as_dict()})
        except SimilarityError as exc:
            results.append(
                {
                    "name": name,
                    "score": None,
                    "threshold": threshold,
                    "passed": False,
                    "method": METHOD,
                    "warnings": [str(exc)],
                    "error": exc.code,
                }
            )
    results.sort(key=lambda item: (-1.0 if item["score"] is None else item["score"]), reverse=True)
    return results

