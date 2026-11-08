# coding: utf-8
"""全局单执行槽的排队语义（8GB 卡跑不动并行，只能排队化）。

PRD-v4 §5 P2 的口径：后来的任务进队列、位置可见、可取消，
前一个结束后自动接上。这里守住这三件事。
"""
from __future__ import annotations

import threading
import time

import pytest

import script_routes as R


@pytest.fixture(autouse=True)
def clean_jobs():
    with R._JOBS_LOCK:
        R._JOBS.clear()
        R._JOB_QUEUE.clear()
    yield
    with R._JOBS_LOCK:
        R._JOBS.clear()
        R._JOB_QUEUE.clear()


def _rows(n=2):
    return [{"idx": i, "role_name": "甲", "text": f"第{i}句", "emotion": "平静"}
            for i in range(n)]


def _script():
    return {"id": "s_test", "title": "排队测试", "gap": 0.35}


def test_second_job_queues_with_visible_position(monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def fake_run(job, sid, todo, gap):
        started.set()
        release.wait(5)
        job["state"] = "done"

    monkeypatch.setattr(R, "_run_job", fake_run)
    rows = _rows()
    first = R._new_job("s_test", _script(), rows, rows)
    assert started.wait(2), "第一个任务应该立刻开跑"
    assert first["state"] == "running"

    second = R._new_job("s_test", _script(), rows, rows)
    assert second["state"] == "queued", second
    view = R._job_view(second)
    assert view["queue_pos"] == 1, view
    assert view["state"] == "queued"

    release.set()
    time.sleep(0.3)


def test_queued_job_can_be_cancelled(monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def fake_run(job, sid, todo, gap):
        started.set()
        release.wait(5)
        job["state"] = "done"

    monkeypatch.setattr(R, "_run_job", fake_run)
    rows = _rows()
    R._new_job("s_test", _script(), rows, rows)
    assert started.wait(2)
    second = R._new_job("s_test", _script(), rows, rows)
    assert second["state"] == "queued"

    # 模拟面板里的「停止」：排队中的任务直接出队并置终态
    with R._JOBS_LOCK:
        second["state"] = "stopped"
        second.pop("_args", None)
        if second["id"] in R._JOB_QUEUE:
            R._JOB_QUEUE.remove(second["id"])
    assert R._job_view(second)["state"] == "stopped"
    assert R._job_view(second)["queue_pos"] == 0
    release.set()
    time.sleep(0.2)


def test_queue_drains_after_previous_finishes(monkeypatch):
    release = threading.Event()
    started = threading.Event()
    ran = []

    def fake_run(job, sid, todo, gap):
        ran.append(job["id"])
        if len(ran) == 1:
            started.set()
            release.wait(5)
        job["state"] = "done"
        # 真实的 _run_job 收尾时会调用 _pump_queue（见 script_routes.py）
        R._pump_queue()

    monkeypatch.setattr(R, "_run_job", fake_run)
    rows = _rows()
    first = R._new_job("s_test", _script(), rows, rows)
    assert started.wait(2)
    second = R._new_job("s_test", _script(), rows, rows)
    assert second["state"] == "queued"

    release.set()
    # 第一个收尾时会调 _pump_queue，第二个应该被顶上来
    deadline = time.time() + 3
    while time.time() < deadline and second["state"] == "queued":
        time.sleep(0.05)
    assert second["state"] in ("running", "done"), second
    assert second["id"] in ran


def test_job_queue_is_global_not_per_script():
    """全局只有一个执行槽：不同剧本的任务也要排队。"""
    with R._JOBS_LOCK:
        R._JOBS.clear()
        R._JOB_QUEUE.clear()
        R._JOBS["j_fake"] = {"id": "j_fake", "script_id": "other",
                             "state": "running", "total": 1, "done": 0,
                             "failed": 0, "current": -1, "started_at": 0,
                             "stop": False, "durs": [], "rows": {}}
    job = R._new_job("s_test", _script(), _rows(), _rows())
    assert job["state"] == "queued"
    assert R._job_view(job)["queue_pos"] == 1
