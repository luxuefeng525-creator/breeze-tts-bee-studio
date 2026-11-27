---
req_id: REQ-20261128-01
stage: design
status: active
owner: trae
updated: 2026-11-28
---

# 方案设计

## 1. 现状

- 相关入口：`api/router.py:1641`（script-gen 降级 `mock_reason`）、`:1753`（script-edit 失败）、`:1645`（mock 也挂）；同文件 `:263`、`:1184`、`:1219`、`:1249`、`:1667`、`:1844`
- 现有数据流：异常对象 → `str(exc)` → `[:200]` 头部硬切 → 拼进响应 JSON → 前端整段显示（`web/bee-studio.html:3384-3390`）
- 现有同类实现：前端已有 `truncateReason`（`web/bee-studio.html:1813`，尾部保留 + 码点切分），本需求把同一策略下沉到后端

## 2. 方案

| 方案 | 做法 | 优点 | 代价 | 结论 |
|---|---|---|---|---|
| A | 新增模块级 `_clip_reason(text, limit=200)`：尾部窗口保留 + 窗口内第一个分隔符断开 + 空值占位；替换 router.py 内 9 处异常文案 | 与前端策略一致；可测；改动局部；不会把末尾 Errno 当前缀切掉 | 需替换多处调用点 | 选用 |
| B | 保留头部截断，仅把上限提到 500 | 改动最小 | 治不了「尾部丢失」——异常链长度不可控，超长仍会切掉尾部 | 否决 |
| C | 不截断，透传完整 `str(exc)` | 信息零丢失 | toast/界面被撑爆；可能带出堆栈与敏感路径 | 否决 |

选定：A，理由：缺陷本质是**截断方向错了**，异常类型/Errno/连接细节都集中在末尾（`NewConnectionError` / `[WinError 10061]` / `getaddrinfo failed`）。实现取尾部 200 字窗口后，在窗口内找**第一个**分隔符断开；若取最后一个分隔符，会把 `[WinError 10061] Connection refused` 中的 Errno 当成前缀切掉。

## 3. 模块与改动点

| 文件/模块 | 改动 | 原因 |
|---|---|---|
| `api/router.py` | 新增 `_clip_reason(text, limit=200)`（放在模块级工具区） | R-01/R-02 |
| `api/router.py` | 替换 `:263`、`:1184`、`:1219`、`:1249`、`:1641`、`:1645`、`:1667`、`:1753`、`:1844` 共 9 处 | R-02 |
| `tests/test_llm_error_clip.py` | 新增单测：helper 行为 + script-gen 降级路径端到端断言 | AC-01/02/03 |

不动的同形位置（语义不同）：`api/llm_client.py:121`、`api/providers/kling.py:57`、`api/router.py:1627`（`raw_preview`）、`:1755`。

## 4. 接口与数据结构

```text
输入：任意对象（通常为异常实例），可为 None
输出：str，长度 ≤ limit + 1（含前缀省略号）；空值返回「未知原因」
存储：无
```

## 5. 权限与边界

- 谁能触发：本机面板使用者（沿用现有口径）
- 越权处理：不新增权限概念
- 只读边界：不写库、不落盘、不外发

## 6. 影响面

- 调用方：`POST /api/llm/script-gen`、`/api/llm/script-edit`、`/api/providers/{p}/keys`、`/api/providers/{p}/test`、`/api/video/generate`、`/api/bee/script/{pid}/shots` 的失败响应；前端仅把 `mock_reason`/`error` 整段显示，不解析格式
- 共享状态：不改 `_API_KEYS`、不改 DB schema、不改端口配置
- 兼容性：字段名与结构完全不变，只改内容截断方向；老前端不受影响（不依赖前缀），新前端（REQ-20261127-03）展示更完整
- 回归风险：9 处替换均为同语义改写，若 helper 对非字符串抛错会影响错误分支——helper 内部必须 `str()` 兜底

## 7. 回退思路

- 回退触发条件：替换后某个失败分支开始抛 `TypeError`/输出异常，或全量 pytest 变红
- 回退步骤：`git revert` 本次提交即可——纯函数替换，无迁移、无数据写入

## 8. 未决问题

| 问题 | 影响 | 待定方 |
|---|---|---|
| 是否顺带统一 `providers/*` 的第三方响应预览 | 那些位置语义是「看响应开头」，本次不动 | 用户（另议） |
| 是否顺带修同模式的其它文件 | `api/video_upscale.py:335`、`api/cost_calc.py:182`、`api/comfy_templates.py:79` 也是异常头部硬切，但不在本需求批准的 `router.py` 9 处范围内 | 用户（另议） |
