# coding: utf-8
"""剧本台「一键出片」真实链路验证（7861 panel → 7860 推理 API，零 mock）。

验证口径（对应 PLAN-2026-11-07-soundtrack §5）：
  1. 素材：生成噪声底成功；上传未勾授权 → 400
  2. 场次铺底写入后「一键出片」：合成 → 拼接 → 混音，任务终态无 concat_error / mix_error
  3. mix.wav 与 full.wav 时长一致（≤0.05s）
  4. 只重算混音时，干声 full.wav 字节完全不变
  5. 导出 ZIP 内含 mix.wav，包内 full.wav 与磁盘文件字节一致
  6. 改铺底音量后旧 mix 被作废（mix_stale），重算后恢复

用法：python tools/soundtrack_e2e.py [--keep]
      默认跑完删除测试剧本，--keep 保留以便人工听。
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import uuid
import wave
import zipfile

import requests

PANEL = "http://127.0.0.1:7861"
S = requests.Session()
S.trust_env = False          # 别让系统代理劫持 localhost
FAILS: list[str] = []
CREATED_ASSETS: list[str] = []   # 跑完自清理，别让测试素材在库里越堆越多

RAW = """## 雨夜
旁白：雨下得很大，路灯把水洼照成一片碎金。
## 清晨
旁白：天亮了，街角的面摊已经支起来。
"""


def ok(name: str, detail: str = "") -> None:
    print("PASS %-42s %s" % (name, detail), flush=True)


def bad(name: str, detail: str = "") -> None:
    FAILS.append(name)
    print("FAIL %-42s %s" % (name, detail), flush=True)


def check(name: str, cond: bool, detail: str = "") -> bool:
    ok(name, detail) if cond else bad(name, detail)
    return cond


def jpost(path: str, payload: dict | None = None, timeout: int = 60):
    r = S.post(PANEL + path, json=payload or {}, timeout=timeout)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:200]}


def jget(path: str, timeout: int = 60):
    r = S.get(PANEL + path, timeout=timeout)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:200]}


def wav_seconds(raw: bytes) -> float:
    with wave.open(io.BytesIO(raw), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def wait_job(job_id: str, timeout: int = 900) -> dict:
    """轮询任务到终态。合成是真实推理，按行数给足时间。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        code, d = jget("/api/scripts/jobs/" + job_id)
        if code != 200:
            raise RuntimeError(f"任务查询失败 {code}: {d}")
        if d.get("state") != "running":
            return d
        cur = d.get("current")
        rows = d.get("rows") or {}
        label = f"{d.get('done')}/{d.get('total')}"
        if isinstance(cur, int) and str(cur) in rows:
            label += " " + str(rows[str(cur)].get("role_name") or "")
        if label != last:
            print("  ... %-16s %s" % (label, d.get("state")), flush=True)
            last = label
        time.sleep(2)
    raise RuntimeError("任务超时未结束")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="跑完保留测试剧本")
    args = ap.parse_args()

    code, meta = jget("/api/archetypes")
    if code != 200:
        print("panel 不可用：", code, meta, flush=True)
        return 2
    print("panel /api/archetypes OK · max_lines=%s" % meta.get("max_lines"), flush=True)

    # ---- 1. 素材库 -------------------------------------------------------
    code, d = jpost("/api/assets/generate",
                    {"kind": "brown", "seconds": 4, "name": "E2E 雨夜底噪"})
    check("生成噪声底素材", code == 201 and d.get("asset", {}).get("id"),
          f"{code} {d.get('asset', {}).get('duration')}s")
    asset_id = (d.get("asset") or {}).get("id")
    if asset_id:
        CREATED_ASSETS.append(asset_id)

    # 未勾授权的上传必须被拒（用一段真 wav，避免「文件不合法」抢在前面）
    tone = _tone_wav(0.5)
    r = S.post(PANEL + "/api/assets",
               files={"file": ("e2e.wav", tone, "application/octet-stream")},
               data={"name": "未授权素材"}, timeout=60)
    check("上传未勾授权被拒", r.status_code == 400 and "授权" in r.text,
          f"{r.status_code} {r.text[:80]}")

    files = {"file": ("e2e.wav", tone, "application/octet-stream")}
    r = S.post(PANEL + "/api/assets", files=files,
               data={"name": "E2E 授权素材", "license_confirmed": "1"}, timeout=60)
    check("勾选授权后可上传", r.status_code == 201, str(r.status_code))
    if r.status_code == 201:
        CREATED_ASSETS.append(((r.json() or {}).get("asset") or {}).get("id"))

    # ---- 2. 建剧本 + 配场次铺底 -----------------------------------------
    code, script = jpost("/api/scripts",
                         {"title": "E2E 出片 " + uuid.uuid4().hex[:4], "raw": RAW,
                          "auto_cast": True})
    if code not in (200, 201):
        print("建剧本失败：", code, script, flush=True)
        return 2
    sid = script["id"]
    print("剧本 %s" % sid, flush=True)

    code, d = jget("/api/scripts/" + sid)
    lines = d.get("lines") or []
    check("剧本解析出 2 行", len(lines) == 2, str(len(lines)))
    scenes = [s["name"] for s in (d.get("scenes") or [])]
    check("剧本解析出 2 个场次", len(scenes) == 2, str(scenes))

    r = S.put(PANEL + f"/api/scripts/{sid}", json={"scenes": [
        {"name": "雨夜", "ambience": asset_id, "bgm": "", "gain": 0.5},
        {"name": "清晨", "ambience": asset_id, "bgm": "", "gain": 0.3},
    ]}, timeout=60)
    view = {s["name"]: s for s in (r.json().get("scenes") or [])}
    check("场次铺底写入并回读", r.status_code == 200
          and view.get("雨夜", {}).get("ambience") == asset_id
          and abs(float(view.get("雨夜", {}).get("gain", 0)) - 0.5) < 1e-6,
          json.dumps(view, ensure_ascii=False)[:120])

    # ---- 3. 一键出片（真实合成 → 拼接 → 混音）---------------------------
    t0 = time.time()
    code, d = jpost(f"/api/scripts/{sid}/produce", {})
    if code != 200:
        bad("一键出片请求", f"{code} {d}")
        _cleanup(sid, args)
        return 1
    job = wait_job(d["job_id"])
    took = time.time() - t0
    check("出片任务正常结束", job.get("state") == "done" and not job.get("failed"),
          f"state={job.get('state')} done={job.get('done')} failed={job.get('failed')} {took:.0f}s")
    check("出片无拼接错误", not job.get("concat_error"), str(job.get("concat_error")))
    check("出片无混音错误", not job.get("mix_error"), str(job.get("mix_error")))

    code, d = jget("/api/scripts/" + sid)
    full_rel, mix_rel = d.get("full_wav") or "", d.get("mix_wav") or ""
    check("出片后有整轨", bool(full_rel), full_rel)
    check("出片后有混音", bool(mix_rel), mix_rel)
    if not full_rel or not mix_rel:
        _cleanup(sid, args)
        return 1

    full_url = _audio_url(full_rel)
    mix_url = _audio_url(mix_rel)
    full1 = S.get(PANEL + full_url, timeout=60).content
    mix1 = S.get(PANEL + mix_url, timeout=60).content
    full_sec, mix_sec = wav_seconds(full1), wav_seconds(mix1)
    check("mix 与 full 时长一致（≤0.05s）", abs(full_sec - mix_sec) <= 0.05,
          f"full={full_sec:.3f}s mix={mix_sec:.3f}s")
    check("整轨时长与剧本 duration 一致",
          abs(float(d.get("duration") or 0) - full_sec) <= 0.1,
          f"duration={d.get('duration')} full={full_sec:.3f}")

    # ---- 4. 只重算混音：干声字节不能变 ---------------------------------
    code, d2 = jpost(f"/api/scripts/{sid}/mix", {})
    check("重算混音返回 mix.wav", code == 200 and (d2.get("mix_wav") or "").endswith("mix.wav"),
          f"{code} {d2}")
    full2 = S.get(PANEL + full_url, timeout=60).content
    check("重混后干声整轨字节不变", full2 == full1,
          f"{len(full1)}B -> {len(full2)}B")
    mix2 = S.get(PANEL + mix_url, timeout=60).content
    check("重混后 mix 仍与干声等长",
          abs(wav_seconds(mix2) - wav_seconds(full2)) <= 0.05,
          f"{wav_seconds(mix2):.3f}s")

    # ---- 5. 导出 ZIP ----------------------------------------------------
    parts = "full,mix,timeline,script,subtitles"
    r = S.get(PANEL + f"/api/scripts/{sid}/export?parts={parts}", timeout=120)
    if r.status_code != 200:
        bad("导出 ZIP", f"{r.status_code} {r.text[:120]}")
    else:
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = z.namelist()
        check("ZIP 内含 mix.wav", "mix.wav" in names, ",".join(names))
        check("ZIP 内含 full.wav", "full.wav" in names, ",".join(names))
        zfull = z.read("full.wav")
        zmix = z.read("mix.wav") if "mix.wav" in names else b""
        check("包内 full.wav 与磁盘一致", zfull == full2, f"{len(zfull)}B")
        check("包内 mix 与 full 等长",
              bool(zmix) and abs(wav_seconds(zmix) - wav_seconds(zfull)) <= 0.05,
              f"mix={wav_seconds(zmix):.3f}s full={wav_seconds(zfull):.3f}s" if zmix else "无 mix")
        check("字幕与时间轴也在包内",
              "timeline.csv" in names and "subtitles.srt" in names, ",".join(names))

    # ---- 6. 改铺底 → 旧混音作废 → 重算恢复 ------------------------------
    r = S.put(PANEL + f"/api/scripts/{sid}", json={"scenes": [
        {"name": "雨夜", "ambience": asset_id, "gain": 0.9},
        {"name": "清晨", "ambience": asset_id, "gain": 0.9},
    ]}, timeout=60)
    body = r.json()
    check("改音量后旧混音被作废",
          r.status_code == 200 and not (body.get("mix_wav") or ""),
          f"mix_wav={body.get('mix_wav')!r} stale={body.get('mix_stale')}")
    code, d3 = jpost(f"/api/scripts/{sid}/mix", {})
    check("重算后混音恢复", code == 200 and (d3.get("mix_wav") or "").endswith("mix.wav"),
          f"{code} {d3}")
    full3 = S.get(PANEL + full_url, timeout=60).content
    check("改铺底后干声仍不变", full3 == full1, f"{len(full1)}B -> {len(full3)}B")

    # ---- 7. 清空铺底：旧混音必须作废，不能把过期成片交付 ------------------
    r = S.put(PANEL + f"/api/scripts/{sid}", json={"scenes": [
        {"name": "雨夜", "ambience": "", "bgm": "", "gain": 0.5},
        {"name": "清晨", "ambience": "", "bgm": "", "gain": 0.5},
    ]}, timeout=60)
    body = r.json()
    check("清空铺底后旧混音作废",
          r.status_code == 200 and not (body.get("mix_wav") or ""),
          f"mix_wav={body.get('mix_wav')!r}")
    code, d4 = jpost(f"/api/scripts/{sid}/mix", {})
    check("无铺底时不产出假混音",
          code == 200 and not (d4.get("mix_wav") or ""), f"{code} {d4}")
    r = S.get(PANEL + f"/api/scripts/{sid}/export?parts=mix", timeout=60)
    check("无混音时导出 mix 被拒", r.status_code == 400, str(r.status_code))

    # 再走一次出片（total=0 → 拼接 + 混音）：_concat 必须把空结果写回，
    # 否则「清空铺底 + 出片」仍会把上一版混音留在磁盘上
    code, d5 = jpost(f"/api/scripts/{sid}/produce", {})
    if code == 200:
        job2 = wait_job(d5["job_id"])
        code, s5 = jget("/api/scripts/" + sid)
        check("出片后无铺底 → 不产出混音",
              job2.get("state") == "done" and not (s5.get("mix_wav") or ""),
              f"state={job2.get('state')} mix_wav={s5.get('mix_wav')!r}")
        full4 = S.get(PANEL + full_url, timeout=60).content
        check("再次出片后干声仍不变", full4 == full1, f"{len(full1)}B -> {len(full4)}B")
    else:
        bad("无铺底时出片", f"{code} {d5}")

    _cleanup(sid, args)
    print("", flush=True)
    if FAILS:
        print("FAILED %d 项：%s" % (len(FAILS), "；".join(FAILS)), flush=True)
        return 1
    print("ALL PASS", flush=True)
    return 0


def _audio_url(rel: str) -> str:
    """后端返回 audio/scripts/<id>/x.wav，面板音频路由挂在 /api/audio/。"""
    return "/api/audio/" + rel[len("audio/"):] if rel.startswith("audio/") else "/api/audio/" + rel


def _tone_wav(seconds: float = 1.0, freq: float = 220.0, rate: int = 24000,
              amp: int = 8000) -> bytes:
    import math
    import struct

    n = int(seconds * rate)
    pcm = b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate)))
                   for i in range(n))
    return (struct.pack("<4sI4s4sIHHIIHH4sI",
                        b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1, 1,
                        rate, rate * 2, 2, 16, b"data", len(pcm)) + pcm)


def _cleanup(sid: str, args) -> None:
    if args.keep:
        print("保留剧本 %s 与素材（--keep）" % sid, flush=True)
        return
    r = S.delete(PANEL + "/api/scripts/" + sid, timeout=60)
    print("清理剧本 %s → %s" % (sid, r.status_code), flush=True)
    for aid in CREATED_ASSETS:
        if not aid:
            continue
        code = S.delete(PANEL + "/api/assets/" + aid, timeout=60).status_code
        print("清理素材 %s → %s" % (aid, code), flush=True)


if __name__ == "__main__":
    sys.exit(main())
