#!/usr/bin/env node
// 失败原因闭环（OI-13 后半）。docs/06 §9 要求「每次 FAIL 必须归类到失败原因字典」。
//
// 实证后加了一类「流程未完成」：
//   实测 630 条 FAIL，最大来源是「人确认字段」（176 条，28%）。它不属于
//   需求歧义 / 实现缺陷 / 测试缺失 / 审查遗漏 / 环境问题 中的任何一个 ——
//   它是「门禁跑了，但人还没签」。硬塞进五类会让 Top3 变成假结论。
//
// 另一个关键口径：按 (需求, 阶段) 收敛，只算**首次** FAIL。
//   同一个 G3 反复重跑会重复计数，那样「失败最多的是人确认字段」其实是
//   「大家都习惯先空跑一遍看看缺什么」，不是真缺陷。
//
// 用法：
//   node harness/scripts/failure-reason.mjs              # 全量
//   node harness/scripts/failure-reason.mjs --since 2026-11-21
//   node harness/scripts/failure-reason.mjs --json
//   node harness/scripts/failure-reason.mjs --selftest
import fs from 'node:fs';
import path from 'node:path';

const ROOT = process.cwd();
const argv = process.argv.slice(2);
const flag = n => argv.includes(n);
const opt = (n, d) => { const i = argv.indexOf(n); return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : d; };
const LOG = path.join(ROOT, 'harness', 'logs', 'gate.log');

// 顺序有意义：越具体的排前面，否则「审查阻断为 0」会被「阻断」类先吃掉
const MAP = [
  [/人确认|验收人信息|发布步骤|回退材料|交付说明/, '流程未完成', '先把该填的填完再请人签，别反复空跑门禁'],
  [/可机检项|审查阻断|审查项已分类|审查/, '审查遗漏', '审查记录要标「性质」并把可机检项落成 selfcheck 检查'],
  [/验收点有结论|测试场景|覆盖值|证据/, '测试缺失', '每个 AC 先写结论与证据路径，再跑门禁'],
  [/功能设计卡|覆盖维度|需求句式|澄清|不做项|同行对标|影响面/, '需求歧义', '回需求阶段补六问与覆盖维度，别在实现里猜'],
  [/脚本缺失|未找到|超时|通道|环境|doctor/, '环境问题', '先 doctor 修环境，环境不对时门禁结果不可信'],
];
function classifyCheck(name) {
  for (const [re, cat, fix] of MAP) if (re.test(name)) return { cat, fix };
  return null;
}

function load() {
  if (!fs.existsSync(LOG)) return [];
  return fs.readFileSync(LOG, 'utf8').split(/\r?\n/).filter(Boolean).map(l => {
    try { return JSON.parse(l); } catch { return null; }
  }).filter(Boolean);
}

function report() {
  const since = opt('--since', null);
  const records = load().filter(r => !since || String(r.at || '') >= since);
  // 按 (需求, 阶段) 收敛：只看首次 FAIL，避免重跑把同一问题数很多遍
  const first = new Map();
  for (const r of records) {
    const key = `${r.req}|${r.stage}`;
    if (!first.has(key) && r.result === 'FAIL') first.set(key, r);
  }
  const fails = [...first.values()].filter(r => r.result === 'FAIL');
  if (!fails.length) {
    if (flag('--json')) console.log(JSON.stringify({ ok: true, firstFails: 0, records: records.length }, null, 2));
    else console.log('FAILURE 没有首次失败记录（门禁都一次过，或还没跑过）');
    process.exit(0);
  }

  const buckets = new Map();       // cat -> { n, checks:Set }
  const unmapped = new Map();      // check 名 -> n
  for (const r of fails) {
    for (const c of r.checks || []) {
      if (c.ok !== false || c.level === 'info') continue;
      const hit = classifyCheck(c.name);
      if (!hit) { unmapped.set(c.name, (unmapped.get(c.name) || 0) + 1); continue; }
      if (!buckets.has(hit.cat)) buckets.set(hit.cat, { n: 0, fix: hit.fix, checks: new Set() });
      const b = buckets.get(hit.cat);
      b.n++; b.checks.add(c.name);
    }
  }
  const total = [...buckets.values()].reduce((n, b) => n + b.n, 0);
  const sorted = [...buckets.entries()].sort((a, b) => b[1].n - a[1].n);

  if (flag('--json')) {
    console.log(JSON.stringify({
      ok: true,
      records: records.length,
      firstFails: fails.length,
      buckets: sorted.map(([cat, b]) => ({ cat, count: b.n, checks: [...b.checks], fix: b.fix })),
      unmapped: [...unmapped.entries()].map(([name, n]) => ({ name, n }))
    }, null, 2));
    process.exit(0);
  }

  console.log(`FAILURE 首次失败 ${fails.length} 次（日志 ${records.length} 条，按需求×阶段收敛）`);
  for (const [cat, b] of sorted) {
    const p = total ? Math.round((b.n / total) * 100) : 0;
    console.log(`  ${cat}  ${b.n} 项（${p}%）`);
    console.log(`      涉及：${[...b.checks].slice(0, 3).join('、')}${b.checks.size > 3 ? ` 等 ${b.checks.size} 项` : ''}`);
  }
  if (unmapped.size) {
    console.log(`  未分类  ${[...unmapped.values()].reduce((a, b) => a + b, 0)} 项 ← 映射表没覆盖，需要补规则：`);
    for (const [name, n] of [...unmapped.entries()].slice(0, 5)) console.log(`      ${n}× ${name}`);
  }
  if (sorted.length) {
    console.log(`\nTop1 ${sorted[0][0]} → ${sorted[0][1].fix}`);
    // 流程类占大头时，说明问题不在代码质量，在流程习惯 —— 这跟「实现差」是两个完全不同的结论
    if (sorted[0][0] === '流程未完成' && sorted[0][1].n / total >= 0.4) {
      console.log('注意：流程类占比 ≥40% —— 门禁失败主因是「该填的没填」，不是代码写错。别拿它当质量指标。');
    }
  }
  process.exit(0);
}

function selftest() {
  const cases = [
    ['G3 人确认字段', '流程未完成'],
    ['审查阻断为 0', '审查遗漏'],
    ['可机检项已落成检查', '审查遗漏'],
    ['验收点有结论与证据', '测试缺失'],
    ['测试场景已声明', '测试缺失'],
    ['覆盖值逐项有证据', '测试缺失'],
    ['功能设计卡', '需求歧义'],
    ['覆盖维度与矩阵一致', '需求歧义'],
    ['脚本缺失', '环境问题'],
  ];
  const bad = [];
  for (const [name, want] of cases) {
    const got = classifyCheck(name);
    if (!got || got.cat !== want) bad.push(`${name} 应为 ${want}，实得 ${got ? got.cat : '未分类'}`);
  }
  if (classifyCheck('某个没人见过的新检查项')) bad.push('未知检查项应返回 null（未分类），不应硬归一类');
  if (bad.length) { console.log('FAILURE SELFTEST FAIL:\n  ' + bad.join('\n  ')); process.exit(1); }
  console.log(`FAILURE SELFTEST OK ${cases.length + 1} 条`);
  process.exit(0);
}

if (flag('--selftest')) selftest();
else report();
