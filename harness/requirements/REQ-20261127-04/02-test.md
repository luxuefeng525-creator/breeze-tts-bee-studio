---
req_id: REQ-20261127-04
stage: test
status: active
owner: agent（breeze-tts 主工作区）
updated: 2026-11-27
---

# 测试记录

> 证据路径相对项目根 `f:\TTS2\breeze-tts`。

## 1. 环境

- 代码版本：主工作区 `f:\TTS2\breeze-tts`（未合并分支前的本地改动：`panel.py`、`web/index.html`、`web/app.js`、新增 `tests/test_voice_ref_upload.py`）
- 场景：本机 Web 面板 + 真实引擎 7860（faster_qwen3_tts，模型 design/clone）；用例数据隔离到 tmp，不碰用户数据
- 入口与参数：`POST /api/voices/{id}/ref`（multipart: ref_audio）；`POST /api/synthesize/stream`
- 运行方式：`python -m pytest tests\test_voice_ref_upload.py -q`；真实环境脚本 `C:\Users\Administrator\AppData\Local\Temp\bee-e2e\live_ref_check.py`
- 数据准备：用现网坏案例 mp3（`data/refs/v_62d4e151_1795775042.mp3`）做上传样本；验收临时音色用完即删，零残留

## 2. 验收点结果

| 验收点 | 场景 | 步骤 | 预期 | 实际 | 结论 | 证据 |
|---|---|---|---|---|---|---|
| AC-01 | 上传 mp3 | 新建克隆音色 → 上传现网 mp3 → 查落盘与索引 → 合成 | 200、落盘为标准 WAV、索引指向 wav、时长>0、合成 200 | 200；`refs/*.wav` 499088 B，头 `RIFF/WAVE`；`ref_duration=10.397`；质检 92；合成 200（226560 B），另见真实环境日志 | PASS | tests/test_voice_ref_upload.py |
| AC-02 | 空文件 / 伪装文件 | 分别上传 0 字节、PNG 内容改名 .wav/.mp3 | 400 且无新文件、索引不变 | 均 400（「无法解码」），refs 目录与上传前一致 | PASS | tests/test_voice_ref_upload.py |
| AC-03 | 超限文件 | 阈值调至 4KB，上传 8KB | 400「过大」且不落盘 | 400，detail 含「过大」，无新文件 | PASS | tests/test_voice_ref_upload.py |
| AC-04 | 前端约束 | 静态核对 `#refFile` 的 accept；`uploadRef` 增加扩展名/类型校验 | 只放行 .wav，非 wav 有提示且不发请求 | `index.html` 为 `.wav,audio/wav`；`app.js` 拦截并 toast 提示 | PASS | web/index.html |

## 3. 边界与异常

| 类型 | 场景 | 结果 | 证据 |
|---|---|---|---|
| 空数据 | 0 字节上传 | 400，无副作用 | tests/test_voice_ref_upload.py |
| 重复操作 | 同一音色连续上传两次不同 wav | 旧文件被清理，只剩最新一份 | tests/test_voice_ref_upload.py::test_repeat_upload_leaves_no_orphan |
| 伪装文件 | PNG 改名 .wav/.mp3 | 400「音频无法解码」 | 同上 |
| 超时 | 大文件拖慢请求 | 体积上限在解码前拦截（≥50MB 直接 400） | panel.py:REF_MAX_BYTES |
| 失败 | 上游不返回音频 | 由既有错误链路与守护处理，本次不涉及 | — |

## 4. 回归

| 受影响功能 | 验证方式 | 结果 |
|---|---|---|
| 面板接口（voices/soundtrack/ambience/api） | pytest 四套件 | 77 passed in 176.97s |
| 面板整体可用性 | 重启后 `/api/voices` `/api/roles` `/api/history` | 全 200 |
| 合成链路 | 转码后音色 `/api/synthesize/stream` | 200，PCM 226 KB |
| 现网坏案例修复 | `v_62d4e151`（雪峰原声）改用转换后的 wav | 合成 200（92–96 KB） |

## 5. 未验证项

| 项 | 未验证原因 | 影响 | 补验条件 |
|---|---|---|---|
| m4a/aac 上传 | 手头没有真实 m4a 样本 | 若 libsndfile 不支持会明确 400（不会静默出错） | 有样本时补一条用例 |
| 浏览器里手点上传的完整交互 | 用例走 HTTP；前端仅静态核对 | 选择器过滤行为变化小 | 需要时用 Playwright 走一遍 |

## 6. 结论

- 验收点通过率：4/4
- 是否可进入审查：是（开发与自测完成，G2 人确认待雪峰）
