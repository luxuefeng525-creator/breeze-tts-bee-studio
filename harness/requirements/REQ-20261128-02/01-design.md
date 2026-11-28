---
req_id: REQ-20261128-02
stage: design
status: active
owner: trae（breeze-tts 主工作区）
updated: 2026-11-28
---

# 方案设计

## 1. 现状

- 唯一出声通道：`panel.py:80-95` 的 `_post_upstream()` 把上游写死在 `API_BASE = http://127.0.0.1:7860`，三条合成入口（`panel.py:2980` 流式、`:3092` 单条、`:3235` 批量）都经 `_infer_with_retry()`（`panel.py:112`）走它。
- 音色数据：`data/voices.json` 字段全集为 `id/name/type/instruction/ref_audio/ref_text/cfg_scale/created_at/ref_duration/archetype`，**没有 engine**，所以引擎无从选择。
- 上游契约：7860 的 `/v1/audio/speech` 收 multipart（text、ref_audio、ref_text、instruction、cfg_scale…），返回裸 PCM；面板在 `panel.py:3104-3110` 做变速重采样并封 WAV。
- 显存实测（2026-11-28 06:19）：RTX 5060 总 8151 MiB，已用约 7290 MiB（主要是常驻的 qwen3-tts 引擎进程），空闲约 606 MiB。

## 2. 方案

| 方案 | 做法 | 优点 | 代价 | 结论 |
|---|---|---|---|---|
| A | 面板侧路由：新增 sidecar 客户端与 `engine` 字段，按音色选引擎；sidecar 只当纯推理服务 | 不动 7860 契约；老数据无感；可逐音色切换 | 要维护两条客户端与两套错误口径；显存要互斥 | 选用 |
| B | 改造 7860 适配器，让它内部支持多模型 | 面板零改动 | 要动已冻结的适配器端口与进程模型，回归面大 | 否决 |
| C | 直接换成 GPT-SoVITS 作为主引擎 | 不用维护两条链路 | 放弃 qwen3-tts 的稳定性与既有角色绑定；显存同样不够 | 否决 |

选定 A，理由：缺陷本质是「只有一条出声通道」，不是「qwen3-tts 不好」。把引擎选择放到面板、sidecar 保持无状态纯推理，改动局部且可回退。

## 3. 模块与改动点

| 文件 / 模块 | 改动 | 原因 |
|---|---|---|
| `tools/gpt_sovits_sidecar.py`（新增） | 启停/探针：拉起官方 `api_v2.py`（默认 9880）、健康检查、日志落 `logs/` | R-01、R-03 |
| `panel.py` | 新增 `_post_sovits()` 与 `voice_engine()`；在 `:3092`/`:3235`/`:2980` 三处按 engine 选择上游 | R-01、R-02 |
| `panel.py` | 合成前自检：engine=gpt-sovits 而探针不通 → 明确报错，不回落 | R-02、R-03 |
| `web/index.html`、`web/app.js` | 音色档案区增加「合成引擎」选择，保存时写入 `engine` | R-02 |
| `tests/test_gpt_sovits_route.py`（新增） | 用例：路由选择、不可用报错、格式归一 | AC-01~AC-04 |

不动：`qwen-adapter/qwen_adapter.py`、7860 端口、既有音频格式与历史表结构。

## 4. 接口与数据结构

```text
输入：text、ref_audio（可选）、ref_text（可选）、speed 等，与现有合成入参同形
输出：24 kHz / 单声道 / PCM16 的 WAV（与 7860 路径完全一致）
sidecar：POST 127.0.0.1:9880/tts（官方 api_v2.py 参数），探针 GET /health 或等价
存储：data/voices.json 新增可选字段 engine（缺省 = qwen3-tts）
```

## 5. 权限与边界

- 谁能触发：本机面板使用者（沿用现有口径，无鉴权）。
- 越权处理：不适用：本机单用户，不新增权限概念。
- 只读边界：sidecar 配置与权重只读；面板不修改模型文件。

## 6. 影响面

涉及 `panel.py` 的三条合成入口与新增 sidecar 客户端、`web/index.html`/`web/app.js` 的音色编辑弹窗；数据侧仅给 `data/voices.json` 增加可选字段 `engine`（缺省即原行为，可逆）；不改 `qwen-adapter`、不改 7860 端口契约、不改既有音频格式与历史记录结构。最大风险是**显存互斥**：本机 8 GB，7860 常驻时已用约 7.2 GB，GPT-SoVITS 需另占数 GB，因此 sidecar 设计为按需启动，并在合成前置检查显存/服务可用性。

## 7. 回退思路

- 回退触发条件：面板合成出现新的 5xx/超时，或 sidecar 引起显存紧张导致 7860 也跑不动。
- 回退步骤：删掉 `engine` 字段与路由分支（`git revert` 本次提交），sidecar 停掉即可；无数据迁移。

## 8. 未决问题

| 问题 | 影响 | 待定方 |
|---|---|---|
| GPT-SoVITS 版本选型（v2/v3 原生 24 kHz 免重采样；v4/v5 原生 48 kHz 需重采样且更吃显存） | 决定是否需要重采样与能否在 8 GB 下稳定跑 | 用户 |
| 是否接受「切到 gpt-sovits 合成时先释放 7860 显存」带来的几十秒等待 | 决定体验口径 | 用户 |
| 是否顺带把 IndexTTS2 / CosyVoice3 也做成可选引擎 | 扩大范围 | 用户（默认不做） |
