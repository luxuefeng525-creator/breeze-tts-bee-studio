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
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import panel as P
from tts_pipeline.script import (
    ARCHETYPES,
    ARCHETYPE_SPEED,
    EMOTIONS,
    MOODS,
    estimate_seconds,
    guess_archetype,
    parse_script,
)

MAX_LINES = 100          # 超过这个行数提示分批（8 GB 卡约 20-30 分钟）
MAX_TAKES = 8            # 单行最多留几版；超出后覆盖最旧一版（不删文件、不无限涨盘）
DEFAULT_GAP = 0.35
DEFAULT_CFG = 4.0
EXPORT_PARTS = ("full", "lines", "timeline", "script")
DEFAULT_PARTS = ",".join(EXPORT_PARTS)

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
# cast 要连写 voices.json / roles.json 两个文件：单次 RMW 各自原子，
# 但两次之间需要一把进程内总锁，否则两个并发请求会基于同一份旧快照
# 各建一遍角色，并在响应里谎报「建成功了」。
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
def _roles_by_name() -> dict[str, dict]:
    return {r["name"]: r for r in P._load("roles.json")}


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


def parse_for(script: dict):
    """按当前角色/音色重新解析剧本（角色改名或换音色后要重新算）。"""
    roles = _roles_by_name()
    voices = _voices_by_id()
    defaults = {name: _role_default_emotion(r) for name, r in roles.items()}
    instr = {name: _role_instruction(r, voices) for name, r in roles.items()}
    moods = {s["name"]: s.get("mood", "") for s in _norm_scenes(script.get("scenes"))}
    return parse_script(script.get("raw", ""), defaults, instr, moods)


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def plan_cast(errors: list[dict], role_names: set[str],
              voices: dict[str, dict]) -> list[dict]:
    """从解析错误里列出「还缺谁」，并给出每个角色该用什么声音。

    名字跟已有音色有 ≥2 字公共前缀时复用那个音色（作者写「小蜜蜂」，
    库里叫「小蜜蜂配音」，显然是想用同一个声音）；其余按名字猜原型
    新建一个设计型音色，避免五个角色全绑一个音色、听成同一个人。
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
        for v in voices.values():
            n = _common_prefix_len(name, str(v.get("name") or "").strip())
            if n >= 2 and n > best:
                hit, best = v, n
        out.append({
            "name": name,
            "archetype": arch,
            "label": (ARCHETYPES.get(arch) or {}).get("label", "自定义"),
            "instruction": (ARCHETYPES.get(arch) or {}).get("instruction", ""),
            "speed": ARCHETYPE_SPEED.get(arch, 1.0),
            "voice": hit,
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
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            mood = str(item.get("mood") or "").strip()
        else:
            continue
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "mood": mood})
    return out


def _scenes_view(names: list[str], configured) -> list[dict]:
    """以解析出的场次为准，氛围从已存配置里取（作者可能刚加了新场次）。"""
    known = {s["name"]: s.get("mood", "") for s in _norm_scenes(configured)}
    return [{"name": n, "mood": known.get(n, "")} for n in names]


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


def _enriched(script: dict, parsed) -> list[dict]:
    """把解析结果补上 role_id/voice_id 与完成状态。

    完成状态必须同时满足「状态为 done」和「内容哈希一致」：只按行号判断时，
    改了台词但行数不变，会把上一版音频当成新台词的成品，用户拿到文不对音
    的文件却看不到任何异常。
    """
    roles = _roles_by_name()
    stored = {l["idx"]: l for l in script.get("lines") or [] if "idx" in l}
    out = []
    for line in parsed.lines:
        row = line.as_dict()
        role = roles.get(line.role_name)
        row["role_id"] = role["id"] if role else None
        row["voice_id"] = (role or {}).get("voice_id") or None
        row["speed"] = _role_speed(role)
        row["hash"] = _line_hash(row)
        rec = stored.get(line.idx) or {}
        fresh = rec.get("status") == "done" and rec.get("hash") == row["hash"]
        row["status"] = "done" if fresh else "pending"
        row["wav"] = rec.get("wav", "") if fresh else ""
        row["duration"] = rec.get("duration", 0.0) if fresh else 0.0
        row.update(_take_info(rec, fresh))
        out.append(row)
    return out


def _synth_line(text: str, role: dict | None, instruction: str,
                cfg: float, seed: int, out_path: Path,
                speed: float = 1.0) -> tuple[float, str]:
    """合成一行到 out_path，返回 (时长秒, 错误串)。"""
    voices = _voices_by_id()
    fields: dict[str, str] = {"text": text, "seed": str(seed)}
    files: dict[str, tuple[str, bytes]] = {}

    voice = voices.get((role or {}).get("voice_id") or "")
    if voice:
        ref = (voice.get("ref_audio") or "").strip()
        txt = (voice.get("ref_text") or "").strip()
        # 设计型音色本来就没有参考音频，只有「有音频却缺文稿」才是配置错误
        if ref and not txt:
            return 0.0, f"音色「{voice.get('name', '')}」缺文字稿（参考音频与文稿必须成对）"
        if ref:
            rp = P.DATA / ref
            if not rp.is_file():
                return 0.0, f"音色「{voice.get('name', '')}」的参考音频文件缺失"
            files["ref_audio"] = (rp.name, rp.read_bytes())
            fields["ref_text"] = txt

    if instruction.strip():
        fields["instruction"] = instruction.strip()
        fields["cfg_scale"] = str(cfg)
    else:
        # 无指令模板没有 negative prompt，cfg 必须=1，否则上游直接 500
        fields["cfg_scale"] = "1"

    body, ctype = P._remultipart(fields, files)
    code, raw = P._post_with_retry("/v1/audio/speech", body, ctype)
    if code != 200:
        try:
            detail = json.loads(raw).get("detail")
        except Exception:
            detail = raw[:160].decode("utf-8", "replace")
        return 0.0, str(detail)[:200]

    pcm = P.resample_pcm(raw, speed) if abs(speed - 1.0) > 1e-6 else raw
    wav = P.pcm_to_wav(pcm)
    out_path.write_bytes(wav)
    return len(pcm) / (P.SAMPLE_RATE * 2), ""


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
    return {
        "id": job["id"],
        "script_id": job["script_id"],
        "state": job["state"],
        "total": job["total"],
        "done": job["done"],
        "failed": job["failed"],
        "current": job["current"],
        "elapsed": round(elapsed, 1),
        "per_line": round(per_line, 1),
        "eta": round(per_line * remaining, 1) if per_line else None,
        "rows": {k: dict(v) for k, v in job["rows"].items()},
    }


def _prune_jobs() -> None:
    """清掉结束超过 10 分钟的任务。_JOBS 只增不删会随使用时间单调涨内存。"""
    cutoff = time.time() - 600
    with _JOBS_LOCK:
        for jid in [k for k, v in _JOBS.items()
                    if v["state"] != "running"
                    and v.get("finished_at", 0) < cutoff]:
            _JOBS.pop(jid, None)


def _run_job(job: dict, script_id: str, rows: list[dict], gap: float) -> None:
    try:
        script = _find(script_id) or {}
        out_dir = _lines_dir(script_id)
        roles = _roles_by_name()
        cfg = float(script.get("cfg_scale") if script.get("cfg_scale") is not None
                    else DEFAULT_CFG)
        seed = int(script.get("seed") if script.get("seed") is not None else 42)

        for row in rows:
            if job["stop"]:
                job["state"] = "stopped"
                return
            idx = row["idx"]
            job["current"] = idx
            job["rows"][idx]["state"] = "running"

            role = roles.get(row["role_name"])
            if role is None:
                # 未知角色在启动前已被拦下，这里是兜底：按行失败，不影响其他行
                job["rows"][idx].update(state="failed", error="未知角色")
                job["failed"] += 1
                continue

            name = f"line_{idx:03d}.wav"
            t_line = time.time()
            try:
                dur, err = _synth_line(row["text"], role, row.get("instruction", ""),
                                       cfg, seed, out_dir / name,
                                       float(row.get("speed") or 1.0))
            except Exception as exc:          # 单行异常不该终止整部剧
                dur, err = 0.0, f"{type(exc).__name__}: {exc}"[:200]

            job.setdefault("durs", []).append(time.time() - t_line)
            if err:
                job["rows"][idx].update(state="failed", error=err)
                job["failed"] += 1
            else:
                rel = f"audio/scripts/{script_id}/{name}"
                job["rows"][idx].update(state="done", wav=rel,
                                        duration=round(dur, 3), error="")
                job["done"] += 1
                # 整剧合成落的是「新内容的第 1 版」：顺手清掉旧内容的版本，
                # 不然换完台词还能切回上一版的音频，文不对音
                _persist_take(script_id, idx, row, 1, dur, rel, reset=True)

        job["state"] = "done"
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

    pcm = bytearray()
    timeline: list[dict] = []
    cursor = 0.0
    last_done = -1
    for row in rows:
        if row["status"] != "done" or not row["wav"]:
            continue
        # 走记录里的实际路径：第 2 版以后的文件名带 _t{n}，按行号拼名字
        # 会拼到第 1 版，用户切了版本却听不出区别
        path = P.DATA / row["wav"]
        if not path.is_file():
            continue
        if last_done >= 0 and gap > 0:
            pcm += _silence(gap)
            cursor += gap
        chunk = _read_pcm(path)
        start = cursor
        pcm += chunk
        dur = len(chunk) / (P.SAMPLE_RATE * 2)
        cursor += dur
        last_done = row["idx"]
        timeline.append({
            "idx": row["idx"] + 1,
            "role": row["role_name"],
            "emotion": row["emotion"],
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
    if ("full" in parts or "timeline" in parts) and pending:
        return done, (f"还有 {pending} 行没合成，或台词改过还没重做；"
                      "整轨与时间轴都是上一版的，先合成再导出")
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
            w.writerow(["序号", "角色", "情绪", "起", "止", "台词"])
            for t in script.get("timeline") or []:
                w.writerow([t["idx"], t["role"], t["emotion"], t["start"], t["end"], t["text"]])
            zf.writestr("timeline.csv", "\ufeff" + csv_buf.getvalue())
        if "script" in parts:
            zf.writestr("script.txt", script.get("raw", ""))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
def handle(h, method: str, path: str, body: bytes, ctype: str) -> bool:
    """处理剧本相关请求；不是本模块的路由时返回 False。"""
    if not path.startswith("/api/scripts") and path != "/api/archetypes":
        return False

    # ---- 元数据 ----
    if method == "GET" and path == "/api/archetypes":
        h._json({
            "archetypes": [{"key": k, **v} for k, v in ARCHETYPES.items()],
            "emotions": list(EMOTIONS.keys()),
            "moods": [{"key": k, "instruction": v} for k, v in MOODS.items() if k],
            "max_lines": MAX_LINES,
            "default_gap": DEFAULT_GAP,
            "default_parts": list(EXPORT_PARTS),
        })
        return True

    # ---- 任务状态 ----
    if method == "GET" and path.startswith("/api/scripts/jobs/"):
        job_id = path.rsplit("/", 1)[-1]
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if not job:
            h._err("任务不存在或已结束", 404)
            return True
        h._json(_job_view(job))
        return True

    if method == "POST" and path.startswith("/api/scripts/jobs/") and path.endswith("/stop"):
        job_id = path.split("/")[4]
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if not job:
            h._err("任务不存在或已结束", 404)
            return True
        job["stop"] = True
        h._json({"ok": True})
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
        parsed = parse_for(script)
        h._json({
            **{k: script.get(k) for k in
               ("id", "title", "raw", "gap", "duration", "full_wav",
                "timeline", "cfg_scale", "seed", "updated_at")},
            # scenes 以解析结果为准（作者可能刚加了 ## 场次），mood 从已存配置里取
            "scenes": _scenes_view(parsed.scenes, script.get("scenes")),
            "lines": _enriched(script, parsed),
            "errors": parsed.errors,
            "estimate": round(estimate_seconds(parsed), 1),
        })
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
            "max_lines": MAX_LINES,
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
        if len(rows) > MAX_LINES:
            h._err(f"一次最多合成 {MAX_LINES} 行（当前 {len(rows)} 行），请分批")
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
        with _JOBS_LOCK:
            _JOBS[job["id"]] = job
        threading.Thread(target=_run_job,
                         args=(job, sid, todo, float(script.get("gap") if script.get("gap") is not None else DEFAULT_GAP)),
                         daemon=True).start()
        h._json({"job_id": job["id"], "total": len(todo),
                 "skipped": len(rows) - len(todo)})
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
        role = _roles_by_name().get(row["role_name"])
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
        dur, err = _synth_line(row["text"], role, row.get("instruction", ""),
                               float(script.get("cfg_scale") if script.get("cfg_scale") is not None else DEFAULT_CFG),
                               int(script.get("seed") if script.get("seed") is not None else 42),
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
                 "duration": round(dur, 3), "wav": rel})
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

    # 一键补齐角色：剧本写完满屏「未知角色」时，不该让用户跑去音色库
    # 手工建 N 次。按角色名自动配原型与设计型音色，一次落库。
    if method == "POST" and path.count("/") == 4 and path.endswith("/cast"):
        sid = path.split("/")[3]
        script = _find(sid)
        if not script:
            h._err("剧本不存在", 404)
            return True
        try:
            d = json.loads(body or b"{}")
        except Exception:
            d = {}
        forced = str(d.get("voice_id") or "").strip()
        with _CAST_LOCK:
            # 锁内重读：两个并发请求不能共用同一份旧快照
            script = _find(sid) or script
            parsed = parse_for(script)
            if len(parsed.lines) > MAX_LINES:
                h._err(f"一次最多 {MAX_LINES} 行")
                return True
            if forced and forced not in _voices_by_id():
                h._err("指定的音色不存在")
                return True
            plan = plan_cast(parsed.errors, set(_roles_by_name()), _voices_by_id())
            if not plan:
                h._json({"created": [], "count": 0,
                         "remaining": len(parsed.errors)})
                return True

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
                    "created_at": now,
                }
                new_roles.append(role)
                created.append({
                    "name": item["name"], "archetype": item["archetype"],
                    "label": item["label"], "role_id": role["id"],
                    "voice_id": voice["id"], "voice_name": voice["name"],
                    "reused": reused, "speed": item["speed"],
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
                names = {str(r.get("name") or "").strip() for r in items}
                for r in _new:
                    key = str(r["name"]).strip()
                    if key not in names:
                        items.append(r)
                        names.add(key)

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
                h._err(f"音色没写成功：{', '.join(dangling[:3])}，请重试")
                return True
            P._update_json("roles.json", _add_roles)

            # 以落盘结果为准回报，不能把「被查重跳过」的对象算成新建
            real_roles = {str(r.get("name") or "").strip(): r
                          for r in P._load("roles.json")}
            real_voices = {v["id"]: v for v in P._load("voices.json")}
            verified = []
            for c in created:
                r = real_roles.get(c["name"])
                if not r or r.get("voice_id") not in real_voices:
                    continue
                c["role_id"] = r["id"]
                c["voice_id"] = r["voice_id"]
                c["voice_name"] = real_voices[r["voice_id"]]["name"]
                verified.append(c)
            remaining = len(parse_for(_find(sid) or script).errors)
            h._json({"created": verified, "count": len(verified),
                     "remaining": remaining}, 201)
        return True

    # ---- 增删改 ----
    if method == "POST" and path == "/api/scripts":
        try:
            d = json.loads(body or b"{}")
        except Exception:
            h._err("请求体必须是 JSON")
            return True
        raw = str(d.get("raw") or "")
        if len(parse_script(raw).lines) > MAX_LINES:
            h._err(f"一次最多 {MAX_LINES} 行")
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
            "lines": [],
            "created_at": now,
            "updated_at": now,
        }
        P._update_json("scripts.json", lambda it: it.append(script))
        h._json(script, 201)
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
            if n > MAX_LINES:
                h._err(f"一次最多 {MAX_LINES} 行（当前 {n} 行），请分批")
                return True
        if "title" in d:
            script["title"] = str(d["title"] or "未命名剧本")
        if "raw" in d:
            script["raw"] = str(d["raw"] or "")   # 存成字符串：dict 会让解析时炸
        if "scenes" in d:
            # 合并而不是整体替换：作者临时删掉或改名一行 `## 场次` 时，
            # 那个场次配好的氛围不该在普通保存里被永久丢掉（改回来还能找回）。
            merged = {s["name"]: s.get("mood", "") for s in _norm_scenes(script.get("scenes"))}
            for s in _norm_scenes(d["scenes"]):
                merged[s["name"]] = s.get("mood", "")
            script["scenes"] = [{"name": n, "mood": m} for n, m in merged.items()]
        for k, cast in (("gap", float), ("cfg_scale", float), ("seed", int)):
            if k in d:
                script[k] = cast(d[k])
        patch = {k: script[k] for k in
                 ("title", "raw", "scenes", "gap", "cfg_scale", "seed") if k in script}
        patch["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        def _mut(items):
            for s in items:
                if s["id"] == sid:
                    s.update(patch)

        P._update_json("scripts.json", _mut)
        fresh = _find(sid) or script
        parsed = parse_for(fresh)
        h._json({**fresh, "lines": _enriched(fresh, parsed), "errors": parsed.errors})
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
        h._json({"ok": True})
        return True

    return False
