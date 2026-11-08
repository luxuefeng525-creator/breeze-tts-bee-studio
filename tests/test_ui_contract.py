# coding: utf-8
"""前端契约的静态守卫：不启服务、不装浏览器也能跑，用来挂 CI / pre-commit。

这里只放「能静态判定」的规则，真布局要靠 `tools/ui_audit.sh`（需要面板 + 浏览器）：

1. `tools/ui_lint.py` 干净——有 id 但 JS 从没引用、或连 id 和 data-* 都没有的按钮，
   点下去毫无反应，比没有按钮更糟（历史上有过 5 个）。
2. CSS 里的 `[data-*]` 选择器必须有代码真的输出这个属性——`[data-type="clone"]`
   写好之后模板从没输出 `data-type`，克隆音色的高亮就这么静默失效了几个月。
3. JS 里 `$("id")` / `getElementById("id")` 取的 id 必须在 index.html 里存在——
   拼错一个字母的表现是「点了没反应」，很难从代码里看出来。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "web" / "index.html"
JS = ROOT / "web" / "app.js"
CSS = [ROOT / "web" / "styles.css", ROOT / "web" / "app.css"]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_ui_lint_is_clean() -> None:
    res = subprocess.run([sys.executable, str(ROOT / "tools" / "ui_lint.py")],
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, f"死控件体检不通过：\n{res.stdout}{res.stderr}"


def test_css_data_selectors_are_emitted() -> None:
    css = "\n".join(_read(p) for p in CSS)
    js = _read(JS)
    html = _read(HTML)
    attrs = sorted(set(re.findall(r"\[data-([a-z][a-z-]*)", css)))
    missing = []
    for a in attrs:
        camel = "dataset." + re.sub(r"-([a-z])", lambda m: m.group(1).upper(), a)
        if f'data-{a}=' in html or f'data-{a}=' in js or camel in js:
            continue
        missing.append(a)
    assert not missing, (
        "CSS 里用了这些 data-* 选择器，但 HTML/JS 从来不输出对应属性，"
        f"规则等于死代码：{', '.join(missing)}"
    )


def test_referenced_ids_exist() -> None:
    js = _read(JS)
    html = _read(HTML)
    # 有些 id 是 JS 模板里拼出来的（例如「立即补齐角色」按钮），
    # 只要 app.js 自己产出过就算存在
    ids = set(re.findall(r'id="([^"]+)"', html))
    ids |= set(re.findall(r'id="([A-Za-z0-9_-]+)"', js))
    used = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', js))
    used |= set(re.findall(r'getElementById\("([A-Za-z0-9_-]+)"\)', js))
    missing = sorted(used - ids)
    assert not missing, f"JS 里取了 index.html 中不存在的 id：{', '.join(missing)}"
