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


def _gen_handler_seg(js: str) -> str:
    """取生成按钮事件处理函数的代码窗口，供多条断言共用。"""
    marker = "$('#scriptModalGen').addEventListener"
    assert marker in js, "找不到生成按钮的事件绑定"
    return js.split(marker, 1)[1][:9000]


def test_success_toast_only_when_not_degraded() -> None:
    """真实 LLM 成功时保留原 success 提示；降级时改弹 warn，不再报成功。"""
    js = _read()
    seg = _gen_handler_seg(js)
    assert re.search(r"usedMock\s*&&\s*[^\n]{0,120}force_mock", seg), (
        "生成按钮处理未按 usedMock 分支，降级会被当成成功"
    )
    assert "'warn'" in seg, "降级分支没有用 warn 级提示"
    # 后端 mock_reason 已限长 200，前端必须放宽，否则尾部截断会丢掉头部信息
    assert re.search(r"truncateReason\(\s*result\.mockReason\s*,\s*(?:[3-9]\d\d|\d{4,})\s*\)", seg), (
        "降级原因用了默认短长度截断，头部错误信息会丢失"
    )
    # 真实成功走 else，其中必须还留着 success 提示（防「修降级顺手删成功提示」的回归）
    parts = re.split(r"\}\s*else\s*\{", seg, maxsplit=1)
    assert len(parts) == 2 and "'success'" in parts[1][:800], (
        "降级判定之外的 success 提示被误删（回归）"
    )
    # 「已创建」必须被取反条件包住（降级时抑制），且不绑具体变量名，改名不该误红
    lines = seg.splitlines()
    hits = [i for i, ln in enumerate(lines) if "已创建" in ln and "showToast" in ln]
    assert hits, "生成成功后不再提示「已创建」（回归）"
    for i in hits:
        window = "\n".join(lines[max(0, i - 6):i + 1])
        assert re.search(r"(?<![.\w])if\s*\(\s*!\s*[A-Za-z_$][\w$]*\s*\)", window), (
            f"「已创建」未被取反条件包住，降级时仍会弹（行 {i}）：{lines[i].strip()}"
        )
    assert "已用演示数据创建" not in js, "降级场景不再另弹第二条重复提示"


def test_catch_path_marks_degraded_and_uses_backend_error() -> None:
    """异常路径（HTTP 错/超时）同样算降级，且要优先展示后端 error 文案。"""
    js = _read()
    seg = _gen_handler_seg(js)
    m = re.search(r"catch\s*\(\s*\w+\s*\)\s*\{", seg)
    assert m, "找不到生成按钮的 catch 分支"
    block = seg[m.end():m.end() + 900]
    assert re.search(r"\b\w+\s*=\s*true\b", block), (
        "catch 分支未把降级标志置真，之后还会弹绿色成功提示"
    )
    assert "payload" in block and "error" in block, (
        "catch 未使用 apiBT 挂出的后端 error 文案，用户只会看到状态码"
    )


def test_apibt_keeps_error_payload() -> None:
    """apiBT 非 2xx 时必须把响应体带出来，否则后端的人话错误丢失。"""
    js = _read()
    seg = js.split("async function apiBT", 1)
    assert len(seg) == 2, "找不到 apiBT"
    body = seg[1].split("\n}", 1)[0]
    assert "payload" in body, "apiBT 抛错时未挂载响应体"


def test_reason_is_truncated() -> None:
    """原因文本必须截断，且不能把 emoji 代理对切成乱码。"""
    js = _read()
    assert "truncateReason" in js, "缺少 truncateReason 截断工具"
    assert re.search(r"truncateReason\(", js), "truncateReason 定义了但没被调用"
    seg = js.split("function truncateReason", 1)
    assert len(seg) == 2, "找不到 truncateReason 定义"
    body = seg[1].split("\n}", 1)[0]
    assert "length<=max" in body.replace(" ", ""), "缺长度上限判断，长文本不会截断"
    assert "Array.from" in body, "未按码点切分，emoji 代理对会被切成乱码"
    assert "'/'" not in body, "用 '/' 当分隔符会在原因以 URL 结尾时切掉 host"
    assert "TAIL_MIN" in body, (
        "缺少「尾巴太短就不切」的保护，原因可能被截成一两个字符"
    )


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
    assert "❌ 改镜头失败" in js, "原有失败提示文案丢失（回归）"
    parts = js.split("❌ 改镜头失败", 1)
    assert len(parts) == 2, "失败文案存在但取不到后续片段"
