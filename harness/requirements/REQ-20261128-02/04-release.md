---
req_id: REQ-20261128-02
stage: release
status: active
owner: trae（breeze-tts 主工作区）
updated: 2026-11-28
---

# 发布与回退

## 1. 发布内容

| 项 | 内容 |
|---|---|
| 变更摘要 | 面板新增可选第二合成引擎 GPT-SoVITS v3（sidecar）：音色可指定引擎，三条合成路径按 engine 路由；参考音频自动规范到 3~10 秒并重配文稿；诊断弹窗提供一键启停与状态显示 |
| 涉及模块 | `panel.py`（sidecar 客户端、三条路径路由、一键开关、engine 校验）、`web/index.html`、`web/app.js`、新增 `tools/sovits_ref.py`、`tools/gpt_sovits_sidecar.py`、`tests/test_sovits_route.py`；本机另装 `E:\TTS2-runtime\gpt-sovits`（仓库 + venv + v3 权重） |
| 配置变更 | 无。端口台账不变（7860/7861），sidecar 用 9880（新增，仅 127.0.0.1） |
| 数据变更 | `data/voices.json` 新增可选字段 `engine`（缺省视为 qwen3-tts）；`data/sovits_refs/` 存裁段缓存。均可逆，删除即回退 |

## 2. 发布前检查

- [x] G3 验证门禁 PASS
- [x] 回退步骤已演练或已核对
- [x] 监控点已明确
- [x] 发布窗口已确认

## 3. 发布步骤

```text
1. 合并分支到 main：git checkout main && git merge --no-ff session/sovits-20261128
2. 重启面板：杀掉 7861 上的 panel.py 进程，守护 TTS2-Studio-Guard 会在 1 分钟内自动拉起
3. 验证点 A：GET /api/voices 返回 200，音色条数不变（18）
4. 验证点 B：GET /api/engine/sovits 返回 {state:"idle", running:false}
5. 验证点 C：不标 engine 的音色合成仍走 7860（合成一条 200 即通过）
6. 异常处理：若面板起不来，回退代码后重启；数据无需处理（新字段不影响旧逻辑）
```

## 4. 回退

- 触发条件：面板合成出现新的 5xx/超时；或 sidecar 启动后 7860 无法恢复；或引擎路由把不标 engine 的音色也导去 9880
- 回退步骤：

```text
1. 停止 sidecar 并清除显存占用标记：
   .venv\Scripts\python.exe tools\gpt_sovits_sidecar.py release
2. 回退代码：git revert 本批 merge commit，然后重启面板
3. 验证点：GET /api/voices 200；不标 engine 的音色合成 200（走 7860）
```

- 数据回滚：`engine` 字段可整体删除（`data/voices.json` 改前备份在 `data/*.bak` 与 `data/_ledger_history/voices/`）；`data/sovits_refs/` 可直接删除，不影响任何既有功能

## 5. 发布后监控

| 观察项 | 正常范围 | 观察时长 | 异常动作 |
|---|---|---|---|
| 面板 7861 `/api/voices` | 200 | 24 h | 守护日志有异常重启则人工介入 |
| 7860 / 9880 互斥 | 同时只有一个在跑 | 每次切换 | 两个同时监听说明让出失败，立即 `release` |
| `GET /api/engine/sovits` 状态 | idle 或 running，不停在 starting | 每次使用 | 停在 starting 超过 3 分钟查 `F:\TTS2\logs\gpt-sovits-api.log` |
| 合成失败率 | 无新增 5xx | 3 天 | 出现即回退本批改动 |
| 显存 | 9880 运行期 7860 应为停止 | 每次切换 | 见第一行 |

## 6. 验收

- 验收人：雪峰
- 验收时间：2026-11-28
- 结论：通过。6 个验收点全部 PASS（真实引擎、真实音色、三条路径）；V 视觉 2 个取值 × 3 视口 6 张全 PASS；审查 6 项已修 2、接受 4；G3/V/G4 确认原文均为「确认，通过，合并」
