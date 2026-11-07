"""剧本台接口的集成测试（真实服务，零 mock）。

覆盖：解析、角色守卫、逐行合成任务、拼接时间轴、导出 ZIP、删除清理。
需要 7860 推理服务在线；不在线时相关用例会跳过（跳过会在输出里显式列出）。
"""
from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile

import pytest

import panel as P

from script_routes import MAX_LINES  # noqa: E402  (需先 import panel)


@pytest.fixture(scope="module")
def data_isolation(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("script-data")
    old = (P.DATA, P.REFS, P.AUDIOS)
    P.DATA, P.REFS, P.AUDIOS = tmp, tmp / "refs", tmp / "audio"
    for d in (P.DATA, P.REFS, P.AUDIOS):
        d.mkdir(parents=True, exist_ok=True)
    try:
        yield
    finally:
        P.DATA, P.REFS, P.AUDIOS = old


@pytest.fixture(scope="module")
def base_url(data_isolation):
    server = P.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _req(url, method="GET", body=None, ctype=None, timeout=60):
    r = urllib.request.Request(url, data=body, method=method)
    if ctype:
        r.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Type", "").startswith("application/zip"):
                return resp.status, raw
            return resp.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload or b"{}")
        except Exception:
            return e.code, {"detail": payload[:120].decode("utf-8", "replace")}


def _json_req(url, method, payload):
    return _req(url, method, json.dumps(payload).encode(), "application/json")


def _req_with_headers(url, method="GET", body=None, ctype=None, timeout=60):
    r = urllib.request.Request(url, data=body, method=method)
    if ctype:
        r.add_header("Content-Type", ctype)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, resp.read(), dict(resp.headers)


def _upstream_online(base_url) -> bool:
    code, _ = _req(base_url + "/api/model")
    return code == 200


def _mk_role(base_url, name, voice_id="", emotion="中性", archetype="custom"):
    code, role = _json_req(base_url + "/api/roles", "POST", {
        "name": name, "voice_id": voice_id, "default_emotion": emotion,
        "archetype": archetype,
    })
    assert code in (200, 201), role
    return role


def _mk_voice(base_url, name="剧本测试音色", instruction="沉稳的纪录片旁白"):
    code, v = _json_req(base_url + "/api/voices", "POST", {
        "name": name, "type": "design", "instruction": instruction,
    })
    assert code in (200, 201), v
    return v


def _mk_script(base_url, raw, title="测试剧本", **extra):
    # 默认关掉自动配角：这些用例要验证「未知角色/解析错误」等旧语义。
    # 想测零操作闭环的用例显式传 auto_cast=True。
    payload = {"title": title, "raw": raw, "auto_cast": False}
    payload.update(extra)
    code, s = _json_req(base_url + "/api/scripts", "POST", payload)
    assert code in (200, 201), s
    return s


# ---------------------------------------------------------------- 解析
def test_create_and_parse_script(base_url):
    # 音色不带指令时，角色原型（teen_f）的描述才会被采用——
    # 这正是「按原型建音色」的路径。
    voice = _mk_voice(base_url, "解析音色", instruction="")
    role = _mk_role(base_url, "解析甲", voice["id"], "迟疑", "teen_f")
    try:
        s = _mk_script(base_url, "\n".join([
            "## 第一场",
            "解析甲：我不太确定。",
            "解析甲[愤怒]：你再说一遍？",
        ]))
        try:
            code, d = _req(base_url + "/api/scripts/" + s["id"])
            assert code == 200
            assert [x["role_name"] for x in d["lines"]] == ["解析甲", "解析甲"]
            assert d["lines"][0]["emotion"] == "迟疑"
            assert d["lines"][0]["emotion_source"] == "role"
            assert d["lines"][1]["emotion"] == "愤怒"
            assert d["lines"][1]["emotion_source"] == "explicit"
            assert [x["name"] for x in d["scenes"]] == ["第一场"]
            # 原型描述要进指令，否则角色没有音色个性
            assert "小女孩" in d["lines"][0]["instruction"]
        finally:
            _req(base_url + "/api/scripts/" + s["id"], "DELETE")
    finally:
        _req(base_url + "/api/roles/" + role["id"], "DELETE")
        _req(base_url + "/api/voices/" + voice["id"], "DELETE")


def test_parse_reports_unknown_role_without_dropping_line(base_url):
    s = _mk_script(base_url, "查无此人：你好。")
    try:
        code, d = _req(base_url + "/api/scripts/" + s["id"])
        assert code == 200
        assert len(d["lines"]) == 1
        assert d["errors"] and "未知角色" in d["errors"][0]["reason"]
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")


def test_update_script_persists_raw_and_gap(base_url):
    s = _mk_script(base_url, "旁白：一。")
    try:
        code, d = _json_req(base_url + "/api/scripts/" + s["id"], "PUT", {
            "raw": "旁白：一。\n旁白：二。", "title": "改名了", "gap": 0.5,
        })
        assert code == 200, d
        assert d["title"] == "改名了"
        assert d["gap"] == 0.5
        code, again = _req(base_url + "/api/scripts/" + s["id"])
        assert again["title"] == "改名了"
        assert len(again["lines"]) == 2
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")


# ---------------------------------------------------------------- 守卫
def test_synth_rejects_unknown_role(base_url):
    if not _upstream_online(base_url):
        pytest.skip("上游 7860 不在线")
    s = _mk_script(base_url, "不存在的人：你好。")
    try:
        code, err = _json_req(base_url + f"/api/scripts/{s['id']}/synth", "POST", {})
        assert code == 400, err
        assert "角色不存在" in str(err.get("detail", "")), err
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")


def test_synth_rejects_role_without_voice(base_url):
    if not _upstream_online(base_url):
        pytest.skip("上游 7860 不在线")
    role = _mk_role(base_url, "无音色甲")
    s = _mk_script(base_url, "无音色甲：你好。")
    try:
        code, err = _json_req(base_url + f"/api/scripts/{s['id']}/synth", "POST", {})
        assert code == 400, err
        assert "还没绑定音色" in str(err.get("detail", "")), err
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")
        _req(base_url + "/api/roles/" + role["id"], "DELETE")


def test_synth_rejects_too_many_lines(base_url):
    raw = "\n".join(f"旁白：第{i}句。" for i in range(MAX_LINES + 1))
    code, err = _json_req(base_url + "/api/scripts", "POST", {"title": "超长", "raw": raw})
    assert code == 400, err
    assert str(MAX_LINES) in str(err.get("detail", "")), err


# ---------------------------------------------------------------- 合成任务
def test_synth_job_lifecycle_and_concat(base_url):
    if not _upstream_online(base_url):
        pytest.skip("上游 7860 不在线")
    voice = _mk_voice(base_url, "任务测试音色")
    role = _mk_role(base_url, "任务甲", voice["id"], "中性", "adult_m")
    s = _mk_script(base_url, "\n".join([
        "任务甲：第一句。",
        "任务甲：第二句。",
    ]), gap=0.3)
    try:
        code, job = _json_req(base_url + f"/api/scripts/{s['id']}/synth", "POST", {})
        assert code == 200 and job.get("job_id"), job
        assert job["total"] == 2

        deadline = time.time() + 240
        state = {}
        while time.time() < deadline:
            code, state = _req(base_url + "/api/scripts/jobs/" + job["job_id"])
            if state.get("state") in ("done", "stopped"):
                break
            time.sleep(2)

        assert state.get("state") == "done", state
        assert state["done"] == 2 and state["failed"] == 0, state

        code, d = _req(base_url + "/api/scripts/" + s["id"])
        tl = d.get("timeline") or []
        assert len(tl) == 2, tl
        assert tl[0]["start"] == 0
        # 行间停顿要生效，否则两行会粘在一起
        assert tl[1]["start"] >= tl[0]["end"] + 0.25, tl
        assert d.get("full_wav"), d
        assert abs((d.get("duration") or 0) - tl[-1]["end"]) < 0.2

        # 再来一次应全部跳过（续做语义）
        code, again = _json_req(base_url + f"/api/scripts/{s['id']}/synth", "POST", {})
        assert code == 200 and again["total"] == 0 and again["skipped"] == 2, again

        # 单行重录只动那一行
        code, redo = _json_req(base_url + f"/api/scripts/{s['id']}/lines/1/redo", "POST", {})
        assert code == 200 and redo.get("duration"), redo
        # 重录后这一行必须仍是 done：漏写 hash 会让它变回 pending，
        # 音频在盘上却听不到（试听按钮灰、整轨里也没它）
        code, after = _req(base_url + "/api/scripts/" + s["id"])
        assert after["lines"][1]["status"] == "done", after["lines"][1]
        assert after["lines"][1]["wav"], after["lines"][1]
        # 重录是「加一版」而不是覆盖：上一版还在，可以切回去
        take_row = after["lines"][1]
        assert take_row["take"] == 2, take_row
        assert [t["n"] for t in take_row["takes"]] == [1, 2], take_row
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")
        _req(base_url + "/api/roles/" + role["id"], "DELETE")
        _req(base_url + "/api/voices/" + voice["id"], "DELETE")


def test_export_zip_contains_four_artifacts(base_url):
    if not _upstream_online(base_url):
        pytest.skip("上游 7860 不在线")
    voice = _mk_voice(base_url, "导出测试音色")
    role = _mk_role(base_url, "导出甲", voice["id"])
    s = _mk_script(base_url, "导出甲：只有一句。")
    try:
        code, job = _json_req(base_url + f"/api/scripts/{s['id']}/synth", "POST", {})
        assert code == 200, job
        deadline = time.time() + 180
        while time.time() < deadline:
            _, state = _req(base_url + "/api/scripts/jobs/" + job["job_id"])
            if state.get("state") in ("done", "stopped"):
                break
            time.sleep(2)

        code, blob = _req(base_url + f"/api/scripts/{s['id']}/export", timeout=60)
        assert code == 200 and blob[:2] == b"PK", code
        names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
        assert "full.wav" in names
        assert "timeline.csv" in names
        assert "script.txt" in names
        assert any(n.startswith("lines/") for n in names), names
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")
        _req(base_url + "/api/roles/" + role["id"], "DELETE")
        _req(base_url + "/api/voices/" + voice["id"], "DELETE")


def test_delete_removes_audio_directory(base_url):
    if not _upstream_online(base_url):
        pytest.skip("上游 7860 不在线")
    voice = _mk_voice(base_url, "删除测试音色")
    role = _mk_role(base_url, "删除甲", voice["id"])
    s = _mk_script(base_url, "删除甲：一句话。")
    try:
        code, job = _json_req(base_url + f"/api/scripts/{s['id']}/synth", "POST", {})
        assert code == 200, job
        deadline = time.time() + 180
        while time.time() < deadline:
            _, state = _req(base_url + "/api/scripts/jobs/" + job["job_id"])
            if state.get("state") in ("done", "stopped"):
                break
            time.sleep(2)
        out_dir = P.AUDIOS / "scripts" / s["id"]
        assert out_dir.is_dir(), "合成后应有分行目录"

        code, _ = _req(base_url + "/api/scripts/" + s["id"], "DELETE")
        assert code == 200
        assert not out_dir.exists(), "删除剧本应连带清理音频目录"
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")
        _req(base_url + "/api/roles/" + role["id"], "DELETE")
        _req(base_url + "/api/voices/" + voice["id"], "DELETE")


def test_voice_instruction_overrides_archetype(base_url):
    """音色自己写了描述就以它为准，原型只当兜底。"""
    voice = _mk_voice(base_url, "覆盖音色", instruction="低沉的中年男声，语速偏慢")
    role = _mk_role(base_url, "覆盖甲", voice["id"], "中性", "teen_f")
    s = _mk_script(base_url, "覆盖甲：随便说一句。")
    try:
        code, d = _req(base_url + "/api/scripts/" + s["id"])
        assert code == 200
        ins = d["lines"][0]["instruction"]
        assert "中年男声" in ins, ins
        assert "小女孩" not in ins, ins
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")
        _req(base_url + "/api/roles/" + role["id"], "DELETE")
        _req(base_url + "/api/voices/" + voice["id"], "DELETE")


def test_resume_redoes_line_whose_text_changed(base_url):
    """改了一句台词后再合成，那一行必须重做，其余行才跳过。

    回归：续做原先只按行号判断，改了文案但行数不变时会把上一版音频
    当成新台词的成品，用户拿到文不对音的文件却看不到异常。
    """
    if not _upstream_online(base_url):
        pytest.skip("上游 7860 不在线")
    voice = _mk_voice(base_url, "续做测试音色")
    role = _mk_role(base_url, "续做甲", voice["id"])
    s = _mk_script(base_url, "续做甲：第一句。\n续做甲：第二句。")
    try:
        code, job = _json_req(base_url + f"/api/scripts/{s['id']}/synth", "POST", {})
        assert code == 200, job
        _wait_job(base_url, job["job_id"])
        code, d = _req(base_url + "/api/scripts/" + s["id"])
        assert all(x["status"] == "done" for x in d["lines"]), d["lines"]

        # 只改第二句，行数不变
        code, _ = _json_req(base_url + "/api/scripts/" + s["id"], "PUT", {
            "raw": "续做甲：第一句。\n续做甲：改过的第二句。",
        })
        assert code == 200

        code, again = _json_req(base_url + f"/api/scripts/{s['id']}/synth", "POST", {})
        assert code == 200, again
        assert again["skipped"] == 1, again
        assert again["total"] == 1, again

        code, d2 = _req(base_url + "/api/scripts/" + s["id"])
        assert d2["lines"][1]["text"] == "改过的第二句。"
        _wait_job(base_url, again["job_id"])
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")
        _req(base_url + "/api/roles/" + role["id"], "DELETE")
        _req(base_url + "/api/voices/" + voice["id"], "DELETE")


def _wait_job(base_url, job_id, timeout=240):
    deadline = time.time() + timeout
    state = {}
    while time.time() < deadline:
        code, state = _req(base_url + "/api/scripts/jobs/" + job_id)
        if state.get("state") in ("done", "stopped", "failed"):
            return state
        time.sleep(2)
    raise AssertionError(f"任务超时: {state}")


# ---------------------------------------------------------------- F6 场次氛围
def test_scene_mood_roundtrip_and_instruction(base_url):
    """氛围要能存、能读回，并且真的进到该场每一行的指令里。"""
    voice = _mk_voice(base_url, "氛围测试音色", instruction="")
    role = _mk_role(base_url, "氛围甲", voice["id"], "", "adult_m")
    s = _mk_script(base_url, "## 深夜书店\n氛围甲：你来了。\n## 医院走廊\n氛围甲：别出声。")
    try:
        code, d = _req(base_url + "/api/scripts/" + s["id"])
        assert [x["name"] for x in d["scenes"]] == ["深夜书店", "医院走廊"]
        # 「书店」是强信号：没手动选时系统自动带「安静」，并标记来源
        assert d["scenes"][0]["mood"] == "安静", d["scenes"]
        assert d["scenes"][0]["auto"] is True, d["scenes"]
        assert "整体氛围安静" in d["lines"][0]["instruction"], d["lines"][0]
        # 中性场次名不推断，避免系统给每场戏都自作主张套情绪
        assert d["scenes"][1]["mood"] == "", d["scenes"]

        code, d2 = _json_req(base_url + "/api/scripts/" + s["id"], "PUT", {
            "scenes": [{"name": "深夜书店", "mood": "紧张"}],
        })
        assert code == 200, d2
        assert "语速略快" in d2["lines"][0]["instruction"], d2["lines"][0]
        # 只给第一场设氛围，第二场不能跟着变
        assert "语速略快" not in d2["lines"][1]["instruction"], d2["lines"][1]

        code, d3 = _req(base_url + "/api/scripts/" + s["id"])
        assert d3["scenes"][0]["mood"] == "紧张"
        assert d3["scenes"][1]["mood"] == ""

        # 解析预览同样要带氛围：前端每次改字都靠它刷新行列表
        code, p = _json_req(base_url + f"/api/scripts/{s['id']}/parse", "POST", {
            "raw": "## 深夜书店\n氛围甲：又来了。",
        })
        assert code == 200, p
        assert p["scenes"][0]["mood"] == "紧张"
        assert "语速略快" in p["lines"][0]["instruction"]
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")
        _req(base_url + "/api/roles/" + role["id"], "DELETE")
        _req(base_url + "/api/voices/" + voice["id"], "DELETE")


def test_scene_mood_survives_temporary_scene_removal(base_url):
    """作者临时删掉/改名场次再改回来，已配好的氛围不该在普通保存里丢。"""
    s = _mk_script(base_url, "## 深夜书店\n旁白：你来了。")
    try:
        code, _ = _json_req(base_url + "/api/scripts/" + s["id"], "PUT", {
            "scenes": [{"name": "深夜书店", "mood": "安静"}],
        })
        assert code == 200

        # 场次行暂时消失（改稿中途），保存一次
        code, _ = _json_req(base_url + "/api/scripts/" + s["id"], "PUT", {
            "raw": "旁白：你来了。", "scenes": [],
        })
        assert code == 200
        # 场次改回来，配置要能跟着回来
        code, d = _json_req(base_url + "/api/scripts/" + s["id"], "PUT", {
            "raw": "## 深夜书店\n旁白：你来了。", "scenes": [],
        })
        assert code == 200, d
        assert d["scenes"][0]["mood"] == "安静", d["scenes"]
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")


# ---------------------------------------------------------------- F7 导出选项
def _seed_audio_files(script_id: str) -> None:
    """给每一行写占位 WAV 并拼一次整轨。

    只测「导出怎么打包」时不值得跑推理：按真实流程把行状态落成 done 再
    `_concat`，得到与合成完成后完全一致的磁盘结构。
    """
    import script_routes as SR

    script = next(s for s in P._load("scripts.json") if s["id"] == script_id)
    rows = SR._enriched(script, SR.parse_for(script))
    out_dir = P.AUDIOS / "scripts" / script_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        name = f"line_{row['idx']:03d}.wav"
        (out_dir / name).write_bytes(P.pcm_to_wav(b"\x00" * P.SAMPLE_RATE))
        SR._persist_line(script_id, row["idx"], {
            "status": "done",
            "wav": f"audio/scripts/{script_id}/{name}",
            "hash": row["hash"],
            "duration": 0.5,
        })
    SR._concat(script_id, 0.35)


def test_export_only_packs_requested_parts(base_url):
    """勾了什么就只给什么：盘上有音频也不能塞进只要文本的包。"""
    s = _mk_script(base_url, "旁白：第一句。\n旁白：第二句。", title="第一幕/雨夜")
    try:
        _seed_audio_files(s["id"])

        code, blob = _req(base_url + f"/api/scripts/{s['id']}/export?parts=script", timeout=30)
        assert code == 200 and blob[:2] == b"PK", (code, blob[:40])
        assert zipfile.ZipFile(io.BytesIO(blob)).namelist() == ["script.txt"]

        code, blob = _req(base_url + f"/api/scripts/{s['id']}/export?parts=full,lines",
                          timeout=30)
        names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
        assert "full.wav" in names, names
        assert len([n for n in names if n.startswith("lines/")]) == 2, names
        assert "script.txt" not in names and "timeline.csv" not in names, names

        # 中文标题走 RFC 5987；标题里的 "/" 必须被编码，否则浏览器整段忽略
        code, _, headers = _req_with_headers(
            base_url + f"/api/scripts/{s['id']}/export?parts=timeline")
        cd = headers.get("Content-Disposition", "")
        assert "filename*=UTF-8''" in cd, cd
        assert "%2F" in cd and "%E9%9B%A8%E5%A4%9C" in cd, cd
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")


def test_export_blocks_stale_full_audio(base_url):
    """台词改了但那一行没重做时，整轨与时间轴是上一版的，必须拒绝导出。"""
    s = _mk_script(base_url, "旁白：第一句。\n旁白：第二句。")
    try:
        _seed_audio_files(s["id"])
        code, _ = _json_req(base_url + "/api/scripts/" + s["id"], "PUT", {
            "raw": "旁白：改过的第一句。\n旁白：第二句。",
        })
        assert code == 200, _

        code, err = _req(base_url + f"/api/scripts/{s['id']}/export?parts=full", timeout=30)
        assert code == 400, (code, err)
        assert "重做" in str(err.get("detail")), err

        # 只要文本仍然可以导出（文本永远是最新的）
        code, blob = _req(base_url + f"/api/scripts/{s['id']}/export?parts=script", timeout=30)
        assert code == 200 and blob[:2] == b"PK"
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")


def test_export_rejects_audio_when_not_synthesized(base_url):
    """要音频却没合成：明确报错，而不是给一个只有文本的包。"""
    s = _mk_script(base_url, "旁白：只有文本。")
    try:
        code, err = _req(base_url + f"/api/scripts/{s['id']}/export?parts=full", timeout=30)
        assert code == 400, (code, err)
        assert "合成" in str(err.get("detail")), err

        code, err = _req(base_url + f"/api/scripts/{s['id']}/export?parts=video", timeout=30)
        assert code == 400, (code, err)

        code, blob = _req(base_url + f"/api/scripts/{s['id']}/export?parts=script", timeout=30)
        assert code == 200 and zipfile.ZipFile(io.BytesIO(blob)).namelist() == ["script.txt"]
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")


# ---------------------------------------------------------------- 多版本（take）
def test_take_switch_rebuilds_full_track(base_url):
    """一条台词可以有多版：切换后整轨按选中那版重拼，选不存在的版本要 404。"""
    import script_routes as SR

    s = _mk_script(base_url, "旁白：第一句。\n旁白：第二句。")
    try:
        _seed_audio_files(s["id"])
        script = next(x for x in P._load("scripts.json") if x["id"] == s["id"])
        row = SR._enriched(script, SR.parse_for(script))[0]
        out_dir = P.AUDIOS / "scripts" / s["id"]
        name = "line_000_t2.wav"
        # 第 2 版故意做长一点：切过去后整轨时长必须跟着变，
        # 否则「切了没生效」这种 bug 会被静默放过
        (out_dir / name).write_bytes(P.pcm_to_wav(b"\x00" * (P.SAMPLE_RATE * 2)))
        SR._persist_take(s["id"], 0, row, 2, 1.0, f"audio/scripts/{s['id']}/{name}")
        SR._concat(s["id"], 0.35)

        code, d = _req(base_url + "/api/scripts/" + s["id"])
        assert [t["n"] for t in d["lines"][0]["takes"]] == [1, 2], d["lines"][0]
        assert d["lines"][0]["take"] == 2, d["lines"][0]
        longer = d["duration"]

        code, r = _json_req(base_url + f"/api/scripts/{s['id']}/lines/0/take", "POST", {"n": 1})
        assert code == 200, r
        code, d = _req(base_url + "/api/scripts/" + s["id"])
        assert d["lines"][0]["take"] == 1, d["lines"][0]
        assert d["lines"][0]["wav"].endswith("line_000.wav"), d["lines"][0]["wav"]
        assert d["duration"] < longer, (d["duration"], longer)

        # 导出打包的是「当前选中的那一版」，不是固定按行号拼的第一个文件
        code, blob = _req(base_url + f"/api/scripts/{s['id']}/export?parts=lines", timeout=30)
        assert code == 200 and blob[:2] == b"PK", (code, blob[:40])

        code, err = _json_req(base_url + f"/api/scripts/{s['id']}/lines/0/take", "POST", {"n": 9})
        assert code == 404, (code, err)
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")


# ---------------------------------------------------------------- 角色语速 / 口音
def test_role_speed_and_accent_reach_the_line(base_url):
    voice = _mk_voice(base_url, "语速测试音色", instruction="")
    code, role = _json_req(base_url + "/api/roles", "POST", {
        "name": "语速甲", "voice_id": voice["id"], "archetype": "adult_m",
        "speed": 0.8, "accent": "带一点东北口音，语调爽朗上扬",
    })
    assert code in (200, 201), role
    s = _mk_script(base_url, "语速甲：你好。")
    try:
        code, d = _req(base_url + "/api/scripts/" + s["id"])
        line = d["lines"][0]
        assert line["speed"] == 0.8, line
        assert "东北" in line["instruction"], line
        first_hash = line["hash"]

        # 改语速 → 行身份必须变，否则续做会拿旧音频冒充新参数的结果
        code, _ = _json_req(base_url + "/api/roles/" + role["id"], "PUT", {"speed": 1.25})
        assert code == 200
        code, d2 = _req(base_url + "/api/scripts/" + s["id"])
        assert d2["lines"][0]["speed"] == 1.25
        assert d2["lines"][0]["hash"] != first_hash
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")
        _req(base_url + "/api/roles/" + role["id"], "DELETE")
        _req(base_url + "/api/voices/" + voice["id"], "DELETE")


# ---------------------------------------------------------------- F9 原型音色
def test_prototype_voice_from_archetype_needs_no_sample(base_url):
    """只给原型就能建出可用音色：没有参考音频也不该报错。"""
    code, v = _json_req(base_url + "/api/voices", "POST", {
        "name": "原型旁白", "type": "design", "archetype": "narrator",
    })
    assert code in (200, 201), v
    try:
        assert v["archetype"] == "narrator"
        assert "纪录片" in v["instruction"], v
        assert not v.get("ref_audio")
    finally:
        _req(base_url + "/api/voices/" + v["id"], "DELETE")


def test_duplicate_role_name_is_rejected(base_url):
    """剧本按名字找角色：同名会让后建的静默顶掉先建的。"""
    first = _mk_role(base_url, "重名甲")
    second = _mk_role(base_url, "重名乙")
    try:
        code, err = _json_req(base_url + "/api/roles", "POST", {"name": "重名甲"})
        assert code == 400, err
        assert "同名" in str(err.get("detail")), err

        code, err = _json_req(base_url + "/api/roles/" + second["id"], "PUT", {"name": "重名甲"})
        assert code == 400, err

        # 改回自己的名字不算冲突
        code, ok = _json_req(base_url + "/api/roles/" + second["id"], "PUT", {"name": "重名乙"})
        assert code == 200, ok
    finally:
        _req(base_url + "/api/roles/" + first["id"], "DELETE")
        _req(base_url + "/api/roles/" + second["id"], "DELETE")


# ---------------------------------------------------------------- 一键补齐角色
def test_cast_autofill_builds_roles_and_voices_from_script(base_url):
    """写完剧本满屏未知角色时，一次补齐就能直接合成，不用去音色库手工建 N 次。"""
    clone = _mk_voice(base_url, "小蜜蜂配音", instruction="我自己的声音")
    raw = "\n".join([
        "## 场景一",
        "小蜜蜂旁白：傍晚六点，一位奶奶带着孙子推门进来。",
        "小蜜蜂[清嗓子]：阿姨，您慢点。",
        "孙子：奶奶你看！这个窗户好大！",
        "奶奶[叹气]：山是真的……你慢点跑。",
    ])
    s = _mk_script(base_url, raw, title="自动配角验收")
    created_ids = []
    created_voice_ids = []
    try:
        code, before = _req(base_url + "/api/scripts/" + s["id"])
        assert len(before["errors"]) == 4, before["errors"]
        assert all(not l["voice_id"] for l in before["lines"])

        code, res = _json_req(base_url + f"/api/scripts/{s['id']}/cast", "POST", {})
        assert code == 201, res
        assert res["count"] == 4, res
        assert res["remaining"] == 0, res
        created_ids = [c["role_id"] for c in res["created"]]
        created_voice_ids = [c["voice_id"] for c in res["created"]
                             if not c["reused"]]
        by_name = {c["name"]: c for c in res["created"]}

        # 返回的 id 必须是**真的落库**的：只断言非空会让「角色指向
        # 不存在的音色」和「并发下谎报创建」悄悄通过
        _, roles_now = _req(base_url + "/api/roles")
        _, voices_now = _req(base_url + "/api/voices")
        role_ids = {r["id"] for r in roles_now}
        voice_ids = {v["id"] for v in voices_now}
        assert set(created_ids) <= role_ids
        assert set(created_voice_ids) <= voice_ids

        # 名字跟已有音色同源的角色复用它；其余按名字猜原型并各自建音色
        assert by_name["小蜜蜂"]["voice_id"] == clone["id"], by_name["小蜜蜂"]
        assert by_name["小蜜蜂旁白"]["voice_id"] == clone["id"]
        assert by_name["奶奶"]["archetype"] == "elder_f"
        assert by_name["孙子"]["archetype"] == "teen_m"
        assert by_name["奶奶"]["voice_id"] != by_name["孙子"]["voice_id"]

        code, after = _req(base_url + "/api/scripts/" + s["id"])
        assert after["errors"] == [], after["errors"]
        assert all(l["voice_id"] in voice_ids for l in after["lines"]), after["lines"]
        # 原型与动作标签都真正落到指令里
        assert "女性长者" in after["lines"][3]["instruction"], after["lines"][3]
        assert "叹" in after["lines"][3]["instruction"]
        assert "清" in after["lines"][1]["instruction"]

        # 幂等：再点一次不重复建角色
        code, again = _json_req(base_url + f"/api/scripts/{s['id']}/cast", "POST", {})
        assert code == 200 and again["count"] == 0, again
        _, roles = _req(base_url + "/api/roles")
        assert len([r for r in roles if r["name"] == "奶奶"]) == 1
    finally:
        for rid in created_ids:
            _req(base_url + "/api/roles/" + rid, "DELETE")
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")
        for vid in set(created_voice_ids) - {clone["id"]}:
            _req(base_url + "/api/voices/" + vid, "DELETE")
        _req(base_url + "/api/voices/" + clone["id"], "DELETE")


def test_cast_respects_forced_voice_and_unknown_id(base_url):
    voice = _mk_voice(base_url, "统一音色")
    s = _mk_script(base_url, "甲：你好。\n乙：你好。")
    made = []
    try:
        code, err = _json_req(base_url + f"/api/scripts/{s['id']}/cast", "POST",
                              {"voice_id": "v_not_exist"})
        assert code == 400, (code, err)

        code, res = _json_req(base_url + f"/api/scripts/{s['id']}/cast", "POST",
                              {"voice_id": voice["id"]})
        assert code == 201 and res["count"] == 2, res
        made = [c["role_id"] for c in res["created"]]
        assert all(c["voice_id"] == voice["id"] and c["reused"] for c in res["created"])
        assert res["remaining"] == 0

        # 幂等分支也必须校验 voice_id：没有缺角色时不能静默吞掉非法 id
        code, err = _json_req(base_url + f"/api/scripts/{s['id']}/cast", "POST",
                              {"voice_id": "v_not_exist"})
        assert code == 400, (code, err)
    finally:
        # 断言中途失败时 made 还是空的，按名字再兜一次，别把角色留在库里
        if not made:
            _, roles = _req(base_url + "/api/roles")
            made = [r["id"] for r in roles if r["name"] in ("甲", "乙")]
        for rid in made:
            _req(base_url + "/api/roles/" + rid, "DELETE")
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")
        _req(base_url + "/api/voices/" + voice["id"], "DELETE")


# ---------------------------------------------------------------- 保存即自动配角
def test_saving_script_autofills_cast_with_zero_clicks(base_url):
    """粘贴剧本 → 保存 → 零红字、每行都有音色，不需要点任何按钮。"""
    s = _mk_script(base_url, "## 街角\n路人丙：你好。\n老奶奶[叹气]：慢点走。",
                   auto_cast=True)
    made = []
    try:
        code, d = _req(base_url + "/api/scripts/" + s["id"])
        assert code == 200
        assert d["errors"] == [], d["errors"]
        assert all(l["voice_id"] for l in d["lines"]), d["lines"]
        _, roles = _req(base_url + "/api/roles")
        made = [r["id"] for r in roles if r["name"] in ("路人丙", "老奶奶")]
        assert len(made) == 2, roles
        _, voices = _req(base_url + "/api/voices")
        assert any(v["archetype"] == "elder_f" for v in voices), voices
    finally:
        for rid in made:
            _req(base_url + "/api/roles/" + rid, "DELETE")
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")
        _, voices = _req(base_url + "/api/voices")
        for v in voices:
            if v["name"] in ("路人丙的声音", "老奶奶的声音"):
                _req(base_url + "/api/voices/" + v["id"], "DELETE")


def test_auto_cast_can_be_turned_off_per_script(base_url):
    s = _mk_script(base_url, "甲：你好。", auto_cast=False)
    try:
        code, d = _req(base_url + "/api/scripts/" + s["id"])
        assert len(d["errors"]) == 1, d["errors"]
        _, roles = _req(base_url + "/api/roles")
        assert not any(r["name"] == "甲" for r in roles)
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")


def test_put_with_new_character_autofills_too(base_url):
    """改稿时新加一个角色，保存就自动配好，不用再点补齐。"""
    voice = _mk_voice(base_url, "改稿音色", instruction="沉稳旁白")
    role = _mk_role(base_url, "原角色", voice["id"])
    s = _mk_script(base_url, "原角色：第一句。", auto_cast=True)
    extra_role = None
    try:
        code, d = _json_req(base_url + "/api/scripts/" + s["id"], "PUT", {
            "raw": "原角色：第一句。\n新来的：第二句。",
        })
        assert code == 200, d
        assert d["errors"] == [], d["errors"]
        _, roles = _req(base_url + "/api/roles")
        extra_role = next((r["id"] for r in roles if r["name"] == "新来的"), None)
        assert extra_role
    finally:
        if extra_role:
            _req(base_url + "/api/roles/" + extra_role, "DELETE")
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")
        _req(base_url + "/api/roles/" + role["id"], "DELETE")
        _req(base_url + "/api/voices/" + voice["id"], "DELETE")
        _, voices = _req(base_url + "/api/voices")
        for v in voices:
            if v["name"] == "新来的的声音":
                _req(base_url + "/api/voices/" + v["id"], "DELETE")


# ---------------------------------------------------------------- 自动氛围不落盘
def test_auto_mood_is_not_persisted_as_a_manual_choice(base_url):
    s = _mk_script(base_url, "## 雨夜\n旁白：你来了。")
    try:
        code, d = _req(base_url + "/api/scripts/" + s["id"])
        assert d["scenes"][0]["mood"] == "压抑", d["scenes"]
        assert d["scenes"][0]["auto"] is True
        # 把界面拿到的 scenes 整组回传，自动值不能被固化
        code, d2 = _json_req(base_url + "/api/scripts/" + s["id"], "PUT",
                             {"scenes": d["scenes"]})
        assert code == 200, d2
        code, d3 = _req(base_url + "/api/scripts/" + s["id"])
        assert d3["scenes"][0]["auto"] is True, d3["scenes"]
        # 作者真的选了一个值后，来源变成手动
        code, d4 = _json_req(base_url + "/api/scripts/" + s["id"], "PUT",
                             {"scenes": [{"name": "雨夜", "mood": "温暖"}]})
        assert code == 200
        code, d5 = _req(base_url + "/api/scripts/" + s["id"])
        assert d5["scenes"][0]["mood"] == "温暖"
        assert d5["scenes"][0]["auto"] is False, d5["scenes"]
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")


# ---------------------------------------------------------------- 字幕导出
def test_subtitles_export_srt_and_vtt_from_timeline(base_url):
    s = _mk_script(base_url, "旁白：第一句。\n旁白：第二句。")
    try:
        _seed_audio_files(s["id"])
        code, blob = _req(base_url + f"/api/scripts/{s['id']}/export?parts=subtitles",
                          timeout=30)
        assert code == 200 and blob[:2] == b"PK", (code, blob[:40])
        z = zipfile.ZipFile(io.BytesIO(blob))
        assert sorted(z.namelist()) == ["subtitles.srt", "subtitles.vtt"], z.namelist()
        srt = z.read("subtitles.srt").decode("utf-8")
        vtt = z.read("subtitles.vtt").decode("utf-8")
        assert srt.startswith("1\n00:00:00,000 --> "), srt[:60]
        assert "-->" in srt and "第一句" in srt and "第二句" in srt
        assert vtt.startswith("WEBVTT"), vtt[:30]
        assert "00:00:00.000 --> " in vtt
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")


def test_subtitles_refuse_stale_timeline(base_url):
    s = _mk_script(base_url, "旁白：第一句。")
    try:
        code, err = _req(base_url + f"/api/scripts/{s['id']}/export?parts=subtitles",
                         timeout=30)
        assert code == 400, (code, err)
        assert "合成" in str(err.get("detail")), err
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")


# ---------------------------------------------------------------- cfg/seed/route
def test_cfg_change_marks_new_rows_pending_but_legacy_rows_stay_done(base_url):
    """新落盘的行改 CFG 要重做；老记录缺字段时不能被升级打成一片未合成。"""
    import script_routes as SR
    s = _mk_script(base_url, "旁白：只有一句。")
    try:
        # _seed_audio_files 写的是老格式（无 cfg_scale/seed 字段）
        _seed_audio_files(s["id"])
        code, d = _req(base_url + "/api/scripts/" + s["id"])
        assert d["lines"][0]["status"] == "done", d["lines"][0]
        code, d2 = _json_req(base_url + "/api/scripts/" + s["id"], "PUT",
                             {"cfg_scale": 7})
        assert code == 200
        # 老记录不知道当时的 CFG，只能按「未知」放行：保住用户已合成的整轨
        assert d2["lines"][0]["status"] == "done", d2["lines"][0]

        # 换成新格式记录（带 cfg_scale/seed），再改 CFG 就必须变 pending
        fresh = SR._find(s["id"])
        row = SR._enriched(fresh, SR.parse_for(fresh))[0]
        SR._persist_take(s["id"], 0, row, 1, 0.5,
                         f"audio/scripts/{s['id']}/line_000.wav", reset=True)
        code, d3 = _req(base_url + "/api/scripts/" + s["id"])
        assert d3["lines"][0]["status"] == "done", d3["lines"][0]
        code, d4 = _json_req(base_url + "/api/scripts/" + s["id"], "PUT",
                             {"cfg_scale": 4})
        assert code == 200
        assert d4["lines"][0]["status"] == "pending", d4["lines"][0]
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")


def test_seed_is_derived_per_role_and_route_is_exposed(base_url):
    """同原型角色 seed 不能全一样；路线信息必须暴露给界面。"""
    voice = _mk_voice(base_url, "同原型音色", instruction="沉稳旁白")
    r1 = _mk_role(base_url, "角色A", voice["id"], archetype="adult_m")
    r2 = _mk_role(base_url, "角色B", voice["id"], archetype="adult_m")
    s = _mk_script(base_url, "角色A：你好。\n角色B：也好。")
    try:
        code, d = _req(base_url + "/api/scripts/" + s["id"])
        assert d["lines"][0]["seed"] != d["lines"][1]["seed"], d["lines"]
        assert d["lines"][0]["route"] == "design"
        assert d["lines"][0]["instruction_effective"] is True
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")
        _req(base_url + "/api/roles/" + r1["id"], "DELETE")
        _req(base_url + "/api/roles/" + r2["id"], "DELETE")
        _req(base_url + "/api/voices/" + voice["id"], "DELETE")


def test_clone_route_is_flagged_as_instruction_ineffective(base_url):
    """Qwen clone 路线忽略 instruction：界面必须看得到，不能静默。"""
    import panel as P
    voice = _mk_voice(base_url, "克隆音色", instruction="")

    def _mut(items):
        for v in items:
            if v["id"] == voice["id"]:
                v["type"] = "clone"
                v["ref_audio"] = "refs/demo.wav"
                v["ref_text"] = "示例文稿"

    P._update_json("voices.json", _mut)
    role = _mk_role(base_url, "克隆角色", voice["id"])
    s = _mk_script(base_url, "## 雨夜\n克隆角色[愤怒]：你再说一遍？")
    try:
        code, d = _req(base_url + "/api/scripts/" + s["id"])
        line = d["lines"][0]
        assert line["route"] == "clone", line
        assert line["instruction_effective"] is False, line
        assert "整体氛围压抑" in line["instruction"], line
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")
        _req(base_url + "/api/roles/" + role["id"], "DELETE")
        _req(base_url + "/api/voices/" + voice["id"], "DELETE")
