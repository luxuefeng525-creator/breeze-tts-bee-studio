# 手册（按需查，不进常驻）

常驻的 `AGENTS.md` 只放判据；这里放细节。用到哪节读哪节，不要整份背。

## 1. 术语白话对照

文档、脚本、门禁里的词，和它们的白话说法：

| 文档里的词 | 白话 |
|---|---|
| 任务单 / REQ-xxx | 这次活的档案，一个目录装需求、方案、测试、审查 |
| G1 | 开工前你点头（需求 + 方案各一次） |
| G2 | 你说「做完了，可以了」 |
| G3 | 独立审查通过 |
| G4 | 发布材料齐了 |
| PASS / FAIL / BLOCKED | 过了 / 没过 / 卡住了（缺什么写清楚） |
| 自检 | 动手前的红灯检查 |
| probe | 看本机工具能不能用 |
| begin | 一条命令给出开工要的全部判断 |
| brief | 带出最多 3 条相关经验 |
| observe | 把看到的记下来，带证据 |
| 域文件 / 行业基线 | 这个领域的同行都怎么做 |
| 设计前六问 | 做界面前先答的六个问题，见 `templates/requirement.md` §10 |
| 交接条 / HANDOFF | 引擎自动刷新的接手快照（不是第一手现场） |

说话时用右边；写代码和跑命令时用左边（标识符不改，改了脚本就找不到）。

## 2. 规则全文在哪

| 要什么 | 读哪 |
|---|---|
| 必须做的（19 条） | `harness/rules/must.md` |
| 建议做的（12 条） | `harness/rules/should.md` |
| 可选做法 | `harness/rules/can.md` |
| 每道门禁查什么、怎么算过 | `harness/references/gates.md` |
| 任务单每一节怎么填 | `harness/references/requirement-package.md` |
| 记忆怎么写、怎么晋升 | `harness/references/memory.md` |
| 长任务、断点续跑、交接 | `harness/references/observability.md` |
| 外部通道（浏览器 / 小程序 / 桌面） | `harness/references/channels.md` |
| 域基线怎么写 | `harness/references/domain-rules.md` |
| 失败怎么分类、什么时候停 | `harness/references/failure-classes.md` |

## 3. 四道门禁，一句话版

- **G1 开工确认**：需求写清了、有代码定位、有边界反例，你说「按这个做」。
- **G2 做完了**：每个验收点都有命令输出或截图，不是「我觉得好了」。
- **G3 审查过**：另开一个只读上下文看 diff，只看需求和改动，不看开发自述。
- **G4 可发布**：版本、说明、回退方案齐了，你说「发」。

小改动可以少走门禁（L0 直接做），但「不可逆先问」这条永不豁免。

### 三档（L0 / L1 / L2）

- **L0 单点**：一个文件、一处逻辑。直接改，说清改了什么。
- **L1 局部**：多文件但行为局部。先说方案等一句确认，改完跑受影响测试。
- **L2 大改**：跨模块、动数据、要上线。方案 + 独立审查 + 全量测试 + 截图证据。

### 常用命令

| 做什么 | 命令 |
|---|---|
| 开工检查（默认唯一入口） | `node harness/engine/harness.mjs begin "<需求>"` |
| 定技能（仅 begin 未命中或需要 JSON） | `node harness/scripts/route.mjs "<需求>"` |
| 取经验（仅 begin 无结果或需要更多） | `node harness/scripts/memory.mjs brief "<需求>"` |
| 查技能索引（仅维护或路由排障） | `harness/references/dispatch-table.md` / `harness/skills-index.md` |
| 验收 | `node harness/engine/harness.mjs gate <REQ> --stage G2` |
| 记一条经验 | `node harness/scripts/memory.mjs add "<事实>" --source "<证据>"` |

## 4. 常见卡点

| 卡在哪 | 怎么办 |
|---|---|
| 换了工具/会话，不知道做到哪 | 跑 `node harness/engine/harness.mjs begin "<你要做的事>"`，它直出当前活跃任务单与下一步；再读 `harness/NEXT-BRIEF.md` 与对应 `state.md`。完整 `NEXT.md` 只在需要历史/任务明细时读，`HANDOFF.md` 只是自动刷新的快照 |
| 不知道算 L0 还是 L1 | 问自己：改动会不会改变程序跑起来的样子。不会→L0；会但只影响局部→L1 |
| 自检红灯 | 先修红灯，别带着问题开工；修完重跑，看输出不要听我说 |
| 工具装不上 | 记 BLOCKED 并写清缺什么；不许用旧截图或「看着没问题」顶替 |
| 同一处改了两三次还失败 | 停下，写假设清单，换验证路径，别盲改 |
| 经验要不要记 | 会重复用到的记；一次性的不记 |
| 记了但没被用上 | 开工跑 `begin`，它会把相关经验带出来 |

## 5. 提交前自动自检（hook）

装一次，之后每次 `git commit` 自动跑：

```
node harness/engine/harness.mjs install-hook
```

- 只跑自检（约 3 秒），不通过就**阻断提交**，不会悄悄放过去
- 超时 / 自检脚本本身出错 → **放行并留痕**。宁可漏检也不把人卡住 —— 卡住人 = 逼人用 `--no-verify`
- 确需绕过：`git commit --no-verify`。但 `--no-verify` 不跳过 post-commit，所以**会被记下来**，下次 `begin` 会提醒你「上次绕过了自检」
- 撤掉：`rm .git/hooks/pre-commit .git/hooks/post-commit`

原理：`pre-commit` 通过时把暂存区的 tree hash 写进 `.git/harness-last-check`；`post-commit` 拿 `HEAD^{tree}` 跟它比，不一致就是绕过了。

## 6. 经验怎么晋升（双通道）

候选池里的经验要够格才进正式记忆。两条路，**任一条满足即可**：

| 通道 | 条件 | 适合谁 |
|---|---|---|
| 常规 | 3 次复现 ∧ 2 个作用域 ∧ 满 30 天 | 慢慢攒的通用经验 |
| 快速 | 5 次复现 ∧ 3 个作用域 ∧ 满 7 天 | 高频、跨项目的经验 —— 不用干等一个月 |

```
node harness/scripts/memory.mjs ready      # 看 READY / NEAR / EARLY
node harness/scripts/memory.mjs promote C012 --confirm
```

`READY` 可以晋升；`NEAR` 只差一条，会写清「还差 2 次复现」这种；`EARLY` 还差两条以上。**看不到 NEAR 的差值就别瞎猜差多少。**

## 7. 重复任务会被认出来

开工跑 `begin`，它会在有历史时多打一段：

- `REUSE 可直接复用` —— 描述、涉及路径、文件内容**全同**。先看上次产物，别从零重做
- `REFERENCE 仅参考` —— 描述像但输入有差，会列出差异。**别直接搬**，输入变了结论未必成立
- 什么都不打 —— 没有可比历史。宁可不提，也不给错的复用建议（假命中比漏命中危害大）

做完一件事想让它下次能被认出来：

```
node harness/scripts/task-signature.mjs record --desc "<一句话说清做了什么>" --artifact "<产物路径>"
```

注意：`harness/memory/`、`harness/requirements/`、`harness/references/`、`harness/rules/`、`harness/design/`、`harness/domains/`、`.workbuddy/` 这些**过程产物不计入签名** —— 它们会被任务本身改动，算进去就永远命不中。

两条容易踩的：

- **`record` 与 `check` 都不带 `--paths` 时，各自推断当时的路径**。所以「做完记一条」和「下次开工查」会自动对齐。但你显式给了 `--paths`，**两次都要给**，否则一边是手填一边是推断，永远对不上。
- **推不出路径时不给复用建议**（干净工作区、或不在 git 仓库里）。这是故意的：没有输入可比，说「输入完全一致」就是假承诺。

## 8. 表结构怎么定（data-modeling）

要定表结构、加字段、改关系、判断某份数据存哪 → 走 `data-modeling` 技能：

```
node harness/engine/harness.mjs skill data-modeling
```

它管「改之前」：该不该建表、实体与关系、字段类型与索引、**四类破坏性操作**（持锁加列 / 删列删表 / 建索引没加并发 / 不兼容重命名）。
真去改库、要回滚方案 → 走 `migration`（它管「改的时候」）。两者分工别混。

## 9. 改工作流自己的代码：只改包内

```
build/v4-staging/          ← 源。改这里
harness/（工程内）          ← 产物。bootstrap 每次会覆盖
~/.<宿主>/skills/ai-workflow/  ← 产物。同上
```

**只改工程内那份，跑一次 bootstrap 就被冲掉了** —— 而且不会报错，改动静默消失。
判断依据：`harness/references/`、`harness/scripts/`、`harness/templates/`、`harness/engine/` 都是产物。

改完的标准动作：
```
node build/v4-staging/scripts/bootstrap.mjs --apply --project <你的工程>
```
自检会比对「包内 vs 工程内」是否一致，不一致就阻断 —— 但**它只能发现分叉，发现不了「你改了产物被覆盖后重新一致」**。所以记住源头在哪。

### 9.1 收口步骤（结项 / 发布必做，缺一步不算收口）

按顺序跑，最后一条退出码必须是 0：

1. `node scripts/build-skills-index.mjs` —— 重建技能索引（独立 `--check` 会阻断陈旧索引；selfcheck 只提示）
2. `node scripts/audit-skills.mjs` —— 技能审计，失败项必须 0
3. `node scripts/release.mjs --pack` —— 打包；只在提交后跑，pack 以 git 跟踪文件为准
4. `node build/v4-staging/scripts/bootstrap.mjs --apply --install-missing --project <工程根>` —— **同步宿主**：把包同步到工程 `harness/` 与 6 个宿主。跳过这步，源里的修复只留在源里，工程与宿主仍跑旧副本（之前就是这么漂的：宿主副本落后 4 个文件）
5. `node harness/scripts/selfcheck.mjs` —— 回归，阻断必须 0；「工程内 harness 依赖与包一致」「宿主包版本一致」两组专门盯第 4 步做没做

## 10. 路由给不出结论时，别当成「没技能」

`route.mjs` 判意图分三层，越往下越粗：

| 层 | 靠什么 | 什么时候生效 |
|---|---|---|
| 一 | 精确词表（每个技能自己的触发词） | 用户用了领域词 |
| 二 | 构式规则（「加个…效果」「悬停+动作」这类句式） | 用户说得出场景但用的不是词表里的词 |
| 三 | **领域兜底**（界面/数据/迁移/故障/功能五个大类） | 前两层都没命中 |

第三层还会先过一道**技术语境否决**：句子里有 `代码 / 函数 / 变量名 / SQL / 接口 / 数据库 / 日志` 这类词时，「丑」「设计」「排版」指的是代码质量，不是界面 —— 这时候不进界面域。

输出三种，意思不一样：

- `1. ui-design (score=12)` —— 精确命中，可以照做
- `1. ui-design (领域兜底)` —— **只是按大类粗分**，去读一遍需求，不对就换
- `ROUTE_NONE（没把握，不硬猜）` —— **不等于没有技能可用**。读用户这句话自己判该加载哪个，别因为这里没给就跳过判断

写词表时的纪律：**只收人话，不收自己的术语**。`聚光 / 液态玻璃 / 骨架屏` 是技能内部的配方名，新手不会这么说，放进词表就是给自己看；它们该由第二层的句式规则兜住（「加个…效果」），或者由第三层兜住（「卡片/列表/加载」）。

词表必然会漏 —— 补的办法是**加规则和兜底**，不是继续往词表里堆词。

## 11. UI 交付门禁（做界面的人必跑）

界面「好不好看」分两半：一半能机器判，一半只能人看。这条门禁判能判的那半。

```
node harness/scripts/ui-gate.mjs --css <样式或目录> [--tokens 约束卡.json] [--shots 截图目录]
```

| 判什么 | 判据 | 不合格 |
|---|---|---|
| 对比度 | 正文 ≥4.5:1，大字（≥24px 或粗体 ≥18.7px）≥3:1 | 致命 |
| 字阶 | 相邻字号比 1.1~1.7，种类 ≤8，整数 px | 提示 |
| 间距节奏 | margin/padding/gap 落在 0/2/4/8/12/16/20/24/32… | 提示 |
| token 一致 | 给了约束卡就用卡里的色，不许硬编码 | 致命 |
| 截图证据 | desktop / tablet / mobile 三张，24 小时内 | 致命 |

给了 `--tokens` 与 `--shots` 才判那两项；没给会标 SKIP 并在交付说明里写明。

**它不判什么**：布局好不好看、层级清不清楚、留白舒不舒服、动效手感 —— 这些看真实截图。所以门禁过了**不等于**界面好看，只等于「没有低级错误」。

顺序：先按门禁把静态做对，再按 `skills/ui-design/references/motion-recipes.md` 挑动效。静态没做对就上动效，等于给错的东西加糖。

## 术语与加载口径（2026-11-22）

> 唯一口径在 `docs/05-Harness标准-v1.0-20261119.md` 的「术语口径」节；本节只引用。

- `e`：EARS 需求语法，需求沿用「当…系统应…以便…」句式。
- `L2`：改动档位（大改），不是成熟度分级。
- 「5人」：是「由人」的转写误差；角色固定为「人拍板、Agent 执行」。
- 安装器：本轮不做图形化，CLI 为唯一交付形态。
- 校验和：SHA-256，清单沿用 manifest 现有字段。
- harness 加载：不进每轮六步，只在多步需求时加载。
