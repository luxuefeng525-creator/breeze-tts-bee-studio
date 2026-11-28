#!/usr/bin/env node
// 全新工程验收：在临时 git 工程里，用「宿主装的包」从零走一遍分发链路。
// 为什么要有它：本仓自检全绿 ≠ 换个工程还能用。历史上真实踩过的坑 ——
// init 少拷脚本、规则路径写错、包根少算一层、技能包位置没记，
// 这些只有「新建工程从零跑」才暴露得出来。手工拼命令会漏步骤，所以固化成一条命令。
//
// 用法：node scripts/verify-fresh.mjs [--pkg <技能包根>] [--keep] [--json]
//   --pkg  指定用哪个包做验收（默认：六宿主里已安装的 ~/.workbuddy/skills/ai-workflow）
//   --keep 保留临时工程（默认清理）
//   --json 机器可读输出
//
// 判据（六条链路全过才 exit 0）：
//   1 init        能在空工程里铺出 harness/ 全套（含需求包模板与规则）
//   2 selfcheck   新工程自检 0 阻断
//   3 begin       开工会话能出 READY（不是 NOT-READY）
//   4 skill       能读到技能正文（且指向真实文件）
//   5 route       人话能路由到技能
//   6 install-hook 能装上 git hook
//
// 退出码：0 全过 · 1 有链路失败 · 2 环境不具备（没装包 / 没 git / 没 node）
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const argv = process.argv.slice(2);
const AS_JSON = argv.includes('--json');
const KEEP = argv.includes('--keep');
const pkgIdx = argv.indexOf('--pkg');
const PKG_OVERRIDE = pkgIdx >= 0 && argv[pkgIdx + 1] ? path.resolve(argv[pkgIdx + 1]) : null;

/** 找一个已安装的技能包根（宿主安装优先 —— 验收的是「装出来的那份」，不是仓库里的源） */
function findInstalledPkg() {
  if (PKG_OVERRIDE) return PKG_OVERRIDE;
  const home = os.homedir();
  const hosts = ['workbuddy', 'claude', 'codex', 'deepseek', 'trae-cn', 'reasonix'];
  for (const h of hosts) {
    const p = path.join(home, '.' + h, 'skills', 'ai-workflow');
    if (fs.existsSync(path.join(p, 'skills', 'harness', 'engine', 'harness.mjs'))) return p;
  }
  return null;
}

/** 在临时目录跑一条命令，返回 { code, out } */
function run(cmd, args, cwd) {
  const r = spawnSync(cmd, args, { cwd, encoding: 'utf8', timeout: 120000, shell: false });
  if (r.error) return { code: -1, out: String(r.error.message || r.error) };
  return { code: r.status === null ? -1 : r.status, out: ((r.stdout || '') + (r.stderr || '')).trim() };
}

function step(name, fn) {
  const t0 = Date.now();
  const r = fn();
  return { name, ok: !!r.ok, detail: r.detail || '', ms: Date.now() - t0 };
}

const pkg = findInstalledPkg();
if (!pkg) {
  console.error('环境不具备：找不到已安装的 ai-workflow 技能包。');
  console.error('装法：把分发包解压到 ~/.workbuddy/skills/ai-workflow，或用 --pkg 指定包根。');
  process.exit(2);
}
const engine = path.join(pkg, 'skills', 'harness', 'engine', 'harness.mjs');
if (!fs.existsSync(engine)) {
  console.error(`环境不具备：包里没有引擎 ${engine}`);
  process.exit(2);
}
if (spawnSync('git', ['--version'], { encoding: 'utf8' }).status !== 0) {
  console.error('环境不具备：需要 git。');
  process.exit(2);
}

const workRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'aw-verify-fresh-'));
const proj = path.join(workRoot, 'proj');
fs.mkdirSync(proj, { recursive: true });

let steps = [];
let env = { ok: true, detail: '' };

try {
  run('git', ['init', '-q'], proj);

  // 1 init
  steps.push(step('init', () => {
    const r = run(process.execPath, [engine, 'init', '全新工程验收', '--dir', proj], proj);
    if (r.code !== 0) return { ok: false, detail: `exit=${r.code} ${r.out.split('\n').slice(-3).join(' / ')}` };
    const must = [
      ['harness', 'SKILL.md'],
      ['harness', 'references', 'rules.md'],
      ['harness', 'templates', 'requirement.md'],
      ['harness', 'scripts', 'selfcheck.mjs'],
      ['harness', 'engine', 'harness.mjs']
    ];
    const missing = must.filter(seg => !fs.existsSync(path.join(proj, ...seg)));
    if (missing.length) return { ok: false, detail: `init 后缺 ${missing.map(m => m.join('/')).join(', ')}` };
    return { ok: true, detail: '铺出 harness/ 全套' };
  }));

  // 2 selfcheck —— 必须在工程目录里跑，cwd 决定检查根
  steps.push(step('selfcheck', () => {
    const r = run(process.execPath, [path.join(proj, 'harness', 'scripts', 'selfcheck.mjs')], proj);
    const m = r.out.match(/阻断\s*(\d+)\s*·\s*提示\s*(\d+)\s*·\s*放行\s*(\d+)/);
    if (!m) return { ok: false, detail: `输出无法解析：${r.out.split('\n').slice(-3).join(' / ')}` };
    if (Number(m[1]) > 0) {
      const blocks = r.out.split('\n').filter(l => l.includes('✖')).map(l => l.trim());
      return { ok: false, detail: `阻断 ${m[1]}：${blocks.slice(0, 3).join(' ; ') || '见输出'}` };
    }
    return { ok: true, detail: `阻断 ${m[1]} · 提示 ${m[2]} · 放行 ${m[3]}` };
  }));

  // 3 begin
  steps.push(step('begin', () => {
    const r = run(process.execPath, [path.join(proj, 'harness', 'engine', 'harness.mjs'), 'begin', '帮我美化一下这个页面'], proj);
    if (!r.out.includes('READY')) return { ok: false, detail: r.out.split('\n').slice(-2).join(' / ') || '无输出' };
    if (r.out.includes('NOT-READY')) return { ok: false, detail: '输出含 NOT-READY' };
    return { ok: true, detail: (r.out.match(/READY[^\n]*/) || ['READY'])[0].slice(0, 70) };
  }));

  // 4 skill —— 要能读到正文，且指向真实文件
  steps.push(step('skill', () => {
    const r = run(process.execPath, [path.join(proj, 'harness', 'engine', 'harness.mjs'), 'skill', 'ui-design'], proj);
    if (r.code !== 0) return { ok: false, detail: `exit=${r.code} ${r.out.split('\n').slice(-2).join(' / ')}` };
    if (r.out.includes('未找到技能')) return { ok: false, detail: '未找到技能：包位置没记对' };
    // 从「读这个文件：<路径>」取路径。注意路径可能带盘符（C:/…），不能按冒号切。
    const line = (r.out.split('\n').find(l => l.includes('读这个文件')) || '');
    const f = line.split('：').slice(1).join('：').trim();
    if (!f || !fs.existsSync(f)) return { ok: false, detail: `输出里没有可读文件：${line.trim() || r.out.split('\n')[0]}` };
    return { ok: true, detail: `ui-design → ${path.basename(f)}` };
  }));

  // 5 route —— 人话要能路由到技能
  steps.push(step('route', () => {
    const r = run(process.execPath, [path.join(proj, 'harness', 'scripts', 'route.mjs'), '帮我美化一下这个页面'], proj);
    if (r.code !== 0) return { ok: false, detail: `exit=${r.code} ${r.out.split('\n').slice(-2).join(' / ')}` };
    if (!/\d+\.\s*\S+/.test(r.out)) return { ok: false, detail: `无路由结果：${r.out.slice(0, 60)}` };
    return { ok: true, detail: (r.out.split('\n')[0] || '').trim() };
  }));

  // 6 install-hook
  steps.push(step('install-hook', () => {
    const r = run(process.execPath, [path.join(proj, 'harness', 'engine', 'harness.mjs'), 'install-hook'], proj);
    const okCount = (r.out.match(/OK\s+已写/g) || []).length;
    if (okCount < 3) return { ok: false, detail: `只装上 ${okCount}/3 个 hook` };
    return { ok: true, detail: '3 个 hook 装好' };
  }));
} finally {
  if (!KEEP) {
    try { fs.rmSync(workRoot, { recursive: true, force: true }); } catch { /* 清理失败不影响结论 */ }
  }
}

const failed = steps.filter(s => !s.ok);
const result = {
  tool: 'verify-fresh',
  pkg,
  kept: KEEP ? workRoot : null,
  total: steps.length,
  passed: steps.length - failed.length,
  failed: failed.length,
  steps: steps.map(s => ({ name: s.name, ok: s.ok, detail: s.detail, ms: s.ms }))
};

if (AS_JSON) {
  console.log(JSON.stringify(result, null, 2));
} else {
  console.log(`全新工程验收 · 包 ${pkg}`);
  for (const s of result.steps) console.log(`${s.ok ? 'OK  ' : 'FAIL'} ${s.name}: ${s.detail}`);
  if (KEEP) console.log(`临时工程保留在：${workRoot}`);
  console.log(`\n${failed.length ? 'FRESH-VERIFY BLOCKED' : 'FRESH-VERIFY READY'} · ${result.passed}/${result.total} 链路通过`);
  if (failed.length) console.log('失败链路：' + failed.map(f => f.name).join(', ') + ' —— 换工程/换宿主后再发布。');
}

process.exit(failed.length ? 1 : 0);
