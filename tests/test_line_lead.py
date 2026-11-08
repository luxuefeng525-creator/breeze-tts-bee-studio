# coding: utf-8
"""抢话（行提前量）的数据口径。

* 只接受 0.05~2 秒；0 或越界值等于没开
* 提前量不进 _line_hash：调它不该把已合成好的行打回待重做
* 老剧本没有 line_lead 字段 → 全部 0，行为不变
"""
import script_routes as R


def test_lead_map_normalizes():
    assert R._lead_map({}) == {}
    assert R._lead_map({"line_lead": {}}) == {}
    m = R._lead_map({"line_lead": {"2": 0.3, "3": "0.45", "4": 0, "5": 3.5, "6": 0.01, "x": 1}})
    assert m == {2: 0.3, 3: 0.45, 4: 0.0} or m == {2: 0.3, 3: 0.45}
    assert 5 not in m and 6 not in m


def test_lead_not_in_line_hash():
    base = {"text": "你好", "role_id": "r1", "voice_id": "v1", "instruction": "温柔", "speed": 1.0}
    h1 = R._line_hash(base)
    h2 = R._line_hash({**base, "lead": 0.6})
    assert h1 == h2
