"""REQ-20261128-02：GPT-SoVITS sidecar 路由与参考音频规范化。

覆盖不依赖 sidecar 的部分（纯逻辑 + 不可用报错），真实 HTTP、零 mock；
需要 sidecar 真出声的验收点由真实环境脚本与产物文件作证据（见 02-test.md）。
"""
from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import panel as P


@pytest.fixture(scope="module")
def data_isolation(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sovits-data")
    old = (P.DATA, P.REFS, P.AUDIOS, P.SOVITS_REF_DIR)
    P.DATA, P.REFS, P.AUDIOS = tmp, tmp / "refs", tmp / "audio"
    P.SOVITS_REF_DIR = tmp / "sovits_refs"
    for d in (P.DATA, P.REFS, P.AUDIOS):
        d.mkdir(parents=True, exist_ok=True)
    try:
        yield tmp
    finally:
        P.DATA, P.REFS, P.AUDIOS, P.SOVITS_REF_DIR = old


@pytest.fixture(scope="module")
def base_url(data_isolation):
    server = P.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _multipart(fields: dict) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode())
        out.write(str(v).encode() + b"\r\n")
    out.write(f"--{b}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={b}"


def _post(url, fields, timeout=30):
    body, ctype = _multipart(fields)
    r = urllib.request.Request(url, data=body, method="POST")
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


def test_voice_engine_defaults_to_qwen():
    """缺省 / 非法值都必须回落 qwen3-tts，老数据行为不变。"""
    assert P.voice_engine({"name": "x"}) == "qwen3-tts"
    assert P.voice_engine({"engine": ""}) == "qwen3-tts"
    assert P.voice_engine({"engine": "foo"}) == "qwen3-tts"
    assert P.voice_engine(None) == "qwen3-tts"
    assert P.voice_engine({"engine": "GPT-SoVITS"}) == "gpt-sovits"


def test_wav_to_pcm_normalizes_to_24k_mono():
    """48 kHz 立体声也要落成 24 kHz 单声道 s16le，否则与 7860 链路格式不一致。"""
    dur = 0.5
    sr = 48000
    stereo = np.zeros((int(sr * dur), 2), dtype=np.float32)
    tone = 0.3 * np.sin(2 * np.pi * 440 * np.arange(int(sr * dur)) / sr)
    stereo[:, 0] = tone
    stereo[:, 1] = tone
    buf = io.BytesIO()
    sf.write(buf, stereo, sr, format="WAV", subtype="PCM_16")

    pcm = P._wav_to_pcm(buf.getvalue())

    assert len(pcm) % 2 == 0
    samples = np.frombuffer(pcm, dtype="<i2")
    # 0.5 秒 @24 kHz 单声道 ≈ 12000 个采样点（允许重采样边界误差）
    assert abs(len(samples) - 12000) < 60


def test_long_reference_is_cropped_within_limit(data_isolation):
    """超过 10 秒的参考音频必须裁到 3~10 秒，并产出可用的缓存文件。"""
    from sovits_ref import MAX_SEC, MIN_SEC, prepare

    src = data_isolation / "long.wav"
    sr = 24000
    t = np.arange(int(sr * 14.7)) / sr
    wave = (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    sf.write(src, wave, sr, subtype="PCM_16")

    out, text = prepare(src, data_isolation / "sovits_refs", "v_test",
                        asr_fn=lambda _p: "测试文稿")

    data, out_sr = sf.read(out)
    dur = len(data) / out_sr
    assert MIN_SEC <= dur <= MAX_SEC, f"裁段后 {dur:.2f}s 仍不在 3~10 秒内"
    assert out_sr == 24000
    assert text == "测试文稿"
    assert (data_isolation / "sovits_refs" / "v_test.json").is_file()


def test_short_reference_is_rejected(data_isolation):
    """短于 3 秒补不出内容，必须明确拒绝而不是硬塞给上游。"""
    from sovits_ref import prepare

    src = data_isolation / "short.wav"
    sf.write(src, np.zeros(24000, dtype=np.float32), 24000, subtype="PCM_16")
    with pytest.raises(ValueError, match="短于 GPT-SoVITS"):
        prepare(src, data_isolation / "sovits_refs", "v_short")


@pytest.mark.parametrize("path,fields", [
    ("/api/synthesize", {"text": "报错测试", "voice_id": "v_x"}),
    ("/api/synthesize/batch", {"texts": json.dumps(["一", "二"]), "voice_id": "v_x"}),
    ("/api/synthesize/stream", {"text": "报错测试", "voice_id": "v_x"}),
])
def test_sovits_unavailable_returns_actionable_error(base_url, data_isolation,
                                                     path, fields):
    """sidecar 不在时三条路径都必须 503 且给出可执行指引，不允许静默回落 7860。"""
    P._save("voices.json", [{"id": "v_x", "name": "测试音色", "type": "clone",
                             "instruction": "", "engine": "gpt-sovits",
                             "ref_audio": "refs/x.wav", "ref_text": "文稿",
                             "cfg_scale": 4.0,
                             "created_at": "2026-11-28 00:00:00"}])
    (P.DATA / "refs").mkdir(parents=True, exist_ok=True)
    # 单条/批量路径在调 sidecar 之前会先校验参考音频文件存在；
    # 这里给一个真实 wav，确保三条路径都走到「服务不可用」这一步。
    ref = P.DATA / "refs" / "x.wav"
    sf.write(ref, np.zeros(24000 * 5, dtype=np.float32), 24000, subtype="PCM_16")

    code, payload = _post(base_url + path, fields)

    assert code == 503, f"{path} 期望 503，实得 {code}"
    assert "GPT-SoVITS 服务未启动" in payload.get("detail", "")
    assert "start --take-gpu" in payload.get("detail", "")


def test_engine_field_validated_on_create(base_url, data_isolation):
    """非法 engine 值必须 400，避免写进账本后到合成时才炸。"""
    body = json.dumps({"name": "非法引擎", "type": "design",
                       "engine": "nope"}).encode()
    r = urllib.request.Request(base_url + "/api/voices", data=body, method="POST")
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 400
