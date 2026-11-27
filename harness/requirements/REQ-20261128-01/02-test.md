---
req_id: REQ-20261128-01
stage: test
status: active
owner: trae
updated: 2026-11-28
---

# 测试记录

> 本文件只记录执行结果，不记录开发自述。

## 1. 环境

- 代码版本：工作树 `f:\TTS2\breeze-tts-trae`（分支 `session/trae-20261128`）未提交改动 `api/router.py`、新增 `tests/test_llm_error_clip.py`
- 场景：localhost；不启服务、不连真实 LLM；用 monkeypatch 让 `api.llm_client.call_chat` 抛长异常，并拦截 `_script_write` 避免落库
- 入口与参数：直接调用 `router.handle_post(h, "/api/llm/script-gen", body, "application/json")`；`pytest tests/test_llm_error_clip.py -q`
- 运行方式：`f:\TTS2\breeze-tts\.venv\Scripts\python.exe -m pytest ...`
- 数据准备：无 DB 写入、无网络请求、无临时文件残留

## 2. 验收点结果

| 验收点 | 场景 | 步骤 | 预期 | 实际 | 结论 | 证据 |
|---|---|---|---|---|---|---|
| AC-01 | LLM 异常降级 | 构造 `str(exc)` > 200、尾部为 `[WinError 10061] ConnectionRefusedTailMarker`，走 script-gen 降级分支 | `mock_reason` 含尾部片段且无半词 | `mock_reason` 保留该尾部标记，长度 ≤ 201 | PASS | `harness/requirements/REQ-20261128-01/logs/pytest-targeted.txt` |
| AC-02 | 短 / 空 / None | 直接调用 `_clip_reason("短文本")`、`_clip_reason("")`、`_clip_reason(None)` | 短文本原样；空值给「未知原因」，不出现 `None` | 三态均符合预期 | PASS | `harness/requirements/REQ-20261128-01/logs/pytest-targeted.txt` |
| AC-03 | 全量回归 + 静态扫描 | 扫 `api/router.py` 裸异常头部硬切；跑全量 `pytest -q` | 无裸 `str(异常)[:N]`；全量不红 | 扫描无命中；650 passed，退出码 0 | PASS | `harness/requirements/REQ-20261128-01/logs/pytest-full.txt` |

## 3. 边界与异常

| 类型 | 场景 | 结果 | 证据 |
|---|---|---|---|
| 超长且无分隔符 | 尾部窗口内找不到空格/中文标点 | 回退为 `…` + 完整尾部窗口，长度仍 ≤ 201 | `tests/test_llm_error_clip.py::test_long_text_keeps_tail_marker` |
| 中文标点 / emoji | 长文本混入 `，`、emoji 与尾部标记 | 按字符限长，末尾标记完整保留 | `tests/test_llm_error_clip.py::test_chinese_punctuation_and_emoji_boundary` |
| 重复前缀 | 同一异常被多层包装、前缀重复 | 按需求不去重，只保证截断方向与断词策略 | `api/router.py:617-638` 的 `_clip_reason` |
| 超时类 | 多层 `Max retries exceeded` 等超长异常链 | 同超长策略，保留尾部连接/超时细节 | `tests/test_llm_error_clip.py::test_script_gen_degradation_preserves_tail` |
| 第三方响应预览 | `resp.text[:N]` 语义是看响应开头 | 明确不动，不在本需求范围 | `harness/requirements/REQ-20261128-01/00-requirement.md` §1 不做项 |

## 4. 回归

| 受影响功能 | 验证方式 | 结果 |
|---|---|---|
| 截断相关既有测试 | `pytest tests/test_llm_error_clip.py tests/test_bee_studio_notice.py tests/test_router_provider_guard.py tests/test_script_api.py tests/test_custom_llm_model.py -q` | PASS 55 passed |
| 全工程回归 | `pytest -q` | PASS 650 passed（1 条 fastapi/httpx deprecation warning，与本次改动无关） |
| 语法编译 | `python -m py_compile api/router.py tests/test_llm_error_clip.py` | PASS |

## 5. 未验证项

| 项 | 未验证原因 | 影响 | 补验条件 |
|---|---|---|---|
| 真实上游超时/拒连的浏览器端展示 | 本需求只改后端字符串；前端展示已在 REQ-20261127-03 验收，本轮用单测构造异常 | 未做真实网络失败的端到端复现 | 有可用 Key/端点时做一次真实降级 |
| `providers/*`、`video_upscale.py`、`cost_calc.py`、`comfy_templates.py` 的同模式截断 | 不在本需求批准的 `router.py` 9 处范围内 | 那些入口的长异常仍可能丢尾部 | 用户确认后另开需求 |

## 6. 结论

- 验收点通过率：3/3
- 是否可进入审查：是（AC-01/02/03 均有命令退出码与日志证据，全量回归 650 passed）
