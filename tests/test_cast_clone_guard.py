# coding: utf-8
"""自动配角不得把角色绑到克隆音色上（PRD-v4 §4.1 定的修复）。

clone 路线不收任何自然语言指令。一旦自动配角把新角色绑到克隆音色上，
这个角色的情绪、场次氛围、原型口音就全被上游静默丢掉——界面看着
「智能配好了」，实际一句都没生效。必须：自动配角一律不复用克隆音色，
作者要指定克隆音色，去角色卡里手动选（那是明确意图）。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

import panel as P
import script_routes as R


@pytest.fixture(scope="module")
def data_isolation(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("clone-guard-data")
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
            return resp.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload or b"{}")
        except Exception:
            return e.code, {"detail": payload[:200].decode("utf-8", "replace")}


def _json_req(url, method, payload):
    return _req(url, method, json.dumps(payload).encode(), "application/json")


# ------------------------------------------------------------------ 纯函数层
def test_plan_cast_never_reuses_clone_voice():
    voices = {
        "v1": {"id": "v1", "name": "小蜜蜂配音", "type": "clone",
               "instruction": "我自己的声音"},
        "v2": {"id": "v2", "name": "沉稳旁白", "type": "design",
               "instruction": "沉稳的纪录片旁白"},
    }
    errors = [{"role_name": "小蜜蜂"}]
    plan = R.plan_cast(errors, set(), voices)
    assert len(plan) == 1
    assert plan[0]["voice"] is None, "克隆音色不该被自动复用"
    assert plan[0]["route"] == "design"
    assert plan[0]["skipped_clone"] == "小蜜蜂配音"


def test_plan_cast_still_reuses_design_voice():
    voices = {
        "v2": {"id": "v2", "name": "小蜜蜂配音", "type": "design",
               "instruction": "清亮的少女声"},
    }
    plan = R.plan_cast([{"role_name": "小蜜蜂"}], set(), voices)
    assert plan[0]["voice"]["id"] == "v2"
    assert plan[0]["skipped_clone"] == ""


def test_plan_cast_uses_clone_when_forced_by_author():
    """作者手动指定音色（forced）时不受这条限制——那是明确意图。"""
    voices = {"v1": {"id": "v1", "name": "小蜜蜂配音", "type": "clone"}}
    plan = R.plan_cast([{"role_name": "小蜜蜂"}], set(), voices)
    assert plan[0]["voice"] is None


# ------------------------------------------------------------------ 接口层
def test_saved_script_role_does_not_bind_clone_voice(base_url):
    code, clone = _json_req(base_url + "/api/voices", "POST", {
        "name": "小蜜蜂配音", "type": "clone", "ref_text": "这是我的声音",
    })
    assert code in (200, 201), clone
    raw = "\n".join([
        "## 场景一",
        "小蜜蜂：奶奶你看！这个窗户好大！",
    ])
    # auto_cast=True：保存即自动配角，走的就是自动复用那条路
    code, s = _json_req(base_url + "/api/scripts", "POST", {
        "title": "克隆守卫验收", "raw": raw, "auto_cast": True,
    })
    assert code in (200, 201), s
    try:
        code, detail = _req(base_url + "/api/scripts/" + s["id"])
        assert detail["errors"] == [], detail["errors"]
        line = detail["lines"][0]
        assert line["voice_id"] and line["voice_id"] != clone["id"], \
            "新角色被绑到克隆音色上了"
        assert line["route"] == "design", line
        # 设计路线才吃指令：情绪/原型真的下发到了
        assert line["instruction_effective"] is True
        assert line["instruction"], "角色应该带上一条原型描述"

        # 补齐接口再点一次也不该把角色改绑回克隆音色
        code, res = _json_req(base_url + f"/api/scripts/{s['id']}/cast",
                              "POST", {})
        assert code in (200, 201), res
        assert res["count"] == 0, res
        code, detail2 = _req(base_url + "/api/scripts/" + s["id"])
        assert detail2["lines"][0]["voice_id"] != clone["id"]
    finally:
        _req(base_url + "/api/scripts/" + s["id"], "DELETE")
        _req(base_url + "/api/voices/" + clone["id"], "DELETE")
