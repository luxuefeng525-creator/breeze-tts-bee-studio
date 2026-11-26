---
name: harness
description: >
  Harness 型交付工作流：人定目标与关键决策，agent 按阶段连续执行，最后由人验收。
  用于把需求做成带证据的交付：需求包（需求/设计/测试/审查/发布 + state）、must/should/can
  三层规则、context router 按阶段只加载所需上下文、开发/测试/审查三角色隔离、两层记忆、
  阶段门禁三态、本地日志对账、视觉验收环（冻结参考 + 可重放截图 + 比对门禁）。
  Triggers on harness、需求包、工作流、阶段门禁、agent 门禁、
  换人续接、需求遗漏、审查、验收、/harness、对标原型、截图比对、视觉验收、
  同行对标、需求分析、清理沙箱、clean。
version: 0.14.0
user-invocable: true
argument-hint: "[begin · init · gate · visual · status · handoff · clean] [需求 ID 或一句话需求]"
license: MIT
lint-terms-exempt: 需求包
---

# harness — 人拍板 · Agent 连续干 · 门禁验收

> 人定目标、在关键节点拍板；agent 在明确边界里连续执行；最后拿证据验收。

> **版本对应**：本技能 **0.14.0** ↔ 引擎 **v0.9**（`engine/harness.mjs`，跑 `harness.mjs --help` 核对）。

## 什么时候用

- 跨多步的需求（新功能、改造、修复链）；换人接手还要能续；交付要带证据、可审查、可回退。
- 不用：单行改动、纯问答、只读排查。

## 三步走

| 步 | 命令 | 说明 |
|---|---|---|
| 1 能不能开工 | `harness.mjs begin "<需求>"` | 一次给出档位、红灯、可用工具、相关经验 |
| 2 建任务单 | `harness.mjs init "<需求>"` | 生成 `requirements/REQ-xxx/`，填需求与方案，等人确认 |
| 3 验收 | `harness.mjs gate <REQ> --stage G2` | 每个验收点都要有真证据，人点头才 PASS |

其余按需：`probe`（查工具）、`handoff`（交接）、`visual`（UI 截图环）、`clean`（回收沙箱）、`inventory`（扫原型）、`status`、`layout`。

引擎退出码：`0` 通过 / `2` 不通过 / `1` 用法错 / `3` 未实现。模型只转述，不自判。

## 三档

| 档 | 典型 | 必须做 |
|---|---|---|
| L0 | 一个文件一处逻辑 | 改完说清改了什么，跑类型/语法检查 |
| L1 | 多文件、行为局部 | 先说方案等一句确认；受影响测试；独立审查看 diff |
| L2 | 跨模块 / 动数据 / 上线 | 全量测试 + 构建；两阶段审查；截图证据；回退方案 |

## 四道门禁（细则见 `references/gates.md`）

| 门 | 问什么 | 谁决定 |
|---|---|---|
| G1 | 需求清不清、有没有代码定位和边界反例 | 人点头（需求 + 方案各一次） |
| G2 | 每个验收点有没有真证据 | 人点头 |
| G3 | 换个只读上下文看 diff，过不过 | 人点头 |
| G4 | 版本、说明、回退齐不齐 | 人点头 |

`state.md` 里每道门一行死字段（确认人 / 时间 / 确认原文），缺一项即 FAIL，agent 不得自签。

## 12 条硬门禁（一句话版，细则在 `references/gates.md` §10）

1. 单入口：没 `init` 就改业务代码 = 违规。
2. 门禁记录是死字段：缺字段、写 `<TBD>`、agent 自判都算 FAIL。
3. 需求要证据锚：`path:line` + ≥2 类边界反例 + 澄清六类。
4. 续接顺序：`begin` → `NEXT-BRIEF.md` → `state.md`；完整 `NEXT.md` 只在需要历史/任务明细时读；`HANDOFF.md` 是自动刷新的快照，不凭聊天记忆。
5. UI 必须有冻结参考（路径 + sha1）+ 可重放截图，缺一张就 FAIL。
6. 每条需求先对标同类与同行，来源要能追溯。
7. 验证沙箱建在系统临时目录，当轮回收。
8. 覆盖取值声明多少做多少，缺值不得用总进度掩盖。
9. 证据链：`PASS` 带路径且文件非空；`FAIL`/`BLOCKED` 同行写原因。
10. 通道不可用就 `BLOCKED`，不许假装跑过、不许用旧图。
11. 同一检查项连续 3 次失败 → 停手换路径，不许原样重跑。
12. 删数据、强推、发布等不可逆动作前必须确认。

## 任务单长什么样

```text
harness/requirements/REQ-xxx/
├─ 00-requirement.md  需求 + 验收点 + 对标
├─ 01-design.md       方案、取舍、影响面、回退
├─ 02-test.md         验收点 ↔ 用例 ↔ 结果
├─ 03-review.md       审查问题与处理
├─ 04-release.md      发布、回退、监控
├─ 05-visual.md       UI 视觉环（非 UI 写「不适用」）
├─ visual/            ref/ 冻结参考，shot/ 截图
└─ state.md           进度、阻塞、门禁记录、下一步
```

模板在 `templates/`，初始化时整包复制。

## 常用参考

| 要什么 | 读哪 |
|---|---|
| **不知道按什么顺序走** | `references/mainline.md`（九步主线：定位→对标→需求→拆分→实现→自检→验收→交付→沉淀） |
| 规则三层（must / should / can） | `references/rules.md` |
| 门禁与硬门禁细则 | `references/gates.md` |
| 需求拆解与对标五步法 | `references/benchmark.md` |
| 通道命令与证据清单 | `references/channels.md` |
| 失败分类与何时停 | `references/failure-classes.md` |
| 开发 / 测试 / 审查三份契约 | `references/agents.md` |
| 记忆与 state 字段 | `references/memory.md` |
| 视觉验收环 | `references/visual-loop.md` |
| 术语白话对照 | `references/manual.md` |
