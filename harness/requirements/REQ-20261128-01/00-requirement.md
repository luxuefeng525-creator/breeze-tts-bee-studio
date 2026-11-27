---
req_id: REQ-20261128-01
stage: requirement
status: active
size: L1
owner: trae
updated: 2026-11-28
acceptance: [AC-01, AC-02, AC-03]
---

# 需求

> L1：填 §1 §2 §3 §4 §6 §7，其余按说明简写。

## 1. 背景与目标

- 触发场景：LLM 出稿失败走 Mock 降级时，后端把异常原因 `str(exc)[:200]` 塞进 `mock_reason` 回传（`api/router.py:1641`），前端再展示（`web/bee-studio.html:3384-3390`）。实测该写法从**头部**硬切 200 字，异常尾部的 `[WinError 10061]` / `getaddrinfo failed` / `connection refused` 全部丢失，界面只剩半截 `Failed to e`（证据：`harness/requirements/REQ-20261127-03/logs/g3-fix-browser-verify.txt` 轮次 3）。
- 期望结果：`mock_reason` 与同文件内其它「给用户看的异常文案」保留**尾部**关键信息，并在分隔符处断开，前端拿到即可直接显示可定位的原因。
- 验证通道：命令退出码（pytest 单测）；异常构造后直接断言返回字符串内容。
- 不做项：不改前端（截断已在 REQ-20261127-03 落地）；不动 `providers/*`、`comfyui` 里对**第三方响应体**的 `resp.text[:N]` 预览（其语义是「看响应开头」，头部截断是对的）；不动 `raw_preview`（`(raw or "")[:500]` 同理）；不引入日志落盘或新表。

## 2. 需求条目

### R-01

当后端需要把异常/失败原因放进响应字段（`mock_reason` 及 `api/router.py` 内同语义的 `error` 文案）时，系统应**保留文本尾部**并在尾部窗口内第一个分隔符处断开，以便用户看到异常类型、Errno、连接细节等真正能定位问题的信息。

- 验收点 AC-01：构造 `str(exc)` 长度 > 200、尾部含 `[WinError 10061]` 的异常，走完整 script-gen 降级路径后，`mock_reason` 必须包含尾部片段；有分隔符时不在词中切断，无分隔符时允许从词中间开始（尾部窗口完整保留）。
- 验收点 AC-02：长度 ≤ 上限的文本原样返回不加省略号；空串 / `None` 返回明确占位（不含 `None`、不留空括号）。
- 边界：超长且无分隔符（硬切尾部）、中文/emoji 多字节、多层包装的重复异常前缀、超时类异常（文本极长）。

### R-02

同一截断策略应应用到 `api/router.py` 内所有「异常转用户文案」的位置，避免只修 `mock_reason` 而在别的入口复现同类缺陷。

- 验收点 AC-03：`api/router.py` 内不再存在裸 `str(e)[:N]` 形式的异常截断（第三方响应预览除外）；全量 `pytest -q` 不红。

## 3. 验收点汇总

| 编号 | 对应需求 | 判定方式 | 优先级 |
|---|---|---|---|
| AC-01 | R-01 | 新增单测：长异常 → `mock_reason` 尾部可见且无半词 | P0 |
| AC-02 | R-01 | 新增单测：短/空/None 三态 | P0 |
| AC-03 | R-02 | 静态扫描 `api/router.py` 无裸 `str(…)[:N]`；`pytest -q` 全绿 | P0 |

## 4. 代码证据与边界反例

| 类型 | 证据 | 说明 |
|---|---|---|
| 现有实现定位 | `api/router.py:1641`（`"mock_reason": f"LLM 调用失败: {str(exc)[:200]}"`）、`:1753`（改镜头失败）、`:1645`（mock 也挂） | 头部硬切，尾部丢失 |
| 同语义位置 | `api/router.py:263`、`:311`、`:1184`、`:1219`、`:1249`、`:1667`、`:1844` | 均为异常转用户文案，同一缺陷模式 |
| 明确不动的位置 | `api/llm_client.py:121`、`api/providers/kling.py:57`、`api/router.py:1627`、`api/router.py:1755` | 第三方响应体/`raw_preview`，语义是「看开头」 |
| 调用方 | `web/bee-studio.html:3384-3390` | 前端整段展示 `err.payload.error` / `mockReason`，不二次加工 |
| 边界 / 反例 1（超长） | `str(exc)` 262 字、尾部为 `[WinError 10061] ...` | 尾部保留，末尾完整可读 |
| 边界 / 反例 2（空） | `str(exc)` 为空串或 `exc` 为 `None` | 输出占位「未知原因」，不出现 `None` |
| 边界 / 反例 3（超时） | 上游超时异常链（多层 `Max retries exceeded`，长度远超 200） | 同超长策略，保留尾部连接/超时细节 |
| 边界 / 反例 4（重复） | 同一异常被多层包装，前缀重复出现 | 本需求不做去重，只保证截断不切碎词 |

- 定位来自实际打开的 `api/router.py`（工作树 f:\TTS2\breeze-tts-trae）。

## 5. 影响面

- 涉及模块：`api/router.py`（新增 1 个模块级 helper + 替换 10 处调用点）、`tests/test_llm_error_clip.py`（新增）
- 涉及接口：`POST /api/llm/script-gen`、`/api/llm/script-edit`、`/api/providers/{p}/keys`、`/api/video/generate`、`/api/bee/script/{pid}/shots` 的失败响应字段
- 数据变更：无（只改内存中的响应字符串，不落库）

## 6. 人确认

```text
G1: PASS | 确认人: 用户 | 时间: 2026-11-28 05:00 | 确认原文: "G1 通过（推荐）"
```

## 7. 澄清清单

| 类别 | 结论 |
|---|---|
| 触发与时序 | LLM/视频任务等操作抛出异常并要回传文案时触发；先取异常字符串，再限长（尾部保留），再拼进响应 |
| 数据来源与精度 | 来源为 `str(exc)`；口径 = 上限 200 字以内原样、超出保留尾部 200 并在最近空格/中文标点处断开，加前缀省略号 |
| 隐私与授权 | 异常文本可能含 URL、host、端口；沿用现有面板本机口径，不外发 |
| 离线与失败 | 端点拒连/超时/上游 5xx 都走同一 helper；helper 本身必须对 `None`、非字符串、空串安全 |
| 存储与留存 | 不新增存储；错误文案仍只在响应与既有日志里 |
| 谁可见 | 本机面板使用者，与现状一致 |

## 8. 需求分析与同行对标

| 需求 | 对标对象 | 来源 | 他们的做法 | 我们的取舍 | 理由 |
|---|---|---|---|---|---|
| R-01 | 域规则 ai-degradation-notice（含 NN/g 与 OpenAI error codes 基线） | `harness/domains/ai-degradation-notice.md:9-13`、`:33` | 错误信息要「高可见性 + 可行动」，给出用户能执行的下一步 | 采纳并细化：「截断到可读长度」在异常场景应保留尾部，否则丢失可行动信息 | 实测已证明头部截断让 UI 只剩 `Failed to e` |
| R-02 | Python 标准库异常链惯例（异常详情在 `str(exc)` 末尾） | `api/router.py:1641` 现状 + `harness/requirements/REQ-20261127-03/logs/g3-fix-browser-verify.txt`（轮次 3 逐字取证） | — | 采纳「同一策略统一替换」 | 只修一处会在 `:1753` 等处复现 |

### 8.1 维度检查

| 维度 | 结论 | 来源 | 不适用理由 |
|---|---|---|---|
| 同类同行 | 见上表 | `harness/domains/ai-degradation-notice.md` | — |
| 功能清单与边界 | R-01/R-02 与 §4 边界 | 本文件 | — |
| UI 设计 | 不适用：无界面 | — | 只改后端响应字符串，动画面 |
| 数据库 | 不适用：无数据库 | — | 不落库、不建表 |
| 技术选型 | 沿用 Python 3 + 标准库，无新依赖 | `api/router.py:1-10` | — |

## 9. 覆盖矩阵

不适用：无 ≥2 取值维度（单一 LLM/任务链路，端口与库表口径唯一）。

## 10. 功能设计卡

### 卡 1：异常文案尾部保留截断

- **用户旅程位置**：入口在 Bee Studio「✨ 生成剧本 & 分镜」「AI 重写这个镜头」；默认态=正常出稿；加载=调用中；失败=Mock 降级并弹 warn（`web/bee-studio.html:3384-3390`）；成功=success toast。上一态=填写主题，下一态=继续配音/出片。
- **行业基线**：错误信息要说明发生了什么并给出可执行下一步（NN/g Visibility/Communication Guidelines）；API 层结构化回传原因与解决方案（OpenAI error codes 的 Cause/Solution 分栏）。证据：`harness/domains/ai-degradation-notice.md:9-13`。
- **我们的现状与差距**：`api/router.py:1641` 用 `str(exc)[:200]` 从头部硬切，用户感知 = 界面上出现 `Failed to e` 这类残句，无法判断是端点错了还是 Key 错了；缺尾部 Errno/连接细节。
- **决策点与后果**：截断方向 → 取「保留尾部 + 分隔符断开」，代价 = 前缀套话（`LLM 调用失败: `）可能被截掉，需接受用省略号表示有省略；策略适用范围 → 只统一 `router.py` 内异常文案，第三方响应预览保持头部截断，风险 = 两处策略并存需在注释里写清，避免后人误改。
- **不做什么**：不做异常文案去重；不做原始堆栈透出；不纳入 `providers/*` 的响应体预览改造（超出范围）；不改前端。
- **交付形态**：无视觉（纯后端字符串处理，动画面）

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-11-28 | 初稿 | REQ-20261127-03 收口时的遗留项 RV-03 + 记忆候选池条目 |

## 12. 功能开发拆解

| 切片 | 目录结构 | 文件归属 | 路由 / 入口 | 函数 / 模块 | 数据表 / 字段 | 验收方式 | 清理项 |
|---|---|---|---|---|---|---|---|
| 切片 A | `api/` | `api/router.py` | 不适用：模块级 helper | 新增 `_clip_reason(text, limit)`，替换 10 处异常文案截断点 | 不适用：无持久化 | `pytest tests/test_llm_error_clip.py -q` | 无 |
| 切片 B | `tests/` | `tests/test_llm_error_clip.py` | `POST /api/llm/script-gen`（经 `handle_post` 构造异常） | 单测：长异常尾部可见 / 短文本原样 / None 占位 / 无裸 `str()[:N]` | 不适用：无持久化 | 同上 | 临时写的注册表探针条目（若用 curl 实测则还原） |
