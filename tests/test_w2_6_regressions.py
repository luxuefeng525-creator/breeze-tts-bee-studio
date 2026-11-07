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


def _blocking_json(base_url, text: str, **extra) -> dict:
    """非流式合成的完整响应（含 text_used / engine_note 等契约字段）。"""
    fields = {"text": text, "instruction": "沉稳旁白",
              "cfg_scale": "4", "seed": "42", "no_history": "1"}
    fields.update(extra)
    body, ctype = _mp(fields)
    req = urllib.request.Request(base_url + "/api/synthesize",
                                 data=body, method="POST")
    req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 409:
            if os.environ.get("TTS_ALLOW_BUSY_SKIP") == "1":
                pytest.skip("上游忙，已按 TTS_ALLOW_BUSY_SKIP=1 放行")
            raise AssertionError("面板未消化 409（排队应保证请求不被拒）")
        raise


def test_norm_applied_on_blocking_path(base_url):
    """非流式路由也必须应用 norm=2（回归：曾漏接线，规范化静默不生效）。

    断言口径（2026-11-07 重校准）：原先用「两次合成音频时长差 > 15%」
    间接推断规范化是否接线，这个口径绑定了具体引擎的读法与采样随机性。
    换到 Qwen3-TTS 后随机采样使时长差落到 13.6%，断言误报。
    改为直接断言面板回报的 ``text_used``——即真正下发给上游的文本：
    norm=0 必须原样，norm=2 必须被规范化管线改写。这是引擎无关的
    确定性契约断言，同时仍要求两次都产出有效音频，保留端到端覆盖。
    """
    if not _api_online(base_url):
        pytest.skip("API 7860 offline")
    text = "订单金额¥1,299.50元"

    plain = _blocking_json(base_url, text, norm="0")
    normalized = _blocking_json(base_url, text, norm="2")
    assert plain.get("duration", 0) > 0.2, plain
    assert normalized.get("duration", 0) > 0.2, normalized
    assert plain.get("text_used") == text, (
        f"norm=0 不应改写文本，实际下发 {plain.get('text_used')!r}")
    assert normalized.get("text_used") != text, (
        "norm=2 似乎未生效：下发给上游的文本与原文本完全相同")
    # 规范化把阿拉伯数字金额改写成中文读法，且与预览端结果一致
    assert "一千二百九十九" in normalized["text_used"], normalized["text_used"]
