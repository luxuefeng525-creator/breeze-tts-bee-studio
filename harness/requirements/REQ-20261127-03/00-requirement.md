---
req_id: REQ-20261127-03
stage: requirement
status: active
size: L1
owner: trae
updated: 2026-11-27
acceptance: [AC-01, AC-02, AC-03, AC-04]
---

# 需求

> **按档位填，不用全填**（L0 不建单，直接做）：
> - **L1**：填 §1 §2 §3 §4 §6 §7，其余写「不适用：L1 无界面/无多值」。
> - **L2**：全填。§9 只在有 ≥2 个取值时填；§10 只在有界面/流程/入口/交互时填。
> 填完跑 `node harness/engine/harness.mjs gate REQ-xxx --stage G1`，别自己判过没过。

## 1. 背景与目标

- 触发场景：用户在 Bee Studio「AI 剧本生成」里选了自定义 LLM 模型（或内置模型）生成/修改剧本，模型端点不可达、Key 无效或返回无法解析时，后端已按设计降级为 mock 并回传 `used_mock`/`mock_reason`/`used_custom_model`（REQ-20261127-02 打通）。
- 期望结果：界面出现**可见的降级/失败提示**，说清「这次不是真实 AI 产出，是演示数据」以及原因摘要；用户不再把 mock 当成真实结果，也知道下一步该修哪里。
- 验证通道：浏览器（真实面板 7861 操作 Bee Studio，写入临时自定义模型条目构造失败场景）。
- 不做项：不做一键重试；不做设置页的模型连通性测试入口；不展示原始异常全文/堆栈；不改后端响应契约（字段已存在，仅消费）。

## 2. 需求条目

### R-01

当 `/api/llm/script-gen` 返回 `used_mock=true` 且不是用户主动选择的 Mock 模式（`mock_reason != "force_mock"`）时，系统应在界面上给出 warn 级可见提示，说明本次为降级演示数据并附原因摘要；当 `used_custom_model=true` 时，提示需指出问题出在自定义模型的配置/端点，并给出下一步动作（检查 base_url 与 Key）。

- 验收点 AC-01：自定义模型 base_url 拒连时，生成结束后界面出现降级提示，文案含「演示」语义与原因片段，且不出现成功（success）提示。
- 验收点 AC-02：provider 无法解析（400）时，界面出现错误提示且包含后端返回的 error 文案。
- 验收点 AC-03：用户主动 Mock 模式与真实 LLM 成功两种情形下，均**不出现**降级提示（不得误报）。
- 边界：`used_mock` 字段缺失（老响应）时按成功处理还是提示？—— 缺失即视为非降级（沿用现有成功路径），因为字段是本次新契约，缺失不该让用户看到假警报。
- 边界：原因文本可能很长（含 URL/端口/异常链）—— 截断到可读长度（≤120 字），不全量展示。

### R-02

当 `/api/llm/script-edit` 返回失败（4xx/502）时，界面提示除包含后端 `error` 外，应在 `used_custom_model=true` 时说明本次修改走的是自定义模型、失败原因来自该端点，并附上 `raw_preview`（截断）以便定位；不得静默失败或无提示。

- 验收点 AC-04：让 script-edit 走一个不可达的自定义模型端点，失败提示包含原因与「自定义模型」上下文（脚本化请求 + 读取界面提示或 DOM 断言）。
- 边界：成功响应不带 `raw_preview`，此时不得出现任何降级提示（改镜头是真改好了）。
- 边界：`project_id`/`shot_index` 非法（400/404）属参数错误，提示后端文案即可，不需要自定义模型上下文。

## 3. 验收点汇总

| 编号 | 对应需求 | 判定方式 | 优先级 |
|---|---|---|---|
| AC-01 | R-01 | 浏览器：写入拒连的自定义模型条目 → script-gen → 界面出现降级提示且无 success 提示 | P0 |
| AC-02 | R-01 | 浏览器：provider 填未知 id → 界面提示含后端 error 文案 | P0 |
| AC-03 | R-01 | 浏览器：force_mock 与真实 LLM 各跑一次 → 均无降级提示 | P0 |
| AC-04 | R-02 | 脚本请求 script-edit 走不可达自定义端点 → 失败提示含原因与自定义模型上下文 | P1 |

## 4. 代码证据与边界反例

| 类型 | 证据 | 说明 |
|---|---|---|
| 现有实现定位 | `web/bee-studio.html:3281-3304` | `_llmGenerate`：只判 `r.error`（:3287），200 一律返回，无 `used_mock` 分支 |
| 现有实现定位 | `web/bee-studio.html:3306-3345` | 生成按钮处理：成功即 `showToast('✅ ...', 'success')`（:3324），降级 200 也走这里 |
| 调用方 / 数据来源 | `api/router.py:1623-1629`、`:1638-1643` | 两类降级：JSON 解析失败、LLM 调用失败，均回传 `used_mock=true` + `mock_reason` + `used_custom_model` |
| 调用方 / 数据来源 | `api/router.py:1540-1552` | `force_mock` 分支：`used_mock=true` 但 `mock_reason="force_mock"`（用户主动） |
| 调用方 / 数据来源 | `api/router.py:1746-1755` | script-edit 成功带 `used_custom_model`；失败 502 带 `error`/`used_custom_model`/`raw_preview` |
| 现有实现定位 | `web/bee-studio.html:3967-3985` | script-edit 请求与失败分支：只取 `data.error`（:3980），丢弃 `raw_preview`/`used_custom_model` |
| 可复用组件 | `web/bee-studio.html:1806-1814`、`:1156-1170` | `showToast(msg,type)` + `--success/--warn/--error` 样式已存在 |
| 边界 / 反例 1（空值） | `used_mock=true` 但 `mock_reason` 为空串或缺失 | 用兜底文案「AI 不可用，已用演示数据」，不允许出现空白提示 |
| 边界 / 反例 2（超长） | 原因文本超过 120 字（含完整 URL/异常链） | 截断展示，保留结尾关键信息，不换行撑破布局 |
| 边界 / 反例 3（主动 vs 失败） | `used_mock=true` + `mock_reason="force_mock"`（用户主动 Mock） | 预期**不**出现降级警告（否则噪音）；与「失败降级」区分 |
| 边界 / 反例 4（成功） | `used_mock=false`（真实 LLM 成功） | 保持现有 success 提示，不出现任何降级字样 |
| 边界 / 反例 5（字段缺失） | 响应不含 `used_mock`（老版本/其它路径返回） | 按非降级处理，不提示（避免假警报） |
| 边界 / 反例 6（参数错误） | script-edit 传非法 project_id/shot_index（400/404） | 只显示后端 error 文案，不拼接「自定义模型」上下文 |
| 边界 / 反例 7（超时） | script-gen 因上游 LLM 超时降级（`mock_reason` 含超时/重试耗尽，`api/router.py:1641`） | 提示原因并建议稍后重试或检查端点响应速度 |
| 边界 / 反例 8（无权限） | Key 无效或未配置（401/402）导致失败或降级 | 提示指向「去配置 Key」，不展示 Key 内容与长度 |
| 边界 / 反例 9（重复） | 短时间内连续多次生成都降级 | 每次都提示，不合并、不静默后续降级 |

## 5. 影响面

- 涉及模块：`web/bee-studio.html`（剧本生成/改镜头的结果处理与提示渲染）
- 涉及接口：`/api/llm/script-gen`、`/api/llm/script-edit`（只消费既有字段，不改后端）
- 数据变更：无

## 6. 人确认

```text
G1: 待确认 | 确认人: <TBD> | 时间: <TBD> | 确认原文: "<TBD>"
```

- 需求与方案各确认一次，方案追加 `G1-B:` 行。
- 确认原文写用户原话，不接受转述；字段不齐 → G1 FAIL。

## 7. 澄清清单

| 类别 | 结论 |
|---|---|
| 触发与时序 | 在 script-gen / script-edit 收到响应后、渲染提示时触发；提示出现在既有 toast 栈，不阻塞后续操作（用户仍可编辑镜头） |
| 数据来源与精度 | 完全使用后端既有响应字段（`used_mock`/`mock_reason`/`used_custom_model`/`raw_preview`）；原因文本截断 ≤120 字，不二次加工 |
| 隐私与授权 | 提示只展示后端已回传的失败摘要；不含 Key、不含完整请求体；不外发任何新数据 |
| 离线与失败 | 后端不可达时走前端已有 catch（网络错误提示）——本需求只处理「HTTP 200 降级」与「4xx/502 失败」两类 |
| 存储与留存 | 提示不落盘、不留历史；镜头数据持久化行为不变（降级仍写 SQLite，沿用现状） |
| 谁可见 | 仅当前操作 Bee Studio 的用户可见；无他人可见性变化 |

## 8. 需求分析与同行对标

| 需求 | 对标对象 | 来源 | 他们的做法 | 我们的取舍 | 理由 |
|---|---|---|---|---|---|
| R-01 | NN/g 错误信息指南 | https://www.nngroup.com/articles/error-message-guidelines/#toc-visibility-guidelines-1 | 错误要「高可见 + 建设性」，说明发生了什么并给可执行的下一步 | 采纳 | 降级提示必须可见且给动作（检查 base_url/Key），不能只报技术码 |
| R-01 | OpenAI API 错误码文档 | https://platform.openai.com/docs/guides/error-codes/api-errors | 每个错误码给 Cause + Solution（如 429→控制频率、401→检查 Key） | 改造 | 我们的原因是自由文本，需按「配置类/网络类」给人话归纳 |
| R-01 | 本仓库出片失败提示 | `web/bee-studio.html:2354` | 「视频不会用 Mock 冒充成功」明确告知并给替代方案 | 采纳 | 同一产品内已有「不冒充成功」的口径，剧本侧应对齐 |
| R-02 | NN/g 错误信息指南 | https://www.nngroup.com/articles/error-message-guidelines/#toc-communication-guidelines-2 | 避免只给一行代码/术语，要解释并给恢复路径 | 采纳 | edit 失败需带上「走的哪个端点 + 返回片段」 |
| R-02 | 本仓库合成失败提示 | `web/bee-studio.html:2278` | 合成被拒时展示具体 msg | 采纳 | 沿用「显示后端 msg」的既有做法 |

### 8.1 维度检查

| 维度 | 结论 | 来源 | 不适用理由 |
|---|---|---|---|
| 同类同行 | 通用产品把失败原因可见并可行动；API 层结构化回传 Cause/Solution | https://www.nngroup.com/articles/error-message-guidelines/ ；https://platform.openai.com/docs/guides/error-codes/api-errors | — |
| 功能清单与边界 | 仅覆盖 gen/edit 两处 LLM 结果的可见性；不覆盖视频/合成/配音链路 | `harness/domains/ai-degradation-notice.md` | 其它链路已有提示，不在本需求范围 |
| UI 设计 | 复用既有 toast（底部右下 + warn 样式）；不新增组件、不改布局 | `web/bee-studio.html:1156-1170`、`:1806-1814` | — |
| 数据库 | 不适用：无数据库 | — | 无持久化改动 |
| 技术选型 | 沿用现有原生 HTML/JS 单文件 + 既有 toast | `web/bee-studio.html:1806` | 沿用，不引入新库 |

## 9. 覆盖矩阵（有 ≥2 个取值时填）

覆盖维度：不适用：非多值需求（仅 Web 端 Bee Studio 一处界面，无多端/多角色取值）

## 10. 功能设计卡（设计前六问；有界面/流程/入口/交互时填）

### 卡 1：LLM 降级与失败提示

- **用户旅程位置**：入口在 Bee Studio 左侧「AI 剧本」弹窗的「✨ 生成剧本 & 分镜」按钮（`web/bee-studio.html:3306`）与「AI 重写这个镜头」模态的提交（`:3955`）。默认态：无提示；加载态：按钮 disabled + 「🎬 AI 正在构思...」；失败态：本次要新增的 warn 提示；成功态：现有 success 提示；空态：未填主题时已有拦截（`:3315`）。上一态 = 发起请求前，下一态 = 镜头列表渲染/项目切换。
- **行业基线**：失败原因要高可见、可行动（说明问题 + 给下一步）；API 侧以 Cause/Solution 结构回传。证据：https://www.nngroup.com/articles/error-message-guidelines/#toc-visibility-guidelines-1 、https://platform.openai.com/docs/guides/error-codes/api-errors ；本域沉淀见 `harness/domains/ai-degradation-notice.md`
- **我们的现状与差距**：后端已回传 `used_mock`/`mock_reason`/`used_custom_model`（`api/router.py:1626`、`:1641`），前端 `_llmGenerate` 只判 `r.error`（`web/bee-studio.html:3287`），降级被当成成功弹 success（`:3324`）；script-edit 失败只显示 `data.error`（`:3980`）。用户感知：**会** 把演示数据当成真实 AI 产出，且无从知道该修 base_url 还是 Key。
- **决策点与后果**：提示放哪 → 取「复用 `showToast` warn」；代价：toast 3.2s 自动消失，长原因可能被错过（取舍：原因截断到 120 字 + 文案自含关键信息，不做常驻横幅）。是否区分主动 Mock → 取「`mock_reason=='force_mock'` 不告警」；风险：用户主动 Mock 时无任何提示可能误以为真 AI（缓解：Mock 模式的按钮文案/既有提示保持不变，本期不做常驻标识）。是否加一键重试 → 不做；成本：需处理幂等与配额，超出本期范围。
- **不做什么**：不做重试按钮；不做设置页模型连通性测试入口；不展示原始异常全文与堆栈；不改后端响应字段；不动视频/合成/配音链路已有提示。
- **交付形态**：界面（Bee Studio 单文件 `web/bee-studio.html` 内的提示逻辑与文案；随附真实截图作为视觉验收证据）

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-11-27 | 初稿 | 由 REQ-20261127-02 结项时的审视意见第 1 条派生 |

## 12. 功能开发拆解（有开发任务时填）

| 切片 | 目录结构 | 文件归属 | 路由 / 入口 | 函数 / 模块 | 数据表 / 字段 | 验收方式 | 清理项 |
|---|---|---|---|---|---|---|---|
| 切片 A | `web/` | `web/bee-studio.html` | 「✨ 生成剧本 & 分镜」点击处理 `:3306` | `_llmGenerate` 返回值扩展 + 调用处提示分支 | 不适用：无持久化 | 浏览器复现 AC-01/02/03 | 临时写入的 model_registry 探针条目 |
| 切片 B | `web/` | `web/bee-studio.html` | 「AI 重写这个镜头」提交 `:3955` | `submitScriptEdit` 失败分支 | 不适用：无持久化 | 脚本请求复现 AC-04 | 无 |
