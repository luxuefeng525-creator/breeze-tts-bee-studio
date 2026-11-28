#!/usr/bin/env node
// 现场生成器：把 NEXT.md 里「机器能算出来的事实」从真相源生成出来，并生成短交接 NEXT-BRIEF.md。
//
// 为什么有它：NEXT.md 是新会话接手时读的第一份文件（AGENTS.md / CLAUDE.md /
// .trae/rules/*.md 都指向它）。但它原来纯手写 —— 实测停在 v4.4.6，
// 而实际已到 v4.4.14：版本号、技能数、任务单清单全错，而且**没有任何机制提醒它过期**。
//
// 「资产存在不等于 agent 可达」的下一层：资产内容过期 = 资产有害。
// 所以凡是机器能算的，一律别手写；手写层只留机器算不出来的判断。
//
// 照 change-log.mjs 的既有模式：脚本从真相源生成 + 闸门校验一致性。
//
// 用法：
//   node scripts/next-scene.mjs            # 打印生成层（不写文件）
//   node scripts/next-scene.mjs --write    # 写回 NEXT.md 的标记区间
//   node scripts/next-scene.mjs --check    # 只校验：生成层与实际是否一致，不一致则退出码非 0
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);
const has = f => args.includes(f);
const val = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };

const MARK_BEGIN = '<!-- next-scene:begin -->';
const MARK_END = '<!-- next-scene:end -->';

// 定位项目根：脚本在 <root>/harness/scripts/ 或 <root>/build/v4-staging/scripts/
// 用 fileURLToPath 而不是 URL.pathname —— 后者不 percent-decode，中文路径下会拿到编码串
function findRoot() {
  let dir = path.dirname(fileURLToPath(import.meta.url));
  for (let i = 0; i < 6; i++) {
    if (fs.existsSync(path.join(dir, 'harness')) && fs.existsSync(path.join(dir, 'harness', 'requirements'))) return dir;
    const up = path.dirname(dir);
    if (up === dir) break;
    dir = up;
  }
  return process.cwd();
}
const ROOT = val('--root', findRoot());

// git 调用是**惰性**的 —— 不是每次运行都需要 git 历史，
// 提前调用会让「消费工程（无 .git）」与「--selftest 造临时项目」直接失败。
let gitFail = null;
const git = (a) => {
  if (gitFail) throw new Error(gitFail);
  try { return execFileSync('git', a, { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, cwd: ROOT }).trim(); }
  catch (e) { gitFail = '不在 git 仓库里 —— 提交数要靠 git 历史。先 git init，或在仓库目录内运行。'; throw new Error(gitFail); }
};

/** 读文件，不存在返回 null（不抛 —— 单个数据源缺失不该拖垮整份生成） */
function readIf(p) { try { return fs.readFileSync(path.join(ROOT, p), 'utf8'); } catch { return null; } }

/** 从文本里抠出第一个匹配项的值 */
function pick(text, re) { const m = (text || '').match(re); return m ? m[1].trim() : null; }

// ---------- 自测 ----------
// 临时目录里造项目根，逐条验证关键行为。
// 这些坑每一个都真实发生过：提交数跑步机会让「每次提交后必红 + 新克隆直接 BLOCKED」，
// 非原子替换会冲掉手写层，多区块只处理第一个会让第二区块的错内容蒙混过关。
if (has('--selftest')) {
  const { spawnSync } = await import('node:child_process');
  const os = await import('node:os');
  const SELF = fileURLToPath(import.meta.url);
  const bad = [];
  // 自测要在系统临时目录里造独立仓库并跑 git。git 在钩子 / IDE 里运行时会导出
  // GIT_DIR / GIT_INDEX_FILE / GIT_WORK_TREE 等，而子进程默认继承父进程 env ——
  // 不清理就会把临时项目解析到调用方仓库：既让「非 git 仓库」误判（⑦ 组 FAIL），
  // 又把临时提交漏进真分支。这里显式构造干净 env 传给所有子进程（含子进程里的 git）。
  const GIT_ENV_KEYS = ['GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
    'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_COMMON_DIR', 'GIT_PREFIX',
    'GIT_CEILING_DIRECTORIES', 'GIT_DISCOVERY_ACROSS_FILESYSTEM'];
  const cleanEnv = { ...process.env };
  for (const k of GIT_ENV_KEYS) delete cleanEnv[k];
  const mkProj = () => {
    const p = fs.mkdtempSync(path.join(os.tmpdir(), 'nsp-'));
    fs.mkdirSync(path.join(p, 'harness', 'memory'), { recursive: true });
    fs.mkdirSync(path.join(p, 'harness', 'requirements', 'REQ-20261121-99'), { recursive: true });
    fs.mkdirSync(path.join(p, 'build/v4-staging/skills/demo'), { recursive: true });
    fs.writeFileSync(path.join(p, 'build/v4-staging/SKILL.md'), 'version: 9.9.9\n', 'utf8');
    fs.writeFileSync(path.join(p, 'build/v4-staging/skills/demo/SKILL.md'), '---\nname: demo\n---\n', 'utf8');
    fs.writeFileSync(path.join(p, 'harness/requirements/REQ-20261121-99/state.md'),
      'stage: done\nstatus: active\nG4: PASS | 确认人: x | 时间: 2026-01-01 00:00 | 确认原文: "x"\n', 'utf8');
    fs.writeFileSync(path.join(p, 'harness/memory/open-items.md'),
      '| ID | 类别 | 事项 | 来源 | 状态 | 证据 |\n|---|---|---|---|---|---|\n| OI-1 | 决策 | 某事 | docs/x.md | 待决 | - |\n', 'utf8');
    fs.writeFileSync(path.join(p, 'harness/memory/long-term.md'),
      '[id:aaaaaaaa] 2026-01-01 | 经验 | 测试 | 一条记忆内容 | x.md | - | 2027-01-01 | 0\n', 'utf8');
    const g = (a) => { try { return execFileSync('git', a, { cwd: p, encoding: 'utf8', env: cleanEnv }).trim(); } catch { return ''; } };
    g(['init', '-q']); g(['config', 'user.email', 't@t']); g(['config', 'user.name', 't']);
    g(['add', '-A']); g(['commit', '-qm', 'init']);
    return p;
  };
  const runIn = (root, ...a) => spawnSync(process.execPath, [SELF, ...a, '--root', root], { encoding: 'utf8', env: cleanEnv });

  // ① 标记式替换：手写层逐字不动，标记外不被碰
  {
    const p = mkProj();
    fs.writeFileSync(path.join(p, 'harness/NEXT.md'), '# 现场\n\n<!-- next-scene:begin -->\n<!-- next-scene:end -->\n\n手写层内容\n', 'utf8');
    runIn(p, '--write');
    const out = fs.readFileSync(path.join(p, 'harness/NEXT.md'), 'utf8');
    if (!out.includes('手写层内容')) bad.push('刷新时手写层被冲掉了');
    if (!/9\.9\.9/.test(out)) bad.push('刷新后没生成包版本');
    fs.rmSync(p, { recursive: true, force: true });
  }
  // ② 标记缺失 → 报错且不覆盖
  {
    const p = mkProj();
    const np = path.join(p, 'harness/NEXT.md');
    fs.writeFileSync(np, '# 现场\n\n没有标记\n\n重要内容\n', 'utf8');
    const before = fs.readFileSync(np, 'utf8');
    const r = runIn(p, '--write');
    if (r.status === 0) bad.push('标记缺失时没报错');
    if (fs.readFileSync(np, 'utf8') !== before) bad.push('标记缺失时竟然覆盖了文件');
    fs.rmSync(p, { recursive: true, force: true });
  }
  // ③ 提交数变化不该阻断（写它就过期，数学上无解）—— 提交后仍要放行
  {
    const p = mkProj();
    fs.writeFileSync(path.join(p, 'harness/NEXT.md'), '# 现场\n\n<!-- next-scene:begin -->\n<!-- next-scene:end -->\n', 'utf8');
    runIn(p, '--write');
    const g = (a) => { try { return execFileSync('git', a, { cwd: p, encoding: 'utf8', env: cleanEnv }).trim(); } catch { return ''; } };
    fs.writeFileSync(path.join(p, 'x.txt'), 'x', 'utf8');
    g(['add', '-A']); g(['commit', '-qm', 'second']);
    const r = runIn(p, '--check');
    if (r.status !== 0) bad.push('提交后 --check 竟阻断（提交数跑步机回归）：' + (r.stderr || '').trim().slice(0, 80));
    fs.rmSync(p, { recursive: true, force: true });
  }
  // ④ 版本号错了必须阻断（这是本单的核心价值）
  {
    const p = mkProj();
    fs.writeFileSync(path.join(p, 'harness/NEXT.md'), '# 现场\n\n<!-- next-scene:begin -->\n<!-- next-scene:end -->\n', 'utf8');
    runIn(p, '--write');
    const np = path.join(p, 'harness/NEXT.md');
    fs.writeFileSync(np, fs.readFileSync(np, 'utf8').replace(/9\.9\.9/g, '1.1.1'), 'utf8');
    const r = runIn(p, '--check');
    if (r.status === 0) bad.push('版本号错了竟没阻断');
    fs.rmSync(p, { recursive: true, force: true });
  }
  // ⑤ 多个区块：全部替换、全部校验
  {
    const p = mkProj();
    const blk = '<!-- next-scene:begin -->\nOLD BLOCK\n<!-- next-scene:end -->';
    fs.writeFileSync(path.join(p, 'harness/NEXT.md'), `# 现场\n\n${blk}\n\n手写 A\n\n${blk}\n\n手写 B\n`, 'utf8');
    runIn(p, '--write');
    const out = fs.readFileSync(path.join(p, 'harness/NEXT.md'), 'utf8');
    if (out.includes('OLD BLOCK')) bad.push('多区块时只替换了第一个');
    if ((out.match(/next-scene:begin/g) || []).length !== 2) bad.push('多区块替换后区块数不对');
    if (!out.includes('手写 A') || !out.includes('手写 B')) bad.push('多区块时手写层被冲掉');
    fs.rmSync(p, { recursive: true, force: true });
  }
  // ⑥ 行尾保持（CRLF 不能被换成 LF）
  {
    const p = mkProj();
    fs.writeFileSync(path.join(p, 'harness/NEXT.md'),
      '# 现场\r\n\r\n<!-- next-scene:begin -->\r\n<!-- next-scene:end -->\r\n\r\n手写层\r\n', 'utf8');
    runIn(p, '--write');
    const txt = fs.readFileSync(path.join(p, 'harness/NEXT.md'), 'utf8');
    if (/[^\r]\n/.test(txt)) bad.push('刷新时把 CRLF 换成了 LF（行尾被整体改写）');
    fs.rmSync(p, { recursive: true, force: true });
  }
  // ⑦ 非 git 仓库 → 报错退出（不能静默生成全「未知」的假现场）
  {
    const p = mkProj();
    fs.rmSync(path.join(p, '.git'), { recursive: true, force: true });
    const r = runIn(p);
    if (r.status === 0) bad.push('非 git 仓库竟退出 0（静默生成假现场）');
    fs.rmSync(p, { recursive: true, force: true });
  }
  // ⑧ 幂等（除时间戳外内容稳定）
  {
    const p = mkProj();
    fs.writeFileSync(path.join(p, 'harness/NEXT.md'), '# 现场\n\n<!-- next-scene:begin -->\n<!-- next-scene:end -->\n', 'utf8');
    runIn(p, '--write');
    const a = fs.readFileSync(path.join(p, 'harness/NEXT.md'), 'utf8').replace(/最后更新：\*\*[^*]+\*\*/, 'T');
    runIn(p, '--write');
    const b = fs.readFileSync(path.join(p, 'harness/NEXT.md'), 'utf8').replace(/最后更新：\*\*[^*]+\*\*/, 'T');
    if (a !== b) bad.push('重复刷新产生了差异（不幂等）');
    fs.rmSync(p, { recursive: true, force: true });
  }
  // ⑨ 消费工程（无包事实源）→ 跳过且退出 0
  {
    const p = fs.mkdtempSync(path.join(os.tmpdir(), 'nscons-'));
    fs.mkdirSync(path.join(p, 'harness', 'memory'), { recursive: true });
    const g = (a) => { try { return execFileSync('git', a, { cwd: p, encoding: 'utf8', env: cleanEnv }).trim(); } catch { return ''; } };
    g(['init', '-q']); g(['config', 'user.email', 't@t']); g(['config', 'user.name', 't']);
    fs.writeFileSync(path.join(p, 'x.txt'), 'x', 'utf8'); g(['add', '-A']); g(['commit', '-qm', 'i']);
    const r = runIn(p, '--check');
    if (r.status !== 0) bad.push('消费工程 --check 竟阻断（应跳过）');
    fs.rmSync(p, { recursive: true, force: true });
  }
  // ⑩ 「最后更新」那一行里的版本号错也要抓到（不能整行排除）
  {
    const p = mkProj();
    fs.writeFileSync(path.join(p, 'harness/NEXT.md'), '# 现场\n\n<!-- next-scene:begin -->\n<!-- next-scene:end -->\n', 'utf8');
    runIn(p, '--write');
    const np = path.join(p, 'harness/NEXT.md');
    fs.writeFileSync(np, fs.readFileSync(np, 'utf8').replace(/(- 最后更新：\*\*)[^*]+(\*\* · 包版本 \*\*)[^*]+/, '$1xx$2 9.9.9'), 'utf8');
    const r = runIn(p, '--check');
    if (r.status === 0) bad.push('「最后更新」行的版本号错了竟没抓到');
    fs.rmSync(p, { recursive: true, force: true });
  }

  // ⑪ 空模板包（刚 init 出来、没填内容）不应扰动现场 —— 它对任何门禁结论都没影响，
  //     但若计入「任务单 N 个」就会让 NEXT.md 立刻被判过期（自检阻断）。
  {
    const p = mkProj();
    const reqDir = path.join(p, 'harness', 'requirements');
    fs.mkdirSync(path.join(reqDir, 'REQ-20261122-99'), { recursive: true });
    fs.writeFileSync(path.join(reqDir, 'REQ-20261122-99', 'state.md'),
      '---\nreq_id: X\nstage: requirement\nstatus: active\nowner: <维护者>\nupdated: 2026-11-22\nsize: S | M | L\n---\n\n# 状态\n\n## 1. 当前进度\n\n- [ ] <已完成项>\n- [ ] <进行中项>\n', 'utf8');
    fs.writeFileSync(path.join(p, 'harness/NEXT.md'), '# 现场\n\n<!-- next-scene:begin -->\n<!-- next-scene:end -->\n', 'utf8');
    runIn(p, '--write');
    const withEmpty = fs.readFileSync(path.join(p, 'harness/NEXT.md'), 'utf8');
    const n1 = (withEmpty.match(/任务单 \*\*\d+\*\*/) || [''])[0];
    fs.rmSync(path.join(reqDir, 'REQ-20261122-99'), { recursive: true, force: true });
    runIn(p, '--write');
    const without = fs.readFileSync(path.join(p, 'harness/NEXT.md'), 'utf8');
    const n2 = (without.match(/任务单 \*\*\d+\*\*/) || [''])[0];
    if (n1 !== n2) bad.push(`空模板包扰动了现场（${n1} vs ${n2}）—— 空包应被忽略`);
    if (runIn(p, '--check').status !== 0) bad.push('有空模板包时 --check 竟阻断');
    fs.rmSync(p, { recursive: true, force: true });
  }

  if (bad.length) { console.error('NEXT-SCENE SELFTEST FAIL: ' + bad.join('；')); process.exit(1); }
  console.log('NEXT-SCENE SELFTEST OK 11 组（标记式替换 / 标记缺失不覆盖 / 提交后不阻断 / 版本错阻断 / 多区块 / 行尾保持 / 非 git 报错 / 幂等 / 消费工程跳过 / 最后更新行校验 / 空模板包不扰动）');
  process.exit(0);
}

// ---------- 收集真相源 ----------

// 包版本：build/v4-staging/SKILL.md 的 version:
const pkgVer = pick(readIf('build/v4-staging/SKILL.md'), /^version:\s*([0-9.]+)/m);

// 消费工程没有包事实源（build/v4-staging/ 是包工程专有）—— 那里没有可生成的现场
const IS_PKG = !!pkgVer;

// 技能数：build/v4-staging/skills/*/SKILL.md
let skillCount = 0;
try {
  const sd = path.join(ROOT, 'build', 'v4-staging', 'skills');
  skillCount = fs.readdirSync(sd).filter(n => fs.existsSync(path.join(sd, n, 'SKILL.md'))).length;
} catch { /* 目录不存在 → 0，--check 会报出来 */ }

// 引擎版本（在文件头的注释里，形如「harness engine v0.9」）
const engineVer = pick(readIf('harness/engine/harness.mjs'), /harness engine (v[0-9.]+)/);

// 提交数 —— **只做展示，不做阻断依据**。
// 理由（数学上无解）：文件写「126 次」的那一刻是对的，提交那一刻 HEAD 就变 127 ——
// 除非把它从比对集里剔除，否则每次提交后 selfcheck 必红，新克隆仓库更是直接 BLOCKED
// （clone 后 HEAD 比文件里大）。阻断依据用稳定量：版本/技能数/任务单/欠账/记忆。
let commitCount = null, commitShort = null;
try {
  commitCount = parseInt(git(['rev-list', '--count', 'HEAD']), 10);
  commitShort = git(['rev-parse', '--short', 'HEAD']);
} catch {
  console.error('不在 git 仓库里 —— 提交数要靠 git 历史。先 git init，或在仓库目录内运行。');
  process.exit(1);
}

// 任务单清单：每个 state.md 的 stage/status/G4
const reqs = [];
try {
  const rd = path.join(ROOT, 'harness', 'requirements');
  for (const name of fs.readdirSync(rd).filter(n => /^REQ-/.test(n)).sort()) {
    const st = readIf(path.join('harness', 'requirements', name, 'state.md'));
    if (!st) continue;
    // 跳过**空模板包** —— 刚 init 出来、state.md 还是模板占位符的包不算真任务单。
    // 否则一个残留空包就会让「任务单 N 个」变多，NEXT.md 立刻被判过期（自检阻断），
    // 而那个包本身对任何门禁结论都没有影响。
    // 判据：state.md 里还有模板占位符（<已完成项> / <进行中项> / <TBD>）→ 空模板。
    // 注意不能按「未收口」跳 —— 那样会把真在推进的包（REQ-09 待确认、REQ-12 未开始）也藏掉。
    // 判据只看「当前进度」那一段 —— 不能全文找 <TBD>：填好的包里也常有 <TBD>（未验证项表）。
    const isEmptyTemplate = /- \[ \] <已完成项>/.test(st) && /- \[ \] <进行中项>/.test(st);
    // 但「模板占位还在」不等于「没在推进」：只要有任何门禁已过、或有人给过确认原文、
    // 或进度里有勾选项，就算在推进，必须计入 —— 否则下一个工具在做的需求包会从现场里消失。
    const hasProgress = /G[1-4]:\s*PASS/.test(st) || /确认人:\s*(?!<TBD>|—|-|$)/.test(st) || /- \[x\]/.test(st);
    const stage = pick(st, /^stage:\s*(.+)$/m) || '未知';
    const status = pick(st, /^status:\s*(.+)$/m) || '未知';
    const g4 = (st.match(/^G4:\s*(\S+)/m) || [, '未开始'])[1];
    // 阶段字段与门禁不一致 —— 标出来（不阻断，但要被看见）。
    // 收口态以引擎的判据为准：stage ∈ archive/done/released（见 harness.mjs 的未收口判定）。
    const CLOSED_STAGES = ['archive', 'done', 'released'];
    const drift = (g4 === 'PASS' && !CLOSED_STAGES.includes(stage)) ? `（阶段字段与 G4=${g4} 不一致）` : '';
    if (isEmptyTemplate && !hasProgress) continue;   // 空模板且毫无进展 → 不计入
    reqs.push({
      name, stage: stage + drift, status, g4, empty: isEmptyTemplate,
      mtime: fs.statSync(path.join(ROOT, 'harness', 'requirements', name, 'state.md')).mtimeMs
    });
  }
} catch { /* 无需求目录 */ }

// 欠账数：open-items.md 的状态列
let debts = null;
const oi = readIf('harness/memory/open-items.md');
if (oi) {
  const counts = {};
  for (const line of oi.split(/\r?\n/)) {
    if (!/^\s*\|/.test(line)) continue;
    const c = line.split('|').slice(1, -1).map(x => x.trim());
    if (c.length < 5) continue;
    const s = c[4];
    if (!['待决', '未做', '部分', '已做', '已废弃'].includes(s)) continue;
    counts[s] = (counts[s] || 0) + 1;
  }
  const order = ['待决', '未做', '部分', '已做', '已废弃'];
  const total = order.reduce((n, k) => n + (counts[k] || 0), 0);
  // 欠账口径与引擎一致：待决 + 未做 + 部分（begin 报的就是这三个之和）
  const OPEN = ['待决', '未做', '部分'];
  const open = OPEN.reduce((n, k) => n + (counts[k] || 0), 0);
  debts = {
    total,
    open,
    openParts: OPEN.filter(k => counts[k]).map(k => `${k} ${counts[k]}`).join(' · ') || '无',
    done: counts['已做'] || 0,
    dropped: counts['已废弃'] || 0
  };
}

// 长期记忆条数（行首可能有 [id:xxxxxxxx] 前缀，别漏了它）
let memCount = null;
const lt = readIf('harness/memory/long-term.md');
if (lt) {
  memCount = lt.split(/\r?\n/).filter(l => /^(?:\[id:[0-9a-f]{8}\]\s*)?\d{4}-\d{2}-\d{2}\s*\|/.test(l)).length;
}

// ---------- 生成层内容 ----------

function localTime() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function build() {
  const L = [];
  L.push('<!-- 本区块由 scripts/next-scene.mjs 生成，勿手改。改了下次跑 `node scripts/next-scene.mjs --write` -->');
  L.push(`- 最后更新：**${localTime()}** · 包版本 **${pkgVer || '未知'}** · 提交 **${commitCount ?? '未知'}**（${commitShort || '—'}）`);
  L.push('');
  L.push('### 当前状态（自动生成）');
  L.push('');
  L.push('');
  L.push(`- 包版本 **${pkgVer || '未知'}** · 技能 **${skillCount}** 个 · 引擎 \`harness engine ${engineVer || '未知'}\` · 提交 **${commitCount ?? '未知'}** 次`);
  L.push(`- 任务单 **${reqs.length}** 个：${reqs.map(r => `${r.name.replace('REQ-', '')}(${r.g4})`).join('、') || '无'}`);
  if (reqs.some(r => r.stage.includes('不一致'))) {
    L.push(`- ⚠ 有 ${reqs.filter(r => r.stage.includes('不一致')).length} 个任务单的阶段字段与门禁状态不一致（不影响判定，但该修）：${reqs.filter(r => r.stage.includes('不一致')).map(r => r.name).join('、')}`);
  }
  // 措辞要对齐引擎口径：欠账 = 待决+未做+部分；已做/已废弃是历史，不算「欠着」。
  // 否则 NEXT.md 说「36 条未结」而 begin 说「欠账 8」，同一概念两个数，新会话会困惑。
  if (debts) L.push(`- 欠账 **${debts.open}** 条（${debts.openParts}）—— 清单共 ${debts.total} 行，含已做 ${debts.done} / 已废弃 ${debts.dropped}（不算欠账）；详见 \`harness/memory/open-items.md\``);
  if (memCount !== null) L.push(`- 长期记忆 **${memCount}** 条 —— \`harness/memory/long-term.md\``);
  L.push('');
  L.push('### 任务单明细（自动生成）');
  L.push('');
  L.push('| 任务单 | 阶段 | 状态 | G4 |');
  L.push('|---|---|---|---|');
  for (const r of reqs) L.push(`| ${r.name} | ${r.stage} | ${r.status} | ${r.g4} |`);
  return L.join('\n');
}

function sectionText(text, title) {
  const m = String(text || '').match(new RegExp('##[^\\n]*' + title + '[^\\n]*\\n([\\s\\S]*?)(?=\\n## |$)'));
  return m ? m[1] : '';
}

function buildBrief() {
  const live = reqs.filter(r => !['archive', 'done', 'released'].includes(r.stage));
  const req = [...(live.length ? live : reqs)].sort((a, b) => b.mtime - a.mtime)[0] || null;
  if (!req) {
    return ['# NEXT-BRIEF', '', '- 当前任务：无', '- 阶段：无', `- 最近提交：${commitShort || '—'}`, '- 阻塞：无', '- 禁止事项：无', '', '完整 NEXT.md 按需查看。'].join('\n');
  }
  const state = readIf(path.join('harness', 'requirements', req.name, 'state.md')) || '';
  const reqText = readIf(path.join('harness', 'requirements', req.name, '00-requirement.md')) || '';
  const next = sectionText(state, '下一步').split(/\r?\n/)
    .map(x => x.trim()).filter(x => /^\d+\./.test(x) && !x.includes('<')).slice(0, 3);
  const blockers = sectionText(state, '阻塞').split(/\r?\n/)
    .filter(x => /^\s*\|/.test(x) && !/^\s*\|\s*---/.test(x) && !/阻塞项/.test(x))
    .map(x => x.split('|').slice(1, -1).map(v => v.trim()).join(' · '))
    .filter(x => x && !/^无\s*·/.test(x));
  const forbidden = (reqText.match(/不做项[：:]\s*(.+)/) || [])[1]
    || (state.match(/接手注意[：:]\s*(.+)/) || [])[1]
    || '按需求边界执行';
  const lines = [
    '# NEXT-BRIEF（自动生成，默认先读这个）',
    '',
    `- 当前任务：${req.name}`,
    `- 阶段：${req.stage}`,
    `- 最近提交：${commitShort || '—'}`,
    `- 阻塞：${blockers.length ? blockers.join('；') : '无'}`,
    `- 禁止事项：${forbidden}`,
    '',
    '## 下一步',
    ...(next.length ? next : ['1. 读取对应 state.md 补齐下一步']),
    '',
    `详细状态：harness/requirements/${req.name}/state.md`,
    '完整 NEXT.md 保留，但默认按需再读。'
  ];
  return lines.join('\n');
}

// ---------- 标记式替换 ----------

const NEXT_REL = 'harness/NEXT.md';
const BRIEF_REL = 'harness/NEXT-BRIEF.md';

// 全局匹配所有生成层区块（文件里有多个就全部替换，顺序不变）
const MARK_RE = /<!-- next-scene:begin -->[\s\S]*?<!-- next-scene:end -->/g;

/** 探测文件行尾（CRLF / LF）—— 写回时必须还原，否则整体重写会把行尾换掉 */
function detectEol(text) { return text.includes('\r\n') ? '\r\n' : '\n'; }

function replaceMarked(text, generated) {
  const matches = text.match(MARK_RE);
  if (!matches || !matches.length) {
    console.error(`NEXT.md 里找不到生成标记（${MARK_BEGIN} / ${MARK_END}）—— 拒绝覆盖，手写内容可能丢。`);
    console.error('  修法：在 NEXT.md 里插入这两个标记，把要自动生成的内容放在中间。');
    process.exit(1);
  }
  const eol = detectEol(text);
  const payload = MARK_BEGIN + eol + generated.replace(/\r?\n/g, eol) + eol + MARK_END;
  // 全局替换：所有区块都换成同一份生成内容，顺序不变
  return text.replace(MARK_RE, () => payload);
}

// ---------- 主流程 ----------

const generated = build();
const briefGenerated = buildBrief();

if (has('--write') || has('--check')) {
  // 消费工程没有包事实源（build/v4-staging/ 是包工程专有），那里生成不出有意义的现场。
  // 若消费工程自己建了 NEXT.md，硬校验会永远红且无法自愈 —— 跳过并说明。
  if (!IS_PKG) {
    console.log('当前工程不是技能包（无 build/v4-staging/SKILL.md）—— 没有可生成的现场事实源，跳过。');
    process.exit(0);
  }
}

if (has('--write')) {
  const nextText = readIf(NEXT_REL);
  if (nextText === null) { console.error(`找不到 ${NEXT_REL}`); process.exit(1); }
  const updated = replaceMarked(nextText, generated);
  fs.writeFileSync(path.join(ROOT, NEXT_REL), updated, 'utf8');
  fs.writeFileSync(path.join(ROOT, BRIEF_REL), briefGenerated + '\n', 'utf8');
  console.log(`已更新 ${NEXT_REL} 的生成层（手写层未动）`);
  console.log(`已更新 ${BRIEF_REL}`);
  console.log(`  包版本 ${pkgVer || '未知'} · 技能 ${skillCount} · 提交 ${commitCount ?? '未知'} · 任务单 ${reqs.length} 个`);
  process.exit(0);
}

if (has('--check')) {
  const nextText = readIf(NEXT_REL);
  if (nextText === null) { console.error(`找不到 ${NEXT_REL} —— 现场文件缺失`); process.exit(1); }
  const blocks = nextText.match(MARK_RE);
  if (!blocks || !blocks.length) {
    console.error('NEXT.md 里没有生成层标记 —— 现场文件是纯手写的，过期了也没人知道。');
    console.error('  修法：插入 <!-- next-scene:begin --> / <!-- next-scene:end --> 标记，跑 --write 生成。');
    process.exit(1);
  }
  // 比较时排除两类**必然变化**的量：
  //  ① 时间戳 —— 每次生成都变
  //  ② 提交数 —— 提交那一刻就变（写它就过期，数学上无解，见下方注释）
  // 其余字段（版本/技能数/任务单清单与门禁/欠账/记忆条数）一律逐字比。
  const normalize = s => s
    .replace(/最后更新：\*\*[^*]+\*\*/g, '最后更新：**T**')
    .replace(/提交 \*\*\d+\*\*（[0-9a-f]+\）/g, '提交 **N**（H）')
    .replace(/提交 \*\*\d+\*\* 次/g, '提交 **N** 次')
    .split('\n').map(l => l.trimEnd()).join('\n').trim();
  const expected = normalize(MARK_BEGIN + '\n' + generated + '\n' + MARK_END);
  const bad = [];
  for (const blk of blocks) {
    if (normalize(blk) !== expected) bad.push(blk);
  }
  const briefText = readIf(BRIEF_REL);
  const briefExpected = briefGenerated.replace(/最近提交：.*$/m, '最近提交：T');
  const briefActual = String(briefText || '').replace(/最近提交：.*$/m, '最近提交：T');
  if (!briefText) bad.push('NEXT-BRIEF.md');
  else if (briefActual.trim() !== briefExpected.trim()) bad.push('NEXT-BRIEF.md');
  if (!bad.length) {
    console.log(`NEXT.md 现场与实际一致（包版本 ${pkgVer || '未知'} · 技能 ${skillCount} · 提交 ${commitCount ?? '未知'}（仅展示） · 任务单 ${reqs.length} 个）`);
    console.log(`NEXT-BRIEF.md 现场与实际一致`);
    process.exit(0);
  }
  // 逐字段指出差在哪 —— 只说「不一致」没用，要说清哪个字段
  const cur = normalize(bad[0]);
  const gen = expected;
  const curLines = new Set(cur.split('\n').map(l => l.trim()));
  const genLines = gen.split('\n').map(l => l.trim());
  const missing = genLines.filter(l => l && !curLines.has(l));
  const extra = [...curLines].filter(l => l && !genLines.includes(l));
  console.error(`NEXT 现场与生成结果不一致（${bad.length} 项）—— 新会话会读到错的现场：`);
  if (missing.length) { console.error('  实际有、文件里没有：'); missing.slice(0, 6).forEach(l => console.error('    + ' + l.slice(0, 100))); }
  if (extra.length) { console.error('  文件里有、实际已变：'); extra.slice(0, 6).forEach(l => console.error('    - ' + l.slice(0, 100))); }
  if (bad.some(x => x === 'NEXT-BRIEF.md')) console.error('  NEXT-BRIEF.md 缺失或过期');
  console.error('  修法：node scripts/next-scene.mjs --write');
  process.exit(1);
}

// 默认：打印
console.log(generated);
