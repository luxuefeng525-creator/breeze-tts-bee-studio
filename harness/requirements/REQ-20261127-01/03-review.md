---
req_id: REQ-20261127-01
stage: review
status: active
owner: trae
updated: 2026-11-27
---

# 审查记录

> 独立只读子代理审查，非开发自述。审查方式：`git diff main...HEAD` + 代码通读 + 复跑新增单测。

## 1. 审查范围

- 改动文件：`api/style_templates.py`（新增）、`data/style_templates.json`（新增）、`api/router.py`、`web/bee-studio.html`、`tests/test_style_templates.py`（新增）
- 需求条目：R-01、R-02、R-03（AC-01~AC-04）
- 未覆盖需求：无；C3 经确认不在本期（已实现且 A 段实测导出 PASS）

## 2. 问题清单

| 编号 | 严重度 | 性质 | 位置 | 问题 | 证据 | 改法 | 已补检查 | 状态 |
|---|---|---|---|---|---|---|---|---|
| RV-01 | 建议 | 需人判断 | `tests/test_style_templates.py`、`02-test.md` AC-02 | AC-02 原表述把「注入单测」与「force_mock 落库」混为一谈 | 单测只断言 `build_script_prompts` 注入；落库靠实调日志 | 已在 02-test 写明两类取证分工 | — | 已修 |
| RV-02 | 建议 | 需人判断 | `api/router.py:942`、`bee-studio.html` 调用处 | `build_script_prompts` 的 `style` 形参未被使用，body 旧字段成死值 | 函数体内无 `style` 引用 | 删除该形参并更新调用 | — | 已修 |
| RV-03 | 建议 | 需人判断 | `web/bee-studio.html` refreshShotSuggestion | 非法总时长只静默保持，无提示 | 原实现直接 return | 补 toast「总时长无效，已保持当前镜头数」 | — | 已修 |
| RV-04 | 建议 | 需人判断 | `web/bee-studio.html` switchProject | 重开旧项目不回显其风格归属 | 数据在 shots_json，UI 不可见 | switchProject 按首个镜头 styleId 回显 select | — | 已修 |
| RV-05 | 建议 | 需人判断 | `00-requirement.md` R-01 边界 | 文档写「内置 4 项」，实现内置 6 项 | 与 `api/style_templates.py:_builtin()` 不一致 | 文档改为 6 项 | — | 已修 |
| RV-06 | 建议 | 需人判断 | `web/bee-studio.html` #scriptShots | 动态建议值追加到末尾，下拉顺序错乱 | 3/8/10/12 会排在 4~7 之后 | 新增 sortShotOptions 按数值排序 | — | 已修 |
| RV-07 | 建议 | 需人判断 | `api/style_templates.py:_valid` | JSON `true` 会被当成合法秒数（bool⊂int） | `isinstance(True,(int,float))` 为真 | 校验排除 bool | — | 已修 |
| RV-08 | 建议 | 需人判断 | 前后端 `suggest_shot_count` / `suggestShotCount` | 双份实现，未来口径可能漂移 | 两处独立四舍五入逻辑 | 本期接受；两端都以「总时长/单镜秒数」为准，后续如改口径需同步 | — | 接受 |
| RV-09 | 建议 | 需人判断 | git 工作区 | 改动尚未提交 | `git status` 显示未跟踪/修改 | 结项时提交并 merge 回 main | — | 接受 |
| RV-10 | 建议 | 需人判断 | `web/bee-studio.html` generateVideo 无 Key 分支 | 原提示误称「LongCat 写不了视频」并暗示可出片 | A 段批量生成只会警告，文案误导 | 改为本地 ComfyUI / 配置 Key 指引，去掉 Mock 承诺 | — | 已修 |
| RV-11 | 建议 | 需人判断 | `web/bee-studio.html` exportProject | 无视频镜头被静默跳过，出现「2 镜头导出 1 秒」 | A 段实测 output_duration=1.0 / 2 镜头 | 发起前统计并 toast 告知跳过数量 | — | 已修 |

## 3. 检查项

| 维度 | 结论 | 说明 |
|---|---|---|
| 需求一致 | PASS | R-01/R-02/R-03 与 AC-01~04 均有代码与证据 |
| 权限 | PASS | 本机单用户，无越权面；模板接口只读本地文件 |
| 异常处理 | PASS | 文件缺失/损坏回退；未知 style_id 落默认；前端接口失败回退 |
| 数据与事务 | PASS | 不新增表；style_id 写入既有 shots_json，可逆（删模板文件即回退内置） |
| 影响面 | PASS | 旧调用不带 style_id 仍可用；全量回归 621 passed |
| 调试残留 | PASS | 无 console.log/print；探针脚本均在仓库外 |
| 安全 | PASS | 风格文案经 textContent 渲染，无 XSS；无密钥 |

## 4. 遗留与接受

| 遗留项 | 接受人 | 理由 | 复审时间 |
|---|---|---|---|
| 真实 LLM 下风格一致性 | 用户 | LongCat 402，明确不充值；仅证明约束进入 prompt | 有可用 Key 时 |
| C3 自动剪辑/拼接 | 用户 | 已实现且实测导出 PASS，本期不做 | 需要时单独验收 |

## 5. 结论

可发布（等 G3 人确认）；无阻断项。
