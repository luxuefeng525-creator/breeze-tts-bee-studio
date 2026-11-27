---
req_id: REQ-20261127-04
stage: visual
status: active
owner: agent（breeze-tts 主工作区）
updated: 2026-11-28
---

# 视觉验收（V 环）

> 本需求可视改动只有两处：`#refFile` 的 accept 收紧为 `.wav,audio/wav`、克隆页提示文案改为「仅 WAV…」。无版式重做，故参考取改动后的现状截图（回归比对用）。

## 1. 参考冻结（人确认）

| 页面 | 状态 | 视口 | 参考文件 | sha1 | 来源 |
|---|---|---|---|---|---|
| home | 默认 | 1440x900 | `visual/ref/ref-home-default-1440x900.png` | 28fb0027e650f2c0ef3c4009e6613ec9b24acfb8 | 本机 7861 实采（headless Chrome），已脱敏 |

- 冻结时间：2026-11-28 06:05
- 冻结确认：`V0: 待确认 | 确认人: — | 时间: — | 确认原文: "<TBD>"`

## 2. 采集矩阵

> 应有张数 = 1 页面 × 1 状态 × 3 视口 = 3。文件名 `<page>-<state>-<w>x<h>.png`。

| 页面 | 状态 | 1440x900 | 768x1024 | 375x812 |
|---|---|---|---|---|
| home | 默认 | `visual/shot/home-default-1440x900.png` | `visual/shot/home-default-768x1024.png` | `visual/shot/home-default-375x812.png` |

采集命令（可重复执行，直接截本机 7861）：

```bash
chrome --headless=new --disable-gpu --hide-scrollbars --no-first-run \
  --user-data-dir=$TEMP/vprof-<w> --window-size=<w>,<h> --virtual-time-budget=8000 \
  --screenshot=<shot 目录>/home-default-<w>x<h>.png http://127.0.0.1:7861/index.html
```

脱敏：左栏（搜索/筛选/音色列表/角色库）与右栏历史列表含用户数据，落盘前做马赛克处理；版式结构保留可比。

## 3. 比对结果

| 维度 | 结果 | 参考图 | 实现图 | 说明 |
|---|---|---|---|---|
| 布局 | PASS | `visual/ref/ref-home-default-1440x900.png` | `visual/shot/home-default-1440x900.png` | 三栏顺序与栅格一致，无溢出 |
| 间距 | PASS | 同上 | 同上 | 未改样式，间距无变化 |
| 颜色 | PASS | 同上 | 同上 | 未改配色 |
| 字号字重 | PASS | 同上 | 同上 | 未改排版 |
| 组件状态 | PASS | — | 克隆页实采 | `#refFile` accept=`.wav,audio/wav`；提示文案「仅 WAV · 建议 10–15 秒 · 单人说话（其它格式请先转成 WAV）」已生效 |
| 响应式 | PASS | — | `visual/shot/home-default-768x1024.png`、`home-default-375x812.png` | 三视口均无横向滚动、无遮挡 |
| 控制台 | PASS | — | 采集时无失败请求 | headless 采集未报 JS 错误 |

## 4. 偏差说明（有 FAIL 才填）

| 偏差项 | 原因 | 是否接受 | 接受人 | 复审时间 |
|---|---|---|---|---|
| 无 | — | — | — | — |

## 5. V 门禁记录

```text
V: 待确认 | 参考: visual/ref/ref-home-default-1440x900.png (sha1 28fb0027…) | 视口: 1440/768/375 | 状态: 1/1 | 截图: visual/shot/ | 确认人: — | 时间: — | 确认原文: "<TBD>"
```

- 连续 3 轮不收敛 → `BLOCKED`，回需求或设计。
