"""剧本解析与情绪推断的单元测试（纯 CPU，毫秒级）。"""
from __future__ import annotations

import pytest

from tts_pipeline.script import (
    ARCHETYPES,
    EMOTIONS,
    NEUTRAL,
    build_instruction,
    emotion_phrase,
    estimate_seconds,
    infer_emotion,
    parse_script,
    resolve_emotion,
)

ROLE_DEFAULTS = {"旁白": "中性", "雪峰": "中性", "小满": "迟疑"}
ROLE_INSTR = {
    "旁白": ARCHETYPES["narrator"]["instruction"],
    "雪峰": ARCHETYPES["adult_m"]["instruction"],
    "小满": ARCHETYPES["teen_f"]["instruction"],
}


# ---------------------------------------------------------------- 解析：语法
def test_parses_three_tag_syntaxes():
    raw = "\n".join([
        "旁白：那天雨很大。",
        "雪峰[愤怒]：你再说一遍？",
        "小满（小声）：我不敢……",
    ])
    p = parse_script(raw, ROLE_DEFAULTS, ROLE_INSTR)
    assert p.ok
    assert [l.role_name for l in p.lines] == ["旁白", "雪峰", "小满"]
    assert p.lines[1].emotion == "愤怒"
    assert p.lines[2].emotion == "小声"        # 认不出的标签按自由指令
    assert p.lines[0].text == "那天雨很大。"


def test_fullwidth_colon_and_brackets_are_accepted():
    p = parse_script("雪峰［愤怒］：走开！", ROLE_DEFAULTS, ROLE_INSTR)
    assert p.ok
    assert p.lines[0].role_name == "雪峰"
    assert p.lines[0].emotion == "愤怒"


def test_scene_headers_are_collected_and_attached():
    raw = "## 深夜书店\n旁白：灯还亮着。\n## 医院走廊\n雪峰：人呢？"
    p = parse_script(raw, ROLE_DEFAULTS, ROLE_INSTR)
    assert p.scenes == ["深夜书店", "医院走廊"]
    assert p.lines[0].scene == "深夜书店"
    assert p.lines[1].scene == "医院走廊"


def test_blank_lines_are_ignored():
    p = parse_script("旁白：一。\n\n\n旁白：二。", ROLE_DEFAULTS, ROLE_INSTR)
    assert len(p.lines) == 2


def test_line_without_colon_is_reported_not_dropped():
    p = parse_script("旁白：好。\n这句忘了写角色", ROLE_DEFAULTS, ROLE_INSTR)
    assert len(p.lines) == 1
    assert p.errors and "认不出这一行" in p.errors[0]["reason"]


def test_unknown_role_is_flagged_but_line_survives():
    p = parse_script("陌生人：你好。", ROLE_DEFAULTS, ROLE_INSTR)
    assert len(p.lines) == 1
    assert p.lines[0].error == "未知角色"
    assert any("未知角色「陌生人」" in e["reason"] for e in p.errors)


def test_empty_script_parses_to_nothing():
    p = parse_script("", ROLE_DEFAULTS, ROLE_INSTR)
    assert p.lines == [] and p.ok


# ---------------------------------------------------------------- 情绪优先级
def test_explicit_tag_beats_role_default():
    p = parse_script("小满[开心]：我不怕！", ROLE_DEFAULTS, ROLE_INSTR)
    assert p.lines[0].emotion == "开心"
    assert p.lines[0].emotion_source == "explicit"


def test_role_default_applies_when_no_tag():
    p = parse_script("小满：我不太确定。", ROLE_DEFAULTS, ROLE_INSTR)
    assert p.lines[0].emotion == "迟疑"
    assert p.lines[0].emotion_source == "role"


def test_auto_inference_when_role_has_no_default():
    roles = {"雪峰": ""}
    p = parse_script("雪峰：你给我滚出去！", roles, ROLE_INSTR)
    assert p.lines[0].emotion == "愤怒"
    assert p.lines[0].emotion_source == "auto"


def test_neutral_when_nothing_suggests_otherwise():
    roles = {"雪峰": ""}
    p = parse_script("雪峰：桌上有一本书。", roles, ROLE_INSTR)
    assert p.lines[0].emotion == NEUTRAL
    assert p.lines[0].emotion_source == "neutral"


@pytest.mark.parametrize("text,expected", [
    ("我哭了。", "悲伤"),
    ("哈哈哈太好了！", "开心"),
    ("别过来，我怕。", "恐惧"),
    ("你说什么？？", "紧张"),
    ("我想想……", "迟疑"),
    ("真的吗？", "迟疑"),
    ("今天 Twenty 号。", None),
])
def test_infer_emotion_cases(text, expected):
    assert infer_emotion(text) == expected


def test_keyword_beats_punctuation():
    # 「我哭了」+ 感叹号：哭比感叹号更能说明情绪
    assert infer_emotion("我哭了！") == "悲伤"


def test_free_form_tag_is_kept_as_instruction():
    p = parse_script("雪峰[压低声音]：别出声。", ROLE_DEFAULTS, ROLE_INSTR)
    assert p.lines[0].emotion == "压低声音"
    assert "压低声音" in p.lines[0].instruction


# ---------------------------------------------------------------- 指令拼装
def test_instruction_joins_mood_then_voice_then_emotion():
    out = build_instruction("成年男声", "愤怒", "夜晚的紧张氛围")
    assert out.index("夜晚的紧张氛围") < out.index("成年男声") < out.index("语气愤怒")


def test_neutral_emotion_adds_nothing():
    assert build_instruction("成年男声", NEUTRAL) == "成年男声"
    assert emotion_phrase(NEUTRAL) == ""


def test_instruction_dedupes_identical_pieces():
    same = "沉稳旁白"
    assert build_instruction(same, NEUTRAL, same) == same


def test_known_emotion_phrase_maps_to_words():
    assert "愤怒" in EMOTIONS["愤怒"]
    assert emotion_phrase("愤怒") == EMOTIONS["愤怒"]


def test_resolve_emotion_unknown_role_default_is_ignored():
    emotion, source = resolve_emotion("今天天气不错。", None, "不存在的情绪")
    assert source == "neutral"


# ---------------------------------------------------------------- 时长估算
def test_estimate_scales_with_chars_and_gaps():
    p = parse_script("旁白：一二三四五六七八九十。\n旁白：一二三四五六七八九十。",
                     ROLE_DEFAULTS, ROLE_INSTR)
    only_text = estimate_seconds(p, chars_per_sec=4.6, gap=0.0)
    with_gap = estimate_seconds(p, chars_per_sec=4.6, gap=0.35)
    assert with_gap > only_text
    assert with_gap - only_text == pytest.approx(0.35, abs=0.01)


def test_estimate_empty_is_zero():
    assert estimate_seconds(parse_script("", ROLE_DEFAULTS, ROLE_INSTR)) == 0.0


# ---------------------------------------------------------------- 原型表
def test_seven_base_archetypes_present():
    base = {"narrator", "elder_m", "elder_f", "teen_m", "teen_f", "adult_m", "adult_f"}
    assert base <= set(ARCHETYPES)
    for key in base:
        assert ARCHETYPES[key]["instruction"], key
        assert ARCHETYPES[key]["default_emotion"] in EMOTIONS, key


def test_every_archetype_default_emotion_is_known():
    for key, spec in ARCHETYPES.items():
        assert spec["default_emotion"] in EMOTIONS, key
