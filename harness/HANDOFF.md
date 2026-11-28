# 接手指南（引擎自动生成，勿手改）

- 生成时间：2026-11-28 18:46
- 需求：REQ-20261128-02
- 当前阶段：done
- 项目：F:\TTS2\breeze-tts

## 1. 现在做到哪
- 验收点：6/6 有结论与证据
- 未过：无
- 覆盖取值：引擎/qwen3-tts = PASS；引擎/gpt-sovits = PASS

## 2. 门禁最新结论
- G1: PASS
- G2: PASS
- G3: PASS
- V: PASS
- G4: PASS
- 最近执行：G4 PASS（2026-11-28 10:29）

## 3. 阻塞
- | sidecar 与 7860 显存互斥（本机 8 GB，实测仅剩约 0.6 GB） | 需定「按需启动 + 合成前置检查」口径 | 雪峰 | 2026-11-28 |

## 4. 下一步（来自 state.md）
- 1. 跑 `node harness/engine/harness.mjs gate REQ-20261128-01 --stage G1 --dir f:\TTS2\breeze-tts`，清 FAIL 项
- 2. 由雪峰给出 G1 确认原文（含 §8 未决问题里的版本选型与切换等待口径）
- 3. 进入开发：切片 A（sidecar 启停/探针）→ B（面板路由）→ C（前端选择项）

## 5. 接手三步
1. 读本文件与 `harness/requirements/REQ-20261128-02/state.md`
2. 跑 `node harness/engine/harness.mjs status REQ-20261128-02` 与 `probe` 确认通道
3. 按「下一步」做；门禁只认退出码，禁止凭聊天记忆续接
