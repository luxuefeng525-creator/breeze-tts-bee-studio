"""text_norm 单元测试：中英文数字、日期、单位、缩写、切分与边界。"""

from __future__ import annotations

import pytest

from tts_pipeline import text_norm
from tts_pipeline.text_norm import (
    Change,
    detect_lang,
    int_to_en,
    int_to_zh,
    normalize_text,
    split_for_tts,
    split_long_text,
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("0", "零"),
        ("7", "七"),
        ("10", "十"),
        ("11", "十一"),
        ("20", "二十"),
        ("101", "一百零一"),
        ("1001", "一千零一"),
        ("1010", "一千零一十"),
        ("10000", "一万"),
        ("10001", "一万零一"),
        ("100010000", "一亿零一万"),
        ("100000001", "一亿零一"),
        ("123456789", "一亿二千三百四十五万六千七百八十九"),
    ],
)
def test_chinese_integers(source: str, expected: str) -> None:
    assert normalize_text(source, lang="zh").text == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("3.14", "三点一四"),
        ("0.5", "零点五"),
        ("-3.5", "负三点五"),
        ("1,234", "一千二百三十四"),
    ],
)
def test_chinese_decimals_and_separators(source: str, expected: str) -> None:
    assert normalize_text(source, lang="zh").text == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("2026-11-06", "二零二六年十一月六日"),
        ("2026/11/6", "二零二六年十一月六日"),
        ("2026年11月6日", "二零二六年十一月六日"),
        ("2026年11月", "二零二六年十一月"),
        ("11月6日", "十一月六日"),
        ("2026年", "二零二六年"),
    ],
)
def test_chinese_dates(source: str, expected: str) -> None:
    assert normalize_text(source, lang="zh").text == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("09:30", "九点三十分"),
        ("9:05", "九点零五分"),
        ("14:00", "十四点"),
        ("9:00-10:30", "九点到十点三十分"),
    ],
)
def test_chinese_times(source: str, expected: str) -> None:
    assert normalize_text(source, lang="zh").text == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("5kg", "五千克"),
        ("10km", "十千米"),
        ("25℃", "二十五摄氏度"),
        ("50%", "百分之五十"),
        ("12.5%", "百分之十二点五"),
        ("3.5万", "三万五千"),
        ("1.2亿", "一亿二千万"),
        ("3-5", "三到五"),
        ("3-5kg", "三到五千克"),
        ("1/2", "二分之一"),
        ("3/4", "四分之三"),
    ],
)
def test_chinese_units_percent_and_ranges(source: str, expected: str) -> None:
    assert normalize_text(source, lang="zh").text == expected


def test_chinese_phone_and_id_are_read_digit_by_digit() -> None:
    phone = normalize_text("13800138000", lang="zh").text
    assert phone == "一三八零零一三八零零零"
    identity = normalize_text("110101199003071234", lang="zh").text
    assert len(identity) == 18
    assert identity.startswith("一一零一")


def test_chinese_measure_words_and_ordinals() -> None:
    assert normalize_text("第3名", lang="zh").text == "第三名"
    assert normalize_text("3个", lang="zh").text == "三个"
    assert normalize_text("重5kg", lang="zh").text == "重五千克"


def test_currency_suffix_is_not_duplicated() -> None:
    assert normalize_text("¥12.5元", lang="zh").text == "十二点五元"
    assert normalize_text("$5", lang="zh").text == "五美元"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("0", "zero"),
        ("21", "twenty-one"),
        ("105", "one hundred five"),
        ("1001", "one thousand one"),
        ("123456", "one hundred twenty-three thousand four hundred fifty-six"),
        ("3.5", "three point five"),
        ("-2", "minus two"),
        ("1,234", "one thousand two hundred thirty-four"),
    ],
)
def test_english_numbers(source: str, expected: str) -> None:
    assert normalize_text(source, lang="en").text == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Nov 6, 2026", "November sixth, two thousand twenty-six"),
        ("6 November 2026", "November sixth, two thousand twenty-six"),
        ("2026-11-06", "November sixth, two thousand twenty-six"),
        ("9:30", "nine thirty"),
        ("9:05", "nine oh five"),
        ("9:00", "nine o'clock"),
        ("50%", "fifty percent"),
        ("12.5%", "twelve point five percent"),
        ("5 kg", "five kilograms"),
        ("1 km", "one kilometer"),
    ],
)
def test_english_dates_times_and_units(source: str, expected: str) -> None:
    assert normalize_text(source, lang="en").text == expected


def test_english_abbreviations() -> None:
    assert normalize_text("Dr. Smith", lang="en").text == "Doctor Smith"
    assert normalize_text("e.g.", lang="en").text == "for example"
    assert normalize_text("etc.", lang="en").text == "et cetera"
    assert normalize_text("U.S.", lang="en").text == "U S"
    assert normalize_text("1st 2nd 3rd 4th", lang="en").text == "first second third fourth"
    assert (
        normalize_text("Dr. Smith met Prof. Lee at 5 p.m.", lang="en").text
        == "Doctor Smith met Professor Lee at five P M."
    )


def test_english_currency() -> None:
    assert normalize_text("$5", lang="en").text == "five dollars"
    assert normalize_text("$5.50", lang="en").text == "five dollars and fifty cents"


def test_english_number_at_sentence_end() -> None:
    # 回归：数字后的句号不应阻止转换
    assert normalize_text("I have 3.", lang="en").text == "I have three."


def test_protected_spans_are_untouched() -> None:
    source = "看 https://example.com/2026/11/06?x=1 和 v1.2.3，邮件 a@b.com，代码 `x=1`"
    result = normalize_text(source, lang="zh")
    for token in (
        "https://example.com/2026/11/06?x=1",
        "v1.2.3",
        "a@b.com",
        "`x=1`",
    ):
        assert token in result.text


def test_changes_record_source_spans() -> None:
    result = normalize_text("2026-11-06", lang="zh")
    assert result.changes == [
        Change(
            start=0,
            end=10,
            kind="date_iso",
            before="2026-11-06",
            after="二零二六年十一月六日",
        )
    ]


def test_all_change_spans_point_at_original_text() -> None:
    source = "我在2026-11-06买了3.5kg苹果，花了¥12.5。"
    result = normalize_text(source, lang="zh")
    assert result.changes
    for change in result.changes:
        assert source[change.start : change.end] == change.before


def test_detect_lang() -> None:
    assert detect_lang("hello world") == "en"
    assert detect_lang("你好，世界") == "zh"
    assert detect_lang("我用 Python 写代码") == "zh"


def test_lang_can_be_forced() -> None:
    assert normalize_text("123", lang="zh").text == "一百二十三"
    assert normalize_text("123", lang="en").text == "one hundred twenty-three"


def test_fullwidth_digits_are_folded() -> None:
    assert normalize_text("２０２６年", lang="zh").text == "二零二六年"


def test_whitespace_normalization() -> None:
    result = normalize_text("你好   世界\t\n测试", lang="zh")
    assert result.text == "你好 世界\n测试"


def test_whitespace_can_be_disabled() -> None:
    result = normalize_text("a  b", normalize_whitespace=False)
    assert result.text == "a  b"


def test_empty_input() -> None:
    result = normalize_text("")
    assert result.text == ""
    assert result.changes == []


def test_non_string_input_is_rejected() -> None:
    with pytest.raises(TypeError):
        normalize_text(123)  # type: ignore[arg-type]


def test_long_input_warns() -> None:
    result = normalize_text("字" * 5001, lang="zh")
    assert result.warnings


@pytest.mark.parametrize(
    "source",
    [
        "2026-11-06",
        "3.5万",
        "13800138000",
        "A vs. B",
        "Nov 6, 2026",
        "Dr. Smith met Prof. Lee at 5 p.m.",
        "¥12.5元",
    ],
)
def test_normalization_is_idempotent(source: str) -> None:
    once = normalize_text(source).text
    twice = normalize_text(once).text
    assert once == twice


@pytest.mark.parametrize(
    "text",
    [
        "这是一段很长的中文文本，" * 12 + "结束。",
        "Dr. Smith went home. He slept well! Then he woke up.",
        "第一句。第二句！第三句？",
        "A" * 200,
        "中英混合 with some English words, 还有数字 12345。",
    ],
)
def test_split_preserves_every_character(text: str) -> None:
    parts = split_long_text(text, max_chars=40)
    assert "".join(parts) == text
    assert all(parts)
    assert all(len(part) <= 50 for part in parts)


def test_split_keeps_abbreviation_with_sentence() -> None:
    parts = split_long_text("Dr. Smith went home. He slept well!", max_chars=80)
    assert len(parts) == 2
    assert parts[0].startswith("Dr. Smith")
    assert "Dr." in parts[0]


def test_split_hard_cuts_an_unbreakable_token() -> None:
    parts = split_long_text("A" * 200, max_chars=40)
    assert parts == ["A" * 40] * 5


def test_split_merges_short_tail() -> None:
    parts = split_long_text("第一句。第二句！第三句？", max_chars=80)
    assert len(parts) == 1


def test_split_empty_and_invalid_limit() -> None:
    assert split_long_text("") == []
    with pytest.raises(ValueError):
        split_long_text("abc", max_chars=4)


def test_split_for_tts_normalizes_first() -> None:
    assert split_for_tts("今天是2026-11-06。", max_chars=80) == [
        "今天是二零二六年十一月六日。"
    ]


def test_selftest_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert text_norm.main(["--selftest"]) == 0
    assert "全部通过" in capsys.readouterr().out


def test_number_helpers_are_public() -> None:
    assert int_to_zh(2026) == "二千零二十六"
    assert int_to_en(2026) == "two thousand twenty-six"



# ---- W2-3a: 千分位金额/数字必须完整读取（回归：¥1,299.50 曾读成「一元,299.50」） ----
def test_currency_thousands_separator_reads_whole_amount():
    from tts_pipeline.text_norm import normalize_text
    r = normalize_text("订单金额¥1,299.50")
    assert r.text == "订单金额一千二百九十九点五零元", r.text


def test_currency_large_thousands_with_currency_suffix():
    from tts_pipeline.text_norm import normalize_text
    r = normalize_text("总价 $12,345.00")
    assert "12,345.00" not in r.text, r.text


def test_plain_number_with_thousands_separator():
    from tts_pipeline.text_norm import normalize_text
    r = normalize_text("共 1,234 件")
    assert "1,234" not in r.text, r.text
