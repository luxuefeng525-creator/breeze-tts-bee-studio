# coding: utf-8
"""角色参考图上传：原型、授权门槛、存储与路径安全。

R-01（参考图存储）/ R-04（肖像授权）的回归护栏。所有断言都打真实
HTTP 接口，不看前端提示。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

import panel as P

# 1x1 透明 PNG，仅用于验证文件头识别与回读一致性
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000100ffff03000006000557bfabd4000000"
    "0049454e44ae426082"
)


@pytest.fixture(scope="module")
def data_isolation(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("role-ref-data")
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


def _http(req, timeout=30):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def _json_req(url, method, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method=method,
        headers={"Content-Type": "application/json"})
    code, raw, _ = _http(req)
    try:
        return code, json.loads(raw or b"{}")
    except Exception:
        return code, {}


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----beetest"
    out = b""
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    for k, (fname, data) in files.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"{k}\"; filename=\"{fname}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode()
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


def _upload(base_url, rid, data, consent="true", fname="face.png"):
    body, ctype = _multipart(
        {"role_id": rid, "consent": consent}, {"file": (fname, data)})
    req = urllib.request.Request(
        base_url + "/api/refs/upload", data=body, method="POST",
        headers={"Content-Type": ctype})
    code, raw, _ = _http(req)
    try:
        return code, json.loads(raw or b"{}")
    except Exception:
        return code, {"detail": raw[:200].decode("utf-8", "replace")}


def _mk_role(base_url, name, **extra):
    payload = {"name": name}
    payload.update(extra)
    code, r = _json_req(base_url + "/api/roles", "POST", payload)
    assert code in (200, 201), r
    return r


def _roles(base_url):
    code, raw, _ = _http(base_url + "/api/roles")
    assert code == 200
    return json.loads(raw or b"[]")


def test_archetype_is_stored_for_young_and_old(base_url):
    r = _mk_role(base_url, "奶奶", archetype="elder_f")
    assert r["archetype"] == "elder_f"
    found = next(x for x in _roles(base_url) if x["id"] == r["id"])
    assert found["archetype"] == "elder_f"


def test_unknown_archetype_is_rejected(base_url):
    code, res = _json_req(base_url + "/api/roles", "POST",
                          {"name": "错原型", "archetype": "elder_x"})
    assert code == 400
    assert "不认识的原型" in res.get("detail", "")


def test_upload_requires_consent(base_url):
    r = _mk_role(base_url, "无授权")
    code, res = _upload(base_url, r["id"], PNG_1PX, consent="false")
    assert code == 400
    assert "授权" in res.get("detail", "")
    # 没授权就不该落盘
    folder = P.REFS / "characters" / r["id"]
    assert not folder.exists() or not any(folder.iterdir())


def test_upload_rejects_non_image(base_url):
    r = _mk_role(base_url, "假图")
    code, res = _upload(base_url, r["id"], b"not an image at all")
    assert code == 400
    assert "PNG" in res.get("detail", "")


def test_upload_stores_serves_and_records_consent(base_url):
    r = _mk_role(base_url, "有脸")
    code, res = _upload(base_url, r["id"], PNG_1PX)
    assert code == 200, res
    url = res["ref_image"]
    assert url.startswith(f"/api/refs/characters/{r['id']}/")

    code, raw, headers = _http(base_url + url)
    assert code == 200
    assert headers.get("Content-Type", "").startswith("image/png")
    assert raw == PNG_1PX

    role = next(x for x in _roles(base_url) if x["id"] == r["id"])
    assert role["ref_image"] == url
    assert role["ref_consent_at"]
    assert role["ref_consent_ver"] == "v1"


def test_upload_overwrites_previous_face(base_url):
    r = _mk_role(base_url, "换脸")
    assert _upload(base_url, r["id"], PNG_1PX)[0] == 200
    folder = P.REFS / "characters" / r["id"]
    first = sorted(p.name for p in folder.iterdir())
    assert len(first) == 1
    assert _upload(base_url, r["id"], PNG_1PX)[0] == 200
    second = sorted(p.name for p in folder.iterdir())
    assert second == first  # 同内容同哈希，不产生第二份


def test_refs_path_traversal_is_blocked(base_url):
    req = urllib.request.Request(base_url + "/api/refs/../../panel.py")
    code, _raw, _h = _http(req, timeout=10)
    assert code in (400, 404)


# ---- 本地出片：/api/refs/... → file: 绝对路径（router._ref_image_local） ----

def _mk_local_role(tmp_path, monkeypatch, ref_image=None, make_file=False):
    """造一个角色记录与可选的真实图片文件，返回 (role, data_dir)。"""
    import api.router as R

    data_dir = tmp_path / "data"
    monkeypatch.setenv("BREEZE_DATA", str(data_dir))
    role = {"id": "r_test01", "name": "测试角色", "ref_image": ref_image or ""}
    if make_file and ref_image:
        rel = ref_image[len("/api/refs/"):] if ref_image.startswith("/api/refs/") else ref_image
        p = data_dir / "refs" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(PNG_1PX)
    return role, data_dir


def test_ref_image_local_returns_absolute_file_path(tmp_path, monkeypatch):
    from api.router import _ref_image_local

    rel = "/api/refs/characters/r_test01/abc123.png"
    role, data_dir = _mk_local_role(tmp_path, monkeypatch, rel, make_file=True)
    got = _ref_image_local(role)
    assert got.startswith("file:")
    p = __import__("pathlib").Path(got[5:])
    assert p.is_file()
    assert p.parent == (data_dir / "refs" / "characters" / "r_test01")


def test_ref_image_local_rejects_role_without_image(tmp_path, monkeypatch):
    from api.providers import ProviderError
    from api.router import _ref_image_local

    role, _ = _mk_local_role(tmp_path, monkeypatch)
    with pytest.raises(ProviderError) as ei:
        _ref_image_local(role)
    assert "还没有参考图" in str(ei.value)


def test_ref_image_local_reports_missing_file(tmp_path, monkeypatch):
    from api.providers import ProviderError
    from api.router import _ref_image_local

    role, _ = _mk_local_role(tmp_path, monkeypatch,
                             "/api/refs/characters/r_test01/gone.png")
    with pytest.raises(ProviderError) as ei:
        _ref_image_local(role)
    assert "参考图文件不存在" in str(ei.value)
