"""剧本解析与情绪推断（纯函数，不依赖 torch / 网络）。

剧本台的排版约定只有三条：

    ## 场景名                 ← 场次（可选）
    角色名：台词              ← 用角色默认情绪
    角色名[情绪]：台词        ← 显式情绪（也认全角括号「角色名（情绪）：」）
    【角色名】台词            ← 结构化脚本写法（讯飞 / 魔音常见），：可省略
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
#
# 词表按语义分组往后追加。消歧靠三条规则（见 guess_archetype）：
#   1. 整名精确匹配（ARCHETYPE_EXACT）——用于同长且语义相反的两性组合名；
#   2. 更长的词优先——「小和尚」压过「和尚」、「老阿姨」压过「阿姨」；
#   3. 同长时取**先命中**的（词表已按具体度排序，越具体的组越靠前）。
ARCHETYPE_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("narrator", ("旁白", "解说", "画外音", "旁述", "narrator")),
    ("elder_f", ("奶奶", "外婆", "姥姥", "婆婆", "祖母", "阿婆", "老太", "老奶奶", "大娘")),
    ("elder_m", ("爷爷", "外公", "祖父", "大爷", "老爷子", "老头", "阿公", "老伯")),
    ("teen_m", ("孙子", "外孙", "男孩", "弟弟", "儿子", "小伙", "少年")),
    ("teen_f", ("孙女", "外孙女", "女孩", "妹妹", "女儿", "小姑娘", "丫头")),
    ("adult_m", ("爸爸", "叔叔", "哥哥", "先生", "老板", "店长", "大哥")),
    ("adult_f", ("妈妈", "阿姨", "姐姐", "女士", "姑娘", "老板娘")),
    ("teen_m", ("小孩", "孩子", "小朋友", "宝宝", "娃娃", "孩童")),
    # 长辈的其余叫法：与「大娘」平行，不与「大妈」混
    ("elder_f", ("老太太", "老婆婆", "老妇人", "太婆", "大姨")),
    ("elder_m", ("老爷爷", "老公公", "老先生", "老太爷", "姥爷", "公公")),
    # 少年：只有带「男孩/女生/童」等性别或年龄标记的词才给童声，
    # 泛称「学生」没有性别线索，留给兜底更稳
    ("teen_m", ("小男孩", "男生", "男童", "男学生", "小伙子", "书生", "少爷", "小少爷")),
    ("teen_f", ("小女孩", "女生", "女童", "女学生", "小丫头", "丫鬟", "小丫鬟")),
    # 儿童/少年身份但和尚尼姑成年：先认身份词，再认「小和尚」这种具体形态
    ("teen_m", ("小和尚", "小道士", "小沙弥")),
    ("teen_f", ("小尼姑",)),
    ("adult_m", ("和尚", "道士")),
    ("adult_f", ("尼姑",)),
    # 成年亲属：叔伯舅姑这一支
    ("adult_m", ("大叔", "大伯", "叔父", "伯父", "姑父", "舅舅", "姐夫", "妹夫", "女婿")),
    ("adult_f", ("大妈", "大婶", "大嫂", "嫂子", "姑姑", "姨妈", "小姨", "舅妈", "婶婶", "伯母")),
    ("adult_m", ("岳父", "丈人", "公公", "老公", "丈夫", "干爹", "继父", "老哥", "小哥", "师傅")),
    ("adult_f", ("丈母娘", "岳母", "老婆", "妻子", "干妈", "继母", "太太", "夫人", "小姐", "新娘")),
    # 带性别前缀的职业角色：长词优先于职业词，把「女老师」从「老师」里分出来
    ("adult_f", ("女老师", "女医生", "女警察", "女司机", "女经理", "女护士", "大小姐", "老板娘")),
    ("adult_m", ("男老师", "男医生", "男警察", "男司机", "男经理", "男护士", "大少爷", "大师傅")),
    # 职业角色：性别只取中文语境里的常见默认值（客服/老师多为女声，
    # 警察/医生/司机多为男声），听感上比所有人一个音色自然
    ("adult_f", ("客服", "老师", "护士", "空姐", "前台", "收银员", "服务员", "话务员", "售货员")),
    ("adult_m", ("警察", "交警", "医生", "大夫", "司机", "快递员", "邮递员", "外卖员",
                 "经理", "总监", "主管", "教练", "保安", "律师", "会计", "程序员", "行长")),
]

# 同一个角色名里塞了两性时，任何单一性别音色都会把另一半演错
# （「爸爸妈妈」里既有爸爸也有妈妈，两个词都是两个字，靠词长分不出来）。
# 这类名字直接整名钉死，避免词表顺序一变、性别就跟着变。
ARCHETYPE_EXACT: dict[str, str] = {
    "爸爸妈妈": "neutral",
    "爷爷奶奶": "neutral",
    "哥哥姐姐": "neutral",
    "公公婆婆": "neutral",
    "外公外婆": "neutral",
    "大叔大婶": "neutral",
    "老公老婆": "neutral",
    "叔叔婶婶": "neutral",
    "大哥大嫂": "neutral",
    "兄弟姐妹": "neutral",
    "爹娘": "neutral",
    "爹": "adult_m",
    "娘": "adult_f",
}

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


def guess_archetype(role_name: str, gender_hint: str | None = None) -> str:
    """按角色名猜原型：整名钉死 → 最长命中词 → 性别线索修正 → 通用人声。

    「林队」「苏娜」「老陈」这类名字没带任何关键词，词表猜出来是 neutral，
    所有人配同一个音色。如果人设里写了性别（gender_hint），就用它纠正：
    neutral + male → adult_m，neutral + female → adult_f。
    性别线索**只补中性**，不覆盖词表已经命中的性别（比如词表说"奶奶"是 elder_f，
    人设里写了（男）大概率是作者写错，不该硬改）。
    """
    name = (role_name or "").strip().lower()
    if name in ARCHETYPE_EXACT:
        return ARCHETYPE_EXACT[name]
    best_key, best_len = "neutral", 0
    for key, words in ARCHETYPE_HINTS:
        for word in words:
            if word in name and len(word) > best_len:
                best_key, best_len = key, len(word)
    # 没命中特征词（=neutral）且有人设性别：按性别猜成年男/女（最常见）
    if best_key == "neutral" and gender_hint in ("male", "female"):
        return "adult_m" if gender_hint == "male" else "adult_f"
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
# 时间标记：AI 生成剧本里的场次常用 "【0:00 - 0:15】 标题" 这种形式，
# 必须在「【角色名】」正则之前识别它，否则会被误当成一个叫 "0:00 - 0:15" 的角色。
_TIME_SCENE_RE = re.compile(r"^【\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}】\s*(?P<name>.+?)\s*$")
# 结构化脚本写法：【角色名】台词 / 【角色名】[情绪]：台词 / 【角色名】（情绪）台词。
# 冒号可省略——同行（讯飞智作、魔音工坊）都让用户直接写「【角色】台词」，
# 没有冒号也要能认出来，否则粘贴别人的剧本第一行就报错。
_BRACKET_ROLE_RE = re.compile(
    r"^【(?P<name>[^】]{1,20})】\s*"
    r"(?:[\[［](?P<tag>[^\]］]{1,12})[\]］]|[（(](?P<tag2>[^）)]{1,12})[）)])?\s*"
    r"[：:]?\s*(?P<text>.+)$"
)

# 粘剧本时 AI 常会把「人设 / 环境音 / 情绪描述 / 台词」拆成不同格式：
#   林队（男）：35-40岁...核心情绪：压抑
#   林队：（极度疲惫，语速缓慢）           ← 情绪单独成行
#   旁白："苏娜……这已经是你坐在这里的第六个小时了……"  ← 引号包台词
#   旁白：(环境音提示：沉闷的雷声)          ← 括号提示不是台词
# normalize 把这些拆干净，parse 循环继续吃它熟悉的 shape。
_HINT_BODY = r"(?:提示|说明|描述|备注|声音|音|动作|环境)"
_HINT_LINE_RE = re.compile(
    r"^【?" + _NAME + r"】?\s*[：:]\s*"
    r"[（(](?:" + _HINT_BODY + r")[提示说明描述备注声音音动作环境]*[：:]?[^）)]*[）)]\s*$"
)
_ROLE_SKIP_HINT_RE = re.compile(
    r"^【?" + _NAME + r"】?\s*[：:]\s*"
    r"(?:[（(]" + _HINT_BODY + r"[提示说明描述备注声音音动作环境]*[：:]?[^）)]*[）)]"
    r"|[（(][^）)]+[）)])\s*$"
)
_QUOTED_RE = re.compile(r'^["「『](.+?)["」』]$')


def _looks_like_hint_text(text: str) -> bool:
    """判断一行「角色：xxx」里的 xxx 是不是括号里的提示/动作/环境音——
    这些不该被念出来，要被丢掉，别进合成指令。
    """
    t = text.strip()
    if not t:
        return True
    # 开头就是括号提示：(环境音提示：...) / （动作提示：...） / (提示：...)
    if re.match(r"^[（(](?:提示|说明|描述|备注|声音|音|动作|环境|环境音)[提示说明描述备注声音音动作环境]*[：:]?", t):
        return True
    if t.startswith("（") or t.startswith("("):
        # 剩下的也可能是纯情绪描述「（极度疲惫，语速缓慢，带一点叹息）」，
        # 这种要单独处理——作为情绪标签，不是跳过。
        return False
    return False


def _strip_outer_quotes(text: str) -> str:
    """剥掉首尾成对的引号（中英文双引 / 单引）。
    还会顺带剥掉引号后紧接的句号「"xx".」——AI 生成剧本常留这个尾巴。
    """
    t = text.strip()
    m = re.match(r'^["「『](.+?)["」』][.\u3002]?$', t)
    if m:
        return m.group(1).strip()
    return t


def _split_leading_emotion(text: str) -> tuple[str | None, str]:
    """把「角色：（情绪描述）后面跟了实际台词」这一行拆开。
    返回 (情绪标签 or None, 剩余文本)。
    """
    t = text.strip()
    if not t:
        return None, ""
    m = re.match(r"^[（(]([^）)]{1,40})[）)]\s*(.*)$", t)
    if not m:
        return None, t
    return m.group(1).strip(), m.group(2).strip()


def _merge_skipped_emotions(lines):
    """跨两行合并：如果上一行是「角色：（情绪）」但没有实际台词，
    下一行是同一角色的台词，就把情绪贴进下一行；如果下一行换了角色，
    也把情绪带到那个新角色头上（因为情绪描述写在台词之前）。
    情绪只往下看一行，超过一行就丢掉——多了连人都搞不清哪段情绪配哪句。
    """
    out = []
    pending_emotion = None     # str | None
    pending_name = None        # 写情绪的角色；None 表示「谁写的不重要」
    for item in lines:
        if item[0] != "line":
            out.append(item)
            pending_emotion = None
            pending_name = None
            continue
        kind, lineno, name, tag, text = item
        if pending_emotion and not tag and text:
            # 上一行的情绪贴进这一行：新 tag 先写上一行的，
            # 如果这行自己也有 tag 就用「上一行情绪 + 这行 tag」合并
            new_tag = tag or pending_emotion
            if tag and tag != pending_emotion:
                new_tag = f"{pending_emotion}，{tag}"
            out.append((kind, lineno, name, new_tag, text))
        else:
            out.append(item)
        pending_emotion = None
        pending_name = None
    return out


def normalize_script(raw: str) -> list[tuple]:
    """把用户粘进来的剧本预处理成干净的 (kind, lineno, ...) 列表。
    目的：让 parse_script 的主循环只吃 shape 稳定的数据，
    不在里面塞一堆「AI 常这么写所以要兼容」的 if 分支。

    返回行的 shape：
      ("scene", lineno, name)
      ("line",  lineno, role_name, emotion_tag_or_None, text)
      ("skip",  lineno, reason)
    """
    out = []
    raw_lines = (raw or "").splitlines()

    for lineno, raw_line in enumerate(raw_lines, 1):
        line = raw_line.strip()
        if not line:
            continue

        # --- 场次：# 开头 or AI 时间标记 【HH:MM - HH:MM】标题 ---
        m_t = _TIME_SCENE_RE.match(line)
        if m_t:
            out.append(("scene", lineno, m_t.group("name").strip()))
            continue
        m_s = _SCENE_RE.match(line)
        if m_s:
            out.append(("scene", lineno, m_s.group("name").strip()))
            continue

        # --- 人设段：「角色（性别）：年龄、音色...核心情绪：X、Y、Z」---
        # 特征是冒号后是音色描述里有「岁 / 低沉 / 沙哑 / 音色 / 核心情绪」
        # 冒号后字数通常 > 40 且含顿号，显然不是一句台词。
        hm = re.match(rf"^(?P<name>{_NAME})\s*[（(][男女][）)]\s*[：:]\s*(?P<body>.+)$", line)
        if hm:
            body = hm.group("body")
            keywords = ("岁", "音色", "沙哑", "清亮", "共鸣", "气声", "拖音",
                        "咬字", "语速", "腔", "腔腔")
            if any(k in body for k in keywords) or len(body) > 40 and ("、" in body or "，" in body):
                # 人设行：提取「核心情绪：」后面的内容作为默认情绪候选
                cm = re.search(r"核心情绪[：:]\s*([^，,。.\n]+)", body)
                if cm:
                    emotions = [x.strip() for x in re.split(r"[、,，]", cm.group(1)) if x.strip()]
                    if emotions:
                        out.append(("role_default", lineno, hm.group("name"), emotions[0]))
                # 同时提取性别，自动配音色时用来给"林队"这种没特征的名字补性别
                gm = re.search(r"[（(]([男女])[）)]", line.split("：")[0] if "：" in line else line.split(":")[0])
                if gm:
                    out.append(("role_gender", lineno, hm.group("name"),
                                "male" if gm.group(1) == "男" else "female"))
                out.append(("skip", lineno, "人设行"))
                continue

        # --- 括号提示 / 环境音 / 动作描述：整行是提示，跳过 ---
        m_br = _BRACKET_ROLE_RE.match(line) if line.startswith("【") else None
        m_ln = m_br or _LINE_RE.match(line)
        if not m_ln:
            # 真认不出来的行（没冒号、没【角色】）：自动归到旁白，
            # 和前端 normalizePaste 的行为一致——让用户粘进来直接能用，
            # 而不是满屏报错。
            out.append(("line", lineno, "旁白", None, line))
            out.append(("soft_warning", lineno, f"行 {lineno} 没写角色，自动归到旁白"))
            continue

        name = m_ln.group("name").strip()
        raw_tag = m_ln.group("tag") or m_ln.group("tag2")
        raw_text = m_ln.group("text").strip()

        # 角色名后面挂了性别：剥掉别让它进台词，也别被当成情绪
        sex_rm = re.search(r"[（(][男女][）)]", name)
        if sex_rm:
            name = re.sub(r"[（(][男女][）)]", "", name).strip()

        if not raw_text:
            out.append(("skip", lineno, "无台词"))
            continue

        # 提示行：「角色：(环境音提示：...)」 / 「角色：（动作提示）」
        if _looks_like_hint_text(raw_text):
            out.append(("skip", lineno, "提示行"))
            continue

        # 情绪描述行（可能跨到下一行台词）：先记下来，让 _merge_skipped_emotions 贴回去
        # 先剥掉可能的情绪前缀「（极度疲惫，语速缓慢，带一点叹息）」
        emo, rest = _split_leading_emotion(raw_text)
        # 如果整行剥完只剩情绪（rest 为空或很短，且没有可念的句子），就标 pending
        if emo and (not rest or len(rest) <= 8) and '"' not in rest and "「" not in rest:
            out.append(("emotion_only", lineno, name, emo))
            continue

        # 有情绪前缀 + 有实际台词：把情绪剥出来当 tag，正文进 text
        tag = raw_tag
        if emo and not tag:
            tag = emo
        elif emo and tag and emo != tag:
            tag = f"{emo}，{tag}"
        # 台词剥首尾引号：AI 生成剧本常写「旁白："台词……"」
        text = _strip_outer_quotes(rest or raw_text)

        out.append(("line", lineno, name, tag, text))

    # 情绪-only 行往下粘一行；整理后交给主循环。
    # AI 生成剧本的典型格式：
    #   林队：（极度疲惫，语速缓慢，带一点叹息）     ← emotion_only，角色=林队
    #   旁白："苏娜……这已经是你坐在这里的第六个小时了……"  ← 台词实际想让林队说
    # 这种情况下不能让旁白白念，要把下一行的旁白台词归到 pending 的角色头上。
    merged = []
    pending = None     # (role_name, emotion) or None
    for item in out:
        if item[0] == "emotion_only":
            _, ln, nm, emo = item
            pending = (nm, emo)
            continue
        if pending and item[0] == "line":
            _, ln2, nm2, tag2, text2 = item
            p_name, p_emo = pending
            if nm2 == "旁白" and text2:
                # 下一行是旁白台词：把旁白的台词归给 pending 的角色 + 情绪
                # （旁白的文本可能带引号，上面 normalize 已经剥过了）
                new_tag = p_emo
                if tag2 and tag2 != p_emo:
                    new_tag = f"{p_emo}，{tag2}"
                merged.append(("line", ln2, p_name, new_tag, text2))
            elif not text2 or nm2 == p_name:
                # 下一行是同一个角色（或无台词）：正常合并
                new_tag = tag2 or p_emo
                if tag2 and tag2 != p_emo:
                    new_tag = f"{p_emo}，{tag2}"
                merged.append(("line", ln2, nm2, new_tag, text2))
            else:
                # 下一行换了角色且不是旁白：pending 情绪丢弃（跨角色不继承）
                merged.append(item)
            pending = None
            continue
        if pending:
            # 下一行不是 line（是 scene / skip）：pending 丢弃，情绪不跨场次
            pending = None
        merged.append(item)
    return merged


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
    # 从人设行提取的角色性别（"male"/"female"），给 auto_cast 用。
    # 名字不带性别特征时（林队 / 苏娜 / 老陈）靠词表猜不出，
    # 人设行写了就别浪费。
    role_genders: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(not (e.get("soft") or False) for e in self.errors)

    def as_dict(self) -> dict:
        return {
            "lines": [l.as_dict() for l in self.lines],
            "scenes": self.scenes,
            "errors": self.errors,
            "ok": self.ok,
            "role_genders": self.role_genders,
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
    carry_emotion: bool = False,
) -> ParsedScript:
    """把剧本文本解析成行列表。

    role_defaults: 角色名 → 默认情绪
    role_instructions: 角色名 → 音色/原型指令（用于拼最终指令）
    scene_moods: 场景名 → 氛围指令
    carry_emotion: 情绪跨行延续。只在「这一行没有任何情绪线索」
        （没标注、角色没默认、文本也推断不出）时，才沿用本场上一行的情绪，
        来源标成 carry，界面上单独显示。标注/角色默认/文本推断一律优先，
        所以打开它不会悄悄改掉作者写过的东西。换场次即重置。
    """
    role_defaults = role_defaults or {}
    role_instructions = role_instructions or {}
    scene_moods = scene_moods or {}

    # 先 normalize：粘进来的剧本格式五花八门（人设 / 时间场次 / 括号提示 /
    # 情绪+台词拆成两行 / 引号包台词），全在这一步拆干净；主循环只吃
    # (kind, lineno, ...) shape 稳定的数据。
    rows = normalize_script(raw)

    # normalize 从人设里提取的「核心情绪」作为角色默认情绪的候选——
    # 只在角色还没默认情绪时补进去，不覆盖作者手动配过的。
    # 同时收集角色性别（人设里写了男/女的），名字没性别特征时给 auto_cast 用。
    pending_genders: dict[str, str] = {}
    for item in rows:
        if item[0] == "role_default":
            _, _, nm, emo = item
            if nm and emo and nm not in role_defaults:
                role_defaults[nm] = emo
        elif item[0] == "role_gender":
            _, _, nm, g = item
            if nm and g and nm not in pending_genders:
                pending_genders[nm] = g

    out = ParsedScript()
    out.role_genders = pending_genders
    scene = ""
    scene_used = False
    last_emotion = ""      # 本场上一行的情绪，供 carry_emotion 沿用

    for item in rows:
        kind = item[0]
        lineno = item[1]

        if kind == "scene":
            scene = item[2]
            if scene and scene not in out.scenes:
                out.scenes.append(scene)
            scene_used = False
            last_emotion = ""      # 换场即重置：别把上一场的情绪带进雨夜
            continue

        if kind != "line":
            # role_default（人设提取，上面已并入 role_defaults）/ skip（提示 / 空台词 / 人设整行）
            # / soft_warning（自动归旁白的软提示——不进 errors，ok 仍为 True）
            if kind == "soft_warning":
                out.errors.append({
                    "line": lineno,
                    "reason": item[2],
                    "role_name": "旁白",
                    "soft": True,
                })
            continue

        _, _, name, tag, text = item

        if not text:
            out.errors.append({"line": lineno, "text": text,
                               "reason": "这一行没有台词内容"})
            continue

        if role_defaults is not None and name not in role_defaults:
            out.errors.append({
                "line": lineno,
                "text": text,
                "reason": f"剧本里出现了未知角色「{name}」",
                "role_name": name,
            })
            # 仍然产出一行，界面上可以就地新建角色，而不是让整段消失
        emotion, source = resolve_emotion(
            text, tag, role_defaults.get(name) if role_defaults else None)
        if carry_emotion and source == "neutral" and last_emotion:
            # 这一行什么都没说：沿用本场上一行的情绪（来源单独标 carry）
            emotion, source = last_emotion, "carry"
        if source != "neutral":
            last_emotion = emotion
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
