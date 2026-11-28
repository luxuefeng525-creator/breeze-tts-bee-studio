---
req_id: REQ-20261128-02
stage: requirement
status: active
owner: trae（breeze-tts 主工作区）
updated: 2026-11-28
size: L
entry: /harness init
hits: []
---

# 状态

## 1. 当前进度

- [x] 立项：背景、需求条目、验收点、代码证据、边界、澄清清单、同行对标、覆盖矩阵、功能设计卡
- [x] 方案初稿：面板侧路由 + sidecar 纯推理（`01-design.md`）
- [x] 切片 A：本机装好 GPT-SoVITS v3（E:\TTS2-runtime\gpt-sovits），sidecar 启停/探针/按需让出显存跑通
- [x] AC-05 参考音频规范化：>10 秒自动裁到语音最密集片段 + ASR 重配文稿（繁转简）+ 按音色缓存
- [x] AC-01 端到端：两条真实音色（小蜜蜂配音 14.7s、雪峰原声 10.4s）均合成 200，产物 24 kHz / 单声道
- [x] 切片 B：面板三条合成路径按 engine 路由（单条/批量/流式全部 PASS）
- [x] 切片 C：前端音色编辑处加引擎选择（克隆页 + 编辑弹窗，设计型禁选 gpt-sovits）
- [x] AC-03：sidecar 未启动时三条路径均 503 且文案含处置指引
- [x] AC-06：面板「设置与诊断」一键启停 sidecar（start 1.6s 返回、31s 就绪、stop 后 80s 恢复 7860）
- [ ] G2 人确认
- [ ] 审查记录 / 发布材料 / V 环

## 2. 阻塞

| 阻塞项 | 等什么 | 谁能解 | 记录时间 |
|---|---|---|---|
| sidecar 与 7860 显存互斥（本机 8 GB，实测仅剩约 0.6 GB） | 需定「按需启动 + 合成前置检查」口径 | 雪峰 | 2026-11-28 |

## 3. 已验证

| 项 | 方法 | 结果 | 证据 |
|---|---|---|---|
| 现网显存基线 | `nvidia-smi`（总 8151 MiB / 已用约 7290 / 空闲约 606） | 记录 | 命令输出（2026-11-28 06:19） |
| 7860/7861 在线 | 端口探测 | LISTEN | 2026-11-28 |
| GPT-SoVITS v3 可运行 | 克隆仓库 + 独立 venv + v3 权重（约 1.9 GB） | 就绪 | `E:\TTS2-runtime\gpt-sovits`，探针 200 |
| AC-05 裁段 | 14.745s → 9.65s（窗口 0.2~9.85s）；10.397s → 9.3s | PASS | `tools/sovits_ref.py probe`；缓存 `data/sovits_refs/` |
| AC-05 文稿重配 | 裁段后用面板同一 ASR 转写并繁转简 | PASS | 「你要相信你会越来越好…」35 字；`opencc t2s` |
| AC-01 端到端合成 | `tts --voice 小蜜蜂配音 / 雪峰原声` | PASS | `data/audio/_engine_demo/*.wav`，24 kHz 单声道，9.18s / 6.54s |
| A 方案显存让出/恢复 | `start --take-gpu` → 停 7860 → `release` → 守护 80s 拉起 | PASS | 08:35 停、08:40 恢复；`ops/logs/gpu-hold.json` |

## 4. 门禁记录

```text
G1: PASS | 确认人: 雪峰 | 时间: 2026-11-28 06:40 | 确认原文: "确认，按方案开工，用 v3，显存按 A 方案"
```

追加记录（不覆盖历史）：

| 门禁 | 结论 | 触发动作 | 证据路径 |
|---|---|---|---|
| G1 | 待确认 | init | — |

## 5. 未验证项

| 项 | 原因 | 影响 | 补验条件 |
|---|---|---|---|
| GPT-SoVITS 在本机 8 GB 下的实际显存占用 | 尚未安装部署 | 决定能否与 7860 共存 | 装好 sidecar 后实测 |

## 6. 下一步

1. 跑 `node harness/engine/harness.mjs gate REQ-20261128-01 --stage G1 --dir f:\TTS2\breeze-tts`，清 FAIL 项
2. 由雪峰给出 G1 确认原文（含 §8 未决问题里的版本选型与切换等待口径）
3. 进入开发：切片 A（sidecar 启停/探针）→ B（面板路由）→ C（前端选择项）

## 7. 交接摘要（可选）

- 上次执行到：需求包初稿完成，等 G1 机检 + 人确认
- 关键判断：上游写死 7860（`panel.py:80`）；`data/voices.json` 无 engine 字段；本机 8 GB 必须做显存互斥
- 接手注意：本包在 `breeze-tts/harness/requirements/` 下，gate 命令须带 `--dir f:\TTS2\breeze-tts`

## 8. 清理记录

| 路径 | 级别 | 依据 | 动作 | 时间 | 确认人 |
|---|---|---|---|---|---|
| 无 | — | 尚未产生清理项 | — | — | — |

## 9. 自检记录

| 步骤 | 命令 | 退出码 | 证据路径 |
|---|---|---|---|
| 1 | `node harness/engine/harness.mjs begin "接入 GPT-SoVITS 作为 sidecar 服务"` | 0 | — |
