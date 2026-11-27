# 接手指南（引擎自动生成，勿手改）

- 生成时间：2026-11-28 05:53
- 需求：REQ-20261128-01
- 当前阶段：test
- 项目：f:\TTS2\breeze-tts-trae

## 1. 现在做到哪
- 验收点：3/3 有结论与证据
- 未过：无
- 覆盖取值：未声明

## 2. 门禁最新结论
- G1: PASS
- G2: 未开始
- G3: 未开始
- G4: 未开始
- 最近执行：G2 FAIL（2026-11-28 05:53）

## 3. 阻塞
- 无

## 4. 下一步（来自 state.md）
- 1. 向用户索取 G2 确认原文
- 2. 写入 `state.md` 门禁块后跑 `gate REQ-20261128-01 --stage G2 --dir f:\TTS2\breeze-tts-trae`
- 3. G2 通过后做 G3 独立只读审查与 G4 发布收口
- 4. 合流 main、重启面板 7861 使新后端生效

## 5. 接手三步
1. 读本文件与 `harness/requirements/REQ-20261128-01/state.md`
2. 跑 `node harness/engine/harness.mjs status REQ-20261128-01` 与 `probe` 确认通道
3. 按「下一步」做；门禁只认退出码，禁止凭聊天记忆续接
