#!/usr/bin/env node
// 记忆跨设备同步：只处理 harness/memory/ 下五个文件，按条目身份证三方合并。
// 设计来源 docs/14 §2.4：base/ours/theirs 逐条决策，冲突只写侧车，禁止带冲突推送。
// 退出码：0 成功 / 1 用法或环境错 / 2 有未解决冲突
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawnSync } from 'node:child_process';

const argv = process.argv.slice(2);
const cmd = argv[0];
const opt = (name, def) => {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : def;
};
const VALUE_FLAGS = new Set(['--remote', '--branch', '--take']);
const positional = argv.slice(1).filter((a, i) => !a.startsWith('--') && !VALUE_FLAGS.has(argv[i]));

function findRoot(start) {
  let d = path.resolve(start);
  for (;;) {
    if (fs.existsSync(path.join(d, 'harness', 'scripts'))) return d;
    const up = path.dirname(d);
    if (up === d) return null;
    d = up;
  }
}
const ROOT = findRoot(process.cwd());
const MEM_REL = path.join('harness', 'memory');
const FILES = ['inbox.md', 'long-term.md', 'recall-ledger.md', 'history.md', 'open-items.md'];
const REMOTE = opt('--remote', 'gitee');
const BRANCH = opt('--branch', 'main');
const REF = `${REMOTE}/${BRANCH}`;
const SIDECAR = path.join(ROOT || '.', MEM_REL, 'CONFLICTS.md');
const MARK = '--- conflict ---';

function die(code, msg) { console.error(msg); process.exit(code); }
function git(args) { return spawnSync('git', args, { cwd: ROOT, encoding: 'utf8' }); }
function gitOut(args) { const r = git(args); return r.status === 0 ? (r.stdout || '') : null; }
function ensureEnv() {
  if (!ROOT) die(1, '找不到工程根（向上没有 harness/scripts/）');
  if (!gitOut(['rev-parse', '--is-inside-work-tree'])) die(1, '不是 git 仓库：同步只能在 git 工程里跑');
  if (!gitOut(['rev-parse', '--verify', '--quiet', REF])) die(1, `本地没有 ${REF} —— 先 git fetch ${REMOTE} ${BRANCH}`);
}
function lines(t) { return String(t || '').split(/\r?\n/); }
function isEntry(line) {
  const t = line.trim();
  return !!t && !t.startsWith('#') && !t.startsWith('>') && !t.startsWith('```');
}
function keyOf(line) {
  const m = /\[id:([0-9a-f]{8})\]/.exec(line);
  if (m) return `id:${m[1]}`;
  // 行尾状态标记不算身份的一部分：加 [promoted]/[dropped] 不应让同一条变成「删除+新增」
  const stripped = line.replace(/\s*\[(?:stale|stale-content|promoted|dropped)\]\s*$/g, '').trim();
  // open-items 这类表格用首列编号当稳定 key，否则文字一改就认不出是同一条
  const oi = /^\|\s*(OI-\d+)/.exec(stripped);
  if (oi) return `oi:${oi[1]}`;
  return `txt:${stripped}`;
}
function index(linesArr) {
  const map = new Map();
  for (const l of linesArr) if (isEntry(l)) map.set(keyOf(l), l);
  return map;
}
function showAt(rev, rel) {
  const r = git(['show', `${rev}:${rel.replace(/\\/g, '/')}`]);
  return r.status === 0 ? (r.stdout || '') : '';
}
function localText(rel) {
  const f = path.join(ROOT, rel);
  return fs.existsSync(f) ? fs.readFileSync(f, 'utf8') : '';
}
function baseOf() { return (gitOut(['merge-base', 'HEAD', REF]) || '').trim(); }

/** 单文件三方合并：返回新文本、冲突、远端新增/替换计数 */
function mergeFile(baseText, oursText, theirsText) {
  const baseMap = index(lines(baseText));
  const oursMap = index(lines(oursText));
  const theirsMap = index(lines(theirsText));
  const conflicts = [];
  const replaced = new Map();
  const additions = [];
  const removed = new Set();
  for (const k of new Set([...oursMap.keys(), ...theirsMap.keys(), ...baseMap.keys()])) {
    const o = oursMap.get(k), t = theirsMap.get(k), b = baseMap.get(k);
    if (o && t) {
      if (o === t) continue;
      if (b === o) { replaced.set(k, t); continue; }        // 只有远端改
      if (b === t) continue;                                 // 只有本地改
      conflicts.push({ key: k, ours: o, theirs: t });         // 双改
    } else if (o && !t) {
      if (b === o) { removed.add(k); continue; }             // 本地未改、远端删 → 真删
      if (!b) continue;                                       // 本地新增（远端没有）
      conflicts.push({ key: k, ours: o, theirs: '' });        // 远端删、本地改
    } else if (!o && t) {
      if (b === t || !b) { additions.push(t); continue; }     // 远端新增
      conflicts.push({ key: k, ours: '', theirs: t });        // 本地删、远端改
    }
    // 双方都没有 → 已删除，不输出
  }
  const out = [];
  let lastEntryIdx = -1;
  for (const l of lines(oursText)) {
    if (isEntry(l) && removed.has(keyOf(l))) continue;
    if (isEntry(l)) lastEntryIdx = out.length;
    const k = isEntry(l) ? keyOf(l) : null;
    out.push(k && replaced.has(k) ? replaced.get(k) : l);
  }
  if (additions.length) {
    if (lastEntryIdx < 0) out.push(...additions);
    else out.splice(lastEntryIdx + 1, 0, ...additions);
  }
  return { text: out.join('\n'), conflicts, added: additions.length, replaced: replaced.size };
}

function readSidecar() {
  if (!fs.existsSync(SIDECAR)) return [];
  const out = [];
  let cur = null;
  for (const line of lines(fs.readFileSync(SIDECAR, 'utf8'))) {
    if (line.trim() === MARK) { if (cur) out.push(cur); cur = { file: '', key: '', ours: '', theirs: '' }; continue; }
    if (!cur) continue;
    const m = /^(file|key|ours|theirs):\s?(.*)$/.exec(line);
    if (m) cur[m[1]] = m[2];
  }
  if (cur) out.push(cur);
  return out;
}
function writeSidecar(list) {
  if (!list.length) { fs.rmSync(SIDECAR, { force: true }); return; }
  const out = ['# 记忆同步冲突', '', '> 未解决前禁止 merge / push。解决：node harness/scripts/memory-sync.mjs resolve <id> --take ours|theirs|both', ''];
  for (const c of list) {
    out.push(MARK, `file: ${c.file}`, `key: ${c.key}`, `ours: ${c.ours}`, `theirs: ${c.theirs}`, '');
  }
  fs.writeFileSync(SIDECAR, out.join('\n'), 'utf8');
}

function diffStat(baseText, oursText, theirsText) {
  const b = index(lines(baseText)), o = index(lines(oursText)), t = index(lines(theirsText));
  let localOnly = 0, remoteOnly = 0, bothChanged = 0;
  for (const [k, v] of o) if (!b.has(k) && !t.has(k)) localOnly++;
  for (const [k, v] of t) if (!b.has(k) && !o.has(k)) remoteOnly++;
  for (const [k, v] of o) if (t.has(k) && t.get(k) !== v) bothChanged++;
  return { localOnly, remoteOnly, bothChanged };
}

if (cmd === 'status') {
  ensureEnv();
  const base = baseOf();
  if (!base) die(1, `找不到与 ${REF} 的共同祖先`);
  const side = readSidecar();
  console.log(`SYNC STATUS · 对比 ${REF}（base ${base.slice(0, 8)}）`);
  let total = 0;
  for (const f of FILES) {
    const rel = path.join(MEM_REL, f);
    const s = diffStat(showAt(base, rel), localText(rel), showAt(REF, rel));
    total += s.localOnly + s.remoteOnly + s.bothChanged;
    console.log(`  ${f.padEnd(18)} 仅本地 ${s.localOnly} · 仅远端 ${s.remoteOnly} · 双方都改 ${s.bothChanged}`);
  }
  console.log(total === 0 && !side.length ? '无差异' : `未解决冲突 ${side.length} 条${side.length ? '（先 resolve）' : ''}`);
  process.exit(0);
}

if (cmd === 'merge') {
  ensureEnv();
  const pending = readSidecar();
  if (pending.length) die(2, `还有 ${pending.length} 条未解决冲突（${path.relative(ROOT, SIDECAR)}），先 resolve 再 merge`);
  const fr = git(['fetch', REMOTE, BRANCH]);
  if (fr.status !== 0) die(1, `git fetch ${REMOTE} ${BRANCH} 失败：${(fr.stderr || '').trim()}`);
  const base = baseOf();
  if (!base) die(1, '找不到共同祖先 —— 先 git fetch 并确认分支存在');
  const all = [];
  let files = 0, added = 0, replaced = 0;
  for (const f of FILES) {
    const rel = path.join(MEM_REL, f);
    const b = showAt(base, rel), o = localText(rel), t = showAt(REF, rel);
    const r = mergeFile(b, o, t);
    for (const c of r.conflicts) all.push({ file: f, ...c });
    if (r.text !== o.replace(/\r\n/g, '\n')) {
      fs.writeFileSync(path.join(ROOT, rel), r.text, 'utf8');
      files++; added += r.added; replaced += r.replaced;
    }
  }
  writeSidecar(all);
  if (all.length) {
    console.error(`MERGE 冲突 ${all.length} 条，已写入 ${path.relative(ROOT, SIDECAR)}；未解决前禁止 push`);
    process.exit(2);
  }
  console.log(`MERGE OK · 更新 ${files} 个文件（远端新增 ${added} / 远端修改 ${replaced}）`);
  process.exit(0);
}

if (cmd === 'resolve') {
  const id = (positional[0] || '').trim();
  const take = opt('--take', '');
  if (!id || !['ours', 'theirs', 'both'].includes(take)) die(1, '用法：resolve <id> --take ours|theirs|both');
  const side = readSidecar();
  const hit = side.filter(c => c.key === `id:${id}` || c.key === id);
  if (!hit.length) die(1, `侧车里没有 ${id}`);
  for (const c of hit) {
    const file = path.join(ROOT, MEM_REL, c.file);
    const arr = lines(fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '');
    const idx = arr.findIndex(l => isEntry(l) && keyOf(l) === c.key);
    if (take === 'ours') {
      // 保留本地：本地有则不动；本地删了（ours 为空）就保持删除
    } else if (take === 'theirs') {
      if (!c.theirs) { if (idx >= 0) arr.splice(idx, 1); }     // 远端删除
      else if (idx >= 0) arr[idx] = c.theirs;
      else arr.push(c.theirs);
    } else if (c.theirs) {
      // both：远端删除时没有第二条可留，等同保留本地
      const used = new Set([...arr.join('\n').matchAll(/\[id:([0-9a-f]{8})\]/g)].map(m => m[1]));
      let newId;
      do { newId = crypto.randomBytes(4).toString('hex'); } while (used.has(newId));
      const extra = c.theirs.replace(/\[id:[0-9a-f]{8}\]/, `[id:${newId}]`);
      if (idx >= 0) arr.splice(idx + 1, 0, extra); else arr.push(extra);
    }
    fs.writeFileSync(file, arr.join('\n'), 'utf8');
  }
  const rest = side.filter(c => !hit.includes(c));
  writeSidecar(rest);
  console.log(`RESOLVED ${id} --take ${take}（剩余冲突 ${rest.length}）`);
  process.exit(0);
}

if (cmd === 'push') {
  ensureEnv();
  const side = readSidecar();
  if (side.length) die(2, `还有 ${side.length} 条未解决冲突，禁止 push`);
  const paths = FILES.map(f => path.join(MEM_REL, f).replace(/\\/g, '/'));
  const st = gitOut(['status', '--porcelain', '--', ...paths]);
  if (st && st.trim()) {
    git(['add', ...paths]);
    const c = git(['commit', '-m', 'chore(memory): 跨设备同步记忆', '--', ...paths]);
    if (c.status !== 0) die(1, `提交失败：${(c.stderr || c.stdout || '').trim()}`);
  }
  const p = git(['push', REMOTE, BRANCH]);
  if (p.status !== 0) {
    const err = (p.stderr || p.stdout || '').trim();
    if (/non-fast-forward|fetch first|Updates were rejected/i.test(err)) {
      die(1, `push 被拒：本地历史落后于 ${REF}。先跑 git pull --no-rebase 合并历史（记忆文件已同步，冲突概率低），再重新 push。`);
    }
    die(1, `push 失败：${err}`);
  }
  console.log(`PUSH OK → ${REF}`);
  process.exit(0);
}

console.log('memory-sync.mjs: status | merge | resolve <id> --take ours|theirs|both | push   [--remote gitee|github] [--branch main]');
process.exit(1);
