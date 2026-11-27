---
req_id: REQ-20261127-03
stage: release
status: active
owner: trae
updated: 2026-11-27
---

# 发布与回退

## 1. 发布内容

| 项 | 内容 |
|---|---|
| 变更摘要 | Bee Studio 前端把 LLM 降级/失败原因显示出来：script-gen 降级弹 warn（含原因 + 自定义模型下一步提示），script-edit 失败带自定义模型上下文与 `raw_preview`；toast 限宽换行 |
| 涉及模块 | `web/bee-studio.html`（apiBT / `_llmGenerate` / 生成按钮处理 / `submitScriptEdit` / `truncateReason` / `.toast`）、`tests/test_bee_studio_notice.py`（新增） |
| 配置变更 | 无（不改端口、不改环境变量） |
| 数据变更 | 无（只读既有响应字段，不写库、不建表） |

## 2. 发布前检查

- [x] G3 验证门禁 PASS（2026-11-27 18:24 人确认）
- [x] V 视觉门禁 PASS（2026-11-27 18:31 人确认）
- [x] 全量静态守卫 12 passed；浏览器 AC-01/02 复验 PASS
- [ ] 回退步骤已演练或已核对
- [ ] 监控点已明确
- [ ] 发布窗口已确认

## 3. 发布步骤

```text
1. node harness/scripts/git-atomic.mjs merge session/trae-20261127 REQ-20261127-03 --dir f:\TTS2\breeze-tts-trae   （取合流命令）
2. 在主工作区 f:\TTS2\breeze-tts 执行合流；git status 确认只碰 web/bee-studio.html、tests/、harness/requirements/REQ-20261127-03/
3. 重启面板 7861（ops/run-api.bat 或等效方式），让它加载合流后的 web/bee-studio.html
4. 验证点：浏览器打开面板 →「1 写剧本」→ 选一个不可达的自定义模型生成 → 出现 warn 且含原因，无 success
5. 异常处理：若前端未更新，先确认面板进程 cwd/ROOT 是否指向主工作区；不要在未合流的情况下改主工作区文件
```

## 4. 回退

- 触发条件：合流后生成/改镜头链路报错，或降级提示导致界面异常（如 toast 撑破布局）
- 回退步骤：

```text
1. git revert 本次合并对应的 commit（在主工作区 f:\TTS2\breeze-tts）
2. 重启面板 7861
3. 验证点：生成流程恢复原行为，降级不再弹 warn
```

- 数据回滚：无（本需求不写库；探针注册表/Key 已在测试后还原为 `[]` / 备份版本）

## 5. 发布后监控

| 观察项 | 正常范围 | 观察时长 | 异常动作 |
|---|---|---|---|
| 生成/改镜头链路 | 成功路径行为不变；降级才弹 warn | 发布后首次使用 | 出现误报 warn 或成功提示丢失 → 按第 4 节回退 |
| 面板启动 | 7861 正常监听，页面可打开 | 重启后即时 | 起不来则检查合流冲突 |
| toast 视觉 | 长原因换行不溢出视口（≤560px） | 人工看一次长原因场景 | 溢出则调 `.toast` max-width |

## 6. 验收

- 验收人：用户
- 验收时间：2026-11-27
- 结论：通过（G4: PASS | 确认人: 用户 | 时间: 2026-11-27 18:34 | 确认原文: "G4 通过（推荐）"）
