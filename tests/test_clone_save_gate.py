"""W2-7 克隆保存闸门回归测试

覆盖「草稿态创建 → 定稿保存」的新语义：
  * POST /api/voices            允许克隆音色的文稿为空（草稿态）
  * PUT  /api/voices/{id}       定稿时必须补齐文稿（闸门在这里）
  * 合成路径拦截占位符文稿（否则克隆会静默劣化，表现为"不像"）
"""
from __future__ import annotations

import json
import math
import struct
import threading
import urllib.request
import uuid

import pytest

import panel as P

CRLF = "\r\n"


@pytest.fixture(scope="module")
def data_isolation(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("clone-data")
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
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _req(url, method="GET", body=None, ctype=None, timeout=30):
    req = urllib.request.Request(url, data=body, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    """构造 multipart 请求体（files: {name: (filename, bytes)}）。"""
    b = uuid.uuid4().hex
    parts: list[bytes] = []
    for k, v in fields.items():
        parts.append(
            f"--{b}{CRLF}"
            f'Content-Disposition: form-data; name="{k}"{CRLF}{CRLF}'
            f"{v}{CRLF}".encode("utf-8")
        )
    for k, (fname, data) in files.items():
        parts.append(
            f"--{b}{CRLF}"
            f'Content-Disposition: form-data; name="{k}"; filename="{fname}"{CRLF}'
            f"Content-Type: application/octet-stream{CRLF}{CRLF}".encode("utf-8")
            + data + CRLF.encode()
        )
    parts.append(f"--{b}--{CRLF}".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def _sine_wav(seconds: float = 3.0) -> bytes:
    pcm = b"".join(struct.pack("<h", int(9000 * math.sin(i / 10)))
                   for i in range(int(24000 * seconds)))
    return P.pcm_to_wav(pcm, normalize=False)


# ---------------------------------------------------------------- 草稿态 / 定稿态

def test_create_clone_voice_without_transcript_is_allowed(base_url):
    """草稿态：创建克隆音色允许文稿为空（上传在前、校对在后）。"""
    code, res = _req(base_url + "/api/voices", "POST", json.dumps({
        "name": "草稿克隆", "type": "clone", "ref_text": "",
    }).encode(), "application/json")
    assert code in (200, 201), res
    assert res["id"].startswith("v_")
    _req(base_url + "/api/voices/" + res["id"], "DELETE")


def test_finalize_clone_voice_without_transcript_is_rejected(base_url):
    """定稿闸门：文稿为空时保存必须被拦，且提示可执行。"""
    code, res = _req(base_url + "/api/voices", "POST", json.dumps({
        "name": "待补文稿", "type": "clone", "ref_text": "",
    }).encode(), "application/json")
    assert code in (200, 201), res
    vid = res["id"]

    code, err = _req(base_url + "/api/voices/" + vid, "PUT", json.dumps({
        "name": "待补文稿", "type": "clone", "ref_text": "",
    }).encode(), "application/json")
    assert code == 400, err
    assert "文字稿" in err.get("detail", ""), err

    _req(base_url + "/api/voices/" + vid, "DELETE")


def test_finalize_clone_voice_with_transcript_succeeds(base_url):
    """补齐文稿后保存成功。"""
    code, res = _req(base_url + "/api/voices", "POST", json.dumps({
        "name": "可保存克隆", "type": "clone", "ref_text": "",
    }).encode(), "application/json")
    assert code in (200, 201), res
    vid = res["id"]

    code, saved = _req(base_url + "/api/voices/" + vid, "PUT", json.dumps({
        "name": "可保存克隆", "type": "clone",
        "ref_text": "你要相信，你会越来越好。",
    }).encode(), "application/json")
    assert code == 200, saved
    assert saved["ref_text"] == "你要相信，你会越来越好。"

    _req(base_url + "/api/voices/" + vid, "DELETE")


def test_design_voice_needs_no_transcript(base_url):
    """设计音色不需要参考音频与文稿。"""
    code, res = _req(base_url + "/api/voices", "POST", json.dumps({
        "name": "纯设计音色", "type": "design",
        "instruction": "一位温柔自信的年轻女性", "ref_text": "",
    }).encode(), "application/json")
    assert code in (200, 201), res
    vid = res["id"]

    code, saved = _req(base_url + "/api/voices/" + vid, "PUT", json.dumps({
        "name": "纯设计音色", "type": "design",
        "instruction": "一位温柔自信的年轻女性", "ref_text": "",
    }).encode(), "application/json")
    assert code == 200, saved

    _req(base_url + "/api/voices/" + vid, "DELETE")


# ---------------------------------------------------------------- 占位符守卫

def test_placeholder_transcript_blocks_synthesis(base_url):
    """占位符文稿必须在合成前被点名拦截（否则克隆静默劣化，表现为"不像"）。"""
    code, res = _req(base_url + "/api/voices", "POST", json.dumps({
        "name": "占位符测试", "type": "clone", "ref_text": "参考文字稿",
    }).encode(), "application/json")
    assert code in (200, 201), res
    vid = res["id"]

    body, ctype = _multipart(
        {"ref_audio": None} if False else {},
        {"ref_audio": ("r.wav", _sine_wav(3.0))},
    )
    code, upres = _req(base_url + f"/api/voices/{vid}/ref", "POST", body, ctype)
    assert code == 200, upres

    body, ctype = _multipart(
        {"text": "占位符拦截测试", "voice_id": vid}, {},
    )
    code, err = _req(base_url + "/api/synthesize", "POST", body, ctype, timeout=60)
    assert code == 400, err
    assert "占位符" in err.get("detail", ""), err

    _req(base_url + "/api/voices/" + vid, "DELETE")


def _multipart_fields(fields):
    """按字段拼 multipart 请求体。"""
    b = uuid.uuid4().hex
    crlf = "\r\n"
    parts = []
    for k, v in fields.items():
        parts.append(f"--{b}{crlf}Content-Disposition: form-data; name=\"{k}\"{crlf}{crlf}{v}{crlf}")
    parts.append(f"--{b}--{crlf}")
    return "".join(parts).encode(), f"multipart/form-data; boundary={b}"


def test_role_without_voice_is_rejected(base_url):
    """角色没绑音色时必须明确拒绝，不能静默回落到默认发音人。

    回归：用户选「雪峰」点合成，实际听到的是通用音色（cfg_used=1、
    instruction_used 为空），表现就是"角色不能生成语音"，而系统毫无提示。
    三个合成端点都要拦，且错误信息必须点名角色。
    """
    try:
        with urllib.request.urlopen(base_url + "/api/model", timeout=5):
            pass
    except Exception:
        pytest.skip("面板不在线")

    code, role = _req(base_url + "/api/roles", "POST", json.dumps({
        "name": "无音色角色", "description": "回归用例",
    }).encode(), "application/json")
    assert code in (200, 201), role
    rid = role["id"]
    assert not (role.get("voice_id") or "").strip()

    try:
        # 三个端点都要拦（守卫在转发之前返回，不会真的连上游）
        for path in ("/api/synthesize", "/api/synthesize/stream",
                     "/api/synthesize/batch"):
            body, ctype = _multipart_fields({
                "text": "角色守卫测试",
                "texts": "[\"角色守卫测试\"]",
                "role_id": rid,
            })
            code, err = _req(base_url + path, "POST", body, ctype, timeout=60)
            assert code == 400, (path, code, err)
            detail = str(err.get("detail", ""))
            assert "还没绑定音色" in detail and "无音色角色" in detail, (path, detail)

        # 指向已删除的角色同样要拦
        body, ctype = _multipart_fields({"text": "角色守卫测试", "role_id": "r_nonexistent"})
        code, err = _req(base_url + "/api/synthesize", "POST", body, ctype, timeout=60)
        assert code == 400, (code, err)
        assert "角色已不存在" in str(err.get("detail", "")), err

        # 显式 voice_id 优先：角色没绑音色也必须放行（自带音色，不依赖已有数据）
        code, v = _req(base_url + "/api/voices", "POST", json.dumps({
            "name": "守卫测试音色", "type": "design", "instruction": "沉稳男声",
        }).encode(), "application/json")
        assert code in (200, 201), v
        try:
            body, ctype = _multipart_fields({
                "text": "显式音色优先", "role_id": rid, "voice_id": v["id"],
                "instruction": "沉稳旁白", "cfg_scale": "4", "seed": "42",
            })
            code, res = _req(base_url + "/api/synthesize", "POST", body, ctype, timeout=300)
            assert code == 200, (code, res)
            assert res.get("wav_url"), res
        finally:
            _req(base_url + "/api/voices/" + v["id"], "DELETE")
    finally:
        _req(base_url + "/api/roles/" + rid, "DELETE")
