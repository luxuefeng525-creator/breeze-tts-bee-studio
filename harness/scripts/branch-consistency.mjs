#!/usr/bin/env node
// 双分支一致性（OI-19）：同一个脚本的「文本输出」与「--json 输出」必须是同一份数。
//
// 为什么专治这个：曾出现过文本显示「±0」而 JSON 里 s=0 —— 人看文本以为没变化，
// 机器读 JSON 拿到另一套结论。这种 bug 不会报错，只会让两边各自「看起来正确」。
//
// 做法：对每个脚本跑两次（文本 / JSON），把关键数字抽出来逐个比对。
// 这是契约测试，不是通用扫描 —— 通用扫描靠猜格式，误报会淹没真问题。
//
// 防递归：selfcheck 里也会调本脚本，所以调用 selfcheck 时必须带 HARNESS_NO_BRANCH=1，
// 否则 selfcheck → 本脚本 → selfcheck 无限套。
//
// 用法：
//   node harness/scripts/branch-consistency.mjs            # 全量（含 selfcheck，较慢）
//   node harness/scripts/branch-consistency.mjs --quick     # 只跑快的（selfcheck 内部调用）
//   node harness/scripts/branch-consistency.mjs --selftest  # 只测提取函数
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

// ROOT 默认取 cwd，但发布闸门是在包目录里跑的 —— 必须显式传 --root，
// 否则这里找 harness/scripts/x.mjs 全部落空，4 组齐刷刷报「不一致」（实测踩过）。
const argv = process.argv.slice(2);
const flag = n => argv.includes(n);
// ROOT 默认取 cwd，但发布闸门是在包目录里跑的 —— 必须显式传 --root，
// 否则这里找 harness/scripts/x.mjs 全部落空，4 组齐刷刷报「不一致」（实测踩过）。
const rootArg = (() => { const i = argv.indexOf('--root'); return i >= 0 ? argv[i + 1] : null; })();
const ROOT = rootArg ? path.resolve(rootArg) : process.cwd();

function run(script, args = [], env = {}) {
  const f = path.join(ROOT, 'harness', 'scripts', script);
  if (!fs.existsSync(f)) return { ok: false, out: '', err: `缺脚本 ${script}` };
  const r = spawnSync(process.execPath, [f, ...args], {
    cwd: ROOT, encoding: 'utf8', timeout: 120000,
    env: { ...process.env, ...env }
  });
  return { ok: r.status === 0, out: (r.stdout || ''), err: (r.stderr || '') };
}
const num = (re, s) => { const m = String(s).match(re); return m ? Number(m[1]) : null; };

const CASES = [
  {
    id: 'route 分数', quick: true,
    check() {
      const q = '这个方案帮我拷问一下';
      const t = run('route.mjs', [q]);
      const j = run('route.mjs', [q, '--json']);
      let jr = null;
      try { jr = JSON.parse(j.out); } catch { return [`JSON 解析失败：${j.out.slice(0, 80)}`]; }
      const m = t.out.match(/^1\.\s+(\S+)\s+\(score=(\d+)\)/m);
      const jTop = (jr.route || [])[0];
      if (!m && !jTop) return [];
      if (!m && jTop) return [`文本无命中，JSON 有 ${jTop.name}`];
      if (m && !jTop) return [`文本命中 ${m[1]}，JSON 无 route`];
      const bad = [];
      if (m[1] !== jTop.name) bad.push(`技能名不同：文本 ${m[1]} / JSON ${jTop.name}`);
      if (Number(m[2]) !== Number(jTop.score)) bad.push(`分数不同：文本 ${m[2]} / JSON ${jTop.score}`);
      return bad;
    }
  },
  {
    id: 'risk-tier 档位', quick: true,
    check() {
      const t = run('risk-tier.mjs', ['--base', 'HEAD']);
      const j = run('risk-tier.mjs', ['--base', 'HEAD', '--json']);
      let jr = null;
      try { jr = JSON.parse(j.out); } catch { return [`JSON 解析失败：${j.out.slice(0, 80)}`]; }
      const tTier = (t.out.match(/^RISK\s+([SML])/m) || [])[1] || null;
      const tFiles = num(/RISK\s+[SML]\s+·\s+(\d+)\s+个文件/, t.out);
      const bad = [];
      if (tTier !== jr.tier) bad.push(`档位不同：文本 ${tTier} / JSON ${jr.tier}`);
      if (tFiles !== null && Number(tFiles) !== Number(jr.files)) bad.push(`文件数不同：文本 ${tFiles} / JSON ${jr.files}`);
      return bad;
    }
  },
  {
    id: 'failure-reason 首次失败数', quick: true,
    check() {
      const t = run('failure-reason.mjs');
      const j = run('failure-reason.mjs', ['--json']);
      let jr = null;
      try { jr = JSON.parse(j.out); } catch { return [`JSON 解析失败：${j.out.slice(0, 80)}`]; }
      const tN = num(/首次失败\s*(\d+)\s*次/, t.out);
      const bad = [];
      // 文本「没有首次失败记录」对应 JSON firstFails=0；两者都为 0 时不应报不一致
      const tt = tN === null ? 0 : tN;
      if (tt !== Number(jr.firstFails)) bad.push(`首次失败数不同：文本 ${tt} / JSON ${jr.firstFails}`);
      return bad;
    }
  },
  {
    id: 'selfcheck 阻断/提示/放行', quick: false,
    check() {
      // 必须禁掉本脚本，否则 selfcheck → 本脚本 → selfcheck 无限套
      const env = { HARNESS_NO_BRANCH: '1' };
      const t = run('selfcheck.mjs', [], env);
      const j = run('selfcheck.mjs', ['--json'], env);
      let jr = null;
      try { jr = JSON.parse(j.out); } catch { return [`JSON 解析失败：${j.out.slice(0, 80)}`]; }
      const tB = num(/阻断\s*(\d+)/, t.out), tW = num(/提示\s*(\d+)/, t.out), tP = num(/放行\s*(\d+)/, t.out);
      const s = jr.summary || {};
      const bad = [];
      if (tB !== s['阻断']) bad.push(`阻断不同：文本 ${tB} / JSON ${s['阻断']}`);
      if (tW !== s['提示']) bad.push(`提示不同：文本 ${tW} / JSON ${s['提示']}`);
      if (tP !== s['放行']) bad.push(`放行不同：文本 ${tP} / JSON ${s['放行']}`);
      return bad;
    }
  }
];

function selftest() {
  // 提取函数是这套检查的核心，写错会让检查永远为真
  const bad = [];
  if (num(/阻断\s*(\d+)/, '阻断 3 · 提示 6') !== 3) bad.push('阻断提取错');
  if (num(/阻断\s*(\d+)/, '阻断 0') !== 0) bad.push('阻断 0 提取错');
  if (num(/首次失败\s*(\d+)\s*次/, '没有首次失败记录') !== null) bad.push('无数字时应为 null');
  if (bad.length) { console.log('BRANCH SELFTEST FAIL: ' + bad.join('；')); process.exit(1); }
  console.log('BRANCH SELFTEST OK 3 条');
  process.exit(0);
}

if (flag('--selftest')) selftest();

const quick = flag('--quick');
const use = CASES.filter(c => !quick || c.quick);
const fails = [];
for (const c of use) {
  let bad = [];
  try { bad = c.check() || []; } catch (e) { bad = [`执行异常：${e && e.message ? e.message : e}`]; }
  if (bad.length) fails.push({ id: c.id, bad });
}

if (!fails.length) {
  console.log(`BRANCH OK ${use.length} 组（文本与 JSON 同口径）${quick ? ' · quick' : ''}`);
  process.exit(0);
}
console.log(`BRANCH FAIL ${fails.length}/${use.length} 组文本与 JSON 不一致：`);
for (const f of fails) {
  console.log(`  ${f.id}`);
  for (const b of f.bad) console.log(`    - ${b}`);
}
console.log('  这类 bug 不会报错：人看文本、机器读 JSON，两边各自「看起来正确」。');
process.exit(1);
