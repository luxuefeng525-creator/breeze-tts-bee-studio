#!/usr/bin/env node
// 度量采集（OI-10）。M1–M8 的定义见 docs/05 §16，这里只负责「算出来并留痕」。
//
// 阈值决策（OI-02，2026-11-21 已定，不再摇摆）：
//   正确性类 M3 验收点覆盖、M4 证据完整 —— 必须 100%。
//     不达标说明门禁在造假：「没有证据的 PASS」等于没验证，不是「差不多」。
//   效率类 M1/M2/M5/M6/M8 —— 先采 14 天基线再定阈值。
//     docs/05 §16 原话「目标值 TBD，不得臆造」，所以这里只记录不判红。
//
// 采集方式：事件驱动，每次门禁跑完追加一行；不做定时任务（没人会看定时报告）。
//
// 用法：
//   node harness/scripts/metrics.mjs record <REQ> --stage G3 --result PASS
//   node harness/scripts/metrics.mjs report [--last 20]
//   node harness/scripts/metrics.mjs --selftest
import fs from 'node:fs';
import path from 'node:path';

const ROOT = process.cwd();
const argv = process.argv.slice(2);
const cmd = argv[0];
const flag = n => argv.includes(n);
const opt = (n, d) => { const i = argv.indexOf(n); return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : d; };

const FILE = path.join(ROOT, 'harness', 'metrics.md');
const HEADER = [
  '# 度量记录',
  '',
  '> 自动追加，来源：每次门禁跑完。M1–M8 定义见 `docs/05` §16。',
  '> 阈值口径（OI-02，2026-11-21 定）：**M3 / M4 必须 100%**（正确性，不达标即门禁造假）；',
  '> M1 / M2 / M5 / M6 / M8 先采 14 天基线再定阈值（不臆造）。',
  '',
  '| 时间 | 需求 | 阶段 | 结论 | M1 一次通过 | M2 返工 | M3 覆盖率 | M4 证据完整率 | M8 记忆命中 |',
  '|---|---|---|---|---|---|---|---|---|',
  ''
].join('\n');

const pct = (a, b) => (b ? Math.round((a / b) * 100) + '%' : 'n/a');
/** 单元格形如 `0% (0/26)` 或 `100% (4/4)`：只取最前面的百分比。
 *  直接去掉非数字会把 26、4 也吃进来 —— 第一版就这么错的（显示 26% 而实际是 0%）。 */
function cellPct(s) {
  const m = String(s || '').match(/^(\d+(?:\.\d+)?)%/);
  return m ? Number(m[1]) : null;
}
function localStamp() {
  const d = new Date(), p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 读 02-test.md 的验收点表：结论在第 6 列，证据在第 7 列 */
function acRows(reqId) {
  const f = path.join(ROOT, 'harness', 'requirements', reqId, '02-test.md');
  if (!fs.existsSync(f)) return [];
  const out = [];
  for (const l of fs.readFileSync(f, 'utf8').split(/\r?\n/)) {
    if (!/^\|\s*AC-/.test(l)) continue;
    const c = l.split('|').map(s => s.trim().replace(/[*`]/g, ''));
    out.push({ id: c[1], result: (c[6] || '').toUpperCase(), evid: c[7] || '' });
  }
  return out;
}

/** 证据列可能有多个路径（顿号/逗号分隔），取第一个存在的就算有证据 */
function hasEvidence(evid) {
  if (!evid || evid === '—' || evid === '-') return false;
  return evid.split(/[、,，]/).map(s => s.trim()).filter(Boolean)
    .some(p => { try { return fs.existsSync(path.join(ROOT, p)); } catch { return false; } });
}

function memHit() {
  const f = path.join(ROOT, 'harness', 'memory', 'long-term.md');
  if (!fs.existsSync(f)) return null;
  const rows = fs.readFileSync(f, 'utf8').split(/\r?\n/).filter(l => /^\d{4}-\d{2}-\d{2}\s*\|/.test(l));
  if (!rows.length) return null;
  const hit = rows.filter(l => Number((l.split('|').pop() || '').trim().replace(/\[stale\]/, '')) > 0).length;
  return { hit, total: rows.length };
}

function compute(reqId, stage, result) {
  const rows = acRows(reqId);
  const solved = rows.filter(r => /^(PASS|FAIL|BLOCKED)$/.test(r.result));
  const withEv = rows.filter(r => r.result !== '' && hasEvidence(r.evid));
  const fail = rows.filter(r => r.result === 'FAIL').length;
  return {
    rows: rows.length,
    solved: solved.length,
    withEv: withEv.length,
    fail,
    m3: pct(solved.length, rows.length),
    m4: pct(withEv.length, solved.length),
    m1: result === 'PASS' ? 1 : 0,
    m2: fail,
    mem: memHit()
  };
}

function record() {
  const reqId = argv[1];
  const stage = opt('--stage', 'G3');
  const result = String(opt('--result', '')).toUpperCase() || 'UNKNOWN';
  if (!reqId) { console.error('用法：metrics.mjs record <REQ> --stage G3 --result PASS'); process.exit(1); }
  if (!fs.existsSync(path.join(ROOT, 'harness', 'requirements', reqId))) {
    console.error(`找不到需求包 ${reqId} —— 指标要从 02-test.md 算，路径不对就是假数`);
    process.exit(1);
  }
  const c = compute(reqId, stage, result);
  const mem = c.mem ? `${pct(c.mem.hit, c.mem.total)} (${c.mem.hit}/${c.mem.total})` : 'n/a';
  const line = `| ${localStamp()} | ${reqId} | ${stage} | ${result} | ${c.m1} | ${c.m2} | ${c.m3} (${c.solved}/${c.rows}) | ${c.m4} (${c.withEv}/${c.solved}) | ${mem} |`;
  if (!fs.existsSync(FILE)) fs.writeFileSync(FILE, HEADER, 'utf8');
  fs.appendFileSync(FILE, line + '\n', 'utf8');
  console.log(`METRICS 已记录 ${reqId} ${stage} ${result} · M3=${c.m3} M4=${c.m4} M2=${c.m2}`);
  process.exit(0);
}

function report() {
  if (!fs.existsSync(FILE)) { console.log('METRICS 暂无记录 —— 跑过门禁后自动生成'); process.exit(0); }
  const lines = fs.readFileSync(FILE, 'utf8').split(/\r?\n/).filter(l => /^\| \d{4}-\d{2}-\d{2}/.test(l));
  const last = Number(opt('--last', '0')) || 0;
  const use = last ? lines.slice(-last) : lines;
  if (!use.length) { console.log('METRICS 暂无记录'); process.exit(0); }
  const num = s => Number(String(s).replace(/[^\d.]/g, '')) || 0;
  const pick = (l, i) => (l.split('|').map(s => s.trim())[i] || '');
  const m3ok = use.filter(l => pick(l, 7).startsWith('100%')).length;
  const m4ok = use.filter(l => pick(l, 8).startsWith('100%')).length;
  const m1 = use.reduce((n, l) => n + num(pick(l, 5)), 0);
  const m2 = use.reduce((n, l) => n + num(pick(l, 6)), 0);
  const memPct = use.map(l => cellPct(pick(l, 9))).filter(v => v !== null);
  const firstDay = pick(use[0], 1).slice(0, 10);
  const days = Math.max(1, Math.round((Date.now() - new Date(firstDay + 'T00:00:00').getTime()) / 864e5) + 1);

  console.log(`METRICS ${use.length} 次记录 · 起于 ${firstDay}（${days} 天）`);
  console.log('  正确性（阈值 100%，OI-02 已定）');
  console.log(`    M3 验收点覆盖   ${m3ok}/${use.length} 次达标${m3ok < use.length ? ' ← 有未达标' : ''}`);
  console.log(`    M4 证据完整     ${m4ok}/${use.length} 次达标${m4ok < use.length ? ' ← 有未达标' : ''}`);
  console.log('  效率（基线采集中）');
  console.log(`    M1 一次通过     ${pct(m1, use.length)}（${m1}/${use.length}）`);
  console.log(`    M2 返工总数     ${m2}（均 ${(m2 / use.length).toFixed(2)} 次/次门禁）`);
  // 口径（OI-33，2026-11-21 定）：brief/search 展示即召回，同日同一条只计一次；
  // 账本 harness/memory/recall-ledger.md 防重复，hit 命令是显式「确实用过」的加强登记。
  // 所以 0% 表示「没有任何正式记忆被召回过」，而不是「指标坏了」。
  console.log(`    M8 记忆命中     ${memPct.length ? pct(memPct.reduce((a, b) => a + b, 0), memPct.length * 100) : 'n/a'}（召回计一次/日/条，brief 展示也算）`);
  console.log('    M5 续接成本 / M6 上下文量：暂无采集口径，n/a');
  if (days < 14) console.log(`  基线不足 14 天（已 ${days} 天）—— 效率类暂不设阈值，避免拿噪声当标准`);
  process.exit(0);
}

function selftest() {
  // 只测纯函数：pct 与证据判定。真实采集要在有需求包的环境跑，不在这里造假数据。
  const bad = [];
  if (pct(0, 0) !== 'n/a') bad.push('pct(0,0) 应为 n/a');
  if (pct(4, 4) !== '100%') bad.push('pct(4,4) 应为 100%');
  if (pct(1, 3) !== '33%') bad.push('pct(1,3) 应为 33%');
  if (hasEvidence('—')) bad.push('破折号不算证据');
  if (hasEvidence('')) bad.push('空串不算证据');
  if (cellPct('0% (0/26)') !== 0) bad.push('0% (0/26) 应解析为 0');
  if (cellPct('100% (4/4)') !== 100) bad.push('100% (4/4) 应解析为 100');
  if (cellPct('n/a') !== null) bad.push('n/a 应解析为 null');
  if (bad.length) { console.log('METRICS SELFTEST FAIL: ' + bad.join('；')); process.exit(1); }
  console.log('METRICS SELFTEST OK 8 条');
  process.exit(0);
}

if (flag('--selftest')) selftest();
else if (cmd === 'record') record();
else if (cmd === 'report') report();
else { console.log('用法：metrics.mjs record <REQ> --stage G3 --result PASS | report [--last N] | --selftest'); process.exit(1); }
