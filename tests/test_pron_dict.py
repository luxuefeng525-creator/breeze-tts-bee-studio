"""tts_pipeline.pron_dict 单测（W2-3b / PRD P0-03 发音词典）"""
from __future__ import annotations

import json

import pytest

from tts_pipeline.pron_dict import DictEntry, PronunciationDict, pinyin_to_hanzi


# ---- 拼音转换 ----
def test_pinyin_tone_maps_to_expected_hanzi():
    assert pinyin_to_hanzi("hang2") == ("行", True)
    assert pinyin_to_hanzi("zhang3") == ("长", True)
    assert pinyin_to_hanzi("hang1") == ("航", True)


def test_pinyin_without_tone_falls_back_to_base():
    hanzi, ok = pinyin_to_hanzi("shi")
    assert ok is True
    assert hanzi == "是"


def test_unknown_pinyin_is_not_guessed():
    hanzi, ok = pinyin_to_hanzi("zzzz")
    assert ok is False
    assert hanzi == "zzzz"


# ---- 词典应用 ----
def test_hanzi_replacement_applies():
    d = PronunciationDict([{"word": "行长", "reading": "航长"}])
    text, hits = d.apply("银行行长来开会")
    assert text == "银行航长来开会"
    assert hits and hits[0]["original"] == "行长"


def test_pinyin_replacement_applies():
    d = PronunciationDict([{"word": "行长", "reading": "hang2 zhang3", "kind": "pinyin"}])
    text, hits = d.apply("银行行长来开会")
    assert text == "银行行长来开会"   # hang2→行, zhang3→长
    assert hits and hits[0]["kind"] == "pinyin"


def test_unresolved_pinyin_keeps_original_and_flags():
    d = PronunciationDict([{"word": "行长", "reading": "qqq zhang3", "kind": "pinyin"}])
    text, hits = d.apply("银行行长来开会")
    assert text == "银行行长来开会"
    assert hits and hits[0]["kind"] == "pinyin-unresolved"


def test_voice_scope_overrides_global():
    d = PronunciationDict([
        {"word": "行长", "reading": "航长", "scope": "global"},
        {"word": "行长", "reading": "兴长", "scope": "v_abc"},
    ])
    text, _ = d.apply("行长", scope="v_abc")
    assert text == "兴长"
    text, _ = d.apply("行长", scope="v_other")
    assert text == "航长"


def test_longer_word_wins_over_shorter():
    d = PronunciationDict([
        {"word": "长", "reading": "常"},
        {"word": "行长", "reading": "航长"},
    ])
    text, _ = d.apply("银行行长")
    assert text == "银行航长"


def test_add_replaces_same_word_same_scope():
    d = PronunciationDict()
    d.add({"word": "行长", "reading": "航长"})
    d.add({"word": "行长", "reading": "场长"})
    assert len(d.entries) == 1
    text, _ = d.apply("行长")
    assert text == "场长"


def test_remove_and_clear():
    d = PronunciationDict([
        {"word": "行长", "reading": "航长", "scope": "global"},
        {"word": "重", "reading": "虫", "scope": "v_x"},
    ])
    assert d.remove("行长") is True
    assert len(d.entries) == 1
    d.clear()
    assert d.entries == []


def test_empty_word_or_reading_rejected():
    d = PronunciationDict()
    with pytest.raises(ValueError):
        d.add({"word": "", "reading": "x"})
    with pytest.raises(ValueError):
        d.add({"word": "x", "reading": ""})


# ---- 持久化 ----
def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "dict.json"
    d = PronunciationDict([{"word": "行长", "reading": "航长", "scope": "global"}])
    d.save(path)
    loaded = PronunciationDict.load(path)
    assert len(loaded.entries) == 1
    assert loaded.entries[0].word == "行长"


def test_load_missing_file_returns_empty(tmp_path):
    d = PronunciationDict.load(tmp_path / "nope.json")
    assert d.entries == []


def test_load_corrupt_file_returns_empty(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert PronunciationDict.load(path).entries == []


# ---- 与文本规范化协同 ----
def test_dict_applies_after_normalization_and_wins():
    """PRD：用户纠正优先级最高，且不被口音预设悄悄改回。"""
    from tts_pipeline.text_norm import normalize_text

    d = PronunciationDict([{"word": "三点", "reading": "仨点"}])
    normalized = normalize_text("请于下午3点前回复").text
    assert normalized == "请于下午三点前回复"
    final, hits = d.apply(normalized)
    assert final == "请于下午仨点前回复"
    assert hits


# ---- 拼音读法自动识别（回归：曾被误判成 replace，文本里出现字面拼音） ----
def test_pinyin_reading_is_auto_detected():
    e = DictEntry.from_dict({"word": "行长", "reading": "hang2 zhang3"})
    assert e.kind == "pinyin", e.kind


def test_pinyin_reading_single_syllable_detected():
    e = DictEntry.from_dict({"word": "行", "reading": "hang2"})
    assert e.kind == "pinyin", e.kind


def test_hanzi_reading_is_not_pinyin():
    e = DictEntry.from_dict({"word": "行长", "reading": "航长"})
    assert e.kind == "replace", e.kind


def test_auto_detected_pinyin_applies_correctly():
    d = PronunciationDict([{"word": "行长", "reading": "hang2 zhang3"}])
    text, hits = d.apply("银行行长")
    assert text == "银行行长", text
    assert hits and hits[0]["kind"] == "pinyin"
