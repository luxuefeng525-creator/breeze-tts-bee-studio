"""环境声 / BGM / 一键出片的接口测试（真实服务，零 mock）。

不需要推理引擎：行音频用本地生成的方波占位，磁盘结构与合成完成后一致。
"""
from __future__ import annotations

import io
import json
import struct
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile

import pytest

import panel as P


@pytest.fixture(scope="module")
def data_isolation(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("soundtrack-data")
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


def _req(url, method="GET", body=None, ctype=None, timeout=60):
    r = urllib.request.Request(url, data=body, method=method)
    if ctype:
        r.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Type", "").startswith("application/zip"):
                return resp.status, raw
            return resp.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload or b"{}")
        except Exception:
            return e.code, {"detail": payload[:200].decode("utf-8", "replace")}


def _json_req(url, method, payload):
    return _req(url, method, json.dumps(payload).encode(), "application/json")


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    """拼一个 multipart/form-data 请求体。"""
    boundary = "----tts2test" + uuid.uuid4().hex[:8]
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        buf.write(str(v).encode("utf-8") + b"\r\n")
    for k, (fname, payload) in files.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{k}"; '
                  f'filename="{fname}"\r\n'.encode())
        buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
        buf.write(payload + b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def _tone_wav(seconds=1.0, freq=220.0, rate=24000, amp=8000) -> bytes:
    import math
    n = int(seconds * rate)
    pcm = b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(n))
    return (
        struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + len(pcm), b"WAVE",
            b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16,
            b"data", len(pcm),
        )
        + pcm
    )


def _mk_script(base_url, raw, title="混音测试剧本", **extra):
    payload = {"title": title, "raw": raw, "auto_cast": True}
    payload.update(extra)
    code, s = _json_req(base_url + "/api/scripts", "POST", payload)
    assert code in (200, 201), s
    return s


def _seed_tone_files(script_id: str) -> None:
    """给每行写一段方波占位并拼整轨（等价于合成完成后的磁盘结构）。"""
    import script_routes as SR

    script = next(s for s in P._load("scripts.json") if s["id"] == script_id)
    rows = SR._enriched(script, SR.parse_for(script))
    out_dir = P.AUDIOS / "scripts" / script_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        name = f"line_{row['idx']:03d}.wav"
        (out_dir / name).write_bytes(_tone_wav(1.0))
        SR._persist_line(script_id, row["idx"], {
            "status": "done",
            "wav": f"audio/scripts/{script_id}/{name}",
            "hash": row["hash"],
            "duration": 1.0,
        })
    SR._concat(script_id, 0.2)


def _wait_job(base_url, job_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, job = _req(base_url + f"/api/scripts/jobs/{job_id}")
        if code != 200:
            return job
        if job.get("state") != "running":
            return job
        time.sleep(0.3)
    raise AssertionError("任务超时没结束")


# --------------------------------------------------------------------- 素材库
def test_generate_noise_asset_and_list_it(base_url):
    code, d = _json_req(base_url + "/api/assets/generate", "POST",
                        {"kind": "white", "seconds": 2, "name": "测试底噪"})
    assert code == 201, d
    asset = d["asset"]
    assert asset["kind"] == "white" and asset["source"] == "generated"
    assert abs(float(asset["duration"]) - 2.0) < 0.2, asset

    code, items = _req(base_url + "/api/assets")
    assert code == 200
    assert any(a["id"] == asset["id"] for a in items), items
    # 路径是内部细节，不该出现在对外字段里
    assert "path" not in items[0], items[0]

    code, _ = _req(base_url + "/api/assets/" + asset["id"], "DELETE")
    assert code == 200
    code, items = _req(base_url + "/api/assets")
    assert not any(a["id"] == asset["id"] for a in items)


def test_generate_rejects_unknown_kind(base_url):
    code, d = _json_req(base_url + "/api/assets/generate", "POST",
                        {"kind": "metal"})
    assert code == 400, d


def test_upload_requires_license_confirmation(base_url):
    body, ctype = _multipart({"name": "上传素材"},
                              {"file": ("a.wav", _tone_wav(0.5))})
    code, d = _req(base_url + "/api/assets", "POST", body, ctype)
    assert code == 400, d
    assert "授权" in str(d.get("detail", "")), d

    body, ctype = _multipart({"name": "上传素材", "license_confirmed": "1"},
                              {"file": ("a.wav", _tone_wav(0.5))})
    code, d = _req(base_url + "/api/assets", "POST", body, ctype)
    assert code == 201, d
    assert d["asset"]["kind"] == "upload"


def test_upload_rejects_undecodable_file(base_url):
    body, ctype = _multipart({"license_confirmed": "1"},
                              {"file": ("a.wav", b"this is not audio")})
    code, d = _req(base_url + "/api/assets", "POST", body, ctype)
    assert code == 400, d


# --------------------------------------------------------------------- 场次绑定
def test_scene_ambience_and_bgm_roundtrip(base_url):
    code, d = _json_req(base_url + "/api/assets/generate", "POST",
                        {"kind": "brown", "seconds": 1})
    assert code == 201, d
    asset_id = d["asset"]["id"]
    s = _mk_script(base_url, "## 夜路\n甲：走吧。\n## 清晨\n乙：到了。")
    try:
        code, d = _json_req(base_url + f"/api/scripts/{s['id']}", "PUT", {
            "scenes": [
                {"name": "夜路", "mood": "安静", "ambience": asset_id,
                 "bgm": "", "gain": 0.5},
                {"name": "清晨", "mood": "", "ambience": "", "bgm": asset_id,
                 "gain": 0.2},
            ],
        })
        assert code == 200, d
        scenes = {x["name"]: x for x in d["scenes"]}
        assert scenes["夜路"]["ambience"] == asset_id, scenes
        assert scenes["夜路"]["gain"] == 0.5, scenes
        assert scenes["清晨"]["bgm"] == asset_id, scenes

        # 只改氛围时，已经配好的铺底不能被悄悄清掉
        code, d = _json_req(base_url + f"/api/scripts/{s['id']}", "PUT", {
            "scenes": [{"name": "夜路", "mood": "紧张"}],
        })
        assert code == 200, d
        scenes = {x["name"]: x for x in d["scenes"]}
        assert scenes["夜路"]["ambience"] == asset_id, scenes
    finally:
        _req(base_url + f"/api/scripts/{s['id']}", "DELETE")


# --------------------------------------------------------------------- 混音
def test_mix_is_built_and_dry_voice_is_untouched(base_url):
    code, d = _json_req(base_url + "/api/assets/generate", "POST",
                        {"kind": "white", "seconds": 3})
    assert code == 201, d
    asset_id = d["asset"]["id"]
    s = _mk_script(base_url, "## 第一场\n甲：你好。\n## 第二场\n乙：再见。")
    try:
        _seed_tone_files(s["id"])
        code, d = _req(base_url + f"/api/scripts/{s['id']}")
        full_rel = d["full_wav"]
        full_path = P.DATA / full_rel
        before = full_path.read_bytes()

        code, d = _json_req(base_url + f"/api/scripts/{s['id']}", "PUT", {
            "scenes": [{"name": "第一场", "ambience": asset_id, "gain": 0.6},
                       {"name": "第二场", "ambience": asset_id, "gain": 0.6}],
        })
        assert code == 200, d

        code, d = _json_req(base_url + f"/api/scripts/{s['id']}/mix", "POST", {})
        assert code == 200, d
        assert d["mix_wav"].endswith("mix.wav"), d
        mix_path = P.DATA / d["mix_wav"]
        assert mix_path.is_file()

        # 干声一个字节都不能变
        assert full_path.read_bytes() == before, "混音不能改动干声整轨"
        # 混音与干声等长（环境声只是叠在下面）
        assert abs(_wav_duration(mix_path.read_bytes())
                   - _wav_duration(before)) < 0.05

        code, d = _req(base_url + f"/api/scripts/{s['id']}")
        assert d.get("mix_wav", "").endswith("mix.wav"), d
    finally:
        _req(base_url + f"/api/scripts/{s['id']}", "DELETE")


def _wav_duration(data: bytes) -> float:
    rate = struct.unpack("<I", data[24:28])[0]
    size = struct.unpack("<I", data[40:44])[0]
    return size / (rate * 2)


def test_mix_not_created_without_assets(base_url):
    s = _mk_script(base_url, "甲：只有一句。")
    try:
        _seed_tone_files(s["id"])
        code, d = _json_req(base_url + f"/api/scripts/{s['id']}/mix", "POST", {})
        assert code == 200, d
        assert d["mix_wav"] == "", d
    finally:
        _req(base_url + f"/api/scripts/{s['id']}", "DELETE")


def test_export_mix_part_contains_mix_wav(base_url):
    code, d = _json_req(base_url + "/api/assets/generate", "POST",
                        {"kind": "hum", "seconds": 2})
    assert code == 201, d
    asset_id = d["asset"]["id"]
    s = _mk_script(base_url, "## 第一场\n甲：你好。")
    try:
        _seed_tone_files(s["id"])
        _json_req(base_url + f"/api/scripts/{s['id']}", "PUT", {
            "scenes": [{"name": "第一场", "ambience": asset_id}]})
        _json_req(base_url + f"/api/scripts/{s['id']}/mix", "POST", {})

        code, blob = _req(base_url + f"/api/scripts/{s['id']}/export?parts=full,mix")
        assert code == 200, blob
        z = zipfile.ZipFile(io.BytesIO(blob))
        assert "mix.wav" in z.namelist(), z.namelist()
        assert "full.wav" in z.namelist(), z.namelist()
    finally:
        _req(base_url + f"/api/scripts/{s['id']}", "DELETE")


def test_export_mix_part_refused_when_no_mix(base_url):
    s = _mk_script(base_url, "甲：只有一句。")
    try:
        _seed_tone_files(s["id"])
        code, d = _req(base_url + f"/api/scripts/{s['id']}/export?parts=mix")
        assert code == 400, d
        assert "混音" in str(d.get("detail", "")), d
    finally:
        _req(base_url + f"/api/scripts/{s['id']}", "DELETE")


# --------------------------------------------------------------------- 一键出片
def test_produce_finishes_and_leaves_mix(base_url):
    code, d = _json_req(base_url + "/api/assets/generate", "POST",
                        {"kind": "brown", "seconds": 2})
    assert code == 201, d
    asset_id = d["asset"]["id"]
    s = _mk_script(base_url, "## 第一场\n甲：你好。\n## 第二场\n乙：再见。")
    try:
        _seed_tone_files(s["id"])
        _json_req(base_url + f"/api/scripts/{s['id']}", "PUT", {
            "scenes": [{"name": "第一场", "ambience": asset_id},
                       {"name": "第二场", "ambience": asset_id}]})
        code, d = _json_req(base_url + f"/api/scripts/{s['id']}/produce", "POST", {})
        assert code == 200, d
        assert d.get("job_id"), d
        job = _wait_job(base_url, d["job_id"])
        assert job.get("state") == "done", job
        code, s2 = _req(base_url + f"/api/scripts/{s['id']}")
        assert s2.get("mix_wav", "").endswith("mix.wav"), s2
        assert s2.get("full_wav"), s2
    finally:
        _req(base_url + f"/api/scripts/{s['id']}", "DELETE")
