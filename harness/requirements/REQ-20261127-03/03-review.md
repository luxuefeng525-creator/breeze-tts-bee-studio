---
req_id: REQ-20261127-03
stage: review
status: active
owner: trae
updated: 2026-11-27
---

# 审查记录

> 只依据实际改动与需求条目，不参考开发过程自述。

## 1. 审查范围

- 改动文件：`web/bee-studio.html`（apiBT / `_llmGenerate` / 生成按钮处理 / `submitScriptEdit` / `truncateReason` / `.toast` 样式）、新增 `tests/test_bee_studio_notice.py`
- 需求条目：R-01（script-gen 降级可见）、R-02（script-edit 失败上下文）
- 未覆盖需求：无
- 审查方式：独立只读子代理两轮（初检 + 复审），逐条给 path:line 证据

## 2. 问题清单

| 编号 | 严重度 | 性质 | 位置 | 问题 | 证据 | 改法 | 已补检查 | 状态 |
|---|---|---|---|---|---|---|---|---|
| RV-01 | 致命 | 需人判断 | [bee-studio.html](file:///f:/TTS2/breeze-tts-trae/web/bee-studio.html#L2230-L2253) apiBT、[L3378](file:///f:/TTS2/breeze-tts-trae/web/bee-studio.html#L3378-L3392) catch 分支 | 非 2xx 时 `throw new Error(status)` 丢弃响应体，后端「未知 LLM provider」等人话错误不可见；异常路径未置降级标志，之后仍弹绿色「已创建」 | 浏览器轮次 1/2 只显示「…e」；轮次 4 修复后显示「未知 LLM provider: no-such-model」 | apiBT 非 ok 时读 body 挂 `e.payload`；catch 开头置 `degraded=true`，并优先用 `err.payload.error` 拼文案 | — | 已修 |
| RV-02 | 建议 | 需人判断 | [bee-studio.html](file:///f:/TTS2/breeze-tts-trae/web/bee-studio.html#L1810-L1826) truncateReason、[L1164](file:///f:/TTS2/breeze-tts-trae/web/bee-studio.html#L1164-L1169) `.toast` | 旧截断在分隔符贴着末尾时只剩一两个字符（实测「…e」）；放宽到 400 字后 toast 无宽度上限会横向溢出视口 | 浏览器轮次 1 文案以「…e」结尾 | 按码点切 + 移除 `/` 分隔符 + TAIL_MIN 保护；调用处上限 400；`.toast` 加 `max-width: min(560px, 92vw)` | — | 已修 |
| RV-03 | 建议 | 需人判断 | [router.py](file:///f:/TTS2/breeze-tts-trae/api/router.py#L1641-L1641) | 后端 `str(exc)[:200]` 从头部硬切，异常尾部（`establish a new connection: [Errno …]`）永久丢失 | 浏览器轮次 3 文案止于「Failed to e」 | 本需求不动后端（面板加载主工作区代码，改了无法验证）；另开需求统一该文件多处 `[:200]` 模式 | — | 接受 |

严重度口径：致命 = 功能错误/越权/数据损坏/密钥泄露；建议 = 可维护性、命名、风格。

## 3. 检查项

| 维度 | 结论 | 说明 |
|---|---|---|
| 需求一致 | PASS | R-01/R-02 均有浏览器 DOM 证据（AC-01/02/04）；AC-03 判定口径见 00-requirement.md |
| 权限 | PASS | 无新增权限概念，沿用本机面板口径 |
| 异常处理 | PASS | 拒连、超时、未知 provider、JSON 解析失败四类均有对应提示路径 |
| 数据与事务 | PASS | 本需求只读展示既有字段，不写库 |
| 影响面 | PASS | `apiBT` 为 32 处调用共用，仅新增 `payload` 挂载，`message` 不变，老调用方不受影响；`_mockGenerate` 内部兜底语义复核无误 |
| 调试残留 | PASS | 探针注册表/Key/代理端口均已还原，临时脚本删除 |
| 安全 | PASS | 不展示原始堆栈全文；Key 走既有占位符与落盘逻辑 |

## 4. 遗留与接受

| 遗留项 | 接受人 | 理由 | 复审时间 |
|---|---|---|---|
| 后端 `mock_reason` 尾部截断（RV-03） | 用户 | 属 `api/router.py` 既有实现，本需求范围是前端展示；已在 02-test.md §5 记录 | 另开需求时 |

## 5. 结论

可发布（G3 层面）。两项致命问题已修并复验；一项后端截断遗留已记录待另开需求。
