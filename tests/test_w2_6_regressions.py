"""W2-6 基准暴露的两个真 bug 的回归测试

1) 指令重复拼接：流式路由里 `merged` 在无额外指令时退化成 ins 本身，
   又被拼到 ins 后面 →「沉稳旁白，沉稳旁白」→ 模型多生成 42% 音频。
   症状是"流式总耗时比非流式高"，靠看代码很难发现。

2) norm=2 在非流式路由漏接线：文本规范化只在预览与流式端点生效，
   走 /api/synthesize 时静默不生效。

两条都用"可观测的生成结果差异"来断言，而不是断言内部实现。
"""
from __future__ import annotations

import os
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
    tmp = tmp_path_factory.mktemp("w2-6-data")
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


def _mp(fields: dict) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    body = "".join(
        f"--{b}{CRLF}Content-Disposition: form-data; name=\"{k}\"{CRLF}{CRLF}{v}{CRLF}"
        for k, v in fields.items()
    ) + f"--{b}--{CRLF}"
    return body.encode("utf-8"), f"multipart/form-data; boundary={b}"


def _api_online(base_url) -> bool:
    try:
        with urllib.request.urlopen(base_url + "/api/model", timeout=5):
            return True
    except Exception:
        return False


def _stream_bytes(base_url, text: str, **extra) -> int:
    fields = {"text": text, "instruction": "沉稳旁白",
              "cfg_scale": "4", "seed": "42"}
    fields.update(extra)
    body, ctype = _mp(fields)
    req = urllib.request.Request(base_url + "/api/synthesize/stream",
                                 data=body, method="POST")
    req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return len(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 409:
            if os.environ.get("TTS_ALLOW_BUSY_SKIP") == "1":
                pytest.skip("上游忙，已按 TTS_ALLOW_BUSY_SKIP=1 放行")
            raise AssertionError("面板未消化 409（排队应保证请求不被拒）")
        raise


def _blocking_duration(base_url, text: str, **extra) -> float:
    fields = {"text": text, "instruction": "沉稳旁白",
              "cfg_scale": "4", "seed": "42", "no_history": "1"}
    fields.update(extra)
    body, ctype = _mp(fields)
    req = urllib.request.Request(base_url + "/api/synthesize",
                                 data=body, method="POST")
    req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return float(json.loads(r.read()).get("duration", 0))
    except urllib.error.HTTPError as e:
        if e.code == 409:
            if os.environ.get("TTS_ALLOW_BUSY_SKIP") == "1":
                pytest.skip("上游忙，已按 TTS_ALLOW_BUSY_SKIP=1 放行")
            raise AssertionError("面板未消化 409（排队应保证请求不被拒）")
        raise


def test_instruction_not_duplicated_between_paths(base_url):
    """流式与非流式对同一文本/参数必须生成等长音频。

    回归：流式路由曾把 instruction 拼成「沉稳旁白，沉稳旁白」，
    导致多生成约 42% 音频、总耗时明显变长。
    """
    if not _api_online(base_url):
        pytest.skip("API 7860 offline")
    text = "指令重复回归测试短句。"

    stream_audio = _stream_bytes(base_url, text) / 48000.0
    block_audio = _blocking_duration(base_url, text)

    assert block_audio > 0.2, block_audio
    ratio = stream_audio / block_audio
    assert 0.75 <= ratio <= 1.33, (
        f"两条路径音频时长差异过大：流式 {stream_audio:.2f}s vs 非流式 {block_audio:.2f}s"
        f"（比值 {ratio:.2f}）——大概率是指令被重复拼接")


def test_norm_applied_on_blocking_path(base_url):
    """非流式路由也必须应用 norm=2（回归：曾漏接线，规范化静默不生效）。

    断言口径：规范化必须产生**可观测的生成差异**。不假设方向——
    原始符号串模型可能逐个念（更慢），规范化后读得流畅（更快）。
    """
    if not _api_online(base_url):
        pytest.skip("API 7860 offline")
    text = "订单金额¥1,299.50元"

    plain = _blocking_duration(base_url, text, norm="0")
    normalized = _blocking_duration(base_url, text, norm="2")
    assert plain > 0.2 and normalized > 0.2
    diff = abs(normalized - plain) / max(plain, normalized)
    assert diff > 0.15, (
        f"norm=2 似乎未生效：norm=0 得到 {plain:.2f}s，norm=2 得到 {normalized:.2f}s"
        f"（差异仅 {diff:.0%}）；规范化会改写读法，音频时长应有可观测变化")
