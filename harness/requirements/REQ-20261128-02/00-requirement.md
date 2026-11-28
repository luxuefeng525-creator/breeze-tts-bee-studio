---
req_id: REQ-20261128-02
stage: requirement
status: active
size: L
owner: trae（breeze-tts 主工作区）
updated: 2026-11-28
acceptance: [AC-01, AC-02, AC-03, AC-04, AC-05, AC-06]
---

# 需求

> 档位 L：本需求为「新增一条可选合成引擎 + 面板路由」，多文件但行为边界清楚，按 L 交付（§9 有 2 个取值，需填矩阵；§10 有界面改动，需填功能设计卡）。

## 1. 背景与目标

- 触发场景：现在合成链路只有 qwen3-tts 一条上游，`panel.py:80` 的 `_post_upstream()` 把 `API_BASE` 固定成 `http://127.0.0.1:7860`，三条合成入口（`panel.py:2980` 流式、`panel.py:3092` 单条、`panel.py:3235` 批量）全都只能走它。用户在克隆音色时希望多一种「更像本人、情绪更足」的引擎可选，且不想改变现有面板的操作习惯。
- 期望结果：能给音色（或角色绑定）指定引擎；指定 GPT-SoVITS 时由本机 sidecar 服务合成，未指定时一切照旧走 7860；两条链路的产物都落成同一种标准音频，后续试听/入库/导出逻辑零改动。
- 验证通道：命令行 HTTP（退出码 + 响应体 + 落盘文件探测）+ 面板实测；GPU 侧用 `nvidia-smi` 读数佐证显存互斥。
- 不做项：不做模型微调训练；不做独立客户端界面；不替换 qwen3-tts 为主力引擎；不改 7860 端口契约与既有音频格式（24 kHz / 单声道 / PCM16）；不接入需要联网的云端 TTS。

## 2. 需求条目

### R-01

当面板收到 engine=gpt-sovits 的合成请求时，系统应把请求转发到本机 GPT-SoVITS sidecar（默认 `127.0.0.1:9880`），并把返回音频统一成 24 kHz / 单声道 / PCM16 的 WAV 后再进入既有落库与试听链路，以便前端与历史记录无需区分引擎。

- 验收点 AC-01：sidecar 起来后 `GET 127.0.0.1:9880/health`（或等价探针）返回 200；用真实参考音频 + 参考文稿合成，返回 200 且响应体头 4 字节为 `RIFF`；面板侧收到的产物采样率 24000。
- 验收点 AC-05：参考音频长于 10 秒时，系统应自动裁出语音最密集的 3~10 秒片段，并用 ASR 重新转写该片段作为参考文稿（繁转简），使既有任意长度的音色都能直接用于 GPT-SoVITS；裁段结果按音色缓存，重复合成不重复裁。
- 边界：参考音频为空或缺失必须明确拒绝，不得发请求；sidecar 未启动时必须给出可执行报错（提示先启动服务），不得静默回落到 7860；参考音频短于 3 秒必须明确拒绝并提示重录（内容补不出来）。

### R-02

当一个音色（或其角色绑定）带 `engine` 字段且值为 `gpt-sovits` 时，三条合成路径都应改走 sidecar；`engine` 缺省或为 `qwen3-tts` 时仍走 7860，以便老数据无感、用户按需逐音色切换。

- 验收点 AC-02：新建一个标了 `engine=gpt-sovits` 的音色并发起 `/api/synthesize/stream`，返回 200，产物可播放且与当前格式一致；同一音色的 `/api/synthesize` 与 `/api/synthesize/batch` 同样生效。
- 验收点 AC-03：sidecar 不可用时，三条路径都返回明确错误且文案含「GPT-SoVITS 服务未启动/不可达」，不允许悄悄用 7860 出声。
- 边界：并发/重复请求要排队，不得同时打爆 sidecar；超长文本要设超时，超时后明确失败而不是挂住连接。

### R-03

当 sidecar 需要显存而本机 7860 已占满时，系统应在合成前给出可执行的处置提示（先停主力引擎或改用 qwen），并在 sidecar 启动失败时留下可读日志，而不是让进程 OOM 崩溃或让用户面对空白失败。

- 验收点 AC-04：在 7860 常驻（显存接近占满）的情况下直接请求 gpt-sovits，返回的错误文案包含处置指引；此时守护日志中能看到 sidecar 启动/退出的原因记录。
- 边界：显存不足属极端值场景，不允许出现「面板显示成功但历史里没有音频」的情况。

### R-04

当用户在面板「设置与诊断」里点「启动 GPT-SoVITS」时，系统应在不阻塞界面的前提下让出显存并拉起 sidecar，界面状态从「启动中」实时变为「运行中」；点「停止」应停掉 sidecar 并让守护自动把主力引擎拉回来，以便用户不必开命令行。

- 验收点 AC-06：`POST /api/engine/sovits/start` 在 3 秒内返回 `starting`；随后 `GET /api/engine/sovits` 能看到状态迁移到 `running`（实测 31 秒）；运行期 9880 可连、7860 已让出；`POST /api/engine/sovits/stop` 后状态回 `idle`，7860 在 2 分钟内由守护恢复。
- 边界：重复点击启动不得叠加多个进程（已在途时直接返回 starting）；启动失败必须落到 `error` 并给出日志路径，不能停在 starting 卡死。

## 3. 验收点汇总

| 编号 | 对应需求 | 判定方式 | 优先级 |
|---|---|---|---|
| AC-01 | R-01 | 命令行 HTTP：探针 200 + 合成 200 + 响应体 RIFF 头 + 采样率 24000 | P0 |
| AC-02 | R-02 | 面板实测 + HTTP：标 engine 的音色三条路径均 200 且产物可播 | P0 |
| AC-03 | R-02 | 停掉 sidecar 后三条路径分别请求，均返回含指引的错误 | P0 |
| AC-04 | R-03 | 制造显存紧张后请求，查错误文案与日志 | P1 |
| AC-05 | R-01 | 用 14.7 秒真实参考音频 → 自动裁到 ≤10 秒且 ASR 文稿匹配 → 合成 200 | P0 |
| AC-06 | R-04 | 面板一键：start 3 秒内返回 starting → 轮询到 running → stop 后 7860 恢复 | P0 |

## 4. 代码证据与边界反例

| 类型 | 证据 | 说明 |
|---|---|---|
| 现有实现定位 | `panel.py:80-95` | `_post_upstream()` 把上游写死在 `API_BASE`（`http://127.0.0.1:7860`），是唯一的出声通道 |
| 调用方 / 数据来源 | `panel.py:2980`、`panel.py:3092`、`panel.py:3235` | 流式 / 单条 / 批量三条合成入口，全部调 `_infer_with_retry()` |
| 上游响应解析 | `panel.py:3041-3072` | 从 `voices.json` 取 `ref_audio`/`ref_text`/`instruction` 组 multipart；音色字段全集见 `data/voices.json`（当前无 `engine`） |
| 前端入口 | `web/app.js:1613` | 新建/保存音色走 `/api/voices`，随后 `web/app.js:1621` 上传参考音频；引擎选择需在此附近露出 |
| 上游适配器 | `qwen-adapter/qwen_adapter.py`（仓库根外相对引用，仅作说明） | 7860 的 `/v1/audio/speech` 契约不变，本需求不动它 |
| 边界 / 反例 1 | 空数据：参考音频为空或文件缺失 | 必须在面板侧明确拒绝，不发出请求 |
| 边界 / 反例 2 | 超时：长文本打到未起好的 sidecar | 必须超时失败并给出可读原因，不允许无限等待 |
| 边界 / 反例 3 | 无权限：本机单用户，无鉴权 | 不适用：沿用现有口径，不新增权限概念（见 §7） |

- 定位均来自实际打开过的文件。
- 对标见 §8；显存基线来自本机实测（见 §8.1 技术选型）。

## 5. 影响面

- 涉及模块：`panel.py`（新增 sidecar 客户端与路由分支）、`web/index.html` + `web/app.js`（音色编辑处增加引擎选择）、新增 sidecar 运行脚本（放在 `scripts/` 或 `tools/`）
- 涉及接口：面板内 `POST /api/synthesize`、`/api/synthesize/stream`、`/api/synthesize/batch`；新增对本机 sidecar 的调用（默认 9880）
- 数据变更：`data/voices.json` 允许新增可选字段 `engine`（缺省视为 `qwen3-tts`）；不改既有字段含义，可逆（删字段即回退）
- 不新增数据库表

## 6. 人确认

```text
G1: 待确认 | 确认人: <TBD> | 时间: <TBD> | 确认原文: "<TBD>"
```

- 需求与方案各确认一次，方案追加 `G1-B:` 行。
- 确认原文写用户原话，不接受转述；字段不齐 → G1 FAIL。

## 7. 澄清清单

| 类别 | 结论 |
|---|---|
| 触发与时序 | 仅在「音色的 engine=gpt-sovits」时改走 sidecar；请求进来先解析音色拿到 engine，再决定转发目标，最后仍走统一的采样率归一与落库 |
| 数据来源与精度 | 参考音频/文稿取自 `data/voices.json` 与 `data/refs/`；统一输出 24 kHz / 单声道 / PCM16，与现有引擎口径一致（GPT-SoVITS v4/v5 原生 48 kHz 需重采样） |
| 隐私与授权 | 参考音频是用户自己的声音样本，仅在本地 sidecar 与 `data/refs/` 之间流转，不外发、不上云；沿用现有授权勾选 |
| 离线与失败 | sidecar 不可用/显存不足必须明确报错且不静默回落；断网不影响（全本地）；超时给可读原因 |
| 存储与留存 | 音频产物仍落 `data/audio/` 与历史记录；sidecar 自身缓存在其自己目录，不纳入面板账本，不需要额外清理任务 |
| 谁可见 | 仅本机面板使用者可见，无跨用户共享场景 |

## 8. 需求分析与同行对标

| 需求 | 对标对象 | 来源 | 他们的做法 | 我们的取舍 | 理由 |
|---|---|---|---|---|---|
| R-01 | GPT-SoVITS 官方 `api_v2.py`（FastAPI） | https://raw.githubusercontent.com/RVC-Boss/GPT-SoVITS/main/api_v2.py | 默认监听 `127.0.0.1:9880`，`POST /tts` 收 text/ref_audio_path/prompt_text/media_type 等，返回音频流 | 采纳：直接复用它的 HTTP 形态做 sidecar，不自己实现推理 | 官方 API 已稳定，自研包装只会多一层出错面 |
| R-02 | VoiceStudio 的多引擎目录（可同时装 CosyVoice/IndexTTS/Qwen3-TTS 等） | https://raw.githubusercontent.com/debpalash/VoiceStudio/main/docs/engines/README.md | 用「引擎目录 + 切换」把不同 TTS 统一到一个界面，每个引擎可单独启停 | 改造：只做两个引擎（qwen3-tts / gpt-sovits），不引入它的桌面壳 | 我们需要的是引擎能力，不是又一个客户端 |
| R-03 | VoiceStudio 对 OmniVoice 的显存下限标注（低于 6 GB 会退化到 CPU 级并显著变慢） | https://raw.githubusercontent.com/debpalash/VoiceStudio/main/docs/engines/omnivoice.md | 明示 VRAM floor，并在低于下限时给出警告而非硬失败 | 采纳：做成「合成前置检查 + 明确文案」 | 本机 8 GB 必须把显存当一等约束，不能靠运气 |

### 8.1 维度检查

| 维度 | 结论 | 来源 | 不适用理由 |
|---|---|---|---|
| 同类同行 | 开源本地 TTS 普遍提供 HTTP API（GPT-SoVITS `api_v2.py`）；多引擎聚合工具用目录+切换 | https://github.com/RVC-Boss/GPT-SoVITS ；https://raw.githubusercontent.com/debpalash/VoiceStudio/main/docs/engines/README.md | 无 |
| 功能需求 | 合成、参考音频克隆、结果返回；不做训练/不做云端 | https://github.com/RVC-Boss/GPT-SoVITS | 无 |
| UI 设计 | 音色编辑处加「引擎」选择，文案随引擎变化 | 本机 `web/index.html`（音色档案区） | 无 |
| 数据库 | 不适用：无数据库 | — | 面板用 JSON 账本，`data/voices.json` 仅新增可选字段 |
| 技术选型 | Python + FastAPI sidecar（沿用仓库现有 Python 栈）；权重用 GPT-SoVITS 官方发布的 v3 预训练（24 kHz 原生，免重采样） | https://huggingface.co/lj1995/GPT-SoVITS/tree/main | 无 |

## 9. 覆盖矩阵

覆盖维度：引擎=qwen3-tts,gpt-sovits

| 维度/取值 | 取值范围 | 验收点 | 对标基线（来源） | 说明 |
|---|---|---|---|---|
| 引擎/qwen3-tts | 不标 engine 或 engine=qwen3-tts 的既有音色 | AC-02 | https://qwen.ai/blog?id=qwen3tts-0115 | 回归项：老数据行为完全不变 |
| 引擎/gpt-sovits | engine=gpt-sovits 的新建音色 | AC-01、AC-03 | https://github.com/RVC-Boss/GPT-SoVITS | 新链路，含不可用时的报错 |

## 10. 功能设计卡

### 卡 1：给音色加「合成引擎」选项并接到 sidecar

- **用户旅程位置**：入口在「克隆/编辑音色」弹窗（`web/index.html` 音色档案区，保存走 `web/app.js:1613`）。默认态：未标 engine 的音色沿用 qwen3-tts；空态：sidecar 未启动时选择 gpt-sovits 会立刻收到「服务未启动」提示而不是等到合成失败；加载态：合成中与现在一致（沿用既有进度提示）；失败态：sidecar 不可用/显存不足 → 明确文案 + 处置建议；成功态：与现在完全一致（音频入历史、可试听）。上一态：不选引擎的旧流程；下一态：导出/剧本批量合成。
- **行业基线**：同类本地 TTS 都提供独立 HTTP 服务供第三方调用（GPT-SoVITS `api_v2.py`，https://raw.githubusercontent.com/RVC-Boss/GPT-SoVITS/main/api_v2.py ），多引擎工具用「引擎目录 + 运行时切换」（https://raw.githubusercontent.com/debpalash/VoiceStudio/main/docs/engines/README.md ）。
- **我们的现状与差距**：`panel.py:80` 的 `_post_upstream()` 把上游写死成 7860，`data/voices.json` 也没有 `engine` 字段，因此没有任何办法选择第二条引擎。用户感知/痛点：想换更接近本人的声音时，只能整套换掉主面板，历史音频和角色绑定全部作废。
- **决策点与后果**：引擎路由放哪 → 取「面板侧路由、sidecar 只当纯推理服务」；代价：面板要多维护一份 sidecar 地址与错误口径，且必须接受 sidecar 与 7860 **不能同时满载**（本机 8 GB，实测 7860 常驻时已用约 7.2 GB、仅剩约 0.6 GB）；风险：切换引擎会有几十秒加载等待。
- **不做什么**：不做模型微调训练（超出范围且需要大量标注）；不做独立客户端（与现有面板重复）；不纳入云端 TTS（参考音频属用户肖像，不外发）；不把 sidecar 直接暴露给浏览器（只允许本机面板调用）。
- **交付形态**：后端接口行为变更 + 前端页面新增选择项；验证以命令行 HTTP 用例与面板实测为准（无整页视觉重做）。

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-11-28 | 初稿 | — |

## 12. 功能开发拆解

| 切片 | 目录结构 | 文件归属 | 路由 / 入口 | 函数 / 模块 | 数据表 / 字段 | 验收方式 | 清理项 |
|---|---|---|---|---|---|---|---|
| 切片 A | `tools/` 或 `scripts/` | 新增 sidecar 启停脚本 + 配置（地址/端口/日志） | 命令行启停；面板健康检查 | `sovits_probe()` | 不适用：无持久化 | 探针 200（AC-01） | 临时权重下载目录、临时日志 |
| 切片 B | `panel.py` | 新增 sidecar 客户端 + 三条合成路径的路由分支 | `/api/synthesize`、`/api/synthesize/stream`、`/api/synthesize/batch` | `_post_sovits()`、`voice_engine()` | `data/voices.json.engine`（可选字段） | HTTP 用例（AC-02、AC-03） | 临时音色与音频 |
| 切片 C | `web/index.html`、`web/app.js` | 音色档案区增加引擎选择并在保存时提交 | 音色编辑弹窗 | `saveVoice()` 附近 | 同上 | 面板实测 + 静态核对（AC-02） | 临时音色 |

- 同一文件不被两个切片声明（`panel.py` 只归切片 B）。
- 无数据库：不适用，无持久化迁移；`engine` 字段删除即回退。
- 每个切片均可单独用命令/用例验收。
