# coding: utf-8
"""可靠性修复的断言（2026-11-09 技术审计）。

审计发现的四类问题，逐条钉死，防止以后改回去：

1. 排队中的任务被 _prune_jobs 误删（没有 finished_at → 被当成「早就结束了」）
2. 状态文件损坏时 _load 返回空列表 → 下一次写入把用户数据全部覆盖
3. 静默吞异常：抢话重拼失败 / 自动配角失败 → 界面却报告成功
4. history.json 只增不减 → 每次写入都重写整个文件（O(N²)）
"""
from __future__ import annotations

import json
import threading
import time

import pytest

import panel as P
import script_routes as R


# ------------------------------------------------------------------ 1. 排队任务
def test_prune_never_drops_queued_job():
    """排队中的任务没有 finished_at，不能被当成「早结束了」清掉。

    真实后果：它从 _JOBS 消失但 id 还在 _JOB_QUEUE 里 → 永远不会被执行，
    前端只能拿到 404「任务不存在或已结束」。
    """
    with R._JOBS_LOCK:
        R._JOBS.clear()
        R._JOB_QUEUE.clear()
        R._JOBS["j_running"] = {"id": "j_running", "state": "running",
                                "started_at": time.time(), "finished_at": 0}
        R._JOBS["j_queued"] = {"id": "j_queued", "state": "queued",
                               "started_at": time.time()}
        R._JOB_QUEUE.append("j_queued")
    try:
        R._prune_jobs()
        with R._JOBS_LOCK:
            assert "j_queued" in R._JOBS, "排队任务被误删了"
            assert "j_queued" in R._JOB_QUEUE
            # 真的结束过（10 分钟前）的还是要清掉
            R._JOBS["j_old"] = {"id": "j_old", "state": "done",
                                "started_at": time.time() - 3600,
                                "finished_at": time.time() - 3600}
        R._prune_jobs()
        with R._JOBS_LOCK:
            assert "j_old" not in R._JOBS, "过期任务没被清理"
    finally:
        with R._JOBS_LOCK:
            R._JOBS.clear()
            R._JOB_QUEUE.clear()


def test_prune_cleans_dangling_queue_ids():
    with R._JOBS_LOCK:
        R._JOBS.clear()
        R._JOB_QUEUE.clear()
        R._JOB_QUEUE.append("j_gone")
    try:
        R._prune_jobs()
        with R._JOBS_LOCK:
            assert "j_gone" not in R._JOB_QUEUE
    finally:
        with R._JOBS_LOCK:
            R._JOBS.clear()
            R._JOB_QUEUE.clear()


# ------------------------------------------------------------------ 2. 数据安全
@pytest.fixture()
def data_dir(tmp_path):
    old = P.DATA
    corrupt_before = set(P._DATA_CORRUPT)
    P.DATA = tmp_path
    try:
        yield tmp_path
    finally:
        P.DATA = old
        # 写保护名单是进程级的全局状态：测试之间不能互相污染
        P._DATA_CORRUPT.clear()
        P._DATA_CORRUPT.update(corrupt_before)


def test_corrupt_state_file_is_not_silently_emptied(data_dir):
    """损坏的状态文件不能返回空列表了事：下一步写入会把它盖掉。"""
    p = data_dir / "scripts.json"
    p.write_text("{ this is not json", encoding="utf-8")
    assert P._load("scripts.json") == []
    assert "scripts.json" in P._DATA_CORRUPT, "损坏后应进入写保护名单"
    # 写保护生效：拒绝写入，而不是把空列表盖回去
    with pytest.raises(P._DataCorrupt):
        P._save("scripts.json", [])
    # 原始文件内容没被动过
    assert p.read_text(encoding="utf-8") == "{ this is not json"


def test_corrupt_state_file_recovers_from_backup(data_dir):
    """有上一版备份时直接恢复，用户无感。"""
    p = data_dir / "voices.json"
    p.write_text('{"broken": ', encoding="utf-8")
    bak = data_dir / "voices.json.bak"
    bak.write_text('[{"id": "v1", "name": "备份里的音色"}]', encoding="utf-8")
    out = P._load("voices.json")
    assert out and out[0]["name"] == "备份里的音色"
    assert "voices.json" not in P._DATA_CORRUPT
    # 主文件已被恢复成可解析的 JSON
    assert json.loads(p.read_text(encoding="utf-8"))[0]["id"] == "v1"


def test_save_keeps_previous_version_as_backup(data_dir):
    p = data_dir / "roles.json"
    P._save("roles.json", [{"id": "r1"}])
    assert not (data_dir / "roles.json.bak").exists(), "第一次写没有旧版本可备份"
    P._save("roles.json", [{"id": "r2"}])
    bak = data_dir / "roles.json.bak"
    assert bak.is_file(), "写前没有留备份"
    assert json.loads(bak.read_text(encoding="utf-8"))[0]["id"] == "r1"


def test_startup_selfcheck_reports_corruption(data_dir):
    (data_dir / "scripts.json").write_text("not json at all", encoding="utf-8")
    P._startup_selfcheck()
    assert "scripts.json" in P._DATA_CORRUPT


# ------------------------------------------------------------------ 3. 失败可见
def test_cast_failure_is_reported_not_swallowed(data_dir, monkeypatch):
    """自动配角失败必须出现在响应里，不能一声不响。

    以前是 `except Exception: pass`：用户拿到一个没有角色、没有音色的剧本，
    界面只显示「未知角色」，没人知道到底发生了什么。
    """
    import threading
    import urllib.request

    server = P.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(
        R, "_apply_cast",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("磁盘满了")))
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/scripts",
            data=json.dumps({"title": "配角失败验收", "raw": "甲：你好。",
                              "auto_cast": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
        assert payload.get("cast_error"), payload
        assert "磁盘满了" in payload["cast_error"], payload
    finally:
        server.shutdown()


def test_history_is_capped(data_dir):
    """历史只增不减会让每次写入都重写整个文件。"""
    for i in range(P.HISTORY_LIMIT + 25):
        P._append_history({"id": f"h{i}"})
    items = P._load("history.json")
    assert len(items) == P.HISTORY_LIMIT
    # 保留的是最近的
    assert items[-1]["id"] == f"h{P.HISTORY_LIMIT + 24}"


def test_log_helper_writes_to_stderr(capsys):
    R._log("test", "这是一条日志")
    err = capsys.readouterr().err
    assert "这是一条日志" in err
    assert "[test]" in err
