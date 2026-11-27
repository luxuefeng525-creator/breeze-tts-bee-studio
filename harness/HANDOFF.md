# 接手指南（引擎自动生成，勿手改）

- 生成时间：2026-11-28 06:12
- 需求：REQ-20261127-04
- 当前阶段：release
- 项目：f:\TTS2\breeze-tts

## 1. 现在做到哪
- 验收点：4/4 有结论与证据
- 未过：无
- 覆盖取值：未声明

## 2. 门禁最新结论
- G1: PASS
- G2: PASS
- G3: PASS
- G4: PASS
- V: PASS
- 最近执行：G4 PASS（2026-11-28 06:12）

## 3. 阻塞
- 无

## 4. 下一步（来自 state.md）
- 1. 打开 `harness/requirements/REQ-20261127-04/state.md` §4，由雪峰给出 G3 确认原文
- 2. 运行 `node harness/engine/harness.mjs gate REQ-20261127-04 --stage G3 --dir f:\TTS2\breeze-tts`，机检全 PASS
- 3. 提交本批改动并按并发纪律合并 main，回填 04-release.md §6 结论

## 5. 接手三步
1. 读本文件与 `harness/requirements/REQ-20261127-04/state.md`
2. 跑 `node harness/engine/harness.mjs status REQ-20261127-04` 与 `probe` 确认通道
3. 按「下一步」做；门禁只认退出码，禁止凭聊天记忆续接
