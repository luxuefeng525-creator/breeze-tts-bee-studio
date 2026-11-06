"""中英文文本规范化与长句切分。

设计约束：
- 纯 CPU、仅标准库，不依赖网络和模型；
- 原文到规范化文本保留逐条变更记录（来源区间、类型、替换前后），便于排查和 A/B；
- 规则只改写命中的片段，未命中部分逐字保留，不做整段重写；
- 默认规则偏保守：URL、邮箱、版本号、代码片段原样保留。

适用场景是 TTS 前的“可读化”，不是排版或机器翻译。语义歧义（例如 11/06
到底是日期还是分数）无法在纯规则层完全消除，调用方应优先传入 `lang`。
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field

__all__ = [
    "Change",
    "NormalizedText",
    "detect_lang",
    "int_to_en",
    "int_to_zh",
    "normalize_text",
    "number_to_en",
    "number_to_zh",
    "ordinal_en",
    "split_for_tts",
    "split_long_text",
]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """一条替换记录；start/end 是原文中的半开区间。"""

    start: int
    end: int
    kind: str
    before: str
    after: str


@dataclass
class NormalizedText:
    """规范化结果。

    `changes` 只记录真正发生变化的片段；被保护而原样保留的片段不会出现。
    """

    source: str
    text: str
    lang: str
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "text": self.text,
            "lang": self.lang,
            "changes": [
                {
                    "start": c.start,
                    "end": c.end,
                    "kind": c.kind,
                    "before": c.before,
                    "after": c.after,
                }
                for c in self.changes
            ],
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# 中文数字
# ---------------------------------------------------------------------------

_ZH_DIGITS = "零一二三四五六七八九"


def _section_to_zh(section: int, *, leading: bool) -> str:
    """0..9999 的四位一节转中文；leading 控制 10..19 是否读作“十X”。"""
    text = f"{section:04d}"
    units = ("千", "百", "十", "")
    out = ""
    pending_zero = False
    for index, char in enumerate(text):
        digit = int(char)
        if digit == 0:
            if out:
                pending_zero = True
            continue
        if pending_zero and out:
            out += "零"
            pending_zero = False
        if leading and digit == 1 and units[index] == "十" and not out:
            out += "十"
        else:
            out += _ZH_DIGITS[digit] + units[index]
    return out


def int_to_zh(number: int) -> str:
    """整数转中文读法；支持到万亿级，超出后退化为逐位读。"""
    if number < 0:
        return "负" + int_to_zh(-number)
    if number == 0:
        return "零"
    if number >= 10**16:
        return "".join(_ZH_DIGITS[int(c)] for c in str(number))

    units = ("", "万", "亿", "万亿")
    groups: list[int] = []
    rest = number
    while rest:
        groups.append(rest % 10000)
        rest //= 10000

    out = ""
    for index in range(len(groups) - 1, -1, -1):
        section = groups[index]
        if section == 0:
            # 只有低位还有非零节时才补“零”，避免“一亿零”这种尾巴
            if out and any(groups[:index]) and not out.endswith("零"):
                out += "零"
            continue
        if out and section < 1000 and not out.endswith("零"):
            out += "零"
        out += _section_to_zh(section, leading=(index == len(groups) - 1)) + units[index]
    return out


def number_to_zh(value: str) -> str:
    """数字字符串（可带负号、千分位、小数点）转中文。"""
    negative = value.startswith("-")
    raw = value.lstrip("+-").replace(",", "")
    if not raw:
        raise ValueError("empty number")
    if "." in raw:
        integer, fraction = raw.split(".", 1)
        head = int_to_zh(int(integer or "0"))
        tail = "".join(_ZH_DIGITS[int(c)] for c in fraction if c.isdigit())
        out = head + "点" + tail
    else:
        out = int_to_zh(int(raw))
    return ("负" if negative else "") + out


def _digits_to_zh(text: str) -> str:
    """逐位读，用于年份、手机号、证件号。"""
    return "".join(_ZH_DIGITS[int(c)] if c.isdigit() else c for c in text)


# ---------------------------------------------------------------------------
# 英文数字
# ---------------------------------------------------------------------------

_EN_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_EN_TENS = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)


def int_to_en(number: int) -> str:
    """整数转英文读法；支持到万亿级。"""
    if number < 0:
        return "minus " + int_to_en(-number)
    if number < 20:
        return _EN_ONES[number]
    if number < 100:
        tens, ones = divmod(number, 10)
        return _EN_TENS[tens] + (f"-{_EN_ONES[ones]}" if ones else "")
    if number < 1000:
        hundreds, rest = divmod(number, 100)
        head = f"{_EN_ONES[hundreds]} hundred"
        return head + (f" {int_to_en(rest)}" if rest else "")
    for scale, name in (
        (10**12, "trillion"),
        (10**9, "billion"),
        (10**6, "million"),
        (1000, "thousand"),
    ):
        if number >= scale:
            head = f"{int_to_en(number // scale)} {name}"
            rest = number % scale
            return head + (f" {int_to_en(rest)}" if rest else "")
    return str(number)  # 理论上不可达


_EN_ORDINAL_SMALL = {
    1: "first",
    2: "second",
    3: "third",
    4: "fourth",
    5: "fifth",
    6: "sixth",
    7: "seventh",
    8: "eighth",
    9: "ninth",
    10: "tenth",
    11: "eleventh",
    12: "twelfth",
    13: "thirteenth",
    14: "fourteenth",
    15: "fifteenth",
    16: "sixteenth",
    17: "seventeenth",
    18: "eighteenth",
    19: "nineteenth",
    20: "twentieth",
    30: "thirtieth",
    40: "fortieth",
    50: "fiftieth",
    60: "sixtieth",
    70: "seventieth",
    80: "eightieth",
    90: "ninetieth",
}


def ordinal_en(number: int) -> str:
    """整数转英文序数词。"""
    if number < 0:
        return "minus " + ordinal_en(-number)
    if number in _EN_ORDINAL_SMALL:
        return _EN_ORDINAL_SMALL[number]
    if number < 100:
        tens, ones = divmod(number, 10)
        return _EN_TENS[tens] + "-" + _EN_ORDINAL_SMALL[ones]
    if number < 1000:
        hundreds, rest = divmod(number, 100)
        head = f"{_EN_ONES[hundreds]} hundred"
        if rest == 0:
            return head + "th"
        return head + " " + ordinal_en(rest)
    for scale, name in ((10**9, "billion"), (10**6, "million"), (1000, "thousand")):
        if number % scale == 0:
            return f"{int_to_en(number // scale)} {name}th"
    return int_to_en(number) + "th"


def number_to_en(value: str) -> str:
    """数字字符串（可带负号、千分位、小数点）转英文。"""
    negative = value.startswith("-")
    raw = value.lstrip("+-").replace(",", "")
    if not raw:
        raise ValueError("empty number")
    if "." in raw:
        integer, fraction = raw.split(".", 1)
        out = int_to_en(int(integer or "0"))
        if fraction:
            out += " point " + " ".join(_EN_ONES[int(c)] for c in fraction if c.isdigit())
    else:
        out = int_to_en(int(raw))
    return ("minus " if negative else "") + out


# ---------------------------------------------------------------------------
# 语言判断与基础表
# ---------------------------------------------------------------------------


def detect_lang(text: str) -> str:
    """粗略判断朗读语言：出现足够 CJK 字符即按中文处理。"""
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk == 0:
        return "en"
    if cjk >= 2 or cjk * 3 >= latin:
        return "zh"
    return "en"


_MONTH_NAMES = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

_MONTH_ALIASES = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_MONTH_PATTERN = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?"
)

# 单位 -> (中文, 英文单数, 英文复数)。只做“数字紧邻单位”的保守匹配。
_UNITS = {
    "km/h": ("千米每小时", "kilometer per hour", "kilometers per hour"),
    "m/s": ("米每秒", "meter per second", "meters per second"),
    "GB/s": ("吉字节每秒", "gigabyte per second", "gigabytes per second"),
    "Mbps": ("兆比特每秒", "megabit per second", "megabits per second"),
    "km²": ("平方千米", "square kilometer", "square kilometers"),
    "m²": ("平方米", "square meter", "square meters"),
    "m³": ("立方米", "cubic meter", "cubic meters"),
    "kHz": ("千赫兹", "kilohertz", "kilohertz"),
    "MHz": ("兆赫兹", "megahertz", "megahertz"),
    "GB": ("吉字节", "gigabyte", "gigabytes"),
    "MB": ("兆字节", "megabyte", "megabytes"),
    "KB": ("千字节", "kilobyte", "kilobytes"),
    "TB": ("太字节", "terabyte", "terabytes"),
    "kg": ("千克", "kilogram", "kilograms"),
    "mg": ("毫克", "milligram", "milligrams"),
    "cm": ("厘米", "centimeter", "centimeters"),
    "mm": ("毫米", "millimeter", "millimeters"),
    "ml": ("毫升", "milliliter", "milliliters"),
    "mL": ("毫升", "milliliter", "milliliters"),
    "km": ("千米", "kilometer", "kilometers"),
    "kW": ("千瓦", "kilowatt", "kilowatts"),
    "mA": ("毫安", "milliamp", "milliamps"),
    "ms": ("毫秒", "millisecond", "milliseconds"),
    "Hz": ("赫兹", "hertz", "hertz"),
    "min": ("分钟", "minute", "minutes"),
    "℃": ("摄氏度", "degree Celsius", "degrees Celsius"),
    "°C": ("摄氏度", "degree Celsius", "degrees Celsius"),
    "°F": ("华氏度", "degree Fahrenheit", "degrees Fahrenheit"),
    "L": ("升", "liter", "liters"),
    "g": ("克", "gram", "grams"),
    "V": ("伏", "volt", "volts"),
    "W": ("瓦", "watt", "watts"),
    "A": ("安", "amp", "amps"),
}

_UNIT_PATTERN = "|".join(
    re.escape(unit) for unit in sorted(_UNITS, key=len, reverse=True)
)

_MEASURE_WORDS = (
    "个", "只", "天", "年", "月", "日", "岁", "次", "条", "张", "台", "人",
    "位", "名", "件", "种", "米", "秒", "分钟", "小时", "公里", "千克", "克",
    "元", "块", "页", "章", "节", "行", "倍", "层", "楼", "度", "斤", "两", "角",
)

_CURRENCY_ZH = {"¥": "元", "￥": "元", "$": "美元", "€": "欧元", "£": "英镑"}
_CURRENCY_EN = {
    "¥": ("yuan", "yuan"),
    "￥": ("yuan", "yuan"),
    "$": ("dollar", "dollars"),
    "€": ("euro", "euros"),
    "£": ("pound", "pounds"),
}

_EN_ABBREVIATIONS = {
    "mr.": "Mister",
    "mrs.": "Missus",
    "ms.": "Miz",
    "dr.": "Doctor",
    "prof.": "Professor",
    "inc.": "Incorporated",
    "ltd.": "Limited",
    "corp.": "Corporation",
    "co.": "Company",
    "dept.": "Department",
    "govt.": "Government",
    "approx.": "approximately",
    "etc.": "et cetera",
    "e.g.": "for example",
    "i.e.": "that is",
    "vs.": "versus",
    "vs": "versus",
    "no.": "number",
    "u.s.a.": "U S A",
    "u.s.": "U S",
    "u.k.": "U K",
    "a.m.": "A M",
    "p.m.": "P M",
    "et al.": "and others",
}

_ZH_ABBREVIATIONS = {
    "e.g.": "例如",
    "i.e.": "也就是",
    "etc.": "等等",
    "vs.": "对比",
    "vs": "对比",
}


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rule:
    kind: str
    pattern: re.Pattern[str]
    handler: object


def _render_date(year: int, month: int, day: int | None, lang: str) -> str | None:
    if not 1 <= month <= 12:
        return None
    if day is not None and not 1 <= day <= 31:
        return None
    if lang == "zh":
        head = (f"{_digits_to_zh(str(year))}年" if year else "") + f"{int_to_zh(month)}月"
        return head + (f"{int_to_zh(day)}日" if day is not None else "")
    head = _MONTH_NAMES[month]
    if day is not None:
        head += f" {ordinal_en(day)}"
    if year:
        head += f", {int_to_en(year)}"
    return head


def _render_time(hour: int, minute: int, lang: str) -> str:
    if lang == "zh":
        if minute == 0:
            return f"{int_to_zh(hour)}点"
        if minute < 10:
            return f"{int_to_zh(hour)}点零{_ZH_DIGITS[minute]}分"
        return f"{int_to_zh(hour)}点{int_to_zh(minute)}分"
    if minute == 0:
        return f"{int_to_en(hour)} o'clock"
    if minute < 10:
        return f"{int_to_en(hour)} oh {_EN_ONES[minute]}"
    return f"{int_to_en(hour)} {int_to_en(minute)}"


def _unit_value(number: str, unit: str, lang: str) -> str:
    zh_name, singular, plural = _UNITS[unit]
    if lang == "zh":
        return number_to_zh(number) + zh_name
    numeric = float(number.replace(",", ""))
    name = singular if abs(numeric - 1.0) < 1e-9 else plural
    return number_to_en(number) + " " + name


def _date_handler(match: re.Match[str], lang: str) -> str | None:
    groups = match.groups()
    if len(groups) == 3:
        first, second, third = groups
        if first.isdigit() and len(first) == 4:
            return _render_date(int(first), int(second), int(third), lang)
        if first and first[0].isalpha():
            month = _MONTH_ALIASES.get(first.rstrip(".").lower())
            if month is None:
                return None
            return _render_date(int(third), month, int(second), lang)
        month = _MONTH_ALIASES.get(second.rstrip(".").lower())
        if month is None:
            return None
        return _render_date(int(third), month, int(first), lang)
    if len(groups) == 2:
        first, second = groups
        if len(first) == 4:
            return _render_date(int(first), int(second), None, lang)
        return _render_date(0, int(first), int(second), lang)
    return None


def _date_ym_handler(match: re.Match[str], lang: str) -> str | None:
    year, month = match.groups()
    return _render_date(int(year), int(month), None, lang)


def _date_md_handler(match: re.Match[str], lang: str) -> str | None:
    month, day = match.groups()
    return _render_date(0, int(month), int(day), lang)


def _time_handler(match: re.Match[str], lang: str) -> str:
    hour, minute = int(match.group(1)), int(match.group(2))
    return _render_time(hour, minute, lang)


def _time_range_handler(match: re.Match[str], lang: str) -> str:
    h1, m1, h2, m2 = (int(match.group(i)) for i in range(1, 5))
    if lang == "zh":
        return f"{_render_time(h1, m1, 'zh')}到{_render_time(h2, m2, 'zh')}"
    return f"{_render_time(h1, m1, 'en')} to {_render_time(h2, m2, 'en')}"


def _phone_handler(match: re.Match[str], lang: str) -> str:
    digits = match.group(1)
    if lang == "zh":
        return _digits_to_zh(digits)
    return " ".join(_EN_ONES[int(c)] for c in digits)


def _long_digits_handler(match: re.Match[str], lang: str) -> str:
    digits = match.group(0)
    if lang == "zh":
        return _digits_to_zh(digits)
    return " ".join(_EN_ONES[int(c)] for c in digits)


def _currency_handler(match: re.Match[str], lang: str) -> str:
    prefix = match.group(1) or ""
    symbol = match.group(2)
    number = match.group(3)
    if lang == "zh":
        label = {
            "US": "美元",
            "HK": "港元",
            "NT": "新台币",
            "A": "澳元",
        }.get(prefix, _CURRENCY_ZH.get(symbol, ""))
        return number_to_zh(number) + label
    if symbol == "$" and "." in number and len(number.split(".")[1]) == 2:
        whole, cents = number.replace(",", "").split(".")
        text = f"{int_to_en(int(whole))} dollar"
        if int(whole) != 1:
            text += "s"
        if int(cents):
            text += f" and {int_to_en(int(cents))} cent"
            if int(cents) != 1:
                text += "s"
        return text
    singular, plural = _CURRENCY_EN.get(symbol, ("", ""))
    # 千分位逗号必须先去掉，否则 float("1,299.50") 抛 ValueError，
    # 整个 normalize_text 会失败并被上层静默吞掉（表现为"规范化没生效"）。
    name = singular if float(number.replace(",", "")) == 1.0 else plural
    return f"{number_to_en(number)} {name}".strip()


def _percent_handler(match: re.Match[str], lang: str) -> str:
    number = match.group(1)
    if lang == "zh":
        return "百分之" + number_to_zh(number)
    return number_to_en(number) + " percent"


def _permille_handler(match: re.Match[str], lang: str) -> str:
    number = match.group(1)
    if lang == "zh":
        return "千分之" + number_to_zh(number)
    return number_to_en(number) + " per mille"


def _multiplier_zh_handler(match: re.Match[str], lang: str) -> str:
    number, unit = match.group(1), match.group(2)
    scale = 10000 if unit == "万" else 100000000
    value = float(number.replace(",", "")) * scale
    if abs(value - round(value)) < 1e-6:
        expanded = str(round(value))
    else:
        expanded = f"{value:.4f}".rstrip("0").rstrip(".")
    if lang == "zh":
        return int_to_zh(int(expanded)) if expanded.isdigit() else number_to_zh(expanded)
    return number_to_en(expanded)


def _multiplier_en_handler(match: re.Match[str], lang: str) -> str:
    number, scale = match.group(1), match.group(2).lower()
    return f"{number_to_en(number)} {scale}"


def _unit_handler(match: re.Match[str], lang: str) -> str:
    number, unit = match.group(1), match.group(2)
    return _unit_value(number, unit, lang)


def _range_handler(match: re.Match[str], lang: str) -> str:
    left, right, unit = match.group(1), match.group(2), match.group(3)
    if lang == "zh":
        base = f"{number_to_zh(left)}到{number_to_zh(right)}"
        return base + (_UNITS[unit][0] if unit else "")
    base = f"{number_to_en(left)} to {number_to_en(right)}"
    if unit:
        _zh, singular, plural = _UNITS[unit]
        name = singular if abs(float(right.replace(",", "")) - 1.0) < 1e-9 else plural
        base += " " + name
    return base


def _fraction_handler(match: re.Match[str], lang: str) -> str:
    numerator, denominator = int(match.group(1)), int(match.group(2))
    if denominator == 0:
        return match.group(0)
    if lang == "zh":
        return f"{int_to_zh(denominator)}分之{int_to_zh(numerator)}"
    common = {
        (1, 2): "one half",
        (1, 4): "one quarter",
        (3, 4): "three quarters",
        (1, 3): "one third",
        (2, 3): "two thirds",
    }
    return common.get(
        (numerator, denominator),
        f"{int_to_en(numerator)} over {int_to_en(denominator)}",
    )


def _ordinal_zh_handler(match: re.Match[str], lang: str) -> str:
    return "第" + int_to_zh(int(match.group(1)))


def _english_ordinal_handler(match: re.Match[str], lang: str) -> str:
    ordinal = ordinal_en(int(match.group(1)))
    return ("第" + int_to_zh(int(match.group(1)))) if lang == "zh" else ordinal


def _year_zh_handler(match: re.Match[str], lang: str) -> str:
    year = match.group(1)
    if lang == "zh":
        return _digits_to_zh(year) + "年"
    return "the year " + int_to_en(int(year))


def _measure_zh_handler(match: re.Match[str], lang: str) -> str | None:
    if lang != "zh":
        return None
    return number_to_zh(match.group(1)) + match.group(2)


def _number_handler(match: re.Match[str], lang: str) -> str:
    number = match.group(0)
    if lang == "zh":
        return number_to_zh(number)
    return number_to_en(number)


def _abbreviation_handler(match: re.Match[str], lang: str) -> str:
    key = match.group(0).lower()
    if lang == "zh" and key in _ZH_ABBREVIATIONS:
        return _ZH_ABBREVIATIONS[key]
    replacement = _EN_ABBREVIATIONS.get(key, match.group(0))
    # 这些缩写自带句点，出现在句末时把句末标点补回来，避免丢断句信息
    if (
        replacement != match.group(0)
        and match.end() == len(match.string)
        and key in {"p.m.", "a.m."}
        and not replacement.endswith(".")
    ):
        replacement += "."
    return replacement


def _identity(match: re.Match[str], lang: str) -> str:
    return match.group(0)


def _whitespace_handler(match: re.Match[str], lang: str) -> str:
    text = match.group(0)
    if "\n" in text or "\r" in text:
        return "\n"
    return " "


# 带千分位的数字：先尝试千分位形式，再退回普通形式（W2-3a 修复金额截断）
_NUM_SEP = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"


def _build_rules(*, normalize_whitespace: bool) -> list[_Rule]:
    rules = [
        # 0. 保护段：只消费，不改写
        _Rule("protect_url", re.compile(r"https?://[^\s<>\"']+|www\.[^\s<>\"']+"), _identity),
        _Rule("protect_email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), _identity),
        _Rule("protect_code", re.compile(r"`[^`\n]+`"), _identity),
        _Rule("protect_version", re.compile(r"\bv?\d+\.\d+\.\d+(?:[-+][\w.]+)?\b"), _identity),
        # 1. 缩写：带点才匹配，避免把 November 开头的 No 当成 No.
        _Rule(
            "abbreviation",
            re.compile(
                r"\b(?:Mr|Mrs|Ms|Dr|Prof|Inc|Ltd|Corp|Co|Dept|Govt|approx|etc|"
                r"e\.g|i\.e|U\.S\.A|U\.S|U\.K|a\.m|p\.m|et al)\."
                r"|\bvs\.?(?=\s|,|$)"
                r"|\bNo\.(?=\s*\d)",
                re.IGNORECASE,
            ),
            _abbreviation_handler,
        ),
        # 2. 日期
        _Rule(
            "date_iso",
            re.compile(r"(?<!\d)(\d{4})\s*[-/]\s*(\d{1,2})\s*[-/]\s*(\d{1,2})\s*日?(?!\d)"),
            _date_handler,
        ),
        _Rule(
            "date_zh_full",
            re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?(?!\d)"),
            _date_handler,
        ),
        _Rule(
            "date_zh_ym",
            re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月(?!\d)"),
            _date_ym_handler,
        ),
        _Rule(
            "date_zh_md",
            re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日(?!\d)"),
            _date_md_handler,
        ),
        _Rule(
            "date_en_mdy",
            re.compile(
                rf"\b({_MONTH_PATTERN})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b",
                re.IGNORECASE,
            ),
            _date_handler,
        ),
        _Rule(
            "date_en_dmy",
            re.compile(
                rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_PATTERN})\.?,?\s+(\d{{4}})\b",
                re.IGNORECASE,
            ),
            _date_handler,
        ),
        # 3. 时间
        _Rule(
            "time_range",
            re.compile(
                r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)\s*(?:[-~～至到])\s*"
                r"([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)"
            ),
            _time_range_handler,
        ),
        _Rule("time", re.compile(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)"), _time_handler),
        # 4. 号码
        _Rule("phone_cn", re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)"), _phone_handler),
        _Rule("id_cn", re.compile(r"(?<!\d)(\d{15}|\d{17}[\dXx])(?!\d)"), _phone_handler),
        _Rule("long_digits", re.compile(r"(?<!\d)\d{10,}(?!\d)"), _long_digits_handler),
        # 5. 货币（可选消费紧跟的“元/美元”后缀，避免“¥12.5元”读成两个元）
        _Rule(
            "currency",
            re.compile(
                r"(?:(US|HK|NT|A))?([$¥￥€£])\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
                r"(?:\s*(元|美元|欧元|英镑|日元|港元))?"
            ),
            _currency_handler,
        ),
        # 6. 百分比 / 千分比
        _Rule("percent", re.compile(rf"({_NUM_SEP})\s*[%％]"), _percent_handler),
        _Rule("permille", re.compile(rf"({_NUM_SEP})\s*[‰]"), _permille_handler),
        # 7. 万 / 亿 / million 等
        _Rule("multiplier_zh", re.compile(r"(\d+(?:\.\d+)?)\s*(万|亿)"), _multiplier_zh_handler),
        _Rule(
            "multiplier_en",
            re.compile(r"(\d+(?:\.\d+)?)\s*(thousand|million|billion|trillion)\b", re.IGNORECASE),
            _multiplier_en_handler,
        ),
        # 8. 单位
        _Rule(
            "unit",
            re.compile(rf"(?<![A-Za-z0-9_.])(\d+(?:\.\d+)?)\s*({_UNIT_PATTERN})(?![A-Za-z0-9])"),
            _unit_handler,
        ),
        # 9. 范围
        _Rule(
            "range",
            re.compile(
                rf"(?<![\d.])(\d+(?:\.\d+)?)\s*[-~～至到]\s*(\d+(?:\.\d+)?)"
                rf"\s*({_UNIT_PATTERN})?(?![A-Za-z0-9])"
            ),
            _range_handler,
        ),
        # 10. 分数
        _Rule(
            "fraction",
            re.compile(r"(?<![\d.])(\d{1,3})\s*/\s*(\d{1,3})(?![\d.])"),
            _fraction_handler,
        ),
        # 11. 序数
        _Rule("ordinal_zh", re.compile(r"第\s*(\d+)"), _ordinal_zh_handler),
        _Rule(
            "ordinal_en",
            re.compile(r"(?<![A-Za-z0-9_.])(\d+)(?:st|nd|rd|th)\b", re.IGNORECASE),
            _english_ordinal_handler,
        ),
        # 12. 年份与量词
        _Rule("year_zh", re.compile(r"(?<!\d)(\d{4})\s*年"), _year_zh_handler),
        _Rule(
            "measure_zh",
            re.compile(
                rf"(?<![\d.])(\d+(?:\.\d+)?)\s*({'|'.join(sorted(_MEASURE_WORDS, key=len, reverse=True))})"
            ),
            _measure_zh_handler,
        ),
        # 13. 普通数字
        _Rule(
            "number",
            re.compile(
                r"(?<![A-Za-z0-9_.])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![A-Za-z0-9])"
            ),
            _number_handler,
        ),
    ]
    if normalize_whitespace:
        rules.extend(
            [
                _Rule("zero_width", re.compile(r"[\u200b\u200c\u200d\ufeff]"), lambda m, l: ""),
                _Rule("newline", re.compile(r"[ \t\u3000]*\r?\n[ \t\u3000]*"), _whitespace_handler),
                _Rule("ideographic_space", re.compile(r"\u3000"), lambda m, l: " "),
                _Rule("spaces", re.compile(r"[ \t]{2,}"), lambda m, l: " "),
                _Rule("edges", re.compile(r"^[ \t]+|[ \t]+$", re.MULTILINE), lambda m, l: ""),
            ]
        )
    return rules


_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def _fold_fullwidth(text: str) -> str:
    # 只折叠全角数字，保证长度不变、来源区间仍然可用
    return text.translate(_FULLWIDTH_DIGITS)


def normalize_text(
    text: str,
    lang: str | None = None,
    *,
    normalize_whitespace: bool = True,
) -> NormalizedText:
    """规范化一段待朗读文本。

    参数：
        text: 原文。
        lang: ``"zh"`` / ``"en"``；省略时按字符粗略判断。
        normalize_whitespace: 是否合并连续空白、去行首尾空格。

    返回：
        :class:`NormalizedText`，其中 ``text`` 可直接送 TTS，``changes``
        记录每条替换及其原文区间。
    """
    if not isinstance(text, str):
        raise TypeError("text must be str")
    source = text
    folded = _fold_fullwidth(text)
    resolved = lang if lang in ("zh", "en") else detect_lang(folded)

    rules = _build_rules(normalize_whitespace=normalize_whitespace)
    candidates: list[tuple[int, int, int, _Rule, re.Match[str]]] = []
    for priority, rule in enumerate(rules):
        for match in rule.pattern.finditer(folded):
            if match.start() == match.end():
                continue
            candidates.append((match.start(), match.end(), priority, rule, match))
    # 起点优先；同起点时高优先级优先；再同则长匹配优先
    candidates.sort(key=lambda item: (item[0], item[2], -(item[1] - item[0])))

    pieces: list[str] = []
    changes: list[Change] = []
    warnings: list[str] = []
    cursor = 0
    for start, end, _priority, rule, match in candidates:
        if start < cursor:
            continue
        replacement = rule.handler(match, resolved)  # type: ignore[operator]
        if replacement is None:
            continue
        if not isinstance(replacement, str):
            raise TypeError(f"rule {rule.kind} returned {type(replacement)!r}")
        pieces.append(folded[cursor:start])
        pieces.append(replacement)
        if replacement != match.group(0):
            changes.append(
                Change(
                    start=start,
                    end=end,
                    kind=rule.kind,
                    before=match.group(0),
                    after=replacement,
                )
            )
        cursor = end
    pieces.append(folded[cursor:])
    output = "".join(pieces).strip()

    if source.strip() and not output:
        warnings.append("规范化后为空文本")
    if len(source) > 5000:
        warnings.append("输入超过 5000 字符，建议先切分再规范化")

    return NormalizedText(
        source=source,
        text=output,
        lang=resolved,
        changes=changes,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 长句切分
# ---------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"[。！？!?；;…]+[”’」』）)\]]*|\n+")
_SOFT_BREAK = re.compile(r"[，,、：:]\s*|\s+")
_ABBREVIATION_TAILS = (
    "mr", "mrs", "ms", "dr", "prof", "st", "vs", "etc", "e.g", "i.e",
    "u.s", "u.k", "no", "inc", "ltd", "co", "jr", "sr",
)


def _english_period_breaks(chunk: str) -> list[int]:
    """找出可以断句的英文句点位置（返回句点之后的索引）。"""
    breaks: list[int] = []
    for match in re.finditer(r"\.(?=\s+[A-Z\"'(]|$)", chunk):
        prefix = chunk[: match.start()].rstrip()
        token = re.split(r"[\s(]", prefix)[-1].lower()
        if token in _ABBREVIATION_TAILS:
            continue
        if len(token) == 1 and token.isalpha():
            continue
        breaks.append(match.end())
    return breaks


def _split_sentences(text: str) -> list[str]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _SENTENCE_END.finditer(text):
        if match.start() > cursor:
            spans.append((cursor, match.end()))
        elif spans:
            start, _end = spans[-1]
            spans[-1] = (start, match.end())
        else:
            spans.append((0, match.end()))
        cursor = match.end()
    if cursor < len(text):
        spans.append((cursor, len(text)))

    chunks: list[str] = []
    for start, end in spans:
        chunk = text[start:end]
        if not chunk:
            continue
        # 只对含拉丁字母的块做英文句点切分，纯中文块保持原样
        if re.search(r"[A-Za-z]", chunk) and "." in chunk:
            breaks = _english_period_breaks(chunk)
            last = 0
            for point in breaks:
                if point > last:
                    chunks.append(chunk[last:point])
                    last = point
            if last < len(chunk):
                chunks.append(chunk[last:])
        else:
            chunks.append(chunk)
    return [chunk for chunk in chunks if chunk]


def _split_oversize(chunk: str, max_chars: int) -> list[str]:
    if len(chunk) <= max_chars:
        return [chunk]
    best = -1
    for match in _SOFT_BREAK.finditer(chunk):
        if 0 < match.end() <= max_chars:
            best = match.end()
    if best <= 0:
        # 英文单词优先在空格处断开
        window = chunk[:max_chars]
        space = window.rfind(" ")
        best = space + 1 if space >= max_chars // 2 else max_chars
        if best <= 0:
            best = max_chars
    head = chunk[:best]
    tail = chunk[best:]
    return [head] + _split_oversize(tail, max_chars)


def split_long_text(text: str, max_chars: int = 80, min_chars: int = 6) -> list[str]:
    """按标点把长文本切成适合逐段合成的片段。

    - 先按句末标点和换行切，再对超长片段按逗号、顿号、冒号、空格回退切分；
    - 过短尾句会并入前一段，允许结果略超 ``max_chars``（上限约 1.25 倍）；
    - ``"".join(result) == text``，不增删任何字符，调用方可用结果反推原文。
    """
    if max_chars < 8:
        raise ValueError("max_chars must be >= 8")
    if not text:
        return []
    raw: list[str] = []
    for sentence in _split_sentences(text):
        raw.extend(_split_oversize(sentence, max_chars))

    merged: list[str] = []
    for chunk in raw:
        if (
            merged
            and len(chunk.strip()) < min_chars
            and len(merged[-1]) + len(chunk) <= int(max_chars * 1.25)
        ):
            merged[-1] += chunk
        else:
            merged.append(chunk)
    return merged


def split_for_tts(text: str, max_chars: int = 80, lang: str | None = None) -> list[str]:
    """规范化后再切分，返回可直接逐段送 TTS 的片段。"""
    return split_long_text(normalize_text(text, lang=lang).text, max_chars=max_chars)


# ---------------------------------------------------------------------------
# 命令行与自检
# ---------------------------------------------------------------------------

_SELFTEST_CASES: tuple[tuple[str, str, str], ...] = (
    ("zh", "0", "零"),
    ("zh", "7", "七"),
    ("zh", "10", "十"),
    ("zh", "11", "十一"),
    ("zh", "20", "二十"),
    ("zh", "101", "一百零一"),
    ("zh", "1001", "一千零一"),
    ("zh", "10000", "一万"),
    ("zh", "10001", "一万零一"),
    ("zh", "100010000", "一亿零一万"),
    ("zh", "100000001", "一亿零一"),
    ("zh", "123456789", "一亿二千三百四十五万六千七百八十九"),
    ("zh", "3.14", "三点一四"),
    ("zh", "-3.5", "负三点五"),
    ("zh", "2026-11-06", "二零二六年十一月六日"),
    ("zh", "2026/11/6", "二零二六年十一月六日"),
    ("zh", "2026年11月6日", "二零二六年十一月六日"),
    ("zh", "2026年11月", "二零二六年十一月"),
    ("zh", "11月6日", "十一月六日"),
    ("zh", "2026年", "二零二六年"),
    ("zh", "09:30", "九点三十分"),
    ("zh", "9:05", "九点零五分"),
    ("zh", "14:00", "十四点"),
    ("zh", "9:00-10:30", "九点到十点三十分"),
    ("zh", "5kg", "五千克"),
    ("zh", "10km", "十千米"),
    ("zh", "25℃", "二十五摄氏度"),
    ("zh", "50%", "百分之五十"),
    ("zh", "12.5%", "百分之十二点五"),
    ("zh", "3.5万", "三万五千"),
    ("zh", "1.2亿", "一亿二千万"),
    ("zh", "13800138000", "一三八零零一三八零零零"),
    ("zh", "第3", "第三"),
    ("zh", "3个", "三个"),
    ("zh", "¥12.5", "十二点五元"),
    ("zh", "A vs. B", "A 对比 B"),
    ("zh", "3-5", "三到五"),
    ("zh", "3-5kg", "三到五千克"),
    ("zh", "1/2", "二分之一"),
    ("zh", "3/4", "四分之三"),
    ("en", "0", "zero"),
    ("en", "21", "twenty-one"),
    ("en", "105", "one hundred five"),
    ("en", "1001", "one thousand one"),
    ("en", "123456", "one hundred twenty-three thousand four hundred fifty-six"),
    ("en", "3.5", "three point five"),
    ("en", "-2", "minus two"),
    ("en", "50%", "fifty percent"),
    ("en", "Nov 6, 2026", "November sixth, two thousand twenty-six"),
    ("en", "2026-11-06", "November sixth, two thousand twenty-six"),
    ("en", "9:30", "nine thirty"),
    ("en", "9:05", "nine oh five"),
    ("en", "9:00", "nine o'clock"),
    ("en", "5 kg", "five kilograms"),
    ("en", "1 km", "one kilometer"),
    ("en", "Dr. Smith", "Doctor Smith"),
    ("en", "e.g.", "for example"),
    ("en", "etc.", "et cetera"),
    ("en", "U.S.", "U S"),
    ("en", "1st", "first"),
    ("en", "2nd", "second"),
    ("en", "3rd", "third"),
    ("en", "4th", "fourth"),
    ("en", "$5", "five dollars"),
    ("en", "1/2", "one half"),
    ("en", "3/4", "three quarters"),
)

_SELFTEST_SPLIT = (
    "这是一段很长的中文文本，" * 12 + "结束。",
    "Dr. Smith went home. He slept well! Then he woke up.",
    "第一句。第二句！第三句？",
    "A" * 200,
)


def _selftest() -> int:
    failures: list[str] = []
    for lang, source, expected in _SELFTEST_CASES:
        try:
            got = normalize_text(source, lang=lang).text
        except Exception as exc:  # noqa: BLE001 - 自检要把异常汇总成失败项
            failures.append(f"[{lang}] {source!r} 抛异常：{exc!r}")
            continue
        if got != expected:
            failures.append(f"[{lang}] {source!r} -> {got!r}，期望 {expected!r}")

    # 变更记录区间必须合法，且 before 与原文对应
    for lang, source, _expected in _SELFTEST_CASES[:10]:
        result = normalize_text(source, lang=lang)
        for change in result.changes:
            if not (0 <= change.start < change.end <= len(source)):
                failures.append(f"变更区间越界：{change}")
            elif source[change.start : change.end] != change.before:
                failures.append(f"变更 before 与原文不符：{change}")

    # 切分不丢字、不产生空段、超长段有上限
    for text in _SELFTEST_SPLIT:
        parts = split_long_text(text, max_chars=40)
        if "".join(parts) != text:
            failures.append(f"切分后无法还原原文：{text[:20]!r}...")
        if any(not part for part in parts):
            failures.append(f"切分产生空段：{text[:20]!r}...")
        if any(len(part) > 50 for part in parts):
            failures.append(f"切分段超过上限：{max(len(p) for p in parts)}")

    # 幂等：规范化结果再跑一次不应变化
    for lang, source, _expected in _SELFTEST_CASES:
        once = normalize_text(source, lang=lang).text
        twice = normalize_text(once, lang=lang).text
        if once != twice:
            failures.append(f"非幂等：[{lang}] {source!r} -> {once!r} -> {twice!r}")

    if failures:
        print(f"[selftest] 失败 {len(failures)} 项：")
        for failure in failures:
            print("  - " + failure)
        return 1
    print(
        f"[selftest] text_norm: {len(_SELFTEST_CASES)} 条规范化用例、"
        f"{len(_SELFTEST_SPLIT)} 条切分用例全部通过"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="中英文 TTS 文本规范化")
    parser.add_argument("--selftest", action="store_true", help="运行内置自检")
    parser.add_argument("--text", help="待规范化文本")
    parser.add_argument("--lang", choices=("zh", "en"), default=None)
    parser.add_argument("--split", type=int, metavar="N", default=0, help="按 N 字符切分")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.text is None:
        parser.print_help()
        return 2

    result = normalize_text(args.text, lang=args.lang)
    if args.split:
        payload: object = {
            "lang": result.lang,
            "text": result.text,
            "parts": split_long_text(result.text, max_chars=args.split),
            "warnings": result.warnings,
        }
    elif args.json:
        payload = result.as_dict()
    else:
        payload = result.text
    if isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
