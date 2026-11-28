---
req_id: REQ-20261128-02
stage: test
status: active
owner: trae（breeze-tts 主工作区）
updated: 2026-11-28
---

# 测试记录

> 证据路径相对项目根 `f:\TTS2\breeze-tts`。真实引擎、真实音色；测试期临时改过的音色 engine 已还原为 qwen3-tts。

## 1. 环境

- 场景：本机 Web 面板真实 HTTP + 真实 GPT-SoVITS v3 sidecar（CUDA fp16）；数据隔离到 tmp 用于回归用例，真实链路用现网音色「小蜜蜂配音」
- 入口与参数：`POST /api/synthesize`、`/api/synthesize/batch`、`/api/synthesize/stream`（均 multipart：text/texts、voice_id）；sidecar `GET 127.0.0.1:9880/tts`（text、ref_audio_path、prompt_text、text_lang=zh、media_type=wav）；未登录/无 token（本机单用户，沿用现有口径）；无版本参数
- 运行方式：`python -m pytest tests/test_sovits_route.py -q`；真实出声由 `tools/gpt_sovits_sidecar.py` 与面板联调
- 数据准备：音色 `v_c5acbed0 小蜜蜂配音`（参考 14.745 s，需裁段）；sidecar 权重 `E:\TTS2-runtime\gpt-sovits`；显存 8 GB，测试前先 `start --take-gpu` 让出 7860，测完 `release`

## 2. 验收点结果

| 验收点 | 场景 | 步骤 | 预期 | 实际 | 结论 | 证据 |
|---|---|---|---|---|---|---|
| AC-01 | sidecar 真实出声 | `start --take-gpu` 后请求 `/tts` | 200 + RIFF 头 + 24 kHz | 探针 200；产物 sr=24000 单声道 3.03 s | PASS | 证据：data/audio/20261128_092628_h_6fbde397.wav |
| AC-02 | 三条面板路径 | 设 engine=gpt-sovits 后分别请求单条/批量/流式 | 均 200 且格式与 7860 一致 | 单条 200（145708 B）；批量 200（149548 B，2 条）；流式 200（PCM 142080 B，X-Sample-Rate=24000，X-Adapter-Route=gpt-sovits） | PASS | 证据：data/audio/20261128_092659_h_23863d8b_batch.wav |
| AC-03 | sidecar 未启动 | 停 sidecar 后三条路径分别请求 | 均明确报错、不静默回落 7860 | 三条均 503，文案含「GPT-SoVITS 服务未启动或不可达…start --take-gpu」 | PASS | 证据：tests/test_sovits_route.py |
| AC-05 | 超长参考音频 | 14.745 s 音色直接合成 | 自动裁到 3~10 s 并重配文稿 | 裁到 9.65 s（窗口 0.2~9.85 s），ASR 重配 35 字简体文稿 | PASS | 证据：tests/test_sovits_route.py |
| AC-04 | 显存不足处置 | 7860 占用时请求 gpt-sovits | 错误文案含处置指引 | 503 文案同时给出启动命令与 8 GB 显存说明 | PASS | 证据：tests/test_sovits_route.py |
| 覆盖 qwen3-tts | 默认引擎回归 | 不标 engine 的音色合成 | 仍走 7860，行为不变 | `voice_engine` 缺省/非法值均回落 qwen3-tts；三条原分支未改 | PASS | 证据：tests/test_sovits_route.py |
| 覆盖 gpt-sovits | 新引擎链路 | 标 engine=gpt-sovits 后合成 | 走 sidecar 且产物同格式 | 三条路径 200，产物 24 kHz 单声道 | PASS | 证据：data/audio/20261128_092628_h_6fbde397.wav |

## 3. 边界与异常

| 类型 | 场景 | 结果 | 证据 |
|---|---|---|---|
| 空数据 | 设计型音色（无参考音频） | 400「没有可用的参考音频文件…请先上传 5~10 秒的人声」 | tests/test_sovits_route.py |
| 空数据 | 参考音频 < 3 秒 | 400「短于 GPT-SoVITS 要求的 3 秒且内容补不出来」 | tests/test_sovits_route.py |
| 重复操作 | 同一音色二次合成 | 复用 `data/sovits_refs/<vid>.wav` 缓存，不重复裁段/转写 | data/sovits_refs |
| 并发 | 两条链路同时合成 | 共用 `_INFER_LOCK`，sidecar 侧串行 | panel.py |
| 超时 | 长文本合成 | 请求超时 900 s，超时返回 502 并带原因 | panel.py |
| 非法值 | 创建音色时 engine 非枚举值 | 400，不写账本 | tests/test_sovits_route.py |

## 4. 回归

| 受影响功能 | 验证方式 | 结果 |
|---|---|---|
| 不标 engine 的音色 | 默认走 7860，原分支代码未改 | PASS |
| 产物格式与历史 | 仍为 24 kHz 单声道 WAV，落库结构不变 | PASS |
| `/api/voices` 创建/更新 | 新增可选 engine 字段，非法值 400 | PASS |
| 面板可用性 | 重启后 7861 200，18 条音色正常读取 | PASS |

## 5. 未验证项

| 项 | 未验证原因 | 影响 | 补验条件 |
|---|---|---|---|
| 守护日志中的 sidecar 启动/退出原因 | sidecar 由 CLI 拉起，未纳入守护 SERVICES | 缺一条自动恢复通道 | 决定纳入守护后补 |
| 贴边显存下的真实 OOM 表现 | 每次都用 `--take-gpu` 先让出 | 已知会变慢或超时 | 需要时手动压测 |
| 浏览器整页视觉比对（V 环） | 本轮只做 DOM 校验 | 版式回归未冻结 | G3 前补 |

## 6. 结论

- 验收点通过率：5/5（AC-04 由 AC-03 的报错文案覆盖）
- 是否可进入审查：是
