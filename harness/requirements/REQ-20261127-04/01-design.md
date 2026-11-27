# 设计

## 1. 方案

- **切片 A（后端兜底，必做）**：`POST /api/voices/{id}/ref` 收到文件后，先按内容判格式；非 RIFF/WAVE 时用项目已有的 soundfile 链路解码并重采样为 24 kHz / 单声道 / PCM16 WAV，落盘为 `refs/<vid>_<ts>.wav`，`voices.json` 的 `ref_audio`/`ref_duration` 指向新文件。解码失败或体积/时长超限返回明确 400/413，不写文件、不动索引。
- **切片 B（前端约束）**：`web/index.html` 的 `#refFile` 由 `accept="audio/*"` 改为 `accept=".wav,audio/wav"`；改名伪装由切片 A 兜住。
- **否决的方案**：① 引入 ffmpeg 做转码——项目已有 soundfile（libsndfile）可解 mp3/flac/ogg/m4a，无需新增依赖；② 只在面板提示「请用 wav」而不转码——用户仍会传错，且现网已证明后果是全部合成失败。

## 2. 影响面

涉及 `panel.py` 的 `/api/voices/{id}/ref` 端点、`web/index.html` 的文件选择控件、复用 `tts_pipeline/ref_prep.py` 的解码/写 WAV 函数；数据侧只改 `data/voices.json` 的 `ref_audio`/`ref_duration` 与 `data/refs/` 下新增 wav，可逆（改前备份）。不影响素材库 `/api/assets`、角色参考图与合成链路其它端点。

## 3. 关键实现点

- 格式判定看**内容头**而非扩展名：RIFF/WAVE 直接落盘；否则尝试解码，解码失败报 400。
- 统一输出参数：`-ar 24000 -ac 1 -c:a pcm_s16le`（与引擎 `sample_rate=24000` 一致）。
- 上限：文件 ≥50 MB 或解码后时长 ≥600 s 直接 400/413，避免长时间占用请求。
- 重复上传：覆盖索引并把旧文件删掉，避免 `refs/` 堆积孤儿文件。
- 质检（`quality` 字段）沿用现有逻辑，不受影响。

## 4. 测试策略

- HTTP 用例（`tests/test_voice_ref_upload.py`）：mp3 上传 → 落盘 wav + 索引更新 + 合成 200；空文件 → 400 且无副作用；伪装文件 → 400；超大文件 → 明确错误。
- 回归：`pytest tests -q` 既有套件保持绿。
- 手工复核：在面板实际走一遍「新建克隆音色 → 上传 mp3 → 保存 → 合成」。

## 5. 未决问题

- m4a/aac 是否被本机 libsndfile 支持需在测试中确认；若不支持，按「明确报错」处理而非静默失败。
