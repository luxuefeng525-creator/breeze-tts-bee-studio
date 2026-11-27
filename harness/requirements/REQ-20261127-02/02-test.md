---
req_id: REQ-20261127-02
stage: test
status: active
owner: trae
updated: 2026-11-27
---

# 测试记录

> 本文件只记录执行结果，不记录开发自述。

## 1. 环境

- 代码版本：工作区未提交改动（`api/llm_client.py`、`api/router.py`、`tests/test_custom_llm_model.py`）
- 场景：localhost，面板 7861（重启后加载新代码），解释器 `.venv\Scripts\python.exe`
- 入口与参数：`POST /api/llm/script-gen`；单测经 `router.handle_post` + 本地假 OpenAI 端点（127.0.0.1 随机端口）
- 运行方式：`.venv\Scripts\python.exe -m pytest tests/test_custom_llm_model.py -q`
- 数据准备：单测把注册表/DB 指向 `tmp_path`，零污染；curl 实测临时写 `data/model_registry.json` 探针条目，测后还原为 `[]` 并删除 `scripts` 表 `proj-curl-probe`（deleted=1 leftover=0）

## 2. 验收点结果

| 验收点 | 场景 | 步骤 | 预期 | 实际 | 结论 | 证据 |
|---|---|---|---|---|---|---|
| AC-01 | 自定义模型带 base_url | 假端点抓请求体，走完整 script-gen | 出站打到该 base_url（保留 /v1 只拼一次）、model = 注册 model_name、used_mock=false | 7 passed，`req["model"]=="glm-probe-1"`，messages 含主题 | PASS | `harness/requirements/REQ-20261127-02/logs/ac01-ac03-custom-llm-pytest.txt` |
| AC-02 | 自定义 id 作 provider / 缺 Key / 未知 id | 分别传 api_key、空 api_key、未知 provider | 有 Key→真实调用；无 Key→402 need_key 且零请求；未知→400 | 三者均符合 | PASS | `harness/requirements/REQ-20261127-02/logs/ac01-ac03-custom-llm-pytest.txt` |
| AC-03 | 端点不可达（拒连） | base_url 指向未监听端口 | mock 兜底 + used_custom_model=true + 原因可见，不 502 | used_mock=true + used_custom_model=true + mock_reason 含连接错误 | PASS | `harness/requirements/REQ-20261127-02/logs/ac02-curl-custom-llm.txt` |

## 3. 边界与异常

| 类型 | 场景 | 结果 | 证据 |
|---|---|---|---|
| 空数据 | base_url 为空字符串 | `_resolve_llm` 报「未填 base_url」，不出站 | `logs/ac01-ac03-custom-llm-pytest.txt` |
| 非法值 | `ftp://nope`、`not-a-url` | 明确报错，不调用 | `logs/ac01-ac03-custom-llm-pytest.txt` |
| 依赖失败 | 端点拒连 / 返回 401 | mock 兜底 + `used_custom_model=true` | `harness/requirements/REQ-20261127-02/logs/ac02-curl-custom-llm.txt` |
| 缺 Key | 不传 api_key 且未配 Key | 402 `need_key`，假端点 0 请求 | `logs/ac01-ac03-custom-llm-pytest.txt` |
| 重复/撞名 | 内置 provider 与注册表同名 | 内置优先（`provider in LLM_PROVIDERS` 直接返回），注册表指向缺失也不影响 | `logs/ac01-ac03-custom-llm-pytest.txt` |

## 4. 回归

| 受影响功能 | 验证方式 | 结果 |
|---|---|---|
| 内置 LongCat/OpenAI 生成链路 | `_resolve_llm("longcat"/"openai")==("","")`，行为不变 | PASS（单测） |
| script-edit（改镜头） | 同一 `_resolve_llm` 接入，`call_chat` 多传两个可选形参 | PASS（代码走查 + 全量回归） |
| provider 连通性测试 | `/api/providers/{provider}/test` 对自定义 LLM 走 chat ping | PASS（代码走查） |
| 全量回归 | `pytest -q` | 见 §6 |

## 5. 未验证项

| 项 | 未验证原因 | 影响 | 补验条件 |
|---|---|---|---|
| 真实第三方 LLM（GLM/DeepSeek）出稿质量 | 本机无对应 Key | 只证明链路通、格式对 | 有 Key 时复跑一次 |
| 前端 mock 降级提示 UI | 本需求不做项 | 用户仍需看 JSON 才知道降级 | 另开需求接 `used_mock`/`mock_reason` |

## 6. 结论

- 验收点通过率：3/3
- 是否可进入审查：是（AC 均有真实证据；全量回归结果见 state.md 自检记录）
