---
req_id: REQ-20261128-02
stage: visual
status: active
owner: trae（breeze-tts 主工作区）
updated: 2026-11-28
---

# 视觉验收（V 环）

> 本需求可视改动：克隆页「音色档案」新增「合成引擎」下拉（选中态会切换提示文案）；编辑弹窗新增同名下拉；音色卡片类型后追加「· GPT-SoVITS」；诊断弹窗新增引擎开关区块。两个覆盖取值（qwen3-tts / gpt-sovits）在克隆页有可见差异，故按取值分别冻结参考。

## 1. 参考冻结（人确认）

| 页面 | 状态 | 视口 | 参考文件 | 来源 |
|---|---|---|---|---|
| qwen3-tts（克隆页，引擎选 qwen3-tts） | 默认 | 1440x900 | `visual/ref/ref-qwen3-tts-default-1440x900.png` | 本机 7861 实采（headless Chrome），已脱敏 |
| gpt-sovits（克隆页，引擎选 gpt-sovits） | 默认 | 1440x900 | `visual/ref/ref-gpt-sovits-default-1440x900.png` | 同上 |

- 冻结时间：2026-11-28 10:42
- 冻结确认：`V0: PASS | 确认人: 雪峰 | 时间: 2026-11-28 10:42 | 确认原文: "确认，通过，合并"`

## 2. 采集矩阵

> 应有张数 = 2 页面 × 1 状态 × 3 视口 = 6。页面名即覆盖取值，文件名因此天然包含取值。

| 页面 | 状态 | 1440x900 | 768x1024 | 375x812 |
|---|---|---|---|---|
| qwen3-tts | 默认 | `visual/shot/qwen3-tts-default-1440x900.png` | `visual/shot/qwen3-tts-default-768x1024.png` | `visual/shot/qwen3-tts-default-375x812.png` |
| gpt-sovits | 默认 | `visual/shot/gpt-sovits-default-1440x900.png` | `visual/shot/gpt-sovits-default-768x1024.png` | `visual/shot/gpt-sovits-default-375x812.png` |

采集命令（可重复执行；`?engine=` 深链让选中态可重放）：

```bash
chrome --headless=new --disable-gpu --hide-scrollbars --no-first-run \
  --user-data-dir=$TEMP/vprof-<engine>-<w> --window-size=<w>,<h> --virtual-time-budget=8000 \
  --screenshot=<shot 目录>/<engine>-default-<w>x<h>.png \
  "http://127.0.0.1:7861/index.html?screen=clone&engine=<engine>"
```

脱敏：左侧音色/角色列表、右侧历史含用户数据；落盘前马赛克处理，标题与表单结构保留。

## 3. 比对结果

| 维度 | 结果 | 参考图 | 实现图 | 说明 |
|---|---|---|---|---|
| 布局 | PASS | `visual/ref/ref-qwen3-tts-default-1440x900.png` | `visual/shot/qwen3-tts-default-1440x900.png` | 引擎下拉位于「类型」与「声音描述」之间，未挤压既有区块 |
| 间距 | PASS | 同上 | 同上 | 沿用 .field-label / .input 现有间距，无新增样式 |
| 颜色 | PASS | 同上 | 同上 | 复用现有下拉样式，未引入新色值 |
| 字号字重 | PASS | 同上 | 同上 | 与其它字段标签一致 |
| 组件状态 | PASS | 同上 | `visual/shot/gpt-sovits-default-1440x900.png` | 选 gpt-sovits 时下拉文案变为「GPT-SoVITS · 更接近本人，需先启动服务」并展开提示；设计型禁选该选项 |
| 响应式 | PASS | — | `qwen3-tts-default-768x1024.png`、`qwen3-tts-default-375x812.png`、`gpt-sovits-default-768x1024.png`、`gpt-sovits-default-375x812.png` | 三视口无横向滚动、无遮挡 |
| 控制台 | PASS | — | 采集时无失败请求 | headless 采集未报 JS 错误 |

## 4. 偏差说明（有 FAIL 才填）

| 偏差项 | 原因 | 是否接受 | 接受人 | 复审时间 |
|---|---|---|---|---|
| 无 | — | — | — | — |

## 5. V 门禁记录

```text
V: PASS | 参考: visual/ref/ref-qwen3-tts-default-1440x900.png + ref-gpt-sovits-default-1440x900.png | 视口: 1440/768/375 | 状态: 2/2 | 截图: visual/shot/ | 确认人: 雪峰 | 时间: 2026-11-28 10:42 | 确认原文: "确认，通过，合并"
```

- 连续 3 轮不收敛 → `BLOCKED`，回需求或设计。
