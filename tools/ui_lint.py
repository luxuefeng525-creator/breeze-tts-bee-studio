# coding: utf-8
"""前端死控件体检：HTML 里有 id、但 JS 从来没碰过。

这类控件在界面上看着能点，点下去毫无反应——比「没有这个按钮」更糟，
用户会以为是自己操作错了或程序卡了。2026-11-08 用它扫出 4 个
（查看诊断 / 试听对比 / 队列 0 / 历史筛选），已接线或移除。

用法：
    python tools/ui_lint.py           # 人读
    python tools/ui_lint.py --json    # 机器读

退出码：0 干净；1 有可疑控件。

为什么不能一刀切「id 没被 JS 引用就是死的」：
* `i-*`      —— <symbol> 图标定义，由 <use href="#i-x"> 引用，JS 不必碰
* `screen-*` —— 屏幕容器，显隐由 body[data-screen] 的 CSS 控制
* 纯文案占位 —— 由 JS 用 textContent 写入时会命中，但静态文案不算死控件
下面用白名单 + 「是否出现在 JS 字符串里」两段判断，宁可少报不乱报。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "web" / "index.html"
JS = ROOT / "web" / "app.js"

# 图标 symbol 与屏幕容器：本来就不需要 JS 引用
IGNORE_PREFIX = ("i-", "screen-")
# 纯展示型节点（文案/容器），由 CSS 或渲染函数整体替换，不必单独引用
IGNORE_IDS = {
    "cloneStepper",   # 由 updateStepper 按 data-step 操作，不按 id 取
    "transcriptNote",
    "scriptDesc",
}


def main() -> int:
    html = HTML.read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")

    # 只统计可交互元素上的 id：button / input / textarea / select / a / dialog
    ids = re.findall(
        r"<(button|input|textarea|select|a|dialog)\b[^>]*\bid=\"([A-Za-z0-9_-]+)\"",
        html,
    )
    seen: dict[str, str] = {}
    for tag, i in ids:
        seen.setdefault(i, tag)

    dead = []
    for i, tag in sorted(seen.items()):
        if i.startswith(IGNORE_PREFIX) or i in IGNORE_IDS:
            continue
        if i in js:            # 直接按 id 取（$("x") / getElementById）
            continue
        dead.append({"id": i, "tag": tag})

    if "--json" in sys.argv:
        print(json.dumps({"checked": len(seen), "suspects": dead},
                         ensure_ascii=False, indent=2))
    else:
        print(f"可交互 id {len(seen)} 个 · 可疑死控件 {len(dead)} 个")
        for d in dead:
            print(f"  <{d['tag']} id=\"{d['id']}\">  JS 未引用")
        if dead:
            print("\n要么接线，要么从界面上拿掉——别让用户点了没反应。")
    return 1 if dead else 0


if __name__ == "__main__":
    sys.exit(main())
