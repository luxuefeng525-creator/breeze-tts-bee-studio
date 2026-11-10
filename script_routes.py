"""剧本台后端：路由 + 逐行合成任务。

从 panel.py 拆出来是因为那边已经 2400 行；这里只做剧本相关的事，
通过 `import panel as P` 复用存储、上游调用和音频工具。

设计要点：
- 剧本以 `raw` 文本为准，行列表是解析结果，不当作真相来源
- 逐行合成是一个可停止、可续做的后台任务；已完成的行落盘后不再重做
- 所有写 JSON 都走 P._update_json，避免并发时后写覆盖先写
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import panel as P
import soundtrack as ST
from tts_pipeline.script import (
    ARCHETYPES,
    ARCHETYPE_SPEED,
    EMOTIONS,
    MOODS,
    estimate_seconds,
    guess_archetype,
    guess_mood,
    parse_script,
)

MAX_LINES = 100          # 一批合成多少行（8 GB 卡约 20-30 分钟一批，超了自动续做）
HARD_MAX_LINES = 300     # 剧本总行数上限：再大就先分剧，别把一张卡跑满
MAX_TAKES = 8            # 单行最多留几版；超出后覆盖最旧一版（不删文件、不无限涨盘）
DEFAULT_GAP = 0.35


def _log(tag: str, msg: str) -> None:
    """写一行带时间戳的日志到 stderr。

    stderr 会被 `ops/run-panel.bat` 重定向进 `ops/logs/panel-api.log`，
    这样「刚才那次为什么失败」事后能 grep 到。写 stdout 会被 HTTP 响应
    污染（面板的响应体是 JSON），不能用。
    """
    try:
        sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                         f"[{tag}] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass

DEFAULT_CFG = 4.0
DEFAULT_SCENE_GAIN = 0.35   # 场次铺底默认音量（线性幅度，0–1）
DUCK_DB = -12.0             # 人声段铺底压低的分贝数
EXPORT_PARTS = ("full", "lines", "timeline", "script", "subtitles", "mix")
# 默认仍是老四件套：新增的字幕要作者勾了才进包，免得旧导出包内容悄悄变
DEFAULT_PARTS = ",".join(("full", "lines", "timeline", "script"))

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
# 8GB 卡上同时跑两个合成任务会直接把显存打满（还会互相抢引擎，
# 两边都变慢）。所以全局只有一个执行槽：后来的任务排队，位置可见、可取消。
_JOB_QUEUE: list[str] = []
# cast 要连着写 voices.json / roles.json / scripts.json 三个文件：单次 RMW
# 各自原子，但几次之间需要一把进程内总锁，否则两个并发请求会基于同一份
# 旧快照各建一遍角色，并在响应里谎报「建成功了」。
_CAST_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
def _all_scripts() -> list[dict]:
    return P._load("scripts.json")


def _find(script_id: str) -> dict | None:
    return next((s for s in _all_scripts() if s["id"] == script_id), None)


def _save_script(script: dict) -> dict:
    """整条替换（按 id）。剧本是整体编辑，不做字段级合并。"""
    def _mut(items):
        for i, s in enumerate(items):
            if s["id"] == script["id"]:
                items[i] = script
                return
        items.append(script)

    P._update_json("scripts.json", _mut)
    return script


def _lines_dir(script_id: str) -> Path:
    d = P.AUDIOS / "scripts" / script_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 角色 / 音色
# ---------------------------------------------------------------------------
def _roles_all() -> list[dict]:
    return P._load("roles.json")


def _roles_by_name() -> dict[str, dict]:
    """全局角色（不属于任何剧本）按名字索引。

    只给「不需要剧本上下文」的调用方用；解析链路一律走 _roles_for()，
    否则两个剧的同名「旁白」会互相顶掉。
    """
    return {str(r.get("name") or "").strip(): r for r in _roles_all()
            if not r.get("script_id")}


def _roles_for(script: dict | None) -> dict[str, dict]:
    """这个剧本眼里的「名字 → 角色」：本剧 cast 优先，其次全局。

    roles.json 允许同名共存（靠 script_id 区分），所以不能只按 name 建一次
    索引就完事——两个剧的「旁白」谁是谁，只有 cast 说了算。

    仍然保留「回退全局」：作者在角色库里手工建的角色是全局的，剧本要能用上。
    「新剧本别静默用上别人的同名角色」这件事由 auto_cast 负责——它用
    _roles_cast_only 判定缺谁（见 _apply_cast），不在这里屏蔽全局。
    """
    by_id = {str(r.get("id")): r for r in _roles_all()}
    out = _roles_by_name()
    for rid in (script or {}).get("cast") or []:
        r = by_id.get(str(rid))
        if r:
            out[str(r.get("name") or "").strip()] = r
    return out


def _roles_cast_only(script: dict | None) -> dict[str, dict]:
    """只取本剧 cast 里的角色，**不**回退全局。

    auto_cast 用它判断「这个剧本还缺谁」：隔离剧本里全局有同名「旁白」
    也不算已配，否则新剧本会静默用上别人剧本的音色。
    """
    by_id = {str(r.get("id")): r for r in _roles_all()}
    out: dict[str, dict] = {}
    for rid in (script or {}).get("cast") or []:
        r = by_id.get(str(rid))
        if r:
            out[str(r.get("name") or "").strip()] = r
    return out


def _sync_cast(script: dict, roles: dict | None = None, parsed=None) -> dict:
    """把台词命中的角色 id 补进 cast（幂等，只补不删）。

    解析允许回退全局角色，但那个「回退」不能是临时的：下次保存时
    auto_cast 会把它换成新建的专属副本，role_id 一变，已合成的行全部
    变 pending。回退命中就固化进 cast。

    全新的隔离剧本（cast 还是空的）不认领：那时还没有任何绑定，要让
    auto_cast 按隔离语义给它建专属角色。
    """
    if not script.get("id"):
        return script
    if script.get("cast_isolated") and not script.get("cast"):
        return script
    roles = _roles_for(script) if roles is None else roles
    try:
        parsed = parsed if parsed is not None else parse_for(script, roles)
    except Exception as exc:
        # 解析失败就静默不认领 cast 的话，用户只会看到「未知角色」，
        # 却不知道自己写错了什么。至少把原因记下来。
        _log("sync_cast", f"script={script.get('id')} 解析失败：{exc}")
        return script

    ids: list[str] = []
    for line in parsed.lines:
        r = roles.get(line.role_name)
        if r and str(r.get("id")) not in ids:
            ids.append(str(r["id"]))
    if not ids:
        return script
    have = {str(x) for x in (script.get("cast") or [])}
    if set(ids) <= have:
        return script          # 已经都认领过了，不再写盘

    def _mut(items):
        for s in items:
            if s.get("id") != script["id"]:
                continue
            # 双检：并发首读/并发保存时只写一次，且不覆盖别人刚加的角色
            cast = [str(x) for x in (s.get("cast") or [])]
            for rid in ids:
                if rid not in cast:
                    cast.append(rid)
            s["cast"] = cast

    P._update_json("scripts.json", _mut)
    merged = list(dict.fromkeys(list(have) + ids))
    return {**script, "cast": merged}


def purge_script_roles(sid: str) -> list[str]:
    """删剧本时连带删掉它的专属角色，返回被删的角色名（给日志/响应看）。

    专属角色只属于这部剧本，剧本没了就永远用不上；留在角色库里既是孤儿，
    又会顶着已经不存在的剧本名，用户没法判断该不该删。音色不动——它可能
    被别的角色复用。
    """
    names: list[str] = []

    def _mut(items):
        keep = []
        for r in items:
            if str(r.get("script_id") or "") == str(sid):
                names.append(str(r.get("name") or ""))
                continue
            keep.append(r)
        items[:] = keep

    P._update_json("roles.json", _mut)
    return names


def drop_role_from_casts(rid: str) -> int:
    """删角色后清理所有剧本的 cast 引用，返回清理条数。

    不清理会留一个悬空 id：解析时找不到角色、界面也不报错，用户只会
    觉得「这一行突然没声音了」。
    """
    hit = 0

    def _mut(items):
        nonlocal hit
        for s in items:
            cast = s.get("cast")
            if isinstance(cast, list) and str(rid) in [str(x) for x in cast]:
                s["cast"] = [x for x in cast if str(x) != str(rid)]
                hit += 1

    P._update_json("scripts.json", _mut)
    return hit


def _voices_by_id() -> dict[str, dict]:
    return {v["id"]: v for v in P._load("voices.json")}


def _role_instruction(role: dict | None, voices: dict[str, dict]) -> str:
    """角色要下发的音色描述：音色自带的指令优先，其次原型预填的描述。

    角色自己的「口音 / 方言」拼在最末：它属于表演方式，不该盖掉音色本身。
    已经包含同一句口音时不重复追加，否则每行指令都会越来越长。
    """
    if not role:
        return ""
    voice = voices.get(role.get("voice_id") or "")
    if voice and (voice.get("instruction") or "").strip():
        base = voice["instruction"].strip()
    else:
        arch = ARCHETYPES.get(role.get("archetype") or "custom") or {}
        base = (arch.get("instruction") or "").strip()
    accent = (role.get("accent") or "").strip()
    if accent and accent not in base:
        base = f"{base}，{accent}" if base else accent
    return base


def _role_speed(role: dict | None) -> float:
    """角色语速倍率，越界值夹到 0.5–2.0（与单条合成的 speed 口径一致）。"""
    try:
        value = float((role or {}).get("speed") or 1.0)
    except (TypeError, ValueError):
        return 1.0
    return min(2.0, max(0.5, value))


def _role_default_emotion(role: dict | None) -> str:
    """角色的默认情绪；未设置返回空串（交给自动推断）。

    原型里的 default_emotion 只在「建角色时预填」用一次，不在解析时兜底：
    否则每个角色都恒有一个默认值，标点与关键词推断永远轮不到。
    """
    if not role:
        return ""
    value = (role.get("default_emotion") or "").strip()
    return value if value in EMOTIONS else ""


def parse_for(script: dict, roles: dict[str, dict] | None = None):
    """按当前角色/音色重新解析剧本（角色改名或换音色后要重新算）。

    roles 显式传入时就用它，不再回退全局：角色隔离模式下「本剧还缺谁」
    必须只看 cast，传进来的就是 cast 限定集。
    """
    roles = _roles_for(script) if roles is None else roles
    voices = _voices_by_id()
    defaults = {name: _role_default_emotion(r) for name, r in roles.items()}
    instr = {name: _role_instruction(r, voices) for name, r in roles.items()}
    raw = script.get("raw", "")
    # 先取场次名（解析是纯 CPU，两次不过毫秒级），把自动氛围算进
    # 第二遍的指令里；自动值不写回 scenes，作者可随时清空。
    probe = parse_script(raw)
    moods = {s["name"]: s.get("mood", "")
             for s in _scenes_view(probe.scenes, script.get("scenes"))}
    return parse_script(raw, defaults, instr, moods,
                        bool(script.get("emotion_carry")))


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _voice_route(voice: dict | None) -> str:
    """这一行走克隆还是设计路线。

    Qwen 引擎的 clone 路线完全不接收自然语言 instruction，而 design
    路线不能带参考音频；两条路线权重互斥、按需换入。把 route 显式算
    出来，才能既「可见地提示用户」又「按路线分组减少换模型次数」。
    """
    if not voice:
        return "design"
    if str(voice.get("type") or "").strip() == "clone":
        return "clone"
    return "clone" if str(voice.get("ref_audio") or "").strip() else "design"


def _seed_for(script: dict, role: dict | None) -> int:
    """同一剧本里给每个角色派生一个稳定 seed。

    全剧固定 seed=42 时，同原型的多个角色会抽到几乎一样的声音；
    按 role_id 派生偏移后仍可复现（同一角色重录结果一致）。
    """
    base = int(script.get("seed") if script.get("seed") is not None else 42)
    rid = str((role or {}).get("id") or "")
    if not rid:
        return base
    return base + int(hashlib.sha1(rid.encode("utf-8")).hexdigest()[:8], 16) % 9973


def plan_cast(errors: list[dict], role_names: set[str],
              voices: dict[str, dict]) -> list[dict]:
    """从解析错误里列出「还缺谁」，并给出每个角色该用什么声音。

    名字跟已有音色有公共前缀时复用那个音色（作者写「小蜜蜂」，库里叫
    「小蜜蜂配音」，显然是想用同一个声音）；判据是 ≥3 字且长度差 ≤3，
    比早期的 ≥2 字更严，避免角色一多「小 X」「大 X」互相误命中。
    其余按名字猜原型新建一个设计型音色，避免五个角色听成一个人。

    **克隆音色一律不复用**（PRD-v4 §4.1 定的修复）：clone 路线不收任何
    自然语言指令，一旦自动把角色绑上去，这个角色的情绪、场次氛围、原型
    口音就全被上游静默丢掉——界面看着「智能配好了」，实际一句都没生效。
    作者真要用某个克隆音色，去角色卡里手动选，那是他自己的明确意图。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for err in errors or []:
        name = str(err.get("role_name") or "").strip()
        if not name or name in role_names or name in seen:
            continue
        seen.add(name)
        arch = guess_archetype(name)
        hit = None
        best = 0
        skipped_clone = ""
        for v in voices.values():
            vname = str(v.get("name") or "").strip()
            n = _common_prefix_len(name, vname)
            if n < 3 or abs(len(name) - len(vname)) > 3 or n <= best:
                continue
            if _voice_route(v) == "clone":
                # 见 docstring：自动配角不碰克隆音色。记下来，界面上告诉
                # 作者「为什么没复用」，免得他又手动改回去
                skipped_clone = skipped_clone or vname
                continue
            hit, best = v, n
        out.append({
            "name": name,
            "archetype": arch,
            "label": (ARCHETYPES.get(arch) or {}).get("label", "自定义"),
            "instruction": (ARCHETYPES.get(arch) or {}).get("instruction", ""),
            "speed": ARCHETYPE_SPEED.get(arch, 1.0),
            "voice": hit,
            "route": _voice_route(hit),
            "skipped_clone": skipped_clone,
        })
    return out


def _norm_scenes(raw) -> list[dict]:
    """场次配置统一成 [{name, mood}]。

    早期数据/第三方调用可能只传名字字符串，直接当 dict 用会在解析时炸；
    这里一次性收口，重名只保留第一条（否则氛围下拉会重复出现）。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw or []:
        if isinstance(item, str):
            name, mood = item.strip(), ""
            ambience = bgm = ""
            gain = DEFAULT_SCENE_GAIN
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            # auto=true 是系统推断的底色，不是作者选的；界面把 scenes
            # 整组回传时不能让它悄悄变成手动配置（否则清空就清不掉了）
            mood = "" if item.get("auto") else str(item.get("mood") or "").strip()
            ambience = str(item.get("ambience") or "").strip()
            bgm = str(item.get("bgm") or "").strip()
            gain = _clamp_gain(item.get("gain"))
        else:
            continue
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "mood": mood, "ambience": ambience,
                    "bgm": bgm, "gain": gain})
    return out


def _clamp_gain(value) -> float:
    """场次铺底音量：脏数据（字符串/None/超范围）也要收敛到 0–1。"""
    try:
        g = float(value)
    except (TypeError, ValueError):
        return DEFAULT_SCENE_GAIN
    return min(1.0, max(0.0, g))


def _scenes_view(names: list[str], configured) -> list[dict]:
    """以解析出的场次为准：作者选过的用作者的值，没选的按场次名自动推断。

    自动值**不落盘**：作者清空下拉就真的没有氛围，不会被下一次保存又填回来；
    界面用 auto 标记告诉用户「这层底色是系统加的，可以覆盖」。
    """
    known = {s["name"]: s for s in _norm_scenes(configured)}
    out = []
    for n in names:
        cfg = known.get(n) or {}
        chosen = cfg.get("mood", "")
        auto = not chosen
        out.append({"name": n, "mood": chosen or guess_mood(n), "auto": auto,
                    "ambience": cfg.get("ambience", ""),
                    "bgm": cfg.get("bgm", ""),
                    "gain": cfg.get("gain", DEFAULT_SCENE_GAIN)})
    return out


def _line_hash(row: dict) -> str:
    """一行音频的身份：文本、角色、音色、指令、语速任何一项变了就不该复用旧音频。"""
    raw = "|".join(str(row.get(k) or "") for k in
                   ("text", "role_id", "voice_id", "instruction", "speed"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _take_name(idx: int, n: int) -> str:
    """第 1 版沿用 line_XXX.wav（兼容老数据与老导出包），之后带 _t{n}。"""
    return f"line_{idx:03d}.wav" if n == 1 else f"line_{idx:03d}_t{n}.wav"


def _norm_takes(rec: dict) -> list[dict]:
    """把一条行记录里的版本列表补成规范形态。

    老数据只有 wav/duration、没有 takes：补成 v1，否则升级后第一次点
    「换版本」会发现「只有一版」，明明播得出来却选不了。
    """
    takes = [t for t in (rec.get("takes") or []) if isinstance(t, dict) and t.get("n")]
    if not takes and rec.get("wav"):
        takes = [{"n": 1, "wav": rec["wav"],
                  "duration": round(float(rec.get("duration") or 0), 3)}]
    return sorted(takes, key=lambda t: t["n"])


def _persist_take(script_id: str, idx: int, row: dict, take_no: int,
                  duration: float, rel: str, reset: bool = False) -> None:
    """落一版音频：reset=True 表示新内容的第 1 版（丢掉旧内容的版本）。

    文本/情绪/语速变了之后旧版本属于「上一版台词」，留着只会让用户对着
    旧音频找不回新台词，所以整剧合成第一次落盘时清空版本列表。
    """
    def _mut(items):
        for s in items:
            if s["id"] != script_id:
                continue
            lines = s.setdefault("lines", [])
            rec = next((l for l in lines if l["idx"] == idx), None)
            if rec is None:
                rec = {"idx": idx}
                lines.append(rec)
            takes = [] if reset else _norm_takes(rec)
            takes = [t for t in takes if t["n"] != take_no]
            takes.append({"n": take_no, "wav": rel, "duration": round(duration, 3)})
            takes.sort(key=lambda t: t["n"])
            rec.update({
                "status": "done", "wav": rel, "duration": round(duration, 3),
                "emotion": row.get("emotion"), "role_name": row.get("role_name"),
                "text": row.get("text"), "hash": row.get("hash", ""),
                "cfg_scale": row.get("cfg_scale"), "seed": row.get("seed"),
                "take": take_no, "takes": takes,
            })
            s["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    P._update_json("scripts.json", _mut)


def _take_info(rec: dict, fresh: bool) -> dict:
    """给前端的版本视图：只暴露 n 与时长，路径由后端自己算。"""
    if not fresh:
        return {"takes": [], "take": 0}
    takes = _norm_takes(rec)
    current = rec.get("take")
    if not current:
        # 老记录没写 take：按 wav 反查是哪一版
        current = next((t["n"] for t in takes if t.get("wav") == rec.get("wav")), 1)
    return {"takes": [{"n": t["n"], "duration": t.get("duration") or 0} for t in takes],
            "take": int(current)}


def _same_scalar(a, b) -> bool:
    """脏数据（字符串/None）也能比，不让它把已合成的行打成 pending。"""
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def _lead_map(script: dict) -> dict[int, float]:
    """行号 → 提前量（秒）。只留 0.05~2 秒的有效值，脏数据直接忽略。

    提前量不参与 _line_hash：它只影响「这一行在整轨里放多早」，不改音频内容，
    所以调它不该把已经合成好的行打回待重做。
    """
    out: dict[int, float] = {}
    for k, v in (script.get("line_lead") or {}).items():
        try:
            idx = int(k)
            sec = round(float(v or 0), 2)
        except (TypeError, ValueError):
            continue
        if 0.05 <= sec <= 2.0:
            out[idx] = sec
    return out


def _enriched(script: dict, parsed) -> list[dict]:
    """把解析结果补上 role_id/voice_id 与完成状态。

    完成状态必须同时满足「状态为 done」「内容哈希一致」「cfg/seed 一致」。
    前两项防止改了台词却复用上一版音频；第三项防止改了 CFG 或角色 seed
    还拿旧音频当新成品。老记录里没有 cfg_scale/seed 字段时用当前值兜底，
    所以升级不会把用户已经合成好的整剧变成一片「未合成」。
    """
    roles = _roles_for(script)
    voices = _voices_by_id()
    cfg = float(script.get("cfg_scale") if script.get("cfg_scale") is not None
                else DEFAULT_CFG)
    stored = {l["idx"]: l for l in script.get("lines") or [] if "idx" in l}
    leads = _lead_map(script)
    out = []
    for line in parsed.lines:
        row = line.as_dict()
        role = roles.get(line.role_name)
        row["role_id"] = role["id"] if role else None
        row["voice_id"] = (role or {}).get("voice_id") or None
        row["speed"] = _role_speed(role)
        row["lead"] = leads.get(line.idx, 0.0)   # 抢话提前量（秒）
        row["hash"] = _line_hash(row)
        row["cfg_scale"] = cfg
        row["seed"] = _seed_for(script, role)
        row["route"] = _voice_route(voices.get(row["voice_id"] or ""))
        # clone 路线上游忽略 instruction：把这件事显式告诉界面，
        # 而不是让用户以为情绪/氛围已经生效
        row["instruction_effective"] = row["route"] != "clone"
        rec = stored.get(line.idx) or {}
        fresh = (rec.get("status") == "done"
                 and rec.get("hash") == row["hash"]
                 and _same_scalar(rec.get("cfg_scale", cfg), cfg)
                 and _same_scalar(rec.get("seed", row["seed"]), row["seed"]))
        row["status"] = "done" if fresh else "pending"
        row["wav"] = rec.get("wav", "") if fresh else ""
        row["duration"] = rec.get("duration", 0.0) if fresh else 0.0
        row.update(_take_info(rec, fresh))
        out.append(row)
    return out


def _synth_line(text: str, role: dict | None, instruction: str,
                cfg: float, seed: int, out_path: Path,
                speed: float = 1.0) -> tuple[float, str, str]:
    """合成一行到 out_path，返回 (时长秒, 错误串, 引擎提示)。

    引擎提示（X-Adapter-Note）承载静默降级原因，比如克隆路线忽略指令；
    丢掉它等于把「为什么效果不对」藏起来，所以必须带回去。
    """
    voices = _voices_by_id()
    fields: dict[str, str] = {"text": text, "seed": str(seed)}
    files: dict[str, tuple[str, bytes]] = {}

    voice = voices.get((role or {}).get("voice_id") or "")
    if voice:
        ref = (voice.get("ref_audio") or "").strip()
        txt = (voice.get("ref_text") or "").strip()
        # 设计型音色本来就没有参考音频，只有「有音频却缺文稿」才是配置错误
        if ref and not txt:
            return 0.0, f"音色「{voice.get('name', '')}」缺文字稿（参考音频与文稿必须成对）", ""
        if ref:
            rp = P.DATA / ref
            if not rp.is_file():
                return 0.0, f"音色「{voice.get('name', '')}」的参考音频文件缺失", ""
            files["ref_audio"] = (rp.name, rp.read_bytes())
            fields["ref_text"] = txt

    if instruction.strip():
        fields["instruction"] = instruction.strip()
        fields["cfg_scale"] = str(cfg)
    else:
        # 无指令模板没有 negative prompt，cfg 必须=1，否则上游直接 500
        fields["cfg_scale"] = "1"

    body, ctype = P._remultipart(fields, files)
    hdrs: dict = {}
    code, raw = P._post_with_retry("/v1/audio/speech", body, ctype, headers_out=hdrs)
    if code != 200:
        try:
            detail = json.loads(raw).get("detail")
        except Exception:
            detail = raw[:160].decode("utf-8", "replace")
        return 0.0, str(detail)[:200], ""

    note = P._engine_note(hdrs) if hdrs else ""
    pcm = P.resample_pcm(raw, speed) if abs(speed - 1.0) > 1e-6 else raw
    wav = P.pcm_to_wav(pcm)
    out_path.write_bytes(wav)
    return len(pcm) / (P.SAMPLE_RATE * 2), "", note


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------
def _job_view(job: dict) -> dict:
    """返回可安全序列化的快照。

    直接返回 job["rows"] 会让 json.dumps 与 worker 的 update() 并发迭代同一个
    dict，偶发 "dictionary changed size during iteration" 断连。
    """
    elapsed = time.time() - job["started_at"]
    finished = job["done"] + job["failed"]
    durs = list(job.get("durs") or [])
    per_line = (sum(durs) / len(durs)) if durs else 0.0
    remaining = max(0, job["total"] - finished)
    pos = 0
    if job.get("state") == "queued":
        try:
            pos = _JOB_QUEUE.index(job["id"]) + 1
        except ValueError:
            pos = 0
    return {
        "id": job["id"],
        "script_id": job["script_id"],
        "state": job["state"],
        "queue_pos": pos,
        "total": job["total"],
        "done": job["done"],
        "failed": job["failed"],
        "current": job["current"],
        "elapsed": round(elapsed, 1),
        "per_line": round(per_line, 1),
        "eta": round(per_line * remaining, 1) if per_line else None,
        "rows": {k: dict(v) for k, v in job["rows"].items()},
        # 终态原因要能分开说：任务本身崩了 / 整轨拼接失败 / 混音失败，
        # 处理方式完全不同，前端不该只看到一个「完成」。
        "error": job.get("error", ""),
        "concat_error": job.get("concat_error", ""),
        "mix_error": job.get("mix_error", ""),
    }


def _prune_jobs() -> None:
    """清掉结束超过 10 分钟的任务。_JOBS 只增不删会随使用时间单调涨内存。

    **只清真正结束过的任务**：排队中的任务还没有 finished_at，用
    `v.get("finished_at", 0) < cutoff` 会把它当成「1970 年就结束了」直接删掉——
    而它的 id 还留在 _JOB_QUEUE 里，于是它永远不会被执行，前端只能拿到
    404「任务不存在」。这是真实踩过的坑：单槽设计下「A 在跑、B 排队、
    再提交 C 触发清理」就会把 B 吞掉。
    """
    cutoff = time.time() - 600
    with _JOBS_LOCK:
        dead = [k for k, v in _JOBS.items()
                if v["state"] != "running"
                and v.get("finished_at")
                and v["finished_at"] < cutoff]
        for jid in dead:
            _JOBS.pop(jid, None)
        # 队列里指向已消失任务的悬空 id 一并清掉，避免 _pump_queue 空转
        if _JOB_QUEUE:
            for jid in list(_JOB_QUEUE):
                if jid not in _JOBS:
                    _JOB_QUEUE.remove(jid)


def _batches(rows: list[dict], size: int):
    """按固定大小切批（生成器）。"""
    if size <= 0:
        yield rows
        return
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def _run_job(job: dict, script_id: str, rows: list[dict], gap: float) -> None:
    try:
        script = _find(script_id) or {}
        out_dir = _lines_dir(script_id)
        roles = _roles_for(script)
        cfg = float(script.get("cfg_scale") if script.get("cfg_scale") is not None
                    else DEFAULT_CFG)
        base_seed = int(script.get("seed") if script.get("seed") is not None else 42)
        # 8GB 卡上 design / clone 权重互斥，混排逐行会反复卸载+加载模型。
        # 按路线分组（同路线内保持原行序），换模型次数压到 ≤2 次；
        # _concat 与时间轴都按 idx 拼，播放顺序不受影响。
        rows = sorted(rows, key=lambda r: (0 if r.get("route") != "clone" else 1,
                                          r["idx"]))

        # >100 行自动分批：8 GB 卡一次扛不住 200 行连着跑，
        # 但用户不该自己记得分批——这里按 MAX_LINES 切批，批与批之间
        # 留一口气给引擎的 409 队列排空，进度照常上报，不丢。
        for batch_no, batch in enumerate(_batches(rows, MAX_LINES)):
            if job["stop"]:
                job["state"] = "stopped"
                return
            job["batch"] = batch_no + 1
            job["batches"] = (len(rows) + MAX_LINES - 1) // MAX_LINES
            for row in batch:
                if job["stop"]:
                    job["state"] = "stopped"
                    return
                idx = row["idx"]
                with _JOBS_LOCK:
                    job["current"] = idx
                    job["rows"][idx]["state"] = "running"

                role = roles.get(row["role_name"])
                if role is None:
                    # 未知角色在启动前已被拦下，这里是兜底：按行失败，不影响其他行
                    with _JOBS_LOCK:
                        job["rows"][idx].update(state="failed", error="未知角色")
                        job["failed"] += 1
                    continue

                name = f"line_{idx:03d}.wav"
                t_line = time.time()
                # clone 路线上游会直接忽略 instruction / cfg_scale，
                # 与其下发一堆无效参数，不如明确置空并让界面标出来。
                clone_row = row.get("route") == "clone"
                instruction = "" if clone_row else row.get("instruction", "")
                try:
                    dur, err, note = _synth_line(row["text"], role, instruction,
                                                 1.0 if clone_row else cfg,
                                                 int(row.get("seed") or base_seed),
                                                 out_dir / name,
                                                 float(row.get("speed") or 1.0))
                except Exception as exc:          # 单行异常不该终止整部剧
                    dur, err, note = 0.0, f"{type(exc).__name__}: {exc}"[:200], ""

                job.setdefault("durs", []).append(time.time() - t_line)
                # 状态改动一律在锁内做：`job["done"] += 1` 是读-改-写，
                # 不加锁时前端轮询可能正好读到中间值，进度条偶尔差一行。
                # 临界区里绝不放 IO（_persist_take 在锁外）。
                with _JOBS_LOCK:
                    if err:
                        job["rows"][idx].update(state="failed", error=err)
                        job["failed"] += 1
                    else:
                        rel = f"audio/scripts/{script_id}/{name}"
                        job["rows"][idx].update(state="done", wav=rel,
                                                duration=round(dur, 3), error="",
                                                engine_note=note)
                        job["done"] += 1
                if not err:
                    # 整剧合成落的是「新内容的第 1 版」：顺手清掉旧内容的版本，
                    # 不然换完台词还能切回上一版的音频，文不对音
                    _persist_take(script_id, idx, row, 1, dur, rel, reset=True)

        # 终态在 finally 里置位：拼接与混音都落盘之后才算「完成」。
        # 早一步报 done，前端会立刻去下载导出包，拿到的是上一版整轨。
    except Exception as exc:
        # 任何意外都要落到一个终态，否则前端会无限轮询、ETA 一直涨
        job["state"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        job["finished_at"] = time.time()
        try:
            _concat(script_id, gap)
        except Exception as exc:
            job["concat_error"] = f"{type(exc).__name__}: {exc}"[:200]
        # 混音在 _concat 内部完成，失败只落在剧本记录上；任务快照也带一份，
        # 前端轮询的是 job，不该再翻一次剧本才知道「混音没成」。
        try:
            job["mix_error"] = "" if job.get("concat_error") else str(
                (_find(script_id) or {}).get("mix_error") or "")
        except Exception as exc:
            # 这里原来把混音错误清空：用户拿到一个没有混音的成片，
            # 界面上却什么都看不到。宁可显示「读不到混音状态」。
            job["mix_error"] = f"读取混音状态失败：{type(exc).__name__}"
            _log("mix", f"script={script_id} 读取混音状态失败：{exc}")
        # 拼接（与混音）都落盘之后才报完成：前端看到 done 就能直接下载
        if job["state"] == "running":
            job["state"] = "done"
        _pump_queue()     # 这一单结束了，看看队列里还有没有下一单


def _persist_line(script_id: str, idx: int, patch: dict) -> None:
    def _mut(items):
        for s in items:
            if s["id"] != script_id:
                continue
            lines = s.setdefault("lines", [])
            hit = next((l for l in lines if l["idx"] == idx), None)
            if hit is None:
                lines.append({"idx": idx, **patch})
            else:
                hit.update(patch)
            s["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    P._update_json("scripts.json", _mut)


def _silence(seconds: float) -> bytes:
    n = int(max(0.0, seconds) * P.SAMPLE_RATE) * 2
    return b"\x00" * n


def _read_pcm(path: Path) -> bytes:
    data = path.read_bytes()
    return data[44:] if data[:4] == b"RIFF" else data


def _concat(script_id: str, gap: float) -> None:
    """把已完成的行按顺序拼接，写整轨 + 时间轴。

    只更新 timeline/full_wav/duration/gap 这几个字段，不做「读整条 → 改 → 整条写回」：
    整条回写会覆盖 worker 刚落的行状态，也会覆盖用户同时 PUT 进来的 raw/title。
    """
    script = _find(script_id)
    if not script:
        return
    parsed = parse_for(script)
    rows = _enriched(script, parsed)
    out_dir = _lines_dir(script_id)

    # 已完成、且音频真的在盘上的行，先取出来再决定走哪条拼接路径
    done: list[tuple[dict, bytes]] = []
    for row in rows:
        if row["status"] != "done" or not row["wav"]:
            continue
        # 走记录里的实际路径：第 2 版以后的文件名带 _t{n}，按行号拼名字
        # 会拼到第 1 版，用户切了版本却听不出区别
        path = P.DATA / row["wav"]
        if not path.is_file():
            continue
        done.append((row, _read_pcm(path)))

    timeline: list[dict] = []
    cursor = 0.0
    overlap = any(float(r.get("lead") or 0) > 0 for r, _ in done)

    if overlap:
        # 抢话：第 N 行可以提前 lead 秒开始，和上一行在时间轴上叠一段。
        # 逐段写字节做不到重叠，这里用 float 累加再限幅落回 s16。
        import numpy as np

        starts: list[float] = []
        for i, (row, chunk) in enumerate(done):
            dur = len(chunk) / (P.SAMPLE_RATE * 2)
            if i == 0:
                start = 0.0
            else:
                start = max(0.0, cursor + gap - float(row.get("lead") or 0))
            starts.append(start)
            cursor = max(cursor, start + dur)      # 重叠时游标不能倒退
        total = cursor
        n = int(round(total * P.SAMPLE_RATE))
        buf = np.zeros(max(1, n), dtype=np.float32)
        for (row, chunk), start in zip(done, starts):
            arr = np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768.0
            i0 = int(round(start * P.SAMPLE_RATE))
            buf[i0:i0 + len(arr)] += arr[:max(0, len(buf) - i0)]
        peak = float(np.abs(buf).max())
        if peak > 0.98:                            # 留 1~2 dB 余量，别削顶
            buf *= 0.98 / peak
        pcm = (np.clip(buf, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        for (row, chunk), start in zip(done, starts):
            dur = len(chunk) / (P.SAMPLE_RATE * 2)
            timeline.append({
                "idx": row["idx"] + 1,
                "role": row["role_name"],
                "emotion": row["emotion"],
                "scene": row.get("scene") or "",
                "start": round(start, 2),
                "end": round(start + dur, 2),
                "lead": round(float(row.get("lead") or 0), 2),
                "text": row["text"],
            })
    else:
        # 没有抢话：保持老的「逐段字节拼接 + 固定静音」，这样已有剧本的
        # full.wav 仍然逐字节不变（有测试盯着这件事）
        pcm = bytearray()
        last_done = -1
        for row, chunk in done:
            if last_done >= 0 and gap > 0:
                pcm += _silence(gap)
                cursor += gap
            start = cursor
            pcm += chunk
            dur = len(chunk) / (P.SAMPLE_RATE * 2)
            cursor += dur
            last_done = row["idx"]
            timeline.append({
                "idx": row["idx"] + 1,
                "role": row["role_name"],
                "emotion": row["emotion"],
                # 场次名：混音要靠它把环境声/BGM 铺到对应的那一段
                "scene": row.get("scene") or "",
                "start": round(start, 2),
                "end": round(cursor, 2),
                "text": row["text"],
            })

    if not pcm:
        return
    full = out_dir / "full.wav"
    full.write_bytes(P.pcm_to_wav(bytes(pcm)))
    patch = {
        "timeline": timeline,
        "full_wav": f"audio/scripts/{script_id}/full.wav",
        "duration": round(cursor, 2),
        "gap": gap,
    }

    def _mut(items):
        for s in items:
            if s["id"] == script_id:
                s.update(patch)

    P._update_json("scripts.json", _mut)
    # 铺底是拼接之后的后处理：不推理、不占显存，重混一次几秒钟。
    # 失败只记错误，不影响已经落盘的干声整轨。
    try:
        mix_rel, mix_err = _build_mix(script_id)
    except Exception as exc:
        mix_rel, mix_err = "", f"{type(exc).__name__}: {exc}"[:200]

    # 有没有结果都要写回：场次铺底被清空、或素材被删之后 _build_mix 返回
    # ("", "")，此时旧的 mix.wav 必须作废——否则导出会把含已删素材的旧混音
    # 当成新成片交付，用户完全看不出来。
    def _mut_mix(items):
        for s in items:
            if s["id"] == script_id:
                s["mix_wav"] = mix_rel
                s["mix_error"] = mix_err
                s["mix_stale"] = False

    P._update_json("scripts.json", _mut_mix)


# ---------------------------------------------------------------------------
# 素材库（环境声 / BGM）与混音
# ---------------------------------------------------------------------------
def _assets_dir() -> Path:
    d = P.DATA / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _assets() -> list[dict]:
    return P._load("assets.json")


def _asset_by_id(aid: str) -> dict | None:
    for a in _assets():
        if str(a.get("id")) == str(aid):
            return a
    return None


def _asset_pcm(aid: str) -> bytes:
    """读素材的 24k 单声道 PCM 缓存；素材丢了就当没有，不让整条链路崩。"""
    a = _asset_by_id(aid)
    if not a:
        return b""
    rel = str(a.get("path") or "")
    if not rel:
        return b""
    p = P.DATA / rel
    try:
        if not p.is_file():
            return b""
        return p.read_bytes()
    except OSError:
        return b""


def _store_asset(meta: dict, pcm: bytes) -> dict:
    """落盘素材（PCM 缓存 + 索引），返回最终元信息。"""
    aid = str(meta["id"])
    rel = f"assets/{aid}.pcm"
    _assets_dir().joinpath(f"{aid}.pcm").write_bytes(pcm)
    meta = dict(meta)
    meta["path"] = rel
    meta["duration"] = round(ST.pcm_duration(pcm), 2)

    def _mut(items):
        if not any(str(a.get("id")) == aid for a in items):
            items.append(meta)

    P._update_json("assets.json", _mut)
    return meta


def _asset_public(a: dict) -> dict:
    """对外只暴露界面需要的字段（路径是内部细节）。"""
    return {
        "id": a.get("id"),
        "name": a.get("name"),
        "kind": a.get("kind"),
        "source": a.get("source"),
        "duration": a.get("duration"),
        "created_at": a.get("created_at"),
    }


def _build_mix(script_id: str) -> tuple[str, str]:
    """按场次铺底重算混音，返回 (mix.wav 相对路径, 错误说明)。

    干声 full.wav 一个字节都不动；混音另存 mix.wav。没配铺底素材时返回
    ("", "")——不产出「只是干声换了个名字」的假混音。
    """
    script = _find(script_id)
    if not script:
        return "", "剧本不存在"
    timeline = script.get("timeline") or []
    if not timeline:
        return "", "还没有整轨音频，先点「合成全剧」"
    full_rel = str(script.get("full_wav") or "")
    full = P.DATA / full_rel if full_rel else None
    if not full or not full.is_file():
        return "", "还没有整轨音频，先点「合成全剧」"
    voice = _read_pcm(full)
    if not voice:
        return "", "整轨音频读不出来"

    scenes = {s["name"]: s for s in _norm_scenes(script.get("scenes"))}
    beds: list[dict] = []
    for name, start, end in ST.scene_ranges(timeline):
        sc = scenes.get(name) or {}
        amb = str(sc.get("ambience") or "")
        bgm = str(sc.get("bgm") or "")
        if not amb and not bgm:
            continue
        gain = _clamp_gain(sc.get("gain", DEFAULT_SCENE_GAIN))
        dur = max(0.0, float(end) - float(start))
        if dur <= 0:
            continue
        if amb:
            pcm = ST.loop_pcm(_asset_pcm(amb), dur)
            if pcm:
                beds.append({"pcm": pcm, "start": start, "gain": gain * 0.8,
                             "fade_in": 0.5, "fade_out": 0.5})
        if bgm:
            pcm = ST.loop_pcm(_asset_pcm(bgm), dur)
            if pcm:
                beds.append({"pcm": pcm, "start": start, "gain": gain,
                             "fade_in": 1.5, "fade_out": 1.5})
    if not beds:
        return "", ""
    mix = ST.build_mix(voice, beds, timeline, duck_db=DUCK_DB)
    _lines_dir(script_id).joinpath("mix.wav").write_bytes(ST.write_wav(mix))
    return f"audio/scripts/{script_id}/mix.wav", ""


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def _export_parts(raw_path: str) -> tuple[list[str], list[str]]:
    """从查询串解析要打包的内容，返回 (有效项, 非法项)。

    默认打全套，与 PRD 的「四件套」一致；显式传 parts 时按勾选来，
    这样用户只要音频、或者只要时间轴时不至于被塞一包用不上的文件。
    """
    query = urlparse(raw_path or "").query
    raw = (parse_qs(query).get("parts") or [DEFAULT_PARTS])[0]
    asked = [p.strip() for p in raw.split(",") if p.strip()]
    valid = [p for p in asked if p in EXPORT_PARTS]
    invalid = [p for p in asked if p not in EXPORT_PARTS]
    return valid, invalid


def _clock(seconds: float, sep: str = ",") -> str:
    """秒 → 字幕时间码（SRT 用逗号、WebVTT 用点）。"""
    ms = max(0, int(round(float(seconds) * 1000)))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _subtitle_text(script: dict, fmt: str) -> str:
    """用时间轴生成 SRT / VTT：多角色剧本里带上角色名才好分辨谁在说话。"""
    timeline = script.get("timeline") or []
    sep = "," if fmt == "srt" else "."
    lines = ["WEBVTT", ""] if fmt == "vtt" else []
    for i, t in enumerate(timeline, 1):
        text = str(t.get("text") or "").replace("\r", " ").replace("\n", " ")
        role = str(t.get("role") or "").strip()
        label = f"{role}：{text}" if role else text
        lines.append(str(i))
        lines.append(f"{_clock(t['start'], sep)} --> {_clock(t['end'], sep)}")
        lines.append(label)
        lines.append("")
    return "\n".join(lines) + ("\n" if lines else "")


def _export_plan(script: dict, parts: list[str]) -> tuple[list[dict], str]:
    """检查每个 part 是否真的可导出，返回 (已完成的行, 错误说明)。

    「有没有音频」是个全局布尔，拿它放行会让用户导出空的那一半：只有整轨时
    parts=lines 会得到空的 lines/，只有分行时 parts=full 会没有 full.wav。
    更要紧的是**旧音频**：改完台词后 full.wav 与 timeline 还是上一版，而
    script.txt 是新文本，三份文件互相矛盾，用户完全看不出来。
    """
    rows = _enriched(script, parse_for(script))
    done = [r for r in rows if r["status"] == "done" and r.get("wav")]
    pending = len(rows) - len(done)
    out_dir = P.AUDIOS / "scripts" / script["id"]

    if "full" in parts and not (out_dir / "full.wav").is_file():
        return done, "还没有整轨音频，先点「合成全剧」再导出"
    if "lines" in parts and not done:
        return done, "还没有合成好的分行音频，先点「合成全剧」再导出"
    # 字幕与时间轴同源于 timeline：有行没重做时它们的秒数是上一版的
    if any(k in parts for k in ("full", "timeline", "subtitles")) and pending:
        return done, (f"还有 {pending} 行没合成，或台词改过还没重做；"
                      "整轨、时间轴和字幕都是上一版的，先合成再导出")
    if "subtitles" in parts and not (script.get("timeline") or []):
        return done, "还没有时间轴，先点「合成全剧」再导出字幕"
    if "mix" in parts:
        mix_rel = str(script.get("mix_wav") or "")
        if not mix_rel or not (P.DATA / mix_rel).is_file():
            return done, ("还没有混音音频（给场次配上环境声或 BGM 后会自动生成，"
                          "也可以点「一键出片」）")
    return done, ""


def _export_zip(script: dict, parts: list[str], rows: list[dict]) -> bytes:
    buf = io.BytesIO()
    out_dir = P.AUDIOS / "scripts" / script["id"]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if "full" in parts:
            full = out_dir / "full.wav"
            if full.is_file():
                zf.write(full, "full.wav")
        if "lines" in parts:
            # 只写状态为 done 的行：磁盘上残留的旧 line_XXX.wav 属于上一版
            # 台词，混进导出包就是文不对音。多版本时导的是「当前选中的那一版」。
            for row in rows:
                p = P.DATA / row["wav"] if row.get("wav") else out_dir / f"line_{row['idx']:03d}.wav"
                if not p.is_file():
                    continue
                # 序号在前保证唯一：两个角色清洗后可能同名，重名条目
                # 在部分解压器里会互相覆盖
                safe = P._safe_name(row["role_name"])[:24] or "line"
                zf.write(p, f"lines/{row['idx'] + 1:02d}_{safe}.wav")
        if "timeline" in parts:
            csv_buf = io.StringIO()
            w = csv.writer(csv_buf)
            w.writerow(["序号", "场次", "角色", "情绪", "起", "止", "台词"])
            for t in script.get("timeline") or []:
                w.writerow([t["idx"], t.get("scene", ""), t["role"], t["emotion"],
                            t["start"], t["end"], t["text"]])
            zf.writestr("timeline.csv", "\ufeff" + csv_buf.getvalue())
        if "script" in parts:
            zf.writestr("script.txt", script.get("raw", ""))
        if "subtitles" in parts:
            zf.writestr("subtitles.srt", _subtitle_text(script, "srt"))
            zf.writestr("subtitles.vtt", _subtitle_text(script, "vtt"))
        if "mix" in parts:
            mix = P.DATA / str(script.get("mix_wav") or "")
            if mix.is_file():
                zf.write(mix, "mix.wav")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
def _apply_cast(sid: str, forced: str = "") -> tuple[int, dict]:
    """把剧本里还没配的角色一次补齐，返回 (HTTP 状态, 响应体)。

    保存剧本时也会走这里：作者粘贴完台词就该能直接点合成，不该再手工
    去音色库建 N 次角色。抽成函数而不是留在路由里，是因为 POST/PUT
    需要在同一个锁内复用它，避免两个入口各写一份逻辑。
    """
    script = _find(sid)
    if not script:
        return 404, {"detail": "剧本不存在"}
    with _CAST_LOCK:
        # 锁内重读：两个并发请求不能共用同一份旧快照
        script = _find(sid) or script
        # cast_isolated=True（新剧本）= 缺的角色各建一份专属；
        # 缺省（老剧本）= 保持升级前的「全局同名复用」行为
        isolated = bool(script.get("cast_isolated"))
        has_cast = bool(script.get("cast"))
        # 判定「缺谁」用哪套角色：
        #  * 全新的隔离剧本（cast 还是空的）→ 只看 cast，保证它的「旁白」是自己的
        #  * 其余（老剧本 / 已经在用的隔离剧本）→ cast 优先 + 回退全局，
        #    手工建的全局角色要能用，回退命中的绑定下面会被固化进 cast
        roles_ctx = (_roles_cast_only(script) if isolated and not has_cast
                     else _roles_for(script))
        parsed = parse_for(script, roles_ctx)
        if len(parsed.lines) > HARD_MAX_LINES:
            return 400, {"detail": f"一次最多 {HARD_MAX_LINES} 行"}
        if forced and forced not in _voices_by_id():
            return 400, {"detail": "指定的音色不存在"}
        # 回退命中的角色先固化进 cast，再决定缺谁去新建——否则保存一次就把
        # 临时回退的角色换成新建的专属副本，role_id 一变已合成行全变 pending
        script = _sync_cast(script, roles_ctx, parsed)
        plan = plan_cast(parsed.errors, set(roles_ctx), _voices_by_id())
        if not plan:
            return 200, {"created": [], "count": 0,
                         "remaining": len(parsed.errors)}

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        new_voices: list[dict] = []
        new_roles: list[dict] = []
        created: list[dict] = []
        # 与 new_roles 一一对应：新建音色的名字（写回后按名换真实 id），
        # 复用已有音色时为 None
        role_voice_names: list[str | None] = []
        for item in plan:
            voice = item["voice"]
            if forced:
                voice = _voices_by_id().get(forced)
            reused = bool(voice)
            voice_name = None
            if not voice:
                voice = {
                    "id": P._new_id("v"),
                    "name": f"{item['name']}的声音",
                    "type": "design",
                    "archetype": item["archetype"],
                    "instruction": item["instruction"],
                    "ref_audio": None,
                    "ref_text": "",
                    "cfg_scale": DEFAULT_CFG,
                    "created_at": now,
                }
                new_voices.append(voice)
                voice_name = voice["name"]
            role_voice_names.append(voice_name)
            route = _voice_route(voice)
            role = {
                "id": P._new_id("r"),
                "name": item["name"],
                "description": f"从剧本自动补齐（{item['label']}）",
                "voice_id": voice["id"],
                "archetype": item["archetype"],
                "default_emotion": "",
                "speed": item["speed"],
                "accent": "",
                "defaults": {"cfg_scale": DEFAULT_CFG, "seed": 42},
                # 隔离模式下这个角色属于本剧：别的剧本写同名会各建一份，
                # 改这里的音色不会波及别人
                "script_id": sid if isolated else "",
                "created_at": now,
            }
            new_roles.append(role)
            created.append({
                "name": item["name"], "archetype": item["archetype"],
                "label": item["label"], "role_id": role["id"],
                "voice_id": voice["id"], "voice_name": voice["name"],
                "reused": reused, "route": route, "speed": item["speed"],
                # 本该复用的那个克隆音色被跳过了 → 界面上说明为什么
                "skipped_clone": item.get("skipped_clone") or "",
            })

        # 每个文件单独原子 RMW；mutator 内按名字查重，防止与用户在
        # 音色库里手工新建的对象撞车。顺序是音色→角色，中间态里
        # 不会出现「角色指向不存在音色」。
        def _add_voices(items, _new=new_voices):
            names = {str(v.get("name") or "").strip() for v in items}
            for v in _new:
                key = str(v["name"]).strip()
                if key not in names:
                    items.append(v)
                    names.add(key)

        def _add_roles(items, _new=new_roles):
            # 查重键是「名字 + 归属剧本」：隔离模式下不同剧本的同名角色要共存，
            # 同一个剧本内重复建同名才是要拦的
            keys = {(str(r.get("name") or "").strip(), str(r.get("script_id") or ""))
                    for r in items}
            for r in _new:
                key = (str(r["name"]).strip(), str(r.get("script_id") or ""))
                if key not in keys:
                    items.append(r)
                    keys.add(key)

        if new_voices:
            P._update_json("voices.json", _add_voices)
            # 同名音色可能是用户先前手工建的（名字带空格也算）；
            # 按 strip 后的名字换回真实 id，不能留下悬空 voice_id
            voices_now = P._load("voices.json")
            actual = {str(v.get("name") or "").strip(): v for v in voices_now}
            for r, name in zip(new_roles, role_voice_names):
                if name and name in actual:
                    r["voice_id"] = actual[name]["id"]
            for c, name in zip(created, role_voice_names):
                if name and name in actual:
                    c["voice_id"] = actual[name]["id"]

        valid_ids = {v["id"] for v in P._load("voices.json")}
        dangling = [r["name"] for r in new_roles if r["voice_id"] not in valid_ids]
        if dangling:
            return 400, {"detail": f"音色没写成功：{', '.join(dangling[:3])}，请重试"}
        P._update_json("roles.json", _add_roles)

        # 以落盘结果为准回报，不能把「被查重跳过」的对象算成新建。
        # 查重键是「名字 + 归属剧本」——同名角色在不同剧本里可以共存。
        scope = sid if isolated else ""
        real_roles = {(str(r.get("name") or "").strip(),
                       str(r.get("script_id") or "")): r
                      for r in P._load("roles.json")}
        real_voices = {v["id"]: v for v in P._load("voices.json")}
        verified = []
        for c in created:
            r = real_roles.get((c["name"], scope))
            if not r or r.get("voice_id") not in real_voices:
                continue
            c["role_id"] = r["id"]
            c["voice_id"] = r["voice_id"]
            c["voice_name"] = real_voices[r["voice_id"]]["name"]
            c["route"] = _voice_route(real_voices[r["voice_id"]])
            verified.append(c)

        # 认领进 cast：写落盘后的真实 role id，不能拿计划对象里的临时 id
        if verified:
            ids = [str(c["role_id"]) for c in verified]

            def _add_cast(items):
                for s in items:
                    if s.get("id") != sid:
                        continue
                    cast = [str(x) for x in (s.get("cast") or [])]
                    for rid in ids:
                        if rid not in cast:
                            cast.append(rid)
                    s["cast"] = cast

            P._update_json("scripts.json", _add_cast)

        remaining = len(parse_for(_find(sid) or script).errors)
        return 201, {"created": verified, "count": len(verified),
                     "remaining": remaining}


def _new_job(sid: str, script: dict, rows: list[dict],
             todo: list[dict]) -> dict:
    """登记一个逐行合成任务并起线程。

    「合成全剧」和「一键出片」共用它：两者跑的是同一套逐行合成，
    区别只是出片额外要走混音与打包。
    """
    job = {
        "id": "j_" + uuid.uuid4().hex[:10],
        "script_id": sid,
        "state": "running",
        "total": len(todo),
        "done": 0,
        "failed": 0,
        "current": -1,
        "started_at": time.time(),
        "stop": False,
        "durs": [],
        "rows": {r["idx"]: {"idx": r["idx"], "state": "pending",
                            "role_name": r["role_name"], "text": r["text"],
                            "emotion": r["emotion"]} for r in todo},
    }
    gap = float(script.get("gap") if script.get("gap") is not None else DEFAULT_GAP)
    with _JOBS_LOCK:
        # 先判忙再入表：job 自己的初始 state 就是 running，先入表会让它把自己
        # 当成「正在跑」，于是每个任务都排队、永远不启动
        busy = any(j["state"] == "running" for j in _JOBS.values())
        _JOBS[job["id"]] = job
        if busy:
            job["state"] = "queued"
            job["_args"] = (sid, todo, gap)     # 排队期间先握住参数
            _JOB_QUEUE.append(job["id"])
        else:
            job["started_at"] = time.time()
            threading.Thread(target=_run_job, args=(job, sid, todo, gap),
                             daemon=True).start()
    return job


def _pump_queue() -> None:
    """有空槽了就启动队首。任务收尾时调用；失败/停止都会走到。"""
    with _JOBS_LOCK:
        if any(j["state"] == "running" for j in _JOBS.values()):
            return
        while _JOB_QUEUE:
            jid = _JOB_QUEUE.pop(0)
            job = _JOBS.get(jid)
            if not job or job.get("state") != "queued":
                continue
            args = job.pop("_args", None)
            if not args:
                continue
            sid, todo, gap = args
            job["state"] = "running"
            job["started_at"] = time.time()
            threading.Thread(target=_run_job, args=(job, sid, todo, gap),
                             daemon=True).start()
            return


def handle(h, method: str, path: str, body: bytes, ctype: str) -> bool:
    """处理剧本相关请求；不是本模块的路由时返回 False。"""
    if (not path.startswith("/api/scripts") and path != "/api/archetypes"
            and not path.startswith("/api/assets")):
        return False

    # ---- 素材库（环境声 / BGM）----
    if method == "GET" and path == "/api/assets":
        h._json([_asset_public(a) for a in _assets()])
        return True

    if method == "POST" and path == "/api/assets/generate":
        try:
            d = json.loads(body or b"{}")
        except Exception:
            d = {}
        kind = str(d.get("kind") or "white")
        if kind not in ST.NOISE_KINDS:
            h._err(f"未知的噪声类型：{kind}", 400)
            return True
        try:
            seconds = float(d.get("seconds") or 30)
        except (TypeError, ValueError):
            seconds = 30.0
        seconds = min(600.0, max(1.0, seconds))
        name = str(d.get("name") or "").strip() or f"{ST.NOISE_KINDS[kind].split('（')[0]} {int(seconds)}秒"
        pcm = ST.generate_noise(kind, seconds)
        meta = {
            "id": P._new_id("a"),
            "name": name[:60],
            "kind": kind,
            "source": "generated",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        h._json({"asset": _asset_public(_store_asset(meta, pcm))}, 201)
        return True

    if method == "POST" and path == "/api/assets":
        fields, files = P.parse_multipart(body or b"", ctype or "")
        if "file" not in files:
            h._err("请选择要上传的音频文件", 400)
            return True
        # 素材版权由用户自己负责：没勾授权声明就不让入库
        if str(fields.get("license_confirmed") or "").strip().lower() not in (
                "1", "true", "on", "yes"):
            h._err("请先确认你拥有该素材的使用授权", 400)
            return True
        fname, payload = files["file"]
        try:
            pcm = ST.decode_to_pcm24k(payload)
        except Exception as exc:
            h._err(f"这个文件解不开：{exc}", 400)
            return True
        if not pcm:
            h._err("这个文件里没有可用的音频", 400)
            return True
        name = str(fields.get("name") or "").strip() or str(fname or "上传素材")
        meta = {
            "id": P._new_id("a"),
            "name": name[:60],
            "kind": "upload",
            "source": "upload",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        h._json({"asset": _asset_public(_store_asset(meta, pcm))}, 201)
        return True

    if method == "DELETE" and path.startswith("/api/assets/") and path.count("/") == 3:
        aid = path.rsplit("/", 1)[-1]
        asset = _asset_by_id(aid)
        if not asset:
            h._err("素材不存在", 404)
            return True

        def _mut(items):
            items[:] = [a for a in items if str(a.get("id")) != str(aid)]

        P._update_json("assets.json", _mut)
        rel = str(asset.get("path") or "")
        if rel:
            try:
                (P.DATA / rel).unlink(missing_ok=True)
            except OSError:
                pass
        h._json({"deleted": aid})
        return True

    # ---- 元数据 ----
    if method == "GET" and path == "/api/archetypes":
        h._json({
            "archetypes": [{"key": k, **v} for k, v in ARCHETYPES.items()],
            "emotions": list(EMOTIONS.keys()),
            "moods": [{"key": k, "instruction": v} for k, v in MOODS.items() if k],
            "max_lines": HARD_MAX_LINES,
            "batch_lines": MAX_LINES,
            "default_gap": DEFAULT_GAP,
            "default_parts": DEFAULT_PARTS.split(","),
        })
        return True

    # ---- 任务状态 ----
    if method == "GET" and path.startswith("/api/scripts/jobs/"):
        job_id = path.rsplit("/", 1)[-1]
        # 快照必须在锁内取：worker 正在改 done/failed/rows，
        # 锁外读会拿到读-改-写中间态，进度条偶尔差一行
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            view = _job_view(job) if job else None
        if not job or view is None:
            h._err("任务不存在或已结束", 404)
            return True
        h._json(view)
        return True

    if method == "POST" and path.startswith("/api/scripts/jobs/") and path.endswith("/stop"):
        job_id = path.split("/")[4]
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if not job:
            h._err("任务不存在或已结束", 404)
            return True
        with _JOBS_LOCK:
            if job.get("state") == "queued":
                # 还没轮到它：直接出队并置终态，不用等 worker 收尾
                job["state"] = "stopped"
                job.pop("_args", None)
                job["finished_at"] = time.time()
                if job["id"] in _JOB_QUEUE:
                    _JOB_QUEUE.remove(job["id"])
            else:
                job["stop"] = True
        h._json({"ok": True, "state": job["state"]})
        return True

    # ---- 列表 / 详情 ----
    if method == "GET" and path == "/api/scripts":
        items = sorted(_all_scripts(), key=lambda s: s.get("updated_at", ""), reverse=True)
        h._json([{
            "id": s["id"], "title": s.get("title", ""), "updated_at": s.get("updated_at", ""),
            "line_count": len(parse_for(s).lines), "duration": s.get("duration", 0.0),
            "full_wav": s.get("full_wav", ""),
        } for s in items])
        return True

    if method == "GET" and path.count("/") == 4 and path.endswith("/export"):
        sid = path.split("/")[3]
        script = _find(sid)
        if not script:
            h._err("剧本不存在", 404)
            return True
        parts, invalid = _export_parts(getattr(h, "path", path))
        if invalid:
            h._err("不认识的导出内容：" + "、".join(invalid)
                   + f"（可选 {'/'.join(EXPORT_PARTS)}）")
            return True
        if not parts:
            h._err("请至少勾选一项要导出的内容")
            return True
        rows, why = _export_plan(script, parts)
        if why:
            h._err(why)
            return True
        data = _export_zip(script, parts, rows)
        title = (script.get("title") or "剧本").strip()[:40] or "剧本"
        ascii_name = f"script_{sid}.zip"
        h.send_response(200)
        h.send_header("Content-Type", "application/zip")
        # 中文文件名必须走 RFC 5987 的 filename*，否则部分浏览器下载成乱码；
        # 同时保留 ASCII 兜底名给不认 filename* 的客户端。
        # safe="" 不能省：标题里的 "/" 留在 filename* 里会让浏览器整段忽略。
        h.send_header("Content-Disposition",
                      f"attachment; filename=\"{ascii_name}\"; "
                      f"filename*=UTF-8''{quote(f'{title}.zip', safe='')}")
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        h.wfile.write(data)
        return True

    if method == "GET" and path.startswith("/api/scripts/") and path.count("/") == 3:
        sid = path.rsplit("/", 1)[-1]
        script = _find(sid)
        if not script:
            h._err("剧本不存在", 404)
            return True
        # 老剧本第一次被读时把现有全局角色认领进 cast（幂等，认领过就不再写）
        script = _sync_cast(script)
        parsed = parse_for(script)
        resp = {
            **{k: script.get(k) for k in
               ("id", "title", "raw", "gap", "duration", "full_wav",
                "timeline", "cfg_scale", "seed", "updated_at")},
            # 本剧用的角色 id 表与是否隔离：界面据此标「剧本专属」
            "cast": list(script.get("cast") or []),
            "cast_isolated": bool(script.get("cast_isolated")),
            "emotion_carry": bool(script.get("emotion_carry")),
            "line_lead": _lead_map(script),
            # scenes 以解析结果为准（作者可能刚加了 ## 场次），mood 从已存配置里取
            "scenes": _scenes_view(parsed.scenes, script.get("scenes")),
            "lines": _enriched(script, parsed),
            "errors": parsed.errors,
            "estimate": round(estimate_seconds(parsed), 1),
        }
        # 混音是可选产物：没配铺底时给空串而不是 null，界面少一处判空
        resp["mix_wav"] = script.get("mix_wav") or ""
        resp["mix_stale"] = bool(script.get("mix_stale"))
        # 上次混音失败的原因：干声整轨仍在，界面据此提示而不是静默无声
        resp["mix_error"] = script.get("mix_error") or ""
        h._json(resp)
        return True

    # ---- 解析预览 ----
    if method == "POST" and path.endswith("/parse"):
        sid = path.split("/")[3]
        try:
            d = json.loads(body or b"{}")
        except Exception:
            h._err("请求体必须是 JSON")
            return True
        script = _find(sid) or {"id": sid, "raw": "", "scenes": []}
        probe = {**script, "raw": d.get("raw", script.get("raw", "")),
                 "scenes": _norm_scenes(d.get("scenes", script.get("scenes") or []))}
        parsed = parse_for(probe)
        h._json({
            "lines": _enriched(probe, parsed),
            "errors": parsed.errors,
            "scenes": _scenes_view(parsed.scenes, probe.get("scenes")),
            "estimate": round(estimate_seconds(parsed), 1),
            "max_lines": HARD_MAX_LINES,
            "batch_lines": MAX_LINES,
        })
        return True

    # ---- 合成 ----
    if method == "POST" and path.endswith("/synth"):
        sid = path.split("/")[3]
        script = _find(sid)
        if not script:
            h._err("剧本不存在", 404)
            return True
        try:
            d = json.loads(body or b"{}")
        except Exception:
            d = {}
        parsed = parse_for(script)
        rows = _enriched(script, parsed)
        if not rows:
            h._err("剧本还没有可合成的台词")
            return True
        if len(rows) > HARD_MAX_LINES:
            h._err(f"一次最多合成 {HARD_MAX_LINES} 行（当前 {len(rows)} 行），请分剧")
            return True

        bad = [r for r in rows if r.get("role_id") is None]
        if bad:
            where = "、".join(str(r["idx"] + 1) for r in bad[:5])
            more = "" if len(bad) <= 5 else f" 等 {len(bad)} 行"
            h._err(f"第 {where}{more} 行的角色不存在，请先在角色库里建好")
            return True
        unbound = [r for r in rows if not r.get("voice_id")]
        if unbound:
            names = sorted({r["role_name"] for r in unbound})
            h._err("这些角色还没绑定音色：" + "、".join(names)
                   + "（角色卡片 → 编辑 → 绑定音色）")
            return True

        # 同一剧本同时只允许一个任务：两条线程会并发写同一批 line_XXX.wav
        with _JOBS_LOCK:
            for j in _JOBS.values():
                if j["script_id"] == sid and j["state"] == "running":
                    h._err("这个剧本正在合成中，请等它结束或先停止", 409)
                    return True
        _prune_jobs()

        resume = bool(d.get("resume", True))
        todo = [r for r in rows if not (resume and r["status"] == "done")]

        job = _new_job(sid, script, rows, todo)
        h._json({"job_id": job["id"], "total": len(todo),
                 "skipped": len(rows) - len(todo)})
        return True

    # 一键出片：合成 → 拼接 → 混音，一条龙。
    # 与「合成全剧」共用同一个任务机制，区别是这里不跳过已完成的行，
    # 出片要保证整轨与当前台词完全一致。
    if method == "POST" and path.endswith("/produce") and path.count("/") == 4:
        sid = path.split("/")[3]
        script = _find(sid)
        if not script:
            h._err("剧本不存在", 404)
            return True
        parsed = parse_for(script)
        rows = _enriched(script, parsed)
        if not rows:
            h._err("剧本还没有可合成的台词")
            return True
        bad = [r for r in rows if r.get("role_id") is None]
        if bad:
            where = "、".join(str(r["idx"] + 1) for r in bad[:5])
            h._err(f"第 {where} 行的角色不存在，请先在角色库里建好")
            return True
        unbound = [r for r in rows if not r.get("voice_id")]
        if unbound:
            names = sorted({r["role_name"] for r in unbound})
            h._err("这些角色还没绑定音色：" + "、".join(names))
            return True
        with _JOBS_LOCK:
            for j in _JOBS.values():
                if j["script_id"] == sid and j["state"] == "running":
                    h._err("这个剧本正在合成中，请等它结束或先停止", 409)
                    return True
        _prune_jobs()
        # 已完成的行直接复用，但**要重录**的行照样重做：出片必须与当前
        # 台词一致，否则导出的整轨和剧本对不上
        todo = [r for r in rows if r["status"] != "done"]
        job = _new_job(sid, script, rows, todo)
        h._json({"job_id": job["id"], "total": len(todo),
                 "skipped": len(rows) - len(todo),
                 "parts": list(EXPORT_PARTS)})
        return True

    # 只重算混音：改完场次的铺底不必重合成整剧，几秒钟出结果
    if method == "POST" and path.endswith("/mix") and path.count("/") == 4:
        sid = path.split("/")[3]
        if not _find(sid):
            h._err("剧本不存在", 404)
            return True
        try:
            mix_rel, err = _build_mix(sid)
        except Exception as exc:
            h._err(f"混音失败：{type(exc).__name__}: {exc}"[:200], 500)
            return True
        if err:
            h._err(err)
            return True

        def _mut(items):
            for s in items:
                if s["id"] == sid:
                    s["mix_wav"] = mix_rel
                    s["mix_error"] = ""
                    s["mix_stale"] = False

        P._update_json("scripts.json", _mut)
        h._json({"mix_wav": mix_rel})
        return True

    if method == "POST" and "/lines/" in path and path.endswith("/redo"):
        parts = path.split("/")
        sid = parts[3]
        try:
            idx = int(parts[5])
        except (IndexError, ValueError):
            h._err("行号不合法")
            return True
        script = _find(sid)
        if not script:
            h._err("剧本不存在", 404)
            return True
        parsed = parse_for(script)
        rows = _enriched(script, parsed)
        row = next((r for r in rows if r["idx"] == idx), None)
        if not row:
            h._err("这一行不存在", 404)
            return True
        if not row.get("voice_id"):
            h._err(f"角色「{row['role_name']}」还没绑定音色")
            return True
        role = _roles_for(script).get(row["role_name"])
        out_dir = _lines_dir(sid)
        # 重录 = 追加一版，不是覆盖：同一句常要试两三遍才满意，覆盖掉
        # 上一版等于逼用户在「两个都还行」之间反复重抽。
        stored = next((l for l in script.get("lines") or [] if l.get("idx") == idx), {})
        same_content = stored.get("hash") == row.get("hash")
        takes = _norm_takes(stored) if same_content else []
        if not takes:
            take_no = 1
        elif len(takes) >= MAX_TAKES:
            take_no = takes[0]["n"]          # 到上限就覆盖最旧那版，不无限涨盘
        else:
            take_no = max(t["n"] for t in takes) + 1
        name = _take_name(idx, take_no)
        clone_row = row.get("route") == "clone"
        dur, err, note = _synth_line(row["text"], role,
                                     "" if clone_row else row.get("instruction", ""),
                                     1.0 if clone_row else float(row.get("cfg_scale") or DEFAULT_CFG),
                                     int(row.get("seed") or 42),
                                     out_dir / name, row.get("speed") or 1.0)
        if err:
            h._err(err)
            return True
        rel = f"audio/scripts/{sid}/{name}"
        # 必须一起写 hash：续做与行状态都靠它判定，缺了会让刚重做好的行
        # 在下次读取时又变回「未合成」，音频在盘上却听不到。
        _persist_take(sid, idx, row, take_no, dur, rel, reset=not same_content)
        _concat(sid, float(script.get("gap") if script.get("gap") is not None else DEFAULT_GAP))
        h._json({"ok": True, "idx": idx, "take": take_no,
                 "duration": round(dur, 3), "wav": rel, "engine_note": note})
        return True

    # 切换某一行的版本：多 take 里选一个当成品，整轨跟着重拼
    if method == "POST" and "/lines/" in path and path.endswith("/take"):
        parts = path.split("/")
        sid = parts[3]
        try:
            idx = int(parts[5])
            d = json.loads(body or b"{}")
            want = int(d.get("n"))
        except (IndexError, ValueError, TypeError):
            h._err("行号或版本号不合法")
            return True
        script = _find(sid)
        if not script:
            h._err("剧本不存在", 404)
            return True
        rec = next((l for l in script.get("lines") or [] if l.get("idx") == idx), None)
        takes = _norm_takes(rec or {})
        hit = next((t for t in takes if t["n"] == want), None)
        if not hit:
            h._err("这个版本不存在，可能已经被覆盖了", 404)
            return True

        def _mut(items):
            for s in items:
                if s["id"] != sid:
                    continue
                for l in s.get("lines") or []:
                    if l.get("idx") == idx:
                        l.update({"take": want, "wav": hit["wav"],
                                  "duration": hit.get("duration") or 0})

        P._update_json("scripts.json", _mut)
        _concat(sid, float(script.get("gap") if script.get("gap") is not None else DEFAULT_GAP))
        h._json({"ok": True, "idx": idx, "take": want,
                 "wav": hit["wav"], "duration": hit.get("duration") or 0})
        return True

    if method == "POST" and path.endswith("/concat"):
        sid = path.split("/")[3]
        script = _find(sid)
        if not script:
            h._err("剧本不存在", 404)
            return True
        _concat(sid, float(script.get("gap") if script.get("gap") is not None else DEFAULT_GAP))
        fresh = _find(sid) or {}
        h._json({"ok": True, "duration": fresh.get("duration", 0.0),
                 "full_wav": fresh.get("full_wav", ""),
                 "timeline": fresh.get("timeline", [])})
        return True

    # 一键补齐角色：也可以由保存路径自动调用（见 _apply_cast）。
    if method == "POST" and path.count("/") == 4 and path.endswith("/cast"):
        sid = path.split("/")[3]
        try:
            d = json.loads(body or b"{}")
        except Exception:
            d = {}
        code, payload = _apply_cast(sid, str(d.get("voice_id") or "").strip())
        h._json(payload, code)
        return True

    # ---- 增删改 ----
    if method == "POST" and path == "/api/scripts":
        try:
            d = json.loads(body or b"{}")
        except Exception:
            h._err("请求体必须是 JSON")
            return True
        raw = str(d.get("raw") or "")
        if len(parse_script(raw).lines) > HARD_MAX_LINES:
            h._err(f"一次最多 {HARD_MAX_LINES} 行")
            return True
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        script = {
            "id": "s_" + uuid.uuid4().hex[:10],
            "title": str(d.get("title") or "未命名剧本"),
            "raw": raw,
            "scenes": _norm_scenes(d.get("scenes")),
            "gap": float(d.get("gap") or DEFAULT_GAP),
            "cfg_scale": float(d.get("cfg_scale") or DEFAULT_CFG),
            "seed": int(d.get("seed") or 42),
            # 保存就自动配角色；作者在剧本上关掉这项才会退回手工模式
            "auto_cast": bool(d.get("auto_cast", True)),
            # 新剧本走角色隔离：它的「旁白」是自己的，改音色不波及别的剧。
            # 老剧本没有这两个字段，保持「认领全局角色」的老语义
            "cast": [],
            "cast_isolated": True,
            # 情绪跨行延续：新剧本默认开（老剧本缺这个字段 = 关，行为不变）
            "emotion_carry": bool(d.get("emotion_carry", True)),
            "lines": [],
            "created_at": now,
            "updated_at": now,
        }
        P._update_json("scripts.json", lambda it: it.append(script))
        cast_error = ""
        if script["auto_cast"]:
            # 自动配角失败不该让「新建剧本」失败：原文已经落盘，
            # 用户还能点界面上的补齐按钮兜底。
            # 但不能一声不响：否则用户拿到一个「没角色、没音色」的剧本，
            # 界面显示「未知角色」，却不知道自己触发了什么错。
            try:
                _apply_cast(script["id"])
            except Exception as exc:
                cast_error = f"自动配角失败：{type(exc).__name__}: {exc}"[:200]
                _log("cast", f"script={script['id']} 新建剧本自动配角失败：{exc}")
        # 回读一次再返回：auto_cast 刚写进去的 cast 与角色要能被调用方看到，
        # 否则 201 里永远是空的 cast，前端只能再发一次 GET
        payload = _find(script["id"]) or script
        if cast_error:
            payload = {**payload, "cast_error": cast_error}
        h._json(payload, 201)
        return True

    if method == "PUT" and path.startswith("/api/scripts/") and path.count("/") == 3:
        sid = path.rsplit("/", 1)[-1]
        script = _find(sid)
        if not script:
            h._err("剧本不存在", 404)
            return True
        try:
            d = json.loads(body or b"{}")
        except Exception:
            h._err("请求体必须是 JSON")
            return True
        if "raw" in d:
            raw_text = str(d["raw"] or "")
            n = len(parse_script(raw_text).lines)
            if n > HARD_MAX_LINES:
                h._err(f"一次最多 {HARD_MAX_LINES} 行（当前 {n} 行），请分剧")
                return True
        if "emotion_carry" in d:
            script["emotion_carry"] = bool(d["emotion_carry"])
        before_lead = _lead_map(script)
        if "line_lead" in d:
            raw_lead = d["line_lead"] if isinstance(d["line_lead"], dict) else {}
            script["line_lead"] = {str(k): v for k, v in _lead_map({"line_lead": raw_lead}).items()}
        if "title" in d:
            script["title"] = str(d["title"] or "未命名剧本")
        if "raw" in d:
            script["raw"] = str(d["raw"] or "")   # 存成字符串：dict 会让解析时炸
        if "scenes" in d:
            # 合并而不是整体替换：作者临时删掉或改名一行 `## 场次` 时，
            # 那个场次配好的氛围不该在普通保存里被永久丢掉（改回来还能找回）。
            before_beds = {s["name"]: (s["ambience"], s["bgm"], s["gain"])
                           for s in _norm_scenes(script.get("scenes"))}
            merged = {s["name"]: s for s in _norm_scenes(script.get("scenes"))}
            for item in d["scenes"] or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                prev = merged.get(name) or {}
                norm = next(iter(_norm_scenes([item])), None)
                if norm is None:
                    continue
                merged[name] = {
                    "name": name,
                    "mood": norm["mood"],
                    # 铺底字段按「有没有这个键」判断：只改氛围时不该把
                    # 已经配好的环境声/BGM 悄悄清掉；显式传空串才是清空
                    "ambience": (str(item.get("ambience") or "").strip()
                                 if "ambience" in item
                                 else prev.get("ambience", "")),
                    "bgm": (str(item.get("bgm") or "").strip()
                            if "bgm" in item else prev.get("bgm", "")),
                    "gain": (_clamp_gain(item.get("gain")) if "gain" in item
                             else prev.get("gain", DEFAULT_SCENE_GAIN)),
                }
            script["scenes"] = list(merged.values())
            after_beds = {s["name"]: (s["ambience"], s["bgm"], s["gain"])
                          for s in _norm_scenes(script["scenes"])}
            # 铺底一改，旧的 mix.wav 就跟新配置对不上了。留着字段会让导出
            # 把过期混音当成成片交付，所以直接作废，等「重算混音」或一键出片。
            if before_beds != after_beds:
                script["mix_wav"] = ""
                script["mix_stale"] = True
        for k, cast in (("gap", float), ("cfg_scale", float), ("seed", int)):
            if k in d:
                script[k] = cast(d[k])
        if "auto_cast" in d:
            script["auto_cast"] = bool(d["auto_cast"])
        patch = {k: script[k] for k in
                 ("title", "raw", "scenes", "gap", "cfg_scale", "seed",
                  "auto_cast", "emotion_carry", "line_lead",
                  "mix_wav", "mix_stale") if k in script}
        patch["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        def _mut(items):
            for s in items:
                if s["id"] == sid:
                    s.update(patch)

        P._update_json("scripts.json", _mut)
        # 台词变了就顺手把新出现的角色配好：作者不需要再点任何按钮，
        # 返回的 lines/errors 直接就是配完后的状态。
        cast_error = ""
        if "raw" in d and script.get("auto_cast", True):
            try:
                _apply_cast(sid)
            except Exception as exc:
                # 用户刚存的台词对应的角色没建出来，却收到 200，
                # 界面只显示「角色没配好」红字，没人知道原因。
                cast_error = f"自动配角失败：{type(exc).__name__}: {exc}"[:200]
                _log("cast", f"script={sid} 保存后自动配角失败：{exc}")
        # 提前量只改时间轴，不用重新合成：直接重拼整轨与混音。
        # 不重拼的话界面上的时间轴和实际整轨会对不上。
        concat_error = ""
        if _lead_map(script) != before_lead and (script.get("full_wav") or ""):
            try:
                _concat(sid, float(script.get("gap") or 0.35))
            except Exception as exc:
                # 静默吞掉会让 line_lead 与磁盘上的整轨对不上，
                # 前端还在 toast「整轨已重拼」——界面在撒谎。
                concat_error = f"整轨重拼失败：{type(exc).__name__}: {exc}"[:200]
                _log("concat", f"script={sid} 抢话后重拼整轨失败：{exc}")
        fresh = _find(sid) or script
        parsed = parse_for(fresh)
        out = {**fresh, "lines": _enriched(fresh, parsed), "errors": parsed.errors}
        if cast_error:
            out["cast_error"] = cast_error
        if concat_error:
            out["concat_error"] = concat_error
        h._json(out)
        return True

    if method == "DELETE" and path.startswith("/api/scripts/") and path.count("/") == 3:
        sid = path.rsplit("/", 1)[-1]
        if not _find(sid):
            h._err("剧本不存在", 404)
            return True
        # 先让任务停下，否则它会在删掉的目录里继续写孤儿文件
        with _JOBS_LOCK:
            for j in _JOBS.values():
                if j["script_id"] == sid and j["state"] == "running":
                    j["stop"] = True

        def _drop(items):
            items[:] = [s for s in items if s["id"] != sid]

        P._update_json("scripts.json", _drop)
        # 专属角色是这部剧情的一部分：剧本没了它就永远用不上（孤儿角色会
        # 一直堆在角色库里，还顶着别人的剧本名）。音色不动——可能被别的
        # 角色复用，让用户在音色库里自己决定
        purged = purge_script_roles(sid)
        # 分行音频与整轨一起清掉，避免留下孤儿目录。
        # 删除是尽力而为：某些环境会拦批量删除并抛非 OSError，
        # 那不该让「删除剧本」这个操作失败。
        d = P.AUDIOS / "scripts" / sid
        if d.is_dir():
            for f in list(d.glob("*")):
                try:
                    f.unlink()
                except Exception:
                    pass
            try:
                d.rmdir()
            except Exception:
                pass
        h._json({"ok": True, "purged_roles": purged})
        return True

    return False
