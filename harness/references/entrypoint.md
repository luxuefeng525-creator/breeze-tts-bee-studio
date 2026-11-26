<!-- ai-workflow:begin -->
## ai-workflow 入口（自动生成，勿手改本段）

- 任何非只读需求**自动进入**流程，不等用户输入触发词：执行 `node "{{ENGINE}}" init "<需求>" --dir <项目>`。
- G1 未通过（无确认原文）不写方案、不写代码；门禁只认退出码。
- 需求必须含 `path:line` 证据、≥2 类边界、六类澄清清单。
- **换会话/换工具续接**：先读 `harness/NEXT.md`（有则读 —— 当前现场、下一步、坑），再读对应需求包的 `harness/requirements/<REQ>/state.md`。禁止凭聊天记忆续接。
- **动手前必跑自检**：`node "{{SELFCHECK}}"`；退出码非 0 = 有阻断项，先修再动。
- 经验沉淀：`node "{{MEMORY}}" add|ready|promote|search|stats`（自动写入只进候选池，正式记忆需三条件 + 人确认）。
- **功能类需求**（有界面/流程/入口/交互）先查域规则 `node "{{ENGINE}}" domain <关键词>`，并在需求包里写满功能设计卡六问（用户旅程位置/行业基线/我们的现状与差距/决策点与后果/不做什么/交付形态）；域未收录先做同行对标再沉淀。
- **命中技能先读正文**：跑 `node "{{ENGINE}}" skill <名字>` 拿「读这个文件」的路径，读完 SKILL.md 再动手；未读不得输出该技能的内容，也不得凭记忆复述它。技能清单见 `harness/skills-index.md`。
- 规则全文：`{{SKILL}}`

## 多会话并发纪律

- 一个会话（agent + 工具）一个工作树一个分支：`node harness/scripts/git-atomic.mjs worktree <tool>` → 分支 `session/<tool>-<yyyymmdd>`、工作树 `../<repo>-<tool>`；同日撞名复用已存在的工作树。
- 只在自己的工作树里改；任务单目录与正式记忆（ADR-0002）不动。
- main 是唯一汇合点：从 main 切出，提交只碰本分支路径；结项后 `git-atomic.mjs merge <branch> <REQ>` 回 main，再删工作树与分支。
- 主工作区的既有改动不是本分支的：不 add、不 commit、不 reset。
<!-- ai-workflow:end -->
