---
req_id: REQ-20261127-01
stage: test
status: active
owner: trae
updated: 2026-11-27
---

# 测试记录

## 1. 环境

- 代码版本：分支 session/trae-20261127（基于 main 66fcf9d），工作树 f:\TTS2\breeze-tts
- 场景：localhost 已登录态不适用（本机单用户无登录）；面板 7861 + ComfyUI 8188 在线；未注入 token，未带版本参数
- 入口与参数：浏览器 http://127.0.0.1:7861/bee-studio.html；接口 GET /api/bee/style-templates、POST /api/llm/script-gen（style_id=cyberpunk, force_mock=true）
- 运行方式：`.venv\Scripts\python.exe -m pytest -q`；`node e:\Temp\trae\bee_probe_style.mjs`（Playwright Chromium）
- 数据准备：mock 项目固定 project_id=proj-style-e2e-001，测后从 data/bee_tasks.db 的 scripts 表删除；不产生其它落盘数据

## 2. 验收点结果

| 验收点 | 场景 | 步骤 | 预期 | 实际 | 结论 | 证据 |
|---|---|---|---|---|---|---|
| AC-01 | 打开新建项目弹窗 | 点「＋新建项目」，等模板加载 | ≥6 个风格且文案来自接口 | 6 个：cinematic/cyberpunk/anime/documentary/product/vertical，含名称与说明 | PASS | `harness/requirements/REQ-20261127-01/logs/ac01-ac04-style-ui.json` |
| AC-02 | style_id 注入与 mock 落库 | POST script-gen force_mock=true style_id=cyberpunk | 响应与每个 shot 带 style_id；注入由单测取证、落库由实调日志取证 | resp.style_id=cyberpunk；shot.style_ids=[cyberpunk,cyberpunk]；system prompt 含风格约束（单测） | PASS | `harness/requirements/REQ-20261127-01/logs/ac02-mock-api.txt` |
| AC-03 | 总时长/风格联动建议镜头数 | 默认 cinematic + 60s；切 vertical | 60s→10（cinematic）；vertical 按 5s/镜 | shots60=10；切 vertical 后=12；单测覆盖 15/30/60/90 与非法值 | PASS | `harness/requirements/REQ-20261127-01/logs/ac02-ac03-pytest.txt` |
| AC-04 | 浏览器端到端 | 打开弹窗→看选项/说明→改时长 | 选项与说明可见、时长改动刷新镜头数 | styleCount=6；desc 非空；时长改动后 #scriptShots 更新 | PASS | `harness/requirements/REQ-20261127-01/logs/ac04-style-modal.png` |
| AC-05 | 无 Key 出片提示真实化 | 读 generateVideo 无 Key 分支与批量按钮文案 | 不再宣称自动 Mock；给出本地/配 Key 出路 | 两处新文案已在 2354/3410 行；未点击批量按钮（避真实出片） | PASS | `harness/requirements/REQ-20261127-01/logs/ac05-copy-check.txt` |
| AC-06 | 部分镜头无视频时导出 | 切到 proj-1795613185（2 镜头，1 无视频）点导出 | 先警告跳过数量再合成 | toast：「1 个镜头还没有视频，本次只导出已有视频的片段」 | PASS | `harness/requirements/REQ-20261127-01/logs/ac06-export-warn.json` |

## 3. 边界与异常

| 类型 | 场景 | 结果 | 证据 |
|---|---|---|---|
| 空数据 | 模板文件缺失 | 回退内置 6 项，load_templates()==_builtin() | PASS | `harness/requirements/REQ-20261127-01/logs/ac02-ac03-pytest.txt` |
| 空数据 | 模板文件损坏（非法 JSON） | 回退内置，不抛错 | PASS | `harness/requirements/REQ-20261127-01/logs/ac02-ac03-pytest.txt` |
| 重复操作 | 同一风格重复提交 | style id 去重校验通过，响应幂等 | PASS | `harness/requirements/REQ-20261127-01/logs/ac02-ac03-pytest.txt` |
| 依赖失败 | 接口不可达时前端 | 回退 FALLBACK_STYLES，弹窗仍可用（探针 errors=[]） | PASS | `harness/requirements/REQ-20261127-01/logs/ac01-ac04-style-ui.json` |
| 极端值 | 总时长 0/负数/非数字、shot_seconds=0 | suggest_shot_count 返回 None，前端保持原选择 | PASS | `harness/requirements/REQ-20261127-01/logs/ac02-ac03-pytest.txt` |
| 未知风格 | get_template("no-such") | 落到默认 cinematic，不返回 None | PASS | `harness/requirements/REQ-20261127-01/logs/ac02-ac03-pytest.txt` |

## 4. 覆盖值逐项结论

| 覆盖值 | 结论 | 证据 |
|---|---|---|
| 风格/电影感 | PASS | `harness/requirements/REQ-20261127-01/logs/ac01-ac04-style-ui.json` |
| 风格/赛博朋克 | PASS | `harness/requirements/REQ-20261127-01/logs/ac02-mock-api.txt` |
| 风格/日系动画 | PASS | `harness/requirements/REQ-20261127-01/logs/ac01-ac04-style-ui.json` |
| 风格/写实纪录片 | PASS | `harness/requirements/REQ-20261127-01/logs/ac01-ac04-style-ui.json` |
| 风格/产品广告 | PASS | `harness/requirements/REQ-20261127-01/logs/ac01-ac04-style-ui.json` |
| 风格/竖屏短视频 | PASS | `harness/requirements/REQ-20261127-01/logs/ac01-ac04-style-ui.json` |

## 5. 回归

| 受影响功能 | 验证方式 | 结果 |
|---|---|---|
| script-gen 既有调用（不带 style_id） | 全量 pytest | PASS，621 passed；证据 `harness/requirements/REQ-20261127-01/logs/regression-pytest.txt` |
| Bee Studio 弹窗/导出/持久化 | A 段实测已建证据 + 本轮全量测试 | PASS | `harness/requirements/REQ-20261127-01/logs/ac04-style-modal.png` |

## 6. 未验证项

| 项 | 未验证原因 | 影响 | 补验条件 |
|---|---|---|---|
| 真实 LLM 下的风格一致性 | LongCat 当前 402 Payment Required，用户明确不充值 | 只能证明「约束已进入 prompt」，不能证明模型输出服从 | 有可用 Key 时复跑 script-gen 并人工看片 |
| 视频自动剪辑/拼接（C3） | 已实现且 A 段实测导出 PASS，不在本期范围 | 无 | 需要时单独验收 |

## 7. 结论

- 验收点通过率：6/6
- 是否可进入审查：是；B 段代码与证据齐备，等 G2 人确认后进 G3 独立审查。
