# coding: utf-8
"""角色批量编辑接口。

角色一多（30+），一个一个点开改默认情绪/语速是纯体力活；PRD-v4 §5 P0-5
把「角色搜索 + 批量编辑」列进验收口径，这里守住批量这一半。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

import panel as P


@pytest.fixture(scope="module")
def data_isolation(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("role-batch-data")
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


def _mk_role(base_url, name, **extra):
    payload = {"name": name}
    payload.update(extra)
    code, r = _json_req(base_url + "/api/roles", "POST", payload)
    assert code in (200, 201), r
    return r


def test_batch_updates_selected_roles(base_url):
    a = _mk_role(base_url, "批量甲", default_emotion="温柔", speed=1.0)
    b = _mk_role(base_url, "批量乙", default_emotion="愤怒", speed=1.2)
    c = _mk_role(base_url, "批量丙", default_emotion="", speed=1.0)
    try:
        code, res = _json_req(base_url + "/api/roles/batch", "POST", {
            "ids": [a["id"], b["id"]],
            "patch": {"default_emotion": "平静", "speed": 1.35},
        })
        assert code == 200, res
        assert res["updated"] == 2, res

        _, roles = _req(base_url + "/api/roles")
        by_id = {r["id"]: r for r in roles}
        assert by_id[a["id"]]["default_emotion"] == "平静"
        assert by_id[b["id"]]["default_emotion"] == "平静"
        assert abs(float(by_id[a["id"]]["speed"]) - 1.35) < 1e-6
        # 没选中的不能动
        assert by_id[c["id"]]["default_emotion"] == ""
        assert abs(float(by_id[c["id"]]["speed"]) - 1.0) < 1e-6
    finally:
        for r in (a, b, c):
            _req(base_url + "/api/roles/" + r["id"], "DELETE")


def test_batch_rejects_bad_input(base_url):
    a = _mk_role(base_url, "批量丁")
    try:
        code, res = _json_req(base_url + "/api/roles/batch", "POST",
                              {"ids": [], "patch": {"speed": 1.0}})
        assert code == 400, res

        code, res = _json_req(base_url + "/api/roles/batch", "POST",
                              {"ids": [a["id"]], "patch": {}})
        assert code == 400, res

        # 越界的语速按单条接口的同一口径夹到 0.5~2（不是报错）
        code, res = _json_req(base_url + "/api/roles/batch", "POST",
                              {"ids": [a["id"]], "patch": {"speed": 9}})
        assert code == 200, res
        _, roles = _req(base_url + "/api/roles")
        by_id = {r["id"]: r for r in roles}
        assert abs(float(by_id[a["id"]]["speed"]) - 2.0) < 1e-6

        # 不是数字 → 报错，别默默当 1.0 写进去
        code, res = _json_req(base_url + "/api/roles/batch", "POST",
                              {"ids": [a["id"]], "patch": {"speed": "快一点"}})
        assert code == 400, res

        code, res = _json_req(base_url + "/api/roles/batch", "POST",
                              {"ids": [a["id"]], "patch": {"voice_id": "v_nope"}})
        assert code == 400, res

        code, res = _json_req(base_url + "/api/roles/batch", "POST",
                              {"ids": [a["id"]], "patch": {"name": "改名"}})
        assert code == 400, res   # 改名要走单条接口（要过同名查重）
    finally:
        _req(base_url + "/api/roles/" + a["id"], "DELETE")


def test_batch_reports_missing_roles(base_url):
    code, res = _json_req(base_url + "/api/roles/batch", "POST", {
        "ids": ["r_不存在"], "patch": {"default_emotion": "平静"},
    })
    assert code == 400, res
