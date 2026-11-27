2026-11-27 | 经验 | 前后端契约 | 后端 mock_reason 用 str(exc)[:200] 从头部硬切，异常尾部 Errno/连接细节永久丢失；前端再按短长度二次截断会丢头部（实测只剩「…e」）。两端都该改为尾部保留 + 分隔符断开 | harness/requirements/REQ-20261127-03/logs/g3-fix-browser-verify.txt | - | 2027-05-26
2026-11-28 | 事件 | REQ-20261127-04 | REQ-20261127-04 G3 通过：审查 4 项（致命 0 / 建议 3），已修 0，遗留 3（RV-01/RV-02/RV-03） | harness\requirements\REQ-20261127-04\03-review.md | 仅本需求包 | 2027-05-27
2026-11-28 | 事件 | REQ-20261127-04 | REQ-20261127-04 G4 通过：发布步骤 7 条、回退相关 6 处，材料已留档 | harness\requirements\REQ-20261127-04\04-release.md | 仅本需求包 | 2027-05-27
