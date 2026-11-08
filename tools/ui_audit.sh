#!/usr/bin/env bash
# 布局 / 可访问性审计：四屏 × 四档视口，一次性把 DOM 量一遍。
#
# 依赖
#   1) 面板在跑：默认 http://127.0.0.1:7861（可用 UI_BASE_URL 覆盖）
#   2) agent-browser CLI 在 PATH 里，或 AGENT_BROWSER=/path/to/agent-browser
#
# 用法
#   bash tools/ui_audit.sh                     # 默认四屏 × 1920/1440/1280/1024
#   UI_AUDIT_VIEWPORTS="1440x900" bash tools/ui_audit.sh
#   UI_AUDIT_SKIP_LIVE=1 bash tools/ui_audit.sh  # 面板没起时：只校验本地文件
#
# 退出码：0 干净；1 有布局/可访问性问题；2 环境不满足（面板没起 / 没装 CLI）
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
JS="$ROOT/tools/ui_audit.js"
BASE="${UI_BASE_URL:-http://127.0.0.1:7861}"
AB="${AGENT_BROWSER:-}"
VIEWPORTS="${UI_AUDIT_VIEWPORTS:-1920x1080 1440x900 1280x800 1024x768}"
SCREENS="${UI_AUDIT_SCREENS:-workbench clone library script}"
TMP="${TMPDIR:-/tmp}/ui-audit-$$"

if [ -z "$AB" ]; then
  AB="$(command -v agent-browser || true)"
fi
if [ -z "$AB" ]; then
  echo "!! 找不到 agent-browser；设 AGENT_BROWSER=/path/to/agent-browser 再跑" >&2
  exit 2
fi
if ! curl -s -o /dev/null -m 3 "$BASE/"; then
  echo "!! $BASE 没响应：先起面板（.venv/Scripts/python.exe panel.py）" >&2
  exit 2
fi

mkdir -p "$TMP"
fails=0

for vp in $VIEWPORTS; do
  W="${vp%x*}"
  H="${vp#*x}"
  "$AB" set viewport "$W" "$H" > /dev/null 2>&1
  for sc in $SCREENS; do
    "$AB" open "$BASE/?screen=$sc" > /dev/null 2>&1
    sleep 2
    "$AB" eval --stdin < "$JS" > "$TMP/$sc-$W.json" 2>&1
  done
done
"$AB" close > /dev/null 2>&1

# 汇总：agent-browser 返回的是被引号包住的 JSON 字符串，先解一层再解一层
python - "$TMP" "$VIEWPORTS" <<'PY' || fails=1
import json
import pathlib
import sys

tmp = pathlib.Path(sys.argv[1])
viewports = sys.argv[2].split()
total = 0
bad = 0
for path in sorted(tmp.glob("*.json")):
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    try:
        data = json.loads(json.loads(raw)) if raw.startswith('"') else json.loads(raw)
    except Exception as exc:                      # noqa: BLE001
        print(f"  {path.name}: 解析失败 {exc}")
        bad += 1
        continue
    screen, vw = data.get("screen"), data.get("vw")
    layout, a11y = data.get("layout") or [], data.get("a11y") or []
    total += 1
    tag = f"{screen} @{vw}"
    if not layout and not a11y and not data.get("pageOverflowX"):
        print(f"  ok   {tag}")
        continue
    bad += 1
    print(f"  FAIL {tag}  layout={len(layout)} a11y={len(a11y)} "
          f"pageOverflowX={data.get('pageOverflowX')}")
    for it in layout[:8]:
        print("       layout:", json.dumps(it, ensure_ascii=False))
    for it in a11y[:8]:
        print("       a11y  :", json.dumps(it, ensure_ascii=False))

print(f"\n{total} 组（{len(viewports)} 档视口）· 有问题 {bad} 组")
sys.exit(1 if bad else 0)
PY

rm -rf "$TMP"
exit "$fails"
