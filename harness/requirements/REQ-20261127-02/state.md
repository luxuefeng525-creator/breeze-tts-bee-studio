---
req_id: REQ-20261127-02
stage: requirement
status: active
owner: trae
updated: 2026-11-27
size: L1
entry: /harness init
hits: []
---

# 状态

## 1. 当前进度

- [x] G1 立项 PASS（用户原文「按方案 A 确认（推荐）」）
- [x] 开发：`api/llm_client.py` 增 `base_url`/`model` 形参；`api/router.py` 新增 `_resolve_llm`，接入 script-gen / script-edit / provider-test 三处
- [x] 新增 `tests/test_custom_llm_model.py` 7 例
- [x] 单测 + 真实 curl 取证（不可达端点带 `custom_model_error`、未知模型 400）
- [x] 全量回归 630 passed
- [x] G3 独立审查：RV-01~RV-05，修 3 条（RV-01/04/05）、接受 2 条（RV-02/03），见 `03-review.md`
- [x] G3 修复后全量回归复跑 630 passed
- [ ] G3 / G4 人确认

## 2. 阻塞

| 阻塞项 | 等什么 | 谁能解 | 记录时间 |
|---|---|---|---|
| 无 | — | — | — |

## 3. 已验证

| 项 | 方法 | 结果 | 证据 |
|---|---|---|---|
| AC-01 base_url/model 生效 | 假端点抓请求体 + curl | PASS | `logs/ac01-ac03-custom-llm-pytest.txt`、`logs/ac02-curl-custom-llm.txt` |
| AC-02 自定义 id 作 provider / 402 / 400 | 单测 + curl | PASS | `logs/ac01-ac03-custom-llm-pytest.txt`、`logs/ac02-curl-custom-llm.txt` |
| AC-03 失败可见不静默 | 假端点 401 + 拒连 curl | PASS | `logs/ac01-ac03-custom-llm-pytest.txt`、`logs/ac02-curl-custom-llm.txt` |
| 全量回归（G3 修复后） | pytest -q | PASS 630 | `logs/regression-pytest.txt` |
| G3 审查整改复跑 | pytest tests/test_custom_llm_model.py + 失败子集 | PASS 17 / 5 | `03-review.md` §5 |
| 探针数据清理 | sqlite 删除 scripts.proj-curl-probe | PASS deleted=1 leftover=0 | `logs/ac02-curl-custom-llm.txt` |

## 4. 门禁记录

> 每道门禁一行，格式固定，字段不可增删改名。缺字段、为空或写 `<TBD>` 一律判 FAIL。
> agent 只能写 `FAIL` / `BLOCKED` / `待确认`；写 `PASS` 必须同时有人的确认原文。

```text
G1: PASS | 确认人: 用户 | 时间: 2026-11-27 07:21 | 确认原文: "按方案 A 确认（推荐）"
G2: PASS | 确认人: 用户 | 时间: 2026-11-27 07:36 | 确认原文: "G2 通过（推荐）"
G3: PASS | 确认人: 用户 | 时间: 2026-11-27 08:11 | 确认原文: "G3 通过（推荐）"
G4: 未开始 | 确认人: — | 时间: — | 确认原文: "<TBD>"
```

追加记录（不覆盖历史）：

| 门禁 | 结论 | 触发动作 | 证据路径 |
|---|---|---|---|
| G1 | PASS | 用户选项「按方案 A 确认（推荐）」 | `00-requirement.md` §2/§4/§7、`01-design.md` §2 |
| G2 | PASS | 全量 629 passed + 用户选项「G2 通过（推荐）」 | `02-test.md`、`logs/regression-pytest.txt` |
| G2 复跑 | PASS | G3 修复后全量 630 passed，AC 证据未变 | `02-test.md`、`logs/regression-pytest.txt` |
| G3 | PASS | 独立审查 RV-01~05（修 3 接受 2，无阻断）+ 用户选项「G3 通过（推荐）」 | `03-review.md`、`logs/regression-pytest.txt` |

## 5. 未验证项

| 项 | 原因 | 影响 | 补验条件 |
|---|---|---|---|
| 真实第三方 LLM（GLM/DeepSeek）出稿质量 | 本机无对应 Key | 只证明链路与报错落对 | 有 Key 时复跑一次 script-gen |
| 前端 `used_mock`/`mock_reason` 提示 UI | 本需求不做项 | 用户仍需看 JSON 才知降级 | 另开需求 |

## 6. 下一步

1. 用户给出 G3 确认原文 → 写 state.md §4
2. G4 人确认 → `stage: done` → `git-atomic.mjs merge` 合流回 main → 删分支
3. 推送远程需单独取得用户同意

## 7. 交接摘要（可选）

- 上次执行到：G3 修复落盘 + 全量 630 passed，等 G3 人确认
- 关键判断：选方案 A（后端按 provider 解析自定义模型），前端 4157 行现有交互零改动；内置 provider 走原逻辑
- 接手注意：面板已重启加载新代码；`_resolve_llm` 抛错时 script-gen/script-edit 返回 400，provider-test 归入 LLM 分支
- 环境坑（2026-11-27）：全量回归曾出现 26 failed，根因是 7860 (qwen-adapter) CUDA OOM 后卡在错误态（`/health` 503），**与本需求代码无关**。guard 因「port held by a process this guard did not start」拒绝接管手动启动的 7860，必须人工 kill 后重跑 `ops/run-api.bat`；判定方法：`/health` 非 200 或合成返回纯文本 `Internal Server Error`

## 8. 清理记录

> 只记本需求产生的清理；C 类永不自动删除。

| 路径 | 级别 | 依据 | 动作 | 时间 | 确认人 |
|---|---|---|---|---|---|
| `data/model_registry.json` 探针条目 | A | curl 取证临时写入 | 还原为 `[]`（备份覆盖） | 2026-11-27 | 自动 |
| `data/bee_tasks.db` 的 `scripts.proj-curl-probe` | A | curl 取证造数 | 删除（deleted=1 leftover=0） | 2026-11-27 | 自动 |

## 9. 自检记录

| 步骤 | 命令 | 退出码 | 证据路径 |
|---|---|---|---|
| 1 | `.venv\Scripts\python.exe -m pytest tests/test_custom_llm_model.py -q` | 0 | `logs/ac01-ac03-custom-llm-pytest.txt` |
| 2 | `.venv\Scripts\python.exe -m pytest -q` | 0 | `logs/regression-pytest.txt` |
| 3 | `curl POST /api/llm/script-gen`（真实面板） | 0 | `logs/ac02-curl-custom-llm.txt` |
