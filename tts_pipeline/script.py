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
    "neutral": {
        "label": "通用人声",
        "instruction": "自然的中性人声，吐字清晰，语气平和，不刻意偏向某一年龄或性别",
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

# 角色名 → 原型 / 语速。剧本作者写「奶奶／孙子」，系统应该直接知道要
# 老妇人的慢语速和孩子的亮嗓，而不是让作者去音色库手工配五遍角色。
# 规则按「更具体」排前面：先认「孙子」，再认泛化的「孩子」。
ARCHETYPE_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("narrator", ("旁白", "解说", "画外音", "旁述", "narrator")),
    ("elder_f", ("奶奶", "外婆", "姥姥", "婆婆", "祖母", "阿婆", "老太", "老奶奶", "大娘")),
    ("elder_m", ("爷爷", "外公", "祖父", "大爷", "老爷子", "老头", "阿公", "老伯")),
    ("teen_m", ("孙子", "外孙", "男孩", "弟弟", "儿子", "小伙", "少年")),
    ("teen_f", ("孙女", "外孙女", "女孩", "妹妹", "女儿", "小姑娘", "丫头")),
    ("adult_m", ("爸爸", "叔叔", "哥哥", "先生", "老板", "店长", "大哥")),
    ("adult_f", ("妈妈", "阿姨", "姐姐", "女士", "姑娘", "老板娘")),
    ("teen_m", ("小孩", "孩子", "小朋友", "宝宝", "娃娃", "孩童")),
]

ARCHETYPE_SPEED: dict[str, float] = {
    "narrator": 0.98,
    "elder_m": 0.92,
    "elder_f": 0.92,
    "teen_m": 1.08,
    "teen_f": 1.08,
}

# 只收「明确的动作」，不收「小声／低声／笑／停顿」这类通用描述词：
# 那些是作者写好的自由指令，改写反而会把原意吞掉（也会破坏老剧本）。
GESTURE_PHRASES: dict[str, str] = {
    "叹气": "先轻轻叹一口气再开口，语气里带着无奈与疲惫",
    "叹了口气": "先轻轻叹一口气再开口，语气里带着无奈与疲惫",
    "清嗓子": "开口前轻咳一声清清嗓子，随后自然说话",
    "轻咳": "开口前轻轻咳嗽一声，再自然说话",
    "轻笑": "带着轻轻的笑意说话，语调柔和上扬",
    "微笑": "面带微笑地说，语气温和轻快",
    "哽咽": "声音哽咽，气息不稳，带着哭腔",
    "颤抖": "声音微微发颤，气息不稳",
    "大喊": "提高音量喊出来，情绪外放",
}


def guess_archetype(role_name: str) -> str:
    """按角色名猜原型，取**最长**的命中词；猜不出给「通用人声」。

    「外孙女」同时命中「外孙」和「外孙女」，按词表顺序会判成少年男，
    必须让更长的那个赢。平手时保持词表顺序（更具体的组别排在前面）。
    """
    name = (role_name or "").strip().lower()
    best_key, best_len = "neutral", 0
    for key, words in ARCHETYPE_HINTS:
        for word in words:
            if word in name and len(word) > best_len:
                best_key, best_len = key, len(word)
    return best_key


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

# ---------------------------------------------------------------------------
# 场次氛围
# ---------------------------------------------------------------------------
# 氛围是「一整场戏的底色」，会拼在每行指令的最前面（见 build_instruction）。
# 与情绪的区别：情绪修饰「这一句怎么说」，氛围修饰「这一场在哪里、什么气压」。
# 认不出的值按自由指令原样下发，作者写「雨夜，压低声音」也能生效。
MOODS: dict[str, str] = {
    "": "",
    "安静": "整体氛围安静，语速放缓，音量偏轻，气息收住",
    "紧张": "整体氛围紧张，语速略快，气息压低，句尾收得干脆",
    "欢快": "整体氛围欢快，语调上扬，节奏轻快",
    "悲伤": "整体氛围悲伤，语速放慢，声音低沉，尾音下沉",
    "温暖": "整体氛围温暖，语气柔和，节奏舒展",
    "压抑": "整体氛围压抑，声音闷而低，句与句之间留出空白",
    "肃杀": "整体氛围肃杀，声音冷硬，咬字短促，不带多余情绪",
}

# 场次名 → 氛围：作者没手动选时自动带一层底色。
# 只认强信号词；「场景一」「傍晚到店」这类中性名字一律不推断，
# 免得系统自作主张给每场戏都套上情绪。认不出的值返回空串。
MOOD_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("安静", ("书店", "图书馆", "夜读", "自习", "禅房")),
    ("欢快", ("大婚", "婚礼", "团圆", "喜庆", "新年", "生日", "庆功", "重逢")),
    ("悲伤", ("葬礼", "临终", "诀别", "永别", "遗书", "悼念")),
    # 「深夜」太宽泛（深夜也可能是温暖/安静），只用强信号词
    ("压抑", ("雨夜", "病房", "急诊", "ICU", "审讯", "失业", "暴雨")),
    ("紧张", ("追捕", "逃亡", "警报", "倒计时", "危机", "拆弹", "追杀")),
    ("肃杀", ("审判", "对峙", "决战", "刑场", "军令")),
    ("温暖", ("围炉", "家常", "团圆饭", "午后", "归家", "睡前")),
]


def guess_mood(scene_name: str) -> str:
    """按场次名猜氛围，取最长命中；猜不出返回空串（不加任何指令）。"""
    name = (scene_name or "").strip().lower()
    best, best_len = "", 0
    for key, words in MOOD_HINTS:
        for word in words:
            if word.lower() in name and len(word) > best_len:
                best, best_len = key, len(word)
    return best

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
    """情绪 → 可下发的自然语言片段。

    先查情绪表，再查动作/语气词表（「叹气」这类标签要扩写成完整指令），
    都不认识时原样返回——作者写「雨夜，压低声音」也该生效。
    """
    if not emotion or emotion == NEUTRAL:
        return ""
    if emotion in EMOTIONS:
        return EMOTIONS[emotion]
    return GESTURE_PHRASES.get(emotion, emotion)


def mood_phrase(mood: str) -> str:
    """场次氛围 → 可下发的自然语言片段；认不出的原样返回。

    与 emotion_phrase 一样，先查表再回退：作者写已知标签时得到打磨过的
    指令文案，写自由描述时原样生效，两种写法都不会被吞掉。
    """
    key = (mood or "").strip()
    if not key:
        return ""
    return MOODS.get(key, key)



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
            mood_phrase(scene_moods.get(scene, "")) if scene else "",
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
