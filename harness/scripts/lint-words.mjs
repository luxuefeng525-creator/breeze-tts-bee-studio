#!/usr/bin/env node
// 查黑话：把没必要让人和模型解码的词揪出来，换成白话。
// 只查自然语言段落；代码块、术语对照行、带 lint-ignore 的行跳过。
//
// 豁免有两种，都必须写清理由，不许整段闭眼跳过：
//   文件级：文件任意位置写 <!-- lint-terms-exempt: 需求包,阶段门禁 -->（用于生成物/索引）
//   文件级：frontmatter 里写 lint-terms-exempt: 需求包（用于 description 触发词）
//
// 用法：node harness/scripts/lint-words.mjs [文件...] [--json]
import fs from 'node:fs';
import path from 'node:path';

const ROOT = process.cwd();
const args = process.argv.slice(2);
const asJson = args.includes('--json');
const targets = args.filter(a => !a.startsWith('--'));

// 黑话 → 白话。只收「没有技术必要、纯粹让人多解码一步」的词。
// 命令名、目录名、门禁编号属于稳定标识符，不在此列。
const BANNED = [
  ['需求包', '任务单'],
  ['机读行', '确认记录'],
  ['收口', '结项'],
  ['覆盖维度', '有几种情况'],
  ['阻断项', '红灯'],
  ['域文件', '行业基线'],
  ['域规则', '行业基线'],
  ['功能设计卡', '设计前六问'],
  ['准入门禁', '开工确认'],
];

const DEFAULT_TARGETS = [
  'AGENTS.md',
  'CLAUDE.md',
  path.join('harness', 'SKILL.md'),
  path.join('harness', 'references', 'manual.md'),
  path.join('harness', 'references', 'gates.md'),
  path.join('harness', 'skills-index.md')
];

/** 收集本文件的豁免词：文件级注释 + frontmatter 字段，两处都认 */
function exemptTerms(text) {
  const set = new Set();
  const add = raw => (raw || '').split(/[，,\s]+/).filter(Boolean).forEach(w => set.add(w.trim()));
  const comment = text.match(/<!--\s*lint-terms-exempt:\s*([^>]+?)-->/);
  if (comment) add(comment[1]);
  const fm = text.match(/lint-terms-exempt:\s*(.+)/);
  if (fm) add(fm[1]);
  return set;
}

function scan(text) {
  const hits = [];
  const lines = text.split(/\r?\n/);
  const exempt = exemptTerms(text);
  let inFence = false;
  lines.forEach((line, i) => {
    if (/^\s*```/.test(line)) { inFence = !inFence; return; }
    if (inFence) return;
    if (/lint-ignore/.test(line)) return;
    // 术语对照表本身（一行里同时出现黑话和它的白话）放过
    for (const [bad, good] of BANNED) {
      if (exempt.has(bad)) continue;
      if (!line.includes(bad)) continue;
      if (line.includes(good)) continue;
      hits.push({ line: i + 1, word: bad, suggest: good, text: line.trim().slice(0, 80) });
    }
  });
  return hits;
}

const list = targets.length ? targets : DEFAULT_TARGETS;
const report = [];
for (const rel of list) {
  const abs = path.isAbsolute(rel) ? rel : path.join(ROOT, rel);
  if (!fs.existsSync(abs)) { report.push({ rel, exists: false, hits: [] }); continue; }
  report.push({ rel, exists: true, hits: scan(fs.readFileSync(abs, 'utf8')) });
}

const total = report.reduce((s, r) => s + r.hits.length, 0);

if (asJson) {
  console.log(JSON.stringify({ total, report }, null, 2));
} else {
  for (const r of report) {
    if (!r.exists) { console.log(`  --   ${r.rel}（不存在，跳过）`); continue; }
    if (!r.hits.length) { console.log(`  OK   ${r.rel}`); continue; }
    console.log(`  FAIL ${r.rel} — ${r.hits.length} 处`);
    for (const h of r.hits) console.log(`       第 ${h.line} 行  把「${h.word}」改成「${h.suggest}」  ${h.text}`);
  }
  console.log(`\n结论：${total === 0 ? 'PASS（黑话 0 处）' : `FAIL（黑话 ${total} 处）`}`);
}

process.exit(total === 0 ? 0 : 2);
