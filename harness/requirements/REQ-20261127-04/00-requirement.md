---
req_id: REQ-20261127-04
stage: requirement
status: active
size: L1
owner: agent（breeze-tts 主工作区）
updated: 2026-11-27
acceptance: [AC-01, AC-02, AC-03, AC-04]
---

# 需求

> 档位 L1：填 §1 §2 §3 §4 §6 §7；§9/§10 不适用。

## 1. 背景与目标

- 触发场景：用户在 BreezeTTS 面板给克隆音色上传参考音频时选了 MP3（现网案例：2026-11-27 18:24 新建的「雪峰原声」`v_62d4e151` 上传的就是 mp3），面板提示上传成功，但之后每次用它合成，上游引擎都回 `400 ref_audio must be a RIFF/WAVE file`，面板汇总成「所有配音都没生成成功」。
- 期望结果：上传的参考音频一律以标准 WAV 落盘并被索引；前端选择器只放 wav 进来；合成不再因格式被拒。
- 验证通道：HTTP 接口用例（退出码 + 响应体 + 落盘文件探测）+ 面板合成复测；前端用浏览器选择器行为核对。
- 不做项：不做自动裁剪/智能选窗（保持参考音频内容不变，仅转格式与采样率）；不做云端转码；不改动其它音频上传口（素材库 `/api/assets`、角色参考图上传）。

## 2. 需求条目

### R-01

当用户上传的参考音频不是 RIFF/WAVE（如 mp3/flac/m4a/ogg/webm）时，系统应自动转码成 24 kHz、单声道、PCM16 的 WAV 后再落盘并写入 `voices.json`，以便合成链路不再因格式被拒。

- 验收点 AC-01：对一个新克隆音色 POST `/api/voices/{id}/ref` 上传一个真实 mp3，返回 200；`data/refs/` 下出现 `.wav` 文件且头部为 `RIFF....WAVE`；`voices.json` 中该音色的 `ref_audio` 指向该 wav、`ref_duration > 0`；随后 `/api/synthesize/stream` 用该音色合成返回 200。
- 验收点 AC-02：上传内容为空、或扩展名是音频但内容不可解码（伪装文件）时，接口返回明确 400 且不写入 `refs/`、不更新 `voices.json`。
- 验收点 AC-03：上传体积/时长明显超标的文件（≥50 MB 或 ≥600 s）时，接口在超时前给出明确 400/413 错误，不挂起、不落半成品。
- 边界：空文件（0 字节）必须直接拒绝；同名/重复上传（同一音色连续传两次）不得留下孤立文件；转码过程要有超时，避免大文件把请求拖死。
- 同行对标：ffmpeg 官方格式文档（https://ffmpeg.org/ffmpeg-formats.html ）——容器/编码互转是通用能力，参考音频统一成 WAV 是行业通行做法（剪映/ElevenLabs 均接受多格式上传后自行归一化）；取舍：本项目已有 soundfile 解码链，优先复用，不额外引入 ffmpeg 依赖。

### R-02

当用户在选择参考音频文件时，前端文件选择器应只接受 `.wav`，非 wav 不给选择并提示改用 WAV。

- 验收点 AC-04：`web/index.html` 的 `#refFile` 控件 `accept` 不含 `audio/*`，只放行 `.wav`；浏览器中选择器默认过滤为 wav。
- 边界：用户改名伪装（把 mp3 改名 .wav）时由后端 R-01 的校验兜住（前端不做二进制判断，避免重复实现）。
- 同行对标：MDN 的 HTML file 控件文档（https://developer.mozilla.org/en-US/docs/Web/HTML/Element/input/file ）——accept 只是过滤，可被绕过；取舍：前端限制减少误传，后端校验兜底防绕过，两层都要。

## 3. 验收点汇总

| 编号 | 对应需求 | 判定方式 | 优先级 |
|---|---|---|---|
| AC-01 | R-01 | HTTP 用例：mp3 上传后查 `refs/` 文件头 + `voices.json` + 合成 200 | P0 |
| AC-02 | R-01 | HTTP 用例：空文件/伪装文件 → 400，且无新文件、无索引变更 | P0 |
| AC-03 | R-01 | HTTP 用例：构造 ≥50MB 文件 → 明确错误且不挂起 | P1 |
| AC-04 | R-02 | 静态核对 + 浏览器选择器过滤行为 | P1 |

## 4. 代码证据与边界反例

| 类型 | 证据 | 说明 |
|---|---|---|
| 现有实现定位 | `panel.py:2628-2666` | `/api/voices/{id}/ref`：按扩展名白名单收文件后**原样落盘** `(REFS/safe).write_bytes(data)`，`wav_duration()` 对 mp3 返回 -1 |
| 格式要求来源 | `tests/test_soundtrack_api.py:298` | 既有测试记录了「ref_audio 与 ref_text 必须成对」的拒绝口径；上游适配器只接受 RIFF/WAVE（现网故障的直接原因） |
| 可复用转码能力 | `tts_pipeline/ref_prep.py:516-538` | `prepare_wav_bytes` 用 soundfile 解码、`write_wav_bytes` 输出 PCM16 WAV，无需外部 ffmpeg |
| 调用方 / 数据来源 | `web/app.js:1548-1561` | 前端保存音色后把 `state.refFile` 发给 `/api/voices/{id}/ref` |
| 现网坏案例 | `data/voices.json:179-186` | `v_62d4e151`（雪峰原声）`ref_audio` 为 `.mp3`、`ref_duration = -1` |
| 边界 / 反例 1 | 空文件（0 字节）上传 | 应 400 拒绝，不写文件 |
| 边界 / 反例 2 | 同一音色连续上传两次不同文件 | 第二次应覆盖索引，且不得残留两份引用不到的孤儿文件 |
| 边界 / 反例 3 | 转码大文件导致请求过久 | 需有上限与超时，不能无限等待 |

- 定位均来自实际打开的文件。
- 对标：ffmpeg 官方格式/滤镜文档（https://ffmpeg.org/ffmpeg-formats.html ）说明容器与编码可互相转；MDN `<input type="file">`（https://developer.mozilla.org/en-US/docs/Web/HTML/Element/input/file ）说明 `accept` 只是过滤器、可被绕过，故后端必须兜底校验。

## 5. 影响面

- 涉及模块：`panel.py`（上传端点）、`web/index.html`（文件选择 accept）、`tts_pipeline/ref_prep.py`（复用转码函数）；新增用例 `tests/test_voice_ref_upload.py`
- 调用方：`web/app.js:1548-1561`（音色编辑弹窗保存后上传参考音频）
- 涉及接口：`POST /api/voices/{id}/ref`（面板内），上游引擎 `POST /v1/audio/speech` 的 `ref_audio` 入参格式不变
- 数据变更：`data/voices.json` 的 `ref_audio`/`ref_duration` 改为指向转换后的 wav；`data/refs/` 新增 wav。可逆（旧文件保留，改前备份见 logs）

## 6. 人确认

```text
G1: 待确认 | 确认人: — | 时间: — | 确认原文: "<TBD>"
```

## 7. 澄清清单

| 类别 | 结论 |
|---|---|
| 触发与时序 | 仅在「克隆/自定义音色上传参考音频」时触发；保存音色本身（`/api/voices/{id}`）不受影响 |
| 数据来源与精度 | 统一转 24 kHz / 单声道 / PCM16（与引擎 `sample_rate=24000` 一致）；不改变音高、不做降噪或选窗 |
| 隐私与授权 | 参考音频属用户上传的肖像声音样本；仅在本地 `data/refs/` 落盘，不外发；原有授权勾选流程不变 |
| 离线与失败 | 转码失败（损坏/伪装文件）必须明确报错且不留脏索引；服务离线时维持现状，不做队列 |
| 存储与留存 | 同一音色重复上传覆盖索引，旧文件删除以免 `refs/` 膨胀；不新增独立清理任务 |
| 谁可见 | 仅本机面板使用者可见，无跨用户共享场景 |

## 8. 同行对标

| 需求 | 同类做法 | 来源 | 我们的差距 | 取舍 |
|---|---|---|---|---|
| R-01 | 剪映/ElevenLabs/CapCut 均接受 mp3/m4a/wav 等多种格式上传，服务端自行归一化成内部统一 WAV | https://ffmpeg.org/ffmpeg-formats.html | 现按扩展名收文件后原样落盘，非 WAV 直接被上游拒绝（`panel.py:2628-2666`） | 复用项目已有 soundfile 解码链（`tts_pipeline/ref_prep.py:516`），不额外引入 ffmpeg 依赖 |
| R-02 | HTML file 控件的 accept 属性只做选择器过滤、可被改名绕过，正规产品会前后端双层校验 | https://developer.mozilla.org/en-US/docs/Web/HTML/Element/input/file | 前端无任何约束，用户传错格式界面也不提示 | 前端限制 `.wav` 减少误传，后端兜底校验防绕过，两层都做 |

## 9. 拆解切片

- 切片 A：后端上传端点加格式校验 + 转码（R-01，AC-01/02/03）
- 切片 B：前端 `#refFile` 的 `accept` 改 `.wav`（R-02，AC-04）
- 切片 C：补 HTTP 用例测试并回归（`tests/test_voice_ref_upload.py`）

## 10. 覆盖维度

不适用：单端（本机 Web 面板）、单角色（本机用户），无多值。

## 11. 功能设计卡

- **用户旅程位置**：音色库 → 新建/编辑克隆音色 → 「上传参考音频」。覆盖状态：默认（已选 wav，可保存）、空（未选文件时按钮不可用）、加载（上传/转码中）、失败（格式非法或损坏 → 明确报错）、成功（显示「已上传」并可试听）。
- **行业基线**：主流配音/剪辑工具普遍接受多格式并服务端归一化（证据：https://ffmpeg.org/ffmpeg-formats.html 说明容器与编码可互转；剪映、ElevenLabs 均为多格式上传）。
- **我们的现状与差距**：`panel.py:2628-2666` 按扩展名白名单收下后**原样落盘**，`wav_duration()` 对 mp3 返回 -1（`data/voices.json:179-186` 的 `v_62d4e151` 即为此类坏数据）。**用户感知/痛点**：界面提示"已上传"，但之后每次合成都失败，报「所有配音都没生成成功」，用户完全找不到原因。
- **决策点与后果**：① 前端只放 `.wav` —— 减少误传，但可被改名绕过；② 后端统一转 24k/单声道/PCM16 —— 与引擎采样率一致，避免二次重采样。代价：每次上传多一次解码，长音频（分钟级）会慢几秒；取舍后认为可接受。
- **不做什么**：不做裁剪/智能选窗/降噪（保持样本原貌）；不做云端转码；不改素材库 `/api/assets` 与角色参考图上传。
- **交付形态**：后端接口行为变更 + 前端页面选择器约束；验证以命令行 HTTP 用例（curl 退出码 + 响应体 + 落盘文件探测）为准，前端核对选择器过滤行为。
