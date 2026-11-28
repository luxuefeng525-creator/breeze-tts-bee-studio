#!/usr/bin/env node
// ai-workflow 动手前自检：体检项目状态、记忆健康度与 6 宿主漂移。
//
// 默认只读；--brief 会把完整报告写入 ignored 的 harness/logs/selfcheck-latest.log。
// changeset consume 边界用例仍在系统临时目录自建自删。退出码 0 = 可以动手；1 = 有阻断项。
//
// 用法：
//   node scripts/selfcheck.mjs              跑全部检查组
//   node scripts/selfcheck.mjs --group hosts 只跑某一组
//   node scripts/selfcheck.mjs --json       输出 JSON（供门禁或 CI 消费）
//   node scripts/selfcheck.mjs --brief      只输出摘要，完整报告落日志
//
// 检查组：目录结构 / 版本一致性 / 记忆健康度 / 临时产物 / 未跟踪文件 / 宿主一致性 / 需求包阶段 / 技能审计 / 并发纪律
// 判定三档：阻断（必须修）· 提示（建议看看）· 放行（正常）
// 包级检查（版本一致性 / 宿主一致性 / 技能审计）在「未安装 ai-workflow 包」的工程里标不适用，不阻断。

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawn, spawnSync } from 'node:child_process';

const argv = process.argv.slice(2);
const wantJson = argv.includes('--json');
const wantBrief = argv.includes('--brief');
const groupArg = (() => { const i = argv.indexOf('--group'); return i >= 0 ? argv[i + 1] : null; })();

const SCRIPT_DIR = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));
const MAX_FILES = 5000;      // 单次文件扫描上限
const MAX_DEPTH = 4;         // 目录扫描深度上限
const MAX_DIRS = 500;        // 空目录扫描的目录数上限
const SUBPROCESS_TIMEOUT = 20000;

const results = [];
let currentGroup = '';
function group(name) { currentGroup = name; }
function ok(name, detail = '') { results.push({ group: currentGroup, name, level: 'ok', detail }); }
function info(name, detail = '') { results.push({ group: currentGroup, name, level: 'info', detail }); }
function block(name, detail = '') { results.push({ group: currentGroup, name, level: 'block', detail }); }

/* ---------- 活跃包判定（HANDOFF 指向谁） ----------
 * 抽成纯函数是为了能自测：这段判据写错的后果是「换工具接手直接做错任务」，
 * 而它只在某个包刚收口、其余包又都不动时才暴露 —— 靠日常运行撞不上，必须有回归用例。 */
function pickActivePack(packs) {
  // 已收口包不参与：收口动作会刷新 mtime，但它不该成为现场快照的目标
  const live = packs.filter(x => !x.closed);
  // 选「最近修改的包」而不是「最近的未收口包」—— 后者会被很久没动的遗留包抢占，
  // 让刚收口的包被判成「HANDOFF 指向错误」。
  // 回退时同样要排除 archive：archive 的语义是「归档 / 主动搁置」，空壳模板包正是用它退出活跃集合的，
  // 把它当现场会把下一个会话引向一个刻意不推进的包（实测：07/09/10 三个空壳搁置后 HANDOFF 指回了 REQ-20261123-09）。
  const candidates = live.length ? live : packs.filter(x => !/^archive$/i.test(x.stage));
  return [...(candidates.length ? candidates : packs)].sort((a, b) => b.t - a.t)[0] || null;
}

/* ---------- 备份残留命名判定（clean 与本自检共用口径） ----------
 * 两种历史命名都要认：memory.mjs 写 .bak.<时间戳>，bootstrap/引擎写 .bak-<时间戳>。
 * 只认其中一种的后果是另一类备份既不报警也永远清不掉（实测踩过）。 */
function isBakName(name) { return /\.bak[-.][0-9T-]+/i.test(name); }

/* ---------- 自测（--selftest） ----------
 * 只断言纯判据，不碰工程文件；输出形如 `SELFCHECK SELFTEST OK n 组`。 */
if (argv.includes('--selftest')) {
  const P = (name, stage, t, g4 = '未开始') => ({ name, stage, t, closed: /^PASS$/i.test(g4) || /^(archive|done|released)$/i.test(stage), g4 });
  const cases = [
    ['有未收口包时选它（不看 mtime）',
      [P('REQ-1', 'done', 900), P('REQ-2', 'develop', 500)], 'REQ-2'],
    ['全收口时选 mtime 最新的非 archive 包',
      [P('REQ-1', 'done', 900), P('REQ-2', 'released', 1000)], 'REQ-2'],
    ['全收口且候选含 archive —— 不得指向 archive（本次修复的回归点）',
      [P('REQ-1', 'archive', 2000), P('REQ-2', 'done', 900)], 'REQ-2'],
    ['G4 PASS 即视为收口（stage 仍是 develop 也退出 live，触发回退）',
      [P('REQ-1', 'develop', 900, 'PASS'), P('REQ-2', 'done', 500)], 'REQ-1'],
    ['全 archive —— 兜底返回一个包而不崩（此时无真实现场可指）',
      [P('REQ-1', 'archive', 900), P('REQ-2', 'archive', 1000)], 'REQ-2'],
    ['没有需求包 —— 返回 null（消费工程不生成 HANDOFF）',
      [], null],
  ];
  let bad = 0;
  for (const [label, packs, want] of cases) {
    const got = pickActivePack(packs);
    const g = got ? got.name : null;
    if (g !== want) { console.error(`✖ ${label} —— 期望 ${want}，实得 ${g}`); bad++; }
  }
  // .bak 口径：memory.mjs 生成 .bak.<时间戳>，bootstrap/引擎用 .bak-<时间戳>，两者都要算残留
  const bakCases = [
    ['认 memory 的 .bak.<时间戳> 命名（本次修复的口径错位）', 'inbox.md.bak.2026-11-21T14-23-27-887Z', true],
    ['认 bootstrap 的 .bak-<时间戳> 命名', 'engine.bak-2026-11-21T14-23-27-887Z', true],
    ['普通文件不算备份', 'state.md', false]
  ];
  for (const [label, name, want] of bakCases) {
    if (isBakName(name) !== want) { console.error(`✖ ${label} —— 期望 ${want}`); bad++; }
  }
  const total = cases.length + bakCases.length;
  if (bad) { console.error(`SELFCHECK SELFTEST FAILED ${bad}/${total}`); process.exit(1); }
  console.log(`SELFCHECK SELFTEST OK ${total} 组`);
  process.exit(0);
}


/* ---------- 定位项目根与包根 ---------- */
function hasProjectRoot(d) {
  try { return fs.existsSync(path.join(d, 'harness', 'harness.config.json')); } catch { return false; }
}
function findProjectRoot() {
  let d = process.cwd();
  for (let i = 0; i < 8; i++) {
    if (hasProjectRoot(d)) return d;
    const up = path.dirname(d);
    if (up === d) break;
    d = up;
  }
  let s = SCRIPT_DIR;
  for (let i = 0; i < 5; i++) {
    if (hasProjectRoot(s)) return s;
    const up = path.dirname(s);
    if (up === s) break;
    s = up;
  }
  return null;
}

const ROOT = findProjectRoot();
const PKG = (() => {
  if (!ROOT) return SCRIPT_DIR;
  const staged = path.join(ROOT, 'build', 'v4-staging');
  if (fs.existsSync(path.join(staged, 'SKILL.md'))) return staged;
  const inHarness = path.join(ROOT, 'harness');
  if (fs.existsSync(path.join(inHarness, 'scripts'))) return inHarness;
  return SCRIPT_DIR;
})();
/** 是否是「装了这个包的工程」—— 决定包级检查（版本 / 技能审计 / 宿主）是否适用 */
const IS_PKG_PROJECT = fs.existsSync(path.join(PKG, 'SKILL.md')) && fs.existsSync(path.join(PKG, 'skills'));
/** 技能生命周期保留目录：不是技能，不进 catalog/index/route，也不计入活跃技能数 */
const LIFECYCLE_DIRS = new Set(['in-progress', 'deprecated', '.releases']);

/** 在多候选位置里找一个脚本（包内 scripts/ → 工程内 harness/scripts/ → 同目录） */
function findScript(name) {
  const cands = [
    path.join(PKG, 'scripts', name),
    path.join(path.dirname(SCRIPT_DIR), 'scripts', name),
    path.join(SCRIPT_DIR, name)
  ];
  return cands.find(p => fs.existsSync(p)) || null;
}

function readText(p) { try { return fs.readFileSync(p, 'utf8'); } catch { return ''; } }
function readJson(p) { try { return JSON.parse(readText(p)); } catch { return null; } }
function runNode(args, cwd = PKG, timeout = SUBPROCESS_TIMEOUT) {
  try {
    const r = spawnSync(process.execPath, args, { cwd, encoding: 'utf8', timeout });
    return { code: r.status ?? 1, out: (r.stdout || '') + (r.stderr || '') };
  } catch (e) { return { code: 1, out: String(e && e.message || e) }; }
}
/** 异步版：给「并发预取自测」用；语义与 runNode 对齐（code/out），多处调用点不改判断逻辑。 */
function runNodeAsync(file, args, cwd, timeout = SUBPROCESS_TIMEOUT + 5000) {
  return new Promise((resolve) => {
    let out = '', settled = false;
    const done = (code) => { if (settled) return; settled = true; clearTimeout(timer); resolve({ code, out }); };
    const p = spawn(process.execPath, [file, ...args], { cwd, encoding: 'utf8' });
    const timer = setTimeout(() => { try { p.kill(); } catch { } done(1); }, timeout);
    p.stdout.on('data', d => { out += d; });
    p.stderr.on('data', d => { out += d; });
    p.on('error', e => { out += String(e && e.message || e); done(1); });
    p.on('close', c => done(c ?? 1));
  });
}
/* 自测并发预取清单：[结果标签, 脚本文件名, 参数] —— 互相独立，串行跑就是白等。
 * 实测 memory --selftest 4.3s、next-scene 4.7s，两项就占了自检一半耗时。 */
const SELFTEST_JOBS = [
  ['观察流', 'observe.mjs', ['--selftest', '--root', ROOT]],
  ['记忆闭环', 'memory.mjs', ['--selftest', '--root', ROOT]],
  ['现场生成器', 'next-scene.mjs', ['--selftest', '--root', ROOT]],
  ['发布收口', 'release-closure.mjs', ['--selftest']],
  ['风险分档', 'risk-tier.mjs', ['--selftest']],
  ['失败归因', 'failure-reason.mjs', ['--selftest']],
  ['度量', 'metrics.mjs', ['--selftest']],
  ['写侧守卫', 'write-guard.mjs', ['--selftest']],
];
/** 直接跑外部可执行文件（如 git）—— 不能用 runNode，那会用 node 去跑 git */
function runBin(bin, args, cwd = ROOT, timeout = SUBPROCESS_TIMEOUT) {
  try {
    const r = spawnSync(bin, args, { cwd, encoding: 'utf8', timeout, shell: false });
    return { code: r.status ?? 1, out: (r.stdout || '') + (r.stderr || '') };
  } catch (e) { return { code: 1, out: String(e && e.message || e) }; }
}
function listDirs(d) { try { return fs.readdirSync(d, { withFileTypes: true }).filter(e => e.isDirectory()).map(e => e.name); } catch { return []; } }
function listFiles(d) { try { return fs.readdirSync(d, { withFileTypes: true }).filter(e => e.isFile()).map(e => e.name); } catch { return []; } }
/** 估算 token：CJK 约 1 字 1 token，其余约 4 字符 1 token（与 build-skills-index.mjs 同一口径） */
function estTokens(text) {
  let cjk = 0, other = 0;
  for (const ch of String(text)) {
    if (/[一-鿿　-〿＀-￯]/.test(ch)) cjk++;
    else other++;
  }
  return Math.round(cjk + other / 4);
}

/* ========== 组 1：目录结构 ========== */
group('目录结构');
{
  if (!ROOT) {
    // 不在 harness 工程内（如在宿主包目录里跑）→ 跳过，不是阻断
    info('项目根未识别', '未找到 harness/harness.config.json —— 不在 harness 工程内，跳过项目级检查');
  } else {
    const required = [
      ['harness', '需求包工作区'],
      ['harness/harness.config.json', 'harness 配置'],
      ['harness/engine/harness.mjs', '引擎'],
      ['harness/scripts/selfcheck.mjs', '自检脚本'],
      ['harness/scripts/memory.mjs', '记忆脚本']
    ];
    if (IS_PKG_PROJECT) {
      required.unshift(['build/v4-staging', '构建副本'], ['build/v4-staging/SKILL.md', '包清单'], ['docs', '项目文档']);
    }
    const missing = required.filter(([rel]) => !fs.existsSync(path.join(ROOT, rel)));
    if (missing.length) block('必要目录/文件缺失', missing.map(m => m[0]).join('、'));
    else ok('必要目录齐全', `${required.length} 项`);

    let empties = [], scanned = 0;
    (function walk(d, depth) {
      if (depth > MAX_DEPTH || scanned > MAX_DIRS) return;
      for (const sub of listDirs(d)) {
        if (scanned > MAX_DIRS) return;
        scanned++;
        const full = path.join(d, sub);
        if (sub === '.git' || sub === 'node_modules') continue;
        const kids = listFiles(full).length + listDirs(full).length;
        if (kids === 0) empties.push(path.relative(ROOT, full).replace(/\\/g, '/'));
        else walk(full, depth + 1);
      }
    })(ROOT, 0);
    if (empties.length) info('空目录残留', `${empties.length} 处：${empties.slice(0, 5).join('、')}${empties.length > 5 ? ' …' : ''}`);
    else ok('无空目录残留', `扫描 ${scanned} 个目录`);
  }
}

/* ========== 组 2：版本一致性（四处比对） ========== */
group('版本一致性');
{
  if (!IS_PKG_PROJECT) {
    info('不适用', '本工程未安装 ai-workflow 包，无包级版本可校');
  } else {
    const skillVer = (readText(path.join(PKG, 'SKILL.md')).match(/^version:\s*([\d.]+)/m) || [])[1] || '';
    const catVer = (readJson(path.join(PKG, 'catalog.json')) || {}).version || '';
    const changelog = readText(path.join(PKG, 'CHANGELOG.md'));
    const hasChangelogVer = skillVer ? changelog.includes(`## ${skillVer}`) : false;
    const engineFile = path.join(PKG, 'skills', 'harness', 'engine', 'harness.mjs');
    const engineVer = (readText(engineFile).match(/harness engine v([\d.]+)/) || [])[1] || '';
    const harnessSkillVer = (readText(path.join(PKG, 'skills', 'harness', 'SKILL.md')).match(/^version:\s*([\d.]+)/m) || [])[1] || '';

    if (!skillVer) block('包版本号缺失', `${path.join(PKG, 'SKILL.md')} 里读不到 version`);
    else ok('包版本号', skillVer);

    const mismatches = [];
    if (catVer && catVer !== skillVer) mismatches.push(`catalog.json=${catVer}`);
    if (!hasChangelogVer) mismatches.push(`CHANGELOG.md 缺 ## ${skillVer} 段`);
    if (engineVer && engineVer !== '0.' + skillVer.split('.').slice(1).join('.')) {
      // 引擎版本与包版本不是同一套编号，只报告不自作主张判错
      info('引擎版本号', `v${engineVer}（与包版本 ${skillVer} 不同编号体系，属正常）`);
    }
    if (mismatches.length) block('版本号不一致', mismatches.join('；'));
    else ok('四处版本号一致', `包 ${skillVer} · catalog ${catVer || '—'} · CHANGELOG ${hasChangelogVer ? '有' : '缺'} · 引擎 v${engineVer || '—'}`);

    if (harnessSkillVer) {
      const mapOk = readText(path.join(PKG, 'skills', 'harness', 'SKILL.md')).includes(engineVer);
      if (mapOk) ok('harness 技能↔引擎版本对应关系已写明', `技能 ${harnessSkillVer} ↔ 引擎 v${engineVer}`);
      else info('harness 技能↔引擎版本对应关系未写明', `技能 ${harnessSkillVer} / 引擎 v${engineVer}`);
    }

    // 工程内 harness 依赖必须与包同步 —— 否则引擎读的是旧版，机制在宿主里形同虚设
    const pairs = [
      ['skills/harness/references', 'harness/references'],
      ['skills/harness/templates', 'harness/templates'],
      ['skills/harness/engine/harness.mjs', 'harness/engine/harness.mjs']
    ];
    const drift = [];
    for (const [pkgRel, rootRel] of pairs) {
      const srcPath = path.join(PKG, pkgRel);
      if (!fs.existsSync(srcPath)) continue;
      const srcIsDir = fs.statSync(srcPath).isDirectory();
      const dstPath = path.join(ROOT, rootRel);
      if (srcIsDir) {
        if (!fs.existsSync(dstPath)) { drift.push(`${rootRel} 缺失`); continue; }
        for (const f of listFiles(srcPath)) {
          const d = path.join(dstPath, f);
          if (!fs.existsSync(d)) { drift.push(`${rootRel}/${f} 缺失`); continue; }
          if (readText(path.join(srcPath, f)) !== readText(d)) drift.push(`${rootRel}/${f} 内容不一致`);
        }
        const extra = listFiles(dstPath).filter(f => !listFiles(srcPath).includes(f));
        if (extra.length) drift.push(`${rootRel} 多出：${extra.join('、')}`);
      } else {
        if (!fs.existsSync(dstPath)) { drift.push(`${rootRel} 缺失`); continue; }
        if (readText(srcPath) !== readText(dstPath)) drift.push(`${rootRel} 内容不一致`);
      }
    }
    if (drift.length) block('工程内 harness 依赖与包不一致', drift.join('；') + ' —— 跑 bootstrap.mjs --apply 同步（引擎读的是工程内那份，不同步则机制不生效）');
    else {
      // 脚本也得比 —— 上一轮就是「memory.mjs 改了工程内那份、bootstrap 一跑被覆盖」，
      // 而当时的比对不含 scripts，分叉检查抓不到。按清单比，不能整目录比（包内脚本多得多）。
      const SYNCED = ['selfcheck.mjs', 'release-closure.mjs', 'release-closure.test.mjs', 'evidence-run.mjs', 'evidence-run.test.mjs', 'memory.mjs', 'observe.mjs', 'lint-words.mjs', 'measure-tokens.mjs',
        'hook-precommit.mjs', 'hook-postcommit.mjs', 'hook-postmerge.mjs', 'task-signature.mjs', 'ui-gate.mjs'];
      const sDrift = [];
      for (const f of SYNCED) {
        const src = path.join(PKG, 'scripts', f);
        const dst = path.join(ROOT, 'harness', 'scripts', f);
        if (!fs.existsSync(src)) continue;
        if (!fs.existsSync(dst)) { sDrift.push(`harness/scripts/${f} 缺失`); continue; }
        if (readText(src) !== readText(dst)) sDrift.push(`harness/scripts/${f} 内容不一致`);
      }
      if (sDrift.length) block('工程内脚本与包不一致', sDrift.join('；') + ' —— 跑 bootstrap.mjs --apply 同步（改代码要改包内，工程内是产物）');
      else ok('工程内 harness 依赖与包一致', `${pairs.length} 组 + ${SYNCED.length} 个脚本`);
    }
  }
}

/* ---------- 自测并发预取 ----------
 * 这些自测互相独立，串行跑只是白等（实测 memory 4.3s、next-scene 4.7s，两项占自检一半耗时）。
 * 一次性并发启动，之后各处只读缓存；缓存缺失时各调用点仍回退到原来的同步调用，语义不变。
 * 用顶层 await（本文件是 .mjs）；不能改成 Atomics.wait 轮询 —— 那会阻塞事件循环，
 * 子进程的 'close' 回调永远收不到（memory.mjs 的自测踩过这个坑，只能靠产物落盘绕）。 */
const selftestResults = new Map();
{
  const selfFile = new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
  const jobs = [];
  for (const [label, file, args] of SELFTEST_JOBS) {
    const f = findScript(file);
    if (!f) continue;                     // 缺脚本交给各调用点的原有 info/block，不在这里造结论
    jobs.push(runNodeAsync(f, args, ROOT).then(r => selftestResults.set(label, r)));
  }
  if (fs.existsSync(selfFile)) {
    jobs.push(runNodeAsync(selfFile, ['--selftest'], ROOT).then(r => selftestResults.set('自检判据', r)));
  }
  await Promise.all(jobs);
}

/* ========== 组 3：记忆健康度 ========== */
group('记忆健康度');
{
  const memScript = findScript('memory.mjs');
  if (!memScript) {
    block('memory.mjs 缺失', '包内与工程内都找不到 scripts/memory.mjs');
  } else if (!ROOT) {
    info('记忆层跳过', '未识别项目根');
  } else {
    const r = runNode([memScript, 'stats', '--root', ROOT]);
    const m = (r.out.match(/STATS (\{.*\})/) || [])[1];
    if (!m) {
      info('记忆层读取失败', (r.out || '').trim().split('\n').slice(-1)[0] || '无输出');
    } else {
      let s = null; try { s = JSON.parse(m); } catch { }
      if (!s) { info('记忆层统计解析失败', m); }
      else {
        const total = (s['正式条目'] || 0) + (s['候选池条目'] || 0);
        // 新工程没有「已收口」的需求包 → 记忆为空是正常初始态，只提示；
        // 有收口历史却没沉淀下任何记忆 → 阻断（自我进化链断了）
        const reqRoot = ROOT ? path.join(ROOT, 'harness', 'requirements') : null;
        const hasHistory = reqRoot ? listDirs(reqRoot).some(d => {
          const st = path.join(reqRoot, d, 'state.md');
          return fs.existsSync(st) && /G4:\s*PASS/.test(readText(st));
        }) : false;
        if (!total) {
          if (hasHistory) block('记忆层是空的', '已有需求包历史但正式记忆与候选池都没有条目 —— 自我进化链没有原料');
          else info('记忆层为空', '新工程，尚无经验可沉淀（正常初始态）');
        }
        else ok('记忆条目', `正式 ${s['正式条目']}（结构化 ${s['结构化条目']}）· 候选 ${s['候选池条目']} · 待晋升 ${s['待晋升候选']}`);
        if ((s['正式条目'] || 0) > 0 && (s['结构化条目'] || 0) < (s['正式条目'] || 0)) {
          info('存在旧格式条目', `${(s['正式条目'] || 0) - (s['结构化条目'] || 0)} 条缺结构化字段，仍可检索`);
        }
        if ((s['已过复审期'] || 0) > 0) info('条目已过复审期', `${s['已过复审期']} 条，需 prune 或复审`);
        if ((s['候选池条目'] || 0) >= 10) info('候选池积压', `${s['候选池条目']} 条待人确认，建议 memory.mjs ready`);
        if ((s['待提炼事件'] || 0) >= 10) info('事件待提炼', `${s['待提炼事件']} 条门禁回流事件未提炼成经验 —— 直接晋升不了，建议人工提炼后 memory.mjs add`);

        // 观察流闭环：同因必须合并，不能让同一条错误刷满 observe.md
        const obsScript = findScript('observe.mjs');
        if (!obsScript) block('observe.mjs 缺失', '找不到观察脚本');
        else {
          const ot = selftestResults.get('观察流') || runNode([obsScript, '--selftest', '--root', ROOT]);
          if (/OBS SELFTEST OK/.test(ot.out)) ok('观察流自测通过', (ot.out.match(/OBS SELFTEST OK[^\n]*/) || [''])[0]);
          else block('观察流自测失败', (ot.out || '').trim().split('\n').slice(-2).join(' ') || '命令无输出');
        }

        // 自我进化闭环机检：召回防刷 + 担保晋升必须真跑过，不能只写在注释里
        const st = selftestResults.get('记忆闭环') || runNode([memScript, '--selftest', '--root', ROOT]);
        if (/MEM SELFTEST OK/.test(st.out)) ok('记忆闭环自测通过', (st.out.match(/MEM SELFTEST OK[^\n]*/) || [''])[0]);
        else block('记忆闭环自测失败', (st.out || '').trim().split('\n').slice(-2).join(' ') || '命令无输出');

        // 现场生成器自测：提交数跑步机 / 非原子替换冲掉手写层 / 多区块只处理第一个 —— 这三个都真实发生过
        const nsScript = findScript('next-scene.mjs');
        if (!nsScript) block('现场生成器缺失', '找不到 scripts/next-scene.mjs');
        else {
          const nst = selftestResults.get('现场生成器') || runNode([nsScript, '--selftest', '--root', ROOT]);
          if (/NEXT-SCENE SELFTEST OK/.test(nst.out)) ok('现场生成器自测通过', (nst.out.match(/NEXT-SCENE SELFTEST OK[^\n]*/) || [''])[0]);
          else block('现场生成器自测失败', (nst.out || '').trim().split('\n').slice(-2).join(' ') || '命令无输出');
        }
      }
    }
  }
}

/* ========== 组 4：临时产物残留 ========== */
group('临时产物');
{
  if (ROOT) {
    let baks = [], zips = [], counted = 0;
    const skillVer = (readText(path.join(PKG, 'SKILL.md')).match(/^version:\s*([\d.]+)/m) || [])[1] || '';
    (function walk(d, depth) {
      if (depth > MAX_DEPTH || counted > MAX_FILES) return;
      for (const f of listFiles(d)) {
        if (counted > MAX_FILES) return;
        counted++;
        if (isBakName(f)) baks.push(path.relative(ROOT, path.join(d, f)).replace(/\\/g, '/'));
        const zm = f.match(/^ai-workflow(?:-harness)?-v(\d+\.\d+\.\d+)\.zip$/i);
        if (zm && zm[1] !== skillVer) zips.push(path.relative(ROOT, path.join(d, f)).replace(/\\/g, '/'));
      }
      for (const sub of listDirs(d)) {
        if (sub === '.git' || sub === 'node_modules') continue;
        walk(path.join(d, sub), depth + 1);
      }
    })(ROOT, 0);
    // 这两类都能自动修，所以「累积」就该拦 —— 只提示的结果是：报了几十次也没人动手。
    // 阈值按「会不会拖慢发布 / 占多少盘」定，不是按个数好看。
    if (!baks.length) ok('无 .bak 残留', `扫描 ${counted} 个文件`);
    else if (baks.length > 5) block('.bak 残留过多', `${baks.length} 处 —— 跑 node harness/engine/harness.mjs clean --apply 清掉（能自动再生，不用留）`);
    else info('引擎/文件备份残留', `${baks.length} 处（未超 5 处阈值）：${baks.slice(0, 3).join('、')}`);
    // 当前版本必须有分发包 —— 原来只查「孤儿太多」，缺了不算错，
    // 于是出现「版本升到 4.4.0、目录里还是 v4.3.0.zip」这种报告与产物不一致（用户截图发现的）
    // 分发包相关检查只对「包工程」成立（复用全局判据，别另造一套口径）：
    // 全新消费工程没有 zip / pack.mjs，早先直接 block 会让 init 后 4 红灯、begin NOT-READY。
    if (!IS_PKG_PROJECT) {
      info('分发包检查跳过', '当前工程不是技能包（无 SKILL.md + skills/）—— zip 与 pack.mjs 不适用');
    } else {
    {
      const verM = (readText(path.join(PKG, 'SKILL.md')).match(/^version:\s*(\S+)/m) || [, ''])[1].trim();
      const curZip = path.join(PKG, `ai-workflow-v${verM}.zip`);
      if (!verM) info('分发包与版本一致', 'SKILL.md 读不到版本号，跳过');
      else if (!fs.existsSync(curZip)) block('缺当前版本的分发包', `SKILL.md 是 ${verM}，但 ${path.basename(curZip)} 不存在 —— 跑 node scripts/release.mjs --pack；不打包就没有可交付产物，版本号只是嘴上说的`);
      else ok('分发包与版本一致', `${path.basename(curZip)} 在（${(fs.statSync(curZip).size / 1048576).toFixed(1)} MB）`);
      // OI-35 回归证明：打包必须以 git 跟踪为准，未跟踪文件不得混入分发包。
      // 只靠 pack.mjs 自己打印不算数 —— 这里独立跑一次 --check 并核对报告。
      const pk = findScript('pack.mjs');
      if (!pk) block('打包器缺失', '找不到 scripts/pack.mjs');
      else {
        // 不让 pack 自证：自检自己问 git 要未跟踪清单，跟 pack 报告的条数对账。
        // 只信 pack 的打印，改成 untracked=0 就能骗过（实测发生过）。
        const { execFileSync } = await import('node:child_process');
        let realUntracked = null;
        try {
          const gitRoot = execFileSync('git', ['rev-parse', '--show-toplevel'], { cwd: PKG, encoding: 'utf8' }).trim();
          const rel = path.relative(gitRoot, PKG).split(path.sep).join('/');
          const prefix = rel ? rel + '/' : '';
          const out = execFileSync('git', ['ls-files', '--others', '--exclude-standard', '-z', '--', prefix || '.'], { cwd: gitRoot, encoding: 'utf8' });
          realUntracked = out.split('\0').filter(Boolean).length;
        } catch { realUntracked = null; }
        const r = runNode([pk, '--check'], PKG);
        const m = (r.out.match(/PACK-CHECK tracked=(\S+) walked=(\d+) included=(\d+) untracked=(\d+)/) || []);
        if (!m) block('打包跟踪检查无输出', (r.out || '').trim().split('\n').slice(-1)[0] || '命令无输出');
        else if (m[1] === 'yes' && realUntracked !== null && Number(m[4]) < realUntracked) {
          block('打包漏报未跟踪文件', `pack 报 ${m[4]}，git ls-files 实测 ${realUntracked} —— 打包器少报了自己排除的文件，不能信`);
        } else if (m[1] === 'yes' && Number(m[4]) > 0 && Number(m[2]) !== Number(m[3]) + Number(m[4])) {
          block('打包未排除未跟踪文件', `walked=${m[2]} included=${m[3]} untracked=${m[4]}，但三者不守恒 —— OI-35 回归了`);
        } else ok('打包以 git 跟踪为准', `tracked=${m[1]} · included=${m[3]} / walked=${m[2]} · 排除未跟踪 ${m[4]}（git 复核 ${realUntracked}）`);
      }
    }
    }
    // NEXT.md 的现场必须与实际一致 —— 新会话接手时读的第一份文件就是它。
    // 它原来纯手写，实测停在 v4.4.6 而实际已 v4.4.14（版本号、技能数、任务单清单全错），
    // 而且没有任何机制提醒它过期 —— 「资产存在不等于 agent 可达」的下一层：资产内容过期 = 资产有害。
    // 现在生成层由 scripts/next-scene.mjs 从真相源生成，这里独立跑 --check 对账（不让生成器自证）。
    {
      const nextPath = path.join(ROOT, 'harness', 'NEXT.md');
      if (!fs.existsSync(nextPath)) {
        info('NEXT.md 现场检查跳过', '当前工程没有 harness/NEXT.md（消费工程不生成它，只有包工程有）');
      } else {
        const ns = findScript('next-scene.mjs');
        if (!ns) block('现场生成器缺失', '找不到 scripts/next-scene.mjs —— NEXT.md 的生成层没人维护，必然过期');
        else {
          const r = runNode([ns, '--check'], PKG);
          if (r.code !== 0) {
            block('NEXT.md 现场与实际不一致', (r.out || '').trim().split('\n').slice(0, 3).join(' ') + ' —— 新会话会读到错的现场；跑 node scripts/next-scene.mjs --write');
          } else {
            ok('NEXT.md 现场与实际一致', (r.out || '').trim().split('\n')[0] || '一致');
          }
        }
      }
    }
    // HANDOFF 必须指向当前活跃包、且不早于它的 state.md —— 换工具接手读的第一份快照就是它。
    // 只比 mtime 不够：git checkout 后所有文件时间几乎相同，会误判或漏判；
    // 所以先比内容里的「需求」行（跨克隆稳定），再比 mtime（容差 60 秒，捕获同包状态更新）。
    {
      const hf = path.join(ROOT, 'harness', 'HANDOFF.md');
      const reqRoot = path.join(ROOT, 'harness', 'requirements');
      const packs = fs.existsSync(reqRoot)
        ? fs.readdirSync(reqRoot, { withFileTypes: true })
            .filter(d => d.isDirectory() && /^REQ-\d{8}-\d{2}$/.test(d.name))
            .map(d => {
              const sf = path.join(reqRoot, d.name, 'state.md');
              if (!fs.existsSync(sf)) return null;
              const t = readText(sf);
              const stage = (t.match(/^stage:\s*(.+)$/m) || [, 'unknown'])[1].trim();
              const g4 = (t.match(/^G4:\s*([^|\n]+)/m) || [, ''])[1].trim();
              const closed = /^PASS$/i.test(g4) || /^(archive|done|released)$/i.test(stage);
              return { name: d.name, f: sf, stage, closed, t: fs.statSync(sf).mtimeMs };
            })
            .filter(Boolean)
        : [];
      const pick = pickActivePack(packs);
      if (!pick) {
        info('HANDOFF 新鲜度检查跳过', '当前工程没有需求包（消费工程不生成 HANDOFF）');
      } else if (!fs.existsSync(hf)) {
        block('HANDOFF.md 缺失', `有 ${packs.length} 个需求包却没有 HANDOFF.md —— 换工具接手找不到现场；跑 node harness/engine/harness.mjs handoff ${pick.name} --write`);
      } else {
        const hText = readText(hf);
        const hReq = (hText.match(/^-\s*需求：\s*(\S+)/m) || [, ''])[1].trim();
        const hTime = fs.statSync(hf).mtimeMs;
        if (hReq && hReq !== pick.name) {
          block('HANDOFF 指向的不是当前活跃包',
            `HANDOFF.md 写的是 ${hReq}，当前活跃包是 ${pick.name} —— 换工具会接着做错的任务；`
            + `跑 node harness/engine/harness.mjs handoff ${pick.name} --write`);
        } else if (hTime + 60000 < pick.t) {
          const d = new Date(hTime);
          const p = n => String(n).padStart(2, '0');
          block('HANDOFF 已过期',
            `HANDOFF.md 停在 ${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}，`
            + `早于 ${pick.name}/state.md —— 换工具会读到旧现场；`
            + `跑 node harness/engine/harness.mjs handoff ${pick.name} --write`);
        } else {
          ok('HANDOFF 与最新任务单同步', `指向 ${pick.name}，不早于它的 state.md`);
        }
      }
    }
    // 自检判据自测：活跃包判定写错的后果是「换工具直接做错任务」，
    // 而它只在某个包刚收口、其余包又都不动时才暴露 —— 靠日常运行撞不上，必须有回归用例。
    {
      const self = new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
      const r = selftestResults.get('自检判据') || spawnSync(process.execPath, [self, '--selftest'], { cwd: ROOT, encoding: 'utf8', timeout: SUBPROCESS_TIMEOUT });
      const line = `${r.stdout || r.out || ''}${r.stderr || ''}`.trim().split('\n').filter(Boolean).pop() || '';
      if ((r.status ?? r.code) === 0) ok('自检判据自测通过', line);
      else block('自检判据自测失败', `${line} —— 规则写错了，结论会是假的`);
    }
    // 范围排除清单：明确不做什么，防止重复提议与范围蠕变。格式错或重复就阻断。
    {
      const oos = path.join(ROOT, 'OUT-OF-SCOPE.md');
      if (!fs.existsSync(oos)) {
        info('范围排除清单未配置', '没有 OUT-OF-SCOPE.md（消费工程可以没有；本仓应有一份）');
      } else {
        // 先剥首尾竖线再 split：合法的无尾竖线行（| A | B | C | 2026-11-22）不能被算成 3 列
        const rows = readText(oos).split(/\r?\n/)
          .filter(l => /^\|/.test(l))
          .map(l => l.trim().replace(/^\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim()))
          .filter(c => !c.every(x => /^:?-{3,}:?$/.test(x) || x === ''));
        // 表头按首行识别，不靠字面「类别」——列名将来改了也不会被当数据行
        if (rows.length && rows[0].some(x => x === '类别' || x === '不收什么')) rows.shift();
        const bad = rows.filter(c => c.length < 4
          || c.slice(0, 3).some(x => !x || /<[^>]*>/.test(x))
          || !/^\d{4}-\d{2}-\d{2}$/.test(c[3] || ''));
        const seen = new Set(), dup = [];
        for (const c of rows) {
          const k = c[1];
          if (!k) continue;
          if (seen.has(k)) dup.push(k); else seen.add(k);
        }
        if (rows.length < 5) block('范围排除清单太短', `只有 ${rows.length} 条，至少 5 条（明确不做什么，防止重复提议）`);
        else if (bad.length) block('范围排除清单格式异常', `${bad.length} 行四列不全或日期不是 YYYY-MM-DD：${bad.slice(0, 2).map(c => c.join('/')).join('；')}`);
        else if (dup.length) block('范围排除清单有重复项', `重复：${dup.slice(0, 3).join('、')}`);
        else ok('范围排除清单健康', `${rows.length} 条，四列齐全、无重复`);
      }
    }
    // 决策记录：跨需求决策要能被后来者查到。编号、索引、四节缺一即断。
    // 只写 README 没有决策文件时只提示 —— 新工程刚建目录不该被拦。
    {
      const adrDir = path.join(ROOT, 'docs', 'adr');
      if (!fs.existsSync(adrDir)) {
        info('决策记录未配置', '没有 docs/adr/（消费工程可以没有；本仓应有一份）');
      } else {
        const isAdr = f => /^\d{4}-[a-z0-9-]+\.md$/.test(f) && !/^0000-/.test(f);
        let files = null;
        try { files = fs.readdirSync(adrDir).filter(isAdr).sort(); }
        catch { block('决策记录读取失败', 'docs/adr/ 存在但读不了，检查目录权限'); }
        if (files) {
          const readmePath = path.join(adrDir, 'README.md');
          // 索引里既可能写裸文件名，也可能写 Markdown 链接；两种都认
          let readmeTxt = '';
          try { readmeTxt = fs.existsSync(readmePath) ? fs.readFileSync(readmePath, 'utf8') : ''; } catch { }
          const listed = new Set((readmeTxt.match(/\b\d{4}-[a-z0-9-]+\.md\b/g) || []).filter(f => !/^0000-/.test(f)));
          const nums = files.map(f => f.slice(0, 4));
          const dup = [...new Set(nums.filter((n, i) => nums.indexOf(n) !== i))];
          const missing = files.filter(f => !listed.has(f));
          const ghost = [...listed].filter(f => !files.includes(f));
          const bad = [];
          for (const f of files) {
            let t = '';
            try { t = fs.readFileSync(path.join(adrDir, f), 'utf8'); }
            catch { bad.push(`${f}（读不了）`); continue; }
            const lack = ['## 背景', '## 决定', '## 后果', '## 替代方案'].filter(h => !t.includes(h));
            if (lack.length) bad.push(`${f}（缺 ${lack.join(' / ')}）`);
          }
          // 目录为空但索引还列着文件 = 文件被删了索引没改，必须拦；两者都空才是「还没开始」
          if (!files.length && !listed.size) info('决策记录为空', 'docs/adr/ 只有索引或模板，尚无决策文件');
          else if (!fs.existsSync(readmePath)) block('决策记录缺索引', 'docs/adr/README.md 不存在，决策写出来也没人查得到');
          else if (dup.length) block('决策记录编号重复', `重复编号：${dup.join('、')} —— 一个编号只能对应一份决策`);
          else if (missing.length || ghost.length) block('决策记录索引与文件不一致', `${missing.length ? `未登记：${missing.slice(0, 3).join('、')}` : ''}${missing.length && ghost.length ? '；' : ''}${ghost.length ? `索引指向不存在：${ghost.slice(0, 3).join('、')}` : ''}`);
          else if (bad.length) block('决策记录结构不全', `${bad.length} 份：${bad.slice(0, 2).join('；')} —— 四节（背景 / 决定 / 后果 / 替代方案）缺一不可`);
          else ok('决策记录健康', `${files.length} 份，编号唯一、索引覆盖、四节齐全`);
        }
      }
    }
    // 多一个就拦：zip 随时能重打，留着没用。原来阈值 3 个才拦，用户截图里看到新旧两个并存
    if (!zips.length) ok('无孤儿分发包');
    else block('有旧版本分发包没清', `${zips.length} 个非当前版本：${zips.slice(0, 3).join('、')} —— 跑 node harness/engine/harness.mjs clean --apply（只留当前版本）`);
    const evalResults = path.join(PKG, 'eval-results');
    if (fs.existsSync(evalResults)) info('评测工作产物残留', 'build/v4-staging/eval-results/');
    else ok('无评测工作产物');

    // 白名单兜底：列黑名单永远会漏 —— 我们就先后漏了 .bak 与历史 zip 两轮，
    // 都是用户看目录截图才发现的。改成「包根只允许这些后缀」，以后再冒出新垃圾
    // 不用改代码就能被抓到。
    // 无后缀的标准文件也放行：LICENSE / NOTICE 这类是分发惯例，不是垃圾。
    const ALLOW_EXT = /\.(md|json|mjs|cjs|js|ts|sh|ya?ml|txt|png|jpe?g|svg|gif|webp|zip|map)$/i;
    const ALLOW_BARE = /^(LICENSE|LICENCE|NOTICE|COPYING)$/;
    const strayRoot = listFiles(PKG).filter(f => !f.startsWith('.') && !ALLOW_EXT.test(f) && !ALLOW_BARE.test(path.basename(f)));
    if (strayRoot.length) block('包根出现非白名单文件', `${strayRoot.length} 个：${strayRoot.slice(0, 5).join('、')} —— 包根只允许 md/json/mjs/sh/yml/图片/zip/LICENSE；要么是垃圾，要么该放进子目录`);
    else ok('包根无多余文件类型', `白名单校验通过（${listFiles(PKG).length} 个文件）`);

    // 包是分发物，不该有运行数据。某脚本在包目录里跑过一次，就会长出 harness/memory/inbox.md 这种
    // 东西 —— 它的扩展名是 .md，能骗过上面的后缀白名单，所以必须按目录名单独拦。
    {
      const junkDirs = ['harness', '.ai-workflow', 'node_modules'].filter(d => fs.existsSync(path.join(PKG, d)));
      if (junkDirs.length) {
        block('包内混入运行数据目录', `${junkDirs.join('、')} —— 包目录里出现了工程运行产物；通常是某个脚本以包目录为 cwd 跑过。删掉并查是哪一条，否则每次分发都带着别人的记忆`);
      } else {
        ok('包内无运行数据目录', 'harness / .ai-workflow / node_modules 均未出现在包根');
      }
    }
  } else {
    info('跳过', '未识别项目根');
  }
}

/* ========== 组 4.5：工作区父目录白名单 ========== */
// 起因：E:\xiaomifeng\ 顶层出现 ac27-verify.mjs / dbg.mjs / ai-workflow-skill-20261018.zip 等散落文件，
// 现有自检只扫 ROOT 内，父目录这一层是盲区 —— 用户看文件管理器才发现。
// 策略：白名单兜底（ADR-0006 worktree 命名 + 用户配置），denyExtensions 拦已知垃圾扩展名；
// 只读不删，只报文件名不显完整路径（隐私）；父目录不存在/不可读 → info 不阻断（自检不能卡死）。
group('工作区父目录');
{
  if (!ROOT) {
    info('不适用', '未识别项目根，无父目录可扫');
  } else {
    const cfg = readJson(path.join(ROOT, 'harness', 'harness.config.json')) || {};
    const wp = cfg.workspaceParent;
    if (!wp || wp.enabled === false) {
      info('不适用', 'harness.config.json 未启用 workspaceParent（enabled=false 或缺失）');
    } else {
      const parentDir = path.dirname(ROOT);
      const allow = Array.isArray(wp.allow) ? wp.allow : [];
      const denyExt = Array.isArray(wp.denyExtensions) ? wp.denyExtensions.map(s => String(s).toLowerCase()) : [];
      const maxEntries = typeof wp.maxEntries === 'number' ? wp.maxEntries : 200;

      // 通配符匹配：把 `ai-workflow-*` 转成正则；只支持前缀/后缀 `*`，够本场景用
      const globToRe = (g) => new RegExp('^' + String(g).replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.') + '$');
      // 目录白名单只用「非 *.ext」项 —— `*.md` 这类文件扩展名通配不该参与目录匹配（RV-05）
      const allowRes = allow.filter(g => !String(g).startsWith('*.')).map(globToRe);

      let entries = [];
      let totalEntries = 0;   // 截断前的真实总数，用于结论行（RV-04）
      try {
        entries = fs.readdirSync(parentDir, { withFileTypes: true });
        totalEntries = entries.length;
      } catch (e) {
        info('父目录不可读', `${parentDir}：${e.message?.split('\n')[0]} —— 不阻断，跳过本组`);
        entries = null;
      }

      if (entries) {
        if (entries.length > maxEntries) {
          info('父目录条目超上限', `${entries.length} > ${maxEntries} —— 只扫前 ${maxEntries} 条，避免扫到 OneDrive 同步目录`);
          entries = entries.slice(0, maxEntries);
        }

        const blocked = [];   // 命中 denyExtensions 且不在白名单
        const unknown = [];   // 不在白名单、也不命中 deny（info 级提示）
        for (const ent of entries) {
          const name = ent.name;
          const isDir = ent.isDirectory();
          // 白名单分两套：
          //   目录：允许 `ai-workflow-*` 这种通配（worktree 命名）
          //   文件：只允许精确名 或 `*.ext` 扩展名通配 —— 防止目录通配 `ai-workflow-*` 误放行 `ai-workflow-skill-20261018.zip`
          if (isDir) {
            if (allowRes.some(re => re.test(name))) continue;
          } else {
            const fileAllowed = allow.some(g => g === name || (g.startsWith('*.') && name.endsWith(g.slice(1))));
            if (fileAllowed) continue;
          }
          const ext = (name.match(/\.[^.]+$/) || [''])[0].toLowerCase();
          if (!isDir && denyExt.includes(ext)) blocked.push(name);
          else unknown.push(name + (isDir ? '/' : ''));
        }

        if (blocked.length) {
          block('父目录散落临时文件',
            `${blocked.length} 个：${blocked.slice(0, 5).join('、')}${blocked.length > 5 ? ' …' : ''}`
            + ` —— 移到 ${path.basename(ROOT)}/ 内，或加进 harness.config.json 的 workspaceParent.allow`);
        } else {
          ok('父目录无散落临时文件', `共 ${totalEntries} 个条目，实扫 ${entries.length} 个`);
        }
        if (unknown.length) {
          info('父目录未在白名单的条目',
            `${unknown.length} 个（仅提示，不阻断）：${unknown.slice(0, 5).join('、')}${unknown.length > 5 ? ' …' : ''}`);
        }
      }
    }
  }
}

/* ========== 组 5：未跟踪文件 ========== */
group('未跟踪文件');
{
  if (!ROOT) { info('跳过', '未识别项目根'); }
  else {
    const r = runBin('git', ['status', '--porcelain'], ROOT, 15000);
    if (r.code !== 0 && !r.out.trim()) { info('跳过', '不是 git 仓库或 git 不可用'); }
    else if (/Cannot find module|not found|不是内部或外部命令/i.test(r.out)) { info('跳过', 'git 不可用'); }
    else {
      const lines = r.out.split(/\r?\n/).map(s => s.trimEnd()).filter(Boolean);
      const untracked = lines.filter(l => l.startsWith('??'));
      if (untracked.length) info('未跟踪文件', `${untracked.length} 个：${untracked.slice(0, 3).join(' | ').slice(0, 120)}`);
      else ok('无未跟踪文件', `工作区 ${lines.length} 项改动`);
    }
  }
}

/* ========== 组 6：宿主一致性 ========== */
group('宿主一致性');
{
  if (!IS_PKG_PROJECT) {
    info('不适用', '本工程未安装 ai-workflow 包，无宿主可校');
  } else {
    const cmpVer = (a, b) => {
      const na = String(a).split('.').map(n => parseInt(n, 10) || 0);
      const nb = String(b).split('.').map(n => parseInt(n, 10) || 0);
      for (let i = 0; i < Math.max(na.length, nb.length); i++) {
        const d = (na[i] || 0) - (nb[i] || 0);
        if (d) return d;
      }
      return 0;
    };
    const boot = readText(path.join(PKG, 'scripts', 'bootstrap.mjs'));
    const hostBlock = (boot.match(/const HOSTS = \{([\s\S]*?)\n\};/) || [, ''])[1];
    const hosts = [...hostBlock.matchAll(/^\s*'?([a-z][a-z0-9-]*)'?\s*:/gm)].map(m => m[1]);
    if (!hosts.length) {
      block('宿主清单读取失败', 'bootstrap.mjs 的 HOSTS 解析不到');
    } else {
      const pkgVer = (readText(path.join(PKG, 'SKILL.md')).match(/^version:\s*([\d.]+)/m) || [])[1] || '';
      const pkgSkills = new Set(listDirs(path.join(PKG, 'skills')).filter(d => fs.existsSync(path.join(PKG, 'skills', d, 'SKILL.md'))));
      let drift = [], skillDiff = [], unreachable = [], okCount = 0;
      for (const h of hosts) {
        const dir = path.join(os.homedir(), '.' + h, 'skills', 'ai-workflow');
        if (!fs.existsSync(path.join(dir, 'SKILL.md'))) { unreachable.push(h); continue; }
        const hv = (readText(path.join(dir, 'SKILL.md')).match(/^version:\s*([\d.]+)/m) || [])[1] || '?';
        const hSkills = new Set(listDirs(path.join(dir, 'skills')).filter(d => fs.existsSync(path.join(dir, 'skills', d, 'SKILL.md'))));
        // 版本不一致 = 真漂移，阻断；技能集合差异 = 多半是受保护技能被 skip，只提示
        if (hv !== pkgVer) {
          // 方向决定修法：宿主比工程新 = 倒挂，要回灌工程再升版；工程比宿主新 = 正常待同步。
          // 倒挂时跑 bootstrap.mjs --apply 会把宿主降级、抹掉宿主独有的修复。
          const inverted = cmpVer(hv, pkgVer) > 0;
          drift.push(`${h}(包版本 ${hv}≠${pkgVer}，${inverted
            ? '倒挂：宿主更新 —— 回灌工程再升版，别跑 bootstrap.mjs --apply（会把宿主降级）'
            : '工程领先 —— 跑 bootstrap.mjs --apply 同步'})`);
          continue;
        }
        const missing = [...pkgSkills].filter(x => !hSkills.has(x));
        const extra = [...hSkills].filter(x => !pkgSkills.has(x));
        if (missing.length || extra.length) {
          skillDiff.push(`${h}(缺 ${missing.join('/') || '无'}${extra.length ? '；多 ' + extra.join('/') : ''})`);
        } else okCount++;
      }
      if (drift.length) block('宿主版本漂移', drift.join('；'));
      else if (okCount > 0) ok('宿主包版本一致', `${okCount} 个宿主版本与包相同`);
      if (skillDiff.length) info('宿主技能集合与包不同', skillDiff.join('；') + ' —— 多为受保护技能被 --skip 跳过，按需用 --skip "" 强制同步');
      if (unreachable.length) info('宿主不可达', `${unreachable.length} 个：${unreachable.join('、')}（未安装或目录不可读）`);
    }
  }
}

/* ========== 组 6.05：发布收口 ========== */
group('发布收口');
{
  if (!IS_PKG_PROJECT) {
    info('发布收口跳过', '当前工程不是技能包，版本 / ZIP / 宿主 / tag 不适用');
  } else {
    const rc = findScript('release-closure.mjs');
    if (!rc) {
      block('发布收口脚本缺失', '找不到 scripts/release-closure.mjs');
    } else {
      const st = selftestResults.get('发布收口') || runNode([rc, '--selftest'], ROOT, 90000);
      if (st.code === 0 && /RELEASE-CLOSURE SELFTEST OK/.test(st.out)) ok('发布收口自测通过', '本地正例 / 故障注入 / 离线 / 写入 / 远端正反例');
      else block('发布收口自测失败', (st.out || '').trim().split('\n').slice(-3).join(' ｜ ') || '命令无输出');
      const r = runNode([rc, '--check'], ROOT, 60000);
      const lines = (r.out || '').trim().split(/\r?\n/).filter(Boolean);
      const summary = lines.filter(x => /(RELEASE-CLOSURE PASS|PRE_RELEASE_PASS)/.test(x)).slice(-1)[0] || '';
      if (r.code === 0 && summary) ok('发布收口本地检查', summary);
      else block('发布收口本地检查失败', lines.slice(-4).join(' ｜ ') || '命令无输出');
    }
  }
}

/* ========== 组 6.1：规则目录洁净 ========== */
group('规则目录洁净');
{
  const homes = ['trae-cn', 'codex', 'claude', 'workbuddy', 'deepseek', 'reasonix'];
  const dirs = homes.map(h => {
    const root = path.join(os.homedir(), '.' + h);
    return h === 'trae-cn' ? path.join(root, 'user_rules') : root;
  });
  const found = dirs.filter(d => fs.existsSync(path.join(d, '.ai-workflow-backup')));
  if (found.length) block('规则加载目录内残留备份目录', found.map(d => path.relative(os.homedir(), d)).join('、') + ' —— 备份只写 ~/.ai-workflow-backups，请删掉这些目录');
  else ok('规则加载目录内无备份目录', `已查 ${dirs.length} 个规则目录`);
}

/* ========== 组 7：需求包阶段 ========== */
group('需求包阶段');
{
  const reqRoot = ROOT ? path.join(ROOT, 'harness', 'requirements') : null;
  if (!reqRoot || !fs.existsSync(reqRoot)) { info('跳过', '未识别项目根或无需求包目录'); }
  else {
    const reqs = listDirs(reqRoot).filter(d => fs.existsSync(path.join(reqRoot, d, 'state.md')));
    if (!reqs.length) { info('无需求包', 'harness/requirements/ 下没有需求包'); }
    else {
      let open = [], archived = 0;
      for (const r of reqs) {
        const st = readText(path.join(reqRoot, r, 'state.md'));
        // archive = 「归档 / 主动搁置」，空壳模板包正是用它退出活跃集合的；它没过 G4 是事实，但不是待办，
        // 混进「未收口」会让提示长期挂噪音（实测：07/09/10 三个空壳搁置后这里一直列着它们）。
        // 与 pickActivePack 的排除口径保持一致。
        if (/^stage:\s*archive\s*$/mi.test(st)) { archived++; continue; }
        const g4 = /G4:\s*PASS/.test(st);
        const g3 = /G3:\s*PASS/.test(st);
        const g2 = /G2:\s*PASS/.test(st);
        const g1 = /G1:\s*PASS/.test(st);
        const stage = g4 ? 'G4 已过' : g3 ? 'G3 已过' : g2 ? 'G2 已过' : g1 ? 'G1 已过' : '未定档';
        if (!g4) open.push(`${r}(${stage})`);
      }
      if (open.length) info('未收口的需求包', `${open.join('、')}${archived ? `；另有 ${archived} 个已归档，不计入` : ''}`);
      else ok(archived ? '需求包均已过 G4 或已归档' : '全部需求包已过 G4',
        `${reqs.length} 个${archived ? `（含归档 ${archived}）` : ''}`);
    }
  }
}

/* ========== 组 7B：决策卡片 ========== */
group('决策卡片');
{
  const roots = [ROOT, path.dirname(SCRIPT_DIR)].filter(Boolean);
  const dcScript = findScript('decision-card.mjs') || roots.map(r => path.join(r, 'build', 'v4-staging', 'scripts', 'decision-card.mjs')).find(p => fs.existsSync(p))
    // 消费工程里只有 harness/ 副本，decision-card 脚本与技能正文都在全局包；不回退则「决策卡片缺失」永远为真，refresh 覆盖后照样复发
    || path.join(os.homedir(), '.trae-cn', 'skills', 'ai-workflow', 'scripts', 'decision-card.mjs');
  const dcSkill = [path.join(PKG, 'skills', 'decision-card', 'SKILL.md'),
    path.join(os.homedir(), '.trae-cn', 'skills', 'ai-workflow', 'skills', 'decision-card', 'SKILL.md'),
    ...roots.map(r => path.join(r, 'build', 'v4-staging', 'skills', 'decision-card', 'SKILL.md'))].find(p => fs.existsSync(p));
  if (!fs.existsSync(dcScript) || !fs.existsSync(dcSkill)) {
    block('决策卡片缺失', `脚本或技能不存在：${dcScript} / ${dcSkill}`);
  } else {
    const GOOD = ['结论：建议按现方案做。', '背景：', '- 已到确认点', '选项：', '  ⭐推荐：A —— 后果：出可验证成果', '  B —— 后果：先试点', '回复：1 / 2（不回=默认按 1）'].join('\n');
    const BAD = ['结论：要做吗？', '背景：见上', '选项：', '  A —— 后果：x', '  B —— 后果：y', '  C —— 后果：z', '  D —— 后果：w'].join('\n');
    const NODEF = ['结论：建议 A。', '背景：见上', '选项：', '  ⭐推荐：A —— 后果：快', '  B —— 后果：慢'].join('\n');
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dc-check-'));
    try {
      const gp = path.join(dir, 'good.md'), bp = path.join(dir, 'bad.md'), np = path.join(dir, 'nodefault.md');
      fs.writeFileSync(gp, GOOD); fs.writeFileSync(bp, BAD); fs.writeFileSync(np, NODEF);
      const g = spawnSync(process.execPath, [dcScript, '--check', gp], { encoding: 'utf8', timeout: 15000 });
      const b = spawnSync(process.execPath, [dcScript, '--check', bp], { encoding: 'utf8', timeout: 15000 });
      const n = spawnSync(process.execPath, [dcScript, '--check', np], { encoding: 'utf8', timeout: 15000 });
      if (g.status === 0 && b.status !== 0 && n.status !== 0) ok('决策卡片检查通过', `好 exit=${g.status} · 坏 exit=${b.status} · 缺默认 exit=${n.status}`);
      else block('决策卡片检查异常', `好 exit=${g.status}（应 0）· 坏 exit=${b.status}（应非 0）· 缺默认 exit=${n.status}（应非 0）`);
    } finally { fs.rmSync(dir, { recursive: true, force: true }); }
  }
}
/* ========== 组 8：技能审计 ========== */
group('技能审计');
{
  const audit = findScript('audit-skills.mjs');
  if (!audit) {
    if (IS_PKG_PROJECT) block('audit-skills.mjs 缺失', audit || '未找到');
    else info('不适用', '本工程未安装 ai-workflow 包，无技能可审');
  } else {
    const r = runNode([audit], path.dirname(path.dirname(audit)));
    const m = r.out.match(/total=(\d+)\s+pass=(\d+)\s+fail=(\d+)/);
    if (!m) { info('审计输出无法解析', (r.out || '').trim().split('\n').slice(-1)[0]); }
    else if (parseInt(m[3], 10) > 0) block('技能审计有失败项', `total=${m[1]} pass=${m[2]} fail=${m[3]}`);
    else ok('技能审计通过', `total=${m[1]} pass=${m[2]} fail=${m[3]}`);
  }
}

/* ========== 组 9：技能可加载性 ========== */
group('技能可加载性');
{
  // 必须用「真正装着技能包的那个根」，不能用 PKG —— 宿主场景下 PKG 会指向用户的工程，
  // 读不到包内的 rules.md，于是恒报「硬规则都没有」（与事实相反，且把正常工程判成 BLOCKED）
  const harnessPkg = (() => {
    const cands = [
      path.join(PKG, 'skills', 'harness'),
      path.join(path.dirname(SCRIPT_DIR), 'skills', 'harness'),
      ROOT ? path.join(ROOT, 'harness') : null
    ].filter(Boolean);
    for (const c of cands) {
      try { if (fs.existsSync(path.join(c, 'references', 'rules.md'))) return c; } catch { }
    }
    return null;
  })();

  if (!harnessPkg) {
    info('技能可加载性', '未找到技能包（rules.md 不在包内）—— 本环境没有技能可校，跳过');
  } else {
    const idxScript = findScript('build-skills-index.mjs');
    const idxFile = ROOT ? path.join(ROOT, 'harness', 'skills-index.md') : null;

    // 硬规则：命中技能先读正文
    // 特征串用有区分度的整句，不用「skill <名字>」这类占位符 —— 占位符自己就能匹配上，检查会永远为真
    const rulesTxt = readText(path.join(harnessPkg, 'references', 'rules.md'));
    const agentsTxt = readText(path.join(harnessPkg, 'templates', 'AGENTS.md'));
    const MARK = /未读不得输出该技能的内容/;
    if (MARK.test(rulesTxt) && MARK.test(agentsTxt)) ok('「命中技能先读正文」硬规则在位', 'rules.md 与 AGENTS.md 模板都有');
    else block('缺「命中技能先读正文」硬规则', `rules.md ${MARK.test(rulesTxt) ? '有' : '缺'} / AGENTS.md 模板 ${MARK.test(agentsTxt) ? '有' : '缺'} —— 技能正文写了但没人读，等于没写`);

    // 技能正文行数（Anthropic 规范 ≤ 500 行）
    const skillsDir = path.dirname(harnessPkg);   // harnessPkg 是 …/skills/harness，它的父目录就是 skills/
    if (fs.existsSync(skillsDir)) {
      const over = [], noDesc = [];
      // 只数活跃技能：保留目录（in-progress/deprecated）不是技能，混进来会让「49 个」和 catalog 收录数对不上
      const activeDirs = listDirs(skillsDir).filter(d => !LIFECYCLE_DIRS.has(d));
      for (const d of activeDirs) {
        const f = path.join(skillsDir, d, 'SKILL.md');
        if (!fs.existsSync(f)) continue;
        const txt = readText(f);
        const lines = txt.split(/\r?\n/).length;
        if (lines > 500) over.push(`${d}(${lines})`);
        if (!/description:\s*\S/.test(txt)) noDesc.push(d);
      }
      if (over.length) info('技能正文超 500 行', `${over.length} 个：${over.slice(0, 5).join('、')}${over.length > 5 ? ' …' : ''}`);
      else ok('技能正文都在 500 行内', `${activeDirs.length} 个技能`);
      if (noDesc.length) info('技能缺 description', `${noDesc.length} 个：${noDesc.slice(0, 5).join('、')} —— 缺描述则模型无法自主匹配`);
    }

    // 技能索引：存在才校「是否最新」——不存在不是阻断（宿主场景下索引属于用户工程，不属于包）
    if (!idxScript) {
      info('技能索引', '未找到 build-skills-index.mjs —— 跳过索引检查');
    } else if (!idxFile || !fs.existsSync(idxFile)) {
      info('技能索引', '本工程尚未生成 harness/skills-index.md —— 跑 node scripts/build-skills-index.mjs 生成（不是阻断）');
    } else {
      const r = runNode([idxScript, '--check'], path.dirname(path.dirname(SCRIPT_DIR)), 20000);
      if (r.code !== 0) info('技能索引陈旧', (r.out || '').trim().split('\n').slice(-1)[0] || '跑 node scripts/build-skills-index.mjs 重新生成');
      else ok('技能索引最新', (r.out || '').trim().split('\n').slice(-1)[0]);
      // 索引 token 上限（Anthropic Level 1 建议 ≤ 5000）—— 按索引全文估算，不信它自报的值
      const txt = readText(idxFile);
      const t = estTokens(txt);
      if (t > 5000) block('技能索引超 token 上限', `全文估算 ${t} token 大于 5000 —— 精简 description，或把细节挪进 SKILL.md 正文`);
      else ok('技能索引在 token 上限内', `全文估算 ${t} / 5000 token`);
    }

    // 文档死链：规则与手册里写的 `node harness/xxx.mjs`，在真实工程里必须真能跑。
    // 本轮就抓到两处：init 只拷 2 个脚本（手册却让人跑 task-signature）、
    // rules.md 写成 `node scripts/selfcheck.mjs`（工程里没有 scripts/ 这个顶层目录）
    {
      // 只查「交付给外部使用者」的文档：SKILL.md / references / templates。
      // 需求包（harness/requirements/）里写的 scripts/xxx 是相对包根的，核不了也不该核。
      const mdFiles = [];
      const hRoot = path.join(ROOT || '', 'harness');
      const skillMd = path.join(hRoot, 'SKILL.md');
      if (fs.existsSync(skillMd)) mdFiles.push(skillMd);
      for (const sub of ['references', 'templates']) {
        const d = path.join(hRoot, sub);
        if (!fs.existsSync(d)) continue;
        for (const x of fs.readdirSync(d)) {
          if (/\.md$/.test(x)) mdFiles.push(path.join(d, x));
        }
      }
      const dead = [];
      for (const f of mdFiles) {
        const txt = readText(f);
        // 只核对带 harness/ 前缀的（工程相对路径）；bare scripts/ 的 cwd 语境不定，不猜
        const refs = txt.match(/node\s+harness\/[\w./-]+\.mjs/g) || [];
        for (const r of refs) {
          const rel = r.replace(/^node\s+/, '');
          if (!fs.existsSync(path.join(ROOT || '', rel))) dead.push(`${path.basename(f)} → ${rel}`);
        }
      }
      const uniq = [...new Set(dead)];
      if (uniq.length) block('文档里的命令指向不存在的文件', `${uniq.length} 处：${uniq.slice(0, 4).join('；')} —— 换工具的人照着敲必然报错`);
      else ok('文档里的命令都指向真实文件', `${mdFiles.length} 份 md 全部核对`);
    }

    // 常驻入口可达：换会话时 agent 只读得到 AGENTS.md（+ 宿主入口块）。
    // 技能索引 / 路由命令 / 长期记忆三者必须在入口里出现 —— 否则就是「资产都在、agent 看不见」：
    // 4.4.2 之前自检全绿，但换会话后一个技能想不起来、一条硬规则读不到，正是这个盲区。
    {
      const cands = [
        path.join(ROOT || '', 'AGENTS.md'),
        path.join(ROOT || '', 'harness', 'templates', 'AGENTS.md'),
        path.join(skillsDir, 'harness', 'templates', 'AGENTS.md')
      ];
      const entry = cands.find(f => f && fs.existsSync(f));
      if (!entry) {
        info('常驻入口可达', '找不到 AGENTS.md 或模板，跳过');
      } else {
        const t = readText(entry);
        const need = [
          ['技能索引', /skills-index\.md/],
          ['路由命令', /route\.mjs/],
          // 必须是记忆文件本身：只写 memory.mjs brief 不够 —— brief 靠关键词最多出 3 条，
          // 拿不到全量就等于「只知道有记忆、查不全」。这条一开始写成 OR，反向验证时没拦住。
          ['长期记忆', /long-term\.md/]
        ];
        const lack = need.filter(([, re]) => !re.test(t)).map(([k]) => k);
        if (lack.length) {
          block('常驻入口没指向技能与记忆', `${lack.join('、')} 在 ${path.basename(entry)} 里找不到 —— 换会话时 agent 只读这个文件，指不到等于没有`);
        } else {
          ok('常驻入口指向技能与记忆', '技能索引 / 路由命令 / 长期记忆 三处都在入口');
        }
      }
    }

    // 动效配方可达：界面动效是「默认先选型」的约定，光有文件不算 ——
    // 必须 (a) 十条配方都在、(b) SKILL.md 真的指向它，否则等于写了没人读。
    const uiDir = path.join(skillsDir, 'ui-design');
    const uiSkill = path.join(uiDir, 'SKILL.md');
    const mrec = path.join(uiDir, 'references', 'motion-recipes.md');
    if (!fs.existsSync(mrec) || !fs.existsSync(uiSkill)) {
      info('UI 动效配方', '本包没有 ui-design 技能，跳过');
    } else {
      const recTxt = readText(mrec);
      const uiTxt = readText(uiSkill);
      const missingNums = [];
      for (let i = 1; i <= 10; i++) {
        if (!new RegExp(`^### ${i}\\. `, 'm').test(recTxt)) missingNums.push(i);
      }
      const linked = /motion-recipes\.md/.test(uiTxt);
      const problems = [];
      if (missingNums.length) problems.push(`配方缺编号 ${missingNums.join('/')}`);
      if (!linked) problems.push('ui-design/SKILL.md 没有指向 motion-recipes.md');
      if (problems.length) block('UI 动效配方不完整或没接上', problems.join('；') + ' —— 配方写了但入口没指过去，等于没写');
      else ok('UI 动效配方在位且已接入', `10 条配方 + SKILL.md 三处指向（开始前 / 区块生成 / 按需再读）`);

      // UI 交付门禁脚本：交付说明里写「跑 ui-gate」，那它必须在工程里真的能跑
      const ug = path.join(ROOT || '', 'harness', 'scripts', 'ui-gate.mjs');
      if (!fs.existsSync(ug)) {
        block('UI 门禁脚本缺失', 'harness/scripts/ui-gate.mjs 不在 —— ui-design 的交付流程会让 agent 跑它，缺了就是死链；跑一次 bootstrap');
      } else if (!/ui-gate\.mjs/.test(uiTxt)) {
        block('UI 门禁没接进交付流程', 'ui-design/SKILL.md 没提 ui-gate.mjs —— 脚本在但没人跑等于没写');
      } else {
        ok('UI 门禁脚本已接入', 'harness/scripts/ui-gate.mjs 在位，且写进 ui-design 的交付步骤');
      }
    }

    // 同名消歧：这 8 个与 mattpocock/skills 同名/近义，description 末尾的
    // 「（ai-workflow 包内版）」是模型同时看到两份时唯一的区分依据。
    // 删掉它 = 放弃消歧；改名代价更大（用户已形成的说法会被拆散），所以守标识不守名字。
    {
      const SAME = ['grilling', 'handoff', 'to-spec', 'to-tickets', 'tdd', 'domain-modeling', 'research', 'diagnose'];
      const lack = SAME.filter(n => {
        const f = path.join(skillsDir, n, 'SKILL.md');
        return fs.existsSync(f) && !/ai-workflow 包内版/.test(readText(f));
      });
      if (lack.length) block('同名技能缺包内标识', `${lack.join('、')} 的 description 没有「（ai-workflow 包内版）」—— 同装第三方同名包时模型分不清该用哪份`);
      else ok('同名技能已消歧', `${SAME.length} 个 description 均带包内标识`);
    }

    // 触发词覆盖：活跃技能必须在 triggers.json 有词条，词条也必须指向真实活跃技能。
    // 否则就是「技能写了但路由永远选不中」—— 与「命中技能先读正文」是对偶的盲区。
    {
      const tc = findScript('check-trigger-coverage.mjs');
      if (!tc) info('触发词覆盖', '未找到 check-trigger-coverage.mjs —— 跳过');
      else {
        const r = runNode([tc], path.dirname(path.dirname(tc)), 20000);
        if (r.code !== 0) block('触发词覆盖有缺口', (r.out || '').trim().split('\n').slice(0, 6).join(' ｜ '));
        else ok('触发词覆盖通过', (r.out || '').trim().split('\n').filter(Boolean)[0]);
      }
    }

    // 待消费技能 changeset：一个技能变更一条 .changeset/*.md，字段不全会在发布前被 validate 拦下。
    {
      const cs = findScript('skill-changeset.mjs');
      if (!cs) info('技能 changeset', '未找到 skill-changeset.mjs —— 跳过');
      else {
        const r = runNode([cs, 'validate'], path.dirname(path.dirname(cs)), 20000);
        if (r.code !== 0) block('技能 changeset 校验失败', (r.out || '').trim().split('\n').slice(-4).join(' ｜ '));
        else ok('技能changeset校验通过', (r.out || '').trim().split('\n').slice(-1)[0]);
      }
    }

    // consume 的 exit 2 边界（REQ-20261122-26 遗留）：原先只有实跑日志，没有自动回归。
    // 把脚本复制到系统临时目录的假包根，造最小夹具，测真实行为；finally 删目录。
    {
      const cs = findScript('skill-changeset.mjs');
      if (!cs) info('技能 changeset consume 边界', '未找到 skill-changeset.mjs —— 跳过');
      else {
        const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'ai-workflow-changeset-'));
        try {
          const scriptDir = path.join(tmpRoot, 'scripts');
          fs.mkdirSync(scriptDir, { recursive: true });
          const tmpScript = path.join(scriptDir, 'skill-changeset.mjs');
          fs.copyFileSync(cs, tmpScript);
          const csDir = path.join(tmpRoot, '.changeset');
          fs.mkdirSync(csDir, { recursive: true });
          const fixture = '2026-01-01-demo-abcd1234.md';
          const consumedCopy = path.join(csDir, 'consumed', fixture);
          fs.writeFileSync(path.join(csDir, fixture), [
            '---',
            'skill: "demo"',
            'type: "changed"',
            'summary: "selfcheck 边界夹具"',
            'evidence: "临时目录，跑完即删"',
            'date: "2026-01-01"',
            '---',
            '',
          ].join('\n'), 'utf8');

          const cases = [
            { name: '空参数', args: ['consume'], code: 2, out: '缺少文件名' },
            { name: '非法文件名', args: ['consume', '../escape.md'], code: 2, out: '文件名不合法' },
            { name: '文件不存在', args: ['consume', '2026-01-01-missing.md'], code: 2, out: '待消费区没有这个文件' },
            {
              name: '正常消费', args: ['consume', fixture], code: 0, out: 'CONSUMED',
              after: () => !fs.existsSync(path.join(csDir, fixture))
                && fs.existsSync(consumedCopy)
                && /(^|\n)consumed:\s*\d{4}-\d{2}-\d{2}/.test(readText(consumedCopy)),
            },
            { name: '重复消费', args: ['consume', fixture], code: 2, out: '已消费过' },
          ];
          const bad = [];
          for (const c of cases) {
            const r = runNode([tmpScript, ...c.args], tmpRoot, 20000);
            if (r.code !== c.code) bad.push(`${c.name} 退出码 ${r.code}，期望 ${c.code}`);
            else if (c.out && !(r.out || '').includes(c.out)) bad.push(`${c.name} 输出缺「${c.out}」`);
            else if (c.after && !c.after()) bad.push(`${c.name} 落盘状态不符`);
          }
          if (bad.length) block('技能 changeset consume 边界退化', bad.join(' ｜ '));
          else ok('技能 changeset consume 边界', `${cases.length}/${cases.length} 通过（空 / 非法 / 不存在 / 正常 / 重复）`);
        } catch (e) {
          block('技能 changeset consume 边界无法验证', `沙箱构造或执行失败：${(e && e.message) || e}`);
        } finally {
          try { fs.rmSync(tmpRoot, { recursive: true, force: true }); } catch { /* 临时目录删不掉不阻断 */ }
        }
      }
    }
    // 交付形态：干完活怎么汇报，比工具是否全绿更影响体感。
    // 光有 md 不算 —— 包入口没指过去，就等于没人读（和「动效配方写了没人读」是同一个坑）。
    {
      // delivery-shape.md 在包根 references/，不在 harness/references/。
      // 新工程里 selfcheck 被拷到 <工程>/harness/scripts/，PKG 指向 <工程>/harness —— 沿本地上溯永远找不到
      // 宿主装的包。按引擎 findSkillsRoot 同口径，从 skillSource / 六宿主 skills 目录反查真正的包根。
      const home = process.env.HOME || process.env.USERPROFILE || '';
      let skillSource = null;
      try { skillSource = JSON.parse(readText(path.join(ROOT || '', 'harness', 'harness.config.json'))).skillSource || null; } catch { }
      const hostPkgs = ['trae-cn', 'codex', 'claude', 'workbuddy', 'deepseek', 'reasonix']
        .map(h => home ? path.join(home, '.' + h, 'skills', 'ai-workflow') : null).filter(Boolean);
      const pkgRoots = [
        skillSource ? path.dirname(skillSource) : null,
        ...hostPkgs,
        path.dirname(harnessPkg)
      ].filter(Boolean);
      const dsCands = [
        ...pkgRoots.map(r => path.join(r, 'references', 'delivery-shape.md')),
        path.join(harnessPkg, 'references', 'delivery-shape.md'),
        path.join(PKG, 'references', 'delivery-shape.md')
      ];
      const ds = dsCands.find(p => fs.existsSync(p));
      const top = path.join(PKG, 'SKILL.md');
      if (!ds) {
        block('交付形态规范缺失', 'references/delivery-shape.md 不在 —— 汇报又会退回「改了几个文件」');
      } else if (fs.existsSync(top) && !/delivery-shape\.md/.test(readText(top))) {
        info('交付形态没接进入口', 'SKILL.md 没提 delivery-shape.md —— 宿主副本没指向，不影响包本身');
      } else {
        ok('交付形态已接入', 'references/delivery-shape.md + SKILL.md 指向');
      }
    }
  }
}

/* ========== 组：未结事项健康度 ========== */
// 这张表是「文档里定的决定」唯一能被 agent 看见的通道。
// 它自己腐化（状态写错、标了已做却没证据）比没有更糟 —— 报着「欠 3 条」其实早做完了，或者反过来。
group('未结事项');
{
  const oiFile = ROOT ? path.join(ROOT, 'harness', 'memory', 'open-items.md') : null;
  if (!oiFile || !fs.existsSync(oiFile)) {
    info('未结事项', '本工程没有 harness/memory/open-items.md —— begin 或 init 会生成（不是阻断）');
  } else {
    const rows = readText(oiFile).split(/\r?\n/).filter(l => /^\|\s*OI-\d+/.test(l));
    const allowed = new Set(['待决', '未做', '部分', '已做', '已废弃']);
    const badState = [], noEvid = [], badCat = [];
    const cnt = { 待决: 0, 未做: 0, 部分: 0, 已做: 0, 已废弃: 0 };
    // 类别列与状态列重名时，人（和脚本）都会改错列 —— 反向验证时就因为这点漏拦过一次。
    const stateWords = new Set([...allowed, '待拍板']);
    for (const l of rows) {
      const c = l.split('|').map(s => s.trim());
      const id = c[1], cat = c[2], st = c[5], ev = c[6] || '';
      if (!allowed.has(st)) badState.push(`${id}「${st || '空'}」`);
      else cnt[st]++;
      if (stateWords.has(cat)) badCat.push(`${id}「${cat}」`);
      if (st === '已做' && (!ev || /^(无|待补|-|TBD)$/i.test(ev))) noEvid.push(id);
    }
    const probs = [];
    if (badState.length) probs.push(`状态不合法：${badState.slice(0, 3).join('、')}`);
    if (badCat.length) probs.push(`类别列写成了状态词：${badCat.slice(0, 3).join('、')}`);
    if (noEvid.length) probs.push(`标了已做却没证据：${noEvid.slice(0, 3).join('、')}`);
    if (probs.length) block('未结事项清单不健康', probs.join('；') + ' —— 状态或证据不实，这张表就是在骗人');
    else ok('未结事项清单健康', `${rows.length} 条：待决 ${cnt.待决} · 未做 ${cnt.未做} · 部分 ${cnt.部分} · 已做 ${cnt.已做} · 已废弃 ${cnt.已废弃}`);
  }
}

/* ========== 组：双分支一致性 ========== */
// 同一个脚本的文本输出与 --json 输出必须是同一份数（OI-19）。
// 这类 bug 不报错：人看文本、机器读 JSON，两边各自「看起来正确」。
// 防递归：本组调 branch-consistency，而它会调 selfcheck —— 给它 HARNESS_NO_BRANCH 标记让本组跳过。
group('双分支一致性');
{
  if (process.env.HARNESS_NO_BRANCH) {
    info('双分支一致性', '被 branch-consistency 调用，跳过（防递归）');
  } else {
    const bc = ROOT ? path.join(ROOT, 'harness', 'scripts', 'branch-consistency.mjs') : null;
    if (!bc || !fs.existsSync(bc)) {
      info('双分支一致性', '没有 harness/scripts/branch-consistency.mjs —— 跑一次 bootstrap');
    } else {
      const r = spawnSync(process.execPath, [bc, '--quick'], { cwd: ROOT, encoding: 'utf8', timeout: 120000 });
      const out = `${r.stdout || ''}${r.stderr || ''}`.trim();
      const first = out.split('\n').filter(Boolean)[0] || '';
      if (r.status === 0) ok('文本与 JSON 同口径', first);
      else block('文本与 JSON 口径不一致', out.split('\n').slice(0, 4).join('；'));
    }
  }
}

/* ========== 组：session 分支陈旧度 ========== */
// session/* 分支结项后应合回 main 并删除；删分支靠人记，没有机制就会反弹（同名分支曾删过又以另一条内容回来）。
// 判据：远端 session/* 已并入 main 且超过阈值未动 —— 7 天提示、30 天阻断。删前先补恢复锚点（见 NEXT.md）。
group('session 分支陈旧度');
{
  if (!ROOT) {
    info('session 分支陈旧度', '未定位项目根，跳过');
  } else {
    const r = spawnSync('git', ['for-each-ref', '--format=%(refname:short)|%(committerdate:unix)', 'refs/remotes'], { cwd: ROOT, encoding: 'utf8', timeout: SUBPROCESS_TIMEOUT });
    if (r.status !== 0) {
      info('session 分支陈旧度', 'git 不可用或非仓库，跳过');
    } else {
      const now = Math.floor(Date.now() / 1000);
      const DAY = 86400;
      const refs = [];
      for (const line of `${r.stdout || ''}`.split('\n')) {
        const m = /^([^/]+)\/(session\/[^|]+)\|(\d+)$/.exec(line.trim());
        if (m) refs.push({ ref: `${m[1]}/${m[2]}`, name: m[2], ts: Number(m[3]) });
      }
      if (!refs.length) {
        ok('session 分支陈旧度', '无 session/* 远端分支');
      } else {
        const lines = [];
        let level = 'ok';
        for (const x of refs.sort((a, b) => a.ref.localeCompare(b.ref))) {
          const anc = spawnSync('git', ['merge-base', '--is-ancestor', x.ref, 'main'], { cwd: ROOT, encoding: 'utf8', timeout: SUBPROCESS_TIMEOUT });
          if (anc.status !== 0) { lines.push(`${x.name} 未并入 main`); continue; }
          const age = Math.floor((now - x.ts) / DAY);
          if (age >= 30) { level = 'block'; lines.push(`${x.name} 已并入 main 且 ${age} 天未动 —— 删除它，删前先把 sha 补进 NEXT.md 恢复锚点`); }
          else if (age >= 7) { if (level === 'ok') level = 'info'; lines.push(`${x.name} 已并入 main 且 ${age} 天未动 —— 建议清理`); }
          else lines.push(`${x.name} 已并入 main，${age} 天`);
        }
        if (level === 'block') block('session 分支陈旧度', lines.join('；'));
        else if (level === 'info') info('session 分支陈旧度', lines.join('；'));
        else ok('session 分支陈旧度', lines.join('；'));
      }
    }
  }
}

/* ========== 组：风险与归因 ========== */
// 分档与失败归因都是「把判断变成数」的环节：规则写错就会给出看着对的错结论，
// 所以两个脚本各自带自测，每次自检都跑；档位一致性只提示不阻断（它是提醒，不是门禁）。
group('风险与归因');
{
  const check = (file, label) => {
    const f = ROOT ? path.join(ROOT, 'harness', 'scripts', file) : null;
    if (!f || !fs.existsSync(f)) { info(label, `没有 harness/scripts/${file} —— 跑一次 bootstrap`); return; }
    const r = selftestResults.get(label) || spawnSync(process.execPath, [f, '--selftest'], { cwd: ROOT, encoding: 'utf8', timeout: 30000 });
    const line = `${r.stdout || r.out || ''}${r.stderr || ''}`.trim().split('\n').filter(Boolean).pop() || '';
    if ((r.status ?? r.code) === 0) ok(`${label}自测通过`, line);
    else block(`${label}自测失败`, `${line} —— 规则写错了，结论会是假的`);
  };
  check('risk-tier.mjs', '风险分档');
  check('failure-reason.mjs', '失败归因');

  // 计划说改一行、结果动了 8 个文件 —— 这里点出来，避免拿开工时的 S 混过去
  const rt = ROOT ? path.join(ROOT, 'harness', 'scripts', 'risk-tier.mjs') : null;
  if (rt && fs.existsSync(rt)) {
    const r = spawnSync(process.execPath, [rt, '--json'], { cwd: ROOT, encoding: 'utf8', timeout: 30000 });
    try {
      const j = JSON.parse((r.stdout || '{}'));
      if (j.mismatch) info('档位一致性', `任务单声明 ${j.declared}，实际改动 ${j.tier}（${j.files} 文件）—— 以大的为准，回写 size`);
      else if (j.tier) ok('档位一致性', `声明与实际相符（${j.tier} · ${j.files} 文件）`);
    } catch { info('档位一致性', '无法解析 risk-tier 输出（可能不在 git 仓库里）'); }
  }
}

/* ========== 组：度量 ========== */
// 指标不采就是「全靠感觉」。阈值口径已定（OI-02）：M3/M4 必须 100%，其余先采基线。
// 这里只守两件事：脚本自己的解析没错（selftest），以及记录别断（30 天没新记录就提醒）。
group('度量');
{
  const ms = ROOT ? path.join(ROOT, 'harness', 'scripts', 'metrics.mjs') : null;
  if (!ms || !fs.existsSync(ms)) {
    info('度量', '没有 harness/scripts/metrics.mjs —— 跑一次 bootstrap');
  } else {
    const r = selftestResults.get('度量') || spawnSync(process.execPath, [ms, '--selftest'], { cwd: ROOT, encoding: 'utf8', timeout: 30000 });
    const line = `${r.stdout || r.out || ''}${r.stderr || ''}`.trim().split('\n').filter(Boolean).pop() || '';
    if ((r.status ?? r.code) === 0) ok('度量脚本自测通过', line);
    else block('度量脚本自测失败', `${line} —— 解析写错了，报告里的数就是假的`);

    const mf = path.join(ROOT, 'harness', 'metrics.md');
    if (!fs.existsSync(mf)) {
      info('度量记录', '还没有 harness/metrics.md —— 跑过 G2/G3 后自动生成');
    } else {
      const rows = readText(mf).split(/\r?\n/).filter(l => /^\| \d{4}-\d{2}-\d{2}/.test(l));
      if (!rows.length) info('度量记录', 'metrics.md 存在但还没有数据行');
      else {
        const last = (rows[rows.length - 1].split('|')[1] || '').trim();
        const days = Math.floor((Date.now() - new Date(last.slice(0, 10) + 'T00:00:00').getTime()) / 864e5);
        if (days > 30) info('度量记录', `最近一条是 ${last}（${days} 天前）—— 门禁很久没跑了，指标断档`);
        else ok('度量记录在更新', `${rows.length} 条 · 最近 ${last}`);
      }
    }
  }
}

/* ========== 组：写侧守卫 ========== */
// 危险写法要在「写入之前」被拦住，而不是等事后审查 —— 事后拦发现晚、返工贵。
// 但守卫自己的规则也会写错（`--force-with-lease` 被当成强推，就是自测抓出来的），
// 所以每次自检都跑一遍它的内置用例：该拦没拦、该放拦了，都会在这里暴露。
group('写侧守卫');
{
  const wg = ROOT ? path.join(ROOT, 'harness', 'scripts', 'write-guard.mjs') : null;
  if (!wg || !fs.existsSync(wg)) {
    block('写侧守卫缺失', 'harness/scripts/write-guard.mjs 不在 —— 危险写法会直接落进代码；跑一次 bootstrap');
  } else {
    const r = selftestResults.get('写侧守卫') || spawnSync(process.execPath, [wg, '--selftest'], { cwd: ROOT, encoding: 'utf8', timeout: 30000 });
    const line = ((r.stdout || r.out || '') + (r.stderr || '')).trim().split('\n').filter(Boolean).pop() || '';
    if ((r.status ?? r.code) === 0) ok('写侧守卫自测通过', line);
    else block('写侧守卫自测失败', `${line} —— 规则写错了，要么漏拦要么误伤`);
  }
}

/* ========== 组：协议预算（防回潮） ========== */
// 常驻一涨、黑话一回流，这套改造就白做了。所以把它固定成每次自检必查的红灯。
group('协议预算');
{
  if (!ROOT) {
    info('协议预算', '未识别项目根，跳过');
  } else {
    const measure = findScript('measure-tokens.mjs');
    if (!measure) {
      info('常驻体积', '未找到 measure-tokens.mjs —— 老工程没有此脚本，跳过');
    } else {
      const r = runNode([measure, '--json'], ROOT, 20000);
      let j = null;
      try { j = JSON.parse((r.out || '').trim()); } catch { }
      if (!j) info('常驻体积', 'measure-tokens 输出无法解析');
      else if (j.residentPass) ok('每轮常驻在预算内', `${j.residentWorst} / ${j.limit} token（${j.residentFile}）`);
      else block('每轮常驻超预算', `${j.residentWorst} / ${j.limit} token —— 把细节挪进 harness/references/manual.md`);
    }

    const lint = findScript('lint-words.mjs');
    if (!lint) {
      info('黑话扫描', '未找到 lint-words.mjs —— 跳过');
    } else {
      const r = runNode([lint, '--json'], ROOT, 20000);
      let j = null;
      try { j = JSON.parse((r.out || '').trim()); } catch { }
      if (!j) info('黑话扫描', 'lint-words 输出无法解析');
      else if (j.total === 0) ok('常驻与手册无黑话', `${j.report.length} 个文件扫过，0 处`);
      else block('发现黑话', `${j.total} 处 —— 跑 node harness/scripts/lint-words.mjs 看具体行，别整段闭眼跳过`);
    }
  }
}

/* ========== 组：dashboard 门禁口径（REQ-20261123-11 防线） ========== */
// dashboard 的 /api/progress 必须与 harness gate 同口径，否则卡片分母与门禁结论各说一套。
// 三条口径都能用源码断言判 —— 踩过一次就落成永久检查，不靠人记得。
group('dashboard 门禁口径');
{
  const sp = ROOT ? path.join(ROOT, 'docs', 'archive', 'dashboard', 'serve-project.mjs') : null;
  if (!sp || !fs.existsSync(sp)) {
    info('dashboard 门禁口径与 gate 一致', 'serve-project.mjs 不在，跳过（非 dashboard 工程）');
  } else {
    const src = readText(sp);
    const bad = [];
    // 1) parseState 必须取 §4 的最后一条机读行（与 harness.mjs parseGateLine 同口径，最新一条才代表当前结论）
    if (!/while\s*\(\(hit\s*=\s*re\.exec\(text\)\)\s*!==\s*null\)\s*last\s*=\s*hit/.test(src)) {
      bad.push('parseState 未取最后一条门禁行 —— 会读到历史流水，与 gate 结论不一致');
    }
    // 2) 门禁枚举必须含「未开始」，否则未开始的门禁落 unknown 而不是 pending
    if (!/待确认\|未开始/.test(src)) {
      bad.push('门禁枚举缺「未开始」 —— 未开始的门禁会落 unknown');
    }
    // 3) evidence 判据必须排除空壳目录（只有子目录不算非空，否则分母偏大）
    if (!/\.some\(\(e\)\s*=>\s*e\.isFile\(\)\)/.test(src)) {
      bad.push('evidence 判据含子目录空壳 —— 空壳包会混进分母');
    }
    if (bad.length) block('dashboard 门禁口径与 gate 一致', bad.join('；'));
    else ok('dashboard 门禁口径与 gate 一致', '§4 取末条、枚举含未开始、evidence 排除空壳目录');
  }
}

/* ========== 组 11：审查项是否落成检查（自我进化的入口） ========== */
// 踩过的坑必须变成永久检查，否则下次同类问题还会漏。
// 在**提交时**就扫最新需求包的审查记录，比等到 G3 早得多。
group('审查项落地');
{
  if (!ROOT) {
    info('审查项落地', '未识别项目根，跳过');
  } else {
    const reqRoot = path.join(ROOT, 'harness', 'requirements');
    const dirs = fs.existsSync(reqRoot) ? listDirs(reqRoot).sort() : [];
    // 对每个有审查记录的包都判，而不是只看最新那个。
    // 只看最新时，新建一个需求包会让历史包复跑 G3 判「检查项名不存在」——
    // 因为 G3 拿 selfcheck 本轮产出的名字去比对，而本轮根本没跑那些包。
    const packets = dirs.filter(d => fs.existsSync(path.join(reqRoot, d, '03-review.md')));
    // 只看「性质」列（第 3 列），整行扫会把问题描述里的「可机检」也算进来
    const natureOf = l => {
      const c = l.split('|').slice(1, -1).map(x => x.trim().replace(/[`*]/g, ''));
      return c[2] || '';
    };
    // 模板里的示例行会带 <TBD> 之类占位符 —— 新工程刚 init 时还没人写评审，
    // 那些行不算真实审查项，否则新工程一装就 BLOCKED（踩过）
    const isPlaceholder = l => /<TBD>|<selfcheck|<diff|<[^|>]{0,20}>/.test(l);
    // 已有检查项名：results 里已收集的 + 本组自己可能产生的三个名字
    // （本组的名要等判定完才 push 进 results，不预先加进来会把自己判成「不存在」）
    const KNOWN_SELF = ['可机检项已落成检查', '可机检项未落成检查', '「已补检查」写了不存在的检查项'];
    const known = new Set([
      ...results.filter(r => r.group !== '审查项落地').map(r => r.name),
      ...KNOWN_SELF
    ]);
    // 无论通过还是失败都用同一个名字 —— 否则「已补检查」里填的名字会随状态变，
    // G3 拿它去比对时就对不上（这是设计缺陷，踩过）
    const NAME = '可机检项已落成检查';
    const hard = [], soft = [], skipped = [];
    let checkedPkgs = 0, machineTotal = 0;
    for (const p of packets) {
      const rf = path.join(reqRoot, p, '03-review.md');
      const stf = path.join(reqRoot, p, 'state.md');
      const closed = fs.existsSync(stf) && /G4:\s*PASS/.test(readText(stf));
      const allRows = readText(rf).split(/\r?\n/)
        .filter(l => /^\s*\|/.test(l) && /(可机检|需人判断)/.test(natureOf(l)));
      const rows = allRows.filter(l => !isPlaceholder(l));
      if (!rows.length) { skipped.push(p); continue; }
      checkedPkgs++;
      const machineRows = rows.filter(l => /可机检/.test(natureOf(l)));
      machineTotal += machineRows.length;
      const missing = [], fake = [];
      for (const row of machineRows) {
        const cells = row.split('|').slice(1, -1).map(x => x.trim().replace(/[`*]/g, ''));
        const raw = (cells[7] || '').trim();
        const id = (row.match(/(?:RV|H|M|L)[-\s]?\d+/i) || [row.slice(0, 10)])[0];
        if (!raw || raw === '—' || raw === '-' || /^<.*>$/.test(raw)) { missing.push(`${p}:${id}`); continue; }
        const bad = raw.split(/[、,，]/).map(x => x.trim()).filter(Boolean).filter(w => !known.has(w));
        if (bad.length) fake.push(`${p}:${id}（${bad.join('、')}）`);
      }
      const msg = missing.length
        ? `${missing.join('、')} 标了可机检却没写检查项名 —— 坑不落成检查，下次还会踩`
        : fake.length
          ? `${fake.join('、')} 写的名字不在检查项里 —— 要与 selfcheck 的检查项完全一致`
          : '';
      if (msg) (closed ? soft : hard).push(msg);
    }
    if (!checkedPkgs) {
      info('审查项落地', packets.length
        ? '审查记录都还是模板（含占位符），没开始写 —— 跳过'
        : '还没有需求包或没有审查记录，跳过');
    } else if (hard.length) {
      block(NAME, hard.join('；'));
    } else if (soft.length) {
      // 已收口的包早就冻结了，不该因为后来加规矩就把它判红；但也不闷着，报出来
      info(NAME, `已收口包的历史记录有偏差（不影响当前）：${soft.join('；')}`);
    } else {
      ok(NAME, `${checkedPkgs} 个包 · ${machineTotal} 项可机检，检查项均存在`);
    }
  }
}

/* ---------- 组 12：并发纪律入口在位 ---------- */
// 多会话并发：工具要能建 session/* 工作树、main 只收已收口需求包、四个入口都写纪律段。
group('并发纪律');
{
  if (!ROOT) { info('并发纪律', '未识别项目根，跳过'); }
  else {
    const ga = readText(path.join(ROOT, 'harness', 'scripts', 'git-atomic.mjs'));
    const gaMiss = [];
    if (!/cmd === 'worktree'/.test(ga) || !/session\//.test(ga)) gaMiss.push('git-atomic.mjs 缺 session/* worktree');
    if (!/cmd === 'merge'/.test(ga) || !/未归档/.test(ga)) gaMiss.push('git-atomic.mjs 缺 merge 归档门槛');
    const entries = ['AGENTS.md', '.cursorrules', 'CLAUDE.md', '.trae/rules/ai-workflow.md'];
    const noMark = [], absent = [];
    for (const rel of entries) {
      const p = path.join(ROOT, rel);
      if (!fs.existsSync(p)) { absent.push(rel); continue; }
      if (!readText(p).includes('多会话并发纪律')) noMark.push(rel);
    }
    if (gaMiss.length) block('并发纪律入口缺失', gaMiss.join('；'));
    else if (noMark.length) block('并发纪律段缺失', `${noMark.join('、')} 未写「多会话并发纪律」段`);
    else ok('并发纪律入口在位', `git-atomic worktree/merge + ${entries.length - absent.length} 个入口`);
    if (absent.length) info('并发纪律入口未建', `${absent.join('、')} 不存在，按宿主补`);
  }
}

/* ---------- 输出 ---------- */
// --group 只输出指定检查组；退出码也只按该组判定
const selected = groupArg ? results.filter(r => r.group === groupArg) : results;
if (groupArg && !selected.length) {
  console.error(`没有名为「${groupArg}」的检查组。可用：${[...new Set(results.map(r => r.group))].join('、')}`);
  process.exit(1);
}
const groups = [];
for (const r of selected) {
  let g = groups.find(x => x.name === r.group);
  if (!g) { g = { name: r.group, items: [] }; groups.push(g); }
  g.items.push(r);
}
const nBlock = selected.filter(r => r.level === 'block').length;
const nInfo = selected.filter(r => r.level === 'info').length;
const nOk = selected.filter(r => r.level === 'ok').length;

const fullText = (() => {
  const MARK = { ok: '✓', info: '⚠', block: '✖' };
  const lines = [`自检 · ${ROOT ? '项目根 ' + ROOT : '未识别项目根'}${groupArg ? ' · 检查组 ' + groupArg : ''}`];
  for (const g of groups) {
    lines.push('', `[${g.name}]`);
    for (const it of g.items) lines.push(`  ${MARK[it.level]} ${it.name}${it.detail ? ' — ' + it.detail : ''}`);
  }
  lines.push('', '─'.repeat(52));
  lines.push(`阻断 ${nBlock} · 提示 ${nInfo} · 放行 ${nOk}`);
  lines.push(nBlock ? 'BLOCKED —— 先修阻断项再动手' : 'READY —— 可以动手');
  return lines.join('\n');
})();

function writeBriefLog(text) {
  const candidates = [
    ROOT ? path.join(ROOT, 'harness', 'logs', 'selfcheck-latest.log') : null,
    path.join(os.tmpdir(), 'ai-workflow-selfcheck-latest.log')
  ].filter(Boolean);
  for (const file of candidates) {
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, text + '\n', 'utf8');
      return file;
    } catch { /* 尝试下一个位置 */ }
  }
  return null;
}

if (wantJson) {
  console.log(JSON.stringify({
    at: new Date().toISOString(), root: ROOT, pkg: PKG, group: groupArg || null,
    summary: { 阻断: nBlock, 提示: nInfo, 放行: nOk },
    groups, results: selected
  }, null, 2));
} else if (wantBrief) {
  const logFile = writeBriefLog(fullText);
  const state = nBlock ? 'BLOCKED' : 'READY';
  const logText = logFile ? (path.relative(ROOT || process.cwd(), logFile).replace(/\\/g, '/') || logFile) : 'write-failed';
  console.log(`SELFCHECK BRIEF ${state} · 阻断 ${nBlock} · 提示 ${nInfo} · 放行 ${nOk} · log=${logText}`);
  const blocked = selected.filter(r => r.level === 'block');
  for (const item of blocked.slice(0, 5)) {
    console.log(`- ${item.name}${item.detail ? '：' + item.detail.slice(0, 180) : ''}`);
  }
  if (blocked.length > 5) console.log(`- 另有 ${blocked.length - 5} 条阻断，详见日志`);
  if (!logFile) console.error(fullText);
} else {
  console.log(fullText);
}

process.exit(nBlock ? 1 : 0);
