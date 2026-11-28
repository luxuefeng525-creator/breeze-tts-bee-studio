#!/usr/bin/env node
// 变更台账：把 git 历史变成人能翻的档案。
//
// CHANGELOG 是「发布说明」——给用户看这一版有什么；
// 台账是「改动流水」——给自己回看「改了什么文件、为什么这么改、影响了什么」。
//
// 为什么必须有它：commit message 里的取舍只活在 git log 里，而没人会去翻 git log。
// 决定了却不留痕 = 下次同样的坑重新踩一遍，或者更糟：别人不知道这里为什么长这样，顺手改掉。
//
// 用法：
//   node scripts/change-log.mjs                          # 全部历史打到终端
//   node scripts/change-log.mjs --since 2026-11-21       # 只今天的
//   node scripts/change-log.mjs --out docs/13-变更台账.md # 写文件（顶部自动带版本总览）
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const args = process.argv.slice(2);
const val = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const since = val('--since', null);
const out = val('--out', null);
const max = Number(val('--max', '0')) || 0;

const git = (a) => execFileSync('git', a, { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
let root = '';
try { root = git(['rev-parse', '--show-toplevel']).trim(); } catch {
  console.error('不在 git 仓库里 —— 台账要靠 git 历史，先 git init');
  process.exit(1);
}
process.chdir(root);

const fmt = '%h%x1f%ad%x1f%s%x1f%b%x1e';
const range = ['log', `--pretty=format:${fmt}`, '--date=short'];
if (since) range.push(`--since=${since}`);
if (max) range.push(`-n${max}`);

let raw = '';
try { raw = git(range); } catch (e) { console.error('读 git 历史失败：' + e.message); process.exit(1); }

const commits = raw.split('\x1e').map(s => s.trim()).filter(Boolean).map(block => {
  const [h, d, s, b] = block.split('\x1f');
  return { h, d, s, b: (b || '').trim() };
});

for (const c of commits) {
  try {
    const ns = git(['show', '--numstat', '--format=', c.h]).trim().split(/\r?\n/).filter(Boolean);
    let add = 0, del = 0;
    const files = [];
    for (const l of ns) {
      const [a, dd, f] = l.split('\t');
      if (a !== '-' && a !== undefined) { add += Number(a) || 0; del += Number(dd) || 0; }
      if (f) files.push(f);
    }
    c.add = add; c.del = del; c.files = files;
  } catch { c.add = 0; c.del = 0; c.files = []; }
}

// 版本总览：从 CHANGELOG 取每个版本的第一段当摘要 —— 台账顶部先给「为什么有这些改动」
function changelogSummary() {
  const f = path.join(root, 'build', 'v4-staging', 'CHANGELOG.md');
  if (!fs.existsSync(f)) return [];
  const t = fs.readFileSync(f, 'utf8');
  const out = [];
  const re = /^## (\d+\.\d+\.\d+) \(([^)]+)\)\s*\n+([\s\S]*?)(?=^## |\s*$)/gm;
  let m;
  while ((m = re.exec(t)) && out.length < 12) {
    // 取第一个「不是小标题」的段落：很多版本的正文直接从 `### Fixed` 开始，
    // 直接拿第一段会得到一串没有信息量的 `### Added`
    const body = m[3].split(/\n\s*\n/).map(s => s.trim()).filter(Boolean).find(s => s && !s.startsWith('#')) || '';
    out.push({ v: m[1], d: m[2], line: body.replace(/\s+/g, ' ').slice(0, 160) });
  }
  return out;
}

const byDate = new Map();
for (const c of commits) { if (!byDate.has(c.d)) byDate.set(c.d, []); byDate.get(c.d).push(c); }

const L = [];
L.push('# 变更台账');
L.push('');
L.push('> 自动生成，勿手改。来源 `git log`，重跑：`node harness/scripts/change-log.mjs --out docs/13-变更台账.md`');
L.push('> **CHANGELOG 是给用户看的发布说明，台账是给自己回看的改动流水**：每条记「改了什么文件、为什么这么改」。');
L.push('> commit message 里的取舍如果没人翻，就等于没写 —— 这份文件就是那个「翻」的地方。');
L.push('');
// 用本地时间：toISOString 是 UTC，东八区会写早 8 小时的「生成于」，看起来像台账没更新
const _now = new Date();
const _p = n => String(n).padStart(2, '0');
const _localNow = `${_now.getFullYear()}-${_p(_now.getMonth() + 1)}-${_p(_now.getDate())} ${_p(_now.getHours())}:${_p(_now.getMinutes())}`;
L.push(`共 ${commits.length} 次提交 · 生成于 ${_localNow}`);
L.push('');

const sum = changelogSummary();
if (sum.length) {
  L.push('## 版本总览（为什么有这些改动）');
  L.push('');
  L.push('| 版本 | 日期 | 这一版在解决什么 |');
  L.push('|---|---|---|');
  for (const s of sum) L.push(`| ${s.v} | ${s.d} | ${s.line} |`);
  L.push('');
}

for (const [d, list] of byDate) {
  const add = list.reduce((n, c) => n + c.add, 0);
  const del = list.reduce((n, c) => n + c.del, 0);
  L.push(`## ${d}（${list.length} 次提交 · +${add} −${del}）`);
  L.push('');
  for (const c of list) {
    L.push(`### \`${c.h}\` ${c.s}`);
    L.push('');
    L.push(`- 规模：${c.files.length} 文件 · +${c.add} −${c.del}`);
    // 正文第一段就是「为什么」—— 写 commit 时约定第一段交代原因，不要只写做了什么
    const why = (c.b.split(/\n\s*\n/)[0] || '').replace(/\n/g, ' ').trim();
    if (why) L.push(`- 为什么：${why}`);
    if (c.files.length) {
      const top = c.files.slice(0, 6).join('、');
      L.push(`- 文件：${top}${c.files.length > 6 ? ` 等 ${c.files.length} 个` : ''}`);
    }
    L.push('');
  }
}

const text = L.join('\n') + '\n';
if (out) {
  const p = path.resolve(root, out);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, text, 'utf8');
  console.log(`台账已写入 ${out}（${commits.length} 次提交 · ${byDate.size} 天）`);
} else {
  console.log(text);
}
