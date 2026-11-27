---
req_id: REQ-20261127-04
stage: release
status: active
owner: agent（breeze-tts 主工作区）
updated: 2026-11-28
---

# 发布与回退

## 1. 发布内容

| 项 | 内容 |
|---|---|
| 变更摘要 | 上传参考音频统一转码：非 RIFF/WAVE 用 soundfile 解码并重采样为 24 kHz / 单声道 / PCM16 WAV 后落盘；前端选择器只放行 `.wav`。坏绑定（角色指向已删除音色）在单条/批量/流式三条合成路径统一返回明确 400。账本写盘新增带时间戳的历史留档（每文件保留 20 份）。 |
| 涉及模块 | `panel.py`（`/api/voices/{id}/ref`、三条 `/api/synthesize*` 的绑定校验、`_save` 历史留档）、`web/index.html`（`#refFile` accept）、`web/app.js`（`uploadRef` 前置校验）；新增 `tests/test_voice_ref_upload.py`、`tests/test_role_binding_guard.py` |
| 配置变更 | 无（端口 7860/7861 沿用台账，未改） |
| 数据变更 | `data/voices.json` 的 `ref_audio`/`ref_duration` 指向转换后的 wav；`data/refs/` 新增 wav。另：现网清理 11 条垃圾悬空角色（`roles.json` 70→59），恢复 8 条被误删音色（`voices.json` 11→19→18）。改前均已备份到 `data/*.before_*` 与 `logs/` |

## 2. 发布前检查

- [x] G3 验证门禁 PASS
- [x] 回退步骤已演练或已核对
- [x] 监控点已明确
- [x] 发布窗口已确认

## 3. 发布步骤

```text
1. 提交本批改动（panel.py / web/index.html / web/app.js / tests/ / data/ 备份与恢复结果），
   提交信息：feat(音色): 参考音频统一转 WAV 并在上传处限制 .wav
2. 重启面板：杀掉 7861 上的 panel.py 进程，守护 TTS2-Studio-Guard 会在 1 分钟内自动拉起
   （或手动 .venv\Scripts\python.exe -u panel.py）
3. 验证点：GET /api/voices 200；新建克隆音色上传 mp3 → 落盘 refs/*.wav 且合成 200；
   上传 0 字节 → 400 且 refs/ 无新文件
4. 异常处理：若面板起不来，回退代码重启即可；数据侧用 data/*.before_* 覆盖回滚
```

## 4. 回退

- 触发条件：上传后端点 500/超时，或转码导致面板内存异常，或合成链路因本次改动回归失败
- 回退步骤：

```text
1. git revert 本批提交（或 git checkout 到 be61b18^ 的 panel.py / web/* ）
2. 重启面板，确认 /api/voices 200
3. 数据回滚（仅在需要时）：用 data/voices.json.before_restore_* 覆盖 data/voices.json
```

- 数据回滚：`data/voices.json` 与 `data/roles.json` 均有改前快照；`_ledger_history/` 保留最近 20 个历史版本，可直接取任一副本覆盖回去

## 5. 发布后监控

| 观察项 | 正常范围 | 观察时长 | 异常动作 |
|---|---|---|---|
| 面板 7861 `/api/voices` | 200 | 24 h | 守护日志 `ops/logs/guard.log` 有重启记录则人工介入 |
| 上传端点错误率 | 无 500 | 3 天 | 出现 500 立即回退本批改动 |
| `_ledger_history/<name>/` 文件数 | ≤ 20 | 每次写盘 | 超过 20 说明裁剪失效，检查 `_HIST_KEEP` |
| 引擎 7860 `/health` | 200 | 24 h | 由守护自动拉起，连续失败查 `ops/logs/qwen-api.log` |

## 6. 验收

- 验收人：雪峰
- 验收时间：2026-11-28
- 结论：通过。功能与数据修复经真实环境复验；V 视觉比对 7 项全 PASS（参考已冻结、三视口截图齐全）；G3/G4/V 门禁全 PASS，确认原文「确认，通过，合并」
