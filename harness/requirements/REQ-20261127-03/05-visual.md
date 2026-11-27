---
req_id: REQ-20261127-03
stage: visual
status: active
owner: trae
updated: 2026-11-27
---

# 视觉验收（V 环）

> 仅 UI 需求填写。参考未冻结不得进入开发；截图与比对缺失 V 门禁判 FAIL。

## 1. 参考冻结（人确认）

| 页面 | 状态 | 视口 | 参考文件 | sha1 | 来源 |
|---|---|---|---|---|---|
| bee-studio | 默认 | 1440x900 | `visual/ref/ref-before-default-1440x900.png` | F89351CC49C46D5D4590677F95823B898A21740B | 本需求改动前的主面板页面（7861 旧版前端） |

- 冻结时间：2026-11-27 18:26
- 冻结确认：`V0: PASS | 确认人: 用户 | 时间: 2026-11-27 18:31 | 确认原文: "V 通过（推荐）"`

## 2. 采集矩阵

| 页面 | 状态 | 1440x900 | 768x1024 | 375x812 |
|---|---|---|---|---|
| bee-studio | 默认 | `bee-studio-default-1440x900.png` | `bee-studio-default-768x1024.png` | `bee-studio-default-375x812.png` |

采集命令（可重复执行；本机 headless Chrome，无需额外依赖）：

```bash
CHROME="/c/Program Files/Google/Chrome/Application/chrome.exe"
SHOT="harness/requirements/REQ-20261127-03/visual/shot"
for VP in "1440,900" "768,1024" "375,812"; do
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --virtual-time-budget=6000 \
    --window-size="$VP" --screenshot="$SHOT/bee-studio-default-${VP/,/x}.png" \
    "http://127.0.0.1:7862/bee-studio.html"
done
```

> 前置：工作树 `web/` 需由 7862 静态代理提供（`/api/*` 反代主面板 7861），否则截到的是主工作区旧版页面。

## 3. 比对结果

| 维度 | 结果 | 参考图 | 实现图 | 说明 |
|---|---|---|---|---|
| 布局 | PASS | `visual/ref/ref-before-default-1440x900.png` | `visual/shot/bee-studio-default-1440x900.png` | 逐区块目视一致，本需求不动画布布局 |
| 间距 | PASS | 同上 | 同上 | 无新增元素，间距未变 |
| 颜色 | PASS | 同上 | 同上 | warn 沿用既有 `--warning` 左侧色条 |
| 字号字重 | PASS | 同上 | 同上 | 无字号改动 |
| 组件状态 | PASS | — | `logs/g3-fix-browser-verify.txt` | toast 为 3.2s 瞬态，静态截图采不到，改用 MutationObserver DOM 快照取证 |
| 响应式 | PASS | — | `visual/shot/bee-studio-default-768x1024.png`、`...375x812.png` | 与参考同为既有响应式表现，本需求未触及 |
| 控制台 | PASS | — | 浏览器轮次 1–4 未见新增报错 | — |

## 4. 偏差说明（有 FAIL 才填）

| 偏差项 | 原因 | 是否接受 | 接受人 | 复审时间 |
|---|---|---|---|---|
| 降级/错误状态无静态截图 | toast 仅存活 3.2s，headless CLI 无法交互触发 | 接受，以 DOM 快照替代 | 待定 | V 确认时 |
| `harness.mjs visual` 采集命令不可用 | 全局 `harness/harness.config.json` 不存在（工程既有缺口，非本需求引入） | 接受，改用 §2 手工命令 | 待定 | 另开工程设施需求 |

## 5. V 门禁记录

```text
V: 待确认 | 参考: 1 张已冻结 | 视口: 1440x900 / 768x1024 / 375x812 | 状态: 默认（降级态以 DOM 快照为准） | 截图: 3 张 | 确认人: — | 时间: — | 确认原文: "<TBD>"
```
