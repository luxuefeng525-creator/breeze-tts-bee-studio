---
req_id: REQ-20261127-02
stage: requirement
status: active
size: L1
owner: trae
updated: 2026-11-27
acceptance: [AC-01, AC-02, AC-03]
---

# 需求

> **L1**：填 §1 §2 §3 §4 §6 §7，其余写「不适用：L1 无界面/无多值」。

## 1. 背景与目标

- 触发场景：用户在 Bee Studio 新建项目弹窗里选「+ 添加自定义」填了自己的模型（如 GLM / DeepSeek），前端把「AI 剧本模型」下拉切到该自定义 id，随后点生成剧本。
- 期望结果：后端按该条目的 `model_name` 与 `base_url` 真实发起 OpenAI 兼容调用；未配 Key 时显式 402，不再静默降级成 mock 剧本。
- 验证通道：命令退出码（pytest 本地假端点实测）+ 面板接口 curl。
- 不做项：不做前端 mock 降级提示 UI（`used_mock`/`mock_reason` 目前前端完全没接，属全局体验缺口，不止自定义模型，另开需求）；不改 4 家视频服务商的 provider 抽象。

## 2. 需求条目

### R-01

当 生成剧本（或改镜头）指定了某个自定义 LLM 模型时，系统应 用该条目的 `model_name` 作模型名、`base_url` 作端点发起 OpenAI 兼容调用，以便 用户填的自定义端点真正生效。

- 验收点 AC-01：自定义条目带 `base_url` 时，出站请求打到该 base_url（`/chat/completions` 拼接在其后），`model` 字段等于注册时的 `model_name`；由本地假端点实测取证。
- 边界：`base_url` 为空 → 自定义模型无内置端点，必须明确报错而不是静默改用 LongCat；`base_url` 非法（不是 http/https）→ 明确 400，且不触发调用；`base_url` 自带 `/v1` 后缀 → 只拼一次 `/chat/completions`，不重复路径。

### R-02

当前端把 provider 传成自定义模型 id（如 `custom-1760...`）时，系统应 从 `data/model_registry.json` 解析出对应条目再调用，以便 前端现有「添加后自动选中」的交互不必改造即可用。

- 验收点 AC-02：`POST /api/llm/script-gen` 传 `provider=<自定义id>` 且该 id 未在 `/api/providers/{provider}/keys` 配 Key 时返回 402 + `need_key`；配上 Key 后返回 `used_mock=false`、`generated_by` 为该 id，剧本来自假端点返回内容。
- 边界：Key 缺失/占位符（`test_d`、`your_` 等）→ 402；模型 id 已被删除或拼错 → 明确报「未知模型」而非回落 LongCat；自定义 id 与内置 provider 同名 → 内置优先，不误伤老链路。

### R-03

当自定义模型调用失败（网络超时、401、非 JSON）时，系统应 在响应 JSON 里带上失败原因（沿用既有 `mock_reason`）并标记 `custom_model_error=true`，以便 前端后续接入提示时有现成字段，不再「看起来成功」。

- 验收点 AC-03：假端点返回 401 时，`POST /api/llm/script-gen` 仍走既有 mock 兜底（不 502），响应含 `mock_reason` 且 `custom_model_error=true`。
- 边界：超时（stub 延迟 > timeout）与连接拒绝（端口未监听）两种失败都要落到同一兜底；Key 为空时不发请求直接 402。

## 3. 验收点汇总

| 编号 | 对应需求 | 判定方式 | 优先级 |
|---|---|---|---|
| AC-01 | R-01 | `pytest tests/test_custom_llm_model.py -q`（本地假端点抓取站请求体） | P0 |
| AC-02 | R-02 | 同上用例 + `curl POST /api/llm/script-gen` 看 402/200 差异 | P0 |
| AC-03 | R-03 | 同上用例（假端点 401 / 拒连） | P1 |

## 4. 代码证据与边界反例

| 类型 | 证据 | 说明 |
|---|---|---|
| 现有实现定位 | `api/router.py:1064-1076` | 自定义模型条目写入 `base_url` 后存入 `data/model_registry.json` |
| 现有实现定位 | `api/router.py:615-630` | `_load_custom_models`/`_save_custom_models` 读写注册表 |
| 消费方缺口 | `api/llm_client.py:22-42` | `LLM_PROVIDERS` 只有 longcat/openai，端点写死 |
| 消费方缺口 | `api/llm_client.py:50-52` | 未知 provider 直接抛 `ValueError`，被上游 catch 成 mock |
| 调用现场 | `api/router.py:1451-1455`、`api/router.py:1585-1592` | script-gen 与 script-edit 都只把 `provider` 透传给 `call_chat`，无 base_url 通道 |
| 数据来源 | `web/bee-studio.html:4144-4151`、`web/bee-studio.html:4157` | 前端提交 base_url，并在添加后把 `#scriptLLM` 设为该自定义 id |
| 边界 / 反例 1 | Key 为空或占位符：不发请求，显式 402 `need_key` | 不允许静默 mock 冒充成功 |
| 边界 / 反例 2 | 超时 / 拒连 / 401：统一走 mock 兜底并带原因字段 | 不允许 502 裸崩或「看起来成功」 |
| 边界 / 反例 3 | `base_url` 空或非法 URL：明确报错，不回落内置端点 | 避免假配置悄悄指向 LongCat |
| 边界 / 反例 4 | 重复 / 同名：自定义 id 与内置 provider 同名时内置优先 | 不误伤老链路 |

## 5. 影响面

- 涉及模块：`api/llm_client.py`（`call_chat` 增可选 base_url + model 覆写）、`api/router.py`（新增自定义模型解析 helper，接入 script-gen / script-edit 两处）、`tests/test_custom_llm_model.py`（新增）
- 涉及接口：`POST /api/llm/script-gen`、`POST /api/llm/script-edit`
- 数据变更：无新增表；只读 `data/model_registry.json`（已有文件，不迁移、可逆）

## 6. 人确认

```text
G1: 待确认 | 确认人: TBD | 时间: TBD | 确认原文: "TBD"
```

- 需求与方案各确认一次，方案追加 `G1-B:` 行。
- 确认原文写用户原话，不接受转述；字段不齐 → G1 FAIL。

## 7. 澄清清单

| 类别 | 结论 |
|---|---|
| 触发与时序 | 用户在新建项目弹窗选定/添加自定义 LLM 模型后点「生成剧本」；改镜头走 `script-edit` 复用同一解析 |
| 数据来源与精度 | 模型条目取自 `data/model_registry.json`（前端 POST `/api/models` 写入）；`base_url` 原样使用，不猜测补全路径 |
| 隐私与授权 | 自定义模型的 API Key 存本机 `data/api_keys.json`（沿用 A2 加密），不外发第三方；调用目标由用户自己填，责任自担 |
| 离线与失败 | 端点不可达 / 超时 / 401 → 既有 mock 兜底 + 原因字段；断网时脚本台老链路不受影响 |
| 存储与留存 | 只读注册表，不新增持久化；Key 沿用现有落盘口径，删 key 即失效 |
| 谁可见 | 仅本机使用者可见；无多租户 |

## 8. 需求分析与同行对标

| 需求 | 对标对象 | 来源 | 他们的做法 | 我们的取舍 | 理由 |
|---|---|---|---|---|---|
| R-01 | OpenAI Chat Completions 接口（base_url + model 由调用方给定） | https://platform.openai.com/docs/api-reference/chat/create | `base_url` 默认官方，允许自建网关覆盖；model 必传 | 采纳：base_url 可覆盖、model 取注册值 | 国产模型（GLM/DeepSeek）均为该格式，前端 placeholder 就是智谱地址 |
| R-01 | 本项目自定义模型契约 | `../docs/product/PRD-v4-intelligent-studio.md` §19.4 | 已登记自定义模型表单含 Base URL 字段 | 采纳：让登记字段真正生效 | 契约写了字段但代码没接 |
| R-02 | 本项目 provider/key 契约 | `docs/tech/api-contract.md` | `/api/providers/{provider}/keys` 允许任意 provider 名存 Key | 采纳：key 以自定义 id 为键 | 无需改 Key 接口即可支持 |
| R-03 | 本项目降级契约 | `../docs/product/VIDEO-BENCHMARK-2026-11-26.md` §1.2 | 无 Key 时 mock 降级、不 502 | 改造：降级必须带原因且不被误认为成功 | 现前端完全没接 `used_mock`，降级对用户不可见 |

### 8.1 维度检查

| 维度 | 结论 | 来源 | 不适用理由 |
|---|---|---|---|
| 同类同行 | OpenAI 兼容网关普遍支持自定义 base_url | https://platform.openai.com/docs/api-reference/chat/create | 无同类时不适用——此处有 |
| 功能需求 | 自定义模型可作 LLM 后端 + base_url 生效 + 失败可见 | `../docs/product/PRD-v4-intelligent-studio.md` §19.6 C4 同源缺口 | 无未纳入项 |
| UI 设计 | 前端表单已存在，本次不改布局/文案/动效 | `web/bee-studio.html:4144-4151` | 不改界面，故不涉及新的 UI 设计 |
| 数据库 | 无新表；只读 `model_registry.json` 与 `api_keys.json` | `api/router.py:594` | 无迁移 |
| 技术选型 | 沿用 Python + requests + 既有 panel.py 路由 | `api/llm_client.py:16` | 沿用现有栈，零新依赖 |

## 9. 覆盖矩阵（有 ≥2 个取值时填）

不适用：非多值需求。改动是同一条 LLM 调用链上的两处分支（script-gen / script-edit 共用一个 helper），不设端/角色/语言维度；对内置 provider（longcat/openai）与自定义 provider 两种取值的覆盖由 §2 的 R-01/R-02 验收点与 §4 的反例承担。

## 10. 功能设计卡（设计前六问；有界面/流程/入口/交互时填）

### 卡 1：自定义 LLM 模型打通

- **用户旅程位置**：入口在新建项目弹窗的「🧠 AI 剧本模型」下拉与「+ 添加自定义」表单；默认态 = 选内置 LongCat 且显示连通性徽章；加载态 = 点生成后等待 LLM（约 60s）；失败态 = 端点不可达/Key 无效 → 明确提示；成功态 = 剧本按自定义模型返回内容渲染。上一态 = 未添加自定义模型（回退内置）；下一态 = 继续选音色/生成。
- **行业基线**：同类开源/国产模型面板都把 Base URL 做成一等输入，允许指向自建网关与 OpenAI 兼容端点；证据 https://platform.openai.com/docs/api-reference/chat/create 与 `web/bee-studio.html:1603` 的 placeholder（智谱 open.bigmodel.cn）。
- **我们的现状与差距**：现在 `api/router.py:1068` 把 base_url 存进注册表就再没有读取方，`api/llm_client.py:56` 端点写死；缺的是「自定义模型 → provider 解析 → base_url 出站」整条通路；用户感知 = 以为在用自己配的模型，实际要么 402 要么静默出 mock 剧本（痛点：生成结果不可信且无法自查）。
- **决策点与后果**：是让前端改成传 `model_id`、还是后端按 `custom-` 前缀解析？取后者（后端解析）→ 代价是后端多一个注册表查询 helper 与两条分支判断；收益是前端 4157 行现有交互零改动、老链路零回归风险。风险：自定义 id 与内置 provider 撞名，用「内置优先」规避。
- **不做什么**：不做前端 mock 降级提示 UI（`used_mock`/`mock_reason` 前端当前完全未接），因为那是全局缺口、影响所有 LLM 生成，纳入本需求会让范围翻倍；不纳入 4 家视频服务商的多主体参考参数改造。
- **交付形态**：无视觉（前端表单不动），交付物为 pytest 用例证据 + curl 实测记录 + 需求包门禁结论。

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-11-27 | 初稿 | 修复 `/api/models` 的 `base_url` 死字段时发现的链路缺口 |

## 12. 功能开发拆解（有开发任务时填）

| 切片 | 目录结构 | 文件归属 | 路由 / 入口 | 函数 / 模块 | 数据表 / 字段 | 验收方式 | 清理项 |
|---|---|---|---|---|---|---|---|
| 切片 A | `api/` | `api/llm_client.py` | 被 router 调用 | `call_chat(base_url=..., model=...)` | 不适用：无持久化 | `pytest tests/test_custom_llm_model.py -q` | 无 |
| 切片 B | `api/` | `api/router.py` | `POST /api/llm/script-gen`、`POST /api/llm/script-edit` | 新增 `_resolve_llm(provider)` helper + 两处调用点 | 只读 `data/model_registry.json`、`data/api_keys.json` | 同上 + curl 402/200 对照 | 无 |
| 切片 C | `tests/` | `tests/test_custom_llm_model.py` | 测试入口 | 本地假端点 + FakeH | 临时 sqlite（tmp_path），零残留 | `pytest -q` 全绿 | tmp_path 自动清理 |
