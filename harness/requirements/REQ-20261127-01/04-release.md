---
req_id: REQ-20261127-01
stage: release
status: active
owner: trae
updated: 2026-11-27
---

# 发布与回退

## 1. 发布内容

| 项 | 内容 |
|---|---|
| 变更摘要 | C2 剧本风格模板库（6 套）+ 风格注入 LLM prompt + 建议镜头数；无 Key 出片提示真实化；导出前缺失素材提示 |
| 涉及模块 | `api/style_templates.py`、`data/style_templates.json`、`api/router.py`、`web/bee-studio.html`、`tests/test_style_templates.py` |
| 配置变更 | 无（模板为数据文件，随仓库）；临时重启过面板使新代码生效 |
| 数据变更 | 新增 `data/style_templates.json`（可逆：删除即回退内置 6 项）；不新增数据库表 |

## 2. 发布前检查

- [x] G3 验证门禁 PASS
- [x] 回退步骤已核对（无迁移、无新表）
- [x] 监控点已明确（见 §5）
- [ ] 发布窗口已确认（等用户指令）

## 3. 发布步骤

```text
1. git -C F:\TTS2\breeze-tts add api/style_templates.py data/style_templates.json api/router.py web/bee-studio.html tests/test_style_templates.py harness/requirements/REQ-20261127-01
2. git commit（feat(bee-studio): 剧本风格模板库与导出提示）
3. node harness/scripts/git-atomic.mjs merge session/trae-20261127 REQ-20261127-01（回 main）
4. 重启面板（ops\run-panel.bat），curl http://127.0.0.1:7861/api/bee/style-templates 应返回 6 项
5. 浏览器打开 bee-studio.html → 新建项目 → 选风格/改时长，确认选项与建议镜头数生效
6. 异常：接口 500 或弹窗空 → 停止 merge，按 §4 回退
```

## 4. 回退

- 触发条件：合并后 `/api/bee/style-templates` 非 200、新建项目弹窗空白、全量测试出现 FAIL。- 回退步骤：

```text
1. git -C F:\TTS2\breeze-tts revert 合并产生的 merge commit（或未推送时 git reset --hard 到合流前提交）
2. 删除 data/style_templates.json 与 api/style_templates.py，还原 router.py / bee-studio.html 到 main
3. 重启面板并复跑 .venv\Scripts\python.exe -m pytest tests/test_style_templates.py -q 以外的全量
```

- 数据回滚：无数据库迁移；仅需删除本需求在 scripts.shots_json 里写入的 style_id 键（老数据不含它，可保留不影响）。

## 5. 发布后监控

| 观察项 | 正常范围 | 观察时长 | 异常动作 |
|---|---|---|---|
| GET /api/bee/style-templates | 200，templates=6 | 24h | 检查 data/style_templates.json 是否被误改/损坏（自动回退内置） |
| 面板健康 /api/model | 200 | 24h | 重启面板；仍失败按 §4 |
| 新建项目→生成 mock | 生成成功且 shots 带 style_id | 24h | 查看 panel-api.log |

## 6. 验收

- 验收人：用户
- 验收时间：2026-11-27 06:21
- 结论：通过（V/G4 用户确认原文「V 通过 + G4 通过（推荐）」）
