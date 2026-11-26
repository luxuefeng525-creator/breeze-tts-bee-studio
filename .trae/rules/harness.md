<!-- harness:begin -->
## harness 入口（自动生成，勿手改）

- 规则只读一份：`AGENTS.md`；细则查 `harness/references/manual.md`。
- 开工先跑 `node harness/engine/harness.mjs begin "<需求>"`：直出当前活跃任务单与下一步，红灯未清不动手。
- 接手别人的活：按 begin → `harness/NEXT-BRIEF.md` → `harness/requirements/<REQ>/state.md` 的顺序读；完整 `NEXT.md` 只在需要历史/任务明细时读；`HANDOFF.md` 是自动刷新的快照。别凭聊天记忆续接。

## 多会话并发纪律

- 一个会话（agent + 工具）一个工作树一个分支：`node harness/scripts/git-atomic.mjs worktree <tool>` → 分支 `session/<tool>-<yyyymmdd>`、工作树 `../<repo>-<tool>`；同日撞名复用已存在的工作树。
- 只在自己的工作树里改；任务单目录与正式记忆（ADR-0002）不动。
- main 是唯一汇合点：从 main 切出，提交只碰本分支路径；结项后 `git-atomic.mjs merge <branch> <REQ>` 回 main，再删工作树与分支。
- 主工作区的既有改动不是本分支的：不 add、不 commit、不 reset。
<!-- harness:end -->
