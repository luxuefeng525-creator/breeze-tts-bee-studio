"""W2-6 分段流式合成 + 真实进度的后端测试

覆盖：
  * /api/synthesize/stream 按 save_as 落盘（供前端分段渐进合成合并）
  * /api/concat 合并多段 → 单条历史
  * 流式响应体就是 PCM s16le（前端据此算字节级真实进度）
"""
from __future__ import annotations

import json
import struct
import threading
import urllib.request
import uuid

import pytest

import panel as P


@pytest.fixture(scope="module")
def data_isolation(tmp_path_factory):
    """测试数据与生产数据隔离（不得污染 data/history.json）。"""
    tmp = tmp_path_factory.mktemp("progress-data")
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


def _post_stream(base_url, text, save_as):
    """POST 流式端点，返回 (状态码, PCM 字节, 响应头)。"""
    fd = uuid.uuid4().hex
    body = (
        f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n{text}\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"instruction\"\r\n\r\n沉稳旁白\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n4\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"seed\"\r\n\r\n42\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"save_as\"\r\n\r\n{save_as}\r\n"
        f"--{fd}--\r\n"
    ).encode()
    req = urllib.request.Request(
        base_url + "/api/synthesize/stream", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={fd}")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        if e.code == 409:
            pytest.skip("上游 7860 忙，跳过本用例")
        return e.code, b"", {}


def test_stream_endpoint_streams_pcm_bytes(base_url):
    """流式端点返回的是 PCM s16le 字节（前端据此算真实字节级进度）。"""
    code, _ = _req_model(base_url)
    if code != 200:
        pytest.skip("API 7860 offline")
    status, data, headers = _post_stream(base_url, "流式字节测试", "t_seg_0.wav")
    assert status == 200, (status, data[:200])
    assert headers.get("X-Sample-Rate") == "24000", headers
    assert headers.get("X-Sample-Format") == "s16le", headers
    assert len(data) > 2000, f"流式返回过短：{len(data)}"
    assert len(data) % 2 == 0, "PCM 必须是偶数字节"


def test_stream_endpoint_saves_file_for_concat(base_url):
    """save_as 指定的文件必须落盘，供 /api/concat 合并。"""
    code, _ = _req_model(base_url)
    if code != 200:
        pytest.skip("API 7860 offline")
    name = "t_seg_save.wav"
    status, data, _ = _post_stream(base_url, "落盘测试", name)
    assert status == 200
    fp = P.AUDIOS / name
    assert fp.is_file(), "save_as 指定的文件未落盘"
    raw = fp.read_bytes()
    assert raw[:4] == b"RIFF", "落盘文件不是 WAV"
    # WAV 头 44 字节后应是 PCM，长度与流式字节一致
    assert len(raw) - 44 == len(data), (len(raw), len(data))


def test_concat_merges_segments_into_one_history(base_url):
    """多段合并成单文件，且只落一条历史。"""
    code, _ = _req_model(base_url)
    if code != 200:
        pytest.skip("API 7860 offline")
    n0 = len(_history(base_url))
    names = []
    for i, t in enumerate(["合并测试第一段", "合并测试第二段"]):
        name = f"t_cat_{i}.wav"
        status, _, _ = _post_stream(base_url, t, name)
        assert status == 200
        names.append(name)

    req = urllib.request.Request(
        base_url + "/api/concat",
        data=json.dumps({"files": names, "text": "合并测试",
                          "gaps": [0.4]}).encode(),
        method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as r:
        res = json.loads(r.read())
    assert res.get("wav_url", "").startswith("/api/audio/")
    assert res["duration"] > 0.5
    n1 = len(_history(base_url))
    assert n1 == n0 + 1, f"合并应只落一条历史：{n0} -> {n1}"


# ---- helpers ----
def _req_model(base_url):
    try:
        with urllib.request.urlopen(base_url + "/api/model", timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except Exception:
        return 0, {}


def _history(base_url):
    try:
        with urllib.request.urlopen(base_url + "/api/history", timeout=10) as r:
            return json.loads(r.read() or b"[]")
    except Exception:
        return []
