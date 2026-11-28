#!/usr/bin/env node
// 量 token：把「每轮都付的」和「按需才付的」分开算，不混在一起吹数字。
// 用法：node harness/scripts/measure-tokens.mjs [--limit 600] [--json]
import fs from 'node:fs';
import path from 'node:path';

const args = process.argv.slice(2);
const asJson = args.includes('--json');
const limitIdx = args.indexOf('--limit');
// 600 → 700：入口必须写清「技能怎么定 / 经验从哪取」，这几十 token 是买「换会话不失忆」；
// 抬预算的同时由 selfcheck 的「常驻入口指向技能与记忆」守住它真的写了，不是白占额度。
const HARD_LIMIT = 700;                                   // 已定的预算，不因参数放宽
const rawLimit = limitIdx >= 0 ? Number(args[limitIdx + 1]) : HARD_LIMIT;
const RESIDENT_LIMIT = Number.isFinite(rawLimit) ? Math.min(HARD_LIMIT, rawLimit) : HARD_LIMIT;
if (limitIdx >= 0 && rawLimit > HARD_LIMIT) {
  console.error(`提示：预算上限已定为 ${HARD_LIMIT} token，--limit ${rawLimit} 不会放宽；要改预算请改代码并说明理由。`);
}

const ROOT = process.cwd();

// 统一估算式：中文按 0.75，其余按 1/3.8。前后对比必须用同一式。
function estimate(text) {
  const cn = (text.match(/[\u4e00-\u9fa5]/g) || []).length;
  const other = text.length - cn;
  return Math.round(cn * 0.75 + other / 3.8);
}

function readIfExists(rel) {
  const abs = path.isAbsolute(rel) ? rel : path.join(ROOT, rel);
  if (!fs.existsSync(abs)) return null;
  return { rel, text: fs.readFileSync(abs, 'utf8'), tok: estimate(fs.readFileSync(abs, 'utf8')) };
}

const groups = {
  // 每轮都注入的：同一个宿主只会用其中一份，取最大值，不相加
  resident: [
    'AGENTS.md',
    'CLAUDE.md',
    path.join('.trae', 'rules', 'harness.md')
  ],
  // 进了流程才读的
  workflow: [
    path.join('harness', 'SKILL.md'),
    path.join('harness', 'skills-index.md')
  ],
  // 按需手册
  manual: [
    path.join('harness', 'references', 'manual.md'),
    path.join('harness', 'references', 'gates.md')
  ]
};

const rows = [];
for (const [group, list] of Object.entries(groups)) {
  for (const rel of list) {
    const f = readIfExists(rel);
    rows.push({ group, rel, exists: !!f, tok: f ? f.tok : 0, chars: f ? f.text.length : 0 });
  }
}

const residentRows = rows.filter(r => r.group === 'resident' && r.exists);
const worst = residentRows.reduce((a, b) => (b.tok > a.tok ? b : a), { rel: '—', tok: 0 });
const residentSum = residentRows.reduce((s, r) => s + r.tok, 0);
const workflowTok = rows.filter(r => r.group === 'workflow' && r.exists).reduce((s, r) => s + r.tok, 0);

const result = {
  limit: RESIDENT_LIMIT,
  residentWorst: worst.tok,
  residentFile: worst.rel,
  residentSum,
  residentPass: worst.tok <= RESIDENT_LIMIT,
  workflowTok,
  rows: rows.map(({ group, rel, exists, tok, chars }) => ({ group, rel, exists, tok, chars }))
};

if (asJson) {
  console.log(JSON.stringify(result, null, 2));
} else {
  const label = { resident: '每轮常驻', workflow: '进流程才读', manual: '按需手册' };
  let cur = '';
  for (const r of rows) {
    if (r.group !== cur) { cur = r.group; console.log(`\n[${label[cur]}]`); }
    if (!r.exists) { console.log(`  --      ${r.rel}（不存在）`); continue; }
    console.log(`  ${String(r.tok).padStart(5)} tok  ${String(r.chars).padStart(6)} 字符  ${r.rel}`);
  }
  console.log('\n────────────────────────────');
  console.log(`每轮常驻（一个宿主只用一份，取最大）：${worst.tok} tok（${worst.rel}）  上限 ${RESIDENT_LIMIT}`);
  console.log(`（参考）若多个宿主同时注入，最坏合计 ${residentSum} tok —— 正常不会发生`);
  console.log(`结论：${result.residentPass ? 'PASS' : 'FAIL'}　进流程额外：${workflowTok} tok`);
}

process.exit(result.residentPass ? 0 : 2);
