"""剧本解析与情绪推断（纯函数，不依赖 torch / 网络）。

剧本台的排版约定只有三条：

    ## 场景名                 ← 场次（可选）
    角色名：台词              ← 用角色默认情绪
    角色名[情绪]：台词        ← 显式情绪（也认全角括号「角色名（情绪）：」）
    旁白：描述                ← 旁白是一个普通角色

情绪按 行内标签 > 角色默认 > 自动推断 > 中性 的优先级决定，
每个结果都带上来源，界面上要能告诉作者「这个情绪是谁定的」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 音色原型
# ---------------------------------------------------------------------------
# breeze 是自然语言驱动的：原型不是「音色 ID」，而是一句能生成该音色的描述。
# 选原型 = 预填这句描述，作者可以改。
ARCHETYPES: dict[str, dict[str, str]] = {
    "narrator": {
        "label": "旁白",
        "instruction": "沉稳的纪录片旁白，字正腔圆，语速均匀，语气平和",
        "default_emotion": "中性",
    },
    "elder_m": {
        "label": "老年男",
        "instruction": "一位六十多岁的男性长者，声音低沉沙哑，语速缓慢，气息沉稳",
        "default_emotion": "疲惫",
    },
    "elder_f": {
        "label": "老年女",
        "instruction": "一位六十多岁的女性长者，声音温厚略哑，语速缓慢，语气慈祥",
        "default_emotion": "温柔",
    },
    "teen_m": {
        "label": "少年男",
        "instruction": "一个十二三岁的男孩，声音清亮，语调活泼，略带稚气",
        "default_emotion": "中性",
    },
    "teen_f": {
        "label": "少年女",
        "instruction": "一个十岁左右的小女孩，声音清脆稚嫩，语调轻快",
        "default_emotion": "开心",
    },
    "adult_m": {
        "label": "成年男",
        "instruction": "一位三十岁左右的男性，声音沉稳有力，吐字清晰",
        "default_emotion": "中性",
    },
    "adult_f": {
        "label": "成年女",
        "instruction": "一位三十岁左右的女性，声音明亮柔和，吐字清晰",
        "default_emotion": "温柔",
    },
    "custom": {
        "label": "自定义",
        "instruction": "",
        "default_emotion": "中性",
    },
}

# ---------------------------------------------------------------------------
# 情绪词表
# ---------------------------------------------------------------------------
# label -> 追加到指令里的说法。中性不追加任何词，避免把「平静」这种
# 无效指令塞给模型。
EMOTIONS: dict[str, str] = {
    "中性": "",
    "温柔": "语气温柔亲切，音量偏轻，节奏舒缓",
    "开心": "语气愉快上扬，语调轻快有笑意",
    "激动": "语气激动，音量提高，语速加快",
    "悲伤": "语气低沉悲伤，语速放慢，气息下沉",
    "愤怒": "语气愤怒，咬字加重，音量提高",
    "紧张": "语气紧张，呼吸略急促，语速不稳",
    "迟疑": "语气犹豫，句中多停顿，尾音下沉",
    "恐惧": "声音发颤，气声偏多，语速急促",
    "讽刺": "语调夸张上扬，尾音拖长",
    "疲惫": "声音虚弱，气息不足，语速缓慢",
}

# 自动推断用的关键词（权重高于标点：写了「我哭了」比感叹号更可信）
#
# 必须是**词组**而不是拼接字符串：按字符匹配会让「岂有此理」里的「有」
# 命中「桌上有一本书」，把陈述句判成愤怒。
_KEYWORD_EMOTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("悲伤", ("哭", "泪", "呜咽", "悲痛", "难受", "去世", "离开", "舍不得")),
    ("愤怒", ("愤怒", "滚出去", "闭嘴", "混蛋", "岂有此理", "放屁", "气死")),
    ("恐惧", ("害怕", "恐怖", "吓死", "别过来", "救命", "阴森", "毛骨悚然")),
    ("开心", ("哈哈", "嘿嘿", "太好了", "真好", "高兴", "开心", "棒极了")),
    ("紧张", ("怎么办", "糟了", "来不及", "快跑", "完蛋")),
]

_PUNCT_EMOTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("激动", ("！！", "!!")),
    ("迟疑", ("……", "...")),
    ("紧张", ("？？", "??")),
]

NEUTRAL = "中性"

# ---------------------------------------------------------------------------
# 行内标注：角色名[情绪]：台词 / 角色名（情绪）：台词 / 角色名：台词
# ---------------------------------------------------------------------------
_NAME = r"[^：:\[\]（）()\s][^：:\[\]（）()]{0,19}?"
_LINE_RE = re.compile(
    rf"^(?P<name>{_NAME})\s*"
    rf"(?:[\[［](?P<tag>[^\]］]{{1,12}})[\]］]|[（(](?P<tag2>[^）)]{{1,12}})[）)])?\s*"
    rf"[：:]\s*(?P<text>.+)$"
)
_SCENE_RE = re.compile(r"^#{1,3}\s*(?P<name>.+?)\s*$")


@dataclass
class ScriptLine:
    """一行台词。idx 从 0 开始，与音频文件序号一一对应。"""

    idx: int
    scene: str
    role_name: str
    text: str
    emotion: str = NEUTRAL
    emotion_source: str = "neutral"      # explicit | role | auto | neutral
    instruction: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "idx": self.idx,
            "scene": self.scene,
            "role_name": self.role_name,
            "text": self.text,
            "emotion": self.emotion,
            "emotion_source": self.emotion_source,
            "instruction": self.instruction,
            "error": self.error,
        }


@dataclass
class ParsedScript:
    lines: list[ScriptLine] = field(default_factory=list)
    scenes: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {
            "lines": [l.as_dict() for l in self.lines],
            "scenes": self.scenes,
            "errors": self.errors,
            "ok": self.ok,
        }


# ---------------------------------------------------------------------------
# 情绪
# ---------------------------------------------------------------------------
def infer_emotion(text: str) -> str | None:
    """从文本推断情绪。只在高置信时才给结论，宁可中性也不要瞎猜。

    关键词优先于标点：一句「我哭了！」里的「哭」比感叹号更能说明情绪。
    """
    if not text:
        return None
    for label, words in _KEYWORD_EMOTIONS:
        if any(w in text for w in words):
            return label
    for label, marks in _PUNCT_EMOTIONS:
        if any(m in text for m in marks):
            return label
    if "？" in text or "?" in text:
        return "迟疑"
    return None


def resolve_emotion(
    text: str,
    explicit_tag: str | None,
    role_default: str | None,
) -> tuple[str, str]:
    """返回 (情绪, 来源)。来源取值 explicit / role / auto / neutral。"""
    if explicit_tag:
        tag = explicit_tag.strip()
        if tag in EMOTIONS:
            return tag, "explicit"
        # 认不出的标签当自由指令用：作者写「压低声音」也该生效，
        # 但不能冒充成已知情绪，所以返回原始词并标记来源。
        return tag, "explicit"
    if role_default and role_default in EMOTIONS:
        return role_default, "role"
    auto = infer_emotion(text)
    if auto:
        return auto, "auto"
    return NEUTRAL, "neutral"


def emotion_phrase(emotion: str) -> str:
    """情绪 → 可下发的自然语言片段；认不出的原样返回（作者的自由指令）。"""
    return EMOTIONS.get(emotion, emotion if emotion and emotion != NEUTRAL else "")


def build_instruction(base: str, emotion: str, scene_mood: str = "") -> str:
    """拼一行要下发的指令：场次氛围 → 音色/原型 → 情绪。

    顺序固定，便于界面按来源高亮，也让模型先读人设、再读情境。
    """
    parts: list[str] = []
    for piece in (scene_mood.strip(), (base or "").strip(), emotion_phrase(emotion)):
        if piece and piece not in parts:
            parts.append(piece)
    return "，".join(parts)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_script(
    raw: str,
    role_defaults: dict[str, str] | None = None,
    role_instructions: dict[str, str] | None = None,
    scene_moods: dict[str, str] | None = None,
) -> ParsedScript:
    """把剧本文本解析成行列表。

    role_defaults: 角色名 → 默认情绪
    role_instructions: 角色名 → 音色/原型指令（用于拼最终指令）
    scene_moods: 场景名 → 氛围指令
    """
    role_defaults = role_defaults or {}
    role_instructions = role_instructions or {}
    scene_moods = scene_moods or {}

    out = ParsedScript()
    scene = ""
    scene_used = False

    # 报错要给「编辑器里的第几行」，不是「第几个解析行」——场次行与坏行
    # 也占行号，按 len(lines)+1 算会让用户找不到地方。
    for lineno, raw_line in enumerate((raw or "").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue

        m_scene = _SCENE_RE.match(line)
        if m_scene:
            scene = m_scene.group("name").strip()
            if scene and scene not in out.scenes:
                out.scenes.append(scene)
            scene_used = False
            continue

        m = _LINE_RE.match(line)
        if not m:
            out.errors.append({
                "line": lineno,
                "text": raw_line,
                "reason": "认不出这一行：请写成「角色名：台词」，或用 ## 开头表示场次",
            })
            continue

        name = m.group("name").strip()
        tag = m.group("tag") or m.group("tag2")
        text = m.group("text").strip()

        if not text:
            out.errors.append({"line": lineno, "text": raw_line,
                               "reason": "这一行没有台词内容"})
            continue

        if role_defaults is not None and name not in role_defaults:
            out.errors.append({
                "line": lineno,
                "text": raw_line,
                "reason": f"剧本里出现了未知角色「{name}」",
                "role_name": name,
            })
            # 仍然产出一行，界面上可以就地新建角色，而不是让整段消失
        emotion, source = resolve_emotion(
            text, tag, role_defaults.get(name) if role_defaults else None)
        instruction = build_instruction(
            role_instructions.get(name, ""),
            emotion,
            scene_moods.get(scene, "") if scene else "",
        )

        out.lines.append(ScriptLine(
            idx=len(out.lines),
            scene=scene,
            role_name=name,
            text=text,
            emotion=emotion,
            emotion_source=source,
            instruction=instruction,
            error="" if name in role_defaults else "未知角色",
        ))
        if scene:
            scene_used = True

    return out


def estimate_seconds(parsed: ParsedScript, chars_per_sec: float = 4.6,
                     gap: float = 0.35) -> float:
    """粗略估计整剧时长：字数 / 语速 + 行间停顿。用于给作者预期。"""
    if not parsed.lines:
        return 0.0
    chars = sum(len(l.text) for l in parsed.lines)
    return chars / max(0.1, chars_per_sec) + gap * max(0, len(parsed.lines) - 1)
