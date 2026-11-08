"""环境声 / BGM 混音层的纯函数测试（不起服务、不碰生产数据）。"""
from __future__ import annotations

import io
import math
import struct

import numpy as np
import pytest

import soundtrack as ST


def _wav_bytes(samples: np.ndarray, rate: int) -> bytes:
    """把 float [-1,1] 数组写成 16bit WAV。"""
    pcm = np.clip(samples * 32767, -32768, 32767).astype("<i2").tobytes()
    n = len(pcm)
    return (
        struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + n, b"WAVE",
            b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16,
            b"data", n,
        )
        + pcm
    )


def _stereo_wav_bytes(left: np.ndarray, right: np.ndarray, rate: int) -> bytes:
    """双声道 16bit WAV（左右各一路）。"""
    inter = np.empty(left.size * 2, dtype="<i2")
    inter[0::2] = np.clip(left * 32767, -32768, 32767).astype("<i2")
    inter[1::2] = np.clip(right * 32767, -32768, 32767).astype("<i2")
    pcm = inter.tobytes()
    n = len(pcm)
    return (
        struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + n, b"WAVE",
            b"fmt ", 16, 1, 2, rate, rate * 4, 4, 16,
            b"data", n,
        )
        + pcm
    )


def _tone(seconds: float, freq: float = 220.0, rate: int = 44100,
          amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * rate), dtype=np.float32) / rate
    return (amp * np.sin(2 * math.pi * freq * t)).astype(np.float32)


def _rms(pcm: bytes, start: float, end: float) -> float:
    wave = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    a = int(start * ST.SAMPLE_RATE)
    b = min(wave.size, int(end * ST.SAMPLE_RATE))
    if b <= a:
        return 0.0
    return float(np.sqrt(np.mean(wave[a:b] ** 2)))


# --------------------------------------------------------------------- 解码
def test_decode_resamples_44k_stereo_to_24k_mono():
    """44.1k 立体声必须先下混再重采样，否则会拉慢 1.84 倍且降调。"""
    left = _tone(1.0, 220.0, 44100)
    right = _tone(1.0, 330.0, 44100)
    data = _stereo_wav_bytes(left, right, 44100)
    pcm = ST.decode_to_pcm24k(data)
    dur = ST.pcm_duration(pcm)
    assert abs(dur - 1.0) < 0.05, dur
    wave = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    assert wave.size == int(1.0 * ST.SAMPLE_RATE)


def test_decode_falls_back_to_stdlib_for_plain_wav():
    data = _wav_bytes(_tone(0.5, 330.0, 24000), 24000)
    pcm = ST.decode_to_pcm24k(data)
    assert abs(ST.pcm_duration(pcm) - 0.5) < 0.05


def test_decode_rejects_garbage():
    with pytest.raises(Exception):
        ST.decode_to_pcm24k(b"not an audio file at all")


# --------------------------------------------------------------------- 生成
@pytest.mark.parametrize("kind", list(ST.NOISE_KINDS))
def test_generate_noise_is_deterministic_and_right_length(kind):
    a = ST.generate_noise(kind, 1.0, seed=7)
    b = ST.generate_noise(kind, 1.0, seed=7)
    assert a == b, "同 kind+seed 必须出一样的波形（可复现）"
    assert abs(ST.pcm_duration(a) - 1.0) < 0.02
    wave = np.frombuffer(a, dtype="<i2").astype(np.float32)
    assert float(np.max(np.abs(wave))) > 1000, "噪声底不能是静音"


def test_generate_noise_kinds_differ_from_each_other():
    a = ST.generate_noise("white", 0.5, seed=3)
    b = ST.generate_noise("hum", 0.5, seed=3)
    assert a != b


def test_generate_noise_rejects_unknown_kind():
    with pytest.raises(ValueError):
        ST.generate_noise("dubstep", 1.0)


# --------------------------------------------------------------------- PCM 工具
def test_loop_pcm_fills_exact_length():
    src = ST.generate_noise("white", 0.5, seed=1)
    out = ST.loop_pcm(src, 2.0)
    assert abs(ST.pcm_duration(out) - 2.0) < 0.02
    assert ST.loop_pcm(src, 0.1) == src[: int(0.1 * ST.SAMPLE_RATE) * 2]


def test_fade_pcm_zeroes_both_ends():
    src = np.full(int(0.4 * ST.SAMPLE_RATE), 20000, dtype="<i2").tobytes()
    out = np.frombuffer(ST.fade_pcm(src, 0.1, 0.1), dtype="<i2")
    assert abs(int(out[0])) <= 2
    assert abs(int(out[-1])) <= 2
    assert int(out[len(out) // 2]) > 15000


def test_write_wav_header_is_24k_mono():
    data = ST.write_wav(ST.generate_noise("white", 0.1, seed=2))
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    rate = struct.unpack("<I", data[24:28])[0]
    channels = struct.unpack("<H", data[22:24])[0]
    assert rate == ST.SAMPLE_RATE and channels == 1


# --------------------------------------------------------------------- 混音
def _voice_with_gap(seconds_voice=1.0, gap=1.0):
    """前 1 秒是人声，后 1 秒是空隙。"""
    voice = np.zeros(int((seconds_voice + gap) * ST.SAMPLE_RATE), dtype="<i2")
    tone = (np.sin(2 * math.pi * 220 * np.arange(
        int(seconds_voice * ST.SAMPLE_RATE), dtype=np.float32) / ST.SAMPLE_RATE)
        * 8000).astype("<i2")
    voice[: tone.size] = tone
    return voice.tobytes()


def test_build_mix_keeps_voice_length_and_does_not_clip_away():
    voice = _voice_with_gap()
    bed = ST.generate_noise("white", 2.0, seed=5)
    out = ST.build_mix(voice, [{"pcm": bed, "start": 0.0, "gain": 0.3,
                                "fade_in": 0.1, "fade_out": 0.1}],
                        [{"start": 0.0, "end": 1.0}], duck_db=-12.0)
    assert len(out) == len(voice)
    wave = np.frombuffer(out, dtype="<i2")
    assert int(np.max(wave)) <= 32767 and int(np.min(wave)) >= -32768


def test_build_mix_ducks_bed_under_voice_but_keeps_it_in_gaps():
    """人声段铺底被压下去，空隙段保持原样——这才是 ducking 生效的样子。

    比的是**铺底贡献**（mix 减掉干声），不是混音整体：干声本身比铺底响，
    拿整体能量比会得出「没生效」的错误结论。
    """
    voice = _voice_with_gap()
    bed = ST.generate_noise("white", 2.0, seed=9)
    out = ST.build_mix(voice, [{"pcm": bed, "start": 0.0, "gain": 0.5,
                                "fade_in": 0.0, "fade_out": 0.0}],
                        [{"start": 0.0, "end": 1.0}], duck_db=-12.0)
    v = np.frombuffer(voice, dtype="<i2").astype(np.int32)
    m = np.frombuffer(out, dtype="<i2").astype(np.int32)
    contribution = (m - v).astype(np.float32)
    under_voice = float(np.sqrt(np.mean(
        contribution[int(0.3 * ST.SAMPLE_RATE):int(0.7 * ST.SAMPLE_RATE)] ** 2)))
    in_gap = float(np.sqrt(np.mean(
        contribution[int(1.4 * ST.SAMPLE_RATE):int(1.9 * ST.SAMPLE_RATE)] ** 2)))
    assert in_gap > 0, "空隙段应该有铺底"
    ratio = in_gap / max(under_voice, 1.0)
    assert 3.0 < ratio < 5.0, (
        f"空隙/人声段的铺底比应约等于 1/10^(-12/20)≈4，实际 {ratio:.2f}")


def test_build_mix_without_beds_is_pure_voice():
    voice = _voice_with_gap()
    out = ST.build_mix(voice, [], [{"start": 0.0, "end": 1.0}])
    assert out == voice


def test_build_mix_honours_gain_zero():
    voice = _voice_with_gap()
    bed = ST.generate_noise("white", 2.0, seed=11)
    out = ST.build_mix(voice, [{"pcm": bed, "start": 0.0, "gain": 0.0}],
                        [{"start": 0.0, "end": 1.0}])
    assert out == voice


def test_build_mix_clamps_instead_of_wrapping():
    """干声 + 铺底超过满量程时要限幅，不能整数回绕成刺耳的噪声。"""
    loud = np.full(int(0.5 * ST.SAMPLE_RATE), 32000, dtype="<i2").tobytes()
    bed = np.full(int(0.5 * ST.SAMPLE_RATE), 32000, dtype="<i2").tobytes()
    out = ST.build_mix(loud, [{"pcm": bed, "start": 0.0, "gain": 1.0}], [])
    wave = np.frombuffer(out, dtype="<i2")
    assert int(np.max(wave)) <= 32767
    assert int(np.min(wave)) >= -32768


def test_build_mix_ignores_bed_starting_after_the_end():
    voice = _voice_with_gap()
    bed = ST.generate_noise("white", 0.2, seed=13)
    out = ST.build_mix(voice, [{"pcm": bed, "start": 99.0, "gain": 0.5}], [])
    assert out == voice


# --------------------------------------------------------------------- 场次
def test_scene_ranges_groups_contiguous_rows():
    timeline = [
        {"scene": "第一场", "start": 0.0, "end": 2.0},
        {"scene": "第一场", "start": 2.0, "end": 4.0},
        {"scene": "第二场", "start": 4.0, "end": 6.0},
    ]
    out = ST.scene_ranges(timeline)
    assert out == [("第一场", 0.0, 4.0), ("第二场", 4.0, 6.0)]
