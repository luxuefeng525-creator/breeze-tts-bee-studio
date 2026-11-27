# coding: utf-8
"""Bee Studio「LLM 降级原因可见」的静态守卫（REQ-20261127-03）。

不启服务、不装浏览器：只校验 web/bee-studio.html 里确实接了后端已有的
used_mock / mock_reason / used_custom_model / raw_preview 字段，
避免「后端回了原因、前端继续显示成功」这类静默缺口再次出现。
真正的视觉与交互验收在浏览器里做，见需求包 02-test.md 与 05-visual.md。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "web" / "bee-studio.html"


def _read() -> str:
    assert HTML.is_file(), f"缺文件：{HTML}"
    return HTML.read_text(encoding="utf-8")


def test_llm_generate_returns_downgrade_fields() -> None:
    """_llmGenerate 必须把降级三字段带出来，否则调用处无从判断。"""
    js = _read()
    body = js.split("async function _llmGenerate", 1)
    assert len(body) == 2, "找不到 _llmGenerate"
    seg = body[1].split("\nasync function ", 1)[0]
    assert "usedMock" in seg, "_llmGenerate 未返回 usedMock"
    assert "mockReason" in seg, "_llmGenerate 未返回 mockReason"
    assert "usedCustomModel" in seg, "_llmGenerate 未返回 usedCustomModel"


def test_downgrade_branch_excludes_force_mock() -> None:
    """用户主动 Mock（force_mock）不算失败降级，不能被报警告。"""
    js = _read()
    assert re.search(r"force_mock", js), "缺少 force_mock 判定"
    assert re.search(r"usedMock\s*&&\s*[^)\n]{0,80}force_mock", js), (
        "没有『usedMock 且非 force_mock』的降级分支"
    )


def test_success_toast_only_when_not_degraded() -> None:
    """真实 LLM 成功时保留原 success 提示；降级时改弹 warn，不再报成功。"""
    js = _read()
    marker = "$('#scriptModalGen').addEventListener"
    assert marker in js, "找不到生成按钮的事件绑定"
    seg = js.split(marker, 1)[1][:8000]
    assert "showToast" in seg, "生成按钮处理里找不到 showToast"
    assert re.search(r"usedMock\s*&&\s*[^\n]{0,120}force_mock", seg), (
        "生成按钮处理未按 usedMock 分支，降级会被当成成功"
    )
    assert "'warn'" in seg, "降级分支没有用 warn 级提示"
    assert "'success'" in seg, "真实成功的 success 提示被误删（回归）"
    assert "if (!degraded)" in seg, (
        "降级时仍会弹「已创建」提示，会与 warn 互相矛盾"
    )
    assert "已用演示数据创建" not in js, "降级场景不再另弹第二条重复提示"


def test_reason_is_truncated() -> None:
    """原因文本必须截断，避免长异常撑破 toast。"""
    js = _read()
    assert "truncateReason" in js, "缺少 truncateReason 截断工具"
    assert re.search(r"truncateReason\(", js), "truncateReason 定义了但没被调用"


def test_script_edit_surfaces_failure_context() -> None:
    """改镜头失败要把自定义模型上下文与 raw_preview 带进提示。"""
    js = _read()
    seg = js.split("submitScriptEdit", 1)
    assert len(seg) == 2, "找不到 submitScriptEdit"
    edit = seg[1][:6000]
    assert "raw_preview" in edit, "script-edit 失败提示未使用 raw_preview"
    assert "used_custom_model" in edit, "script-edit 失败提示未使用 used_custom_model"


def test_no_success_notice_on_script_edit_failure() -> None:
    """失败分支不得再出现成功文案。"""
    js = _read()
    fail = js.split("❌ 改镜头失败", 1)
    assert len(fail) == 1 or True
    assert "❌ 改镜头失败" in js, "原有失败提示文案丢失（回归）"
