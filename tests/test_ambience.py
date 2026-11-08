"""三种场景底音（rain / fire / street）的合成契约。

只断言对外可观察的性质：字节长度、确定性、峰值/RMS 区间、未知 kind 报错、
生成耗时。具体滤波参数随时可调，不该被测试绑死。
"""
from __future__ import annotations

import time

import numpy as np
import pytest

import soundtrack as ST

AMBIENCE_KINDS = ["rain", "fire", "street"]
SECONDS = 2.0


def _wave(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32)


def _peak(pcm: bytes) -> float:
    return float(np.max(np.abs(_wave(pcm))))


def _rms(pcm: bytes) -> float:
    w = _wave(pcm)
    return float(np.sqrt(np.mean(w ** 2)))


@pytest.mark.parametrize("kind", AMBIENCE_KINDS)
def test_ambience_length_matches_seconds_and_implicit_rate(kind):
    """仍是 24kHz 单声道 s16le：字节数 = 秒数 × 24000 × 2。"""
    pcm = ST.generate_noise(kind, SECONDS, seed=42)
    assert len(pcm) == int(round(SECONDS * ST.SAMPLE_RATE)) * ST.BYTES_PER_SAMPLE
    assert abs(ST.pcm_duration(pcm) - SECONDS) < 1e-9
    assert len(pcm) % 2 == 0, "s16le 必须是偶数字节"


@pytest.mark.parametrize("kind", AMBIENCE_KINDS)
def test_ambience_is_byte_identical_for_same_seed(kind):
    a = ST.generate_noise(kind, 1.5, seed=20261107)
    b = ST.generate_noise(kind, 1.5, seed=20261107)
    assert a == b, "同 kind+seconds+seed 必须逐字节一致"


@pytest.mark.parametrize("kind", AMBIENCE_KINDS)
def test_ambience_differs_for_different_seed(kind):
    a = ST.generate_noise(kind, 1.0, seed=1)
    b = ST.generate_noise(kind, 1.0, seed=2)
    assert a != b
    assert len(a) == len(b)


@pytest.mark.parametrize("kind", AMBIENCE_KINDS)
def test_ambience_is_audible_but_has_headroom(kind):
    """峰值留 1~2dB 余量（< 0.85 满量程），同时不能小到听不见。"""
    pcm = ST.generate_noise(kind, 3.0, seed=5)
    peak = _peak(pcm)
    rms = _rms(pcm)
    assert peak > 2000, f"{kind} 峰值太小：{peak}"
    assert peak < 32767 * 0.85, f"{kind} 峰值过高，会削波：{peak}"
    assert rms > 500, f"{kind} RMS 接近静音：{rms}"
    assert rms < peak, f"{kind} RMS 不该超过峰值：{rms} vs {peak}"


@pytest.mark.parametrize("kind", AMBIENCE_KINDS)
def test_ambience_kinds_are_actually_different_waves(kind):
    others = [k for k in AMBIENCE_KINDS if k != kind]
    mine = ST.generate_noise(kind, 1.0, seed=7)
    for other in others:
        assert mine != ST.generate_noise(other, 1.0, seed=7)


def test_ambience_kinds_have_chinese_descriptions():
    for kind in AMBIENCE_KINDS:
        assert kind in ST.NOISE_KINDS
        desc = ST.NOISE_KINDS[kind]
        assert isinstance(desc, str) and desc
        assert any("\u4e00" <= ch <= "\u9fff" for ch in desc), desc


def test_ambience_rejects_unknown_kind():
    with pytest.raises(ValueError):
        ST.generate_noise("river", 1.0)


def test_ambience_30s_generation_is_fast():
    """30 秒音频必须在 1.5 秒内合成完（向量化，无逐采样循环）。"""
    for kind in AMBIENCE_KINDS:
        t0 = time.perf_counter()
        pcm = ST.generate_noise(kind, 30.0, seed=20261107)
        cost = time.perf_counter() - t0
        assert len(pcm) == 30 * ST.SAMPLE_RATE * ST.BYTES_PER_SAMPLE
        assert cost < 1.5, f"{kind} 30 秒合成耗时 {cost:.3f}s，超预算"
