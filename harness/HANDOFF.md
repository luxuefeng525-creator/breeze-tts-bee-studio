# 接手指南（引擎自动生成，勿手改）

- 生成时间：2026-11-27 07:37
- 需求：REQ-20261127-02
- 当前阶段：requirement
- 项目：f:\TTS2\breeze-tts

## 1. 现在做到哪
- 验收点：3/3 有结论与证据
- 未过：无
- 覆盖取值：未声明

## 2. 门禁最新结论
- G1: PASS
- G2: PASS
- G3: 未开始
- G4: 未开始
- 最近执行：G2 PASS（2026-11-27 07:37）

## 3. 阻塞
- 无

## 4. 下一步（来自 state.md）
- 1. 用户给出 G2 确认原文 → 写 state.md §4
- 2. G3 独立审查 → G4 发布材料 → 合流回 main

## 5. 接手三步
1. 读本文件与 `harness/requirements/REQ-20261127-02/state.md`
2. 跑 `node harness/engine/harness.mjs status REQ-20261127-02` 与 `probe` 确认通道
3. 按「下一步」做；门禁只认退出码，禁止凭聊天记忆续接
