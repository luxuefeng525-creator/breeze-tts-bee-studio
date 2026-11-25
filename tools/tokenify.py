#!/usr/bin/env python3
"""tokenify.py — replace magic number px values in bee-studio.html CSS with var(--token)"""
import re, sys
from pathlib import Path

SRC = Path("web/bee-studio.html")
html = SRC.read_text(encoding="utf-8")

m = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
if not m: print("NO STYLE"); sys.exit(1)
css, start, end = m.group(1), m.start(1), m.end(1)

def do_props(text, props, rmap):
    prop_alt = "|".join(props)
    pattern = re.compile(rf"\b({prop_alt})(?:-[a-z]+)?:\s*([^;]+);?")
    def _repl(m):
        prop = m.group(1)
        values_str = m.group(2)
        changes = []
        def _vrepl(vm):
            vi = int(vm.group(1))
            changes.append(1)
            return rmap.get(vi, vm.group(0))
        new = re.sub(r"(\d+)px", _vrepl, values_str)
        if not changes: return m.group(0)
        return f"{prop}: {new}"
    return pattern.sub(_repl, text)

# BEFORE
b_s = len(re.findall(r"(?:margin|padding|gap|column-gap|row-gap)\s*:\s*\d+px\b", css))
b_f = len(re.findall(r"font-size\s*:\s*\d+px\b", css))
b_r = len(re.findall(r"border-radius\s*:\s*\d+px\b", css))

# SPACING
space_map = {1:"var(--space-1)", 2:"var(--space-1)", 3:"var(--space-1)",
             4:"var(--space-1)", 5:"var(--space-1)", 6:"var(--space-1)", 7:"var(--space-1)",
             8:"var(--space-2)", 9:"var(--space-2)", 10:"var(--space-2)", 11:"var(--space-3)",
             12:"var(--space-3)", 13:"var(--space-3)", 14:"var(--space-3)",
             15:"var(--space-4)", 16:"var(--space-4)", 18:"var(--space-5)",
             20:"var(--space-5)", 22:"var(--space-6)", 24:"var(--space-6)", 26:"var(--space-7)",
             28:"var(--space-7)", 32:"var(--space-8)"}
css = do_props(css, ["margin","padding","gap","column-gap","row-gap"], space_map)

# FONT
font_map = {10:"var(--font-xs)", 10.5:"var(--font-xs)", 11:"var(--font-xs)", 11.5:"var(--font-xs)",
            12:"var(--font-sm)", 12.5:"var(--font-sm)", 13:"var(--font-base)", 13.5:"var(--font-base)",
            14:"var(--font-md)", 16:"var(--font-lg)", 18:"var(--font-lg)", 20:"var(--font-xl)",
            22:"var(--font-2xl)", 24:"var(--font-2xl)", 28:"var(--font-2xl)"}
# font-size 有小数
def do_font(text):
    def _repl(m):
        val = float(m.group(1))
        token = font_map.get(int(val) if val == int(val) else val)
        return token if token else m.group(0)
    return re.sub(r"font-size:\s*([\d.]+)px\b", _repl, text)
css = do_font(css)

# RADIUS
radius_map = {2:"var(--radius-sm)", 3:"var(--radius-sm)", 4:"var(--radius-sm)", 5:"var(--radius-sm)",
              6:"var(--radius-sm)", 8:"var(--radius-md)", 10:"var(--radius-md)",
              12:"var(--radius-lg)", 14:"var(--radius-lg)", 16:"var(--radius-xl)",
              999:"var(--radius-pill)"}
css = do_props(css, ["border-radius"], radius_map)

# AFTER
a_s = len(re.findall(r"(?:margin|padding|gap|column-gap|row-gap)\s*:\s*\d+px\b", css))
a_f = len(re.findall(r"font-size\s*:\s*\d+px\b", css))
a_r = len(re.findall(r"border-radius\s*:\s*\d+px\b", css))

print(f"Spacing: {b_s} -> {a_s}  (-{b_s-a_s})")
print(f"Font:    {b_f} -> {a_f}  (-{b_f-a_f})")
print(f"Radius:  {b_r} -> {a_r}  (-{b_r-a_r})")

new_html = html[:start] + css + html[end:]
SRC.write_text(new_html, encoding="utf-8")
print(f"Written {SRC}")
