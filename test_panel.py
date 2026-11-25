"""BreezeTTS Studio 面板测试（环 4）——真实服务、零 mock、数据隔离。"""
from __future__ import annotations

import os
import json
import struct
import threading
import time
import urllib.request
import uuid
from unittest.mock import patch

import pytest

import panel as P


@pytest.fixture(scope="module")
def data_isolation(tmp_path_factory):
    """测试数据与生产数据完全隔离，不污染音色库/历史。"""
    tmp = tmp_path_factory.mktemp("panel-data")
    old = (P.DATA, P.REFS, P.AUDIOS)
    P.DATA, P.REFS, P.AUDIOS = tmp, tmp / "refs", tmp / "audio"
    for d in (P.DATA, P.REFS, P.AUDIOS):
        d.mkdir(parents=True, exist_ok=True)
    try:
        yield
    finally:
        P.DATA, P.REFS, P.AUDIOS = old   # 必须还原：否则后续用例写到临时目录
    P.DATA, P.REFS, P.AUDIOS = old


# ---------- 纯逻辑 ----------
def test_pcm_to_wav_header():
    pcm = b"\x01\x02" * 240
    wav = P.pcm_to_wav(pcm)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    (sr,) = struct.unpack_from("<I", wav, 24)
    assert sr == P.SAMPLE_RATE
    assert len(wav) == 44 + len(pcm)


def test_parse_multipart_fields_and_files():
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n你好\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"ref_audio\"; filename=\"a.wav\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
        + b"RIFFDATA\r\n"
        + f"--{boundary}--\r\n".encode()
    )
    fields, files = P.parse_multipart(body, f"multipart/form-data; boundary={boundary}")
    assert fields["text"] == "你好"
    fname, data = files["ref_audio"]
    assert fname == "a.wav" and data == b"RIFFDATA"


def test_adapter_headers_are_case_insensitive():
    headers = {
        "x-adapter-note": "%E4%BD%BF%E7%94%A8%20CUDA%20Graph",
        "x-adapter-route": "design",
    }

    assert P._header_get(headers, "X-Adapter-Note") == headers["x-adapter-note"]
    assert P._header_get(headers, "X-Adapter-Route") == "design"
    assert P._engine_note(headers) == "使用 CUDA Graph"


def test_upstream_headers_are_lowercased():
    class FakeResponse:
        status = 200
        headers = {
            "Content-Type": "audio/pcm",
            "x-adapter-route": "design",
            "x-adapter-note": "ok",
        }

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b"pcm"

    headers = {}
    with patch.object(P, "urlopen", return_value=FakeResponse()):
        code, body, ctype = P._post_upstream(
            "/v1/audio/speech", b"", "application/octet-stream",
            headers_out=headers,
        )

    assert code == 200 and body == b"pcm" and ctype == "audio/pcm"
    assert headers["x-adapter-route"] == "design"
    assert P._header_get(headers, "X-Adapter-Route") == "design"


# ---------- 面板服务（进程内真实起服） ----------
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
        detail = json.loads(e.read() or b"{}")
        if e.code == 409 and "另一条合成" in str(detail.get("detail", "")):
            # 默认按失败处理：面板本该排队消化 409，漏到客户端说明排队失效。
            # 仅在明确知道有人在用工作室时，用 TTS_ALLOW_BUSY_SKIP=1 显式放行。
            if os.environ.get("TTS_ALLOW_BUSY_SKIP") == "1":
                pytest.skip("上游忙，已按 TTS_ALLOW_BUSY_SKIP=1 放行")
            raise AssertionError(
                "面板未消化 409（排队应保证请求不被拒）：" + str(detail.get("detail")))
        return e.code, detail


def test_index_page(base_url):
    with urllib.request.urlopen(base_url + "/", timeout=10) as r:
        html = r.read().decode("utf-8")
    assert r.status == 200
    assert "BreezeTTS Studio" in html
    assert "音色库" in html and "角色库" in html and "合成历史" in html


def test_voices_crud(base_url):
    code, v = _req(base_url + "/api/voices", "POST", json.dumps({
        "name": "测试音色A", "type": "design", "instruction": "温柔女声", "cfg_scale": 4,
    }).encode(), "application/json")
    assert code == 201 and v["id"].startswith("v_")
    vid = v["id"]

    code, lst = _req(base_url + "/api/voices")
    assert code == 200 and any(x["id"] == vid for x in lst)

    code, v2 = _req(base_url + "/api/voices/" + vid, "PUT", json.dumps({"name": "改名B"}).encode(), "application/json")
    assert code == 200 and v2["name"] == "改名B"

    code, _ = _req(base_url + "/api/voices/" + vid, "DELETE")
    assert code == 200
    code, lst = _req(base_url + "/api/voices")
    assert not any(x["id"] == vid for x in lst)


def test_roles_crud(base_url):
    code, v = _req(base_url + "/api/voices", "POST", json.dumps({
        "name": "角色绑定音色", "type": "design", "instruction": "低沉男声",
    }).encode(), "application/json")
    vid = v["id"]
    code, r = _req(base_url + "/api/roles", "POST", json.dumps({
        "name": "旁白", "voice_id": vid, "defaults": {"cfg_scale": 4, "seed": 7},
    }).encode(), "application/json")
    assert code == 201 and r["id"].startswith("r_")
    rid = r["id"]
    code, r2 = _req(base_url + "/api/roles/" + rid, "PUT", json.dumps({"name": "旁白2"}).encode(), "application/json")
    assert code == 200 and r2["name"] == "旁白2"
    code, _ = _req(base_url + "/api/roles/" + rid, "DELETE")
    assert code == 200
    _req(base_url + "/api/voices/" + vid, "DELETE")


def test_ref_upload_and_synthesize(base_url):
    """真实链路：上传参考音频→建 clone 音色→合成（需 7860 API 在线，否则跳过）。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")

    # 造一段真实 wav 作参考音频（1s 24k s16le 正弦，非静音）
    import math
    pcm = b"".join(struct.pack("<h", int(12000 * math.sin(i / 8))) for i in range(24000))
    wav = P.pcm_to_wav(pcm)
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"ref_audio\"; filename=\"ref.wav\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + wav + b"\r\n"
        + f"--{boundary}--\r\n".encode()
    )
    code, v = _req(base_url + "/api/voices", "POST", json.dumps({
        "name": "克隆测试", "type": "clone", "ref_text": "这是参考音频里实际说出的一句话。", "cfg_scale": 1,
    }).encode(), "application/json")
    vid = v["id"]
    code, up = _req(base_url + f"/api/voices/{vid}/ref", "POST", body, f"multipart/form-data; boundary={boundary}")
    assert code == 200 and up["ok"]

    fd = uuid.uuid4().hex
    synth_body = (
        f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n你好世界\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"voice_id\"\r\n\r\n{vid}\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n1\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"seed\"\r\n\r\n42\r\n"
        f"--{fd}--\r\n"
    ).encode()
    code, res = _req(base_url + "/api/synthesize", "POST", synth_body, f"multipart/form-data; boundary={fd}", timeout=300)
    assert code == 200, res
    assert res["duration"] > 0.3
    assert res["wav_url"].startswith("/api/audio/")

    # 历史落盘
    code, hist = _req(base_url + "/api/history")
    assert any(h["id"] == res["id"] for h in hist)

    # WAV 可取
    with urllib.request.urlopen(base_url + res["wav_url"], timeout=10) as r:
        wav_bytes = r.read()
    assert wav_bytes[:4] == b"RIFF" and len(wav_bytes) > 8000

    _req(base_url + "/api/voices/" + vid, "DELETE")


def test_clone_voice_cfg_is_clamped(base_url):
    """克隆音色（无指令）不支持 CFG：面板应自动钳到 1 并成功合成（回归：曾触发上游500）。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")

    import math
    pcm = b"".join(struct.pack("<h", int(12000 * math.sin(i / 8))) for i in range(24000))
    wav = P.pcm_to_wav(pcm)
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"ref_audio\"; filename=\"ref.wav\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + wav + b"\r\n"
        + f"--{boundary}--\r\n".encode()
    )
    code, v = _req(base_url + "/api/voices", "POST", json.dumps({
        "name": "克隆CFG钳位", "type": "clone", "ref_text": "这是参考音频里实际说出的一句话。",
    }).encode(), "application/json")
    vid = v["id"]
    code, up = _req(base_url + f"/api/voices/{vid}/ref", "POST", body, f"multipart/form-data; boundary={boundary}")
    assert code == 200

    # 用户传 cfg_scale=4（原触发上游 ValueError 500），修复后面板应钳位并合成成功
    fd = uuid.uuid4().hex
    synth_body = (
        f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n克隆音色测试文本\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"voice_id\"\r\n\r\n{vid}\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n4\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"seed\"\r\n\r\n42\r\n"
        f"--{fd}--\r\n"
    ).encode()
    code, res = _req(base_url + "/api/synthesize", "POST", synth_body, f"multipart/form-data; boundary={fd}", timeout=300)
    assert code == 200, res
    assert res["duration"] > 0.3
    assert res.get("cfg_used") == 1  # 面板回报实际使用的 cfg

    _req(base_url + "/api/voices/" + vid, "DELETE")


def _synth_req(base_url, text, tag):
    fd = uuid.uuid4().hex
    body = (
        f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n{text}\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n4\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"seed\"\r\n\r\n42\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"instruction\"\r\n\r\n沉稳旁白\r\n"
        f"--{fd}--\r\n"
    ).encode()
    code, res = _req(base_url + "/api/synthesize", "POST", body,
                     f"multipart/form-data; boundary={fd}", timeout=300)
    return tag, code, res


def test_concurrent_synthesize_are_queued(base_url):
    """上游单并发：面板须内部排队，两个并发请求都应成功（回归：曾报 already running）。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")

    results = []
    def run(tag, text):
        results.append(_synth_req(base_url, text, tag))
    ts = [
        threading.Thread(target=run, args=("a", "第一个并发请求测试文本")),
        threading.Thread(target=run, args=("b", "第二个并发请求测试文本")),
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=300)

    assert len(results) == 2
    for tag, code, res in sorted(results):
        assert code == 200, (tag, res)
        assert res["duration"] > 0.3, (tag, res)


def test_accent_upgrades_clone_to_direction(base_url):
    """克隆音色 + 口音指令 → 变为导向合成，cfg=4 应可用（回归：曾被钳到 1/报错）。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")

    import math
    pcm = b"".join(struct.pack("<h", int(12000 * math.sin(i / 8))) for i in range(24000))
    wav = P.pcm_to_wav(pcm)
    b = uuid.uuid4().hex
    up = (
        f"--{b}\r\nContent-Disposition: form-data; name=\"ref_audio\"; filename=\"r.wav\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + wav + b"\r\n" + f"--{b}--\r\n".encode()
    )
    code, v = _req(base_url + "/api/voices", "POST", json.dumps({
        "name": "口音克隆", "type": "clone", "ref_text": "这是参考音频里实际说出的一句话。",
    }).encode(), "application/json")
    vid = v["id"]
    code, _ = _req(base_url + f"/api/voices/{vid}/ref", "POST", up, f"multipart/form-data; boundary={b}")
    assert code == 200

    fd = uuid.uuid4().hex
    body = (
        f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n用贵州口音念这段话测试\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"voice_id\"\r\n\r\n{vid}\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"accent\"\r\n\r\n用贵州方言说话，带贵州口音的语调和咬字\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n4\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"seed\"\r\n\r\n42\r\n"
        f"--{fd}--\r\n"
    ).encode()
    code, res = _req(base_url + "/api/synthesize", "POST", body, f"multipart/form-data; boundary={fd}", timeout=300)
    assert code == 200, res
    assert res["cfg_used"] == 4  # 口音=指令 → 导向模板，CFG 可用
    assert res["duration"] > 0.3

    _req(base_url + "/api/voices/" + vid, "DELETE")


def test_accent_without_voice(base_url):
    """无音色 + 仅口音（作为指令）也应能合成。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")
    fd = uuid.uuid4().hex
    body = (
        f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n口音预设独立测试\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"accent\"\r\n\r\n用东北方言说话，豪爽直率\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n4\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"seed\"\r\n\r\n42\r\n"
        f"--{fd}--\r\n"
    ).encode()
    code, res = _req(base_url + "/api/synthesize", "POST", body, f"multipart/form-data; boundary={fd}", timeout=300)
    assert code == 200, res
    assert res["cfg_used"] == 4


def test_clone_create_requires_ref_text(base_url):
    """克隆音色：创建允许草稿态（文稿可空），但定稿保存必须补齐文稿。

    W2-7 语义调整：正确流程是「先上传 → 质检 → 校对文稿 → 保存」，
    因此创建阶段不再拦，定稿（PUT）阶段才拦。
    """
    code, res = _req(base_url + "/api/voices", "POST", json.dumps({
        "name": "缺文稿", "type": "clone", "ref_text": "",
    }).encode(), "application/json")
    assert code in (200, 201), res
    vid = res["id"]
    # 定稿（PUT）必须被拦，并给出人话提示
    code, err = _req(base_url + "/api/voices/" + vid, "PUT", json.dumps({
        "name": "缺文稿", "type": "clone", "ref_text": "",
    }).encode(), "application/json")
    assert code == 400, err
    assert "文字稿" in err["detail"]
    _req(base_url + "/api/voices/" + vid, "DELETE")


def test_synthesize_guard_for_unpaired_ref(base_url):
    """音色数据不完整（有音频没文稿）时，合成前拦截并点名音色（不发上游）。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")
    # 直接写一条缺文稿的音色（模拟历史数据）
    voice = {
        "id": "v_broken", "name": "缺文稿音色", "type": "clone",
        "instruction": "", "ref_audio": "refs/missing_pair.wav", "ref_text": "",
        "cfg_scale": 4, "created_at": "2026-11-05 20:00:00",
    }
    items = P._load("voices.json")
    items.append(voice)
    P._save("voices.json", items)
    (P.REFS / "missing_pair.wav").write_bytes(P.pcm_to_wav(b"\x00\x01" * 100))

    fd = uuid.uuid4().hex
    body = (
        f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n测试\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"voice_id\"\r\n\r\nv_broken\r\n"
        f"--{fd}--\r\n"
    ).encode()
    code, res = _req(base_url + "/api/synthesize", "POST", body, f"multipart/form-data; boundary={fd}")
    assert code == 400, res
    assert "缺文稿音色" in res["detail"] and "文字稿" in res["detail"]

    items = [v for v in P._load("voices.json") if v["id"] != "v_broken"]
    P._save("voices.json", items)


def test_wav_output_is_normalized(base_url):
    """合成音频响度归一化（回归：曾只有约 10% 幅度，太小声）。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")
    fd = uuid.uuid4().hex
    body = (
        f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n响度测试文本\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"instruction\"\r\n\r\n沉稳旁白\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n4\r\n"
        f"--{fd}--\r\n"
    ).encode()
    code, res = _req(base_url + "/api/synthesize", "POST", body, f"multipart/form-data; boundary={fd}", timeout=300)
    assert code == 200, res
    with urllib.request.urlopen(base_url + res["wav_url"], timeout=30) as r:
        wav = r.read()
    samples = struct.unpack_from(f"<{len(wav[44:])//2}h", wav, 44)
    rms = (sum(x * x for x in samples) / len(samples)) ** 0.5
    assert rms >= 4500, f"rms {rms:.0f} 太小声（应归一化到 ≥4500）"


def test_instruction_used_returned(base_url):
    """响应须回报实际下发的指令（回归：用户不知道贵州话到底合成了什么口音）。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")
    accent = "用浓重的贵州方言说话，明显的贵州方言语调和咬字"
    fd = uuid.uuid4().hex
    body = (
        f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n指令可见测试\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"accent\"\r\n\r\n{accent}\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n4\r\n"
        f"--{fd}--\r\n"
    ).encode()
    code, res = _req(base_url + "/api/synthesize", "POST", body, f"multipart/form-data; boundary={fd}", timeout=300)
    assert code == 200, res
    assert accent in (res.get("instruction_used") or ""), res


def test_long_text_full_coverage(base_url):
    """长文本必须完整合成（回归：曾被误判截断；时长应与字数成比例）。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")
    # 中性等长长文本：只用于验证「字数 ↔ 时长」比例，不携带任何真实用户信息
    text = ("这是一段用于校验长文本完整性的中性测试文本，内容与任何真实用户无关。"
            "它需要有足够的长度，才能覆盖分段合成与时长比例的检查逻辑，避免出现截断。")
    fd = uuid.uuid4().hex
    body = (
        f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n{text}\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"instruction\"\r\n\r\n沉稳旁白\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n4\r\n"
        f"--{fd}--\r\n"
    ).encode()
    code, res = _req(base_url + "/api/synthesize", "POST", body, f"multipart/form-data; boundary={fd}", timeout=600)
    assert code == 200, res
    n = len(text)
    assert res["duration"] >= n / 8.0, f"{n}字只出 {res['duration']:.1f}s，疑似截断"
    assert res["duration"] <= n / 2.0, "时长异常偏长"


def test_no_history_and_concat(base_url):
    """分段合成：no_history 不落历史，/api/concat 拼接后生成唯一历史条目。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")
    n0 = len(_req(base_url + "/api/history")[1])
    files = []
    total = 0.0
    for i, t in enumerate(["第一段落测试文本", "第二段落测试文本"]):
        fd = uuid.uuid4().hex
        body = (
            f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n{t}\r\n"
            f"--{fd}\r\nContent-Disposition: form-data; name=\"instruction\"\r\n\r\n沉稳旁白\r\n"
            f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n4\r\n"
            f"--{fd}\r\nContent-Disposition: form-data; name=\"no_history\"\r\n\r\n1\r\n"
            f"--{fd}--\r\n"
        ).encode()
        code, res = _req(base_url + "/api/synthesize", "POST", body, f"multipart/form-data; boundary={fd}", timeout=300)
        assert code == 200, res
        files.append(res["wav_url"].rsplit("/", 1)[-1])
        total += res["duration"]
    # no_history 生效：历史未增加
    n1 = len(_req(base_url + "/api/history")[1])
    assert n1 == n0, "no_history=1 不应写历史"

    code, res = _req(base_url + "/api/concat", "POST", json.dumps({
        "files": files, "text": "第一段落测试文本 / 第二段落测试文本",
    }).encode(), "application/json")
    assert code == 200, res
    assert abs(res["duration"] - total) < 0.5, (res["duration"], total)
    n2 = len(_req(base_url + "/api/history")[1])
    assert n2 == n0 + 1, "concat 应生成唯一历史条目"


def test_ref_audio_trim(base_url):
    """参考音频过长应可裁剪到 15 秒内（提速关键）。"""
    import math
    pcm = b"".join(struct.pack("<h", int(12000 * math.sin(i / 8))) for i in range(24000 * 20))
    wav = P.pcm_to_wav(pcm, normalize=False)
    b = uuid.uuid4().hex
    up = (
        f"--{b}\r\nContent-Disposition: form-data; name=\"ref_audio\"; filename=\"long.wav\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + wav + b"\r\n" + f"--{b}--\r\n".encode()
    )
    code, v = _req(base_url + "/api/voices", "POST", json.dumps({
        "name": "长参考", "type": "clone", "ref_text": "这是参考音频里实际说出的一句话。",
    }).encode(), "application/json")
    vid = v["id"]
    code, upres = _req(base_url + f"/api/voices/{vid}/ref", "POST", up, f"multipart/form-data; boundary={b}")
    assert code == 200 and upres.get("ref_duration", 0) > 19, upres

    code, trim = _req(base_url + f"/api/voices/{vid}/trim", "POST", b"", "application/json")
    assert code == 200, trim
    assert trim["ref_duration"] <= 15.5, trim
    _req(base_url + "/api/voices/" + vid, "DELETE")


def test_emotion_merges_into_instruction(base_url):
    """情感/风格指令（经 accent 字段合并）应下发并解锁 CFG。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")
    merged = "语气温柔亲切，情感细腻，像讲故事一样娓娓道来"
    fd = uuid.uuid4().hex
    body = (
        f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n情感预设测试文本\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"accent\"\r\n\r\n{merged}\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n4\r\n"
        f"--{fd}--\r\n"
    ).encode()
    code, res = _req(base_url + "/api/synthesize", "POST", body, f"multipart/form-data; boundary={fd}", timeout=300)
    assert code == 200, res
    assert merged in (res.get("instruction_used") or "")
    assert res["cfg_used"] == 4


def test_resample_pcm_speed():
    """语速重采样纯逻辑：1.5x 后时长缩至 2/3，波形连续。"""
    import math
    pcm = b"".join(struct.pack("<h", int(10000 * math.sin(i / 20))) for i in range(24000))
    out = P.resample_pcm(pcm, 1.5)
    assert abs(len(out) / len(pcm) - 2 / 3) < 0.02, (len(out), len(pcm))
    assert P.resample_pcm(pcm, 1.0) == pcm
    slow = P.resample_pcm(pcm, 0.75)
    assert abs(len(slow) / len(pcm) - 4 / 3) < 0.02


def test_concat_with_gaps(base_url):
    """拼接支持段间停顿：总时长 = 各段之和 + gaps 之和。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")
    files, total = [], 0.0
    for t in ["停顿测试甲", "停顿测试乙"]:
        fd = uuid.uuid4().hex
        body = (
            f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n{t}\r\n"
            f"--{fd}\r\nContent-Disposition: form-data; name=\"instruction\"\r\n\r\n沉稳旁白\r\n"
            f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n4\r\n"
            f"--{fd}\r\nContent-Disposition: form-data; name=\"no_history\"\r\n\r\n1\r\n"
            f"--{fd}--\r\n"
        ).encode()
        code, res = _req(base_url + "/api/synthesize", "POST", body, f"multipart/form-data; boundary={fd}", timeout=300)
        assert code == 200, res
        files.append(res["wav_url"].rsplit("/", 1)[-1])
        total += res["duration"]
    code, res = _req(base_url + "/api/concat", "POST", json.dumps({
        "files": files, "text": "gap测试", "gaps": [0.6],
    }).encode(), "application/json")
    assert code == 200, res
    assert abs(res["duration"] - (total + 0.6)) < 0.25, (res["duration"], total)


def test_speed_field_shortens_audio(base_url):
    """API 语速参数：speed=1.5 的时长应明显短于 speed=1.0（同文本同种子）。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")
    durs = {}
    for sp in ("1.0", "1.5"):
        fd = uuid.uuid4().hex
        body = (
            f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n语速对照测试文本内容\r\n"
            f"--{fd}\r\nContent-Disposition: form-data; name=\"instruction\"\r\n\r\n沉稳旁白\r\n"
            f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n4\r\n"
            f"--{fd}\r\nContent-Disposition: form-data; name=\"seed\"\r\n\r\n7\r\n"
            f"--{fd}\r\nContent-Disposition: form-data; name=\"speed\"\r\n\r\n{sp}\r\n"
            f"--{fd}--\r\n"
        ).encode()
        code, res = _req(base_url + "/api/synthesize", "POST", body, f"multipart/form-data; boundary={fd}", timeout=300)
        assert code == 200, res
        durs[sp] = res["duration"]
    assert durs["1.5"] < durs["1.0"] * 0.85, durs


def test_default_mandarin_norm(base_url):
    """克隆音色默认应下发标准普通话规范（回归：曾默认继承参考音频口音）。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")
    import math
    pcm = b"".join(struct.pack("<h", int(12000 * math.sin(i / 8))) for i in range(24000))
    wav = P.pcm_to_wav(pcm, normalize=False)
    b = uuid.uuid4().hex
    up = (
        f"--{b}\r\nContent-Disposition: form-data; name=\"ref_audio\"; filename=\"r.wav\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + wav + b"\r\n" + f"--{b}--\r\n".encode()
    )
    code, v = _req(base_url + "/api/voices", "POST", json.dumps({
        "name": "规范测试", "type": "clone", "ref_text": "这是参考音频里实际说出的一句话。",
    }).encode(), "application/json")
    vid = v["id"]
    _req(base_url + f"/api/voices/{vid}/ref", "POST", up, f"multipart/form-data; boundary={b}")

    fd = uuid.uuid4().hex
    body = (
        f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n默认普通话测试\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"voice_id\"\r\n\r\n{vid}\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"norm\"\r\n\r\n1\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n1\r\n"
        f"--{fd}--\r\n"
    ).encode()
    code, res = _req(base_url + "/api/synthesize", "POST", body, f"multipart/form-data; boundary={fd}", timeout=300)
    assert code == 200, res
    assert "标准普通话" in (res.get("instruction_used") or ""), res
    _req(base_url + "/api/voices/" + vid, "DELETE")


def test_accent_suppresses_norm(base_url):
    """显式选择方言口音时不叠加普通话规范（口音优先）。"""
    code, _ = _req(base_url + "/api/model")
    if code != 200:
        pytest.skip("API 7860 offline")
    fd = uuid.uuid4().hex
    body = (
        f"--{fd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n口音优先测试\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"accent\"\r\n\r\n用浓重的贵州方言说话\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"norm\"\r\n\r\n1\r\n"
        f"--{fd}\r\nContent-Disposition: form-data; name=\"cfg_scale\"\r\n\r\n4\r\n"
        f"--{fd}--\r\n"
    ).encode()
    code, res = _req(base_url + "/api/synthesize", "POST", body, f"multipart/form-data; boundary={fd}", timeout=300)
    assert code == 200, res
    ins = res.get("instruction_used") or ""
    assert "贵州" in ins
    assert "标准普通话" not in ins, ins
