---
req_id: REQ-20261127-03
stage: requirement
status: active
owner: <维护者>
updated: 2026-11-27
size: S | M | L
entry: /harness init
hits: ["ui-design", "diagnose"]
---

# 状态

## 1. 当前进度

> 只记工作项，不复述门禁结论。门禁唯一来源是 §4 门禁记录；本节不要写 `- [ ] G1 门禁` 这类门禁勾选，否则同一状态两处表示必然漂移。

- [x] 需求包建立 + 域沉淀（ai-degradation-notice）
- [x] 前端降级提示实现（bee-studio.html：truncateReason / 降级 warn / edit 失败上下文）
- [x] 静态守卫测试 tests/test_bee_studio_notice.py（8 例，含 catch/apiBT 用例）
- [x] G3 审查 + 两项阻断整改（apiBT 带 payload、catch 置降级标志、截断策略）
- [x] 整改后浏览器复验（AC-01/AC-02，4 轮）
- [ ] V 环截图（UI 需求强制，需人工接管截图）
- [ ] G4 收口 + 合流 main

## 2. 阻塞

| 阻塞项 | 等什么 | 谁能解 | 记录时间 |
|---|---|---|---|
| 无 | — | — | — |

## 3. 已验证

| 项 | 方法 | 结果 | 证据 |
|---|---|---|---|
| AC-01 降级原因可见 | 浏览器真实操作 + DOM 采样 | PASS | `harness/requirements/REQ-20261127-03/logs/browser-verify.txt` |
| AC-02 未知 provider | 注入 no-such-model 走 catch 分支，浏览器捕获 toast | PASS | `harness/requirements/REQ-20261127-03/logs/g3-fix-browser-verify.txt`（轮次 4） |
| AC-03 不误报 | 成功路径 + force_mock 防御分支判定 | PASS | `logs/browser-verify.txt` + 静态断言 |
| AC-04 edit 失败上下文 | `POST /api/llm/script-edit` 502 取证 + 前端拼接静态断言 | PASS | `harness/requirements/REQ-20261127-03/logs/ac04-edit-502.txt` |
| 回归 | `pytest tests/test_bee_studio_notice.py tests/test_ui_contract.py -q` | PASS 10 passed | 见 §9 |

## 4. 门禁记录

> 每道门禁一行，格式固定，字段不可增删改名。缺字段、为空或写 `<TBD>` 一律判 FAIL。
> agent 只能写 `FAIL` / `BLOCKED` / `待确认`；写 `PASS` 必须同时有人的确认原文。

```text
G1: PASS | 确认人: 用户 | 时间: 2026-11-27 09:05 | 确认原文: "G1 通过（推荐）"
G2: PASS | 确认人: 用户 | 时间: 2026-11-27 17:34 | 确认原文: "G2 通过（推荐）"
G3: PASS | 确认人: 用户 | 时间: 2026-11-27 18:24 | 确认原文: "G3 通过（推荐）"
G4: 未开始 | 确认人: — | 时间: — | 确认原文: "<TBD>"
V: 待确认 | 参考: 1 张已冻结（visual/ref/ref-before-default-1440x900.png） | 视口: 1440x900 / 768x1024 / 375x812 | 状态: 默认（降级态以 DOM 快照为准） | 截图: 3 张 | 确认人: — | 时间: — | 确认原文: "<TBD>"
```

追加记录（不覆盖历史）：

| 门禁 | 结论 | 触发动作 | 证据路径 |
|---|---|---|---|
| G1 | PASS | 人确认 | — |
| G2 | PASS | 人确认 + pytest/浏览器取证 | `harness/requirements/REQ-20261127-03/logs/` |
| G3 | PASS | 独立只读审查 + 复审 + 人确认 | `harness/requirements/REQ-20261127-03/03-review.md` |

## 5. 未验证项

| 项 | 原因 | 影响 | 补验条件 |
|---|---|---|---|
| <TBD> | <TBD> | <TBD> | <TBD> |

## 6. 下一步

1. G3 复审：由原审查者复核两项阻断 + 实测发现的截断缺陷是否闭环
2. V 环：冻结参考图 + 三视口截图（1440×900 / 768×1024 / 375×812）+ V 人确认；截图工具在本环境不可用，需人工接管
3. 更新本文件并重跑 `gate REQ-20261127-03 --stage G3/G4 --dir f:\TTS2\breeze-tts-trae`
4. G4 通过后合流 main 并清理工作树

## 7. 交接摘要（可选）

- 上次执行到：<TBD>
- 关键判断：<TBD>
- 接手注意：<TBD>

## 8. 清理记录

> 只记本需求产生的清理；C 类永不自动删除。

| 路径 | 级别 | 依据 | 动作 | 时间 | 确认人 |
|---|---|---|---|---|---|
| <路径> | A / B / C | <为什么能删/为什么要留> | 删除 / 迁移 / 保留 | <YYYY-MM-DD HH:MM> | <人名或 自动> |

## 9. 自检记录

| 步骤 | 命令 | 退出码 | 证据路径 |
|---|---|---|---|
| 1 | `f:\TTS2\breeze-tts\.venv\Scripts\python.exe -m pytest tests/test_bee_studio_notice.py tests/test_ui_contract.py -q`（cwd=工作树） | 0 | 10 passed |
| 2 | `node harness\engine\harness.mjs gate REQ-20261127-03 --stage G2 --dir f:\TTS2\breeze-tts-trae` | 0 | PASS |
| 3 | G3 整改后 `pytest tests/test_bee_studio_notice.py tests/test_ui_contract.py -q` | 0 | 12 passed |
| 4 | 浏览器复验 AC-01/AC-02（代理 7862，4 轮） | 0 | `logs/g3-fix-browser-verify.txt` |
