// 跨浏览器巡检：Firefox / WebKit 各跑四屏 × 两视口，收集控制台报错与布局审计结果。
// 用法：node xb.mjs firefox [webkit...]
import fs from "node:fs";
import { createRequire } from "node:module";

// 仓库里没有 package.json（前端是原生 JS、零构建），playwright 装在外部目录，
// 用 PLAYWRIGHT_HOME 指过去：PLAYWRIGHT_HOME=E:/Temp/pwtest node tools/xb_check.mjs firefox
let pw = process.env.PLAYWRIGHT_HOME || "E:/Temp/pwtest";
pw = pw.split("\\").join("/");
if (!pw.endsWith("/")) pw += "/";
const require = createRequire(pw);
const { firefox, webkit } = require("playwright");

const BASE = process.env.UI_BASE_URL || "http://127.0.0.1:7861";
const AUDIT = fs.readFileSync(new URL("./ui_audit.js", import.meta.url), "utf8");
const SCREENS = ["workbench", "clone", "library", "script"];
const VIEWS = [
  [1440, 900],
  [1280, 800],
];
const TYPES = { firefox, webkit };

async function run(name) {
  const type = TYPES[name];
  let browser;
  try {
    browser = await type.launch({ timeout: 60000 });
  } catch (e) {
    return { browser: name, launchError: String(e).split("\n")[0].slice(0, 220) };
  }
  const result = { browser: name, screens: [], errors: [] };
  for (const [w, h] of VIEWS) {
    const ctx = await browser.newContext({ viewport: { width: w, height: h } });
    const page = await ctx.newPage();
    const errs = [];
    page.on("console", (m) => {
      if (m.type() === "error") errs.push(m.text().slice(0, 200));
    });
    page.on("pageerror", (e) => errs.push("pageerror: " + String(e).slice(0, 200)));
    for (const sc of SCREENS) {
      await page.goto(`${BASE}/?screen=${sc}`, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(1500);
      let audit = {};
      try {
        audit = JSON.parse(await page.evaluate(AUDIT));
      } catch (e) {
        audit = { error: String(e).slice(0, 160) };
      }
      result.screens.push({
        vp: `${w}x${h}`,
        sc,
        layout: (audit.layout || []).length,
        a11y: (audit.a11y || []).length,
        overflowX: audit.pageOverflowX,
        layoutItems: (audit.layout || []).slice(0, 4),
        err: audit.error,
      });
      if (w === 1440) {
        await page.screenshot({ path: `F:/TTS2/docs/tech/screens/xb-${name}-${sc}.png` });
      }
    }
    result.errors.push(...errs);
    await ctx.close();
  }
  result.errors = [...new Set(result.errors)];
  await browser.close();
  return result;
}

const out = [];
for (const name of process.argv.slice(2)) out.push(await run(name));
console.log(JSON.stringify(out, null, 1));
