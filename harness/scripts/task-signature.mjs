#!/usr/bin/env node
// 重复任务识别 —— 同一个任务做第二次，能被认出来。
//
// 设计约束（来自 Bazel 的教训）：**假命中比漏命中更糟**。
// false cache hit 会让人复用错的结论，比"没提醒"危害大得多。
// 所以：签名必须含输入（路径集合 + 文件内容 hash），不能只比描述相似度；
// 输出分两级 —— 输入全同才说「可直接复用」，有差只给「仅参考」并列出差异；
// 两者都不满足就**什么也不说**，不为了显得聪明而硬凑。
//
// 用法：
//   node harness/scripts/task-signature.mjs check --desc "<任务描述>" [--paths a,b]
//   node harness/scripts/task-signature.mjs record --desc "<描述>" --artifact "<产物路径>" [--paths a,b]
import { spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const args = process.argv.slice(2);
const optVal = (n, d = null) => { const i = args.indexOf(n); return i >= 0 && args[i + 1] !== undefined ? args[i + 1] : d; };
const has = n => args.includes(n);
const cmd = args[0];
const ROOT = optVal('--root') || process.cwd();
const LEDGER = path.join(ROOT, 'harness', 'memory', 'tasks.md');
const MAX_ROWS = 500;
const KEEP_ROWS = 300;

function localDate(d = new Date()) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function localStamp(d = new Date()) {
  return `${localDate(d)} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** 归一化描述：去掉易变词（日期/单号/数量/语气），否则同一个任务第二次就认不出来了 */
function normDesc(s) {
  return String(s)
    .replace(/\d{4}-\d{2}-\d{2}/g, ' ')
    .replace(/REQ-\d{6}-\d+/gi, ' ')
    .replace(/[（(]\s*\d+\s*[)）]/g, ' ')
    .replace(/(\d+)\s*(个|条|次|处|行|天|版)/g, ' ')
    .replace(/[，。；、！？,.!?;:：]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
}

// 过程产物不是任务输入。它们会被任务本身改动 —— 若算进签名，同一个任务第二次
// 必然算不出同一个签名，REUSE 永远不成立。所以显式排除。
// 例外：任务本身就是改这些文件时，用 --paths 显式指定，别靠自动推断。
const PROCESS_ARTIFACT = [
  // 整个 harness/ 都是工具（engine / scripts / templates / references / memory / requirements…）。
  // 新工程一 init 就生成 40+ 个文件，算进签名的话 bootstrap 一同步就全变，
  // 之后每次 check 只会得到 REFERENCE 噪声。任务本身就是在改工作流代码时，用 --paths 显式指定。
  /^harness\//,
  // 入口文件与宿主规则也是生成的
  /^(AGENTS|CLAUDE)\.md$/i,
  /^\.(trae|codex|claude|workbuddy|deepseek|reasonix)\//,
  /^\.workbuddy\//
];
const isProcessArtifact = p => PROCESS_ARTIFACT.some(re => re.test(p));

/** 取路径集合：优先 --paths，否则用 git 的改动清单 */
function currentPaths() {
  const given = optVal('--paths', null);
  if (given) return given.split(',').map(s => s.trim()).filter(Boolean).sort();
  const out = new Set();
  for (const argv of [['diff', '--name-only'], ['diff', '--name-only', '--cached'], ['ls-files', '--others', '--exclude-standard']]) {
    try {
      const r = spawnSync('git', argv, { cwd: ROOT, encoding: 'utf8', timeout: 15000 });
      for (const l of (r.stdout || '').split(/\r?\n/)) if (l.trim()) out.add(l.trim());
    } catch { /* 不是 git 仓库：路径集合为空，签名会退化 —— 这种情况下不给复用建议更安全 */ }
  }
  return [...out].filter(p => !isProcessArtifact(p)).sort();
}

/** 文件内容 hash：路径不存在记 'absent'，这样"文件被删了"也能体现在签名里 */
function hashOf(rel) {
  try {
    const abs = path.join(ROOT, rel);
    if (!fs.existsSync(abs) || !fs.statSync(abs).isFile()) return 'absent';
    return crypto.createHash('sha1').update(fs.readFileSync(abs)).digest('hex').slice(0, 10);
  } catch { return 'err'; }
}

function buildSignature(desc, paths) {
  const basis = [normDesc(desc), paths.join(','), paths.map(p => `${p}:${hashOf(p)}`).join(',')].join('|');
  return {
    sig: crypto.createHash('sha1').update(basis).digest('hex').slice(0, 12),
    norm: normDesc(desc)
  };
}

function readLedger() {
  if (!fs.existsSync(LEDGER)) return [];
  return fs.readFileSync(LEDGER, 'utf8').split(/\r?\n/)
    .filter(l => l.trim() && !l.trim().startsWith('#'))
    .map(l => {
      // 用「空格竖线空格」切：描述里出现单个 | 时不至于把列切错
      const c = l.split(/\s\|\s/).map(s => s.trim());
      if (c.length < 7) return null;
      return { stamp: c[0], norm: c[1], sig: c[2], paths: c[3], artifact: c[4], count: Number(c[5]) || 1, last: c[6] };
    })
    .filter(Boolean);
}

if (cmd === 'check') {
  const desc = optVal('--desc');
  if (!desc) { console.error('用法：task-signature.mjs check --desc "<任务描述>" [--paths a,b]'); process.exit(1); }
  const paths = currentPaths();
  const { sig, norm } = buildSignature(desc, paths);
  const rows = readLedger();

  // 路径集合为空 = 无法核对输入（干净工作区 / 非 git 仓库 / 自动推断失败）。
  // 此时签名只剩描述，说「输入与上次完全一致」是假承诺 —— 不能给 REUSE。
  // 但也别只说「不给建议」：上次做过同一件事这件事本身就是有用信息，照实说清能参考什么、不能参考什么。
  if (!paths.length) {
    const sameDesc = rows.filter(r => r.norm === norm);
    if (sameDesc.length) {
      const r = sameDesc[sameDesc.length - 1];
      const np = r.paths ? r.paths.split(',').filter(Boolean).length : 0;
      console.log(`NOTE  描述相同的任务做过 ${r.count} 次（最近 ${r.last}${np ? `，上次涉及 ${np} 个文件` : ''}）`);
      if (r.artifact && r.artifact !== '-') console.log(`      上次产物：${r.artifact}`);
      console.log('      但工作区干净，推不出这次会动哪些文件 —— **输入是否一致无法核对**。');
      console.log('      产物可以拿来参考，别当结论；要正式判断就补 --paths a,b。');
    } else {
      console.log('NOTE  没推断出涉及的路径（工作区干净或不在 git 仓库里），也没有可比的历史 —— 不给复用建议。');
      console.log('      做完一件事想让它下次被认出来：record --desc "..." --paths a,b 明确指定涉及的文件。');
    }
    process.exit(0);
  }

  // 一级：签名完全一致 —— 描述 + 路径集合 + 每个文件的内容 hash 都同
  const exact = rows.filter(r => r.sig === sig);
  if (exact.length) {
    const r = exact[exact.length - 1];
    console.log(`REUSE 可直接复用 —— 输入与上次完全一致（做过 ${r.count} 次，最近 ${r.last}）`);
    if (r.artifact && r.artifact !== '-') console.log(`  产物：${r.artifact}`);
    console.log('  输入没变，结论大概率没变；先看产物，别从零重做。');
    process.exit(0);
  }

  // 二级：描述相同但输入有差 —— 只给参考，并逐条列出差异
  const sameDesc = rows.filter(r => r.norm === norm);
  if (sameDesc.length) {
    const r = sameDesc[sameDesc.length - 1];
    const prev = new Set(r.paths ? r.paths.split(',').map(s => s.trim()).filter(Boolean) : []);
    const now = new Set(paths);
    const added = [...now].filter(p => !prev.has(p));
    const gone = [...prev].filter(p => !now.has(p));
    if (added.length || gone.length) {
      console.log(`REFERENCE 仅参考 —— 描述相同，但涉及的路径变了（上次 ${r.last}）`);
      for (const a of added.slice(0, 8)) console.log(`  + 新增 ${a}`);
      for (const g of gone.slice(0, 8)) console.log(`  - 不再涉及 ${g}`);
      if (prev.size && added.length < 8 && gone.length < 8) console.log(`  = ${[...now].filter(p => prev.has(p)).length} 个路径与上次重合`);
      if (r.artifact && r.artifact !== '-') console.log(`  上次产物：${r.artifact}`);
      console.log('  **别直接搬** —— 输入变了，结论未必还成立。');
      process.exit(0);
    }
    if (paths.length) {
      // 路径完全一样但签名不同 → 只可能是内容变了（签名含内容 hash）。
      // 但台账只存了路径没存逐文件 hash，所以不能声称"这几个都变了"——
      // 只能说"这 N 个里至少有一个变了，具体哪个要自己比"。
      console.log(`REFERENCE 仅参考 —— 描述与路径都同，但文件内容变了（上次 ${r.last}）`);
      console.log(`  ~ 这 ${paths.length} 个路径里至少有一个改了，具体哪个要自己比（台账没存逐文件 hash）`);
      if (r.artifact && r.artifact !== '-') console.log(`  上次产物：${r.artifact}`);
      console.log('  **结论要重新判断** —— 同样的文件改了内容，上次的结论未必还成立。');
      process.exit(0);
    }
  }

  // 三级：没有可比历史 —— 什么都不说（宁可不提，也不给错建议）
  process.exit(0);
}

if (cmd === 'record') {
  const desc = optVal('--desc');
  const artifact = optVal('--artifact', '-');
  if (!desc) { console.error('用法：task-signature.mjs record --desc "<描述>" --artifact "<产物路径>" [--paths a,b]'); process.exit(1); }
  const paths = optVal('--paths') ? optVal('--paths').split(',').map(s => s.trim()).filter(Boolean).sort() : currentPaths();
  const { sig, norm } = buildSignature(desc, paths);
  const stamp = localStamp();
  const rows = readLedger();
  const prev = rows.filter(r => r.sig === sig);
  const count = prev.length ? Math.max(...prev.map(r => r.count)) + 1 : 1;

  fs.mkdirSync(path.dirname(LEDGER), { recursive: true });
  if (!fs.existsSync(LEDGER)) {
    fs.writeFileSync(LEDGER, '# 任务台账（自动生成 · 勿手改）\n# 格式：时间 | 归一化描述 | 签名 | 路径 | 产物 | 次数 | 最近时间\n', 'utf8');
  }
  fs.appendFileSync(LEDGER, `${stamp} | ${norm} | ${sig} | ${paths.join(',')} | ${artifact} | ${count} | ${stamp}\n`, 'utf8');

  // 台账膨胀控制：超 500 条只留最近 300 条
  const all = fs.readFileSync(LEDGER, 'utf8').split(/\r?\n/);
  const head = all.filter(l => l.startsWith('#'));
  const body = all.filter(l => l.trim() && !l.startsWith('#'));
  if (body.length > MAX_ROWS) {
    fs.writeFileSync(LEDGER, [...head, ...body.slice(-KEEP_ROWS)].join('\n') + '\n', 'utf8');
    console.log(`RECORD 已记（台账 ${body.length} 条，保留最近 ${KEEP_ROWS} 条）`);
  } else {
    console.log(`RECORD 已记 · 签名 ${sig} · ${paths.length} 个路径 · 第 ${count} 次`);
  }
  process.exit(0);
}

console.log('用法：');
console.log('  task-signature.mjs check  --desc "<任务描述>" [--paths a,b]   查历史，给 REUSE / REFERENCE / 什么都不说');
console.log('  task-signature.mjs record --desc "<描述>" --artifact "<产物>"  做完记一条');
process.exit(1);
