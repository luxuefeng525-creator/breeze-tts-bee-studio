# coding: utf-8
"""引擎卡死检测与自愈（2026-11-10）。

背景：一次推理独占 eng.lock。GPU 调用一旦挂死，Python 杀不掉那个线程 →
锁永远不释放 → 之后每个请求都只能拿到 409，而 /health 依然报 ready，
守护脚本因此永远不会重启它。表现是「面板活着但再也合不出声」。

这里钉死四件事：
1. 正常推理期间不会误判卡死
2. 超时后 /health 返回 503（守护脚本据此重启进程）
3. 卡死期间的新请求返回 503（而不是继续堆 409）
4. 推理真的回来了 → 解除卡死标记（否则引擎会被永久判死）

全部用假引擎，不加载 GPU、不访问真实 7860。
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_ADAPTER = Path(__file__).resolve().parents[2] / "qwen-adapter" / "qwen_adapter.py"


@pytest.fixture(scope="module")
def qa():
    assert _ADAPTER.is_file(), f"adapter not found: {_ADAPTER}"
    spec = importlib.util.spec_from_file_location("qwen_adapter_stall", _ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def engine(qa):
    eng = qa.Engine(Path("."), Path("."), Path("."), "auto", "cpu")
    eng.ready = True
    eng.current = "design"
    return eng


def test_normal_inference_is_not_flagged_stuck(qa, engine):
    """正常推理期间 check_stall() 必须一直是 False。"""
    engine.mark_busy(1)
    assert engine.check_stall() is False
    assert engine.busy_seconds() >= 0
    engine.mark_idle()
    assert engine.busy_seconds() == 0.0


def test_stuck_after_deadline(qa, engine, monkeypatch):
    """超过上限 → 判定卡死并写清原因。"""
    monkeypatch.setattr(qa, "MAX_SYNTH_SECONDS", 0)
    engine.mark_busy(1)
    assert engine.check_stall() is True
    assert engine.stuck is True
    assert "stuck" in (engine.error or "")


def test_batch_gets_extra_time_budget(qa, engine, monkeypatch):
    """批量请求文本多，截止时间要往后延，不能误判。"""
    monkeypatch.setattr(qa, "MAX_SYNTH_SECONDS", 100)
    monkeypatch.setattr(qa, "EXTRA_SECONDS_PER_TEXT", 30)
    engine.mark_busy(1)
    single = engine.busy_deadline - engine.busy_since
    engine.mark_idle()
    engine.mark_busy(128)
    batch = engine.busy_deadline - engine.busy_since
    assert single == 100
    assert batch == 100 + 30 * 127


def test_health_returns_503_when_stuck(qa, engine, monkeypatch):
    """卡死时 /health 必须说 503 —— 这是守护脚本重启它的唯一依据。"""
    monkeypatch.setattr(qa, "engine", engine, raising=False)
    monkeypatch.setattr(qa, "MAX_SYNTH_SECONDS", 0)
    c = TestClient(qa.app)
    engine.mark_busy(1)
    r = c.get("/health")
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["status"] == "stuck", body
    assert "stuck" in body["detail"], body


def test_model_info_exposes_stuck_flag(qa, engine, monkeypatch):
    monkeypatch.setattr(qa, "engine", engine, raising=False)
    monkeypatch.setattr(qa, "MAX_SYNTH_SECONDS", 0)
    c = TestClient(qa.app)
    engine.mark_busy(1)
    body = c.get("/v1/model").json()
    assert body["stuck"] is True
    assert body["max_synth_seconds"] == 0


def test_stuck_engine_rejects_new_requests_with_503(qa, engine, monkeypatch):
    """卡死期间的新请求要 503（说清原因），不是继续堆 409。"""
    monkeypatch.setattr(qa, "engine", engine, raising=False)
    monkeypatch.setattr(qa, "MAX_SYNTH_SECONDS", 0)
    c = TestClient(qa.app)
    engine.mark_busy(1)
    r = c.post("/v1/audio/speech", data={"text": "测试", "cfg_scale": "1",
                                          "seed": "42"})
    assert r.status_code == 503, r.text
    assert "stuck" in r.text.lower()


def test_recovery_clears_stuck_flag(qa, engine, monkeypatch):
    """推理居然回来了 → 解除卡死，否则引擎会被永久判死。"""
    monkeypatch.setattr(qa, "MAX_SYNTH_SECONDS", 0)
    engine.mark_busy(1)
    assert engine.check_stall() is True
    engine.mark_idle()
    assert engine.stuck is False
    assert engine.error is None


def test_watchdog_does_not_crash_on_engine_without_state(engine):
    """看门狗不能因为异常状态把整个线程搞死。"""
    engine.busy_since = None
    engine.busy_deadline = None
    assert engine.check_stall() is False
    # busy_deadline 有值但 busy_since 被清掉（异常顺序）也不能误判
    engine.busy_deadline = time.time() - 10
    assert engine.check_stall() is False
