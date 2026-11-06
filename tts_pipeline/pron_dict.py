"""tts_pipeline · 发音词典（W2-3 / PRD P0-03）

让用户自己在界面上纠正多音字与专有名词——这是"多音字只能改代码"的直接补丁。

设计要点：
  * 两种读法输入：
      - 汉字替换：``行长`` → ``航长``（最可靠，推荐）
      - 带声调拼音：``hang2 zhang3`` → 自动转成汉字 ``航长``
  * 两级作用域：``global``（全局）与音色 id（音色级）。音色级覆盖全局。
  * 优先级最高：词典在文本规范化**之后**应用，因此口音预设不会把用户的纠正改回去。
  * 查不到拼音音节时**不猜**：保留原词并在 hits 里标记 ``pinyin-unresolved``，
    界面据此提示用户改用汉字写法。
  * 纯标准库，无网络、无 GPU 依赖。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

__all__ = ["DictEntry", "PronunciationDict", "pinyin_to_hanzi"]


# ---------------------------------------------------------------------------
# 拼音 → 汉字
# ---------------------------------------------------------------------------
# 只覆盖高频音节，给出该音节最常见的代表字。查不到就如实返回未命中。
_PINYIN_BASE: dict[str, str] = {
    "a": "啊", "ai": "爱", "an": "安", "ang": "昂", "ao": "熬",
    "ba": "八", "bai": "白", "ban": "班", "bang": "帮", "bao": "包",
    "bei": "北", "ben": "本", "beng": "崩", "bi": "比", "bian": "边",
    "biao": "标", "bie": "别", "bin": "宾", "bing": "冰", "bo": "波",
    "bu": "不", "ca": "擦", "cai": "才", "can": "参", "cang": "仓",
    "cao": "操", "ce": "册", "ceng": "层", "cha": "叉", "chai": "柴",
    "chan": "产", "chang": "长", "chao": "超", "che": "车", "chen": "沉",
    "cheng": "成", "chi": "吃", "chong": "冲", "chou": "丑", "chu": "出",
    "chua": "刷", "chuan": "川", "chuang": "窗", "chui": "吹", "chun": "春",
    "chuo": "戳", "ci": "词", "cong": "从", "cu": "粗", "cun": "村",
    "cuo": "错", "da": "搭", "dai": "呆", "dan": "单", "dang": "当",
    "dao": "刀", "de": "得", "deng": "灯", "di": "低", "dian": "点",
    "diao": "雕", "die": "爹", "ding": "丁", "dong": "东", "dou": "斗",
    "du": "读", "duan": "段", "dui": "对", "dun": "顿", "duo": "多",
    "e": "鹅", "en": "恩", "er": "儿", "fa": "发", "fan": "翻",
    "fang": "方", "fei": "飞", "fen": "分", "feng": "风", "fo": "佛",
    "fou": "否", "fu": "服", "gai": "该", "gan": "干", "gang": "刚",
    "gao": "高", "ge": "哥", "gei": "给", "gen": "根", "geng": "更",
    "gong": "工", "gou": "沟", "gu": "姑", "gua": "瓜", "guai": "怪",
    "guan": "关", "guang": "光", "gui": "归", "gun": "滚", "guo": "国",
    "hai": "海", "han": "汉", "hang": "行", "hao": "好", "he": "喝",
    "hei": "黑", "hen": "很", "heng": "横", "hong": "红", "hou": "后",
    "hu": "湖", "hua": "花", "huai": "坏", "huan": "还", "huang": "黄",
    "hui": "会", "hun": "婚", "huo": "火", "ji": "机", "jia": "家",
    "jian": "见", "jiang": "江", "jiao": "交", "jie": "街", "jin": "金",
    "jing": "京", "jiong": "窘", "jiu": "九", "ju": "句", "juan": "卷",
    "jue": "决", "jun": "军", "ka": "卡", "kai": "开", "kan": "看",
    "kang": "康", "kao": "考", "ke": "科", "ken": "肯", "keng": "坑",
    "kong": "空", "kou": "口", "ku": "哭", "kua": "夸", "kuai": "快",
    "kuan": "宽", "kuang": "框", "kui": "亏", "kun": "困", "kuo": "阔",
    "la": "拉", "lai": "来", "lan": "蓝", "lang": "浪", "lao": "老",
    "le": "乐", "lei": "类", "leng": "冷", "li": "里", "lian": "连",
    "liang": "量", "liao": "聊", "lie": "列", "lin": "林", "ling": "令",
    "liu": "六", "long": "龙", "lou": "楼", "lu": "路", "luan": "乱",
    "lun": "论", "luo": "落", "ma": "妈", "mai": "买", "man": "满",
    "mang": "忙", "mao": "猫", "me": "么", "mei": "美", "men": "门",
    "meng": "梦", "mi": "米", "mian": "面", "miao": "苗", "mie": "灭",
    "min": "民", "ming": "明", "mo": "摸", "mou": "某", "mu": "木",
    "na": "拿", "nai": "奶", "nan": "男", "nao": "脑", "ne": "呢",
    "nei": "内", "nen": "嫩", "neng": "能", "ni": "你", "nian": "年",
    "niang": "娘", "niao": "鸟", "nie": "聂", "nin": "您", "ning": "宁",
    "niu": "牛", "nong": "农", "nu": "努", "nv": "女", "nuan": "暖",
    "nun": "嗯", "nuo": "诺", "o": "哦", "ou": "欧", "pa": "趴",
    "pai": "拍", "pan": "盘", "pang": "旁", "pao": "抛", "pei": "配",
    "pen": "喷", "peng": "朋", "pi": "批", "pian": "片", "piao": "飘",
    "pie": "撇", "pin": "拼", "ping": "平", "po": "坡", "pou": "剖",
    "pu": "扑", "qi": "七", "qia": "恰", "qian": "千", "qiang": "枪",
    "qiao": "桥", "qie": "切", "qin": "亲", "qing": "青", "qiong": "穷",
    "qiu": "秋", "qu": "去", "quan": "全", "que": "却", "qun": "群",
    "ran": "然", "rang": "让", "rao": "饶", "re": "热", "ren": "人",
    "reng": "仍", "ri": "日", "rong": "荣", "rou": "肉", "ru": "如",
    "ruan": "软", "rui": "锐", "run": "润", "ruo": "若", "sa": "撒",
    "sai": "赛", "san": "三", "sang": "桑", "sao": "扫", "se": "色",
    "sen": "森", "seng": "僧", "sha": "沙", "shai": "晒", "shan": "山",
    "shang": "上", "shao": "少", "she": "蛇", "shei": "谁", "shen": "深",
    "sheng": "生", "shi": "是", "shou": "手", "shu": "书", "shua": "刷",
    "shuai": "帅", "shuan": "拴", "shuang": "双", "shui": "水", "shun": "顺",
    "shuo": "说", "si": "四", "song": "松", "sou": "搜", "su": "苏",
    "suan": "算", "sui": "岁", "sun": "孙", "suo": "所", "ta": "他",
    "tai": "太", "tan": "探", "tang": "汤", "tao": "桃", "te": "特",
    "teng": "疼", "ti": "梯", "tian": "天", "tiao": "条", "tie": "铁",
    "ting": "听", "tong": "同", "tou": "头", "tu": "图", "tuan": "团",
    "tui": "推", "tuo": "托", "wa": "挖", "wai": "外", "wan": "完",
    "wang": "王", "wei": "为", "wen": "文", "weng": "翁", "wo": "我",
    "wu": "五", "xi": "西", "xia": "下", "xian": "先", "xiang": "想",
    "xiao": "小", "xie": "写", "xin": "心", "xing": "行", "xiong": "胸",
    "xiu": "休", "xu": "需", "xuan": "选", "xue": "学", "xun": "寻",
    "ya": "呀", "yan": "言", "yang": "羊", "yao": "要", "ye": "叶",
    "yi": "一", "yin": "因", "ying": "英", "yo": "哟", "yong": "用",
    "you": "有", "yu": "鱼", "yuan": "元", "yue": "月", "yun": "云",
    "za": "杂", "zai": "在", "zan": "咱", "zang": "脏", "zao": "造",
    "ze": "则", "zei": "贼", "zen": "怎", "zeng": "增", "zha": "渣",
    "zhai": "摘", "zhan": "站", "zhang": "张", "zhao": "找", "zhe": "这",
    "zhen": "真", "zheng": "正", "zhi": "知", "zhong": "中", "zhou": "周",
    "zhu": "住", "zhua": "抓", "zhuan": "转", "zhuang": "装", "zhui": "追",
    "zhun": "准", "zhuo": "桌", "zi": "字", "zong": "宗", "zou": "走",
    "zu": "组", "zuan": "钻", "zui": "最", "zun": "尊", "zuo": "做",
}

# 带声调时优先取这些字（多音字的关键）
_PINYIN_TONE_OVERRIDES: dict[str, str] = {
    "hang1": "航", "hang2": "行", "hang4": "巷",
    "zhang1": "张", "zhang3": "长", "zhang4": "丈",
    "xing2": "行", "xing4": "姓",
    "chang2": "长", "chang3": "厂", "chang4": "唱",
    "shuo1": "说", "shuo4": "硕",
    "shi4": "是", "shi2": "十", "shi3": "使",
    "du2": "读", "du4": "度",
    "dao3": "倒", "dao4": "到",
    "le4": "乐", "le5": "了",
    "zhong1": "中", "zhong4": "重",
    "tai2": "台", "tai4": "太",
    "shuai1": "帅", "shuai3": "甩",
    "hao3": "好", "hao4": "号",
    "ren2": "人", "ren4": "任",
    "shan1": "山", "shan3": "闪",
    "huo2": "活", "huo4": "或",
    "di4": "地", "di2": "敌",
    "di3": "底", "di1": "低",
    "shu4": "数", "shu3": "属", "shu1": "书",
    "ju4": "句", "ju1": "居",
    "qu1": "区", "qu4": "去",
    "shao4": "少", "shao1": "烧",
    "zao3": "早", "zao4": "造",
    "jian4": "见", "jian1": "间", "jian3": "剪",
    "jin1": "金", "jin4": "尽",
    "qian2": "钱", "qian1": "千",
    "mai3": "买", "mai4": "卖",
    "tai4": "太", "tai1": "台",
    "he2": "河", "he4": "贺",
    "bing3": "饼", "bing4": "病", "bing1": "冰",
    "ban3": "板", "ban1": "班",
    "fan4": "饭", "fan1": "翻",
    "xi1": "西", "xi4": "细",
    "xu1": "需", "xu3": "许",
    "yu2": "鱼", "yu4": "玉", "yu3": "雨",
    "ye2": "爷", "ye4": "业",
    "xia4": "下", "xia2": "霞",
    "miao2": "苗", "miao4": "庙",
    "niao3": "鸟", "niao1": "尿",
    "lv4": "绿", "lv3": "旅", "lv2": "驴",
    "nve3": "女",
}


def pinyin_to_hanzi(token: str) -> tuple[str, bool]:
    """``"hang2"`` → ``("航", True)``；查不到返回 ``(原样, False)``。"""
    m = re.fullmatch(r"([a-zA-Zü]+)([1-5])?", token.strip())
    if not m:
        return token, False
    base, tone = m.group(1).lower(), m.group(2)
    if tone and base + tone in _PINYIN_TONE_OVERRIDES:
        return _PINYIN_TONE_OVERRIDES[base + tone], True
    if base in _PINYIN_BASE:
        return _PINYIN_BASE[base], True
    return token, False


_PINYIN_SEQ = re.compile(r"^[a-zA-Zü]+[1-5]?(\s+[a-zA-Zü]+[1-5]?)*$")


def _looks_like_pinyin(reading: str) -> bool:
    """判断读法是否写成拼音音节序列（而非汉字）。"""
    text = reading.strip()
    if not text or not re.search(r"[a-zA-Z]", text):
        return False
    return bool(_PINYIN_SEQ.fullmatch(text))


# ---------------------------------------------------------------------------
# 词典条目
# ---------------------------------------------------------------------------
@dataclass
class DictEntry:
    """一条发音词典记录。"""

    word: str
    reading: str
    scope: str = "global"
    kind: str = "replace"          # "replace" | "pinyin"

    def to_dict(self) -> dict[str, Any]:
        return {"word": self.word, "reading": self.reading,
                "scope": self.scope, "kind": self.kind}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "DictEntry":
        word = str(d.get("word", "")).strip()
        reading = str(d.get("reading", "")).strip()
        kind = str(d.get("kind", "")).strip()
        if kind not in ("replace", "pinyin"):
            # 输入像"带声调的拼音音节序列"就按拼音处理：
            # hang2 zhang3 / hang2zhang3 / hang2 都算；普通汉字不算
            kind = "pinyin" if _looks_like_pinyin(reading) else "replace"
        return DictEntry(word=word, reading=reading,
                         scope=str(d.get("scope", "global")), kind=kind)


class PronunciationDict:
    """发音词典容器：全局 + 音色级。"""

    def __init__(self, entries: Iterable[DictEntry | dict[str, Any]] | None = None):
        self._entries: list[DictEntry] = []
        for e in entries or []:
            if isinstance(e, dict):
                e = DictEntry.from_dict(e)
            if e.word:
                self._entries.append(e)

    # ---- 增删改查 ----
    def add(self, entry: DictEntry | dict[str, Any]) -> DictEntry:
        e = entry if isinstance(entry, DictEntry) else DictEntry.from_dict(entry)
        if not e.word:
            raise ValueError("词条不能为空")
        if not e.reading:
            raise ValueError("读法不能为空")
        self._entries = [x for x in self._entries
                         if not (x.word == e.word and x.scope == e.scope)]
        self._entries.append(e)
        return e

    def remove(self, word: str, scope: str = "global") -> bool:
        before = len(self._entries)
        self._entries = [x for x in self._entries
                         if not (x.word == word and x.scope == scope)]
        return len(self._entries) != before

    def clear(self, scope: str | None = None) -> None:
        if scope is None:
            self._entries = []
        else:
            self._entries = [x for x in self._entries if x.scope != scope]

    @property
    def entries(self) -> list[DictEntry]:
        return list(self._entries)

    def lookup(self, word: str, scope: str = "global") -> DictEntry | None:
        for e in self._entries:          # 音色级优先
            if e.word == word and e.scope == scope:
                return e
        for e in self._entries:
            if e.word == word and e.scope == "global":
                return e
        return None

    def for_scope(self, scope: str) -> list[DictEntry]:
        return [e for e in self._entries if e.scope in ("global", scope)]

    # ---- 应用 ----
    def apply(self, text: str, scope: str = "global") -> tuple[str, list[dict[str, str]]]:
        """把词典命中项替换进文本。

        返回 ``(新文本, 命中列表)``；命中项含 ``original/replacement/kind``。
        拼音无法转换时保留原词并标记 ``pinyin-unresolved``。

        实现要点：单次扫描 + 最长优先，避免短词污染长词的替换结果
        （"行长"→"航长" 之后不应再被 "长"→"常" 二次改写）。
        """
        hits: list[dict[str, str]] = []
        if not text:
            return text, hits
        words = sorted({e.word for e in self.for_scope(scope) if e.word},
                       key=len, reverse=True)
        if not words:
            return text, hits
        pattern = re.compile("|".join(re.escape(w) for w in words))

        def _replace(m: re.Match[str]) -> str:
            matched = m.group(0)
            entry = self.lookup(matched, scope)
            if entry is None:
                return matched
            if entry.kind == "pinyin":
                parts = entry.reading.split()
                converted: list[str] = []
                ok = True
                for piece in parts:
                    hanzi, matched_pinyin = pinyin_to_hanzi(piece)
                    if not matched_pinyin:
                        ok = False
                        break
                    converted.append(hanzi)
                if not ok:
                    hits.append({"original": entry.word, "replacement": entry.word,
                                 "kind": "pinyin-unresolved"})
                    return matched
                replacement = "".join(converted)
            else:
                replacement = entry.reading
            if entry.kind == "replace" and replacement == entry.word:
                return matched
            hits.append({"original": entry.word, "replacement": replacement,
                         "kind": entry.kind})
            return replacement

        return pattern.sub(_replace, text), hits

    # ---- 持久化 ----
    def to_list(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self._entries]

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_list(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def load(path: str | Path) -> "PronunciationDict":
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return PronunciationDict()
        return PronunciationDict(data if isinstance(data, list) else [])
