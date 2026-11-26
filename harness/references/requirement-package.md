# 需求包规范

## 1. 定位

一个需求一个包，包即该需求的团队知识库。换人、换工具、换 agent，读这个包就能接手。

## 2. 目录布局

```text
<目标工程>/harness/
├─ rules/
│  ├─ must.md
│  ├─ should.md
│  └─ can.md
├─ requirements/
│  └─ REQ-YYYYMMDD-xx/
│     ├─ 00-requirement.md
│     ├─ 01-design.md
│     ├─ 02-test.md
│     ├─ 03-review.md
│     ├─ 04-release.md
│     └─ state.md
└─ memory/
   └─ long-term.md
```

命名：`REQ-` + 需求创建日期 + 两位序号；同一天第二个需求为 `-02`。需求 ID 一经创建不再修改。

## 3. 文件职责

| 文件 | 只写什么 | 不写什么 |
|---|---|---|
| `00-requirement.md` | 需求、验收点、边界、不做项、同行对标、多端矩阵 | 实现方案 |
| `01-design.md` | 方案、取舍、影响面、回退思路 | 测试结果 |
| `02-test.md` | 验收点 ↔ 用例 ↔ 结果 ↔ 证据路径、未验证项 | 审查意见 |
| `03-review.md` | 审查发现、处理、遗留 | 开发自述 |
| `04-release.md` | 发布步骤、回退步骤、监控点 | 新需求 |
| `state.md` | 进度、阻塞、已验证、下一步 | 长篇设计 |

## 4. 需求写法

句式固定：

```text
当 <触发条件> 时，系统应 <可观测行为>，以便 <目的>。
验收点 AC-01：<可判定的检查方式>
```

规则：

- 一条需求至少一个验收点，编号 `AC-01` 递增；
- 验收点必须能被测试或人工复现判定；
- 边界（空数据、重复操作、无权限、超时）单列为需求条目，不埋在正文里；
- 明确「不做」项，避免范围膨胀。

## 5. 元数据（每份文档头部）

```yaml
---
req_id: REQ-20261119-01
stage: requirement | design | develop | test | review | release | accept
status: active | passed | failed | blocked | accepted | archived
owner: <维护者>
updated: YYYY-MM-DD
acceptance: [AC-01, AC-02]
---
```

`state.md` 额外字段见 `memory.md`。

## 6. 生命周期

```text
创建 → 需求确认 → 设计确认 → 开发 → 测试 → 审查 → 发布准备 → 验收
                                                        ↓
                                                    阻塞 → 解除后回到阻塞前阶段
```

- 阶段推进只改 `state.stage`，不改历史文档结论。
- 需求变更：新增条目或标注「已废弃」，不删除原文。
- 验收后状态置 `accepted`；需求包保留，不随分支删除。

## 7. 写入边界

- 需求包只由当前需求的工作写；跨需求复用内容写进 `harness/memory/long-term.md`。
- 代码仓库的约定（目录、命令、端口）写进 `harness/rules/`，不散落在需求包里。
- 任何密钥、令牌、个人数据不得写入需求包。

## 8. 最小可用标准

一个需求包达到「可接手」需满足：

1. `state.md` 的下一步明确到「打开哪个文件、改什么、跑什么验证」；
2. 每个验收点在 `02-test.md` 有对应结果或未验证说明；
3. 每条需求在 `00-requirement.md` 有可追溯的同行对标行（来源为 URL 或本地证据文件）；
4. 审查问题要么关闭，要么写明接受理由；
5. 发布与回退步骤可被第二个人照着执行。
