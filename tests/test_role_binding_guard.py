"""REQ-20261128-01：角色绑定失效必须明确报错，两条合成路径口径一致。"""
from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
import uuid

import panel as P


class _Srv:
    def __init__(self, tmp):
        old = (P.DATA, P.REFS, P.AUDIOS)
        self.old = old
        P.DATA, P.REFS, P.AUDIOS = tmp, tmp / "refs", tmp / "audio"
        for d in (P.DATA, P.REFS, P.AUDIOS):
            d.mkdir(parents=True, exist_ok=True)
        self.server = P.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        P.DATA, P.REFS, P.AUDIOS = self.old

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"


def _req(url, method="GET", body=None, ctype=None):
    r = urllib.request.Request(url, data=body, method=method)
    if ctype:
        r.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {"detail": raw[:200].decode("utf-8", "replace")}


def _multipart(fields: dict) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    out.write(f"--{b}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={b}"


def test_broken_role_binding_reported_on_both_paths(tmp_path):
    srv = _Srv(tmp_path)
    try:
        code, role = _req(srv.base + "/api/roles", "POST", json.dumps(
            {"name": "坏绑定角色", "voice_id": "v_does_not_exist"}).encode(),
            "application/json")
        assert code in (200, 201), role
        rid = role["id"]

        for path in ("/api/synthesize", "/api/synthesize/stream"):
            body, ctype = _multipart({"text": "测试", "role_id": rid})
            code, j = _req(srv.base + path, "POST", body, ctype)
            assert code == 400, (path, j)
            assert "已不存在" in str(j.get("detail")), (path, j)

        # 对照组：绑定有效音色的角色仍可用（有 design 音色则用，否则跳过）
        voices = json.loads((tmp_path / "voices.json").read_text(encoding="utf-8")) \
            if (tmp_path / "voices.json").exists() else []
        if not voices:
            return
        vid = voices[0]["id"]
        code, role2 = _req(srv.base + "/api/roles", "POST", json.dumps(
            {"name": "好绑定角色", "voice_id": vid}).encode(), "application/json")
        assert code in (200, 201), role2
        body, ctype = _multipart({"text": "测试", "role_id": role2["id"]})
        code, j = _req(srv.base + "/api/synthesize", "POST", body, ctype)
        assert code != 400, j  # 不应再被绑定校验拦下（后续可能因上游不可用而失败）
    finally:
        srv.close()
