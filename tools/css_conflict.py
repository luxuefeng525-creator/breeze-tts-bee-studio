# coding: utf-8
"""两份 CSS 之间的冲突体检。

web/styles.css 与 web/app.css 都会定义同名选择器，靠 `<link>` 的加载顺序
决定谁生效。历史上就踩过：styles.css 里的 `.voice-card{height:200px}` 被
app.css 的 `height:auto` 悄悄盖掉，音色卡在窄屏被压扁，几个月没人发现。

判定规则（避免误报）：
- 只比较**同一条 @media 上下文**里的同名选择器（媒体查询内的规则只在
  那个断点生效，与文件顶层的规则本来就不冲突）
- 同一个选择器、同一个属性、两个文件里写了**不同的值** → 冲突（后者生效）
- 两边值相同 → 无害，不报
- app.css 里在媒体查询内覆盖 styles.css 顶层 → 属正常做法，不报

用法：python tools/css_conflict.py
退出码：0 = 无冲突；1 = 有冲突。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = [("styles.css", ROOT / "web" / "styles.css"),
         ("app.css", ROOT / "web" / "app.css")]

# 只关心会直接影响布局/可见性的属性
WATCH = {
    "display", "position", "width", "height", "max-width", "max-height",
    "min-width", "min-height", "grid-template-columns", "grid-template-rows",
    "grid-template-areas", "grid-area", "flex", "flex-direction", "flex-wrap",
    "gap", "overflow", "overflow-x", "overflow-y", "padding", "margin",
    "border", "border-radius", "background", "background-color", "color",
    "font-size", "line-height", "visibility", "opacity", "z-index",
    "white-space", "text-overflow", "pointer-events", "cursor",
}

_PROP = re.compile(r"([-a-zA-Z]+)\s*:\s*([^;]+);")


def parse(path: Path) -> dict:
    """→ {(媒体上下文, 选择器): {属性: 值}}。

    媒体上下文为空串表示文件顶层规则。嵌套只支持一层 @media（本项目够用）。
    """
    out: dict = {}
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    pos = 0
    media = ""          # 当前所在的 @media 条件
    while pos < len(text):
        brace = text.find("{", pos)
        if brace < 0:
            break
        head = text[pos:brace].strip()
        # 找到这一层块的结束位置
        depth, i = 1, brace + 1
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        body = text[brace + 1:i - 1]
        if head.startswith("@media"):
            # 媒体查询：递归进去，上下文加上这个条件
            for sel, props in _rules(body).items():
                out[(f"{media} & {head}".strip(), sel)] = props
        elif head.startswith("@"):
            pass        # @keyframes / @font-face 等，不参与比较
        else:
            sel = " ".join(head.split())
            props = {}
            for prop, val in _PROP.findall(body):
                prop = prop.strip().lower()
                val = " ".join(val.split())
                if prop in WATCH:
                    props[prop] = val
            if props:
                out[(media, sel)] = props
        pos = i
    return out


def _rules(body: str) -> dict:
    """解析一层嵌套规则（@media 内部）。"""
    out: dict = {}
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", body, re.S):
        sel = " ".join(m.group(1).split())
        if not sel or sel.startswith("@"):
            continue
        props = {}
        for prop, val in _PROP.findall(m.group(2)):
            prop = prop.strip().lower()
            val = " ".join(val.split())
            if prop in WATCH:
                props[prop] = val
        if props:
            out[sel] = props
    return out


def main() -> int:
    tables = [(name, parse(p)) for name, p in FILES]
    conflicts = []
    for (name_a, a), (name_b, b) in zip(tables, tables[1:]):
        for key in sorted(set(a) & set(b)):
            media, sel = key
            for prop in sorted(set(a[key]) & set(b[key])):
                if a[key][prop] != b[key][prop]:
                    conflicts.append((name_a, name_b, media, sel, prop,
                                      a[key][prop], b[key][prop]))
    if not conflicts:
        print(f"CSS 冲突体检：{len(tables[0][1])} / {len(tables[1][1])} 条规则，无冲突")
        return 0
    print(f"CSS 冲突体检：{len(conflicts)} 处冲突"
          f"（同一媒体上下文下，后加载的文件生效）\n")
    for name_a, name_b, media, sel, prop, va, vb in conflicts:
        scope = f" @media{media}" if media else ""
        print(f"  {sel}{scope}")
        print(f"    {prop}: {name_a}={va!r}  vs  {name_b}={vb!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
