"""ref_prep 单元测试：声道、质量门、VAD、窗口选择、响度、重采样、WAV 往返。"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from tts_pipeline.ref_prep import (
    RefPrepError,
    assess_audio,
    normalize_loudness,
    prepare_reference,
    prepare_wav_bytes,
    resample_linear,
    select_reference_window,
    to_mono,
    trim_silence,
    write_wav_bytes,
)

SR = 24000


def tone(
    freq: float = 220.0,
    duration: float = 4.0,
    amp: float = 0.1,
    sample_rate: int = SR,
    phase: float = 0.0,
) -> np.ndarray:
    t = np.arange(int(sample_rate * duration)) / sample_rate
    return (amp * np.sin(2 * np.pi * freq * t + phase)).astype(np.float32)


def silence(duration: float, sample_rate: int = SR) -> np.ndarray:
    return np.zeros(int(sample_rate * duration), dtype=np.float32)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


def issue_codes(report) -> set[str]:
    return {issue.code for issue in report.issues}


def test_to_mono_averages_channels() -> None:
    left = tone(220)
    right = tone(440)
    mono = to_mono(np.column_stack([left, right]))
    np.testing.assert_allclose(mono, (left + right) / 2, atol=1e-6)
    assert mono.dtype == np.float32


def test_to_mono_keeps_mono() -> None:
    source = tone(220)
    np.testing.assert_allclose(to_mono(source), source, atol=1e-7)


def test_to_mono_rejects_three_dimensions() -> None:
    with pytest.raises(RefPrepError) as excinfo:
        to_mono(np.zeros((10, 2, 2), dtype=np.float32))
    assert excinfo.value.code == "bad_shape"


def test_clean_tone_is_ready() -> None:
    report = assess_audio(tone(duration=4.0), SR)
    assert report.ready
    assert report.score == 100
    assert report.duration_s == pytest.approx(4.0, abs=1e-3)
    assert report.snr_db is not None and report.snr_db > 30


def test_short_audio_is_blocked() -> None:
    report = assess_audio(tone(duration=1.0), SR)
    assert not report.ready
    assert "too_short" in issue_codes(report)


def test_silent_audio_is_blocked() -> None:
    report = assess_audio(silence(4.0), SR)
    assert not report.ready
    assert "silent" in issue_codes(report)


def test_clipped_audio_is_blocked() -> None:
    clipped = np.clip(tone(amp=1.5, duration=4.0), -1.0, 1.0)
    report = assess_audio(clipped, SR)
    assert not report.ready
    assert "clipped" in issue_codes(report)


def test_dc_offset_warns_but_does_not_block() -> None:
    report = assess_audio(tone(duration=4.0) + 0.05, SR)
    assert report.ready
    assert "dc_offset" in issue_codes(report)


def test_noisy_audio_is_flagged() -> None:
    rng = np.random.default_rng(0)
    noisy = rng.normal(0, 0.1, SR * 4).astype(np.float32)
    report = assess_audio(noisy, SR)
    assert "noisy" in issue_codes(report)
    assert report.snr_db is not None and report.snr_db < 12


def test_too_long_audio_warns() -> None:
    report = assess_audio(tone(duration=20.0), SR)
    assert report.ready
    assert "too_long" in issue_codes(report)


def test_trim_silence_removes_edges() -> None:
    source = np.concatenate([silence(0.5), tone(duration=2.0), silence(0.5)])
    trimmed, span = trim_silence(source, SR)
    assert len(trimmed) < len(source)
    assert 1.9 <= len(trimmed) / SR <= 2.5
    assert span[0] < 0.6
    assert span[1] > 2.4


def test_trim_silence_all_silent_is_unchanged() -> None:
    source = silence(2.0)
    trimmed, span = trim_silence(source, SR)
    assert len(trimmed) == len(source)
    assert span == (0.0, pytest.approx(2.0))


def test_select_window_prefers_loud_section() -> None:
    quiet = np.full(10 * SR, 0.001, dtype=np.float32)
    loud = tone(duration=10.0, amp=0.2)
    source = np.concatenate([quiet, loud, quiet])
    start, end = select_reference_window(source, SR, target_s=8, max_s=15)
    assert end - start == 8 * SR
    assert start >= 9 * SR
    assert end <= 21 * SR


def test_select_window_keeps_short_audio() -> None:
    source = tone(duration=5.0)
    assert select_reference_window(source, SR, target_s=8, max_s=15) == (
        0,
        len(source),
    )


def test_normalize_loudness_hits_target_and_ceiling() -> None:
    source = tone(amp=0.05, duration=4.0)
    out, gain_db = normalize_loudness(source, SR, target_dbfs=-20.0)
    assert 7.0 < gain_db < 11.0
    assert abs(20 * math.log10(rms(out)) + 20.0) < 1.0
    assert float(np.max(np.abs(out))) <= 10 ** (-1.0 / 20.0) + 1e-6


def test_normalize_silence_is_noop() -> None:
    source = silence(1.0)
    out, gain_db = normalize_loudness(source, SR)
    assert gain_db == 0.0
    assert np.array_equal(out, source)


def test_resample_preserves_tone_and_length() -> None:
    source = tone(freq=440.0, duration=1.0, amp=0.2, sample_rate=48000)
    out = resample_linear(source, 48000, 24000)
    assert out.size == 24000
    spectrum = np.abs(np.fft.rfft(out))
    peak = float(np.argmax(spectrum)) * 24000 / out.size
    assert abs(peak - 440.0) < 10.0


def test_resample_same_rate_returns_copy() -> None:
    source = tone(duration=1.0)
    out = resample_linear(source, SR, SR)
    assert np.array_equal(out, source)
    assert out is not source


def test_prepare_short_reference_is_not_ready() -> None:
    prepared = prepare_reference(tone(duration=1.0), SR)
    assert not prepared.quality.ready
    assert "too_short" in issue_codes(prepared.quality)
    assert prepared.duration_s == pytest.approx(1.0, abs=1e-3)


def test_prepare_long_reference_trims_to_limit() -> None:
    prepared = prepare_reference(tone(duration=40.0), SR)
    assert prepared.quality.ready
    assert 11.0 <= prepared.duration_s <= 15.0
    assert "select_window" in prepared.operations
    assert "trimmed_long" in issue_codes(prepared.quality)


def test_prepare_stereo_reports_downmix() -> None:
    stereo = np.column_stack([tone(duration=4.0), tone(duration=4.0, amp=0.05)])
    prepared = prepare_reference(stereo, SR)
    assert prepared.quality.channels == 2
    assert "downmix" in prepared.operations
    assert "stereo_downmix" in issue_codes(prepared.quality)


def test_prepare_records_loudness_operation() -> None:
    prepared = prepare_reference(tone(duration=4.0, amp=0.01), SR)
    assert any(op.startswith("loudness:") for op in prepared.operations)


def test_prepare_wav_bytes_roundtrip() -> None:
    data = write_wav_bytes(tone(duration=4.0), SR)
    prepared = prepare_wav_bytes(data)
    assert prepared.quality.ready
    assert prepared.duration_s == pytest.approx(4.0, abs=0.01)


def test_prepare_wav_bytes_rejects_garbage() -> None:
    with pytest.raises(RefPrepError) as excinfo:
        prepare_wav_bytes(b"not an audio file")
    assert excinfo.value.code == "decode_failed"


def test_prepare_rejects_non_finite() -> None:
    bad = np.array([0.1, np.nan, 0.2], dtype=np.float32)
    with pytest.raises(RefPrepError) as excinfo:
        prepare_reference(bad, SR)
    assert excinfo.value.code == "non_finite"


def test_quality_report_is_json_serializable() -> None:
    report = assess_audio(tone(duration=4.0), SR)
    payload = json.dumps(report.as_dict(), ensure_ascii=False)
    assert '"ready": true' in payload

