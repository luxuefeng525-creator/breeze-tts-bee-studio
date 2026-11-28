#!/usr/bin/env node
// 风险自动分档（OI-13 前半）。口径来自 docs/06 §5.1：
//   files ≤1 且 无接口/数据/权限变更 → S
//   files ≤5 或 单接口变更          → M
//   否则                            → L
//   任一命中「权限 / 迁移 / 删除 / 发布」→ 至少 M，且 G3 必须人工复核
//
// 为什么按 diff 而不是按需求文本：begin 时按需求文本定的是「打算做多大」，
// 这里是「实际改了多少」。两者不一致时以大的为准 —— 计划说改一行、结果动了 8 个文件，
// 那就是 L，不能拿开工时的 S 混过去。
//
// 用法：
//   node harness/scripts/risk-tier.mjs                # 比 HEAD（工作区全部改动）
//   node harness/scripts/risk-tier.mjs --base main     # 比某个基线
//   node harness/scripts/risk-tier.mjs --json
//   node harness/scripts/risk-tier.mjs --selftest
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = process.cwd();
const argv = process.argv.slice(2);
const flag = n => argv.includes(n);
const opt = (n, d) => { const i = argv.indexOf(n); return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : d; };

const SIG = {
  api: /(^|\/)(api|routes?|controllers?|handlers?|endpoints?)(\/|\.)|openapi|swagger/i,
  data: /(^|\/)(schema|migrations?|models?|db|database|sql)(\/|\.|$)|\.sql$/i,
  perm: /(^|\/)(auth|authz|permission|permissions|roles?|acl|policy|policies|rbac)(\/|\.|$)/i,
  release: /(^|\/)(\.github\/workflows|deploy|k8s|helm|dockerfile|release)(\/|\.|$)|Dockerfile$|docker-compose/i,
  concurrency: /(^|\/)(lock|mutex|queue|worker|transaction)(\/|\.|$)/i
};

/** 纯函数：给规模与信号，出档位与理由。自测和真实运行走同一条路径。 */
function computeTier({ files, api, data, perm, release, del, concurrency }) {
  const reasons = [];
  let tier = 'S';
  const sensitive = api + data + perm;
  if (files <= 1 && sensitive === 0) {
    reasons.push('单文件且不涉接口/数据/权限');
  } else if (files <= 5 || sensitive === 1) {
    tier = 'M';
    reasons.push(files > 1 ? `${files} 个文件` : '');
    if (sensitive) reasons.push(`${sensitive} 处接口/数据/权限`);
  } else {
    tier = 'L';
    reasons.push(`${files} 个文件`);
    if (sensitive) reasons.push(`${sensitive} 处接口/数据/权限`);
  }
  let mustHumanReview = false;
  const hard = [];
  if (perm) hard.push('权限');
  if (data) hard.push('数据结构');
  if (del) hard.push('删除');
  if (release) hard.push('发布配置');
  if (hard.length) {
    if (tier === 'S') tier = 'M';
    mustHumanReview = true;
    reasons.push(`命中 ${hard.join('/')} → 至少 M 且 G3 人工复核`);
  }
  if (concurrency && tier === 'S') { tier = 'M'; reasons.push('涉及并发'); }
  return { tier, reasons: reasons.filter(Boolean), mustHumanReview };
}

function gather(base) {
  const files = new Map();
  const addNumstat = (args) => {
    let out = '';
    try { out = execFileSync('git', args, { cwd: ROOT, encoding: 'utf8' }); } catch { return; }
    for (const l of out.split(/\r?\n/)) {
      if (!l.trim()) continue;
      const [a, d, f] = l.split('\t');
      if (!f) continue;
      const prev = files.get(f) || { add: 0, del: 0, status: 'M' };
      files.set(f, {
        add: prev.add + (a === '-' ? 0 : Number(a) || 0),
        del: prev.del + (d === '-' ? 0 : Number(d) || 0),
        status: a === '-' ? 'B' : prev.status
      });
    }
  };
  addNumstat(['diff', '--numstat', base || 'HEAD']);
  addNumstat(['diff', '--numstat', '--cached', base || 'HEAD']);
  // 未跟踪文件也算改动 —— 新建文件往往正是「计划外扩出来的部分」
  try {
    const st = execFileSync('git', ['status', '--porcelain'], { cwd: ROOT, encoding: 'utf8' });
    for (const l of st.split(/\r?\n/)) {
      if (!l.trim()) continue;
      const tag = l.slice(0, 2).trim();
      const f = l.slice(3).trim();
      if (tag === '??' || tag === 'D') {
        const prev = files.get(f) || { add: 0, del: 0, status: tag === 'D' ? 'D' : 'A' };
        files.set(f, { ...prev, status: tag === 'D' ? 'D' : 'A' });
      }
    }
  } catch { }
  return files;
}

function run() {
  const base = opt('--base', null);
  const files = gather(base);
  const paths = [...files.keys()];
  const signals = { api: 0, data: 0, perm: 0, release: 0, concurrency: 0, del: 0 };
  for (const p of paths) {
    for (const k of ['api', 'data', 'perm', 'release', 'concurrency']) if (SIG[k].test(p)) signals[k]++;
    if ((files.get(p) || {}).status === 'D') signals.del++;
  }
  const r = computeTier({ files: paths.length, ...signals });

  // 与任务单声明的档位比对：计划说小、实际改大，必须点出来
  let declared = null;
  try {
    const reqRoot = path.join(ROOT, 'harness', 'requirements');
    const dirs = fs.readdirSync(reqRoot).filter(d => /^REQ-/.test(d) && fs.existsSync(path.join(reqRoot, d, 'state.md')));
    const latest = dirs.sort().pop();
    if (latest) {
      const m = fs.readFileSync(path.join(reqRoot, latest, 'state.md'), 'utf8').match(/^size:\s*(S|M|L|L0|L1|L2)\b/m);
      if (m) declared = m[1];
    }
  } catch { }
  const norm = v => (v === 'L0' ? 'S' : v === 'L1' ? 'M' : v === 'L2' ? 'L' : v);
  const mismatch = declared && norm(declared) !== r.tier;

  if (flag('--json')) {
    console.log(JSON.stringify({ ok: true, tier: r.tier, files: paths.length, signals, reasons: r.reasons, mustHumanReview: r.mustHumanReview, declared, mismatch }, null, 2));
    process.exit(0);
  }
  console.log(`RISK ${r.tier} · ${paths.length} 个文件${Object.values(signals).some(Boolean) ? ' · ' + Object.entries(signals).filter(([, v]) => v).map(([k, v]) => `${k}×${v}`).join(' ') : ''}`);
  for (const x of r.reasons) console.log(`  ${x}`);
  if (r.mustHumanReview) console.log('  → 必须人工复核（G3），不可仅凭机判放行');
  if (mismatch) console.log(`  WARN 任务单声明 ${declared}，实际 ${r.tier} —— 以大的为准，回写 size`);
  process.exit(0);
}

function selftest() {
  const cases = [
    { in: { files: 1, api: 0, data: 0, perm: 0, release: 0, del: 0, concurrency: 0 }, want: 'S' },
    { in: { files: 1, api: 0, data: 1, perm: 0, release: 0, del: 0, concurrency: 0 }, want: 'M' },
    { in: { files: 3, api: 0, data: 0, perm: 0, release: 0, del: 0, concurrency: 0 }, want: 'M' },
    { in: { files: 7, api: 0, data: 0, perm: 0, release: 0, del: 0, concurrency: 0 }, want: 'L' },
    { in: { files: 1, api: 0, data: 0, perm: 1, release: 0, del: 0, concurrency: 0 }, want: 'M', human: true },
    { in: { files: 1, api: 0, data: 0, perm: 0, release: 1, del: 0, concurrency: 0 }, want: 'M', human: true },
    { in: { files: 8, api: 2, data: 1, perm: 0, release: 0, del: 0, concurrency: 0 }, want: 'L' },
    { in: { files: 0, api: 0, data: 0, perm: 0, release: 0, del: 0, concurrency: 0 }, want: 'S' },
  ];
  const bad = [];
  for (const c of cases) {
    const r = computeTier(c.in);
    if (r.tier !== c.want) bad.push(`files=${c.in.files} api=${c.in.api} perm=${c.in.perm} rel=${c.in.release}：应 ${c.want}，实得 ${r.tier}`);
    if (c.human && !r.mustHumanReview) bad.push(`files=${c.in.files} 命中硬信号却没标人工复核`);
  }
  if (bad.length) { console.log('RISK SELFTEST FAIL:\n  ' + bad.join('\n  ')); process.exit(1); }
  console.log(`RISK SELFTEST OK ${cases.length} 条`);
  process.exit(0);
}

if (flag('--selftest')) selftest();
else run();
