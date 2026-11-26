# 工程常驻规则

> 由 `harness init` 生成，勿手改。改 `templates/AGENTS.md` 后跑 `node harness/engine/harness.mjs refresh`。
> 此处只放「不写就会做错」的判据；完整规则和术语白话对照见 `harness/references/manual.md`。

## 原则

先看懂再动手，改完拿证据，拿不准就问。经验写下来，下次直接用。

## 换会话或换工具怎么接上

先跑 `node harness/engine/harness.mjs begin "<需求>"`（直出活跃单与下一步），再读 `harness/NEXT-BRIEF.md` 与 `state.md`；完整 `NEXT.md` 只在需要历史/任务明细时读；`HANDOFF.md` 是自动快照。别凭聊天记忆续接。

## 什么时候建任务单

- 改运行行为（逻辑 / 接口 / 数据 / 界面 / 配置）→ 建任务单。
- 纯问答、只读排查、改错别字 → 直接做，不建单。
- 判据只有一条：这次改动会不会改变程序跑起来的样子。

## 开工第一件事（硬规则）

不跑就动手算违规：

```
node harness/engine/harness.mjs begin "<一句话需求>"
```

一次给出：档位、红灯、可用工具、**相关经验**。经验只从这里出来 —— 不跑等于每次失忆。有经验就说清怎么用，红灯没清不动手。常用命令表见 `harness/references/manual.md`「常用命令」。

## 档位与红线

- 档位分 L0 / L1 / L2，定义与各自要求见 `harness/references/manual.md`「三档」。
- 删数据、强推分支、改生产配置、涉及钱：先说清后果，等明确同意再动。沉默不算同意。
- 装过 hook 的话 `git commit` 会先跑自检，不通过即阻断；绕过会被记下来。细则见 `harness/references/manual.md`「提交前自动自检（hook）」。

## 技能与记忆：先取再动手

- **技能与经验**：默认只运行 `begin`；它已经带出当前任务、相关经验和技能建议。只有 `begin` 未命中或信息不足时，才按需运行 `route.mjs` 或 `memory.mjs brief`。`skills-index.md`、`catalog.json` 和完整 `long-term.md` 默认不读；需要人工查表时优先 `harness/references/dispatch-table.md`，`catalog.json` 只供路由脚本或排障使用。拿到技能名后先读 SKILL.md 正文，未读不得输出该技能的内容。
- **界面活**必读 `ui-design`，动效按 `references/motion-recipes.md` 选型表选。

## 多会话并发纪律

- 一会话一工作树一分支：`worktree <tool>` → `session/<tool>-<yyyymmdd>`；结项后 `merge <branch> <REQ>` 回 main。见 `harness/references/manual.md`。
- 跨会话回执：`node harness/engine/harness.mjs msg --to <REQ|all> --text "…"`；`begin` 直出未读回执。
