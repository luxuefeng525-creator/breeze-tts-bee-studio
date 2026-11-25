#!/usr/bin/env python3
"""
route-lint.py — 静态扫描 api/router.py 路由表，发现三类静默 bug：

1. METHOD 函数错位：GET 分支写进了 handle_post（或反之）
2. parts[N] 索引错误：对照路径结构，代码里 parts[N] 索引算错
3. handle_* 函数内 method 注释与代码不一致

用法：
    python tools/route-lint.py               # 默认扫 api/router.py
    python tools/route-lint.py api/router.py # 指定文件
    python tools/route-lint.py -q            # 只报 ERROR，不报 INFO
    python tools/route-lint.py --fix         # （暂不支持）自动修复建议输出

退出码：0 = 全绿；1 = 有 ERROR；2 = 有 ERROR + 有 WARN
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ------------------------------------------------------------
# 数据结构
# ------------------------------------------------------------

@dataclass
class RouteEntry:
    """一条路由分支（从 # ---- METHOD /path 注释到下一条路由注释之间的代码）"""
    method: str           # GET / POST / DELETE
    path: str             # /api/bee/script/{pid}
    comment_line: int     # 注释所在行号（1-based）
    body_start_line: int  # 分支代码起始行
    body_end_line: int    # 分支代码结束行
    parent_func: str      # handle_get / handle_post / handle_delete
    parent_func_start: int
    parent_func_end: int
    parts_accesses: list = field(default_factory=list)  # [(line, idx), ...]
    raw_guard: str = ""   # `if parts == [...]` 或 `if len(parts)==N and parts[0:M]==[...]` 原文


# ------------------------------------------------------------
# 核心正则
# ------------------------------------------------------------

# 路由注释：# ---- [METHOD] /api/xxx/{param} —— 可选说明 ----
# METHOD 可缺省（router.py 里部分内联分支注释没写 GET/POST）
RE_ROUTE_COMMENT = re.compile(
    r"^\s*#\s*----\s*(?:(GET|POST|PUT|DELETE)\s+)?(\S+)"
)

# 函数定义：def handle_get(h: ...) -> bool:
RE_FUNC_DEF = re.compile(
    r"^def\s+(handle_(?:get|post|delete))\s*\("
)

# parts 匹配守卫（抓 if 行）
RE_IF_PARTS_EXACT = re.compile(r'if\s+parts\s*==\s*\[([^\]]+)\]')
RE_IF_PARTS_SLICE = re.compile(r'if\s+len\(parts\)\s*==\s*(\d+)\s+and\s+parts\[0:(\d+)\]\s*==\s*\[([^\]]+)\]')
RE_IF_PARTS_GENERAL = re.compile(r'if\s+.*parts\[')  # 兜底：任何涉及 parts 的 if

# parts[N] 访问（只抓整数索引，跳过 parts[a:b] 切片、parts[xxx] 变量索引）
RE_PARTS_ACCESS = re.compile(r'parts\[(\d+)\]')


# ------------------------------------------------------------
# 解析：读文件 → 切函数 → 切路由分支
# ------------------------------------------------------------

def parse_file(src: str):
    """返回 (func_ranges, route_entries)"""
    lines = src.splitlines()
    func_ranges: dict[str, tuple[int, int]] = {}  # func_name → (start_line_1based, end_line_1based)
    func_body_by_line: dict[int, str] = {}         # line_no → func_name

    # 1. 找三个 handle_* 函数的起止行
    current_func = None
    current_func_start = 0
    brace_depth = 0

    for i, line in enumerate(lines, start=1):
        m = RE_FUNC_DEF.match(line)
        if m:
            # 新函数开始 → 先关闭上一个
            if current_func:
                func_ranges[current_func] = (current_func_start, i - 1)
                # 回填 func_body_by_line 只在函数体内标记；这里我们在填
            current_func = m.group(1)
            current_func_start = i
            brace_depth = line.count("{") - line.count("}")
        elif current_func:
            brace_depth += line.count("{") - line.count("}")
            # 函数结束条件：遇到下一个 def 或顶层缩进回到 0
            if line.strip() and not line.startswith(" ") and not line.startswith("\t") and not line.startswith("#"):
                if brace_depth <= 0 and current_func_start < i - 1:
                    func_ranges[current_func] = (current_func_start, i - 1)
                    current_func = None
                    current_func_start = 0

    if current_func:
        func_ranges[current_func] = (current_func_start, len(lines))

    # 给每行标记属于哪个函数（只在函数体内部标记）
    for fname, (fs, fe) in func_ranges.items():
        for ln in range(fs, fe + 1):
            func_body_by_line[ln] = fname

    # 2. 在整个文件里找所有路由注释（不管有没有 METHOD 前缀）
    routes: list[RouteEntry] = []

    def infer_method(parent_func: str) -> str:
        """METHOD 注释缺省时从所在函数推断"""
        return {
            "handle_get": "GET",
            "handle_post": "POST",
            "handle_delete": "DELETE",
        }.get(parent_func, "UNKNOWN")

    comment_positions = []
    for i, line in enumerate(lines, start=1):
        m = RE_ROUTE_COMMENT.match(line)
        if m:
            explicit_method = m.group(1)  # None 表示没写 METHOD
            path = m.group(2)
            # 排除分隔线：path 几乎全是 '-'
            stripped = path.lstrip("/")
            if len(stripped) < 4 or all(c == "-" for c in stripped):
                continue
            parent_func = func_body_by_line.get(i, "TOPLEVEL")
            method = explicit_method or infer_method(parent_func)
            comment_positions.append((i, method, path, parent_func, bool(explicit_method)))

    # 按 start 排序后切区间
    for idx, (start_ln, method, path, parent_func, explicit) in enumerate(comment_positions):
        # end = 下一条路由注释的前一行；或文件尾；或函数结束
        if idx + 1 < len(comment_positions):
            end_ln = comment_positions[idx + 1][0] - 1
        else:
            end_ln = len(lines)
        # 函数内分支不能跨越函数边界
        pf_end = func_ranges.get(parent_func, (0, len(lines)))[1]
        end_ln = min(end_ln, pf_end)

        body_start = start_ln + 1
        body_end = max(body_start, end_ln)

        route = RouteEntry(
            method=method,
            path=path,
            comment_line=start_ln,
            body_start_line=body_start,
            body_end_line=body_end,
            parent_func=parent_func,
            parent_func_start=func_ranges.get(parent_func, (0, 0))[0],
            parent_func_end=func_ranges.get(parent_func, (0, 0))[1],
        )

        # 扫描分支内的 parts 访问
        for ln in range(body_start, body_end + 1):
            text = lines[ln - 1]
            if not route.raw_guard and ("if parts" in text or "len(parts)" in text):
                route.raw_guard = text.strip()
            for m2 in RE_PARTS_ACCESS.finditer(text):
                route.parts_accesses.append((ln, int(m2.group(1))))

        routes.append(route)

    return func_ranges, routes


# ------------------------------------------------------------
# 检查逻辑
# ------------------------------------------------------------

FUNC_TO_METHOD = {
    "handle_get": {"GET"},
    "handle_post": {"POST", "PUT"},
    "handle_delete": {"DELETE"},
}


def method_label_to_func_name(method: str) -> str:
    mapping = {"GET": "handle_get", "POST": "handle_post",
               "PUT": "handle_post", "DELETE": "handle_delete"}
    return mapping.get(method, "???")


def parse_expected_parts(path: str):
    """把 '/api/bee/script/{pid}' 拆成 ['api','bee','script','{pid}']
    （去掉首斜杠、按 / 切）。返回 expected_len 和 expected 列表。"""
    segments = [s for s in path.strip("/").split("/") if s]
    return segments


def check(route: RouteEntry) -> list[tuple[str, str, int]]:
    """返回 [(severity, message, line), ...]  severity: ERROR / WARN / INFO"""
    issues = []

    # --- 检查 1：函数错位 ---
    expected_func = method_label_to_func_name(route.method)
    if route.parent_func != expected_func:
        issues.append((
            "ERROR",
            f"{route.method} 路由写在 {route.parent_func} 里，应该在 {expected_func} 里",
            route.comment_line,
        ))

    # --- 检查 2：parts 索引错误 ---
    expected_segments = parse_expected_parts(route.path)
    expected_len = len(expected_segments)

    if route.parts_accesses:
        for ln, idx in route.parts_accesses:
            if idx >= expected_len:
                # 索引越界 → 必错
                issues.append((
                    "ERROR",
                    f"parts[{idx}] 索引越界：路径 {route.path} 只有 {expected_len} 段"
                    f"（{expected_segments}），最多 parts[{expected_len - 1}]",
                    ln,
                ))
            else:
                # 索引对了也标 INFO，帮助确认
                label = expected_segments[idx]
                if label.startswith("{") and label.endswith("}"):
                    issues.append((
                        "INFO",
                        f"parts[{idx}] = PID 段 ({label}) ✓",
                        ln,
                    ))

    # --- 检查 3：守卫行 parts == [...] 和路径段数是否一致 ---
    if route.raw_guard:
        # exact match: if parts == ["api","bee","video","jobs"]
        m_exact = RE_IF_PARTS_EXACT.search(route.raw_guard)
        if m_exact:
            # 数一下逗号分隔的字符串数量
            items = m_exact.group(1)
            # 简单计数：按引号 + 逗号
            quoted = re.findall(r'"([^"]*)"', items)
            if quoted:
                if len(quoted) != expected_len:
                    issues.append((
                        "WARN",
                        f"守卫行 parts == [{quoted}]({len(quoted)} 段)"
                        f" 与路径声明 {route.path}({expected_len} 段) 段数不一致",
                        route.body_start_line,
                    ))

        # slice match: if len(parts) == 4 and parts[0:3] == ["api","bee","script"]
        m_slice = RE_IF_PARTS_SLICE.search(route.raw_guard)
        if m_slice:
            claimed_len = int(m_slice.group(1))
            claimed_start = int(m_slice.group(2))
            if claimed_len != expected_len:
                issues.append((
                    "WARN",
                    f"守卫行 len(parts) == {claimed_len}"
                    f" 与路径声明段数 {expected_len} 不一致",
                    route.body_start_line,
                ))
            items = m_slice.group(3)
            quoted = re.findall(r'"([^"]*)"', items)
            if quoted and claimed_start != len(quoted):
                issues.append((
                    "WARN",
                    f"守卫行 parts[0:{claimed_start}] == [{quoted}]({len(quoted)} 段)"
                    f" 切片起点与数组长度不匹配",
                    route.body_start_line,
                ))

    return issues


# ------------------------------------------------------------
# 主入口
# ------------------------------------------------------------

def main(argv: list[str]) -> int:
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0

    quiet = "-q" in argv
    args = [a for a in argv if a not in ("-q",)]
    target = Path(args[1]) if len(args) > 1 else Path("api/router.py")

    if not target.is_file():
        print(f"route-lint: 文件不存在: {target}")
        return 2

    src = target.read_text(encoding="utf-8")
    func_ranges, routes = parse_file(src)

    errors = 0
    warns = 0
    infos = 0

    print(f"route-lint · {target}")
    print(f"函数数: {len(func_ranges)}  路由分支数: {len(routes)}")
    print("=" * 60)

    for route in routes:
        issues = check(route)
        if not issues:
            if not quiet:
                print(f"  ✓ L{route.comment_line}: {route.method:6} {route.path}")
            continue

        # 打印路由标题
        first_issue_line = min(i[2] for i in issues)
        print(f"  ⚠ L{route.comment_line}: {route.method:6} {route.path}")
        for sev, msg, ln in issues:
            tag = {"ERROR": "🔴", "WARN": "🟡", "INFO": "🔵"}[sev]
            print(f"     {tag} L{ln}: {msg}")
            if sev == "ERROR":
                errors += 1
            elif sev == "WARN":
                warns += 1
            else:
                infos += 1

    print("=" * 60)
    total = errors + warns + infos
    print(f"ERROR={errors}  WARN={warns}  INFO={infos}  TOTAL={total}")

    if errors:
        return 1
    if warns:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
