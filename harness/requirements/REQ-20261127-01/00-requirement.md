---
req_id: REQ-20261127-01
stage: requirement
status: active
size: L2
owner: trae
updated: 2026-11-27
acceptance: [AC-01, AC-02, AC-03, AC-04, AC-05, AC-06]
---

# 需求

> L2：全填。本需求只做「剧本创作侧缺口」，不重做已实现的ffmpeg 合成。

## 1. 背景与目标

- 触发场景：用户在 Bee Studio 点「新建项目 / AI 生成剧本」时，视觉风格只能从 4 个硬编码项里选；选定风格只作为一行文本进 LLM prompt，生成出的镜头 prompt 不保证符合该风格；镜头数量靠人手选，与总时长不匹配时全靠用户自己发现。
- 期望结果：风格来自可维护的模板库（接口 + 前端渲染），模板的结构化提示真正注入 LLM system prompt，并按模板/总时长给出建议镜头数；生成失败时模板行为仍可用 mock 复现。
- 验证通道：浏览器（Bee Studio 新建项目弹窗 DOM）+ pytest（模板加载、prompt 注入、建议镜头数）+ curl（`/api/bee/style-templates`）。
- 不做项：不做 C3「视频自动剪辑/拼接」新开发（`video_pipeline/ffmpeg_export.py:155` + `api/router.py:1251` 已实现且 2026-11-27 实测导出成功）；不接云端 LLM Key（LongCat 当前 402 Payment Required，用户已明确全本地、不充值）；不改 TTS 合成链路；不动角色/参考图授权。

## 2. 需求条目

### R-01 剧本风格模板库

当 用户打开「AI 生成剧本」弹窗或请求风格列表 时，系统应 从模板库返回风格（id/名称/说明/提示词要点/建议镜头秒数），以便 风格选项可维护且不写死在 HTML。

- 验收点 AC-01：`GET /api/bee/style-templates` 返回 ≥6 个模板，每项含 `id,name,description,prompt_hint,shot_seconds`；前端 `#scriptStyle` 的选项文案与接口返回一致（DOM 断言）。
- 边界：模板文件缺失、内容为空或接口 500/超时 → 回退到内置 6 项且接口仍 200；历史项目里的未知风格名 → 前端显示原值且不报错；同一风格被重复选择/重复提交 → 幂等，不产生重复数据。

### R-02 风格真正注入 LLM 与 mock

当 以某个风格模板发起 `POST /api/llm/script-gen` 时，系统应 把该模板的 `prompt_hint` 写进 system prompt 的画面约束，并在返回的 shot 上带上 `style_id`，以便 生成的 prompt 与所选风格一致。

- 验收点 AC-02：单元测试断言 `style=cyberpunk` 时 system prompt 含对应 `prompt_hint` 关键词，且 force_mock 返回的 shots 全部带 `style_id=cyberpunk`。
- 边界：风格 id 不存在 → 400 或回退默认模板（取默认，不静默产出一个"无风格"结果）；mock 与真实 LLM 走同一注入函数；模板文本超长 → 截断到上限。

### R-03 建议镜头数与总时长联动

当 用户修改总时长或风格 时，系统应 按 `shot_seconds` 给出建议镜头数并填入 `#scriptShots`（可手工改），以便 不出现「15 秒要 7 个镜头」这类每镜不足 2 秒的配比。

- 验收点 AC-03：`total=15` 时建议 ≤3；`total=30` 时建议 5；`total=60` 时建议 8~10；建议值计算函数有单测（边界 15/30/60/90）。
- 边界：总时长非法（0/负数/非数字）→ 保持上次建议不变并提示；用户手选后不被自动覆盖（仅在切换时长/风格时刷新建议）。

### R-04 无 Key 出片提示真实化

当 镜头/批量出片的 provider 未配置 API Key 时，系统应 明确告知「不会用 Mock 冒充成功」并给出本地 ComfyUI 或配置 Key 的出路，以便 用户不再被「无 Key 自动 Mock」的假承诺误导。

- 验收点 AC-05：批量按钮 toast 不再出现「自动 Mock」字样；单镜头无 Key 提示改为本地/配 Key 指引（代码位置可查，不点击批量按钮以免真实出片）。
- 边界：占位 Key（placeholder）与完全缺失分别给不同原因；提示不阻断剧本/配音/导出。

### R-05 导出前缺失素材可见

当 项目里部分镜头没有视频却点「导出成片」时，系统应 在发起合成前提示将跳过这些镜头，以便 不再出现「2 个镜头导出成 1 秒」而无解释。

- 验收点 AC-06：浏览器实测导出含 1 个无视频镜头的项目，先出现警告 toast 再开始合成。
- 边界：全部镜头都有视频时不警告；缺音频但有视频仍可导出（不因缺音频拦截）。

## 3. 验收点汇总

| 编号 | 对应需求 | 判定方式 | 优先级 |
|---|---|---|---|
| AC-01 | R-01 | `curl /api/bee/style-templates` + 浏览器 DOM 断言 | P0 |
| AC-02 | R-02 | `pytest tests/test_style_templates.py`（注入函数 + mock 落库） | P0 |
| AC-03 | R-03 | `pytest tests/test_style_templates.py`（建议镜头数表） | P1 |
| AC-04 | R-01/R-03 | 浏览器实测：弹窗风格选项来自接口、改总时长后镜头数变化 | P0 |
| AC-05 | R-04 | 代码定位 + 文案核对（不点击批量按钮，避免真实出片） | P1 |
| AC-06 | R-05 | 浏览器实测导出警告 toast | P0 |

## 4. 代码证据与边界反例

| 类型 | 证据 | 说明 |
|---|---|---|
| 现有实现定位 | `web/bee-studio.html:1536-1541` | 风格 `<select>` 硬编码 4 项 |
| 现有实现定位 | `web/bee-studio.html:3148`、`:3197` | `styleHints` 仅 4 个硬编码中文后缀，生成后才追加 |
| 调用方 / 数据来源 | `api/router.py:1407-1424` | system/user prompt；风格只占 `风格/色调：` 一行 |
| 调用方 / 数据来源 | `api/router.py:1353-1364` | `_normalize_shot` 不含 style 字段 |
| 现状已实现（不做） | `video_pipeline/ffmpeg_export.py:155`、`api/router.py:1251-1312` | ffmpeg concat/export 已实现；2026-11-27 A 段实测导出 PASS（`e:\Temp\trae\bee-a\result-export-ui.json`） |
| 差距依据 | `../docs/product/feature-matrix.md:433-436` | §7.4 把「镜头数推断/画面描述质量/风格模板」列为 🟡/❌（其中「剧本迭代未做」已被 `api/router.py:1521` 推翻，本需求不采信该行） |
| 差距依据 | `../docs/product/PRD-v4-intelligent-studio.md:573` | C2 剧本风格模板库「本轮未排，下一轮」= 本需求 |
| 边界 / 反例 1 | 风格模板接口 500 / 文件缺失 | 回退内置模板，前端不白屏 |
| 边界 / 反例 2 | 总时长 15s 配 7 镜头 | 建议值收敛到 ≤3，避免每镜 <2s |
| 边界 / 反例 3 | 历史 shots 带未知 style_id | 详情读回不报错，按默认风格渲染 |
| 现有实现定位 | `web/bee-studio.html` generateVideo 无 Key 分支 | 原提示误称 LongCat 与「自动 Mock」 |
| 现有实现定位 | `web/bee-studio.html` exportProject | 原实现静默跳过无视频片段 |
| 边界 / 反例 4 | 部分镜头无视频仍要求导出 | 先警告跳过数量，再发起合成 |

## 5. 影响面

- 涉及模块：`api/router.py`（script-gen）、新增模板数据与加载函数、`web/bee-studio.html`（弹窗渲染）、`tests/test_style_templates.py`。
- 涉及接口：新增 `GET /api/bee/style-templates`；`POST /api/llm/script-gen` 请求/响应扩展 `style_id`（向后兼容旧调用）。
- 数据变更：新增 `data/style_templates.json`（可逆，删除即回退内置）；不新增数据库表，不改 `scripts` 表结构（style_id 存进既有 `shots_json`）。

## 6. 人确认

```text
G1: PASS | 确认人: 用户 | 时间: 2026-11-27 05:36 | 确认原文: "按当前范围确认（推荐）"
```

## 7. 澄清清单

| 类别 | 结论 |
|---|---|
| 触发与时序 | 打开弹窗即拉模板；改总时长/风格后重算建议镜头数；点生成时把 style_id 传给 script-gen |
| 数据来源与精度 | 模板来自 `data/style_templates.json`，接口每次读盘（小文件，无需缓存）；镜头数为整数建议值 |
| 隐私与授权 | 不涉及个人信息；无外发（模板本地文件） |
| 离线与失败 | 断网不影响模板；LLM 不可用按现状降级 mock，mock 同样带 style_id |
| 存储与留存 | 模板文件随仓库；生成结果存既有 `scripts` 表 `shots_json`，无新表 |
| 谁可见 | 本机单用户；无登录、无外发 |

## 8. 需求分析与同行对标

| 需求 | 对标对象 | 来源 | 他们的做法 | 我们的取舍 | 理由 |
|---|---|---|---|---|---|
| R-01 | 同类产品风格预设 | `../docs/product/feature-matrix.md:436` | 工程可补（模板库 JSON） | 采纳 | 本地即可做，无依赖 |
| R-01 | 本项目 PRD 排期 | `../docs/product/PRD-v4-intelligent-studio.md:573` | 下一轮 | 采纳 | 用户指定补剧本创作侧 |
| R-02 | 本项目差距表 | `../docs/product/feature-matrix.md:434` | Prompt engineering | 改造为模板化注入 | 比每次手写更稳定 |
| R-03 | 短视频分镜常规 | `../docs/product/PRD-v4-intelligent-studio.md`（分镜时长 5~15s 约束，`api/router.py:1412`） | 每镜 5~15s | 采纳为建议口径 | 与既有 LLM 约束一致 |
| R-04 | 本项目差距表 | `../docs/product/feature-matrix.md:390` | 工程可补（ffmpeg concat）；反馈要真实 | 采纳「不假装成功」 | A 段实测暴露假文案 |
| R-05 | A 段实测 | `e:\Temp\trae\bee-a\result-export-ui.json` | 导出成功但时长 1s/2 镜头 | 采纳为提示缺口 | 用户会以为导出坏了 |

### 8.1 维度检查

| 维度 | 结论 | 来源 | 不适用理由 |
|---|---|---|---|
| 同类同行 | 风格预设以模板/预设包形式提供 | `../docs/product/feature-matrix.md:436` | 无 |
| 功能需求 | 模板 CRUD 最小集（本期只读+内置回退）；注入；建议镜头数 | 本文件 §2 | 无 |
| UI 设计 | 沿用现有 modal 的 select 与说明区，不新增页面 | `web/bee-studio.html:1503-1568` | 无 |
| 数据库 | 无新表；style_id 写入既有 `shots_json` | `api/router.py:338-355` | 无 |
| 技术选型 | 沿用 Python stdlib + 原生 JS，零新依赖 | `api/router.py:1-30` | 无 |

## 9. 覆盖矩阵（有 ≥2 个取值时填）

覆盖维度：风格=电影感,赛博朋克,日系动画,写实纪录片,产品广告,竖屏短视频

| 维度/取值 | 取值范围 | 验收点 | 对标基线（来源） | 说明 |
|---|---|---|---|---|
| 风格/电影感 | 模板库第 1 项 | AC-01、AC-04 | `../docs/product/feature-matrix.md:436` | 内置默认项，prompt_hint 注入 |
| 风格/赛博朋克 | 模板库第 2 项 | AC-01、AC-04 | `../docs/product/feature-matrix.md:436` | 霓虹/雨夜提示 |
| 风格/日系动画 | 模板库第 3 项 | AC-01、AC-04 | `../docs/product/feature-matrix.md:436` | 柔和色调提示 |
| 风格/写实纪录片 | 模板库第 4 项 | AC-01、AC-04 | `../docs/product/feature-matrix.md:436` | 手持质感提示 |
| 风格/产品广告 | 模板库第 5 项 | AC-01、AC-04 | `../docs/product/PRD-v4-intelligent-studio.md:573` | 新增模板，补足营销场景 |
| 风格/竖屏短视频 | 模板库第 6 项 | AC-01、AC-04 | `../docs/product/PRD-v4-intelligent-studio.md:573` | 9:16 构图提示 |

## 10. 功能设计卡（设计前六问）

### 卡 1：剧本风格模板库与注入

- **用户旅程位置**：入口在 Bee Studio 左栏「＋新建项目」→「✨ AI 生成剧本」弹窗（`web/bee-studio.html:1297`、`web/bee-studio.html:1503-1568`）；默认态 = 4 个硬编码风格；空态 = 模板接口失败时回退内置 4 项；加载态 = 与「检测中」同级显示模板加载中；失败态 = toast 提示并回退内置；成功态 = 选项与说明可见、改总时长后镜头数建议刷新；上一态 = 项目列表；下一态 = 分镜墙。
- **行业基线**：同类产品把风格做成可维护预设（模板 JSON/预设包），并把预设约束放进生成 prompt；证据 `../docs/product/feature-matrix.md:436`、`../docs/product/PRD-v4-intelligent-studio.md:573`。
- **我们的现状与差距**：现在 `web/bee-studio.html:1536-1541` 写死 4 项、`web/bee-studio.html:3148` 生成后才追加风格后缀、`api/router.py:1407-1424` 风格仅一行；缺模板库与结构化注入；用户感知 = 选风格像装饰，效果不显性。
- **决策点与后果**：模板放 `data/style_templates.json` 并由后端接口暴露 → 取「前后端同源」；代价 = 多一个接口与加载回退逻辑；风险 = 历史未知风格需容错。
- **不做什么**：不做风格的新建/编辑界面（超出本期范围），因为当前只需把硬编码变成可维护数据；不做 C3 新开发，因为已实现。
- **交付形态**：原型（弹窗 DOM 选项与说明）+ 冻结截图（路径 + sha1）；命令行 `curl` 与 `pytest` 证据。

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-11-27 | 初稿；范围收窄 B 段为 C2+镜头数建议，明确 C3 不重做 | A 段实测导出链路 PASS，避免重复造轮子 |
| 2026-11-27 | 追加 R-04/R-05（无 Key 提示真实化、导出缺失素材可见） | 同属「PRD-v4 智能工作台剩余界面缺口」，A 段实测暴露 |

## 12. 功能开发拆解（有开发任务时填）

| 切片 | 目录结构 | 文件归属 | 路由 / 入口 | 函数 / 模块 | 数据表 / 字段 | 验收方式 | 清理项 |
|---|---|---|---|---|---|---|---|
| 切片 A | `api/`、`data/` | `api/router.py`、`data/style_templates.json`、`tests/test_style_templates.py` | `GET /api/bee/style-templates` | `list_style_templates()`、`get_style_template()` | 不适用：无新表 | pytest + curl | 无 |
| 切片 B | `api/`、`web/` | `api/router.py`、`web/bee-studio.html` | `POST /api/llm/script-gen`、`#scriptStyle` | `build_style_prompt()`、`_llmGenerate` | `scripts.shots_json.style_id`（既有列） | pytest + 浏览器 DOM | 测试项目（如有） |
| 切片 C | `web/` | `web/bee-studio.html` | `#scriptDuration`/`#scriptStyle` change | `suggestShotCount()` | 不适用：无持久化 | pytest（纯函数导出）+ DOM | 无 |
| 切片 D | `web/` | `web/bee-studio.html` | generateVideo 无 Key 分支、`#videoAllBtn`、exportProject | toast 文案 / 缺失统计 | 不适用：无持久化 | 代码定位 + Playwright 导出实测 | 无 |
