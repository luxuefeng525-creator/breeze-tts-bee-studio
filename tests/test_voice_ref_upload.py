"""REQ-20261127-04：参考音频上传归一化（前端只收 wav，后端兜底转 WAV）。

真实面板 HTTP 接口、零 mock；数据目录隔离到 tmp，不碰用户数据。
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

import panel as P

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="机器上没有 ffmpeg")


@pytest.fixture(scope="module")
def data_isolation(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("voice-ref-data")
    old = (P.DATA, P.REFS, P.AUDIOS)
    P.DATA, P.REFS, P.AUDIOS = tmp, tmp / "refs", tmp / "audio"
    for d in (P.DATA, P.REFS, P.AUDIOS):
        d.mkdir(parents=True, exist_ok=True)
    try:
        yield tmp
    finally:
        P.DATA, P.REFS, P.AUDIOS = old


@pytest.fixture(scope="module")
def base_url(data_isolation):
    server = P.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _req(url, method="GET", body=None, ctype=None, timeout=120):
    r = urllib.request.Request(url, data=body, method=method)
    if ctype:
        r.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload or b"{}")
        except Exception:
            return e.code, {"detail": payload[:200].decode("utf-8", "replace")}


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, (fname, data) in files.items():
        out.write(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n"
                  f"Content-Type: application/octet-stream\r\n\r\n".encode())
        out.write(data)
        out.write(b"\r\n")
    out.write(f"--{b}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={b}"


def _make_mp3(path: Path, seconds: float = 1.5) -> bytes:
    """用 ffmpeg 生成一段真实 mp3（不是假文件），确保用例反映现实。"""
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "libmp3lame", str(path)],
        capture_output=True, check=True,
    )
    return path.read_bytes()


def _new_voice(base_url) -> str:
    code, j = _req(base_url + "/api/voices", "POST",
                   json.dumps({"name": "用例音色", "type": "clone",
                               "instruction": "", "ref_text": "测试文稿"}).encode(),
                   "application/json")
    assert code in (200, 201), j
    return j["id"]


def test_ac01_mp3_upload_is_normalized_to_wav(base_url, data_isolation, tmp_path):
    vid = _new_voice(base_url)
    mp3 = _make_mp3(tmp_path / "sample.mp3")
    assert mp3[:3] == b"ID3" or mp3[:2] == b"\xff\xfb", "生成的应为 mp3"

    body, ctype = _multipart({}, {"ref_audio": ("sample.mp3", mp3)})
    code, j = _req(f"{base_url}/api/voices/{vid}/ref", "POST", body, ctype)
    assert code == 200, j

    rel = j["ref_audio"]
    assert rel.endswith(".wav"), rel
    disk = data_isolation / rel
    assert disk.is_file(), f"落盘文件不存在: {disk}"
    head = disk.read_bytes()[:12]
    assert head[:4] == b"RIFF" and head[8:12] == b"WAVE", f"不是标准 WAV: {head!r}"

    voices = json.loads((data_isolation / "voices.json").read_text(encoding="utf-8"))
    rec = next(v for v in voices if v["id"] == vid)
    assert rec["ref_audio"] == rel
    assert float(rec["ref_duration"]) > 0


def test_ac02_bad_files_are_rejected_without_side_effect(base_url, data_isolation, tmp_path):
    # 空文件
    vid = _new_voice(base_url)
    before = sorted(p.name for p in (data_isolation / "refs").iterdir())
    body, ctype = _multipart({}, {"ref_audio": ("empty.wav", b"")})
    code, j = _req(f"{base_url}/api/voices/{vid}/ref", "POST", body, ctype)
    assert code == 400, j
    assert sorted(p.name for p in (data_isolation / "refs").iterdir()) == before

    # 伪装扩展名：png 内容改名成 wav/mp3
    fake = b"\x89PNG\r\n\x1a\n" + b"0" * 2048
    for name in ("fake.wav", "fake.mp3"):
        vid2 = _new_voice(base_url)
        body, ctype = _multipart({}, {"ref_audio": (name, fake)})
        code, j = _req(f"{base_url}/api/voices/{vid2}/ref", "POST", body, ctype)
        assert code == 400, (name, j)
        assert "无法解码" in str(j.get("detail")) or "过大" in str(j.get("detail"))


def test_ac03_oversize_rejected_fast(base_url, data_isolation, monkeypatch):
    # 直接把阈值调小：验证「超限在解码前被拒」的逻辑，不必真的读 50MB
    monkeypatch.setattr(P, "REF_MAX_BYTES", 4096)
    vid = _new_voice(base_url)
    before = {p.name for p in (data_isolation / "refs").iterdir()}
    blob = b"\x00" * 8192
    body, ctype = _multipart({}, {"ref_audio": ("big.wav", blob)})
    code, j = _req(f"{base_url}/api/voices/{vid}/ref", "POST", body, ctype)
    assert code == 400, j
    assert "过大" in str(j.get("detail"))
    assert {p.name for p in (data_isolation / "refs").iterdir()} == before


def test_repeat_upload_leaves_no_orphan(base_url, data_isolation, tmp_path):
    vid = _new_voice(base_url)
    refs = data_isolation / "refs"
    for i in range(2):
        wav = tmp_path / f"s{i}.wav"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                        "-i", f"sine=frequency={440 + i}:duration=1",
                        "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                       capture_output=True, check=True)
        body, ctype = _multipart({}, {"ref_audio": (f"s{i}.wav", wav.read_bytes())})
        code, j = _req(f"{base_url}/api/voices/{vid}/ref", "POST", body, ctype)
        assert code == 200, j
    mine = [p.name for p in refs.iterdir() if p.name.startswith(vid)]
    assert len(mine) == 1, f"同一音色重复上传应只剩最新一份，实际: {mine}"
