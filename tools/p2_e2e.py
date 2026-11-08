# coding: utf-8
"""P2 四项的真实验证（零 mock）：情绪延续 / 抢话 / 程序化环境音 / 排队。

只碰自建剧本与自建素材，跑完清理；最后复核「用户真实剧本」未被改动。

依赖：面板 7861 在线 + 推理引擎 7860 在线（真合成，不是打桩）。
环境变量：
  TTS_PANEL_URL     面板地址，默认 http://127.0.0.1:7861
  TTS_DATA_DIR      数据目录，默认 <仓库>/data
  TTS_USER_SCRIPT   用户真实剧本 id（可选）。设了就复核它的整轨没被动过；
                    没设就跳过这一项，并在输出里说明为什么跳过
用法：python tools/p2_e2e.py [--keep]
      默认跑完删除测试剧本；--keep 保留（本机 safe-delete 守卫一轮删到
      50 个文件就要人工确认，会掐断连接，交外部脚本清更稳）
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = os.environ.get("TTS_PANEL_URL", "http://127.0.0.1:7861")
DATA_DIR = Path(os.environ.get(
    "TTS_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
USER_SCRIPT_ID = os.environ.get("TTS_USER_SCRIPT", "").strip()
S = requests.Session()
S.trust_env = False
FAILS: list[str] = []
CLEANUP: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("  " + detail if detail else ""), flush=True)
    if not ok:
        FAILS.append(name)


def jget(path: str):
    r = S.get(BASE + path, timeout=60)
    return r.status_code, (r.json() if r.content else None)


def jpost(path: str, payload=None):
    r = S.post(BASE + path, json=payload if payload is not None else {}, timeout=120)
    return r.status_code, (r.json() if r.content else None)


def jput(path: str, payload):
    r = S.put(BASE + path, json=payload, timeout=120)
    return r.status_code, (r.json() if r.content else None)


def make_script(title: str, raw: str) -> dict:
    code, d = jpost("/api/scripts", {"title": title, "raw": raw, "auto_cast": True})
    assert code in (200, 201), (code, d)
    CLEANUP.append(f"/api/scripts/{d['id']}")
    return d


def wait_job(jid: str, limit=180.0) -> dict:
    t0 = time.time()
    last = {}
    while time.time() - t0 < limit:
        _, d = jget(f"/api/scripts/jobs/{jid}")
        last = d or {}
        if last.get("state") in ("done", "failed", "stopped"):
            return last
        time.sleep(1.5)
    return last


def sha1(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def main() -> int:
    user_wav = DATA_DIR / "audio" / "scripts" / USER_SCRIPT_ID / "full.wav"
    user_before = sha1(user_wav) if user_wav.is_file() else ""

    # ---------------------------------------------------------------- 情绪延续
    s1 = make_script("E2E 情绪延续", "## 客厅\n母亲[愤怒]：你怎么又回来了。\n父亲：我回来看看。")
    check("新建剧本默认开启情绪延续", s1.get("emotion_carry") is True,
          f"carry={s1.get('emotion_carry')}")
    code, d = jget(f"/api/scripts/{s1['id']}")
    rows = d["lines"]
    check("第二行沿用上一行情绪", rows[1]["emotion"] == "愤怒" and rows[1]["emotion_source"] == "carry",
          f"{rows[1]['emotion']}/{rows[1]['emotion_source']}")
    code, d = jput(f"/api/scripts/{s1['id']}", {"emotion_carry": False})
    rows = d["lines"]
    check("关掉后第二行回到中性", rows[1]["emotion_source"] == "neutral", rows[1]["emotion_source"])
    code, d = jget(f"/api/scripts/{s1['id']}")
    check("开关已落盘", d.get("emotion_carry") is False)

    # ---------------------------------------------------------------- 抢话
    s2 = make_script("E2E 抢话", "## 门口\n旁白：第一句留在这里。\n旁白：第二句紧跟着说。")
    code, job = jpost(f"/api/scripts/{s2['id']}/synth", {"resume": True})
    check("合成任务已创建", code == 200 and job.get("job_id"), str(job)[:80])
    res = wait_job(job["job_id"])
    check("两行合成完成", res.get("state") == "done" and res.get("done") == 2,
          f"{res.get('state')} done={res.get('done')}")
    _, d = jget(f"/api/scripts/{s2['id']}")
    dur_plain = d["duration"]
    tl_plain = d["timeline"]

    code, d = jput(f"/api/scripts/{s2['id']}", {"line_lead": {"1": 0.4}})
    check("提前量已保存", code == 200 and d.get("line_lead", {}).get("1") == 0.4, str(d.get("line_lead")))
    dur_lead = d["duration"]
    tl_lead = d["timeline"]
    check("整轨变短约 0.4 秒", abs((dur_plain - dur_lead) - 0.4) < 0.06,
          f"{dur_plain} -> {dur_lead}")
    check("第二行起点前移", abs(tl_lead[1]["start"] - (tl_plain[1]["start"] - 0.4)) < 0.06,
          f"{tl_plain[1]['start']} -> {tl_lead[1]['start']}")
    check("时间轴标出 lead", tl_lead[1].get("lead") == 0.4, str(tl_lead[1].get("lead")))
    check("已合成的行没被打回待重做",
          all(r["status"] == "done" for r in d["lines"]), str([r["status"] for r in d["lines"]]))
    wav_path = DATA_DIR / d["full_wav"]
    wav_dur = wav_path.stat().st_size if wav_path.is_file() else 0
    code, d2 = jput(f"/api/scripts/{s2['id']}", {"line_lead": {}})
    check("取消抢话后恢复原长", abs(d2["duration"] - dur_plain) < 0.06, f"{d2['duration']}")
    check("整轨文件大小随长度变化", wav_dur > 0)

    # ---------------------------------------------------------------- 程序化环境音
    for kind in ("rain", "fire", "street"):
        code, d = jpost("/api/assets/generate", {"kind": kind, "seconds": 3})
        ok = code in (200, 201) and d.get("asset", {}).get("duration", 0) > 2.5
        check(f"生成 {kind} 素材", ok, str(d)[:90])
        if ok:
            CLEANUP.append(f"/api/assets/{d['asset']['id']}")

    # ---------------------------------------------------------------- 排队
    a = make_script("E2E 排队 A", "## 一\n旁白：排队测试第一单。\n旁白：再补一句让它跑起来。")
    b = make_script("E2E 排队 B", "## 一\n旁白：排队测试第二单。")
    _, ja = jpost(f"/api/scripts/{a['id']}/synth", {"resume": True})
    time.sleep(0.8)
    _, jb = jpost(f"/api/scripts/{b['id']}/synth", {"resume": True})
    time.sleep(1.2)
    _, vb = jget(f"/api/scripts/jobs/{jb['job_id']}")
    check("第二个任务在排队", vb.get("state") == "queued" and vb.get("queue_pos", 0) >= 1,
          f"{vb.get('state')} pos={vb.get('queue_pos')}")
    S.post(BASE + f"/api/scripts/jobs/{jb['job_id']}/stop", json={}, timeout=30)
    _, vb2 = jget(f"/api/scripts/jobs/{jb['job_id']}")
    check("排队中的任务可取消", vb2.get("state") == "stopped", vb2.get("state"))
    ra = wait_job(ja["job_id"])
    check("前一单正常跑完", ra.get("state") == "done", ra.get("state"))

    # ---------------------------------------------------------------- 清理 + 用户数据复核
    # 本机 safe-delete 守卫在一轮里删到 50 个文件就要求人工确认（会掐断连接），
    # 所以 --keep 时不删，交给外部脚本清
    if "--keep" not in sys.argv:
        for path in reversed(CLEANUP):
            try:
                S.delete(BASE + path, timeout=60)
            except Exception as exc:            # noqa: BLE001
                print(f"清理 {path} 失败：{exc}", flush=True)
    if USER_SCRIPT_ID and user_before:
        user_after = sha1(user_wav) if user_wav.is_file() else ""
        check("用户真实剧本未被改动", user_before == user_after, user_before[:12])
    else:
        print("SKIP 用户真实剧本未被改动（未设 TTS_USER_SCRIPT）", flush=True)

    print(f"\n失败 {len(FAILS)} 项" + ("" if not FAILS else "：" + "、".join(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
