# coding: utf-8
"""抢话（行提前量）的数据口径。

* 只接受 0.05~2 秒；0 或越界值等于没开
* 提前量不进 _line_hash：调它不该把已合成好的行打回待重做
* 老剧本没有 line_lead 字段 → 全部 0，行为不变
* 拼整轨时的时间轴：抢话那一行真的提前了，且整轨变短
"""
import pytest

import panel as P
import script_routes as R


@pytest.fixture()
def isolated_data(tmp_path):
    old = (P.DATA, P.REFS, P.AUDIOS)
    P.DATA, P.REFS, P.AUDIOS = tmp_path, tmp_path / "refs", tmp_path / "audio"
    for d in (P.DATA, P.REFS, P.AUDIOS):
        d.mkdir(parents=True, exist_ok=True)
    try:
        yield
    finally:
        P.DATA, P.REFS, P.AUDIOS = old


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


# ------------------------------------------------------------------ 时间轴层
def _fake_script(sid: str, lines: list[str], lead: dict | None = None):
    script = {
        "id": sid, "title": "抢话时间轴", "raw": "\n".join(lines),
        "cast": [], "cast_isolated": True, "gap": 0.35,
        "line_lead": lead or {}, "lines": [],
    }
    P._update_json("scripts.json", lambda it: it.append(script))
    return script


def _write_line_wav(sid: str, idx: int, seconds: float = 1.0) -> str:
    out = P.DATA / "audio" / "scripts" / sid
    out.mkdir(parents=True, exist_ok=True)
    n = int(P.SAMPLE_RATE * seconds)
    pcm = b"\x00\x00" * n
    rel = f"audio/scripts/{sid}/line{idx}.wav"
    (P.DATA / rel).write_bytes(P.pcm_to_wav(pcm))
    return rel


def _mark_done(script: dict):
    """按当前解析结果算出每行的 hash，把行标成已合成并指向假音频。"""
    rows = R._enriched(script, R.parse_for(script))
    recs = []
    for row in rows:
        rel = _write_line_wav(script["id"], row["idx"])
        recs.append({
            "idx": row["idx"], "status": "done", "hash": row["hash"],
            "cfg_scale": row["cfg_scale"], "seed": row["seed"],
            "wav": rel, "duration": 1.0,
        })
    def _mut(items):
        for s in items:
            if s["id"] == script["id"]:
                s["lines"] = recs
    P._update_json("scripts.json", _mut)
    return recs


def _timeline(sid):
    for s in P._load("scripts.json"):
        if s["id"] == sid:
            return s.get("timeline") or [], s.get("duration")
    return [], None


def test_concat_without_lead_keeps_gap(isolated_data):
    sid = "s_lead_plain"
    script = _fake_script(sid, ["甲：第一句。", "甲：第二句。"])
    _mark_done(script)
    R._concat(sid, 0.35)
    tl, dur = _timeline(sid)
    assert len(tl) == 2
    assert tl[0]["start"] == 0.0 and tl[0]["end"] == 1.0
    # 没有抢话：第二行 = 第一行结束 + 停顿
    assert abs(tl[1]["start"] - 1.35) < 0.02, tl[1]
    assert abs(dur - 2.35) < 0.02, dur


def test_concat_with_lead_overlaps_and_shortens(isolated_data):
    sid = "s_lead_on"
    script = _fake_script(sid, ["甲：第一句。", "甲：第二句。"],
                          lead={"1": 0.4})
    _mark_done(script)
    R._concat(sid, 0.35)
    tl, dur = _timeline(sid)
    assert len(tl) == 2
    assert tl[1]["lead"] == 0.4, tl[1]
    # 第二行提前 0.4 秒：1.35 - 0.4 = 0.95，与第一行有 0.05 秒重叠
    assert abs(tl[1]["start"] - 0.95) < 0.02, tl[1]
    assert abs(tl[1]["end"] - 1.95) < 0.02, tl[1]
    # 整轨比不抢话时短 0.4 秒
    assert abs(dur - 1.95) < 0.02, dur
    # 重叠是真叠上了：第二行的起点早于第一行的终点
    assert tl[1]["start"] < tl[0]["end"], (tl[0], tl[1])
