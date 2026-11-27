# 域：AI 降级与失败提示（ai-degradation-notice）

> 建立：2026-11-27
> 状态：初稿，行业基线已取证（2 个外部来源 + 本仓库实践）；「已定取舍」待人确认后生效

## 行业基线（同行都有什么）

| 能力 | 行业做法 | 证据锚 |
|---|---|---|
| 失败原因可见、可行动 | 的错误信息要求「高可见性 + 建设性沟通」，即说明发生了什么、并给出用户能执行的下一步（换 Key、重试、降级说明），而不是只给技术码 | https://www.nngroup.com/articles/error-message-guidelines/#toc-visibility-guidelines-1 （Visibility Guidelines / Communication Guidelines 章节） |
| API 层把失败结构化回传，由 UI 呈现「原因 + 解决方案」 | OpenAI API 每个错误码都带 Cause 与 Solution 两栏（如 401 → "Ensure the correct API key..."、429 → "Pace your requests"、503 → "retry... check the status page"） | https://platform.openai.com/docs/guides/error-codes/api-errors （Code Overview 表） |
| 降级与失败区分对待 | 429/503 属「稍后重试」类，UI 提示重试；鉴权/配置类属「改配置」类，UI 提示去配置——不同失败给不同动作 | 同上（各错误码的 Solution 分栏） |
| 演示数据不冒充真实结果 | Mock/演示数据必须在界面上可辨认，避免用户把占位内容当成真实产出 | 本仓库已有同类实践：出片失败时明确「视频不会用 Mock 冒充成功」（`web/bee-studio.html:2354`）、合成失败给具体原因（`web/bee-studio.html:2278`） |

## 我们的现状与差距

| 能力 | 现状 | 差距 |
|---|---|---|
| 剧本生成的 LLM 失败降级 | 后端已回传 `used_mock=true` + `mock_reason`（`api/router.py:1626`、`:1641`）+ `used_custom_model`（`:1628`、`:1642`）；前端 `_llmGenerate` 只判 `r.error`，200 一律当成功（`web/bee-studio.html:3287`） | 降级被显示成 success toast（`:3324`），用户以为拿到了真实 AI 剧本 |
| 主动 Mock 模式 | `force_mock` 也返回 `used_mock=true`（`api/router.py:1550`），但那是用户主动选择，不该与「失败降级」同口径告警 | 缺区分 |
| 剧本改镜头失败原因 | 后端 502 带 `error`/`raw_preview`/`used_custom_model`（`api/router.py:1753-1755`）；前端只取 `data.error`（`web/bee-studio.html:3980`） | `raw_preview` 与是否自定义模型未呈现，定位靠猜 |
| 提示组件 | 已有 `showToast(msg, type)`（`web/bee-studio.html:1806`）与 `--success/--warn/--error` 样式（`:1167-1169`）；另有第二套 `flashNotice`（`:4011`） | 两套提示并存，新增提示需明确用哪套 |

## 已定取舍

> 追加不改写；以下均待人确认。

| 决策 | 取舍 | 理由 | 日期 | 状态 |
|---|---|---|---|---|
| 降级提示走既有 `showToast`，不新建组件 | 复用 `--warn` 样式与 toast 栈 | 视觉一致、零新样式；`flashNotice` 留给模态框内反馈 | 2026-11-27 | 待确认 |
| `force_mock`（用户主动 Mock）不报警告 | 只在非主动的失败降级时提示 | 主动选择不需要被警示，否则噪音 | 2026-11-27 | 待确认 |
| 不做「一键重试」按钮 | 本期只做信息可见 | 重试涉及请求幂等与配额，超出范围 | 2026-11-27 | 待确认 |
| 提示文案给人话原因 + 下一步，不展示原始异常全文 | 截断到可读长度 | 原始堆栈对小白是噪音，技术细节留在日志 | 2026-11-27 | 待确认 |

## 变更历史

| 日期 | 变更 | 依据 |
|---|---|---|
| 2026-11-27 | 初建：4 条行业基线、4 项差距、4 条待确认取舍 | NN/g 错误信息指南 + OpenAI error codes 文档 + 本仓库 `path:line` |
