"""similarity 单元测试：嵌入、余弦、阈值、降级和边界输入。"""

from __future__ import annotations

import numpy as np
import pytest

from tts_pipeline.similarity import (
    SimilarityError,
    compare,
    compare_embeddings,
    cosine_similarity,
    extract_embedding,
    rank_candidates,
)

SR = 24000


def voice(
    f1: float,
    f2: float,
    duration: float = 4.0,
    phase: float = 0.0,
    sample_rate: int = SR,
) -> np.ndarray:
    t = np.arange(int(sample_rate * duration)) / sample_rate
    return (
        0.10 * np.sin(2 * np.pi * f1 * t + phase)
        + 0.03 * np.sin(2 * np.pi * f2 * t + phase * 0.5)
    ).astype(np.float32)


def same_voice_a() -> np.ndarray:
    return voice(180.0, 900.0)


def same_voice_b() -> np.ndarray:
    return voice(180.0, 900.0, phase=0.4)


def other_voice() -> np.ndarray:
    return voice(1800.0, 3500.0)


def test_embedding_shape_and_norm() -> None:
    embedding = extract_embedding(same_voice_a(), SR)
    assert embedding.vector.shape == (42,)
    assert np.linalg.norm(embedding.vector) == pytest.approx(1.0, abs=1e-6)
    assert embedding.frames > 0
    assert embedding.method == "mfcc-stats-v1"


def test_same_timbre_scores_high() -> None:
    left = extract_embedding(same_voice_a(), SR)
    right = extract_embedding(same_voice_b(), SR)
    score = cosine_similarity(left.vector, right.vector)
    assert score > 0.95


def test_different_timbre_scores_low() -> None:
    left = extract_embedding(same_voice_a(), SR)
    right = extract_embedding(other_voice(), SR)
    score = cosine_similarity(left.vector, right.vector)
    assert score < 0.5
    assert cosine_similarity(
        extract_embedding(same_voice_a(), SR).vector,
        extract_embedding(same_voice_b(), SR).vector,
    ) > score + 0.3


def test_compare_returns_explainable_result() -> None:
    result = compare(same_voice_a(), same_voice_b(), SR)
    assert result.passed
    assert result.score > 0.95
    assert result.threshold == 0.72
    assert result.method == "mfcc-stats-v1"
    assert any("不是神经说话人嵌入" in warning for warning in result.warnings)
    assert any("未经标注数据校准" in warning for warning in result.warnings)
    payload = result.as_dict()
    assert payload["passed"] is True
    assert 0.0 <= payload["score"] <= 1.0


def test_compare_different_voice_fails_threshold() -> None:
    result = compare(same_voice_a(), other_voice(), SR)
    assert not result.passed
    assert result.score < 0.72


def test_threshold_can_be_stricter() -> None:
    result = compare(same_voice_a(), same_voice_b(), SR, threshold=0.99999)
    assert not result.passed


def test_short_reference_adds_warning() -> None:
    short = voice(180.0, 900.0, duration=2.0)
    result = compare(short, same_voice_b(), SR)
    assert any("短于 3 秒" in warning for warning in result.warnings)


def test_cosine_identity_and_opposite() -> None:
    assert cosine_similarity(np.array([1.0, 0.0]), np.array([1.0, 0.0])) == pytest.approx(
        1.0
    )
    assert cosine_similarity(np.array([1.0, 0.0]), np.array([-1.0, 0.0])) == pytest.approx(
        -1.0
    )


def test_cosine_rejects_dimension_mismatch() -> None:
    with pytest.raises(SimilarityError) as excinfo:
        cosine_similarity(np.array([1.0, 2.0]), np.array([1.0]))
    assert excinfo.value.code == "dim_mismatch"


def test_cosine_rejects_zero_vector() -> None:
    with pytest.raises(SimilarityError) as excinfo:
        cosine_similarity(np.zeros(4), np.ones(4))
    assert excinfo.value.code == "degenerate_embedding"


def test_compare_embeddings_accepts_precomputed_vectors() -> None:
    left = np.array([1.0, 0.0, 0.0])
    right = np.array([0.8, 0.6, 0.0])
    result = compare_embeddings(left, right, threshold=0.7)
    assert result.score == pytest.approx(0.8, abs=1e-9)
    assert result.passed


def test_compare_embeddings_validates_threshold() -> None:
    with pytest.raises(SimilarityError) as excinfo:
        compare_embeddings(np.array([1.0]), np.array([1.0]), threshold=2.0)
    assert excinfo.value.code == "bad_threshold"


def test_too_short_audio_raises() -> None:
    with pytest.raises(SimilarityError) as excinfo:
        extract_embedding(voice(180.0, 900.0, duration=0.1), SR)
    assert excinfo.value.code == "too_short"


def test_silent_audio_raises() -> None:
    with pytest.raises(SimilarityError) as excinfo:
        extract_embedding(np.zeros(SR, dtype=np.float32), SR)
    assert excinfo.value.code == "silent"


def test_empty_audio_raises() -> None:
    with pytest.raises(SimilarityError) as excinfo:
        extract_embedding(np.array([], dtype=np.float32), SR)
    assert excinfo.value.code == "empty_audio"


def test_bad_shape_raises() -> None:
    with pytest.raises(SimilarityError) as excinfo:
        extract_embedding(np.zeros((100, 2, 2), dtype=np.float32), SR)
    assert excinfo.value.code == "bad_shape"


def test_bad_sample_rate_raises() -> None:
    with pytest.raises(SimilarityError) as excinfo:
        extract_embedding(same_voice_a(), 0)
    assert excinfo.value.code == "bad_sample_rate"


def test_non_finite_audio_raises() -> None:
    bad = np.full(SR, np.nan, dtype=np.float32)
    with pytest.raises(SimilarityError) as excinfo:
        extract_embedding(bad, SR)
    assert excinfo.value.code == "non_finite"


def test_stereo_audio_is_supported() -> None:
    mono = same_voice_a()
    stereo = np.column_stack([mono, mono * 0.5])
    embedding = extract_embedding(stereo, SR)
    assert embedding.vector.shape == (42,)


def test_embedding_is_deterministic() -> None:
    first = extract_embedding(same_voice_a(), SR)
    second = extract_embedding(same_voice_a(), SR)
    np.testing.assert_array_equal(first.vector, second.vector)


def test_rank_candidates_sorts_and_reports_errors() -> None:
    ranked = rank_candidates(
        same_voice_a(),
        [
            ("other", other_voice()),
            ("same", same_voice_b()),
            ("broken", np.zeros(100, dtype=np.float32)),
        ],
        SR,
    )
    assert ranked[0]["name"] == "same"
    assert ranked[0]["passed"] is True
    assert ranked[-1]["name"] == "broken"
    assert ranked[-1]["error"] == "too_short"
    assert ranked[-1]["score"] is None


def test_rank_empty_candidates() -> None:
    assert rank_candidates(same_voice_a(), [], SR) == []

