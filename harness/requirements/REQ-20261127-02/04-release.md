---
req_id: REQ-20261127-02
stage: release
status: active
owner: trae
updated: 2026-11-27
---

# 发布与回退

## 1. 发布内容

| 项 | 内容 |
|---|---|
| 变更摘要 | 自定义 LLM 模型链路打通：`call_chat` 支持 base_url/model；新增 `_resolve_llm` 让自定义模型 id 可作 provider（读 `data/model_registry.json`）；script-gen / script-edit / provider-test 三处接入；响应统一带 `used_custom_model` |
| 涉及模块 | `api/llm_client.py`、`api/router.py`、`tests/test_custom_llm_model.py`（新增） |
| 配置变更 | 无（读已有 `data/model_registry.json`、`data/api_keys.json`）；面板需重启加载新代码 |
| 数据变更 | 无新增表；不迁移、不写库（只读注册表与 Key） |

## 2. 发布前检查

- [x] G3 验证门禁 PASS
- [x] 回退步骤已核对（无迁移、无新表）
- [x] 监控点已明确（见 §5）
- [ ] 发布窗口已确认（等用户指令）

## 3. 发布步骤

```text
1. 提交已在分支 session/trae-20261127；确认全量 pytest 绿
2. node harness/scripts/git-atomic.mjs merge session/trae-20261127 REQ-20261127-02 取命令，手动合流回 main
3. 重启面板（ops/run-panel.bat），确认 7861 返回 200
4. 真实接口复验：写入一条 type=llm 的自定义条目（base_url 指向本机不可达端口）→ POST /api/llm/script-gen 应返回 used_mock=true 且 used_custom_model=true；未知 id 应返回 400
5. 异常：内置 LongCat 生成回归失败或全量测试变红 → 停止合流，按 §4 回退
```

## 4. 回退

- 触发条件：合流后 `/api/llm/script-gen` 对内置 provider 行为异常、`/api/models` 列表错乱、全量测试 FAIL。
- 回退步骤：

```text
1. git revert 本次合流产生的 merge commit（未推送时可 reset 到合流前提交）
2. 还原 api/llm_client.py 与 api/router.py；删除 tests/test_custom_llm_model.py
3. 重启面板并复跑 .venv\Scripts\python.exe -m pytest -q
```

- 数据回滚：无数据库变更；只需删掉验收时临时写入的自定义模型条目（还原 data/model_registry.json 为原内容）。

## 5. 发布后监控

| 观察项 | 正常范围 | 观察时长 | 异常动作 |
|---|---|---|---|
| `POST /api/llm/script-gen` 内置 provider | 200，`used_custom_model=false` | 24h | 检查是否误传 base_url（现在会直接报错） |
| 自定义模型调用 | 200，端点指向注册 base_url；失败带 `used_custom_model=true` 与 `mock_reason` | 24h | 查 panel-api.log 的网络错误 |
| 全量 pytest | 630 passed | 每次改动后 | 变红即回退 |

## 6. 验收

- 验收人：用户
- 验收时间：2026-11-27
- 结论：待 G4 人确认
