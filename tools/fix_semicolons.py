#!/usr/bin/env python3
"""fix_semicolons.py — 修复 tokenify 导致的 CSS 分号丢失"""
import re
from pathlib import Path

SRC = Path("web/bee-studio.html")
html = SRC.read_text(encoding="utf-8")

m = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
if not m: print("NO STYLE"); exit(1)
css, start, end = m.group(1), m.start(1), m.end(1)

before_broken = len(re.findall(r"var\([^)]+\)\s*\n\s*[a-z-]+\s*:", css))

lines = css.split("\n")
fixed = []
for i, line in enumerate(lines):
    stripped = line.strip()
    # 如果行以 var(...) 结尾且没分号，检查下一行是不是新声明
    if stripped and not stripped.endswith((";", "}", "{", "/*", "*/")):
        if re.search(r"var\([^)]+\)\s*$", stripped):
            fixed.append(line.rstrip() + ";")
            continue
        # 还有：padding: var(--space-1) var(--space-2) 这种
        if re.search(r"^\s*(?:margin|padding|gap|font-size|border-radius|width|height)\s*:\s*[^;}]*var\(", stripped):
            if not stripped.endswith(";"):
                fixed.append(line.rstrip() + ";")
                continue
    fixed.append(line)

css = "\n".join(fixed)
after_broken = len(re.findall(r"var\([^)]+\)\s*\n\s*[a-z-]+\s*:", css))

print(f"Broken: {before_broken} -> {after_broken}")

new_html = html[:start] + css + html[end:]
SRC.write_text(new_html, encoding="utf-8")
print("Fixed!")
