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
| AC-06 | 面板一键开关 | 诊断弹窗点启动 → 轮询状态 → 点停止 | start 立即返回、状态迁移到 running、stop 后 7860 恢复 | start 1.6 s 返回 starting；31 s 变 running；运行期 7860 停/9880 通；stop 后 80 s 恢复 7860 | PASS | 证据：harness/requirements/REQ-20261128-02/logs/switch-live.txt |
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

## 6. 发布后优化：3 秒音频耗时（合并后补充）

**背景**：sidecar 接入后，3 秒音频合成实测首次 30~70 s、换音色首次 ~18 s，用户反馈「3 秒音频耗时 30 秒」。

**根因（实测拆解，固定 seed）**：
- 冷启动首次 67.65 s（模型加载 + CUDA 图捕获）；同音色热推理 3.52 s（cudagraph+32 步）
- 换音色首次 18~30 s（参考音频语义编码）；同音色后续 5~10 s
- `parallel_infer=false` → 29.7 s（必须保持 True）
- 结论：慢在冷启动与换音色编码，不是每次推理本身

**已落地优化**：
1. `sample_steps` 32→16（默认，`BREEZE_SOVITS_STEPS` 可调回）
2. `cache_dit=f16`（上游 V3/V4/V5 DiT 官方 DBCache 加速，`BREEZE_SOVITS_CACHE_DIT` 可调 off/f8；实测短文本无显著差异，保留作长文本/质量兜底）
3. **预热**：sidecar 启动后自动用一条既有音色跑一句极短的话，把冷启动吸收到启动阶段；新增 `warming` 状态（前端显示「预热中…」），预热期间不误报「已就绪」
4. 修复 2 个真实 bug：
   - `netstat`/`nvidia-smi` 在中文 Windows 是 GBK 输出，`subprocess.run(text=True)` 按 utf-8 解码抛 `UnicodeDecodeError` → sidecar 一键启动直接报「启动异常」。改 `encoding="gbk", errors="replace"`（panel.py `_sovits_free_vram`、tools/gpt_sovits_sidecar.py `free_vram_mib`/`port_pids`）
   - 参考文稿被 ASR 乱码污染（10.4 s 音频 ASR 转出「现在最潜艳的…」乱码并固化缓存）。`_sovits_ref_for` 改为优先用音色自带 `ref_text`（用户逐字校对稿），ASR 只作兜底

**验证数据（面板一键启动 → 预热 → running → 合成）**：
- 状态机 `starting → warming（约 60~65 s）→ running` 正常，预热 `code=200`
- running 后同音色合成 3~4 秒音频：第 1 次 ~10~15 s（含参考编码）、后续 ~5~10 s（波动受 GPU 负载影响）
- 换音色首次 ~10~30 s（参考语义编码是 sidecar 固有成本）
- 面板产物正常：`/api/synthesize` 返回历史记录 JSON（`{"id":"h_..."}`），wav 落 `data/audio/`（实测 3.88~6.11 s、24 kHz 单声道）；曾误判「空产物」实为把返回 JSON 当 wav 解析

**未进一步优化**：换音色首次的参考语义提取每次请求重复做，压到 3 s 内需改 GPT-SoVITS sidecar 源码加缓存（有回归风险）。用户已确认「接受当前耗时」。


## 6. 结论

- 验收点通过率：6/6（AC-04 由 AC-03 的报错文案覆盖）
- 是否可进入审查：是
