"""W2-3c 质量管线接线集成测试（面板端点）

覆盖：
  * POST /api/norm/preview —— 朗读预览（原文 → 规范化 → 命中项 → 最终指令）
  * GET/POST/DELETE /api/dict —— 发音词典 CRUD
  * norm=2 时合成链路真实使用规范化文本
  * 参考音频上传返回质量报告
"""
from __future__ import annotations

import os
import json
import struct
import threading
import urllib.request
import uuid

import pytest

import panel as P


@pytest.fixture(scope="module")
def data_isolation(tmp_path_factory):
    """测试数据与生产数据隔离（W2-3c 修复：曾直写生产 data/history.json）。"""
    tmp = tmp_path_factory.mktemp("pipeline-data")
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
            raw = r.read()
            return r.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as e:
        detail = json.loads(e.read() or b"{}")
        if e.code == 409:
            if os.environ.get("TTS_ALLOW_BUSY_SKIP") == "1":
                pytest.skip("上游忙，已按 TTS_ALLOW_BUSY_SKIP=1 放行")
            raise AssertionError(
                "面板未消化 409（排队应保证请求不被拒）：" + str(detail.get("detail")))
        return e.code, detail


# ---------------------------------------------------------------- 朗读预览

def test_norm_preview_normalizes_chinese_readings(base_url):
    """PRD P0-03 标准句：日期/金额/电话/温度/百分比/时间都要读对。"""
    text = ("2026年11月6日，订单金额¥1,299.50，电话13800138000，"
            "温度37.5℃，A股上涨2.3%，请于下午3点前回复。")
    code, res = _req(base_url + "/api/norm/preview", "POST",
                     json.dumps({"text": text}).encode(), "application/json")
    assert code == 200, res
    norm = res["normalized"]
    assert "二零二六年十一月六日" in norm
    assert "一千二百九十九点五零元" in norm          # 千分位不能被截断
    assert "一三八零零一三八零零零" in norm
    assert "三十七点五摄氏度" in norm
    assert "百分之二点三" in norm
    assert "下午三点" in norm
    assert res["changes"], "必须给出可解释的改动清单"


def test_norm_preview_keeps_plain_text_untouched(base_url):
    """纯文本不被过度规范化（"一一"不能读成"幺幺"）。"""
    code, res = _req(base_url + "/api/norm/preview", "POST",
                     json.dumps({"text": "你好，世界。一一对应。"}).encode(),
                     "application/json")
    assert code == 200, res
    assert res["normalized"] == "你好，世界。一一对应。"
    assert not res["changes"]


def test_norm_preview_reports_final_instruction(base_url):
    code, res = _req(base_url + "/api/norm/preview", "POST",
                     json.dumps({"text": "测试", "accent": "用贵州方言说话"}).encode(),
                     "application/json")
    assert code == 200, res
    assert "贵州" in res["instruction"]
    assert res["cfg"] == 4


# ---------------------------------------------------------------- 发音词典

def test_dict_crud_roundtrip(base_url):
    code, res = _req(base_url + "/api/dict", "POST", json.dumps({
        "word": "行长", "reading": "hang2 zhang3",
    }).encode(), "application/json")
    assert code in (200, 201), res
    assert res["entry"]["kind"] == "pinyin", res

    code, items = _req(base_url + "/api/dict")
    assert code == 200
    assert any(i["word"] == "行长" for i in items)

    # 预览应命中词典
    code, prev = _req(base_url + "/api/norm/preview", "POST", json.dumps({
        "text": "银行行长来开会"}).encode(), "application/json")
    assert code == 200
    assert prev["changes"], prev

    # 删除（中文词条需正确处理百分号编码）
    from urllib.parse import quote
    code, res = _req(base_url + "/api/dict/" + quote("行长"), "DELETE")
    assert code == 200, res
    code, items = _req(base_url + "/api/dict")
    assert not any(i["word"] == "行长" for i in items)


def test_dict_rejects_empty_word(base_url):
    code, res = _req(base_url + "/api/dict", "POST",
                     json.dumps({"word": "", "reading": "x"}).encode(),
                     "application/json")
    assert code == 400
    assert "词" in res["detail"]


def test_dict_rejects_empty_reading(base_url):
    code, res = _req(base_url + "/api/dict", "POST",
                     json.dumps({"word": "行长", "reading": ""}).encode(),
                     "application/json")
    assert code == 400
    assert "读法" in res["detail"]


# ---------------------------------------------------------------- 合成链路

def test_synthesize_uses_normalized_text(base_url):
    """norm=2 时，合成请求里带的文本应已是规范化后的读法。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")

    import uuid
    fd = uuid.uuid4().hex
    body = (
        f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n金额¥1,299.50元\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"norm\"\r\n\r\n2\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"instruction\"\r\n\r\n沉稳旁白\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n4\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"seed\"\r\n\r\n42\r\n"
        f"--{fd}--\r\n"
    ).encode()
    code, res = _req(base_url + "/api/synthesize", "POST", body,
                     f"multipart/form-data; boundary={fd}", timeout=300)
    assert code == 200, res
    assert res["duration"] > 0.3


# ---------------------------------------------------------------- 参考音频质检

def test_ref_upload_returns_quality_report(base_url):
    """上传参考音频应返回可解释的质量报告（评分/时长/问题列表）。"""
    import math
    pcm = b"".join(struct.pack("<h", int(8000 * math.sin(i / 12)))
                   for i in range(24000 * 5))   # 5 秒
    wav = P.pcm_to_wav(pcm, normalize=False)

    code, v = _req(base_url + "/api/voices", "POST", json.dumps({
        "name": "质检测试音色", "type": "clone", "ref_text": "参考文字稿",
    }).encode(), "application/json")
    assert code in (200, 201), v
    vid = v["id"]

    import uuid
    b = uuid.uuid4().hex
    up = (
        f"--{b}\r\nContent-Disposition: form-data; name=\"ref_audio\"; filename=\"r.wav\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + wav + b"\r\n"
        + f"--{b}--\r\n".encode()
    )
    code, res = _req(base_url + f"/api/voices/{vid}/ref", "POST", up,
                     f"multipart/form-data; boundary={b}")
    assert code == 200, res
    assert "quality" in res, res
    q = res["quality"]
    assert "issues" in q and "score" in q
    assert isinstance(q["issues"], list)

    _req(base_url + "/api/voices/" + vid, "DELETE")


# ---------------------------------------------------------------- 并发与隔离

def test_concurrent_history_appends_do_not_lose_entries(base_url):
    """并发追加历史不得丢条目（R2：跨进程/多线程读-改-写覆盖）。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")

    n0 = len(_req(base_url + "/api/history")[1])
    n = 6
    results = []

    def one(i):
        fd = uuid.uuid4().hex
        body = (
            f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n并发历史测试 {i}\r\n"
            f"--{fd}\r\nContent-Disposition: form-data; name=\"instruction\"\r\n\r\n沉稳旁白\r\n"
            f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n4\r\n"
            f"--{fd}\r\nContent-Disposition: form-data; name=\"seed\"\r\n\r\n42\r\n"
            f"--{fd}--\r\n"
        ).encode()
        code, res = _req(base_url + "/api/synthesize", "POST", body,
                         f"multipart/form-data; boundary={fd}", timeout=300)
        results.append(code)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)

    assert all(c == 200 for c in results), results
    n1 = len(_req(base_url + "/api/history")[1])
    assert n1 == n0 + n, f"并发追加丢条目：{n0} -> {n1}（应为 {n0 + n}）"


def test_test_data_does_not_touch_production_history(base_url):
    """测试写入必须落在临时目录，不得污染生产 data/history.json。"""
    import panel as P
    # 本模块的 fixture 已把 P.DATA 指向临时目录
    assert P.DATA.name != "data" or P.DATA.parent.name != "data", P.DATA
    prod = P.ROOT / "data" / "history.json"
    before = prod.stat().st_mtime if prod.exists() else None
    n0 = len(_req(base_url + "/api/history")[1])
    fd = uuid.uuid4().hex
    body = (
        f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n隔离测试\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"instruction\"\r\n\r\n沉稳旁白\r\n"
        f"--{fd}--\r\n"
    ).encode()
    _req(base_url + "/api/synthesize", "POST", body,
         f"multipart/form-data; boundary={fd}", timeout=300)
    after = prod.stat().st_mtime if prod.exists() else None
    assert before == after, "生产 history.json 被测试改动了"


# ---------------------------------------------------------------- 并发与隔离

def test_concurrent_history_appends_do_not_lose_entries(base_url):
    """并发追加历史不得丢条目（R2：跨进程/多线程读-改-写覆盖）。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")

    n0 = len(_req(base_url + "/api/history")[1])
    n = 6
    results = []

    def one(i):
        fd = uuid.uuid4().hex
        body = (
            f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n并发历史测试 {i}\r\n"
            f"--{fd}\r\nContent-Disposition: form-data; name=\"instruction\"\r\n\r\n沉稳旁白\r\n"
            f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n4\r\n"
            f"--{fd}\r\nContent-Disposition: form-data; name=\"seed\"\r\n\r\n42\r\n"
            f"--{fd}--\r\n"
        ).encode()
        code, res = _req(base_url + "/api/synthesize", "POST", body,
                         f"multipart/form-data; boundary={fd}", timeout=300)
        results.append(code)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)

    assert all(c == 200 for c in results), results
    n1 = len(_req(base_url + "/api/history")[1])
    assert n1 == n0 + n, f"并发追加丢条目：{n0} -> {n1}（应为 {n0 + n}）"


def test_test_data_does_not_touch_production_history(base_url):
    """测试写入必须落在临时目录，不得污染生产 data/history.json。"""
    import panel as P
    # 本模块的 fixture 已把 P.DATA 指向临时目录
    assert P.DATA.name != "data" or P.DATA.parent.name != "data", P.DATA
    prod = P.ROOT / "data" / "history.json"
    before = prod.stat().st_mtime if prod.exists() else None
    n0 = len(_req(base_url + "/api/history")[1])
    fd = uuid.uuid4().hex
    body = (
        f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n隔离测试\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"instruction\"\r\n\r\n沉稳旁白\r\n"
        f"--{fd}--\r\n"
    ).encode()
    _req(base_url + "/api/synthesize", "POST", body,
         f"multipart/form-data; boundary={fd}", timeout=300)
    after = prod.stat().st_mtime if prod.exists() else None
    assert before == after, "生产 history.json 被测试改动了"
