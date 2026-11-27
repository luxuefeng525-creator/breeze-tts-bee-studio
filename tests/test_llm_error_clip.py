# coding: utf-8
"""异常文案尾部保留截断的守卫（REQ-20261128-01）。

只证明后端把「能定位问题的尾部信息」带出来了，不启服务、不连真实 LLM：
- helper 行为：长文本保留尾部、短文本原样、空值占位；
- script-gen 降级路径：call_chat 抛长异常时，响应里的 mock_reason 仍含尾部标记；
- 静态扫描：router.py 不再出现 `str(异常)[:N]` 形式的头部硬切。
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

from api import router

ROOT = Path(__file__).resolve().parents[1]
ROUTER_PY = ROOT / "api" / "router.py"

_LONG_TAIL = "[WinError 10061] ConnectionRefusedTailMarker"


def _long_exc() -> RuntimeError:
    """构造一条长度远超上限、真正信息在末尾的异常。"""
    prefix = ("Failed to establish a new connection: "
              + "x" * 260 + " ")
    return RuntimeError(prefix + _LONG_TAIL)


class _FakeHandler:
    """最小 HTTP 写入端，只接住 _h_json 写出的状态与响应体。"""

    def __init__(self) -> None:
        self.status = 0
        self.headers: list[tuple[str, str]] = []
        self.wfile = io.BytesIO()

    def send_response(self, code: int) -> None:
        self.status = code

    def send_header(self, key: str, value: str) -> None:
        self.headers.append((key, value))

    def end_headers(self) -> None:
        pass

    def body(self) -> dict:
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_long_text_keeps_tail_marker() -> None:
    out = router._clip_reason(str(_long_exc()))
    assert _LONG_TAIL in out, f"尾部关键信息被切掉：{out!r}"
    assert out.endswith(_LONG_TAIL), "截断结果没有完整保留异常末尾"
    assert len(out) <= 201, f"超出上限：{len(out)}"


def test_short_empty_none_states() -> None:
    assert router._clip_reason("短文本") == "短文本"
    assert router._clip_reason("") == "未知原因"
    assert router._clip_reason(None) == "未知原因"
    assert "None" not in router._clip_reason(None)


def test_bad_str_object_falls_back_to_placeholder() -> None:
    """异常对象的 __str__ 自己再抛错时，helper 不能二次抛错。"""

    class BadStr:
        def __str__(self) -> str:
            raise RuntimeError("str 本身挂了")

    assert router._clip_reason(BadStr()) == "未知原因"


def test_cut_happens_at_separator_without_half_word() -> None:
    """超长且在分隔符处断开时，断点后应从完整词开始，不留半词。"""
    text = ("开头套话 " + "alpha beta gamma delta " * 40 + "结尾标记")
    out = router._clip_reason(text)
    assert out.startswith("…")
    after = out[1:]
    # 断点落在空格上，后面每个片段都应是原词之一
    for piece in after.split(" "):
        if piece:
            assert piece in {"alpha", "beta", "gamma", "delta", "结尾标记"}, (
                f"出现被切开的残词：{piece!r}"
            )


def test_chinese_punctuation_and_emoji_boundary() -> None:
    """中文标点与 emoji 混入时长文本仍保留末尾，且按字符限长。"""
    text = "前" * 250 + "，" + "词 " * 80 + "😀" * 50 + "尾部标记"
    out = router._clip_reason(text)
    assert out.endswith("尾部标记"), out
    assert len(out) <= 201


def test_script_gen_degradation_preserves_tail(monkeypatch) -> None:
    """走完整 script-gen 降级分支，mock_reason 必须带尾部而不是半截前缀。"""
    def _boom(**_kwargs):
        raise _long_exc()

    monkeypatch.setattr("api.llm_client.call_chat", _boom)
    monkeypatch.setattr(router, "_script_write", lambda *a, **k: None)

    h = _FakeHandler()
    body = json.dumps({
        "topic": "测试主题",
        "shots_count": 2,
        "provider": "longcat",
        "api_key": "sk-test",
    }).encode("utf-8")
    assert router.handle_post(h, "/api/llm/script-gen", body, "application/json") is True

    data = h.body()
    assert h.status == 200, data
    assert data["used_mock"] is True
    reason = data["mock_reason"]
    assert _LONG_TAIL in reason, f"降级原因丢了尾部：{reason!r}"
    assert len(reason) <= 201, f"mock_reason 未限长：{len(reason)}"


def test_router_has_no_bare_exception_head_slice() -> None:
    """同一策略覆盖 router.py：不能再有裸的异常头部硬切。"""
    src = ROUTER_PY.read_text(encoding="utf-8")
    # 覆盖两种写法：f-string 整体切片、str(...) 切片；同时覆盖 [:N] 与 [0:N]
    pattern = (r'(?:f?"[^"\n]*\{(?:e|exc|err|error)\}[^"\n]*"'
               r'|str\([^)\n]*\))\s*\[\s*(?:\d+\s*:|:\s*\d+)\s*\]')
    hits = re.findall(pattern, src)
    assert not hits, f"router.py 仍存在异常头部硬切：{hits}"
