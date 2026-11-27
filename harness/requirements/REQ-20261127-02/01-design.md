---
req_id: REQ-20261127-02
stage: design
status: active
owner: trae
updated: 2026-11-27
---

# 方案设计

## 1. 现状

- 相关入口：`api/router.py:1349`（script-gen）、`api/router.py:1546`（script-edit）；LLM 出口 `api/llm_client.py:45 call_chat`
- 现有数据流：前端 `POST /api/models` 写入 `data/model_registry.json`（含 `base_url`）→ 无任何读取方；`provider` 只用于 `_get_key(provider)` 与 `call_chat(provider=...)`，后者只认 longcat/openai
- 现有同类实现：`api/providers/comfyui.py:147-150` 已有「base_url 可注入、env 兜底」的成熟写法，本次照搬到 LLM client

## 2. 方案

| 方案 | 做法 | 优点 | 代价 | 结论 |
|---|---|---|---|---|
| A | 后端按 provider 解析：内置直接用；`custom-*` 或注册表内 id → 取 `model_name`+`base_url`，`call_chat` 增可选 `base_url`/`model` | 前端零改动（`bee-studio.html:4157` 已把自定义 id 塞进 `#scriptLLM`）；老链路零回归 | 后端多一个 helper + 两处判断 | 选用 |
| B | 前端改成传 `model_id`，后端按 model_id 查表 | 语义更显式 | 前端要改提交逻辑与状态管理，且旧版本前端不兼容 | 否决：代价大于收益 |
| C | 删掉 base_url 输入与存储 | 改动最小 | 放弃用户自建端点能力，与产品方向相反 | 否决 |

选定：A，理由：修的是「字段存了没人读」的断链，最小闭环是后端把已有字段接通；前端交互已经按这个前提写好了。

## 3. 模块与改动点

| 文件/模块 | 改动 | 原因 |
|---|---|---|
| `api/llm_client.py` | `call_chat` 增可选形参 `base_url: str = ""`、`model: str = ""`；`base_url` 为空时保持现状 | R-01 |
| `api/router.py` | 新增 `_resolve_llm(provider) -> (model, base_url)`：内置返回 `("", "")`；其余查 `_load_custom_models()`，找不到抛错；校验 `base_url` 必须 http(s) | R-01/R-02 |
| `api/router.py` | script-gen 与 script-edit 两处改为先 resolve 再 `call_chat(model=..., base_url=...)`；失败时在 mock 响应里加 `custom_model_error=True` | R-02/R-03 |
| `tests/test_custom_llm_model.py` | 新增：本地假 OpenAI 端点抓请求体、401 兜底、402 缺 Key | AC-01/02/03 |

## 4. 接口与数据结构

```text
输入：POST /api/llm/script-gen { topic, shots_count, audience, style_id?, provider, api_key?, project_id? }
     provider ∈ {longcat, openai} ∪ {自定义模型 id}
输出：200 { project_id, title, shots[], generated_by, used_mock, style_id }
     402 { error, need_key: true }  —— 自定义模型未配 Key
     400 { error }                  —— base_url 非法
     200 + used_mock=true + custom_model_error=true  —— 调用失败的兜底
存储：只读 data/model_registry.json、data/api_keys.json；不新增表
```

## 5. 权限与边界

- 谁能触发：本机面板使用者（沿用现有面板鉴权口径，无新增角色）
- 越权处理：不新增权限概念；Key 仍走 `_PLACEHOLDER_MARKERS` 占位符拦截（`api/router.py:555`）
- 只读边界：本次不写数据库；只读注册表与 Key 文件

## 6. 影响面

- 调用方：`web/bee-studio.html` 的剧本生成与改镜头两条链路；`POST /api/providers/{provider}/test` 也调 `call_chat`（本次同步支持，避免测试按钮对自定义模型报错）
- 共享状态：`_API_KEYS` 内存字典 + `data/api_keys.json`；不改结构
- 兼容性：内置 provider 路径完全不变（resolve 返回空值 → 走原逻辑）；老数据（无 base_url 的自定义条目）在选中时会明确报「缺 base_url」，不再静默走 mock

## 7. 回退思路

- 回退触发条件：改动后内置 LongCat 生成出现 FAIL，或全量 pytest 变红
- 回退步骤：`git revert` 本次提交即可——无迁移、无新表、无数据写入；自定义模型条目本身保留不删

## 8. 未决问题

| 问题 | 影响 | 待定方 |
|---|---|---|
| 前端何时接 `used_mock`/`mock_reason` 提示 | 全局降级可见性，非本需求范围 | 用户（另开需求） |
| 自定义模型是否需要在 UI 上显示连通性徽章 | 体验增强 | 用户（本需求不做） |
