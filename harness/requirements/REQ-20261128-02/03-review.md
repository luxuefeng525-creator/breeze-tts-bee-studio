---
req_id: REQ-20261128-02
stage: review
status: active
owner: trae（breeze-tts 主工作区）
updated: 2026-11-28
---

# 审查记录

> 依据实际 diff（`git diff main` 的 7 个文件）与需求条目，不参考开发自述。本轮为 L 档，采用「主代理逐条自查 + 可复核证据」；未另派独立子代理。

## 1. 审查范围

- 改动文件：`panel.py`（+299 行：sidecar 客户端、三条路径路由、一键开关、`engine` 字段校验）、`web/index.html`（+12 行：引擎下拉 ×2、诊断区块）、`web/app.js`（+71 行：保存带 engine、禁选逻辑、开关轮询）、新增 `tools/sovits_ref.py`、`tools/gpt_sovits_sidecar.py`、`tests/test_sovits_route.py`
- 需求条目：R-01（AC-01/05）、R-02（AC-02/03）、R-03（AC-04）、R-04（AC-06）
- 未覆盖需求：无

## 2. 问题清单

| 编号 | 严重度 | 性质 | 位置 | 问题 | 证据 | 改法 | 已补检查 | 状态 |
|---|---|---|---|---|---|---|---|---|
| RV-01 | 建议 | 需人判断 | panel.py:_sovits_synthesize | 批量路径逐条串行调 sidecar，N 条文本耗时为 N × 单条；10 条 3 秒音频可能超过 900 秒超时 | 实测单条 3 秒音频约 30~50 秒 | 批量前置估算并给前端提示，或限制条数 | 02-test.md §5 | 接受 |
| RV-02 | 建议 | 需人判断 | panel.py:_sovits_ref_for | 无锁：同音色并发首次合成会重复裁段与 ASR | 代码路径 `_sovits_ref_for` → `_prepare` | 加按 voice_id 的锁；当前并发场景罕见 | 02-test.md §3 | 接受 |
| RV-03 | 建议 | 需人判断 | panel.py:_sovits_switch_status | 每次状态查询都 probe（1.5 s 超时），弹窗打开时有可感知延迟 | 浏览器实测：打开诊断到区块渲染约 2 s | 状态加短时缓存；当前可接受 | — | 接受 |
| RV-04 | 建议 | 需人判断 | panel.py:_stream_pcm | 空 PCM 未显式防御（sidecar 正常不会返回空，异常已在 `_sovits_synthesize` 拦截） | panel.py:_stream_pcm | 可加 `if not pcm: 返回 502`；当前链路已拦 | tests/test_sovits_route.py | 接受 |
| RV-05 | 建议 | 需人判断 | panel.py:PUT /api/voices/{id} | 后端未校验「design 类型 + engine=gpt-sovits」组合，前端禁选但直接调 API 可绕过，最终在合成时报「没有参考音频」 | 前端 `syncEngineOptions` 只在前端拦 | 后端加同一条组合校验，错误更早更准 | tests/test_sovits_route.py::test_design_voice_cannot_use_sovits | 已修 |
| RV-06 | 建议 | 需人判断 | web/app.js:renderLibrary | 音色卡片不显示引擎标识，用户看不出哪个音色走 GPT-SoVITS | 卡片模板只有类型与时长 | 卡片加引擎小标签 | 浏览器 DOM 核对 | 已修 |

## 3. 检查项

| 维度 | 结论 | 说明 |
|---|---|---|
| 需求一致 | PASS | R-01~R-04 均有实现与证据；不做项遵守（未做训练、未做独立客户端、未替换主力引擎、未动 7860 契约） |
| 权限 | PASS | 不涉及鉴权；启动接口只允许本机面板调用，未新增对外暴露面 |
| 异常处理 | PASS | sidecar 不可用 503 带指引；参考音频缺失/过短/无文稿均 400；启动失败落 error 不卡在 starting |
| 数据与事务 | PASS | `engine` 走 `_update_json` 原子写；非法值拒绝；删字段即回退 |
| 影响面 | PASS | 仅三条合成路径的分支 + 音色弹窗；不标 engine 的音色原分支未改 |
| 可维护 | PASS | 启停逻辑复用 CLI 一份；裁段逻辑复用 `sovits_ref` 一份，无重复实现 |
| 安全 | PASS | 参考音频路径取自面板账本并校验存在，越界由 `DATA / rel` 约束；sidecar 仅监听 127.0.0.1 |
| 调试残留 | PASS | 无 print/console.log；临时探针脚本已删；`data/sovits_refs` 是正式缓存 |
| 并发 | PASS | 两条链路共用 `_INFER_LOCK`；重复点启动直接返回在途状态 |

## 4. 遗留与接受

| 遗留项 | 接受人 | 理由 | 复审时间 |
|---|---|---|---|
| RV-01 批量耗时线性增长 | 雪峰 | GPT-SoVITS v3 本身的推理特性，接线层无法抹平 | 有批量需求时 |
| RV-02 并发重复裁段 | 雪峰 | 同一音色并发合成属罕见操作，重复裁段只多花一次时间 | 2026-12-28 |
| RV-03 状态查询延迟 | 雪峰 | 打开诊断才查一次，2 秒可接受 | 2026-12-28 |
| RV-04 空 PCM 防御 | 雪峰 | 上游异常已在调用层拦下 | 2026-12-28 |
| RV-05 后端组合校验 | 雪峰 | 已在入库前拦下，前端与 API 两条路都拦 | — |
| RV-06 卡片显示引擎 | 雪峰 | 卡片已显示「类型 · GPT-SoVITS」 | — |

## 5. 结论

可发布（功能、用例、真实环境端到端均闭环；审查 6 项已修 2、接受 4）。
