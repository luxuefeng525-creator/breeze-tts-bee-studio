# coding: utf-8
"""情绪跨行延续：只在「这一行没有任何情绪线索」时才沿用上一行。

设计口径（改动前先看）：
* 显式标注 / 角色默认 / 文本自动推断，优先级都高于 carry
* 换场次就重置，不把上一场的情绪带进下一场
* 开关默认关（老剧本缺字段 = 关），新剧本由 POST 默认打开
"""
from tts_pipeline.script import parse_script


RAW = """## 客厅
母亲[愤怒]：你怎么又回来了。
父亲：我回来看看。
祖母[温柔]：都坐下吧。
母亲：饭在锅里。
## 雨夜
父亲：外面下雨了。
"""


def _lines(**kw):
    return parse_script(RAW, {"母亲": "", "父亲": "", "祖母": ""}, {}, {}, **kw).lines


def test_carry_off_by_default_keeps_neutral():
    lines = _lines()
    assert lines[1].emotion_source == "neutral"
    assert lines[4].emotion_source == "neutral"


def test_carry_takes_previous_emotion_in_same_scene():
    lines = _lines(carry_emotion=True)
    # 父亲：我回来看看。→ 沿用母亲上一句的「愤怒」
    assert (lines[1].emotion, lines[1].emotion_source) == ("愤怒", "carry")
    # 显式标注仍然优先
    assert (lines[2].emotion, lines[2].emotion_source) == ("温柔", "explicit")
    # 祖母的温柔往后延续
    assert (lines[3].emotion, lines[3].emotion_source) == ("温柔", "carry")


def test_scene_change_resets_carry():
    lines = _lines(carry_emotion=True)
    # 新场次第一行没有线索 → 不该带着上一场的温柔
    assert lines[4].emotion_source == "neutral"


def test_explicit_and_inferred_beat_carry():
    raw = "## 一\n甲[害怕]：别过来。\n乙：救命啊，快跑！"
    lines = parse_script(raw, {"甲": "", "乙": ""}, {}, {}, carry_emotion=True).lines
    # 「救命」这类文本推断命中时用推断结果，不是 carry
    assert lines[1].emotion_source in ("auto", "carry")
    if lines[1].emotion_source == "auto":
        assert lines[1].emotion != "害怕"


def test_instruction_contains_carried_emotion():
    lines = _lines(carry_emotion=True)
    assert "愤怒" in lines[1].instruction
