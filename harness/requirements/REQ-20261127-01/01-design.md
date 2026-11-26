---
req_id: REQ-20261127-01
stage: design
status: active
owner: trae
updated: 2026-11-27
---

# 方案设计

## 1. 现状

- 相关入口：`web/bee-studio.html:1536-1541`（风格 select）、`web/bee-studio.html:3148`/`:3197`（styleHints）、`api/router.py:1407-1424`（LLM prompt）、`api/router.py:1353-1364`（`_normalize_shot`）。
- 现有数据流：前端把 style 文本作为 user_prompt 的一行传给 `/api/llm/script-gen`；风格没有独立数据结构，生成后也不落库。
- 现有同类实现：`api/cost_calc.py` 的定价表 + `_load_custom_models()` 的「文件优先、内置兜底」模式，可复用到模板加载。

## 2. 方案

| 方案 | 做法 | 优点 | 代价 | 结论 |
|---|---|---|---|---|
| A | 新增 `data/style_templates.json` + 后端接口，前端动态渲染，模板注入 LLM | 前后端同源、可维护、可测 | 多一个接口与回退逻辑 | 选用 |
| B | 继续硬编码，仅扩充到 6 项 | 改动最小 | 每次加风格都要改两处前端 + 一处后端 | 否决 |

选定：A，理由：风格需同时驱动 UI、LLM prompt 与建议镜头数，硬编码必然漂移。

## 3. 模块与改动点

| 文件/模块 | 改动 | 原因 |
|---|---|---|
| `api/style_templates.py` | 新增：加载/校验/默认模板/`suggest_shot_count()` | R-01/R-03 |
| `data/style_templates.json` | 新增 6 项模板数据 | R-01 |
| `api/router.py` | 新增 `GET /api/bee/style-templates`；script-gen 注入风格并回传 `style_id`；mock 同步 | R-02 |
| `web/bee-studio.html` | 弹窗打开时渲染风格选项与说明；时长/风格变化刷新建议镜头数 | R-01/R-03 |
| `tests/test_style_templates.py` | 新增单测 | AC-01/02/03 |

## 4. 接口与数据结构

```text
输入：GET /api/bee/style-templates（无参）
输出：{"templates":[{id,name,description,prompt_hint,shot_seconds}]}，失败回退内置且仍 200
存储：data/style_templates.json（可读可替换）；生成结果写入既有 scripts.shots_json（新增 style_id 键，可逆）
```

## 5. 权限与边界

- 谁能触发：本机 Bee Studio 用户；无登录、无外发。
- 越权处理：不适用（无写接口；模板文件仅服务端读取）。
- 只读边界：模板接口只读文件，不连数据库、不调远端。

## 6. 影响面

- 调用方：`web/bee-studio.html` 的新建项目弹窗、`POST /api/llm/script-gen`。
- 共享状态：`data/style_templates.json`；无内存缓存，避免面板重启前后不一致。
- 兼容性：旧调用不带 style_id 时按默认「电影感」注入；历史 shots 无 style_id 时详情读回不报错。

## 7. 回退思路

- 回退触发条件：模板加载异常或 AC 失败。
- 回退步骤：删除 `api/style_templates.py` 与 `data/style_templates.json`，还原 `api/router.py`/`web/bee-studio.html` 到提交前；无数据库迁移，无需清数据。

## 8. 未决问题

| 问题 | 影响 | 待定方 |
|---|---|---|
| 真实 LLM 当前 402（LongCat 未充值） | AC-02 只能以注入函数单测 + force_mock 结构断言取证 | 用户已明确不充值，接受该局限 |
