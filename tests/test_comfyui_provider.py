# coding: utf-8
"""ComfyUI provider 的「任务丢失」判定。

背景（2026-11-26）：ComfyUI 的任务队列只存在内存里，守护脚本自愈重启后，
之前提交的 prompt 会彻底消失 —— /history 永远为空、/queue 里也没有。
旧实现遇到这种情况会一直报 running，前端傻等满 30 分钟才超时，
用户看到的就是「轮询中 #332 · running」这种假活着。

这里用假 response 覆盖三条分支，不连真实 8188。
"""
from __future__ import annotations

import time

from api.providers import GenResult
from api.providers.comfyui import ComfyUIProvider


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _provider():
    return ComfyUIProvider(base_url="http://127.0.0.1:8188")


def _patch_get(monkeypatch, table):
    """按 URL 后缀分发假响应；值为异常实例时直接抛。"""
    def fake_get(url, *a, **kw):
        for suffix, payload in table.items():
            if url.endswith(suffix):
                if isinstance(payload, Exception):
                    raise payload
                return FakeResp(payload)
        raise AssertionError("unexpected url: %s" % url)
    monkeypatch.setattr("api.providers.comfyui.requests.get", fake_get)


def test_queue_has_detects_running_and_pending(monkeypatch):
    p = _provider()
    _patch_get(monkeypatch, {"/queue": {
        "queue_running": [[1, "job-a", {}, {}, []]],
        "queue_pending": [[2, "job-b", {}, {}, []]],
    }})
    assert p._queue_has("job-a") is True
    assert p._queue_has("job-b") is True
    assert p._queue_has("job-c") is False


def test_queue_has_returns_none_when_unreachable(monkeypatch):
    import requests
    p = _provider()
    _patch_get(monkeypatch, {"/queue": requests.ConnectionError("refused")})
    assert p._queue_has("job-a") is None


def test_lost_needs_grace_period(monkeypatch):
    p = _provider()
    _patch_get(monkeypatch, {"/queue": {"queue_running": [], "queue_pending": []}})
    fresh = GenResult(job_id="job-a", status="running")
    assert p._lost(fresh, "job-a") is False, "刚提交 90 秒内不许判丢"
    old = GenResult(job_id="job-a", status="running")
    old._bee_first_poll = time.time() - 120
    assert p._lost(old, "job-a") is True


def test_lost_false_while_still_queued(monkeypatch):
    p = _provider()
    _patch_get(monkeypatch, {"/queue": {
        "queue_running": [[1, "job-a", {}, {}, []]], "queue_pending": [],
    }})
    old = GenResult(job_id="job-a", status="running")
    old._bee_first_poll = time.time() - 300
    assert p._lost(old, "job-a") is False


def test_poll_reports_lost_job_as_failed(monkeypatch):
    p = _provider()
    _patch_get(monkeypatch, {
        "/history/job-a": {},
        "/queue": {"queue_running": [], "queue_pending": []},
    })
    old = GenResult(job_id="job-a", status="running")
    old._bee_first_poll = time.time() - 300
    r = p.poll(old, "job-a")
    assert r.status == "failed"
    assert "丢失" in r.error


def test_poll_keeps_running_within_grace(monkeypatch):
    p = _provider()
    _patch_get(monkeypatch, {
        "/history/job-a": {},
        "/queue": {"queue_running": [], "queue_pending": []},
    })
    fresh = GenResult(job_id="job-a", status="running")
    assert p.poll(fresh, "job-a").status == "running"
