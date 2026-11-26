---
req_id: REQ-YYYYMMDD-01
stage: visual
status: active
updated: YYYY-MM-DD
---

# 视觉验收（V 环）

> 仅 UI 需求填写。参考未冻结不得进入开发；截图与比对缺失 V 门禁判 FAIL。

## 1. 参考冻结（人确认）

| 页面 | 状态 | 视口 | 参考文件 | sha1 | 来源 |
|---|---|---|---|---|---|
| <页面> | 默认 | 1440x900 | `visual/ref/ref-home-default-1440.png` | <TBD> | <原型路径 / 设计稿 / URL> |

- 冻结时间：<TBD>
- 冻结确认：`V0: PASS | 确认人: <姓名> | 时间: <TBD> | 确认原文: "<TBD>"`

## 2. 采集矩阵

> 应有张数 = `config.pages` 的 states 总数 × `config.viewports` 总数；文件名 `<page>-<state>-<w>x<h>.png`，缺一张或命名不符即 FAIL。

| 页面 | 状态 | 1440x900 | 768x1024 | 375x812 |
|---|---|---|---|---|
| <页面> | 默认 | <TBD> | <TBD> | <TBD> |
| <页面> | 空态 | <TBD> | <TBD> | <TBD> |
| <页面> | 错误 | <TBD> | <TBD> | <TBD> |

采集命令（可重复执行）：

```bash
<TBD>
```

## 3. 比对结果

| 维度 | 结果 | 参考图 | 实现图 | 说明 |
|---|---|---|---|---|
| 布局 | PASS/FAIL/BLOCKED | <路径> | <路径> | <TBD> |
| 间距 | PASS/FAIL/BLOCKED | <路径> | <路径> | <TBD> |
| 颜色 | PASS/FAIL/BLOCKED | <路径> | <路径> | <TBD> |
| 字号字重 | PASS/FAIL/BLOCKED | <路径> | <路径> | <TBD> |
| 组件状态 | PASS/FAIL/BLOCKED | <路径> | <路径> | <TBD> |
| 响应式 | PASS/FAIL/BLOCKED | <路径> | <路径> | <TBD> |
| 控制台 | PASS/FAIL/BLOCKED | — | <日志路径> | <TBD> |

## 4. 偏差说明（有 FAIL 才填）

| 偏差项 | 原因 | 是否接受 | 接受人 | 复审时间 |
|---|---|---|---|---|
| <TBD> | <TBD> | <TBD> | <TBD> | <TBD> |

## 5. V 门禁记录

```text
V: 待确认 | 参考: <TBD> | 视口: <TBD> | 状态: <TBD> | 截图: <TBD> | 确认人: <TBD> | 时间: <TBD> | 确认原文: "<TBD>"
```

- 连续 3 轮不收敛 → `BLOCKED`，回需求或设计。
