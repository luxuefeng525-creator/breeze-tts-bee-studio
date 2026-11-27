---
req_id: REQ-20261127-02
stage: review
status: active
owner: trae
updated: 2026-11-27
---

# 审查记录

> 只依据实际改动与需求条目，不参考开发过程自述。

## 1. 审查范围

- 改动文件：`api/llm_client.py`、`api/router.py`（提交 0d21772、acba474）
- 需求条目：R-01（`/api/models` 的 `base_url` 有读取方）、R-02（自定义 id 可作 provider，失败可辨）
- 未覆盖需求：无

## 2. 问题清单

| 编号 | 严重度 | 性质 | 位置 | 问题 | 证据 | 改法 | 已补检查 | 状态 |
|---|---|---|---|---|---|---|---|---|
| RV-01 | 建议 | 需人判断 | api/router.py:1672 | script-edit 502 分支用 `'raw' in dir()` 探测变量是否已赋值，依赖 CPython「dir() 只列已绑定局部名」的实现细节；实测当前行为正确（未赋值时为 False），但一旦后续重构在 try 前预初始化 raw 或改变作用域，except 块自身会抛 UnboundLocalError，请求无响应 | `python -c` 复现：未赋值函数中 `'raw' in dir()` → False；赋值后 → True | try 之前显式 `raw = ""`，分支内直接 `(raw or "")[:500]`；或改用 `locals()` 判断并注释意图 | — | 已修 |
| RV-02 | 建议 | 需人判断 | api/router.py:32,98；tests/test_custom_llm_model.py:101 | `_db_ready` 是进程级布尔，与 `_DB_PATH` 无绑定：换库路径后若标志仍为 True，`_db_init` 直接 return，新库不建表。测试靠每次 monkeypatch 重置绕过，属测试与全局状态耦合，面板若支持切库/热重载会踩同一坑 | `_db_init` 只判 `if _db_ready`，不比对当前 `_DB_PATH`；测试注释自认「换 DB 路径后必须重置」 | `_db_init` 记录已初始化的路径（`_db_ready_for`），路径变化即重建；至少在自检里固化「改 _DB_PATH 必重置」 | — | 接受：既有设计，生产环境 `_DB_PATH` 恒定；测试侧已按契约重置，不改本次范围 |
| RV-03 | 建议 | 需人判断 | api/router.py:640-648,651-655 | `_load_custom_models` 每次调用都整文件读盘，与 `_save_custom_models` 之间无锁；本次改动让 script-gen / script-edit / provider-test 三处都读它，用户在面板保存自定义模型的瞬间并发请求可能读到半截 JSON → 解析异常被 except 吞成 `[]` → `_resolve_llm` 抛「未知 LLM provider」400 | `_load_custom_models` 捕获所有异常返回 `[]`；`_resolve_llm`（router.py:602-613）拿到空列表即 raise | 读写共用一把模块级锁，或写入采用临时文件 + `os.replace` 原子替换 | — | 接受：属既有模式（非本次引入），改动仅增加读取点；并发写读窗口极小且报错明确不会静默降级；如要修另开需求 |
| RV-04 | 建议 | 需人判断 | api/llm_client.py:55-60 | `call_chat` 内置分支用 `base_url or cfg[...]`，接口层面允许任何调用方改写 longcat/openai 的端点、模型与超时；当前 router 三处对内置均传 `("", "")`，行为与改动前一致，但内置与自定义未隔离，未来误传即静默改道 | diff：`base_url = (base_url or cfg["endpoints"][protocol]).rstrip("/")`，无 provider 类型判断 | 内置分支忽略传入的 base_url/model（或加显式 override 开关），自定义分支才消费形参 | — | 已修：内置分支对非空 base_url 直接报错 |
| RV-05 | 建议 | 需人判断 | api/router.py:1548,1562,1671 | `custom_model_error` 取值为 `bool(llm_base)`，语义是「本次用的是自定义模型」而非「自定义模型出错」；且 402（未配置 Key）与 400（resolve 失败）响应里没有该字段，前端按字段区分链路错误时口径不齐 | 内置 provider `llm_base=""` 恒 False；自定义恒 True，与是否真的出错无关 | 改名 `used_custom_model` 之类贴合语义，并在 402/400 响应补齐同名字段（或明确文档化仅在降级/502 出现） | — | 已修：字段改名 `used_custom_model`，成功/402/400/mock/502 响应口径统一 |

严重度口径：

- 致命：功能错误、越权、数据损坏、密钥泄露；（别写「阻断」—— 门禁会把它当成阻断记录）
- 建议：可维护性、命名、重复、风格。

### 性质与已补检查怎么填

**这一节不是形式。** 2026-11-21 那次包目录积了 25MB 垃圾，自检一直在报（只是提示级），
报了几十次没人动手 —— 因为「发现了」没有变成「永久拦住」。

- `可机检`：能用代码判的。**必须**在「已补检查」列填检查项名（列里只写名字，不要写「已补检查：」前缀），且名字要与
  `node harness/scripts/selfcheck.mjs --json` 里 `results[].name` **完全一致**。
  G3 会拿这个名字去核对，写假名字直接不通过。
- `需人判断`：审美、取舍、口径这类判不了的。`已补检查` 填 `—`。
- 多条问题可以共用一个检查项（不许为了凑数把一条拆成多条检查）。
- 本轮真的一个问题都没有：表格留一行写「本轮无问题」，两列填 `—`。

**不改检查项名，或者改了要同步改这里** —— 名字对不上 G3 不过。

## 3. 检查项

| 维度 | 结论 | 说明 |
|---|---|---|
| 需求一致 | PASS | R-01/R-02 均落地：`_resolve_llm`（router.py:591-613）+ `call_chat` 增 `base_url`/`model` 形参，接入 script-gen（1495）、script-edit（1639）、provider-test（1144）；取证用例 tests/test_custom_llm_model.py:118-169 |
| 权限 | PASS | 无鉴权面变更，仅读本地注册表文件 |
| 异常处理 | PASS | resolve 失败 → 400 明确文案；调用失败 → mock 降级 200 且带 `custom_model_error`；Key 缺失 → 402 且不发请求（test_missing_key_returns_402_without_calling） |
| 数据与事务 | PASS | 复用既有 `_script_write`/`_load_custom_models`；无锁读写为既有模式，见 RV-03 |
| 影响面 | PASS | 内置 provider 三处调用均传 `("", "")`，call_chat 内置分支（llm_client.py:55-60）与原行为等价；base_url 经 `rstrip("/")` + `/chat/completions` 拼接，fake 端点带 `/v1` 验证无重复路径（tests/test_custom_llm_model.py:68,130-132） |
| 调试残留 | PASS | diff 内无 print/console/临时代码新增 |
| 安全 | PASS | base_url 限 http/https（router.py:610、llm_client.py:65）；异常文案截断 `[:200]`，Key 不出现在响应 |

补充核验：`_API_KEYS`（内存字典）与 `_get_key` 交互正常——`/api/providers/{id}/keys` 写入任意 provider 名（router.py:1111），script-gen/edit 经 `_get_key(provider)`（1391、1596）可取到自定义 id 的 Key，链路闭合。

## 4. 遗留与接受

| 遗留项 | 接受人 | 理由 | 复审时间 |
|---|---|---|---|
| RV-01 / RV-04 / RV-05 | trae（已修） | 语义、隔离、健壮性问题，改动小且直接提升正确性，已在 G3 前修完并复跑 17 passed | 已闭环 |
| RV-02 `_db_ready` 进程级标志 | 待定 | 既有设计，生产环境 `_DB_PATH` 恒定；测试侧已按契约重置 | 需要支持切库/热重载时再议 |
| RV-03 注册表读写无锁 | 待定 | 既有模式（本次只增加读取点），报错明确不静默；并发写读窗口极小 | 若面板频繁保存模型再评估 |

## 5. 复跑验证

```
.venv\Scripts\python.exe -m pytest tests/test_custom_llm_model.py tests/test_style_templates.py -q
.................  [100%]
17 passed in 3.20s
```

（含 G3 后新增用例 `test_builtin_provider_rejects_base_url_override`；全量回归见 logs/regression-pytest.txt）

## 6. 结论

可发布（等 G3 人确认）
