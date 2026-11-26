# 接手指南（引擎自动生成，勿手改）

- 生成时间：2026-11-27 06:56
- 需求：REQ-20261127-01
- 当前阶段：release
- 项目：F:\TTS2\breeze-tts

## 1. 现在做到哪
- 验收点：6/6 有结论与证据
- 未过：无
- 覆盖取值：风格/电影感 = PASS；风格/赛博朋克 = PASS；风格/日系动画 = PASS；风格/写实纪录片 = PASS；风格/产品广告 = PASS；风格/竖屏短视频 = PASS

## 2. 门禁最新结论
- G1: PASS
- G2: PASS
- G3: PASS
- G4: PASS
- V: PASS
- 最近执行：G4 PASS（2026-11-27 06:56）

## 3. 阻塞
- 无

## 4. 下一步（来自 state.md）
- 1. 用户给出 G2 确认原文
- 2. 跑 `node harness/engine/harness.mjs gate REQ-20261127-01 --stage G2`
- 3. G3 独立审查 → V 视觉门禁 → G4

## 5. 接手三步
1. 读本文件与 `harness/requirements/REQ-20261127-01/state.md`
2. 跑 `node harness/engine/harness.mjs status REQ-20261127-01` 与 `probe` 确认通道
3. 按「下一步」做；门禁只认退出码，禁止凭聊天记忆续接
