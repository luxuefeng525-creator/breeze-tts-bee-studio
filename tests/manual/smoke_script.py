"""剧本台端到端冒烟：建角色 → 建剧本 → 解析 → 逐行合成 → 拼接 → 导出 → 清理。

用完即删，不往用户的角色库/剧本库留测试数据。
"""
import json
import time
import urllib.error
import urllib.request

B = "http://127.0.0.1:7861"


def req(path, method="GET", data=None, ctype=None, timeout=120):
    r = urllib.request.Request(B + path, data=data, method=method)
    if ctype:
        r.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Type", "").startswith("application/zip"):
                return resp.status, raw
            return resp.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body or b"{}")
        except Exception:
            return e.code, {"detail": body[:120].decode("utf-8", "replace")}


steps = []


def check(name, ok, detail=""):
    steps.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (("  " + str(detail)[:150]) if detail else ""))


voice_id = role_a = role_b = script_id = None
try:
    # ---- 1. 一个设计音色（旁白用），避免依赖已有克隆音色 ----
    code, v = req("/api/voices", "POST", json.dumps({
        "name": "冒烟-旁白音", "type": "design",
        "instruction": "沉稳的纪录片旁白，字正腔圆，语速均匀",
    }).encode(), "application/json")
    check("建原型音色", code in (200, 201), code)
    voice_id = v.get("id")

    # ---- 2. 两个角色：旁白（绑新音色）+ 老兵（绑已有克隆音色 小蜜蜂配音）----
    code, ra = req("/api/roles", "POST", json.dumps({
        "name": "冒烟旁白", "archetype": "narrator", "voice_id": voice_id,
        "default_emotion": "中性",
    }).encode(), "application/json")
    check("建角色A", code in (200, 201), code)
    role_a = ra.get("id")

    voices = req("/api/voices")[1]
    clone = next((x for x in voices if x.get("type") == "clone" and x.get("ref_audio")), None)
    if clone:
        code, rb = req("/api/roles", "POST", json.dumps({
            "name": "冒烟老兵", "archetype": "elder_m", "voice_id": clone["id"],
            "default_emotion": "疲惫",
        }).encode(), "application/json")
        check("建角色B（绑克隆音色）", code in (200, 201), code)
        role_b = rb.get("id")
    else:
        check("建角色B（绑克隆音色）", True, "跳过：没有可用的克隆音色")

    # ---- 3. 建剧本 ----
    raw = "\n".join([
        "## 深夜书店",
        "冒烟旁白：那天雨很大。",
        "冒烟旁白[迟疑]：门被推开了一条缝。",
        "冒烟老兵：还有书吗？",
    ])
    code, sc = req("/api/scripts", "POST", json.dumps({
        "title": "冒烟剧本", "raw": raw, "gap": 0.3,
    }).encode(), "application/json")
    check("建剧本", code in (200, 201), code)
    script_id = sc.get("id")

    # ---- 4. 解析 ----
    code, parsed = req(f"/api/scripts/{script_id}")
    lines = parsed.get("lines") or []
    check("解析出 3 行", len(lines) == 3, len(lines))
    check("场次识别", [x["name"] for x in (parsed.get("scenes") or [])] == ["深夜书店"], parsed.get("scenes"))
    check("显式情绪生效", lines[1]["emotion"] == "迟疑" and lines[1]["emotion_source"] == "explicit",
          (lines[1]["emotion"], lines[1]["emotion_source"]))
    check("角色默认情绪生效", lines[2]["emotion"] == "疲惫" and lines[2]["emotion_source"] == "role",
          (lines[2]["emotion"], lines[2]["emotion_source"]))
    check("指令含原型描述", "纪录片旁白" in lines[0]["instruction"], lines[0]["instruction"][:40])
    check("无解析错误", not parsed.get("errors"), parsed.get("errors"))

    # ---- 5. 逐行合成 ----
    code, job = req(f"/api/scripts/{script_id}/synth", "POST", b"{}", "application/json")
    check("启动合成任务", code == 200 and job.get("job_id"), (code, job))
    jid = job.get("job_id")
    deadline = time.time() + 300
    last = {}
    while time.time() < deadline:
        code, last = req(f"/api/scripts/jobs/{jid}")
        if last.get("state") in ("done", "stopped"):
            break
        time.sleep(3)
    check("任务完成", last.get("state") == "done",
          (last.get("state"), last.get("done"), last.get("failed")))
    check("三行全部成功", last.get("done") == 3 and last.get("failed") == 0,
          (last.get("done"), last.get("failed")))

    # ---- 6. 整轨 + 时间轴 ----
    code, full = req(f"/api/scripts/{script_id}")
    check("整轨已生成", bool(full.get("full_wav")), full.get("full_wav"))
    tl = full.get("timeline") or []
    check("时间轴 3 条且递增", len(tl) == 3 and tl[0]["start"] == 0
          and all(tl[i]["end"] <= tl[i + 1]["start"] + 0.01 for i in range(len(tl) - 1)),
          tl)
    total = tl[-1]["end"] if tl else 0
    check("整轨时长与时间轴一致", abs((full.get("duration") or 0) - total) < 0.15,
          (full.get("duration"), total))

    # ---- 7. 单行重录 ----
    code, redo = req(f"/api/scripts/{script_id}/lines/0/redo", "POST", b"{}", "application/json")
    check("单行重录", code == 200 and redo.get("duration"), (code, redo))

    # ---- 8. 导出 ----
    code, blob = req(f"/api/scripts/{script_id}/export")
    check("导出 ZIP", code == 200 and blob[:2] == b"PK", (code, len(blob) if blob else 0))
    import io
    import zipfile
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    check("ZIP 含四类文件",
          any(n == "full.wav" for n in names) and any(n.startswith("lines/") for n in names)
          and "timeline.csv" in names and "script.txt" in names, names)

finally:
    if script_id:
        req(f"/api/scripts/{script_id}", "DELETE")
    for rid in (role_a, role_b):
        if rid:
            req(f"/api/roles/{rid}", "DELETE")
    if voice_id:
        req(f"/api/voices/{voice_id}", "DELETE")

print()
print("ALL_PASS" if all(s[1] for s in steps) else "HAS_FAILURE")
