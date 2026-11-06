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
    code, s = _json_req(base_url + "/api/scripts", "POST", {
        "title": title, "raw": raw, **extra,
    })
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
        assert d["scenes"][0]["mood"] == "", d["scenes"]

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
