#!/usr/bin/env node
// 长眼：把「看到的东西」留下来，下次不用重看。
// 一行一条，带证据锚；没有证据锚的观察拒收（防止「看着正常」这种空话变成依据）。
//
// 用法：
//   node harness/scripts/observe.mjs record --saw "看到什么" --evidence "路径/退出码/URL" --conclusion "结论"
//   node harness/scripts/observe.mjs last [--n 3]
//   node harness/scripts/observe.mjs find "<关键词>"
//   node harness/scripts/observe.mjs digest [--apply]   把存量的同因观察合并成一行带计数
//   node harness/scripts/observe.mjs --selftest         临时目录验证写入合并与存量合并
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync as requireSpawn } from 'node:child_process';

const args = process.argv.slice(2);
const optVal = n => { const i = args.indexOf(n); return i >= 0 && args[i + 1] !== undefined ? args[i + 1] : null; };
const ROOT = optVal('--root') || process.cwd();
const FILE = path.join(ROOT, 'harness', 'memory', 'observe.md');
const MAX_LINES = 400;

const cmd = args[0];

function now() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function lines() {
  try { return fs.readFileSync(FILE, 'utf8').split(/\r?\n/).filter(l => l.trim()); } catch { return []; }
}
/** 同因键：同样的「看到什么 + 结论」视为同一类观察；证据细节不同也合并（保留首次证据原文） */
const SUFFIX = /〔同因 ×(\d+)，最近 ([^〕]+)〕\s*$/;
function causeKey(saw, conclusion) {
  return `${saw}||${String(conclusion).replace(/[\s。！!.，,]/g, '')}`;
}
function keyOfLine(l) {
  const base = l.replace(SUFFIX, '').trim();
  const c = base.split(' | ');
  return c.length >= 4 ? causeKey(c[1] || '', (c[3] || '').replace(/^结论：/, '')) : null;
}
function mergeCount(base, when) {
  const m = SUFFIX.exec(base);
  const n = m ? Number(m[1]) + 1 : 2;
  return `${base.replace(SUFFIX, '').trim()} 〔同因 ×${n}，最近 ${when}〕`;
}

if (cmd === 'record') {
  const saw = (optVal('--saw') || '').trim();
  const evidence = (optVal('--evidence') || '').trim();
  const conclusion = (optVal('--conclusion') || '').trim();
  const missing = [];
  if (!saw) missing.push('--saw');
  if (!evidence) missing.push('--evidence（路径 / 退出码 / URL，必须能复核）');
  if (!conclusion) missing.push('--conclusion');
  if (missing.length) {
    console.error(`拒收：缺 ${missing.join('、')}`);
    console.error('一条观察必须能被别人复核；只写「看着正常」不算证据。');
    process.exit(2);
  }
  // 证据要长得像能复核的东西：路径、行号、URL、退出码、命令或产物文件
  const EVIDENCE_SHAPE = /(\/|\\|:\d+|^https?:\/\/|exit\s*[=:]|\bPASS\b|\bFAIL\b|\.(md|json|txt|png|jpe?g|log|csv|html|yml|yaml)\b|\bnode\b|\bnpm\b|\bgit\b)/i;
  if (evidence.length < 4 || !EVIDENCE_SHAPE.test(evidence)) {
    console.error(`拒收：「${evidence}」不像能复核的证据。给路径、行号、URL、退出码或命令输出文件。`);
    process.exit(2);
  }
  const bare = conclusion.replace(/[\s。！!.，,]/g, '');
  if (bare.length < 4 || /^(正常|没问题|没问题了|ok|OK|可以|好的|还行|fine|looksgood)$/i.test(bare)) {
    console.error(`拒收：「${conclusion}」不是结论。写清看到了什么现象、意味着什么、下一步做什么。`);
    process.exit(2);
  }
  const when = now();
  const row = `${when} | ${saw} | 证据：${evidence} | 结论：${conclusion}`;
  fs.mkdirSync(path.dirname(FILE), { recursive: true });
  const all = lines();
  const k = causeKey(saw, conclusion);
  const idx = all.findIndex(l => keyOfLine(l) === k);
  if (idx >= 0) {
    // 同因合并：保留首次原文与证据，只更新计数与最近时间 —— 不抹掉发生过的事
    all[idx] = mergeCount(all[idx], when);
    fs.writeFileSync(FILE, all.slice(-MAX_LINES).join('\n') + '\n', 'utf8');
    console.log(`OK 同因已合并（原记录保留，计数 +1）\n  ${all[idx]}`);
    process.exit(0);
  }
  fs.writeFileSync(FILE, [...all, row].slice(-MAX_LINES).join('\n') + '\n', 'utf8');
  console.log(`OK 已记下\n  ${row}`);

} else if (cmd === 'digest') {
  const all = lines();
  const groups = new Map();
  all.forEach(l => {
    const k = keyOfLine(l);
    if (!k) return;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(l);
  });
  const plan = new Map();
  for (const [k, bucket] of groups) {
    if (bucket.length < 2) continue;
    const last = bucket[bucket.length - 1];
    const lastTime = (last.match(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/) || [''])[0];
    const total = bucket.reduce((n, l) => {
      const m = SUFFIX.exec(l);
      return n + (m ? Number(m[1]) : 1);
    }, 0);
    const merged = `${bucket[0].replace(SUFFIX, '').trim()} 〔同因 ×${total}，最近 ${lastTime}〕`;
    plan.set(k, { from: bucket.length, to: merged });
  }
  if (!plan.size) { console.log('DIGEST 无同因重复'); process.exit(0); }
  const list = [...plan.values()];
  const saved = list.reduce((n, p) => n + p.from - 1, 0);
  if (!args.includes('--apply')) {
    console.log(`DRY-RUN ${plan.size} 组同因，可省 ${saved} 行；加 --apply 落盘（只合并，不删首次证据）`);
    list.slice(0, 5).forEach(p => console.log(`  ${p.from} → 1：${p.to.slice(0, 90)}`));
    process.exit(0);
  }
  const seen = new Set();
  const next = [];
  for (const l of all) {
    const k = keyOfLine(l);
    if (k && seen.has(k)) continue;
    if (k) seen.add(k);
    next.push(k && plan.has(k) ? plan.get(k).to : l);
  }
  fs.writeFileSync(FILE, next.join('\n') + '\n', 'utf8');
  console.log(`DIGEST 已合并 ${plan.size} 组，${all.length} → ${next.length} 行（省 ${saved} 行）`);

} else if (args.includes('--selftest')) {
  const os = await import('node:os');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'obs-selftest-'));
  // 用临时 root 走真实命令链，避免碰工程里的观察记录
  const self = process.argv[1];
  const run = (...a) => {
    const r = requireSpawn(process.execPath, [self, ...a, '--root', tmp], { encoding: 'utf8' });
    return r;
  };
  const bad = [];
  const ev = 'harness/scripts/observe.mjs:60';
  run('record', '--saw', '自测同因', '--evidence', ev, '--conclusion', '自测结论需保留');
  run('record', '--saw', '自测同因', '--evidence', ev, '--conclusion', '自测结论需保留');
  let out = fs.readFileSync(path.join(tmp, 'harness', 'memory', 'observe.md'), 'utf8');
  if (out.split('\n').filter(Boolean).length !== 1 || !/同因 ×2/.test(out)) bad.push('同因写入未合并成一行 ×2');
  run('record', '--saw', '自测同因', '--evidence', ev, '--conclusion', '自测结论需保留');
  out = fs.readFileSync(path.join(tmp, 'harness', 'memory', 'observe.md'), 'utf8');
  if (!/同因 ×3/.test(out)) bad.push('第三次未累计到 ×3');
  const d = run('digest', '--apply');
  if (!/无同因重复/.test(d.stdout || '')) bad.push('digest 后仍识别到重复：' + (d.stdout || ''));
  // 反向验证：手工造 3 行同因（无计数后缀），digest 必须算出 ×3 而不是 ×2
  const manual = path.join(tmp, 'harness', 'memory', 'observe.md');
  fs.writeFileSync(manual, [
    `${ev} | 存量同因 | 证据：${ev} | 结论：存量要正确计数`,
    `${ev} | 存量同因 | 证据：${ev} | 结论：存量要正确计数`,
    `${ev} | 存量同因 | 证据：${ev} | 结论：存量要正确计数`
  ].join('\n') + '\n', 'utf8');
  const d2 = run('digest', '--apply');
  const digested = fs.readFileSync(manual, 'utf8');
  if (!/同因 ×3/.test(digested)) bad.push('存量三行同因未计成 ×3：' + digested.slice(0, 120));
  if (!/已合并 1 组/.test(d2.stdout || '')) bad.push('digest 未报告合并组数：' + (d2.stdout || ''));
  fs.rmSync(tmp, { recursive: true, force: true });
  if (bad.length) { console.error('OBS SELFTEST FAIL: ' + bad.join('；')); process.exit(1); }
  console.log('OBS SELFTEST OK 6 条（同因合并 / 计数累计 / digest 幂等 / 存量×3 / 组数报告 / 无残留）');
  process.exit(0);

} else if (cmd === 'last') {
  const n = Number(optVal('--n') || 3);
  const all = lines().slice(-n).reverse();
  if (!all.length) { console.log('NONE（还没有观察记录）'); process.exit(0); }
  all.forEach((l, i) => console.log(`${i + 1}. ${l}`));

} else if (cmd === 'find') {
  const q = (args[1] || '').trim();
  if (!q) { console.error('用法：observe.mjs find "<关键词>"'); process.exit(1); }
  const hit = lines().filter(l => l.includes(q));
  if (!hit.length) { console.log('NONE'); process.exit(0); }
  hit.forEach((l, i) => console.log(`${i + 1}. ${l}`));

} else {
  console.log('observe.mjs: record | last | find');
  console.log('  record --saw "看到什么" --evidence "路径或退出码" --conclusion "这意味着什么"');
}
