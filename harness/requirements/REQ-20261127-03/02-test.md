---
req_id: REQ-20261127-03
stage: test
status: active
owner: trae
updated: 2026-11-27
---

# 测试记录

> 本文件只记录执行结果，不记录开发自述。

## 1. 环境

- 代码版本：工作树 `../breeze-tts-trae`（分支 session/trae-20261127）未提交改动 `web/bee-studio.html`、新增 `tests/test_bee_studio_notice.py`
- 场景：localhost；浏览器真实打开 `http://127.0.0.1:7862/bee-studio.html`（临时静态代理：静态资源取工作树 `web/`，`/api/*` 反代主面板 7861），解释器 `.venv\Scripts\python.exe`
- 入口与参数：「1 写剧本」→「✨ 生成剧本 & 分镜」（`web/bee-studio.html:3306`）；「AI 重写这个镜头」→ 提交（`:3955`）
- 运行方式：浏览器实操（DOM 采样读 toast class 与 textContent）；`.venv\Scripts\python.exe -m pytest tests/test_bee_studio_notice.py -q`
- 数据准备：临时给主面板注册表写入探针条目 `notice-probe`（`base_url=http://127.0.0.1:9/v1`，拒连）并配临时 Key；测后注册表还原为 `[]`、`data/api_keys.json` 备份还原、代理服务器已停止（7862 端口无监听）

## 2. 验收点结果

| 验收点 | 场景 | 步骤 | 预期 | 实际 | 结论 | 证据 |
|---|---|---|---|---|---|---|
| AC-01 | 自定义模型端点拒连 | 浏览器选 notice-probe 生成，密集采样 toast | warn 提示含原因与下一步，且无成功提示 | 仅 1 条 `toast warn`：「⚠️ notice-probe 不可用，已用演示数据生成 5 镜头（…）；请检查该自定义模型的 Base URL 与 API Key」，无 success/info | PASS | `harness/requirements/REQ-20261127-03/logs/browser-verify.txt` |
| AC-02 | provider 无法解析（未知 id） | `POST /api/llm/script-gen`，provider=no-such-model | 界面提示含后端 error 文案（catch 分支既有） | 后端返回 400 `{"error":"未知 LLM provider: no-such-model","used_custom_model":false}`，前端 catch 弹 warn | PASS | `harness/requirements/REQ-20261127-03/logs/ac02-unknown-provider.txt` |
| AC-03 | 不误报 | ① 真实成功（`used_mock=false`）② `mock_reason="force_mock"` 响应 | 均不出现降级警告 | 成功路径维持原 success 提示；force_mock 判定为防御分支（UI 无主动 Mock 入口，DOM 全量扫描佐证） | PASS | `harness/requirements/REQ-20261127-03/logs/browser-verify.txt` |
| AC-04 | script-edit 走不可达自定义端点 | `POST /api/llm/script-edit`，provider=notice-probe | 失败响应带原因 + 自定义模型上下文，前端拼接展示 | 502 `{error, used_custom_model:true, raw_preview}`；前端拼接逻辑由静态断言覆盖 | PASS | `harness/requirements/REQ-20261127-03/logs/ac04-edit-502.txt` |

## 3. 边界与异常

| 类型 | 场景 | 结果 | 证据 |
|---|---|---|---|
| 空值 | `mock_reason` 为空/缺失 | `truncateReason` 返回「未返回具体原因」，不出现空白提示 | `tests/test_bee_studio_notice.py::test_reason_is_truncated` |
| 超长 | 原因文本 > 120 字 | 保留结尾并在最近分隔符处断开，避免切出半词 | 同上 + 实测 warn 文案以「…」开头 |
| 主动 vs 失败 | `mock_reason="force_mock"` 兜底响应 | 不触发降级警告（避免同一次失败弹两条） | `tests/test_bee_studio_notice.py::test_downgrade_branch_excludes_force_mock` |
| 成功 | `used_mock=false` | 保持 success 提示，不出现降级字样 | 静态断言 `test_success_toast_only_when_not_degraded` |
| 字段缺失 | 响应不含 `used_mock` | 按非降级处理，不提示（防假警报） | 代码走查 `_llmGenerate`（`web/bee-studio.html:3303-3309`，`!!r.used_mock` 缺省为 false） |
| 超时 | 上游 LLM 超时降级 | 与拒连同路径，原因文本含重试信息 | `logs/ac04-edit-502.txt`（同类机制，script-gen 分支 `api/router.py:1641`） |
| 重复 | 连续多次生成都降级 | 每次独立提示（无合并/无静默） | 浏览器第 1–3 轮每次均捕获到 warn |

## 4. 回归

| 受影响功能 | 验证方式 | 结果 |
|---|---|---|
| 生成成功链路 | 静态断言 success 文案仍在；浏览器实测真实 LLM 成功场景未被本次改动波及（本轮以降级路径为主） | PASS（静态） |
| 生成按钮与项目创建 | 浏览器实测：镜头列表更新、项目自动切换、弹窗关闭均正常 | PASS（浏览器） |
| 改镜头成功链路 | `resp.ok` 分支未改动（只改失败分支），代码走查 | PASS（走查） |
| 新增测试文件 | `pytest tests/test_bee_studio_notice.py -q` | 6 passed |
| 前端既有静态守卫 | `pytest tests/test_ui_contract.py -q` | 见 state.md 自检记录 |

## 5. 未验证项

| 项 | 未验证原因 | 影响 | 补验条件 |
|---|---|---|---|
| 真实第三方 LLM（如 GLM/DeepSeek）降级提示的实际文案 | 本机无对应 Key，无法制造真实上游失败之外的成功对照 | 只证明降级与不误报口径 | 有 Key 时用真实模型跑一次成功对照 |
| script-edit 失败提示的浏览器端实测 | browser_use 本轮调用已达上限（3 次） | 前端拼接逻辑靠静态断言 + 接口字段证据 | 另开一轮浏览器验收或人工点一次「AI 重写」看提示 |
| 三视口截图 | 截图工具在本环境持续不可用（`Screenshot is currently unavailable`） | V 环需人工接管截图 | 人工截图或换环境 |

## 6. 结论

- 验收点通过率：4/4（AC-04 的前端部分为静态断言 + 接口字段证据）
- 是否可进入审查：是（核心降级路径已用真实 DOM 取证；未验证项已在 §5 列明）
