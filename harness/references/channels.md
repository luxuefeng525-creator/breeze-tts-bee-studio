# 验证通道

> 目的：把「我验证过了」变成「哪条通道、跑了什么命令、产物在哪」。
> 原则：**先探测 → 再执行 → 留证据**。通道不可用就 `BLOCKED`，不得用别处的截图或口头描述代替。

## 1. 先探测

```bash
node harness/engine/harness.mjs probe
node harness/engine/harness.mjs probe --json          # 机读，便于 CI
node harness/engine/harness.mjs probe --module-dir <装了 playwright 的项目>
```

判读：

| 输出 | 含义 |
|---|---|
| `OK playwright` | 可从该项目解析到浏览器自动化，能做 DOM 断言与截图 |
| `OK 系统浏览器` | 有 Chrome/Edge，可配合 `--channel chrome` 使用 |
| `OK wechatide` | 找到微信开发者工具 CLI，可做小程序预览与自动化 |
| `NO ...` | 该通道不可用；声明了它就必须 `BLOCKED` |

## 2. 浏览器通道（Web / H5 / 后台）

- 视觉与溢出：[scripts/verify-visual.mjs](../../../scripts/verify-visual.mjs)（1920×1080 截图 + 控制台错误 + 横向溢出）
- 交互与断言：[scripts/browser.mjs](../../../scripts/browser.mjs)（点击、填表、断言文本/可见/URL）
- 证据清单：
  - 截图 `logs/<REQ>/<AC>-<page>.png`（须为真实图片，引擎校验文件头）
  - 控制台与失败请求 `logs/<REQ>/<AC>-console.json`
  - 命令与退出码写进 `02-test.md` 的「步骤 / 证据」列
- 常见失败：playwright 未安装、缺少 `npx playwright install chromium`、端口未起、跨域登录态缺失。缺一项就 `BLOCKED` 写明。

## 3. 小程序通道（微信）

本包不分发 `wechatide-skill`；按需加载它，由它调用微信开发者工具 CLI 产出证据。

- 前置：安装微信开发者工具，并在「设置 → 安全 → 服务端口」开启命令行；或设环境变量 `WECHATIDE_CLI=<cli.bat 路径>`。
- 典型命令（以实际 CLI 为准，不猜参数）：
  - 打开项目：`cli.bat open --project <小程序目录>`
  - 预览：`cli.bat preview --project <小程序目录>`
  - 自动化：通过 wechatide-skill 的 automator 能力跑页面操作与截图
- 证据清单：
  - CLI 输出日志 `logs/<REQ>/<AC>-wechatide.log`（含命令、返回码、二维码路径）
  - 页面截图 `logs/<REQ>/<AC>-<page>.png`
  - 编译错误 / 控制台输出，若有
- 常见失败：未登录开发者工具、服务端口未开、CLI 路径不在默认目录、项目 appid 不匹配、工具已在运行导致端口占用。任一项未解决 → `BLOCKED`。

## 4. 桌面通道（无法脚本化的原生界面）

- 使用宿主的 computer-use 能力操作真实桌面，截图留证。
- 仅作兜底：能用浏览器或 CLI 的，不许走桌面。
- 证据：截图 `logs/<REQ>/<AC>-desktop.png` + 操作步骤说明；无法复现就 `BLOCKED`。

## 5. 证据落盘约定

- 统一放项目内 `logs/<REQ>/`，文件名带 AC 编号或覆盖取值，便于门禁与人工核对。
- 一个 AC / 一个覆盖取值至少一份证据，不允许一份证据覆盖多项。
- 图片必须能打开；改名的文本、0 字节文件、旧截图复用都会被门禁拒绝。
- 证据是交付物的一部分，随需求包保留；不要放到临时目录。

## 6. 通道与证据对照

| 通道 | 最小证据 | 机判点 |
|---|---|---|
| 浏览器 | 截图 + 控制台日志 | 文件存在、非空、图片文件头正确 |
| 小程序 | CLI 日志 + 截图 | 同上；CLI 不可用则 G2/G3 FAIL |
| 桌面 | 截图 + 步骤 | 同上；不可探测时由人确认 |

## 7. 内容校验（防「少了组件也通过」）

截图只能证明「页面上有东西」，证明不了「该有的都有」。V 门禁支持按页面声明期望项：

```json
{
  "pages": [
    { "name": "home", "path": "/", "states": ["default", "empty"],
      "expect": ["class:restockCard", "text:补货", "count:rowCard>=15"],
      "emptyAllowed": ["补货卡"],
      "expectFrom": "prototype/pages/home.js" }
  ]
}
```

- `expect` 支持四种写法：`class:类名`（匹配 class 属性）、`text:文案`（可见文本）、`count:片段>=N`（出现次数）、裸 token（原始 HTML 子串，兼容旧写法）；
- `expectFrom`：期望清单的来源文件或目录；首次需人工过目后运行 `gate V --approve` 锁定 sha1，之后原型一改就提示「清单需重评」；
- `emptyAllowed`：空态下允许缺失的项；写 `*` 表示该页空态不校验正文长度；
- 证据：`visual/dom/<page>-<state>.html`（或用 `browser.mjs` 的 `{"html":"..."}` 步骤直接落盘）；
- 机判：缺快照、快照为空、疑似空页（无可见正文且未声明例外）、缺任一期望项 → `FAIL`；**空数据不再自动 PASS**。

期望清单怎么来（这是最容易漏的地方）：

0. 先跑 `node harness/engine/harness.mjs inventory --from <原型目录> --write` 生成候选（组件函数、class、文案），**人工删减**后写入 `expect`；
1. 不要只扫「页面函数」里的容器类名——有些组件由 `restockCard()`、`rowCard()` 这类辅助函数拼出来，不在页面函数里；
2. 把辅助函数、组件工厂、循环模板一起纳入扫描，输出「页 → 组件清单」；
3. 空数据时不要跳过检查，要么组件仍在（空态占位），要么在 `emptyAllowed` 里声明并写理由；
4. 清单有变更就重跑，不要复用上一次的旧清单。

