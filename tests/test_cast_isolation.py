"""角色按剧本隔离的接口测试（真实服务，零 mock，不需要推理引擎）。

对应 `PLAN-2026-11-08-cast-isolation.md` §5 的验收口径：
* 两个新剧本的同名「旁白」各有自己的一份，改一个不影响另一个
* 老剧本（没有 cast 字段）零迁移：读到即认领全局角色，只写一次，行为不变
* 角色重名只在「同一个归属」内冲突
* 删角色会把剧本 cast 里的引用一起清掉
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import uuid

import pytest

import panel as P


@pytest.fixture(scope="module")
def data_isolation(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cast-data")
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
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload or b"{}")
        except Exception:
            return e.code, {"detail": payload[:200].decode("utf-8", "replace")}


def _json_req(url, method, payload):
    return _req(url, method, json.dumps(payload).encode(), "application/json")


def _mk_script(base_url, raw, title="隔离测试剧本", **extra):
    payload = {"title": title, "raw": raw, "auto_cast": True}
    payload.update(extra)
    code, s = _json_req(base_url + "/api/scripts", "POST", payload)
    assert code in (200, 201), s
    return s


def _mk_voice(base_url, name):
    code, v = _json_req(base_url + "/api/voices", "POST", {
        "name": name, "type": "design", "instruction": "平稳的旁白",
    })
    assert code in (200, 201), v
    return v


def _line_of(detail, role_name):
    return next(l for l in detail["lines"] if l["role_name"] == role_name)


# ---------------------------------------------------------------- 新剧本隔离
def test_new_scripts_isolate_same_named_role(base_url):
    a = _mk_script(base_url, "## 第一场\n旁白：甲剧本的旁白。", title="隔离A")
    b = _mk_script(base_url, "## 第一场\n旁白：乙剧本的旁白。", title="隔离B")
    try:
        da = _req(base_url + f"/api/scripts/{a['id']}")[1]
        db = _req(base_url + f"/api/scripts/{b['id']}")[1]
        assert da["cast_isolated"] is True and db["cast_isolated"] is True
        assert da["cast"] and db["cast"], (da["cast"], db["cast"])

        rid_a = _line_of(da, "旁白")["role_id"]
        rid_b = _line_of(db, "旁白")["role_id"]
        assert rid_a and rid_b and rid_a != rid_b, "两个剧本的「旁白」必须各是各的角色"

        # 改 A 剧旁白的音色，B 剧不能跟着变
        v = _mk_voice(base_url, "只给A剧用的音色")
        code, patched = _json_req(base_url + f"/api/roles/{rid_a}", "PUT",
                                  {"voice_id": v["id"]})
        assert code == 200, patched

        da2 = _req(base_url + f"/api/scripts/{a['id']}")[1]
        db2 = _req(base_url + f"/api/scripts/{b['id']}")[1]
        assert _line_of(da2, "旁白")["voice_id"] == v["id"], da2["lines"][0]
        assert _line_of(db2, "旁白")["voice_id"] == _line_of(db, "旁白")["voice_id"], \
            "改 A 剧的音色不能波及 B 剧"
        assert _line_of(db2, "旁白")["role_id"] == rid_b
    finally:
        for s in (a, b):
            _req(base_url + f"/api/scripts/{s['id']}", "DELETE")


def test_roles_list_carries_script_title(base_url):
    s = _mk_script(base_url, "## 第一场\n旁白：带标题。", title="带标题的剧本")
    try:
        code, roles = _req(base_url + "/api/roles")
        assert code == 200
        mine = [r for r in roles if str(r.get("script_id")) == s["id"]]
        assert mine, "新剧本应该有自己的专属角色"
        assert mine[0]["script_title"] == "带标题的剧本", mine[0]
        # 全局角色不该带剧本标题
        assert all(r["script_title"] == "" for r in roles if not r.get("script_id"))
    finally:
        _req(base_url + f"/api/scripts/{s['id']}", "DELETE")


def test_new_script_ignores_global_role_with_same_name(base_url):
    """全局已经有同名角色时，新剧本仍然建自己的一份。

    这条是真实现场踩出来的：只按「解析有没有报错」判断缺谁，会命中全局
    的「旁白」，于是两个新剧本静默共用同一个角色——隔离等于没做。
    """
    v = _mk_voice(base_url, "全局同名音色")
    code, grole = _json_req(base_url + "/api/roles", "POST",
                            {"name": "全局旁白", "voice_id": v["id"]})
    assert code in (200, 201), grole

    s = _mk_script(base_url, "## 第一场\n全局旁白：我应该有自己的角色。", title="同名隔离")
    try:
        d = _req(base_url + f"/api/scripts/{s['id']}")[1]
        rid = _line_of(d, "全局旁白")["role_id"]
        assert rid and rid != grole["id"], "全局有同名角色也不能直接拿来用"
        assert rid in d["cast"], d["cast"]
        owner = next(r for r in P._load("roles.json") if r["id"] == rid)
        assert owner["script_id"] == s["id"], owner
    finally:
        _req(base_url + f"/api/scripts/{s['id']}", "DELETE")


# ---------------------------------------------------------------- 老数据零迁移
def _push_legacy_script(raw, title="老剧本"):
    """直接往 scripts.json 塞一条升级前形态的记录（没有 cast 字段）。"""
    sid = "s_legacy" + uuid.uuid4().hex[:6]
    rec = {
        "id": sid, "title": title, "raw": raw,
        "scenes": [], "gap": 0.35, "cfg_scale": 4.0, "seed": 42,
        "auto_cast": True, "lines": [],
        "created_at": "2026-11-01 10:00:00", "updated_at": "2026-11-01 10:00:00",
    }
    assert "cast" not in rec
    P._update_json("scripts.json", lambda items: items.append(rec))
    return rec


def test_legacy_script_claims_global_role_once(base_url):
    # 造一个全局角色（script_id 为空），模拟升级前角色库的形态
    v = _mk_voice(base_url, "全局旁白音色")
    code, global_role = _json_req(base_url + "/api/roles", "POST",
                                  {"name": "老旁白", "voice_id": v["id"]})
    assert code in (200, 201), global_role

    rec = _push_legacy_script("## 第一场\n老旁白：升级前的剧本。")
    try:
        code, d1 = _req(base_url + f"/api/scripts/{rec['id']}")
        assert code == 200, d1
        assert d1["cast_isolated"] is False, "老剧本不能被当成隔离模式"
        assert global_role["id"] in d1["cast"], d1["cast"]
        assert _line_of(d1, "老旁白")["role_id"] == global_role["id"], \
            "老剧本必须认领全局角色，而不是新建一份"

        # 再读一次：cast 不变（认领只写一次，不是每次读都改数据）
        code, d2 = _req(base_url + f"/api/scripts/{rec['id']}")
        assert d2["cast"] == d1["cast"]
        stored = next(s for s in P._load("scripts.json") if s["id"] == rec["id"])
        assert stored["cast"] == d1["cast"]
        assert "cast_isolated" not in stored
    finally:
        _req(base_url + f"/api/scripts/{rec['id']}", "DELETE")


def test_legacy_script_does_not_copy_roles_on_recase(base_url):
    """老剧本再走一次「补齐角色」也不该产生专属副本（否则行为悄悄变了）。"""
    v = _mk_voice(base_url, "老剧专用音色")
    code, role = _json_req(base_url + "/api/roles", "POST",
                           {"name": "老角色X", "voice_id": v["id"]})
    assert code in (200, 201), role
    rec = _push_legacy_script("## 第一场\n老角色X：你好。\n新人Y：我也在。")
    try:
        code, cast_res = _json_req(base_url + f"/api/scripts/{rec['id']}/cast", "POST", {})
        assert code in (200, 201), cast_res
        stored = next(s for s in P._load("scripts.json") if s["id"] == rec["id"])
        assert "cast_isolated" not in stored
        new_roles = [r for r in P._load("roles.json")
                     if r["name"] == "新人Y" and r.get("script_id")]
        assert not new_roles, "老剧本补齐的新角色应进全局库，而不是变成专属副本"
    finally:
        _req(base_url + f"/api/scripts/{rec['id']}", "DELETE")


# ---------------------------------------------------------------- 重名作用域
def test_role_name_collision_is_scoped_by_script(base_url):
    v = _mk_voice(base_url, "重名测试音色")
    # 归属必须是真实剧本（后端会校验），否则建出来的是谁也认领不了的孤儿
    sa = _mk_script(base_url, "## 一\n甲：只用来挂角色。", title="作用域A", auto_cast=False)
    sb = _mk_script(base_url, "## 一\n乙：只用来挂角色。", title="作用域B", auto_cast=False)
    try:
        code, r1 = _json_req(base_url + "/api/roles", "POST",
                             {"name": "同名角色", "script_id": sa["id"], "voice_id": v["id"]})
        assert code in (200, 201), r1
        code, err = _json_req(base_url + "/api/roles", "POST",
                              {"name": "同名角色", "script_id": sa["id"], "voice_id": v["id"]})
        assert code == 400, err
        assert "同名角色" in str(err.get("detail", "")), err

        code, r2 = _json_req(base_url + "/api/roles", "POST",
                             {"name": "同名角色", "script_id": sb["id"], "voice_id": v["id"]})
        assert code in (200, 201), r2
        assert r2["id"] != r1["id"]

        # 不存在的剧本不能挂角色
        code, err2 = _json_req(base_url + "/api/roles", "POST",
                               {"name": "孤儿角色", "script_id": "s_not_exist",
                                "voice_id": v["id"]})
        assert code == 400, err2

        # 全局作用域独立：没同名全局角色时能建，建完再建就冲突
        code, g1 = _json_req(base_url + "/api/roles", "POST",
                             {"name": "全局独有名", "voice_id": v["id"]})
        assert code in (200, 201), g1
        code, err3 = _json_req(base_url + "/api/roles", "POST",
                               {"name": "全局独有名", "voice_id": v["id"]})
        assert code == 400, err3
    finally:
        for s in (sa, sb):
            _req(base_url + f"/api/scripts/{s['id']}", "DELETE")


def test_rename_collision_is_scoped_by_script(base_url):
    """改名查重只看同一个归属：别的剧本的「旁白」不该拦住这次改名。"""
    v = _mk_voice(base_url, "改名作用域音色")
    sa = _mk_script(base_url, "## 一\n甲：x。", title="改名A", auto_cast=False)
    sb = _mk_script(base_url, "## 一\n乙：y。", title="改名B", auto_cast=False)
    try:
        code, r1 = _json_req(base_url + "/api/roles", "POST",
                             {"name": "改名甲", "script_id": sa["id"], "voice_id": v["id"]})
        assert code in (200, 201), r1
        code, r2 = _json_req(base_url + "/api/roles", "POST",
                             {"name": "改名乙", "script_id": sa["id"], "voice_id": v["id"]})
        assert code in (200, 201), r2

        # 同一剧本内改成重名 → 400
        code, err = _json_req(base_url + f"/api/roles/{r2['id']}", "PUT", {"name": "改名甲"})
        assert code == 400, err

        # 另一个剧本里已有同名角色，也不影响这里改名（不同 scope）
        code, r3 = _json_req(base_url + "/api/roles", "POST",
                             {"name": "改名丙", "script_id": sb["id"], "voice_id": v["id"]})
        assert code in (200, 201), r3
        code, renamed = _json_req(base_url + f"/api/roles/{r2['id']}", "PUT",
                                  {"name": "改名丁"})
        assert code == 200, renamed
        assert renamed["name"] == "改名丁"
    finally:
        for s in (sa, sb):
            _req(base_url + f"/api/scripts/{s['id']}", "DELETE")


# ---------------------------------------------------------------- 删除清理
def test_deleting_script_purges_its_own_roles(base_url):
    """删剧本要连带删它的专属角色：剧本没了，这些角色永远用不上。"""
    s = _mk_script(base_url, "## 第一场\n旁白：跟着剧本一起删。", title="级联删除剧本")
    d = _req(base_url + f"/api/scripts/{s['id']}")[1]
    rid = _line_of(d, "旁白")["role_id"]
    voice_id = _line_of(d, "旁白")["voice_id"]
    assert any(r["id"] == rid for r in P._load("roles.json"))

    code, _ = _req(base_url + f"/api/scripts/{s['id']}", "DELETE")
    assert code == 200
    assert not any(r["id"] == rid for r in P._load("roles.json")), \
        "专属角色应随剧本一起删除"
    # 音色不跟着删：可能被别的角色复用，留给用户在音色库里决定
    assert any(v["id"] == voice_id for v in P._load("voices.json"))


def test_delete_role_cleans_cast(base_url):
    s = _mk_script(base_url, "## 第一场\n旁白：会被删掉的角色。", title="删除清理剧本")
    try:
        d = _req(base_url + f"/api/scripts/{s['id']}")[1]
        rid = _line_of(d, "旁白")["role_id"]
        assert rid in d["cast"], d["cast"]

        code, res = _req(base_url + f"/api/roles/{rid}", "DELETE")
        assert code == 200
        assert res.get("cleaned") == 1, res      # 剧本 cast 里正好引用了这一条

        d2 = _req(base_url + f"/api/scripts/{s['id']}")[1]
        assert rid not in d2["cast"], "删角色后剧本 cast 里不能留悬空 id"
        # 同名全局角色存在时会回退到它（手工建的全局角色要能用在剧本里），
        # 这里只要求「不能再指回被删的那个」
        assert _line_of(d2, "旁白")["role_id"] != rid
        stored = next(x for x in P._load("scripts.json") if x["id"] == s["id"])
        assert rid not in (stored.get("cast") or [])
    finally:
        _req(base_url + f"/api/scripts/{s['id']}", "DELETE")
