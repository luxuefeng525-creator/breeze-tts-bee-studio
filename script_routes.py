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

import panel as P
from tts_pipeline.script import (
    ARCHETYPES,
    EMOTIONS,
    estimate_seconds,
    parse_script,
)

MAX_LINES = 100          # 超过这个行数提示分批（8 GB 卡约 20-30 分钟）
DEFAULT_GAP = 0.35
DEFAULT_CFG = 4.0

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


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
    """角色要下发的音色描述：音色自带的指令优先，其次原型预填的描述。"""
    if not role:
        return ""
    voice = voices.get(role.get("voice_id") or "")
    if voice and (voice.get("instruction") or "").strip():
        return voice["instruction"].strip()
    arch = ARCHETYPES.get(role.get("archetype") or "custom") or {}
    return (arch.get("instruction") or "").strip()


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
    moods = {s.get("name", ""): s.get("mood", "") for s in script.get("scenes") or []}
    return parse_script(script.get("raw", ""), defaults, instr, moods)


def _line_hash(row: dict) -> str:
    """一行音频的身份：文本、角色、音色、指令任何一项变了就不该复用旧音频。"""
    raw = "|".join(str(row.get(k) or "") for k in
                   ("text", "role_id", "voice_id", "instruction"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


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
        row["hash"] = _line_hash(row)
        rec = stored.get(line.idx) or {}
        fresh = rec.get("status") == "done" and rec.get("hash") == row["hash"]
        row["status"] = "done" if fresh else "pending"
        row["wav"] = rec.get("wav", "") if fresh else ""
        row["duration"] = rec.get("duration", 0.0) if fresh else 0.0
        out.append(row)
    return out


def _synth_line(text: str, role: dict | None, instruction: str,
                cfg: float, seed: int, out_path: Path) -> tuple[float, str]:
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

    wav = P.pcm_to_wav(raw)
    out_path.write_bytes(wav)
    return len(raw) / (P.SAMPLE_RATE * 2), ""


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
                                       cfg, seed, out_dir / name)
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
                _persist_line(script_id, idx, {
                    "status": "done", "wav": rel, "duration": round(dur, 3),
                    "emotion": row.get("emotion"), "role_name": row["role_name"],
                    "text": row["text"], "hash": row.get("hash", ""),
                })

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
        path = out_dir / f"line_{row['idx']:03d}.wav"
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
def _export_zip(script: dict) -> bytes:
    buf = io.BytesIO()
    out_dir = _lines_dir(script["id"])
    parsed = parse_for(script)
    rows = _enriched(script, parsed)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        full = out_dir / "full.wav"
        if full.is_file():
            zf.write(full, "full.wav")
        for row in rows:
            p = out_dir / f"line_{row['idx']:03d}.wav"
            if p.is_file():
                # 序号在前保证唯一：两个角色清洗后可能同名，重名条目
                # 在部分解压器里会互相覆盖
                safe = P._safe_name(row["role_name"])[:24] or "line"
                zf.write(p, f"lines/{row['idx'] + 1:02d}_{safe}.wav")
        csv_buf = io.StringIO()
        w = csv.writer(csv_buf)
        w.writerow(["序号", "角色", "情绪", "起", "止", "台词"])
        for t in script.get("timeline") or []:
            w.writerow([t["idx"], t["role"], t["emotion"], t["start"], t["end"], t["text"]])
        zf.writestr("timeline.csv", "\ufeff" + csv_buf.getvalue())
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
            "max_lines": MAX_LINES,
            "default_gap": DEFAULT_GAP,
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
        data = _export_zip(script)
        h.send_response(200)
        h.send_header("Content-Type", "application/zip")
        h.send_header("Content-Disposition",
                      f'attachment; filename="script_{sid}.zip"')
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
        moods = {s.get("name", ""): s.get("mood", "") for s in script.get("scenes") or []}
        h._json({
            **{k: script.get(k) for k in
               ("id", "title", "raw", "gap", "duration", "full_wav",
                "timeline", "cfg_scale", "seed", "updated_at")},
            # scenes 以解析结果为准（作者可能刚加了 ## 场次），mood 从已存配置里取
            "scenes": [{"name": n, "mood": moods.get(n, "")} for n in parsed.scenes],
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
                 "scenes": d.get("scenes", script.get("scenes") or [])}
        parsed = parse_for(probe)
        h._json({
            "lines": _enriched(probe, parsed),
            "errors": parsed.errors,
            "scenes": parsed.scenes,
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
        name = f"line_{idx:03d}.wav"
        dur, err = _synth_line(row["text"], role, row.get("instruction", ""),
                               float(script.get("cfg_scale") if script.get("cfg_scale") is not None else DEFAULT_CFG),
                               int(script.get("seed") if script.get("seed") is not None else 42), out_dir / name)
        if err:
            h._err(err)
            return True
        rel = f"audio/scripts/{sid}/{name}"
        _persist_line(sid, idx, {"status": "done", "wav": rel,
                                 "duration": round(dur, 3),
                                 "emotion": row.get("emotion"),
                                 "role_name": row["role_name"], "text": row["text"]})
        _concat(sid, float(script.get("gap") if script.get("gap") is not None else DEFAULT_GAP))
        h._json({"ok": True, "idx": idx, "duration": round(dur, 3), "wav": rel})
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
            "scenes": d.get("scenes") or [],
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
            script["scenes"] = d["scenes"] if isinstance(d["scenes"], list) else []
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
