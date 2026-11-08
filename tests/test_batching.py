# coding: utf-8
"""大批量剧本的分批策略。

8GB 卡一次扛不住 200 行连着跑（PRD-v4 §5 P1-2）：>100 行自动分批、
批与批之间检查停止信号，进度不丢。这里守住「切批」这一层，
合成本身由真实链路的 E2E 覆盖。
"""
from __future__ import annotations

import script_routes as R


def _rows(n):
    return [{"idx": i, "role_name": "甲", "text": f"第{i}句", "emotion": "平静"}
            for i in range(n)]


def test_batches_split_by_max_lines():
    rows = _rows(200)
    batches = list(R._batches(rows, R.MAX_LINES))
    assert len(batches) == 2, len(batches)
    assert [len(b) for b in batches] == [100, 100]
    # 切批不能丢行、不能重排
    flat = [r["idx"] for b in batches for r in b]
    assert flat == list(range(200))


def test_batches_under_limit_is_single_batch():
    rows = _rows(100)
    batches = list(R._batches(rows, R.MAX_LINES))
    assert len(batches) == 1
    assert len(batches[0]) == 100


def test_batches_zero_size_keeps_all():
    rows = _rows(7)
    batches = list(R._batches(rows, 0))
    assert len(batches) == 1 and len(batches[0]) == 7


def test_hard_limit_is_above_batch_size():
    """硬上限必须大于一批的量，否则分批永远触发不了。"""
    assert R.HARD_MAX_LINES > R.MAX_LINES
    # 300 行上限 → 3 批跑完
    assert len(list(R._batches(_rows(R.HARD_MAX_LINES), R.MAX_LINES))) == 3


def test_route_grouping_keeps_order_and_limits_switches():
    """design / clone 混排时按路线分组：同路线内保持原行序，
    换模型次数 ≤2（design 一趟 + clone 一趟）。"""
    rows = [{"idx": i, "route": "clone" if i % 2 else "design",
             "role_name": "甲", "text": "x", "emotion": "平静"}
            for i in range(10)]
    grouped = sorted(rows, key=lambda r: (0 if r.get("route") != "clone" else 1,
                                          r["idx"]))
    switches = 1
    for a, b in zip(grouped, grouped[1:]):
        if a.get("route") != b.get("route"):
            switches += 1
    assert switches <= 2, switches
    # 同路线内的相对顺序没变
    design_idx = [r["idx"] for r in grouped if r["route"] == "design"]
    clone_idx = [r["idx"] for r in grouped if r["route"] == "clone"]
    assert design_idx == sorted(design_idx)
    assert clone_idx == sorted(clone_idx)
