# coding: utf-8
"""成本估算接口：is_image 必须真的改变报价档位。

背景（2026-11-26）：前端成本面板原先不传 is_image，后端也不解析，导致
本地带图出片（Wan2.2 5B，实测 305 秒）被报成 1.3B 纯文本的「35 秒」。
这条测试锁住「参数真的生效」，防止哪天又被漏掉。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

import panel as P


@pytest.fixture(scope="module")
def data_isolation(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cost-data")
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


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_text_mode_reports_fast_path(base_url):
    code, d = _get(base_url + "/api/cost/estimate?provider=comfyui&resolution=480p&duration=5")
    assert code == 200
    # 纯文本走 1.3B 4 步蒸馏，是分钟以内的档
    assert d["eta_sec"] < 120, d
    assert "纯文本" in d["notes"]
    assert d["supports_image_input"] is True


def test_image_mode_reports_five_minutes(base_url):
    code, d = _get(
        base_url
        + "/api/cost/estimate?provider=comfyui&resolution=480p&duration=5&is_image=1")
    assert code == 200
    # 带图走 5B，实测约 305 秒；报价不能低于 300 秒，否则就是低估
    assert d["eta_sec"] >= 300, d
    assert "与片长" in d["notes"], d["notes"]
    assert "5 分钟" in d["notes"], d["notes"]


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_forms_of_is_image(base_url, flag):
    code, d = _get(
        base_url
        + f"/api/cost/estimate?provider=comfyui&resolution=480p&duration=5&is_image={flag}")
    assert code == 200
    assert d["eta_sec"] >= 300, (flag, d)


def test_image_mode_differs_from_text_mode(base_url):
    _, text = _get(
        base_url + "/api/cost/estimate?provider=comfyui&resolution=480p&duration=5&is_image=0")
    _, img = _get(
        base_url + "/api/cost/estimate?provider=comfyui&resolution=480p&duration=5&is_image=1")
    assert img["eta_sec"] > text["eta_sec"] * 3, (text["eta_sec"], img["eta_sec"])
    assert img["notes"] != text["notes"]


def test_compare_honours_is_image(base_url):
    """四家对比用的是同一个 is_image 口径，否则本地档会被低估成 35 秒。"""
    _, text = _get(base_url + "/api/cost/compare?resolution=480p&duration=5")
    _, img = _get(base_url + "/api/cost/compare?resolution=480p&duration=5&is_image=1")
    t = next(q for q in text if q["provider"] == "comfyui")
    i = next(q for q in img if q["provider"] == "comfyui")
    assert i["eta_sec"] > t["eta_sec"] * 3, (t, i)

