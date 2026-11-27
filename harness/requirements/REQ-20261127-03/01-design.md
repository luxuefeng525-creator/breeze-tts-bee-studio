---
req_id: REQ-20261127-03
stage: design
status: active
owner: trae
updated: 2026-11-27
---

# 方案设计

## 1. 现状

- 相关入口：剧本生成按钮处理 `web/bee-studio.html:3306-3345`；改镜头提交 `web/bee-studio.html:3955-4001`
- 后端契约（已存在，无需改）：script-gen 成功 `{used_mock:false, generated_by}`、失败降级 `{used_mock:true, mock_reason, used_custom_model}`（`api/router.py:1626`、`:1641`）、主动 Mock `{used_mock:true, mock_reason:"force_mock"}`（`:1550`）；script-edit 失败 `{error, used_custom_model, raw_preview}`（`:1753-1755`）
- 前端缺口：`_llmGenerate` 只判 `r.error`（`web/bee-studio.html:3287`），200 一律返回 → 调用处无条件弹 success（`:3324`）；`submitScriptEdit` 失败只取 `data.error`（`:3980`）
- 可复用组件：`showToast(msg,type)`（`:1806`）+ `.toast.success/.info/.error` 样式（`:1167-1169`）；另有第二套 `flashNotice`（`:4011`，顶部右侧，用于模态框内反馈）

## 2. 方案

| 方案 | 做法 | 优点 | 代价 | 结论 |
|---|---|---|---|---|
| A | 复用 `showToast(msg,'warn')`，在 `_llmGenerate` 返回值里带 `usedMock`/`mockReason`/`usedCustomModel`，由调用处决定 success 还是 warn | 与既有反馈同一套组件与位置，零新样式；改动集中在两个函数 | toast 3.2s 自动消失，原因可能被错过 | 选用 |
| B | 新增常驻提示条（dismiss 才消失） | 信息不会被漏看 | 与 toast 体系并行成两套反馈，占屏；新增样式与状态管理 | 否决：代价大于收益 |
| C | 复用 `flashNotice`（顶部右侧） | 现成 | 与生成链路的 toast 不在同一位置，口径分裂；且 flashNotice 无类型样式 | 否决 |

选定：A。理由：本期目标是「降级不再被显示成成功」，最小闭环是复用既有反馈通道；文案自含关键信息（是哪类问题 + 下一步），抵消自动消失的代价。

## 3. 模块与改动点

| 文件/模块 | 改动 | 原因 |
|---|---|---|
| `web/bee-studio.html` `_llmGenerate`（:3281-3304） | 返回值增加 `usedMock` / `mockReason` / `usedCustomModel` 三个字段（透传响应，不加工） | R-01 |
| `web/bee-studio.html` 生成按钮处理（:3320-3334） | 按 `usedMock && mockReason !== 'force_mock'` 分支：warn 提示（含原因截断 + 自定义模型下一步）；否则维持 success | R-01 / AC-01/03 |
| `web/bee-studio.html` `submitScriptEdit`（:3979-3985） | 失败分支拼接 `used_custom_model` 上下文与 `raw_preview` 截断 | R-02 / AC-04 |
| `web/bee-studio.html` 新增小工具函数 | `truncateReason(text, max=120)`：空值兜底 + 超长截断（保留结尾） | 边界反例 1/2 |

## 4. 接口与数据结构

```text
输入：不变（沿用现有请求体，无新增参数）
消费字段（后端已有）：
  script-gen 200 → { used_mock, mock_reason, used_custom_model, generated_by }
  script-edit 非 2xx → { error, need_key?, used_custom_model, raw_preview? }
展示：toast 文本 = 人话原因 + （自定义模型时）下一步；≤120 字
存储：不落盘、不留历史
```

## 5. 权限与边界

- 谁能触发：本机面板使用者，无新增角色
- 越权处理：不涉及（只读响应字段并展示）
- 隐私：不展示 Key；只展示后端已回传的失败摘要，原始堆栈仍只留在日志

## 6. 影响面

- 调用方：仅 `web/bee-studio.html` 内部两处结果处理；后端与接口零改动
- 共享状态：不改 `state` 结构、不动项目/镜头数据流；提示为一次性副作用
- 兼容性：内置 provider 路径（`used_mock=false`）行为完全不变；响应缺 `used_mock` 的老路径按非降级处理（不提示），不会误报
- 其它链路：视频/合成/配音的既有提示不动（不在本需求范围）

## 7. 回退思路

- 触发条件：改动后正常 LLM 生成出现误报告警，或界面脚本异常导致生成流程不可用
- 回退步骤：`git revert` 本次提交——纯前端展示层改动，无数据迁移、无接口变更

## 8. 未决问题

| 问题 | 影响 | 待定方 |
|---|---|---|
| 是否给降级提示加「一键重试」 | 体验增强 | 用户（本需求不做） |
| 是否在设置页做模型连通性测试入口 | 前置发现配置错误 | 用户（另开需求） |
