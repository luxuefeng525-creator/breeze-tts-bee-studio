#!/usr/bin/env node
// harness engine v0.9：init / layout / status / probe / inventory / scaffold / handoff / gate G1-G4 / gate V / visual / clean
// 设计原则：判定权在脚本，模型只负责填文档、改代码、转述退出码。
// 退出码：0 通过 / 1 用法或 IO 错 / 2 门禁不通过（含清理未完成） / 3 能力未实现
// 零依赖：只用 node 内置模块。
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const SELF = fileURLToPath(import.meta.url);
const SELF_DIR = path.dirname(SELF);
const TEMPLATES_DIR = path.resolve(SELF_DIR, '..', 'templates');
const REFS_DIR = path.resolve(SELF_DIR, '..', 'references');

const argv = process.argv.slice(2);
const cmd = argv[0];
const opt = (name, def) => {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : def;
};
const ROOT = path.resolve(opt('--dir', process.cwd()));

/** 取位置参数，跳过 --dir/--stage/--req 的值 */
function positionals() {
  const out = [];
  const withValue = new Set(['--dir', '--stage', '--req', '--size']);
  for (let i = 1; i < argv.length; i++) {
    const a = argv[i];
    if (withValue.has(a)) { i++; continue; }
    if (a.startsWith('--')) continue;
    out.push(a);
  }
  return out;
}

function die(code, msg) {
  console.error(msg);
  process.exit(code);
}

function mkdirp(p) {
  fs.mkdirSync(p, { recursive: true });
}

/** 写入文件；已存在则跳过。返回 created / exists */
function writeIfAbsent(file, content) {
  if (fs.existsSync(file)) return 'exists';
  mkdirp(path.dirname(file));
  fs.writeFileSync(file, content, 'utf8');
  return 'created';
}

const ENTRY_BEGIN = '<!-- harness:begin -->';
const ENTRY_END = '<!-- harness:end -->';

/** 跨助手入口块：任何 AI / 工具先跑 begin 拿到活跃任务单，再读 NEXT-BRIEF.md 与 state.md；
 *  HANDOFF.md 只是自动刷新的快照，不当作第一手现场。 */
function handoffBlock() {
  return [
    ENTRY_BEGIN,
    '## harness 入口（自动生成，勿手改）',
    '',
    '- 规则只读一份：`AGENTS.md`；细则查 `harness/references/manual.md`。',
    '- 开工先跑 `node harness/engine/harness.mjs begin "<需求>"`：直出当前活跃任务单与下一步，红灯未清不动手。',
    '- 接手别人的活：按 begin → `harness/NEXT-BRIEF.md` → `harness/requirements/<REQ>/state.md` 的顺序读；完整 `NEXT.md` 只在需要历史/任务明细时读；`HANDOFF.md` 是自动刷新的快照。别凭聊天记忆续接。',
    '',
    '## 多会话并发纪律',
    '',
    '- 一个会话（agent + 工具）一个工作树一个分支：`node harness/scripts/git-atomic.mjs worktree <tool>` → 分支 `session/<tool>-<yyyymmdd>`、工作树 `../<repo>-<tool>`；同日撞名复用已存在的工作树。',
    '- 只在自己的工作树里改；任务单目录与正式记忆（ADR-0002）不动。',
    '- main 是唯一汇合点：从 main 切出，提交只碰本分支路径；结项后 `git-atomic.mjs merge <branch> <REQ>` 回 main，再删工作树与分支。',
    '- 主工作区的既有改动不是本分支的：不 add、不 commit、不 reset。',
    ENTRY_END,
    ''
  ].join('\n');
}

/** 幂等写入入口块：有旧块就替换，没有就追加；返回写入后的内容 */
function upsertBlock(file, text) {
  const old = fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '';
  const next = old.includes(ENTRY_BEGIN)
    ? old.replace(new RegExp(`${ENTRY_BEGIN}[\\s\\S]*?${ENTRY_END}\\n?`), text)
    : old.trimEnd() + (old.trim() ? '\n\n' : '') + text;
  mkdirp(path.dirname(file));
  fs.writeFileSync(file, next, 'utf8');
  return next;
}

function readTemplate(name) {
  const p = path.join(TEMPLATES_DIR, name);
  return fs.existsSync(p) ? fs.readFileSync(p, 'utf8') : null;
}

/** 定位脚本：包内 scripts/ → 工程内 harness/scripts/ → 技能目录 scripts/
 *  引擎可能被复制到任意工程的 harness/engine/ 下，三处都要能找得到 */
function findScript(name) {
  const cands = [
    path.join(path.dirname(path.dirname(path.dirname(path.dirname(SELF)))), 'scripts', name),
    path.join(path.dirname(path.dirname(SELF)), 'scripts', name),
    path.join(SELF_DIR, 'scripts', name)
  ];
  return cands.find(p => fs.existsSync(p)) || null;
}

/** 定位技能包根（templates/references 所在处） */
function findSkillRoot() {
  const cands = [
    path.dirname(path.dirname(path.dirname(path.dirname(SELF)))),   // <pkg>/skills/harness/engine → <pkg>
    path.dirname(path.dirname(SELF))                              // <工程>/harness/engine → <工程>/harness
  ];
  for (const c of cands) {
    if (fs.existsSync(path.join(c, 'templates', 'AGENTS.md'))) return c;
  }
  return cands[cands.length - 1];
}

function today() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function stamp() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return `${today()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 秒级本地时间戳 —— 信箱回执用它（需求精度到秒） */
function tsSec() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return `${today()} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 信箱回执来源：优先 git 分支名（能区分并发的工作树），取不到就用工程目录名 */
function msgFrom() {
  try {
    const r = spawnSync('git', ['rev-parse', '--abbrev-ref', 'HEAD'], { cwd: ROOT, encoding: 'utf8', timeout: 5000 });
    const b = (r.stdout || '').trim();
    if (r.status === 0 && b) return b;
  } catch { /* 不在 git 仓库里就降级 */ }
  return path.basename(ROOT);
}

/** 正文疑似密钥/口令 —— 命中就不落盘（需求 §4.1 隐私边界） */
const SECRET_RE = /(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+)/i;

function mailboxFile() {
  return path.join(ROOT, 'harness', 'mailbox.jsonl');
}

function readMailbox() {
  const f = mailboxFile();
  if (!fs.existsSync(f)) return [];
  return fs.readFileSync(f, 'utf8').split(/\r?\n/).filter(Boolean)
    .map(l => { try { return JSON.parse(l); } catch { return null; } })
    .filter(Boolean);
}

/** 未读回执：只追加、不重写（需求 §2.2）。
 *  已读不是改字段，而是追加一条 type=read 事件；事件之后的写条目视为已读。
 *  这样并发的 msg 追加与 --read 不再争抢同一个文件，谁也不会抹掉谁。 */
function unreadMail() {
  const rows = readMailbox();
  const pending = new Set();
  rows.forEach((r, i) => {
    if (!r) return;
    if (r.type === 'read') {
      for (const j of pending) {
        if (r.to === 'all' || rows[j].to === r.to) pending.delete(j);
      }
      return;
    }
    if (r.text !== undefined) pending.add(i);
  });
  return [...pending].map(i => rows[i]);
}

// ---------- msg：跨会话信箱 ----------
function cmdMsg() {
  const to = opt('--to', null);
  const text = opt('--text', null);
  const doRead = argv.includes('--read');
  const f = mailboxFile();

  if (doRead) {
    const target = to || 'all';
    const n = unreadMail().filter(m => target === 'all' || m.to === target).length;
    const ev = { type: 'read', to: target, by: msgFrom(), ts: tsSec() };
    try {
      mkdirp(path.dirname(f));
      fs.appendFileSync(f, JSON.stringify(ev) + '\n', 'utf8');
    } catch (e) { die(1, `信箱写不了：${e.message}`); }
    console.log(`已读 ${n} 条${target !== 'all' ? `（to=${target}）` : ''} · harness/mailbox.jsonl`);
    process.exit(0);
  }

  if (!to) die(1, '用法：harness.mjs msg --to <REQ|all> --text "<正文>"');
  if (!text) die(1, '缺少 --text');
  if (SECRET_RE.test(text)) die(1, '正文疑似含密钥/口令，按需求 §4.1 不落盘 —— 请改用占位（如 <密钥见 .env>）');

  const rec = { from: msgFrom(), to, text, ts: tsSec(), read: false };
  try {
    mkdirp(path.dirname(f));
    fs.appendFileSync(f, JSON.stringify(rec) + '\n', 'utf8');
  } catch (e) { die(1, `回执写不进去：${e.message}`); }
  console.log(`已写入回执 → ${to} · harness/mailbox.jsonl（${rec.ts}）`);
  process.exit(0);
}

/** 本地日期 YYYY-MM-DD（不用 toISOString —— 它取 UTC，本地凌晨会差一天） */
function localDate(d = new Date()) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

const REQ_RE = /^REQ-(\d{8})-(\d{2})$/;

function nextReqId() {
  const base = today().replace(/-/g, '');
  const dir = path.join(ROOT, 'harness', 'requirements');
  let max = 0;
  const taken = new Set();
  if (fs.existsSync(dir)) {
    for (const name of fs.readdirSync(dir)) {
      const m = name.match(REQ_RE);
      if (!m || m[1] !== base) continue;
      taken.add(name);
      const n = Number(m[2]);
      if (Number.isInteger(n)) max = Math.max(max, n);
    }
  }
  // 编号必须避开**所有**已存在的目录，不只是数字后缀的 ——
  // 并发写入时可能出现 REQ-20261122-G1 这种非数字后缀的目录（另一个工具建的），
  // Number('G1') 是 NaN，取不到最大编号，下次 init 会算出已存在的编号 → 覆盖别人的包。
  let i = max + 1;
  while (taken.has(`REQ-${base}-${String(i).padStart(2, '0')}`)) i++;
  return `REQ-${base}-${String(i).padStart(2, '0')}`;
}

const CONFIG = {
  version: '0.1.0',
  baseUrl: 'YOUR_BASE_URL_HERE',
  viewports: [
    { name: 'desktop', width: 1440, height: 900 },
    { name: 'tablet', width: 768, height: 1024 },
    { name: 'mobile', width: 375, height: 812 }
  ],
  pages: [{ name: 'home', path: '/', states: ['default'], expect: [], emptyAllowed: [] }],
  capture: { command: 'YOUR_CAPTURE_COMMAND_HERE' },
  evidenceMaxAgeHours: 24
};

/** 读取目标工程的采集配置；证据时效默认 24 小时，0 表示不校验 */
let _cfgCache = null;
function readConfig() {
  if (_cfgCache) return _cfgCache;
  const f = path.join(ROOT, 'harness', 'harness.config.json');
  try { _cfgCache = JSON.parse(fs.readFileSync(f, 'utf8')); }
  catch { _cfgCache = {}; }
  return _cfgCache;
}
function evidenceMaxAgeHours() {
  const v = readConfig().evidenceMaxAgeHours;
  return typeof v === 'number' ? v : 24;
}

/** 从 references/rules.md 拆出 must / should / can 三块；失败返回 null */
function splitRules(text) {
  const blocks = text.split(/\n(?=## )/);
  // 必须匹配「章节标题行」而不是整块首行 —— 标题块「# 三层规则（must / should / can）」
  // 同时含三个关键词，用首行匹配会让三份文件都取到同一块（历史 bug：三份都只剩标题）
  const pick = kw => blocks.find(b => {
    const first = b.split('\n')[0].trim().toLowerCase();
    return first.startsWith('## ') && first.includes(kw);
  });
  const must = pick('must');
  const should = pick('should');
  const can = pick('can');
  if (!must || !should || !can) return null;
  return { must, should, can };
}

// ---------- init ----------
function cmdInit() {
  const desc = positionals().join(' ').trim();
  if (!desc) die(1, '用法：harness.mjs init "一句话需求" [--dir <项目目录>]');
  if (!fs.existsSync(TEMPLATES_DIR)) {
    die(1, `找不到模板目录：${TEMPLATES_DIR}\n请从技能目录运行：node <技能目录>/engine/harness.mjs init "需求" --dir <项目>`);
  }
  mkdirp(ROOT);

  const reqId = nextReqId();
  const reqDir = path.join(ROOT, 'harness', 'requirements', reqId);
  const docs = [
    ['00-requirement.md', 'requirement.md'],
    ['01-design.md', 'design.md'],
    ['02-test.md', 'test.md'],
    ['03-review.md', 'review.md'],
    ['04-release.md', 'release.md'],
    ['05-visual.md', 'visual.md']
  ];
  const created = [];
  const existed = [];

  for (const [target, tplName] of docs) {
    const tpl = readTemplate(tplName);
    if (tpl === null) die(1, `模板缺失：templates/${tplName}`);
    const r = writeIfAbsent(path.join(reqDir, target), tpl);
    (r === 'created' ? created : existed).push(target);
  }

  const stateTpl = readTemplate('state.md');
  if (stateTpl === null) die(1, '模板缺失：templates/state.md');

  // ------- 强制跑 route.mjs，把命中技能写进 state.md hits -------
  let hits = [];
  try {
    const routeScript = path.join(ROOT, 'harness', 'scripts', 'route.mjs');
    if (fs.existsSync(routeScript)) {
      const r = spawnSync('node', [routeScript, desc], { encoding: 'utf8', timeout: 10000 });
      if (r.status === 0) {
        hits = [...r.stdout.matchAll(/^\s*\d+\.\s+(\S+)\s+\(score=/gm)].map(m => m[1]);
        console.log(`  route 命中 ${hits.length} 个技能：${hits.join(', ') || '(无)'}`);
      }
    }
  } catch (e) {
    console.log(`  route 跳过：${e.message?.split('\n')[0]}`);
  }

  const state = stateTpl
    .replace(/REQ-YYYYMMDD-01/g, reqId)
    .replace(/updated: YYYY-MM-DD/g, `updated: ${today()}`)
    .replace(/hits: \[\]/, `hits: [${hits.map(h => JSON.stringify(h)).join(', ')}]`);
  const st = writeIfAbsent(path.join(reqDir, 'state.md'), state);
  (st === 'created' ? created : existed).push('state.md');

  // 广播 req.active（让 3D 大屏大脑切换到新 REQ）
  try {
    const emitScript = path.join(ROOT, 'harness', 'scripts', 'emit-event.mjs');
    if (fs.existsSync(emitScript)) {
      spawnSync(process.execPath, [emitScript, '--type', 'req.active', '--req', reqId],
        { cwd: ROOT, encoding: 'utf8', timeout: 3000 });
    }
  } catch { /* server 没跑 → 静默 */ }

  for (const d of ['visual/ref', 'visual/shot']) mkdirp(path.join(reqDir, d));
  mkdirp(path.join(ROOT, 'harness', 'logs'));
  writeIfAbsent(path.join(ROOT, 'harness', 'logs', '.gitkeep'), '');
  mkdirp(path.join(ROOT, 'harness', 'memory'));
  writeIfAbsent(path.join(ROOT, 'harness', 'memory', 'long-term.md'), '# 长期记忆\n\n> 只收「已验证 + 可复用」的经验，带证据与复审时间。\n');
  // 未结事项：文档里定了但没执行的事。没有这张表，写在 docs 里的决定等于从没发生。
  {
    const oiTpl = path.join(SELF_DIR, '..', 'templates', 'open-items.md');
    writeIfAbsent(path.join(ROOT, 'harness', 'memory', 'open-items.md'),
      fs.existsSync(oiTpl) ? fs.readFileSync(oiTpl, 'utf8')
        : '# 未结事项（活清单）\n\n| ID | 类别 | 事项 | 来源 | 状态 | 证据/卡在哪 |\n|---|---|---|---|---|---|\n');
  }
  // 域目录：行业基线的落点。一个域一份文件，写法见 references/domain-rules.md
  mkdirp(path.join(ROOT, 'harness', 'domains'));
  const domainReadme = path.join(ROOT, 'harness', 'domains', 'README.md');
  if (!fs.existsSync(domainReadme)) {
    fs.writeFileSync(domainReadme,
      '# 域文件\n\n' +
      '一个域一份文件（如 `rider.md`），四块固定：行业基线 / 我们的现状与差距 / 已定取舍 / 变更历史。\n' +
      '每条行业基线必须带证据锚（可公开引用的 URL 带章节，或 `文件:行号`）。\n' +
      '查询：`node harness/engine/harness.mjs domain <关键词>`。\n' +
      '写法与对标方法：`build/v4-staging/skills/harness/references/domain-rules.md` 与 `feature-design` 技能。\n',
      'utf8');
    created.push('harness/domains/README.md');
  }

  // 引擎自复制
  const engineDst = path.join(ROOT, 'harness', 'engine', 'harness.mjs');
  mkdirp(path.dirname(engineDst));
  if (path.resolve(engineDst) !== path.resolve(SELF)) {
    fs.copyFileSync(SELF, engineDst);
    created.push('harness/engine/harness.mjs');
  }

  // 引擎依赖随工程落地：templates / references / scripts
  // 否则目标工程里的引擎找不到模板与规则源（refresh 与 init 都会 die），
  // 且规则里写的 `node harness/scripts/selfcheck.mjs` 会指向不存在的文件
  const harnessDir = path.join(ROOT, 'harness');
  for (const sub of ['templates', 'references']) {
    const src = path.join(SELF_DIR, '..', sub);
    if (!fs.existsSync(src)) continue;
    for (const f of fs.readdirSync(src)) {
      const s = path.join(src, f);
      if (!fs.statSync(s).isFile()) continue;
      const d = path.join(harnessDir, sub, f);
      mkdirp(path.dirname(d));
      fs.copyFileSync(s, d);
      created.push(`harness/${sub}/${f}`);
    }
  }
  const pkgScripts = path.join(path.dirname(path.dirname(path.dirname(path.dirname(SELF)))), 'scripts');
  // 清单必须与 bootstrap 一致 —— 少拷一个，手册里写的命令在新工程里就是死链
  for (const name of ['selfcheck.mjs', 'memory.mjs', 'memory-sync.mjs', 'observe.mjs', 'lint-words.mjs', 'measure-tokens.mjs',
    'hook-precommit.mjs', 'hook-postcommit.mjs', 'hook-postmerge.mjs', 'task-signature.mjs', 'ui-gate.mjs',
    'route.mjs', 'change-log.mjs', 'next-scene.mjs', 'write-guard.mjs', 'metrics.mjs', 'risk-tier.mjs', 'failure-reason.mjs',
    'branch-consistency.mjs', 'start.mjs', 'git-atomic.mjs', 'agent.mjs', 'skills-standard.mjs',
    'verify-fresh.mjs']) {
    const s = path.join(pkgScripts, name);
    if (!fs.existsSync(s)) continue;
    const d = path.join(harnessDir, 'scripts', name);
    mkdirp(path.dirname(d));
    fs.copyFileSync(s, d);
    created.push(`harness/scripts/${name}`);
  }
  // 技能清单也落地（入口块里的「规则全文」指向它）
  const skillMdSrc = path.join(SELF_DIR, '..', 'SKILL.md');
  if (fs.existsSync(skillMdSrc)) {
    const d = path.join(harnessDir, 'SKILL.md');
    fs.copyFileSync(skillMdSrc, d);
    created.push('harness/SKILL.md');
  }

  // 技能索引：入口里引用它，目标工程里必须真的有一份
  {
    const idxBuilder = findScript('build-skills-index.mjs');
    if (idxBuilder) {
      const idxOut = path.join(harnessDir, 'skills-index.md');
      try {
        const r = spawnSync(process.execPath, [idxBuilder, '--out', idxOut], { encoding: 'utf8', timeout: 30000 });
        if (r.status === 0) created.push('harness/skills-index.md');
        else console.log(`  WARN 技能索引生成失败：${(r.stderr || r.stdout || '').trim().split('\n').slice(-1)[0]}`);
      } catch (e) {
        console.log(`  WARN 技能索引生成失败：${String(e && e.message || e)}`);
      }
    }
  }

  const cfg = writeIfAbsent(path.join(ROOT, 'harness', 'harness.config.json'), JSON.stringify(CONFIG, null, 2) + '\n');
  (cfg === 'created' ? created : existed).push('harness/harness.config.json');

  // 把「技能包在哪」记进配置：工程副本里没有 skills/ 目录（44 个技能只装在包里），
  // 不记这一笔，工程内 `skill <名字>` 就只能靠猜宿主路径。
  try {
    const cfgPath = path.join(ROOT, 'harness', 'harness.config.json');
    const cfgObj = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
    if (!cfgObj.skillSource) {
      const sr = findSkillsRoot();
      const insideProject = sr && path.resolve(sr).startsWith(path.resolve(ROOT) + path.sep);
      if (sr && !insideProject) {
        cfgObj.skillSource = sr;
        fs.writeFileSync(cfgPath, JSON.stringify(cfgObj, null, 2) + '\n');
      }
    }
  } catch { }

  const rulesSrc = path.join(REFS_DIR, 'rules.md');
  if (!fs.existsSync(rulesSrc)) die(1, `找不到规则源：${rulesSrc}`);
  const split = splitRules(fs.readFileSync(rulesSrc, 'utf8'));
  if (!split) die(1, '规则源解析失败：references/rules.md 缺少 must/should/can 章节');
  const rulesMap = { must: split.must, should: split.should, can: split.can };
  for (const [key, body] of Object.entries(rulesMap)) {
    const r = writeIfAbsent(path.join(ROOT, 'harness', 'rules', `${key}.md`),
      `<!-- 由 harness engine 从 references/rules.md 生成，勿手改 -->\n\n${body.trim()}\n`);
    (r === 'created' ? created : existed).push(`harness/rules/${key}.md`);
  }

  const agentsTpl = readTemplate('AGENTS.md');
  if (agentsTpl === null) die(1, '模板缺失：templates/AGENTS.md');
  const agents = writeIfAbsent(path.join(ROOT, 'AGENTS.md'), agentsTpl);
  (agents === 'created' ? created : existed).push('AGENTS.md');

  // 跨助手入口：换 AI / 换工具也能找到续接点
  const entries = [];
  for (const rel of ['.trae/rules/harness.md', 'CLAUDE.md']) {
    const file = path.join(ROOT, rel);
    const before = fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '';
    const after = upsertBlock(file, handoffBlock());
    entries.push(`${rel}（${after === before ? '未变' : '已更新'}）`);
  }

  console.log(`INIT ${reqId} · ${desc}`);
  console.log(`目录：${path.join('harness', 'requirements', reqId)}`);
  console.log(`入口：${entries.join('、')}`);
  if (created.length) console.log(`新建：${created.length} 项`);
  if (existed.length) console.log(`保留：${existed.join('、')}（已存在，未覆盖）`);
  console.log('\n下一步：');
  console.log(`  1. 填 00-requirement.md：需求句式、验收点、path:line 代码证据、≥2 类边界、每条需求的同行对标`);
  console.log(`  2. 运行 node harness/engine/harness.mjs layout`);
  console.log(`  3. 运行 node harness/engine/harness.mjs gate ${reqId} --stage G1`);
  // 立项就把现场钉到新包 —— 否则换工具接手时 HANDOFF 还指着上一个包
  writeHandoff(reqId);
  process.exit(0);
}

// ---------- refresh ----------
/** 模板更新后重新生成工程根入口与三层规则。
 *  只覆盖「自证是生成物」的文件（带「勿手改」/「由 harness init 复制」标记）；
 *  被人工改过而丢了标记的文件一律跳过，除非加 --force。 */
function cmdRefresh() {
  const force = argv.includes('--force');
  const rulesSrc = path.join(REFS_DIR, 'rules.md');
  if (!fs.existsSync(rulesSrc)) die(1, `找不到规则源：${rulesSrc}`);
  const split = splitRules(fs.readFileSync(rulesSrc, 'utf8'));
  if (!split) die(1, '规则源解析失败：references/rules.md 缺少 must/should/can 章节');
  const rulesMap = { must: split.must, should: split.should, can: split.can };

  const targets = [];
  for (const [key, body] of Object.entries(rulesMap)) {
    targets.push({
      rel: path.join('harness', 'rules', `${key}.md`),
      content: `<!-- 由 harness engine 从 references/rules.md 生成，勿手改 -->\n\n${body.trim()}\n`,
      marker: '勿手改'
    });
  }
  const agentsTpl = readTemplate('AGENTS.md');
  if (agentsTpl === null) die(1, '模板缺失：templates/AGENTS.md');
  targets.push({ rel: 'AGENTS.md', content: agentsTpl, marker: 'harness init' });

  let updated = 0, unchanged = 0, skipped = 0;

  // 跨助手入口（CLAUDE.md / .trae/rules/harness.md）：只替换 harness 生成块，不动人工内容
  const hb = handoffBlock();
  for (const rel of ['CLAUDE.md', path.join('.trae', 'rules', 'harness.md')]) {
    const abs = path.join(ROOT, rel);
    const cur = fs.existsSync(abs) ? fs.readFileSync(abs, 'utf8') : '';
    const next = cur.includes(ENTRY_BEGIN)
      ? cur.replace(new RegExp(`${ENTRY_BEGIN}[\\s\\S]*?${ENTRY_END}\\n?`), hb)
      : cur.trimEnd() + (cur.trim() ? '\n\n' : '') + hb;
    if (next === cur) { console.log(`  未变   ${rel}`); unchanged++; continue; }
    if (!cur.includes(ENTRY_BEGIN) && cur.trim() && !force) {
      console.log(`  跳过   ${rel}（无 harness 生成块，加 --force 覆盖）`); skipped++; continue;
    }
    mkdirp(path.dirname(abs));
    fs.writeFileSync(abs, next, 'utf8');
    console.log(`  已更新 ${rel}`); updated++;
  }

  // ai-workflow 入口块（.trae/rules/ai-workflow.md）—— 与 6 宿主入口同一份模板
  const entryTplPath = path.join(REFS_DIR, 'entrypoint.md');
  if (fs.existsSync(entryTplPath)) {
    const skillRoot = findSkillRoot();
    const tpl = fs.readFileSync(entryTplPath, 'utf8')
      .replace(/\{\{ENGINE\}\}/g, path.join(skillRoot, 'engine', 'harness.mjs'))
      .replace(/\{\{SKILL\}\}/g, path.join(skillRoot, 'SKILL.md'))
      .replace(/\{\{SELFCHECK\}\}/g, findScript('selfcheck.mjs') || path.join(skillRoot, 'scripts', 'selfcheck.mjs'))
      .replace(/\{\{MEMORY\}\}/g, findScript('memory.mjs') || path.join(skillRoot, 'scripts', 'memory.mjs'))
      .trimEnd() + '\n';
    const rel = path.join('.trae', 'rules', 'ai-workflow.md');
    const abs = path.join(ROOT, rel);
    const cur = fs.existsSync(abs) ? fs.readFileSync(abs, 'utf8') : '';
    const A_START = '<!-- ai-workflow:begin -->', A_END = '<!-- ai-workflow:end -->';
    const next = cur.includes(A_START)
      ? cur.replace(new RegExp(`${A_START}[\\s\\S]*?${A_END}\\n?`), tpl)
      : cur.trimEnd() + (cur.trim() ? '\n\n' : '') + tpl;
    if (next === cur) { console.log(`  未变   ${rel}`); unchanged++; }
    else if (!cur.includes(A_START) && cur.trim() && !force) {
      console.log(`  跳过   ${rel}（无 ai-workflow 生成块，加 --force 覆盖）`); skipped++;
    } else {
      mkdirp(path.dirname(abs));
      fs.writeFileSync(abs, next, 'utf8');
      console.log(`  已更新 ${rel}`); updated++;
    }
  }

  for (const t of targets) {
    const abs = path.join(ROOT, t.rel);
    const cur = fs.existsSync(abs) ? fs.readFileSync(abs, 'utf8') : '';
    if (cur === t.content) { console.log(`  未变   ${t.rel}`); unchanged++; continue; }
    const generated = cur.includes(t.marker);
    if (!generated && !force) {
      console.log(`  跳过   ${t.rel}（无生成物标记，疑似人工改过；确认要覆盖加 --force）`);
      skipped++; continue;
    }
    mkdirp(path.dirname(abs));
    fs.writeFileSync(abs, t.content, 'utf8');
    console.log(`  已更新 ${t.rel}`);
    updated++;
  }
  console.log(`REFRESH updated=${updated} unchanged=${unchanged} skipped=${skipped}`);
  if (skipped) console.log('下一步：确认跳过项是否需要人工合并，或用 --force 覆盖');
  process.exit(0);
}

// ---------- layout ----------
/** 读需求包 state.md 的 stage；读不到返回 ''（按未收口处理，不豁免） */
function readStage(base) {
  const f = path.join(base, 'state.md');
  if (!fs.existsSync(f)) return '';
  const m = fs.readFileSync(f, 'utf8').match(/^stage:\s*(\S+)/m);
  return m ? m[1] : '';
}

function cmdLayout() {
  const fails = [];
  const oks = [];
  const need = [
    'AGENTS.md',
    'harness/engine/harness.mjs',
    'harness/harness.config.json',
    'harness/rules/must.md',
    'harness/rules/should.md',
    'harness/rules/can.md',
    'harness/memory/long-term.md',
    'harness/logs'
  ];
  for (const rel of need) {
    (fs.existsSync(path.join(ROOT, rel)) ? oks : fails).push(rel);
  }

  const reqRoot = path.join(ROOT, 'harness', 'requirements');
  const reqDirs = fs.existsSync(reqRoot)
    ? fs.readdirSync(reqRoot, { withFileTypes: true }).filter(d => d.isDirectory()).map(d => d.name)
    : [];
  if (!reqDirs.length) fails.push('harness/requirements（没有任何需求包）');

  for (const name of reqDirs) {
    if (!REQ_RE.test(name)) { fails.push(`harness/requirements/${name}（目录名不合规）`); continue; }
    const base = path.join(reqRoot, name);
    for (const f of ['00-requirement.md', '01-design.md', '02-test.md', '03-review.md', '04-release.md', '05-visual.md', 'state.md']) {
      const rel = `harness/requirements/${name}/${f}`;
      (fs.existsSync(path.join(base, f)) ? oks : fails).push(rel);
    }
    // visual 是 UI 需求专用（templates/visual.md「仅 UI 需求填写」），
    // 且 git 不跟踪空目录 —— 已收口的历史包不可能补出这两个目录，一律要求只会误报。
    const stage = readStage(base);
    if (stage !== 'done' && stage !== 'archive') {
      for (const d of ['visual/ref', 'visual/shot']) {
        const rel = `harness/requirements/${name}/${d}`;
        const p = path.join(base, d);
        (fs.existsSync(p) && fs.statSync(p).isDirectory() ? oks : fails).push(rel);
      }
    }
  }

  // 错位检测：根目录或 harness 根下的需求文档
  const misplaced = [];
  const scanTop = (dir, depth) => {
    if (!fs.existsSync(dir) || depth > 2) return;
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name);
      const rel = path.relative(ROOT, p).replace(/\\/g, '/');
      if (e.isDirectory()) {
        // requirements 内是规范位置；templates 是模板目录（state.md 等模板就在里面），都不是错位
        if (e.name === 'requirements' || e.name === 'templates') continue;
        scanTop(p, depth + 1);
        continue;
      }
      if (/^(00|01|02|03|04|05)-.*\.md$/.test(e.name) || e.name === 'state.md') misplaced.push(rel);
      if (/\.(png|jpg|jpeg)$/i.test(e.name) && !/\/visual\/(ref|shot)\//.test('/' + rel)) misplaced.push(rel);
    }
  };
  scanTop(path.join(ROOT, 'harness'), 0);
  for (const e of fs.readdirSync(ROOT, { withFileTypes: true })) {
    if (!e.isFile()) continue;
    if (/^(00|01|02|03|04|05)-.*\.md$/.test(e.name) || e.name === 'state.md') misplaced.push(e.name);
  }

  console.log(`LAYOUT ${ROOT}`);
  for (const p of oks) console.log(`  OK    ${p}`);
  for (const p of fails) console.log(`  FAIL  ${p}`);
  for (const p of misplaced) console.log(`  MOVE  ${p}（放错位置）`);
  const bad = fails.length + misplaced.length;
  console.log(bad ? `\n结论：FAIL（缺失 ${fails.length} / 错位 ${misplaced.length}）` : '\n结论：PASS');
  process.exit(bad ? 2 : 0);
}

// ---------- gate ----------
function resolveReq(arg) {
  const reqRoot = path.join(ROOT, 'harness', 'requirements');
  if (arg) {
    const p = path.join(reqRoot, arg);
    if (!fs.existsSync(p)) die(1, `需求包不存在：harness/requirements/${arg}`);
    return arg;
  }
  const all = fs.existsSync(reqRoot)
    ? fs.readdirSync(reqRoot, { withFileTypes: true }).filter(d => d.isDirectory() && REQ_RE.test(d.name)).map(d => d.name)
    : [];
  if (all.length === 1) return all[0];
  if (!all.length) die(1, '没有任何需求包，先运行 init');
  die(1, `存在多个需求包，请指定：${all.join(' / ')}`);
}

/** 当前活跃需求包。
 *  判定用「最近修改的包」而不是「最近修改的未收口包」—— 后者在收口后会被很久没动的
 *  遗留未收口包抢占（实测 REQ-08 刚收口，begin 却指向三天没动的 REQ-09）。
 *  收口与否只决定标签（当前需求包 / 最近收口），未收口的其它包单独列出来提示。 */
function activeReqs() {
  const reqRoot = path.join(ROOT, 'harness', 'requirements');
  if (!fs.existsSync(reqRoot)) return { active: [], latest: null };
  const info = fs.readdirSync(reqRoot, { withFileTypes: true })
    .filter(d => d.isDirectory() && REQ_RE.test(d.name))
    .map(d => {
      const sf = path.join(reqRoot, d.name, 'state.md');
      const text = fs.existsSync(sf) ? fs.readFileSync(sf, 'utf8') : '';
      const stage = (text.match(/^stage:\s*(.+)$/m) || [, 'unknown'])[1].trim();
      // 只认门禁记录行里的 G4 结论 —— 全文匹配会把正文里提到的 "G4: PASS" 误当已收口
      const g4 = parseGateLine(text, 'G4');
      const closed = (g4.ok && /^PASS$/i.test(g4.conclusion)) || /^(archive|done|released)$/i.test(stage);
      const mtime = fs.existsSync(sf) ? fs.statSync(sf).mtimeMs : 0;
      return { name: d.name, stage, closed, mtime };
    });
  const sorted = [...info].sort((a, b) => b.mtime - a.mtime);
  // 归档包不参与「当前现场」的竞争：归档动作本身会把 mtime 刷新成最新，
  // 但它明确是历史保留，不该让 HANDOFF 指向它（否则每次归档都会把现场拽回老包）。
  const live = sorted.filter(x => !/^archive$/i.test(x.stage));
  return { active: sorted.filter(x => !x.closed), latest: live[0] || sorted[0] || null };
}

function parseGateLine(text, id) {
  // 取最后一条：门禁记录允许追加，不覆盖历史，最新一条才代表当前结论
  const re = new RegExp('^' + id + ':\\s*([^|\\n]+)\\|(.*)$', 'gm');
  let last = null;
  let hit;
  while ((hit = re.exec(text)) !== null) last = hit;
  if (!last) return { ok: false, why: `state.md 缺少 ${id} 机读行` };
  const fields = {};
  for (const seg of last[2].split('|')) {
    const i = seg.indexOf(':');
    if (i > 0) fields[seg.slice(0, i).trim()] = seg.slice(i + 1).trim();
  }
  return { ok: true, conclusion: last[1].trim(), fields };
}

const isPlaceholder = v => !v || /<[^>]*>/.test(v) || /^—+$/.test(v) || /tbd/i.test(v) || /your_/i.test(v);

/** 从审查记录里提炼「这次到底发生了什么」。
 *  只写「通过门禁」等于流水账 —— BRIEF 里十几条一模一样的句子，没人能从里面学到东西。
 *  提取的是审查项数量、致命/建议分布、遗留编号；提不到就退回通用句子，不硬编。 */
function summarizeReview(reqId) {
  const f = path.join(ROOT, 'harness', 'requirements', reqId, '03-review.md');
  if (!fs.existsSync(f)) return null;
  const rows = fs.readFileSync(f, 'utf8').split(/\r?\n/).filter(l => /^\|\s*RV-\d+/.test(l));
  if (!rows.length) return null;
  let fatal = 0, advise = 0, fixed = 0, left = 0, machine = 0;
  const leftIds = [];
  for (const l of rows) {
    const c = l.split('|').map(s => s.trim());
    const id = c[1], sev = c[2], nature = c[3], st = c[9];
    if (/致命/.test(sev)) fatal++; else if (/建议/.test(sev)) advise++;
    if (/已修/.test(st)) fixed++;
    if (/待修|接受/.test(st)) { left++; if (id) leftIds.push(id); }
    if (/可机检/.test(nature)) machine++;
  }
  return { total: rows.length, fatal, advise, fixed, left, machine, leftIds };
}

/** G4 的回流同理：只写「通过发布门禁」没有信息量，写清发布步骤与回退材料的规模。 */
function summarizeRelease(reqId) {
  const f = path.join(ROOT, 'harness', 'requirements', reqId, '04-release.md');
  if (!fs.existsSync(f)) return null;
  const t = fs.readFileSync(f, 'utf8');
  const steps = (t.match(/^\s*\d+\.\s+\S/gm) || []).length;
  const backouts = (t.match(/回退/g) || []).length;
  return { steps, backouts };
}

/** 门禁结论回流：把「审完的结论 + 证据」写进记忆候选池，让系统越用越准。
 *  只在 G3/G4 通过时触发；写的是「事件」类条目（不可直接晋升），
 *  需人工提炼成可复用经验后才参与三条件晋升。任何异常都不影响门禁退出码。 */
function writebackToMemory(stageId, reqId, passed) {
  if (!passed) return;
  if (stageId !== 'G3' && stageId !== 'G4') return;
  try {
    const memScript = findScript('memory.mjs');
    if (!memScript) return;
    const inbox = path.join(ROOT, 'harness', 'memory', 'inbox.md');
    mkdirp(path.dirname(inbox));
    const evidence = stageId === 'G3'
      ? path.join('harness', 'requirements', reqId, '03-review.md')
      : path.join('harness', 'requirements', reqId, '04-release.md');
    let fact;
    if (stageId === 'G3') {
      const s = summarizeReview(reqId);
      fact = s
        ? `${reqId} G3 通过：审查 ${s.total} 项（致命 ${s.fatal} / 建议 ${s.advise}），已修 ${s.fixed}，遗留 ${s.left}`
          + (s.leftIds.length ? `（${s.leftIds.slice(0, 3).join('/')}）` : '')
          + (s.machine ? `；其中可机检 ${s.machine} 项已落成检查` : '')
        : `${reqId} 通过 G3 验证门禁；审查结论与证据已留档，待人工提炼可复用经验`;
    } else {
      const s = summarizeRelease(reqId);
      fact = s && s.steps
        ? `${reqId} G4 通过：发布步骤 ${s.steps} 条、回退相关 ${s.backouts} 处，材料已留档`
        : `${reqId} 通过 G4 发布门禁；发布与回退材料已留档，待人工提炼可复用经验`;
    }
    const scope = reqId;
    const line = [localDate(), '事件', scope, fact, evidence, '仅本需求包', localDate(new Date(Date.now() + 180 * 864e5))].join(' | ');
    // 去重：同一需求包的同一阶段只留一条事件，重复跑门禁不堆积
    const existing = fs.existsSync(inbox) ? fs.readFileSync(inbox, 'utf8').split(/\r?\n/) : [];
    if (existing.some(l => l.includes(scope) && l.includes(fact))) return;
    if (!fs.existsSync(inbox)) fs.writeFileSync(inbox, '# 记忆候选池\n\n> 自动写入只落这里；类别「事件」不可直接晋升，需人工提炼后另写。\n\n', 'utf8');
    fs.appendFileSync(inbox, line + '\n', 'utf8');
    console.log(`\n记忆回流：已写入候选池（${path.relative(ROOT, inbox)}）—— memory.mjs ready 查看待提炼事件`);
  } catch (e) {
    console.log(`\n记忆回流：跳过（${e && e.message ? e.message : e}）—— 不影响门禁结论`);
  }
}

/** 度量采集：门禁跑完就把数记下来。
 *  为什么挂在门禁上而不是定时任务：定时报告没人看，门禁是必经之路。
 *  只记 G2/G3 —— G1 查需求文档没有 AC 结论，G4 不新增 AC，记了就是重复计数。 */
function recordMetrics(stageId, reqId, passed) {
  if (stageId !== 'G2' && stageId !== 'G3') return;
  try {
    const ms = findScript('metrics.mjs');
    if (!ms) return;
    spawnSync(process.execPath, [ms, 'record', reqId, '--stage', stageId, '--result', passed ? 'PASS' : 'FAIL'],
      { cwd: ROOT, encoding: 'utf8', timeout: 20000 });
  } catch { }
}

function emitGate(stageId, title, reqId, checks, hint) {
  // level: 'info' 的检查不参与通过判定，只提示。用于「建议这样做但不强制」的项 ——
  // 比如历史包没做审查分类，不该因为我们后来加了规矩就把已收口的包判红。
  const judged = checks.filter(c => c.level !== 'info');
  const passed = judged.every(c => c.ok);
  const result = passed ? 'PASS' : 'FAIL';
  // 广播到 WebSocket server（端口 8765）——让 3D 大屏实时更新
  try {
    const emitScript = path.join(ROOT, 'harness', 'scripts', 'emit-event.mjs');
    if (fs.existsSync(emitScript)) {
      spawnSync(process.execPath, [emitScript,
        '--type', 'gate.' + result.toLowerCase(), '--req', reqId, '--gate', stageId, '--result', result],
        { cwd: ROOT, encoding: 'utf8', timeout: 3000 });
    }
  } catch { /* server 没跑 → 静默 */ }
  console.log(`门禁：${title}`);
  console.log(`需求：${reqId}`);
  console.log(`结论：${result}\n`);
  for (const c of checks) {
    const tag = c.level === 'info' ? (c.ok ? 'INFO' : 'WARN') : (c.ok ? 'PASS' : 'FAIL');
    console.log(`  ${tag}  ${c.name} — ${c.detail}`);
  }
  if (!passed) console.log(`\n下一步：${hint}`);
  const logDir = path.join(ROOT, 'harness', 'logs');
  mkdirp(logDir);
  fs.appendFileSync(path.join(logDir, 'gate.log'),
    JSON.stringify({ at: stamp(), req: reqId, stage: stageId, result, checks }) + '\n', 'utf8');
  if (!passed) {
    const rep = repeatedFailures(stageId, reqId);
    if (rep.length) {
      console.log(`\n熔断：${rep.join('、')} 已连续 3 次 FAIL。停止原样重试——换清单/换采集方式/回需求，或记 BLOCKED（见 references/failure-classes.md §5）。`);
    }
  }
  writebackToMemory(stageId, reqId, passed);
  recordMetrics(stageId, reqId, passed);
  // 门禁跑完顺手刷新现场：这是「换工具不丢进度」的关键一环，失败只提示、不影响结论
  writeHandoff(reqId);
  process.exit(passed ? 0 : 2);
}

/** 同一 stage 连续 3 次 FAIL 且失败项重合时，返回重合项 */
function repeatedFailures(stageId, reqId) {
  const logFile = path.join(ROOT, 'harness', 'logs', 'gate.log');
  if (!fs.existsSync(logFile)) return [];
  const recs = [];
  for (const line of fs.readFileSync(logFile, 'utf8').trim().split(/\r?\n/).slice(-60)) {
    try {
      const r = JSON.parse(line);
      if (r.req === reqId && r.stage === stageId) recs.push(r);
    } catch { /* 跳过坏行 */ }
  }
  const last = recs.slice(-3);
  if (last.length < 3 || last.some(r => r.result !== 'FAIL')) return [];
  const sets = last.map(r => (r.checks || []).filter(c => !c.ok).map(c => c.name));
  return sets[0].filter(n => sets[1].includes(n) && sets[2].includes(n));
}

function readBody(file) {
  if (!fs.existsSync(file)) return '';
  return fs.readFileSync(file, 'utf8').replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, '');
}

/** 从需求正文提取 AC，忽略仍带占位符的行 */
function collectAcs(body) {
  const acs = new Set();
  for (const line of body.split(/\r?\n/)) {
    if (line.includes('<')) continue;
    for (const hit of line.match(/AC-\d{2,}/g) || []) acs.add(hit);
  }
  return acs;
}

/** 归一化：去空白、小写，用于维度值的集合比对 */
const normVal = s => String(s).replace(/\s+/g, '').toLowerCase();
const hasSource = v => /https?:\/\//i.test(v) || /\.(md|pdf|png|jpe?g|txt|html)\b/i.test(v);

/**
 * 解析覆盖维度声明。
 * 主格式：覆盖维度：端=Web,iOS; 角色=管理员,普通用户; 语言=zh,en
 * 兼容旧格式：端清单：Web, iOS
 * 维度与取值都由需求决定，引擎不预设「三端」之类的固定集合。
 */
function parseScopes(body) {
  const dims = {};
  const line = (body.match(/覆盖维度[：:]\s*(.+)/) || [, ''])[1].trim();
  if (line && !/<[^>]*>/.test(line)) {
    for (const seg of line.split(/[;；]+/)) {
      const i = seg.indexOf('=');
      if (i <= 0) continue;
      const dim = seg.slice(0, i).trim();
      const vals = seg.slice(i + 1).split(/[,，、/|]+/).map(s => s.trim()).filter(Boolean);
      if (dim && vals.length) dims[dim] = vals;
    }
  }
  const ends = (body.match(/端清单[：:]\s*(.+)/) || [, ''])[1].trim();
  if (!dims['端'] && ends && !/<[^>]*>/.test(ends)) {
    const vals = ends.split(/[,，、/|]+/).map(s => s.trim()).filter(Boolean);
    if (vals.length) dims['端'] = vals;
  }
  const values = [];
  for (const [dim, vals] of Object.entries(dims)) for (const v of vals) values.push({ dim, value: v });
  return { dims, values };
}

/** 从一行测试记录里取证据路径：支持「证据：路径」「表格最后一列」与裸路径 */
function evidencePathFromLine(line) {
  let m = line.match(/证据[：:]\s*`?([^\s|`，,]+)`?/);
  if (m) return m[1];
  if (line.trim().startsWith('|')) {
    const cells = line.split('|').slice(1, -1).map(s => s.trim().replace(/`/g, ''));
    const last = [...cells].reverse().find(c => c && c !== '—' && c !== '-' && c !== '无');
    if (last) return last;
  }
  m = line.match(/`?([A-Za-z0-9_][A-Za-z0-9_./\\-]*\.(?:png|jpe?g|txt|log|json|md|html|xml|zip|mp4))`?/i);
  return m ? m[1] : null;
}

/** 证据必须真实存在且非空：空文件、空目录、占位符都不算 */
function evidenceStatus(line) {
  const p = evidencePathFromLine(line);
  if (!p) return { ok: false, why: '缺「证据：<路径>」' };
  if (/<[^>]*>|tbd|your_/i.test(p)) return { ok: false, why: '证据路径仍为占位符' };
  const abs = path.resolve(ROOT, p.replace(/\\/g, '/'));
  if (!fs.existsSync(abs)) return { ok: false, why: `证据不存在：${p}` };
  const st = fs.statSync(abs);
  if (st.isFile() && st.size === 0) return { ok: false, why: `证据为空文件：${p}` };
  if (st.isDirectory() && fs.readdirSync(abs).length === 0) return { ok: false, why: `证据目录为空：${p}` };
  const maxAge = evidenceMaxAgeHours();
  if (maxAge > 0 && Date.now() - st.mtimeMs > maxAge * 3600e3) {
    return { ok: false, why: `证据过期（>${maxAge}h）：${p}` };
  }
  if (st.isFile() && /\.(png|jpe?g|gif|webp)$/i.test(abs)) {
    const fd = fs.openSync(abs, 'r');
    const head = Buffer.alloc(12);
    fs.readSync(fd, head, 0, 12, 0);
    fs.closeSync(fd);
    const png = head.slice(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
    const jpg = head[0] === 0xff && head[1] === 0xd8;
    const gif = head.slice(0, 3).toString() === 'GIF';
    const webp = head.slice(0, 4).toString() === 'RIFF' && head.slice(8, 12).toString() === 'WEBP';
    if (!(png || jpg || gif || webp)) return { ok: false, why: `不是有效图片：${p}` };
  }
  return { ok: true, path: p };
}

/** 探测本机可用的验证通道：浏览器自动化、系统浏览器、微信开发者工具 CLI */
function detectCapabilities(moduleDirs = []) {
  const dirs = [ROOT, ...moduleDirs, process.cwd()].filter(Boolean);
  const deps = {};
  for (const mod of ['playwright', 'playwright-core', 'puppeteer']) {
    for (const dir of dirs) {
      try {
        createRequire(path.join(dir, 'noop.js')).resolve(mod);
        deps[mod] = dir;
        break;
      } catch { /* 下一个目录 */ }
    }
  }
  const browsers = (process.platform === 'win32' ? [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
  ] : process.platform === 'darwin' ? [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'
  ] : [
    '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/microsoft-edge'
  ]).filter(p => fs.existsSync(p));

  const wxCandidates = [];
  if (process.env.WECHATIDE_CLI) wxCandidates.push(process.env.WECHATIDE_CLI);
  if (process.platform === 'win32') {
    const la = process.env.LOCALAPPDATA || '';
    wxCandidates.push(
      'C:\\Program Files (x86)\\Tencent\\微信web开发者工具\\cli.bat',
      'C:\\Program Files\\Tencent\\微信web开发者工具\\cli.bat',
      path.join(la, '微信开发者工具', 'cli.bat'),
      path.join(la, 'Programs', '微信web开发者工具', 'cli.bat')
    );
    const w = spawnSync('where', ['wechatide'], { encoding: 'utf8' });
    if (w.status === 0) wxCandidates.push(...w.stdout.split(/\r?\n/).filter(Boolean));
  } else if (process.platform === 'darwin') {
    wxCandidates.push('/Applications/wechatwebdevtools.app/Contents/MacOS/cli');
  }
  const wxPath = wxCandidates.find(p => p && fs.existsSync(p)) || '';
  return {
    node: process.version,
    deps,
    browsers,
    wechatide: { found: !!wxPath, path: wxPath }
  };
}

/** 把「验证通道：浏览器, 小程序」转成能力判定；未声明返回 null（不检查） */
function channelCheck(reqBody) {
  const line = (reqBody.match(/验证通道[：:]\s*(.+)/) || [, ''])[1].trim();
  if (!line || /<[^>]*>/.test(line)) return null;
  const ALIAS = {
    '浏览器': 'browser', 'browser': 'browser', 'dom': 'browser', 'web': 'browser',
    '小程序': 'wechatide', 'wechatide': 'wechatide', '微信开发者工具': 'wechatide', 'miniapp': 'wechatide',
    '桌面': 'desktop', 'desktop': 'desktop', 'computer-use': 'desktop', 'computeruse': 'desktop'
  };
  const channels = line.split(/[,，、/|]+/).map(s => s.trim()).filter(Boolean)
    .map(s => ALIAS[s.toLowerCase()] || s);
  const caps = detectCapabilities();
  const bad = [];
  for (const ch of channels) {
    if (ch === 'browser' && !Object.keys(caps.deps).length && !caps.browsers.length) {
      bad.push('浏览器（未解析到 playwright/puppeteer，也未找到系统 Chrome/Edge）');
    } else if (ch === 'wechatide' && !caps.wechatide.found) {
      bad.push('小程序（未找到微信开发者工具 CLI：装工具后在设置里开启「命令行」，或设 WECHATIDE_CLI）');
    }
  }
  return {
    name: '验证通道可用',
    ok: bad.length === 0,
    detail: bad.length ? bad.join('；') : `已声明 ${channels.length} 个通道，均探测可用`
  };
}

/** 去掉标签与脚本，只留可见文本 */
function stripHtml(html) {
  return html.replace(/<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style[\s\S]*?<\/style>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ').trim();
}

/** 期望项判定：class:类名 / text:文案 / count:片段>=N / 纯 token（兼容） */
function checkExpectToken(html, token) {
  const t = String(token);
  if (t.startsWith('class:')) {
    const name = t.slice(6).trim();
    const classes = [...html.matchAll(/class\s*=\s*["']([^"']*)["']/gi)]
      .flatMap(m => m[1].split(/\s+/)).filter(Boolean);
    return { ok: classes.includes(name), label: t };
  }
  if (t.startsWith('text:')) {
    const s = t.slice(5);
    return { ok: stripHtml(html).includes(s), label: t };
  }
  const cnt = t.match(/^count:(.+?)(>=|>|=)(\d+)$/);
  if (cnt) {
    const [, needle, op, n] = cnt;
    const times = html.split(needle).length - 1;
    const need = Number(n);
    const ok = op === '>=' ? times >= need : op === '>' ? times > need : times === need;
    return { ok, label: `${t}（实际 ${times}）` };
  }
  return { ok: html.includes(t), label: t };
}

/** 目录或文件的内容指纹，用于清单过期检测 */
function hashPath(p) {
  const h = crypto.createHash('sha1');
  const walk = q => {
    if (fs.statSync(q).isDirectory()) {
      for (const n of fs.readdirSync(q).sort()) walk(path.join(q, n));
    } else {
      h.update(path.basename(q));
      h.update(fs.readFileSync(q));
    }
  };
  walk(p);
  return h.digest('hex');
}

/** 页面级内容校验：DOM 快照 + 期望项 + 空页防线 + 清单锁定。
 *  approve 由调用方传：只有真正跑 `gate V --approve` 才允许写锁文件，
 *  G4 复用本函数时必须传 false，否则发布门禁会顺手改掉 expect.lock.json。 */
function checkPageContent(pages, reqDir, approve = false) {
  const expectPages = pages.filter(p => Array.isArray(p.expect) && p.expect.length);
  if (!expectPages.length) return null;
  const domDir = path.join(reqDir, 'visual', 'dom');
  const lockFile = path.join(ROOT, 'harness', 'expect.lock.json');
  let lock = {};
  if (fs.existsSync(lockFile)) {
    try { lock = JSON.parse(fs.readFileSync(lockFile, 'utf8')); }
    catch { lock = {}; }
  }
  const problems = [];

  for (const page of expectPages) {
    if (page.expectFrom) {
      const src = path.resolve(ROOT, page.expectFrom);
      if (!fs.existsSync(src)) {
        problems.push(`${page.name} 清单源不存在：${page.expectFrom}`);
      } else {
        const sha = hashPath(src);
        const rec = lock[page.name];
        if (approve) {
          lock[page.name] = { file: page.expectFrom, sha1: sha, at: stamp() };
        } else if (!rec) {
          problems.push(`${page.name} 清单未锁定（人工确认后运行 gate V --approve）`);
        } else if (rec.sha1 !== sha) {
          problems.push(`${page.name} 原型已变更，清单需重评（${page.expectFrom}）`);
        }
      }
    }

    const states = Array.isArray(page.states) && page.states.length ? page.states : ['default'];
    const allow = new Set((page.emptyAllowed || []).map(String));
    for (const state of states) {
      const cands = [
        path.join(domDir, `${page.name}-${state}.html`),
        path.join(domDir, `${page.name}-${state}.json`)
      ];
      if (states.length === 1) cands.push(path.join(domDir, `${page.name}.html`));
      const file = cands.find(f => fs.existsSync(f));
      if (!file) { problems.push(`${page.name}/${state} 缺 DOM 快照`); continue; }
      const maxAge = evidenceMaxAgeHours();
      if (maxAge > 0 && Date.now() - fs.statSync(file).mtimeMs > maxAge * 3600e3) {
        problems.push(`${page.name}/${state} DOM 快照过期（>${maxAge}h）`); continue;
      }
      let html = fs.readFileSync(file, 'utf8');
      if (file.endsWith('.json')) {
        try { html = String(JSON.parse(html).html || ''); }
        catch { problems.push(`${page.name}/${state} 快照不是合法 JSON`); continue; }
      }
      if (!html.trim()) { problems.push(`${page.name}/${state} 快照为空`); continue; }
      const visible = stripHtml(html);
      if (state !== 'empty' && visible.length === 0 && !allow.has('*')) {
        problems.push(`${page.name}/${state} 疑似空页（无可见正文）`);
      }
      const bad = page.expect.filter(t => !checkExpectToken(html, t).ok
        && !(state === 'empty' && allow.has(String(t))));
      if (bad.length) problems.push(`${page.name}/${state} 缺：${bad.join('、')}`);
    }
  }

  if (approve) {
    fs.mkdirSync(path.dirname(lockFile), { recursive: true });
    fs.writeFileSync(lockFile, JSON.stringify(lock, null, 2) + '\n', 'utf8');
  }
  return {
    name: `内容校验 ${expectPages.length} 页`,
    ok: problems.length === 0,
    detail: problems.length ? problems.join('；')
      : `每页 DOM 快照均含声明的期望项${approve ? '（清单已锁定）' : ''}`
  };
}

/** 测试场景必须写明：场景、入口/参数、数据准备（防「注入了 token 就说实测通过」） */
function envCheck(testBody) {
  const sec = section(testBody, '环境');
  const need = [
    ['场景', /场景[：:]\s*(.+)/],
    ['入口与参数', /(?:入口与参数|运行方式)[：:]\s*(.+)/],
    ['数据准备', /数据准备[：:]\s*(.+)/]
  ];
  const missing = need.filter(([, re]) => {
    const m = sec.match(re);
    return !m || isPlaceholder(m[m.length - 1].trim());
  }).map(([label]) => label);
  return {
    name: '测试场景已声明',
    ok: missing.length === 0,
    detail: missing.length
      ? `缺/无效：${missing.join('、')}（须写实际入口、未登录/已登录、是否注入 token、是否有版本参数）`
      : '场景、入口/参数、数据准备均已写明'
  };
}

/** 收集 AC 结论：PASS 必须有真实证据；FAIL/BLOCKED 必须写原因 */
function collectAcResults(acs, lines) {
  const out = [];
  for (const ac of acs) {
    const line = lines.find(l => l.includes(ac) && /(PASS|FAIL|BLOCKED)/.test(l));
    if (!line) { out.push({ ac, ok: false, why: '缺结论' }); continue; }
    const conclusion = (line.match(/PASS|FAIL|BLOCKED/) || [''])[0];
    if (conclusion === 'PASS') {
      const ev = evidenceStatus(line);
      out.push({ ac, ok: ev.ok, why: ev.ok ? '' : ev.why, conclusion });
    } else {
      const reason = (line.match(/原因[：:]\s*([^|]+)/) || [, ''])[1].trim();
      const ok = reason.length >= 2 && !/<[^>]*>/.test(reason);
      out.push({ ac, ok, why: ok ? '' : 'FAIL/BLOCKED 必须写「原因：…」', conclusion });
    }
  }
  return out;
}

/** 覆盖值是否在测试文档里逐项留下结论（PASS 需真实证据，FAIL/BLOCKED 需原因） */
function coverageEvidence(reqBody, testBody) {
  const { values } = parseScopes(reqBody);
  const lines = testBody.split(/\r?\n/).filter(l => !l.includes('<'));
  const bad = [];
  for (const v of values) {
    const line = lines.find(l => normVal(l).includes(normVal(v.value)) && /(PASS|FAIL|BLOCKED)/.test(l));
    if (!line) { bad.push(`${v.dim}/${v.value}（缺结论）`); continue; }
    const conclusion = (line.match(/PASS|FAIL|BLOCKED/) || [''])[0];
    if (conclusion === 'PASS') {
      const ev = evidenceStatus(line);
      if (!ev.ok) bad.push(`${v.dim}/${v.value}（${ev.why}）`);
    } else {
      const reason = (line.match(/原因[：:]\s*([^|]+)/) || [, ''])[1].trim();
      if (reason.length < 2 || /<[^>]*>/.test(reason)) bad.push(`${v.dim}/${v.value}（FAIL/BLOCKED 缺原因）`);
    }
  }
  const required = values.length >= 2;
  return {
    name: '覆盖值逐项有证据',
    ok: !required || bad.length === 0,
    detail: !required ? '未声明多值覆盖维度，不适用'
      : bad.length ? bad.join('、')
      : `${values.length} 个覆盖值均有结论与证据`
  };
}

/** 校验机读行字段是否齐全且为 PASS */
function validateGateLine(stateText, id) {
  const line = parseGateLine(stateText, id);
  if (!line.ok) return { ok: false, detail: line.why };
  const f = line.fields;
  const who = f['确认人'] || '';
  const when = f['时间'] || '';
  const words = (f['确认原文'] || '').replace(/^["“]|["”]$/g, '').trim();
  const timeOk = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(when) && !Number.isNaN(Date.parse(when.replace(' ', 'T')));
  const ok = line.conclusion === 'PASS' && !isPlaceholder(who) && timeOk && !isPlaceholder(words);
  return {
    ok,
    detail: ok ? `${who} ${when}：“${words}”` :
      `结论=${line.conclusion}；确认人=${who || '空'}；时间=${when || '空'}；原文=${words || '空'}`
  };
}

function cmdGate() {
  const stage = opt('--stage', 'G1');
  const reqId = resolveReq(positionals()[0] || opt('--req', null));
  if (stage === 'G2') return gateG2(reqId);
  if (stage === 'G3') return gateG3(reqId);
  if (stage === 'G4') return gateG4(reqId);
  if (stage === 'V') return gateV(reqId);
  if (stage !== 'G1') die(3, `${stage} 尚未实现（当前做 G1 / G2 / G3 / G4 / V）。`);

  const reqDir = path.join(ROOT, 'harness', 'requirements', reqId);
  const reqFile = path.join(reqDir, '00-requirement.md');
  const stateFile = path.join(reqDir, 'state.md');
  const checks = [];

  const add = (name, ok, detail) => checks.push({ name, ok, detail });

  add('需求文档存在', fs.existsSync(reqFile), fs.existsSync(reqFile) ? '00-requirement.md' : '缺 00-requirement.md');
  const body = readBody(reqFile);

  // 验收点：忽略仍带占位符的行
  const acs = collectAcs(body);
  add('验收点 ≥1', acs.size >= 1, `找到 ${acs.size} 个 AC`);

  // 代码证据：path:line，必须真实存在于项目，且不能指向 harness/
  const refs = [];
  const re = /`?([A-Za-z0-9_][A-Za-z0-9_./\\-]*\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?`?/g;
  let m;
  while ((m = re.exec(body)) !== null) {
    const p = m[1].replace(/\\/g, '/');
    if (p.startsWith('harness/')) continue;
    if (isPlaceholder(p)) continue;
    refs.push({ raw: m[0], path: p, exists: fs.existsSync(path.join(ROOT, p)) });
  }
  const validRefs = refs.filter(r => r.exists);
  add('代码证据 ≥1 条且路径存在', validRefs.length >= 1,
    refs.length ? `有效 ${validRefs.length} / 共 ${refs.length}；无效：${refs.filter(r => !r.exists).map(r => r.path).join('、') || '无'}`
                : '未找到 path:line，或路径指向 harness/');

  // 边界：只看含「边界/反例」且不是占位符的行
  const cats = new Set();
  for (const line of body.split(/\r?\n/)) {
    if (!/边界|反例/.test(line) || /<TBD>/i.test(line)) continue;
    for (const c of ['空', '重复', '无权限', '超时']) if (line.includes(c)) cats.add(c);
  }
  add('边界反例 ≥2 类', cats.size >= 2, `覆盖：${[...cats].join('、') || '无'}`);

  // 澄清清单：六类必填；不适用必须带理由
  const REQUIRED_CLARIFY = ['触发与时序', '数据来源与精度', '隐私与授权', '离线与失败', '存储与留存', '谁可见'];
  const clarifySection = (body.match(/##[^\n]*澄清清单[^\n]*\n([\s\S]*?)(?=\n## |$)/) || [, ''])[1];
  const clarify = {};
  for (const line of clarifySection.split(/\r?\n/)) {
    if (!line.trim().startsWith('|')) continue;
    const cells = line.split('|').slice(1, -1).map(s => s.trim().replace(/^\*\*|\*\*$/g, ''));
    if (cells.length < 2) continue;
    if (/^[-: ]+$/.test(cells[0]) || cells[0].includes('类别')) continue;
    clarify[cells[0]] = cells[1];
  }
  const missingClarify = REQUIRED_CLARIFY.filter(k => {
    const v = clarify[k] || '';
    if (isPlaceholder(v)) return true;
    if (/不适用/.test(v)) return v.replace(/不适用/g, '').replace(/[：:，,。.\s]/g, '').length < 2;
    return false;
  });
  add('澄清清单六类齐全', missingClarify.length === 0,
    missingClarify.length ? `缺/无效：${missingClarify.join('、')}` : '六类均有答复或注明不适用理由');

  // 不做项
  const noDo = (body.match(/不做项[：:]\s*(.+)/) || [, ''])[1].trim();
  add('不做项已写', !isPlaceholder(noDo), noDo || '缺「不做项」');

  // 需求分析与同行对标：每条 R-xx 都要有可追溯的对标行，且来源至少 2 个
  const Rs = new Set();
  for (const line of body.split(/\r?\n/)) {
    if (line.includes('<')) continue;
    for (const hit of line.match(/\bR-\d{2,}\b/g) || []) Rs.add(hit);
  }
  const benchSection = (body.match(/##[^\n]*对标[^\n]*\n([\s\S]*?)(?=\n## |$)/) || [, ''])[1];
  const bench = {};
  for (const line of benchSection.split(/\r?\n/)) {
    if (!line.trim().startsWith('|')) continue;
    const cells = line.split('|').slice(1, -1).map(s => s.trim().replace(/^\*\*|\*\*$/g, ''));
    if (cells.length < 5) continue;
    if (/^[-: ]+$/.test(cells[0]) || cells[0].includes('需求')) continue;
    const key = (cells[0].match(/\bR-\d{2,}\b/) || [])[0];
    if (key) (bench[key] = bench[key] || []).push(cells);
  }
  const benchRowOk = c => {
    const source = c[2] || '';
    return !isPlaceholder(c[1] || '') && hasSource(source) && !isPlaceholder(c[4] || '');
  };
  // 一条需求可以有多行对标（同类 + 同行），任何一行不完整都算缺陷
  const benchBad = [...Rs].filter(r => {
    const rows = bench[r];
    return !rows || !rows.length || rows.some(c => !benchRowOk(c));
  });
  const benchSources = new Set(Object.values(bench).flat()
    .map(c => (c[2] || '').trim())
    .filter(s => !isPlaceholder(s)));
  add('每条需求有同行对标', Rs.size > 0 && benchBad.length === 0 && benchSources.size >= 2,
    Rs.size === 0 ? '未找到 R-xx 需求条目' :
      benchBad.length ? `缺对标行，或来源/取舍不可追溯：${benchBad.join('、')}（来源须为 URL 或本地证据文件）` :
      benchSources.size < 2 ? `对标来源仅 ${benchSources.size} 个，至少需 2 个不同来源（同类 + 同行）` :
      `${Rs.size} 条需求均有对标，来源 ${benchSources.size} 个`);

  // 覆盖维度：先声明「维度=取值」，再逐值交付。端只是其中一种维度，数量由需求决定。
  const { dims, values: scopeValues } = parseScopes(body);
  const declaredSet = new Set(scopeValues.map(v => `${normVal(v.dim)}/${normVal(v.value)}`));
  // 判定只看需求本体：剥掉「对标」与「矩阵」章节，避免参考了 Web 版就被判成多端
  const bodyMain = body
    .replace(/##[^\n]*对标[\s\S]*?(?=\n## |$)/g, '')
    .replace(/##[^\n]*矩阵[\s\S]*?(?=\n## |$)/g, '');
  const PLATFORM_RE = /Web|iOS|Android|鸿蒙|Harmony|小程序|H5|Pad|iPad|桌面|macOS|Windows|TV|电视|watch|手表|公众号|快应用|车机/gi;
  const mentioned = new Set((bodyMain.match(PLATFORM_RE) || []).map(normVal));
  const saysMulti = /三端|多端|跨端|多端一致|三端一致|多角色|多语言|多环境/.test(bodyMain);
  const isMulti = declaredSet.size >= 2 || saysMulti || mentioned.size >= 2;
  // 只认「验收点汇总」里登记过的 AC，避免在矩阵里新造 AC 绕过
  const acSection = (body.match(/##[^\n]*验收点[^\n]*\n([\s\S]*?)(?=\n## |$)/) || [, ''])[1];
  const declaredAcs = new Set();
  for (const line of acSection.split(/\r?\n/)) {
    if (line.includes('<')) continue;
    for (const hit of line.match(/AC-\d{2,}/g) || []) declaredAcs.add(hit);
  }
  if (!declaredAcs.size) for (const a of acs) declaredAcs.add(a);

  const coverSection = (body.match(/##[^\n]*矩阵[^\n]*\n([\s\S]*?)(?=\n## |$)/) || [, ''])[1];
  const coverRows = [];
  for (const line of coverSection.split(/\r?\n/)) {
    if (!line.trim().startsWith('|')) continue;
    const cells = line.split('|').slice(1, -1).map(s => s.trim().replace(/^\*\*|\*\*$/g, ''));
    if (cells.length < 4) continue;
    if (/^[-: ]+$/.test(cells[0]) || /^(端|平台|端类型|目标端|维度\/取值|维度)$/.test(cells[0])) continue;
    coverRows.push(cells);
  }
  const rowKeys = [];
  for (const c of coverRows) {
    let dim = '', val = c[0];
    if (c[0].includes('/')) {
      const parts = c[0].split('/');
      dim = parts[0].trim();
      val = parts.slice(1).join('/').trim();
    } else {
      dim = Object.keys(dims).find(d => dims[d].some(x => normVal(x) === normVal(c[0]))) || '';
    }
    rowKeys.push(`${normVal(dim || '?')}/${normVal(val)}`);
  }
  const missingValues = scopeValues
    .filter(v => !rowKeys.includes(`${normVal(v.dim)}/${normVal(v.value)}`))
    .map(v => `${v.dim}/${v.value}`);
  const unknownValues = rowKeys.filter(k => !declaredSet.has(k) && !k.startsWith('?/'));
  const coverBad = coverRows.filter(c => {
    const ac = (c[2] || '').match(/AC-\d{2,}/);
    return isPlaceholder(c[0]) || isPlaceholder(c[1] || '') || !ac || !declaredAcs.has(ac[0]) || !hasSource(c[3] || '');
  });
  let coverDetail;
  if (!isMulti) coverDetail = '非多值需求，不适用';
  else if (declaredSet.size < 2) coverDetail = '识别为多值需求，但缺「覆盖维度：维度=取值; …」声明（先问人确认要交付哪些端/角色/语言，不得默认只做第一个）';
  else if (missingValues.length) coverDetail = `覆盖矩阵缺值：${missingValues.join('、')}（声明共 ${declaredSet.size} 个）`;
  else if (unknownValues.length) coverDetail = `覆盖矩阵出现未声明的值：${unknownValues.join('、')}`;
  else if (coverBad.length) coverDetail = `覆盖矩阵行不完整（维度/取值范围/验收点/对标基线）：${coverBad.map(c => c[0] || '空行').join('、')}`;
  else coverDetail = `${declaredSet.size} 个覆盖值与声明一致，逐项有验收点与对标基线`;
  add('覆盖维度与矩阵一致',
    !isMulti || (declaredSet.size >= 2 && missingValues.length === 0 && unknownValues.length === 0 && coverBad.length === 0),
    coverDetail);

  // 设计影响面
  const designFile = path.join(reqDir, '01-design.md');
  const designText = fs.existsSync(designFile) ? fs.readFileSync(designFile, 'utf8') : '';
  const impact = (designText.match(/影响面[^\n]*\n+([^\n]+)/) || [, ''])[1].trim();
  add('方案影响面已写', !isPlaceholder(impact), impact || '01-design.md 缺影响面内容');

  // 功能设计卡：本节由模板自带，不得删。纯后端/纯工具需求保留「非功能类：理由」；
  // 功能类需求把六问填上并删掉「非功能类」行。两种写法都算通过，但写不全的卡会被拦下。
  {
    const cardSec = section(body, '功能设计卡');
    // 说明行（> 引用）是模板自带的写法提示，不是填写内容 ——
    // 原来在整段里找「非功能类：」，会命中文档里那句示例，把「没填」当成「填了」
    const cardBody = cardSec.split(/\r?\n/).filter(l => !/^\s*>/.test(l)).join('\n');
    const FIELDS = ['用户旅程位置', '行业基线', '我们的现状与差距', '决策点与后果', '不做什么', '交付形态'];
    if (!cardSec.trim()) {
      add('功能设计卡', false, '缺「功能设计卡」章节 —— 模板自带，纯后端需求保留「非功能类：理由」，功能类需求填满六问');
    } else if (/非功能类[：:]/.test(cardBody)) {
      const reason = (cardBody.match(/非功能类[：:]\s*([^\n]*)/) || [, ''])[1].trim().replace(/^[`'"]+|[`'"。．.]+$/g, '');
      // 判据与提示语必须一致：原来 ok=false 时仍然打印「理由已写」，读起来像通过（踩过）
      const okReason = reason.length >= 4 && !isPlaceholder(reason);
      add('功能设计卡', okReason,
        okReason ? `非功能类需求，理由已写：${reason.slice(0, 40)}`
          : (reason ? `非功能类理由是占位符（${reason.slice(0, 20)}）—— 要写清为什么不是功能类`
            : '非功能类需求必须写清理由（至少 4 个字）'));
      // 需求正文出现界面/流程/用户类字眼，却声明「非功能类」—— 提醒，不阻断（工具类改动也会提到界面）
      const needSix = /(用户旅程|界面|页面|按钮|交互|入口|流程|表单|弹窗|导航|列表)/;
      if (okReason && needSix.test(body)) {
        checks.push({ name: '功能设计卡（可疑分类）', ok: false, level: 'info',
          detail: '声明了非功能类，但需求里出现界面/流程类字眼 —— 若确实改的是用户能看到的东西，六问不能省' });
      }
    } else {
      // 只查「字段名在不在」等于没查 —— 模板原样（值全是 <…>）也能过。
      // 现在每个字段必须有实际答案：去掉占位符与标点后至少 2 个字。
      const missing = [], thin = [];
      for (const f of FIELDS) {
        const m = cardBody.match(new RegExp(f + '[^：:\\n]{0,6}[：:]\\s*([^\\n]*)'));
        if (!m) { missing.push(f); continue; }
        const raw = m[1];
        const val = raw.replace(/<[^>]*>/g, '').replace(/[…\.]{2,}/g, '').trim();
        // 还留着 <…> 或省略号 = 模板骨架没换掉（模板里每问都带「各是什么」这类提示语，
        // 只看字数会被这些提示语骗过去）
        if (/[<]|…/.test(raw) || val.replace(/[，,、。;；:：\s\-—()（）]/g, '').length < 6) thin.push(f);
      }
      if (missing.length) add('功能设计卡', false, `功能类需求，卡缺字段：${missing.join('、')}`);
      else if (thin.length) add('功能设计卡', false, `功能类需求，这几问还是空的/占位符：${thin.join('、')} —— 六问是设计前的判断，不是标题`);
      else {
        add('功能设计卡', true, `功能类需求，卡六问齐全（${FIELDS.length} 问）`);
        // 填了不等于答得对。四条结构判据，都可机检：
        // 基线要有证据锚、现状要有代码定位、旅程要覆盖状态、决策要写代价。
        // 语义质量仍留给 G3 独立审查，门禁只认能复现的结构。
        // **逐卡判**：一个需求可能有多张卡，只验第一张等于放过后面的卡（审查指出）。
        const cardBlocks = cardSec.split(/\n(?=### )/).filter(b => /^### /m.test(b.trim()));
        const blocks = cardBlocks.length ? cardBlocks : [cardSec];
        const q = [];
        // 本地证据锚要真的存在：只认扩展名会让虚构的 docs/nope.md 也过。
        // 同文档章节引用（「证据见第 8 节」）也认，但要校验该章节真的存在。
        const anchorOk = s => {
          if (/https?:\/\//i.test(s)) return true;
          const m = s.match(/[\w./\\-]+\.(md|html|pdf|txt)/i);
          if (m) {
            const p = path.isAbsolute(m[0]) ? m[0] : path.join(ROOT, m[0]);
            if (fs.existsSync(p)) return true;
          }
          const sec = s.match(/(?:见|参见|详见)\s*(?:第|§)\s*(\d+)\s*[节章]?/);
          if (sec) return new RegExp(`^##\\s*${sec[1]}\\.`, 'm').test(body);
          return false;
        };
        blocks.forEach((blk, i) => {
          const blkBody = blk.split(/\r?\n/).filter(l => !/^\s*>/.test(l)).join('\n');
          const valOf = f => {
            const m = blkBody.match(new RegExp(f + '[^：:\\n]{0,6}[：:]\\s*([^\\n]*)'));
            return m ? m[1].trim() : '';
          };
          const p = [];
          if (!anchorOk(valOf('行业基线'))) {
            p.push('行业基线缺证据锚（URL / 真实存在的本地 md / 「见第 N 节」且该节存在）');
          }
          const cur = valOf('我们的现状与差距');
          // 支持 path:line，也支持「第 42 行」这种自然写法（需求 AC-02 明写两种都算）
          if (!/[\w./\\-]+:\d+/.test(cur) && !/第\s*\d+\s*行/.test(cur) && !/(无现状|新功能|全新)/.test(cur)) {
            p.push('现状与差距缺代码定位（path:line 或「第 N 行」），纯新功能请写「无现状：…」');
          }
          const journey = valOf('用户旅程位置');
          // 按「态」归组并收同义词：用户常写「首屏 / 无数据 / 请求中 / 出错 / 完成」，
          // 只认模板原词会把这些正常写法误杀。命中任一别名即算覆盖该态。
          const STATE_GROUPS = [
            ['默认', ['默认', '首屏', '初始']],
            ['空', ['空', '无数据', '没有数据', '暂无']],
            ['加载', ['加载', '请求中', '读取中', 'loading']],
            ['失败', ['失败', '出错', '错误', '异常']],
            ['成功', ['成功', '完成', '已保存']],
            ['上一', ['上一', '上一步']],
            ['下一', ['下一', '下一步']]
          ];
          const lowJourney = journey.toLowerCase();
          const hitStates = STATE_GROUPS
            .filter(([, alts]) => alts.some(a => lowJourney.includes(a.toLowerCase())))
            .map(([n]) => n);
          if (hitStates.length < 3) {
            p.push(`用户旅程只覆盖 ${hitStates.length} 个状态（至少 3 个：默认/首屏、空/无数据、加载/请求中、失败/出错、成功/完成、上一、下一）`);
          }
          const decision = valOf('决策点与后果');
          const costIdx = decision.search(/代价|取舍|成本|风险/);
          if (costIdx < 0) {
            p.push('决策点与后果没写代价 / 取舍 / 成本 / 风险');
          } else if (decision.slice(costIdx + 2).replace(/[，,、。;；:：\s\-—()（）]/g, '').length < 2) {
            p.push('决策点写了代价但没有实际内容（如「代价：代价」）');
          }
          // 交付形态要写清产物类型：只写「到时候看」等于没定交付物
          const FORM_WORDS = ['原型', '截图', '命令行', '文本', '文件', '无视觉', '页面', '大屏', '界面', 'HTML', 'H5', '小程序', 'Web'];
          if (!FORM_WORDS.some(w => valOf('交付形态').includes(w))) {
            p.push('交付形态要写清产物类型（原型 / 截图 / 命令行 / 文件 / 无视觉 等）');
          }
          // 不做什么不能只写「无」：要写清排除项与原因
          const notDo = valOf('不做什么');
          if (!/(不做|排除|不纳入|不考虑|不实现)/.test(notDo)) {
            p.push('不做什么要用「不做 / 排除 / 不纳入」写清排除项');
          } else if (notDo.replace(/[，,、。;；:：\s\-—()（）]/g, '').length < 8) {
            p.push('不做什么太短，要写清排除什么、为什么');
          }
          // 现状与差距要落到用户感知：只写代码现状，不知道痛不痛
          const cur2 = valOf('我们的现状与差距');
          if (!/(用户感知|痛点|影响用户|体验)/.test(cur2) && !/(无用户感知|不影响用户)/.test(cur2)) {
            p.push('现状与差距要写「用户感知 / 痛点」，或明确「无用户感知影响」');
          }
          if (p.length) q.push(`${blocks.length > 1 ? `卡 ${i + 1}：` : ''}${p.join('、')}`);
        });
        add('功能设计卡（判据质量）', q.length === 0,
          q.length ? q.join('；') : '六问都有可核对的锚：证据链接、代码定位、≥3 状态、代价说明');
      }
    }
  }

  // G1 机读行
  const stateText = fs.existsSync(stateFile) ? fs.readFileSync(stateFile, 'utf8') : '';
  const g1 = validateGateLine(stateText, 'G1');
  add('G1 人确认字段', g1.ok, g1.detail);

  emitGate('G1', 'G1 立项门禁', reqId, checks, '补齐 FAIL 项后重跑；G1 的 PASS 必须由人给出确认原文，引擎不代签。');
}

/** G2 开发完成门禁：每个 AC 有自测结论 + G2 人确认 */
function gateG2(reqId) {
  const reqDir = path.join(ROOT, 'harness', 'requirements', reqId);
  const reqFile = path.join(reqDir, '00-requirement.md');
  const testFile = path.join(reqDir, '02-test.md');
  const stateFile = path.join(reqDir, 'state.md');
  if (!fs.existsSync(reqFile) || !fs.existsSync(testFile)) {
    die(3, '缺 00-requirement.md 或 02-test.md，G2 无法判定');
  }

  const acs = collectAcs(readBody(reqFile));
  const testBody = readBody(testFile);
  const testLines = testBody.split(/\r?\n/).filter(l => !l.includes('<'));
  const acBad = collectAcResults(acs, testLines).filter(r => !r.ok);
  const stateText = fs.existsSync(stateFile) ? fs.readFileSync(stateFile, 'utf8') : '';
  const g2 = validateGateLine(stateText, 'G2');
  const ch = channelCheck(readBody(reqFile));

  const checks = [
    {
      name: '验收点有结论与证据',
      ok: acs.size > 0 && acBad.length === 0,
      detail: acs.size === 0 ? '00-requirement.md 没有可判定 AC'
        : acBad.length ? acBad.map(r => `${r.ac}（${r.why}）`).join('、') : `${acs.size} 个 AC 均有结论与证据`
    },
    coverageEvidence(readBody(reqFile), testBody),
    envCheck(testBody),
    ...(ch ? [ch] : []),
    { name: 'G2 人确认字段', ok: g2.ok, detail: g2.detail }
  ];
  emitGate('G2', 'G2 开发完成门禁', reqId, checks, '为每个 AC 与覆盖值补结论与真实证据路径（PASS 证据须存在且非空；FAIL/BLOCKED 写原因），并由人给出 G2 确认原文后重跑。');
}

/** G3 验证门禁：AC 结论齐全 + 审查阻断为 0 + G3 人确认 */
/**
 * 踩过的坑必须变成永久检查 —— 否则下次同类问题还会漏。
 * 2026-11-21 那次包目录积 25MB 垃圾，自检一直在报（info 级），报了几十次没人动手。
 *
 * 判据分两级：
 * - 记录里标了「可机检」→ 每项都要有「已补检查：<名字>」，且名字在 selfcheck 里真实存在（否则不通过）
 * - 整份记录没分类（REQ-01~07 那批）→ 只提示，不破坏已收口的包
 */
function machineCheck(reviewBody, reviewFile) {
  const lines = reviewBody.split(/\r?\n/);
  // 只认表格行（以 | 开头），且含「可机检」或「需人判断」
  // 只看「性质」列（第 3 列）—— 整行扫会把问题描述里提到的「可机检」也算进来
  const isTable = l => /^\s*\|/.test(l);
  const natureOf = l => {
    const c = l.split('|').slice(1, -1).map(x => x.trim().replace(/[`*]/g, ''));
    return c[2] || '';
  };
  const rows = lines.filter(l => isTable(l) && /(可机检|需人判断)/.test(natureOf(l)));
  const machineRows = rows.filter(l => /可机检/.test(natureOf(l)));

  // 检查项名清单：从 selfcheck --json 拿，拿不到就降级
  let names = null;
  let degraded = false;
  const sc = path.join(ROOT, 'harness', 'scripts', 'selfcheck.mjs');
  if (fs.existsSync(sc)) {
    try {
      const r = spawnSync(process.execPath, [sc, '--json'], { cwd: ROOT, encoding: 'utf8', timeout: 60000 });
      const j = JSON.parse((r.stdout || '').trim());
      if (Array.isArray(j.results)) names = new Set(j.results.map(x => x.name));
    } catch { /* 降级处理 */ }
  }
  if (!names) degraded = true;

  // 检查一：有没有分类（提示级）
  const classified = { name: '审查项已分类', level: 'info' };
  if (!rows.length) {
    classified.ok = false;
    classified.detail = '本轮审查未分类（建议按模板补「性质」与「已补检查」两列 —— 坑不落成检查，下次还会踩）';
  } else {
    classified.ok = true;
    classified.detail = `${rows.length} 项已分类（可机检 ${machineRows.length} / 需人判断 ${rows.length - machineRows.length}）`;
  }

  // 检查二：可机检项是否都落成了真实检查（阻断级）
  const missing = [], fake = [];
  for (const row of machineRows) {
    // 只认列位置，不用前缀匹配 —— 否则「改法」列里举例写的「已补检查：xxx」会被吃进来。
    const cells = row.split('|').slice(1, -1).map(x => x.trim().replace(/[`*]/g, ''));
    const raw = (cells[7] || '').trim();
    const id = (cells[0] || row.slice(0, 12)).trim();
    if (!raw || raw === '—' || raw === '-' || /^<.*>$/.test(raw)) { missing.push(id); continue; }
    // 「已补检查」可能写多个名字（顿号/逗号分隔）
    const want = raw.split(/[、,，]/).map(x => x.trim()).filter(Boolean);
    const bad = want.filter(w => names && !names.has(w));
    if (bad.length) fake.push(`${id}（${bad.join('、')}）`);
  }
  const landed = {
    name: '可机检项已落成检查',
    ok: machineRows.length === 0 ? true : (missing.length === 0 && fake.length === 0),
    detail: machineRows.length === 0
      ? (rows.length ? '本轮无可机检项' : '未分类，跳过（补上「性质」列后此项才会生效）')
      : missing.length ? `缺「已补检查：<名字>」：${missing.join('、')}`
      : fake.length ? `检查项名在 selfcheck 里不存在：${fake.join('、')}`
      : degraded ? `${machineRows.length} 项已落地（**降级**：selfcheck --json 不可用，只校验了名字非空）`
      : `${machineRows.length} 项已落地，名字与 selfcheck 检查项一致`
  };

  return [classified, landed];
}

function gateG3(reqId) {
  const reqDir = path.join(ROOT, 'harness', 'requirements', reqId);
  const reqFile = path.join(reqDir, '00-requirement.md');
  const testFile = path.join(reqDir, '02-test.md');
  const reviewFile = path.join(reqDir, '03-review.md');
  const stateFile = path.join(reqDir, 'state.md');
  if (!fs.existsSync(reqFile) || !fs.existsSync(testFile) || !fs.existsSync(reviewFile)) {
    die(3, '缺 00-requirement.md / 02-test.md / 03-review.md，G3 无法判定');
  }

  const acs = collectAcs(readBody(reqFile));
  const testBody = readBody(testFile);
  const testLines = testBody.split(/\r?\n/).filter(l => !l.includes('<'));
  const acBad = collectAcResults(acs, testLines).filter(r => !r.ok);
  const blocking = readBody(reviewFile).split(/\r?\n/)
    // 只认「严重度列 = 阻断 且 状态列 ≠ 已修」的行。
    // 扫全文必然误伤：一句「问题 4 项（阻断 2 / 建议 2），全部已修」、
    // 或者「结论 PASS + WARN，不阻断」，都会被当成阻断记录。这坑踩了五次，改成按列判。
    .filter(l => /^\s*\|/.test(l))
    .filter(l => {
      const c = l.split('|').slice(1, -1).map(x => x.trim().replace(/[`*]/g, ''));
      const sev = (c[1] || '').trim();
      const status = (c[c.length - 1] || '').trim();
      return /^阻断$/.test(sev) && !/^已修$/.test(status);
    });
  const stateText = fs.existsSync(stateFile) ? fs.readFileSync(stateFile, 'utf8') : '';
  const g3 = validateGateLine(stateText, 'G3');
  const ch = channelCheck(readBody(reqFile));

  const checks = [
    {
      name: '验收点有结论与证据',
      ok: acs.size > 0 && acBad.length === 0,
      detail: acs.size === 0 ? '00-requirement.md 没有可判定 AC'
        : acBad.length ? acBad.map(r => `${r.ac}（${r.why}）`).join('、') : `${acs.size} 个 AC 均有结论与证据`
    },
    coverageEvidence(readBody(reqFile), testBody),
    envCheck(testBody),
    ...(ch ? [ch] : []),
    {
      name: '审查阻断为 0',
      ok: blocking.length === 0,
      detail: blocking.length ? `存在阻断记录：${blocking[0].trim()}` : '无阻断记录'
    },
    ...machineCheck(readBody(reviewFile), reviewFile),
    { name: 'G3 人确认字段', ok: g3.ok, detail: g3.detail }
  ];
  emitGate('G3', 'G3 验证门禁', reqId, checks, '补齐 AC 与覆盖值的结论与真实证据、清空阻断项，并由人给出 G3 确认原文后重跑。');
}

/** UI 需求判定：功能设计卡的「交付形态」写了可视产物，且没声明「非功能类」。
 *  用途只有一个 —— UI 需求的 G4 必须连带 V 视觉验收，避免「没冻结参考、没截图」照样收口。 */
function isUiRequirement(reqDir) {
  const f = path.join(reqDir, '00-requirement.md');
  if (!fs.existsSync(f)) return false;
  const body = readBody(f);
  const card = section(body, '功能设计卡');
  // 只认行首声明，且排除引用块：模板说明行（> 非功能类写一行理由即可…）就在卡内，
  // 扫整张卡会让「保留模板说明」的新建包被误判成非功能类，结果 G4 静默跳过 V。
  if (!card || /^(?!\s*>)\s*非功能类[：:]/m.test(card)) return false;
  const form = (card.match(/交付形态\**\s*[：:]\s*([^\n]*)/) || [, ''])[1];
  // 只有整行就是 <占位符> 才算没填；交付形态里写「截图命名 <page>-<state>.png」是正常内容，
  // 用 isPlaceholder 会把它当占位而漏判 UI。
  if (!form || /^\s*<.*>\s*$/.test(form)) return false;
  return /页面|大屏|看板|可视化|图表|界面|Web|HTML|H5|小程序|前端|原型|截图/i.test(form) && !/无视觉/.test(form);
}

/** G4 发布门禁：发布步骤、回退与验收材料齐全 + G4 人确认 + （UI 需求）V 视觉验收 */
function gateG4(reqId) {
  const reqDir = path.join(ROOT, 'harness', 'requirements', reqId);
  const releaseFile = path.join(reqDir, '04-release.md');
  const stateFile = path.join(reqDir, 'state.md');
  if (!fs.existsSync(releaseFile)) die(3, '缺 04-release.md，G4 无法判定');

  const body = readBody(releaseFile);
  const hasContent = text => text && !/<[^>]*>/.test(text) && text.replace(/[\s`|#*\-]/g, '').length > 10;
  const publishOk = hasContent(section(body, '发布步骤'));
  const rollbackText = section(body, '回退');
  const trigger = (rollbackText.match(/触发条件[：:]\s*(.+)/) || [, ''])[1].trim();
  const rollbackOk = !isPlaceholder(trigger) && hasContent(rollbackText);
  const acceptText = section(body, '验收');
  const who = (acceptText.match(/验收人[：:]\s*(.+)/) || [, ''])[1].trim();
  const conclusion = (acceptText.match(/结论[：:]\s*(.+)/) || [, ''])[1].trim();
  const acceptOk = !isPlaceholder(who) && !isPlaceholder(conclusion);

  const stateText = fs.existsSync(stateFile) ? fs.readFileSync(stateFile, 'utf8') : '';
  const g4 = validateGateLine(stateText, 'G4');
  const checks = [
    { name: '发布步骤可照做', ok: publishOk, detail: publishOk ? '已填写' : '缺发布步骤或仍为占位' },
    { name: '回退材料齐全', ok: rollbackOk, detail: rollbackOk ? `触发条件：${trigger}` : '缺触发条件或回退步骤仍为占位' },
    { name: '验收人信息齐全', ok: acceptOk, detail: acceptOk ? `${who}：${conclusion}` : '缺验收人或结论' },
    { name: 'G4 人确认字段', ok: g4.ok, detail: g4.detail }
  ];
  // UI 需求必须连带 V 视觉验收 —— 参考图没冻结、截图没比对，不许宣布发布完成
  if (isUiRequirement(reqDir)) {
    const bad = vGateChecks(reqId).filter(c => c.level !== 'info' && !c.ok);
    checks.push({
      name: 'UI 需求已过 V 视觉验收',
      ok: bad.length === 0,
      detail: bad.length
        ? `V 未通过：${bad.map(c => c.name).join('、')} —— 先跑 gate ${reqId} --stage V，冻结参考图并补截图`
        : '参考已冻结、截图矩阵齐全、V 人确认通过'
    });
  }
  emitGate('G4', 'G4 发布门禁', reqId, checks, '补齐发布/回退/验收材料，必要时先过 V 视觉验收，并由人给出 G4 确认原文后重跑。');
}

/** V 门禁检查项：参考冻结 + 截图矩阵 + 覆盖/状态对齐 + V 人确认。
 *  抽出来给 gate V 与 G4 复用 —— UI 需求不许绕过视觉验收。 */
function vGateChecks(reqId, opts = {}) {
  // 只有 gate V 显式 --approve 才锁定清单；G4 调用时默认 false，避免发布门禁产生写副作用
  const approve = opts.approve === true;
  const checks = [];
  const reqDir = path.join(ROOT, 'harness', 'requirements', reqId);
  const stateFile = path.join(reqDir, 'state.md');
  const stateText = fs.existsSync(stateFile) ? fs.readFileSync(stateFile, 'utf8') : '';
  const cfgFile = path.join(ROOT, 'harness', 'harness.config.json');
  // 按需求包矩阵优先：包内 visual/config.json 合法且结构完整就用它，否则整份回退全局配置
  const localCfgFile = path.join(reqDir, 'visual', 'config.json');
  const readCfg = f => {
    try { return { ok: true, value: JSON.parse(fs.readFileSync(f, 'utf8')) }; }
    catch (e) { return { ok: false, why: e.message }; }
  };
  const usable = c => c && Array.isArray(c.pages) && c.pages.length && Array.isArray(c.viewports) && c.viewports.length;
  let cfg, cfgFrom = '全局', localWhy = '';
  const local = fs.existsSync(localCfgFile) ? readCfg(localCfgFile) : { ok: false, why: '不存在' };
  if (local.ok && usable(local.value)) {
    cfg = local.value; cfgFrom = '包内';
  } else {
    localWhy = local.ok ? '结构不完整（pages/viewports 缺失或为空）'
      : (fs.existsSync(localCfgFile) ? `非法 JSON：${local.why}` : '不存在');
    if (!fs.existsSync(cfgFile)) {
      checks.push({ name: 'V 配置可读', ok: false, detail: '缺少全局 harness/harness.config.json，且包内 visual/config.json 不可用' });
      return checks;
    }
    const g = readCfg(cfgFile);
    if (!g.ok) {
      checks.push({ name: 'V 配置可读', ok: false, detail: `harness.config.json 不是合法 JSON：${g.why}` });
      return checks;
    }
    if (!usable(g.value)) {
      checks.push({ name: 'V 配置可读', ok: false, detail: 'config 缺少 pages 或 viewports，无法计算应有截图数' });
      return checks;
    }
    cfg = g.value;
  }
  checks.push({
    name: 'V 矩阵来源', ok: true, level: 'info',
    detail: cfgFrom === '包内' ? '使用需求包 visual/config.json' : `回退全局配置：${localWhy}`
  });

  const pages = Array.isArray(cfg.pages) ? cfg.pages : [];
  const viewports = Array.isArray(cfg.viewports) ? cfg.viewports : [];

  const expected = [];
  for (const page of pages) {
    const states = Array.isArray(page.states) && page.states.length ? page.states : ['default'];
    for (const state of states) {
      for (const vp of viewports) expected.push(`${page.name}-${state}-${vp.width}x${vp.height}.png`);
    }
  }

  const shotDir = path.join(reqDir, 'visual', 'shot');
  const actual = fs.existsSync(shotDir) ? fs.readdirSync(shotDir).filter(f => /\.png$/i.test(f)).sort() : [];

  // 覆盖取值：声明了几个值，就要有对应截图；允许 <值>-<page>-<state>-<w>x<h>.png 的前缀命名
  const reqFile = path.join(reqDir, '00-requirement.md');
  const scope = fs.existsSync(reqFile) ? parseScopes(readBody(reqFile)) : { values: [] };
  const prefixes = scope.values.map(v => `${normVal(v.value)}-`);
  const isExpected = name => {
    const low = name.toLowerCase();
    return expected.includes(name) || prefixes.some(p => low.startsWith(p) && expected.includes(name.slice(p.length)));
  };
  const missing = expected.filter(e => !actual.some(f => f === e || prefixes.some(p => f.toLowerCase() === p + e)));
  const extra = actual.filter(f => !isExpected(f));
  const refDir = path.join(reqDir, 'visual', 'ref');
  const refCount = fs.existsSync(refDir) ? fs.readdirSync(refDir).filter(f => !f.startsWith('.')).length : 0;

  checks.push({ name: '参考已冻结', ok: refCount >= 1, detail: `visual/ref ${refCount} 个文件` });
  checks.push({
    name: `截图矩阵 ${expected.length} 张`,
    ok: expected.length > 0 && missing.length === 0,
    detail: missing.length ? `缺 ${missing.length} 张：${missing.join('、')}` : `实得 ${actual.length} 张，与矩阵一致`
  });
  if (extra.length) checks.push({ name: '多余截图（命名不符）', ok: false, detail: `${extra.length} 张：${extra.join('、')}` });

  if (scope.values.length >= 2) {
    const names = actual.map(n => n.toLowerCase());
    const missScope = scope.values.filter(v => !names.some(n => n.includes(normVal(v.value))));
    checks.push({
      name: `覆盖取值截图 ${scope.values.length} 项`,
      ok: missScope.length === 0,
      detail: missScope.length
        ? `缺截图：${missScope.map(v => `${v.dim}/${v.value}`).join('、')}（文件名需包含该取值）`
        : '每个覆盖取值均有截图'
    });
  }

  // 状态维度与采集配置对齐：需求声明的状态必须真的被 pages[].states 采到
  const STATE_ALIAS = {
    '游客': 'guest', '未登录': 'guest', '访客': 'guest', 'guest': 'guest', 'loggedout': 'guest',
    '已登录': 'logged', '登录': 'logged', 'logged': 'logged', 'loggedin': 'logged',
    '空': 'empty', '空态': 'empty', 'empty': 'empty',
    '加载': 'loading', '加载中': 'loading', 'loading': 'loading',
    '错误': 'error', '失败': 'error', 'error': 'error',
    '无权限': 'forbidden', 'forbidden': 'forbidden',
    '默认': 'default', 'default': 'default'
  };
  const canonState = s => STATE_ALIAS[normVal(s)] || normVal(s);
  const stateValues = scope.values.filter(v => /状态|state/i.test(v.dim)).map(v => v.value);
  if (stateValues.length) {
    const configured = new Set(pages.flatMap(p =>
      (Array.isArray(p.states) && p.states.length ? p.states : ['default']).map(canonState)));
    const missStates = stateValues.filter(s => !configured.has(canonState(s)));
    checks.push({
      name: `状态配置覆盖 ${stateValues.length} 项`,
      ok: missStates.length === 0,
      detail: missStates.length
        ? `V 配置未覆盖状态：${missStates.join('、')}（在 config 的 pages[].states 里补上，否则等于没测）`
        : '声明的状态均已进入采集配置'
    });
  }

  // 内容校验：DOM 快照 + 期望项 + 空页防线 + 清单锁定（详见 checkPageContent）
  const contentCheck = checkPageContent(pages, reqDir, approve);
  if (contentCheck) checks.push(contentCheck);

  const line = parseGateLine(stateText, 'V');
  if (!line.ok) {
    checks.push({ name: 'V 人确认字段', ok: false, detail: line.why });
  } else {
    const f = line.fields;
    const who = f['确认人'] || '';
    const when = f['时间'] || '';
    const words = (f['确认原文'] || '').replace(/^["“]|["”]$/g, '').trim();
    const fieldsOk = ['参考', '视口', '状态', '截图'].every(k => !isPlaceholder(f[k] || ''));
    const timeOk = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(when) && !Number.isNaN(Date.parse(when.replace(' ', 'T')));
    const ok = line.conclusion === 'PASS' && !isPlaceholder(who) && timeOk && !isPlaceholder(words) && fieldsOk;
    checks.push({
      name: 'V 人确认字段', ok,
      detail: ok ? `${who} ${when}：“${words}”` :
        `结论=${line.conclusion}；确认人=${who || '空'}；时间=${when || '空'}；原文=${words || '空'}；字段齐全=${fieldsOk}`
    });
  }

  return checks;
}

function gateV(reqId) {
  emitGate('V', 'V 视觉门禁', reqId, vGateChecks(reqId, { approve: argv.includes('--approve') }),
    '按 config 的 pages × states × viewports 补齐截图（命名 <page>-<state>-<w>x<h>.png），并由人给出 V 确认原文后重跑。');
}

// harness engine v0.9：init / layout / status / probe / inventory / scaffold / handoff / gate G1-G4 / gate V / visual / clean

function section(text, title) {
  const m = text.match(new RegExp('##[^\\n]*' + title + '[^\\n]*\\n([\\s\\S]*?)(?=\\n## |$)'));
  return m ? m[1] : '';
}

/** 只读 state.md，输出阶段、阻塞与下一步 */
/** 查域规则：行业基线 + 我们的差距。检索只限 harness/domains/ 内，不全仓扫。 */
function cmdDomain() {
  const kw = positionals().join(' ').trim();
  if (!kw) {
    console.log('用法：harness.mjs domain <领域关键词>（如：骑手 / 客服 / 商家）');
    const dir = path.join(ROOT, 'harness', 'domains');
    const list = fs.existsSync(dir) ? fs.readdirSync(dir).filter(f => f.endsWith('.md') && f !== 'README.md') : [];
    console.log(list.length ? `已收录域：${list.map(f => f.replace(/\.md$/, '')).join('、')}` : '已收录域：（无）');
    process.exit(0);
  }
  const dir = path.join(ROOT, 'harness', 'domains');
  if (!fs.existsSync(dir)) {
    console.log(`未收录该域：${kw}`);
    console.log(`原因：域目录不存在（${path.relative(ROOT, dir) || dir}）`);
    console.log('下一步：先做同行对标，把行业基线与证据锚沉淀到 harness/domains/<域>.md');
    process.exit(0);
  }
  const files = fs.readdirSync(dir).filter(f => f.endsWith('.md') && f !== 'README.md');
  const hits = [];
  for (const f of files) {
    const abs = path.join(dir, f);
    const text = fs.readFileSync(abs, 'utf8');
    // 匹配域名或正文中的关键词
    const name = f.replace(/\.md$/, '');
    if (name.includes(kw) || text.includes(kw)) hits.push({ f, text, name });
  }
  if (!hits.length) {
    console.log(`未收录该域：${kw}`);
    console.log(`已收录域（${files.length} 个）：${files.map(f => f.replace(/\.md$/, '')).join('、') || '（无）'}`);
    console.log('下一步：');
    console.log('  1. 先做同行对标 —— 找同类产品的真实入口、默认态、失败态与基础能力（导航/语音/自动填充/离线）');
    console.log('  2. 每条基线带证据锚（可公开引用的 URL 带章节，或 文件:行号），没证据的不写');
    console.log('  3. 把结论沉淀到 harness/domains/<域>.md 四块：行业基线 / 我们的现状与差距 / 已定取舍 / 变更历史');
    process.exit(0);
  }
  for (const h of hits) {
    const baseline = section(h.text, '行业基线');
    const gap = section(h.text, '我们的现状与差距');
    const trade = section(h.text, '已定取舍');
    // 只数数据行：排除表头、分隔线，以及「（暂无）」这类占位内容行
    const rows = s => s.split(/\r?\n/).filter(l => /^\s*\|/.test(l) && !/^\s*\|[\s:|-]+\|\s*$/.test(l)
      && !/^\s*\|\s*(能力|决策)\s*\|/.test(l) && !/^\s*\|[^|]*[（(]\s*暂无\s*[)）]/.test(l)).length;
    console.log(`\n══ 域：${h.name} （${path.relative(ROOT, path.join(dir, h.f))}）`);
    console.log(`基线 ${rows(baseline)} 条 · 差距 ${rows(gap)} 条 · 取舍 ${rows(trade)} 条`);
    console.log('─'.repeat(56));
    console.log(h.text.trim());
  }
  process.exit(0);
}

/** 估算 token：CJK 约 1 字 1 token，其余约 4 字符 1 token */
function estTokens(text) {
  let cjk = 0, other = 0;
  for (const ch of String(text)) {
    if (/[一-鿿　-〿＀-￯]/.test(ch)) cjk++;
    else other++;
  }
  return Math.round(cjk + other / 4);
}

/** 找到技能所在目录（包内 / 项目内 / 宿主内都能找） */
/** 目录里至少要有个像样的技能数才算「技能根」—— 工程根/包根不会有 */
function looksLikeSkillsRoot(d) {
  try {
    if (!d || !fs.existsSync(d) || !fs.statSync(d).isDirectory()) return false;
    let n = 0;
    for (const x of fs.readdirSync(d)) {
      try { if (fs.existsSync(path.join(d, x, 'SKILL.md'))) n++; } catch { }
    }
    return n >= 5;
  } catch { return false; }
}

/**
 * 找技能根。工程副本（<project>/harness/engine/）里没有 skills/ 目录 ——
 * 44 个技能只装在包或宿主里。所以这里必须能回落到包/宿主，否则在真实工程里
 * `skill <名字>` 恒报「未找到技能」。
 */
function findSkillsRoot() {
  let cfg = {};
  try { cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'harness', 'harness.config.json'), 'utf8')); } catch { }
  const home = process.env.HOME || process.env.USERPROFILE || '';
  const hostRoots = ['trae-cn', 'codex', 'claude', 'workbuddy', 'deepseek', 'reasonix']
    .map(h => home ? path.join(home, '.' + h, 'skills', 'ai-workflow', 'skills') : null)
    .filter(Boolean);
  const cands = [
    cfg.skillSource,                                               // init/bootstrap 记下的来源包
    path.join(path.dirname(SELF_DIR), '..'),                        // <pkg>/skills（引擎在 skills/harness/engine 下）
    path.join(ROOT, 'build', 'v4-staging', 'skills'),
    path.join(ROOT, 'harness', 'skills'),
    ...hostRoots
  ].filter(Boolean);
  return cands.find(looksLikeSkillsRoot) || null;
}

/** 读技能命令：命中技能后先读正文 —— 输出「读哪个文件 / 多长 / 值多少 token」 */
function cmdSkill() {
  const name = positionals()[0];
  const roots = [];
  const sr = findSkillsRoot();
  if (sr) roots.push(sr);
  for (const c of [path.join(ROOT, 'build', 'v4-staging', 'skills'), path.join(ROOT, 'harness', 'skills')]) {
    if (fs.existsSync(c) && !roots.includes(c)) roots.push(c);
  }
  if (!name) {
    console.log('用法：harness.mjs skill <技能名>');
    const all = [];
    for (const r of roots) {
      if (!fs.existsSync(r)) continue;
      for (const d of fs.readdirSync(r)) {
        if (fs.existsSync(path.join(r, d, 'SKILL.md')) && !all.includes(d)) all.push(d);
      }
    }
    console.log(all.length ? `已收录技能（${all.length}）：${all.join('、')}` : '没找到任何技能目录');
    process.exit(0);
  }
  let hit = null;
  for (const r of roots) {
    const f = path.join(r, name, 'SKILL.md');
    if (fs.existsSync(f)) { hit = { root: r, file: f }; break; }
  }
  if (!hit) {
    console.log(`未找到技能：${name}`);
    console.log('  技能装在包里，不装在工程里。查 harness.config.json 的 skillSource，或跑一次 bootstrap 让它写进去。');
    const all = [];
    for (const r of roots) {
      if (!fs.existsSync(r)) continue;
      for (const d of fs.readdirSync(r)) {
        if (fs.existsSync(path.join(r, d, 'SKILL.md')) && !all.includes(d)) all.push(d);
      }
    }
    console.log(all.length ? `已收录技能（${all.length}）：${all.join('、')}` : '没找到任何技能目录');
    console.log('下一步：先查 skills-index.md 看有没有相近的技能；确实没有再按需新写一个。');
    process.exit(0);
  }
  const text = fs.readFileSync(hit.file, 'utf8');
  const lines = text.split(/\r?\n/).length;
  const tokens = estTokens(text);
  // 技能使用日志：按天一个文件，避免长期单文件变大；只追加不改历史
  try {
    const logDir = path.join(ROOT, 'harness', 'logs');
    fs.mkdirSync(logDir, { recursive: true });
    const d = new Date();
    const p2 = (n) => String(n).padStart(2, '0');
    const ymd = `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
    fs.appendFileSync(path.join(logDir, `skill-usage-${ymd}.jsonl`),
      JSON.stringify({ at: d.toISOString(), skill: name, source: 'skill-cmd' }) + '\n');
    // 只保留最近 30 天，旧文件直接清掉（日志可随时删，不影响任何流程）
    const keepMs = 30 * 24 * 3600 * 1000;
    for (const f of fs.readdirSync(logDir)) {
      if (!/^skill-usage-\d{4}-\d{2}-\d{2}\.jsonl$/.test(f)) continue;
      const fp = path.join(logDir, f);
      try { if (Date.now() - fs.statSync(fp).mtimeMs > keepMs) fs.unlinkSync(fp); } catch { /* 跳过 */ }
    }
  } catch { /* 日志失败不影响读技能 */ }
  // 技能文件常在包或宿主里（工程副本没有 skills/ 目录）—— 那时相对路径会变成 ../..，
  // 换个工具/换个工作目录就找不到。工程外的给绝对路径。
  const relRaw = path.relative(ROOT, hit.file).replace(/\\/g, '/');
  const rel = relRaw.startsWith('..') ? hit.file.replace(/\\/g, '/') : relRaw;
  console.log(`技能：${name}`);
  console.log(`读这个文件：${rel}`);
  console.log(`行数：${lines} · 估算 token：${tokens}`);
  if (lines > 500) {
    console.log(`⚠ 规范建议：SKILL.md 正文控制在 500 行 / 5000 token 以内（此技能 ${lines} 行，超标）`);
    const refDir = path.join(path.dirname(hit.file), 'references');
    if (fs.existsSync(refDir)) {
      const refs = fs.readdirSync(refDir).filter(f => !f.startsWith('.'));
      if (refs.length) console.log(`  优先按它的「按需再读」表只读需要的分册：${refs.join('、')}`);
    }
  } else if (tokens > 5000) {
    console.log('⚠ 规范建议：正文控制在 5000 token 以内，建议把细节挪进 references/');
  }
  const refDir = path.join(path.dirname(hit.file), 'references');
  const scriptDir = path.join(path.dirname(hit.file), 'scripts');
  const refs = fs.existsSync(refDir) ? fs.readdirSync(refDir).filter(f => !f.startsWith('.')) : [];
  const scripts = fs.existsSync(scriptDir) ? fs.readdirSync(scriptDir).filter(f => !f.startsWith('.')) : [];
  if (refs.length) console.log(`按需再读（references/）：${refs.map(f => 'references/' + f).join('、')}`);
  if (scripts.length) console.log(`可执行脚本（scripts/）：${scripts.map(f => 'scripts/' + f).join('、')}`);
  console.log('读完正文再动手；未读不得输出该技能的内容，也不得凭记忆复述它。');
  process.exit(0);
}

function cmdStatus() {
  // 不指定包时按活跃度取一个：换工具接手常用的就是 `status`，
  // 让它因为「有多个包」直接报错等于又逼人自己猜哪个是当前现场。
  let arg = positionals()[0] || opt('--req', null);
  if (!arg) {
    const { active, latest } = activeReqs();
    const pick = latest;
    if (pick) {
      arg = pick.name;
      console.log(`（未指定需求包，按活跃度取 ${pick.name}）`);
    }
  }
  const reqId = resolveReq(arg);
  const stateFile = path.join(ROOT, 'harness', 'requirements', reqId, 'state.md');
  if (!fs.existsSync(stateFile)) die(1, `状态文件不存在：${reqId}`);
  const stateText = fs.readFileSync(stateFile, 'utf8');
  const stage = (stateText.match(/^stage:\s*(.+)$/m) || [, 'unknown'])[1].trim();

  const blockers = section(stateText, '阻塞')
    .split(/\r?\n/)
    .filter(l => l.trim().startsWith('|'))
    .filter(l => !/^\|\s*-/.test(l) && !/阻塞项/.test(l))
    .filter(l => !/^\|\s*无\s*\|/.test(l));
  const next = section(stateText, '下一步')
    .split(/\r?\n/)
    .filter(l => /^\d+\./.test(l.trim()))
    .slice(0, 3);

  console.log(`需求：${reqId}`);
  console.log(`阶段：${stage}`);
  console.log(`阻塞：${blockers.length ? '\n' + blockers.map(l => '  ' + l.trim()).join('\n') : '无'}`);
  console.log('下一步：');
  console.log(next.length ? next.map(l => '  ' + l.trim()).join('\n') : '  state.md 未写下一步');
  process.exit(0);
}

/** 执行 harness.config.json 里的 capture.command；未配置返回 3 */
function cmdVisual() {
  const cfgFile = path.join(ROOT, 'harness', 'harness.config.json');
  if (!fs.existsSync(cfgFile)) die(3, '缺少 harness/harness.config.json，无法采集');
  let cfg;
  try {
    cfg = JSON.parse(fs.readFileSync(cfgFile, 'utf8'));
  } catch (e) {
    die(1, `harness.config.json 不是合法 JSON：${e.message}`);
  }
  const command = cfg && cfg.capture && cfg.capture.command;
  if (!command || /YOUR_|PLACEHOLDER|TBD/i.test(command) || /<[^>]*>/.test(command)) {
    die(3, 'capture.command 未配置：请在 harness/harness.config.json 填入可重放的采集命令');
  }
  const result = spawnSync(command, { shell: true, stdio: 'inherit', cwd: ROOT });
  process.exit(typeof result.status === 'number' ? result.status : 1);
}

// ---------- inventory ----------
const SCAN_EXT = /\.(html?|js|jsx|ts|tsx|vue|wxml|wxss|css)$/i;
const SKIP_DIRS = new Set(['node_modules', '.git', 'dist', 'build', '.next', 'unpackage', 'miniprogram_npm']);

function walkScan(dir, acc = [], skipped = []) {
  if (!fs.existsSync(dir)) return acc;
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.name.startsWith('.')) continue;
    const p = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (SKIP_DIRS.has(e.name)) skipped.push(path.relative(ROOT, p).replace(/\\/g, '/'));
      else walkScan(p, acc, skipped);
    } else if (SCAN_EXT.test(e.name)) acc.push(p);
  }
  return acc;
}

/** 扫描原型源码，产出组件/类名/文案候选 + 扫描盲区（动态表达式、跳过的目录） */
function buildInventory(dir) {
  const bump = (m, k, f) => {
    if (!k) return;
    const rec = m.get(k) || { count: 0, files: new Set() };
    rec.count++;
    rec.files.add(path.relative(ROOT, f).replace(/\\/g, '/'));
    m.set(k, rec);
  };
  const classes = new Map();
  const funcs = new Map();
  const texts = new Map();
  const dynamic = [];
  const skipped = [];
  const files = walkScan(dir, [], skipped);

  for (const f of files) {
    let src = '';
    try { src = fs.readFileSync(f, 'utf8'); } catch { continue; }
    const rel = path.relative(ROOT, f).replace(/\\/g, '/');

    // class / className 属性（排除 Vue 的 :class，交给下面单独处理）
    for (const m of src.matchAll(/(?<![:\w-])class(?:Name)?\s*=\s*["'`]([^"'`]+)["'`]/g)) {
      if (/\$\{|{{/.test(m[1])) { dynamic.push(`${rel}: 动态 class "${m[1].slice(0, 40)}"`); continue; }
      for (const c of m[1].split(/\s+/)) {
        if (/^[A-Za-z_][\w-]*$/.test(c)) bump(classes, c, f);
      }
    }
    // Vue :class 绑定
    for (const m of src.matchAll(/:class\s*=\s*["']([^"']+)["']/g)) {
      for (const k of m[1].matchAll(/['"]?([A-Za-z_][\w-]*)['"]?\s*:/g)) bump(classes, k[1], f);
      dynamic.push(`${rel}: :class 绑定 "${m[1].slice(0, 40)}"`);
    }
    // classList.add/remove/toggle
    for (const m of src.matchAll(/classList\.(?:add|remove|toggle)\(\s*([^)]*)\)/g)) {
      for (const q of m[1].matchAll(/['"]([^'"]+)['"]/g)) {
        for (const c of q[1].split(/\s+/)) if (/^[A-Za-z_][\w-]*$/.test(c)) bump(classes, c, f);
      }
      if (/[A-Za-z_$][\w$]*\s*[,)]/.test(m[1].replace(/['"][^'"]*['"]/g, ''))) {
        dynamic.push(`${rel}: classList 参数含变量`);
      }
    }
    // 函数 / 组件工厂
    for (const m of src.matchAll(/(?:export\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/g)) bump(funcs, m[1], f);
    for (const m of src.matchAll(/(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:\(|async|function)/g)) bump(funcs, m[1], f);
    // 对象方法（小程序 Page/Component 里常见的 onLoad(){} / renderXxx(){}）
    const KEYWORDS = new Set(['if', 'for', 'while', 'switch', 'catch', 'function', 'return', 'do', 'try', 'else', 'typeof', 'await', 'new']);
    for (const m of src.matchAll(/^\s{2,}([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{/gm)) {
      if (!KEYWORDS.has(m[1])) bump(funcs, m[1], f);
    }
    // 拼接 / 动态赋值产生的 class：静态解析不到，必须人工确认
    for (const m of src.matchAll(/class\s*=\s*["'][^"'<>]{0,40}["']\s*\+/g)) dynamic.push(`${rel}: class 字符串拼接 ${m[0].slice(0, 40)}`);
    for (const m of src.matchAll(/class(?:Name)?\s*=\s*(?!["'`])[^\n;]+/g)) dynamic.push(`${rel}: className 动态赋值`);
    // WXML 自定义组件标签
    if (/\.wxml$/i.test(f)) {
      for (const m of src.matchAll(/<([a-z][\w-]*-[\w-]+)/g)) bump(funcs, m[1], f);
    }
    // WXSS / CSS 类选择器
    if (/\.(wxss|css)$/i.test(f)) {
      for (const m of src.matchAll(/\.([a-zA-Z][\w-]*)\s*(?=[,{.:\s])/g)) bump(classes, m[1], f);
    }
    // 中文文案
    for (const m of src.matchAll(/>\s*([\u4e00-\u9fa5][^<>{}"'`]{1,12}?)\s*</g)) bump(texts, m[1], f);
  }

  const toArr = m => [...m.entries()]
    .map(([name, v]) => ({ name, count: v.count, files: [...v.files].slice(0, 5) }))
    .sort((a, b) => b.count - a.count);
  return {
    scanned: files.length,
    dir: path.relative(ROOT, dir).replace(/\\/g, '/') || '.',
    skipped,
    dynamic,
    classes: toArr(classes),
    functions: toArr(funcs),
    texts: toArr(texts)
  };
}

/** 清单覆盖自检：expect 是否都能在原型里找到 / 原型高频项是否没进 expect */
function inventoryCheck(inv) {
  const cfg = readConfig();
  const pages = Array.isArray(cfg.pages) ? cfg.pages : [];
  const expectNames = new Set();
  for (const p of pages) {
    for (const t of p.expect || []) {
      const s = String(t);
      const name = s.replace(/^(class|text):/, '').replace(/^count:(.+?)(>=|>|=)\d+$/, '$1');
      expectNames.add(name.trim());
    }
  }
  const all = new Set([...inv.classes, ...inv.functions, ...inv.texts].map(x => x.name));
  const missing = [...expectNames].filter(n => !all.has(n));
  const uncoveredClasses = inv.classes.filter(x => x.count >= 2 && !expectNames.has(x.name)).slice(0, 10);
  const uncoveredFuncs = inv.functions.filter(x => x.count >= 2 && !expectNames.has(x.name)).slice(0, 10);
  return { expectCount: expectNames.size, missing, uncoveredClasses, uncoveredFuncs, dynamic: inv.dynamic.length };
}

function cmdInventory() {
  const from = opt('--from', null);
  const dir = path.resolve(ROOT, from || (fs.existsSync(path.join(ROOT, 'prototype')) ? 'prototype' : '.'));
  if (!fs.existsSync(dir)) die(1, `扫描目录不存在：${dir}`);
  const inv = buildInventory(dir);
  if (argv.includes('--json')) {
    console.log(JSON.stringify(inv, null, 2));
    process.exit(0);
  }
  const top = (arr, n) => arr.slice(0, n).map(x => `    ${x.name}（${x.count} 处）`).join('\n') || '    （无）';
  console.log(`INVENTORY ${inv.dir} · 扫描 ${inv.scanned} 个文件`);
  console.log(`  组件/函数候选 ${inv.functions.length} 个`);
  console.log(top(inv.functions, 25));
  console.log(`  class 候选 ${inv.classes.length} 个`);
  console.log(top(inv.classes, 25));
  console.log(`  文案候选 ${inv.texts.length} 个`);
  console.log(top(inv.texts, 15));
  if (inv.skipped.length) console.log(`  跳过目录：${inv.skipped.join('、')}`);
  if (inv.dynamic.length) {
    console.log(`  动态表达式 ${inv.dynamic.length} 处（无法静态解析，需人工确认）：`);
    for (const d of inv.dynamic.slice(0, 8)) console.log(`    ${d}`);
  }

  const doCheck = argv.includes('--check');
  const chk = doCheck ? inventoryCheck(inv) : null;
  if (chk) {
    console.log(`\n清单自检：expect ${chk.expectCount} 项`);
    console.log(chk.missing.length
      ? `  FAIL expect 中 ${chk.missing.length} 项在原型里找不到：${chk.missing.join('、')}`
      : '  PASS expect 中所有项均能在原型扫描结果里找到');
    if (chk.uncoveredClasses.length) {
      console.log(`  WARN 原型里出现 ≥2 次但未进 expect 的 class：${chk.uncoveredClasses.map(x => x.name).join('、')}`);
    }
    if (chk.uncoveredFuncs.length) {
      console.log(`  WARN 原型里出现 ≥2 次但未进 expect 的组件/函数：${chk.uncoveredFuncs.map(x => x.name).join('、')}`);
    }
  }

  if (argv.includes('--write')) {
    const dst = path.join(ROOT, 'harness', 'inventory.json');
    fs.mkdirSync(path.dirname(dst), { recursive: true });
    fs.writeFileSync(dst, JSON.stringify(inv, null, 2) + '\n', 'utf8');
    console.log(`\n已写入 ${dst}（人工筛选后写入 config 的 expect，再 gate V --approve 锁定）`);
  } else {
    console.log('\n提示：加 --write 落盘 harness/inventory.json；加 --check 与当前 expect 比对覆盖');
  }
  process.exit(chk && chk.missing.length ? 2 : 0);
}

// ---------- scaffold ----------
/** 从小程序 pages/*.wxml 生成 config.pages 与 expect 基线，并把动态点写成人工复核清单 */
function cmdScaffold() {
  const from = opt('--from', 'miniprogram');
  const dir = path.resolve(ROOT, from);
  if (!fs.existsSync(dir)) die(1, `目录不存在：${dir}`);
  const states = (opt('--states', 'default')).split(/[,，]/).map(s => s.trim()).filter(Boolean);
  const files = [];
  const walk = d => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      if (e.name.startsWith('.') || SKIP_DIRS.has(e.name)) continue;
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (/\.wxml$/i.test(e.name)) files.push(p);
    }
  };
  walk(dir);

  const pages = [];
  const dynamic = [];
  for (const f of files) {
    const rel = path.relative(dir, f).replace(/\\/g, '/');
    const m = rel.match(/(?:^|\/)([^/]+)\/([^/]+)\.wxml$/);
    const name = m ? m[2] : rel.replace(/\.wxml$/i, '').replace(/\//g, '-');
    const src = fs.readFileSync(f, 'utf8');
    const classes = new Set();
    for (const mm of src.matchAll(/(?<![:\w-])class\s*=\s*["']([^"']+)["']/g)) {
      if (/\{\{|\$\{/.test(mm[1])) { dynamic.push(`${from}/${rel}: ${mm[1].slice(0, 60)}`); continue; }
      for (const c of mm[1].split(/\s+/)) if (/^[A-Za-z_][\w-]*$/.test(c)) classes.add(c);
    }
    pages.push({
      name,
      path: `/${rel.replace(/\.wxml$/i, '').replace(/^pages\//, '')}`,
      states: [...states],
      expect: [...classes].map(c => `class:${c}`),
      emptyAllowed: [],
      source: `${from}/${rel}`
    });
  }

  console.log(`SCAFFOLD ${from} · ${pages.length} 页 · states=${states.join(',')}`);
  for (const p of pages.slice(0, 10)) console.log(`  ${p.name}  expect=${p.expect.length}  ${p.path}`);
  if (pages.length > 10) console.log(`  … 其余 ${pages.length - 10} 页`);
  console.log(`动态 class 待人工确认：${dynamic.length} 处`);

  if (argv.includes('--apply')) {
    const cfgFile = path.join(ROOT, 'harness', 'harness.config.json');
    const cfg = readConfig();
    const next = { ...cfg, pages };
    fs.mkdirSync(path.dirname(cfgFile), { recursive: true });
    fs.writeFileSync(cfgFile, JSON.stringify(next, null, 2) + '\n', 'utf8');
    if (dynamic.length) {
      const md = ['# 动态 class 复核清单', '', `> 由 scaffold 生成，共 ${dynamic.length} 处；逐条确认实际类名后写入 expect，再 gate V --approve。`, ''];
      for (const d of dynamic) md.push(`- [ ] ${d}`);
      md.push('');
      const dst = path.join(ROOT, 'harness', 'dynamic-review.md');
      fs.writeFileSync(dst, md.join('\n'), 'utf8');
      console.log(`已写入 ${dst}`);
    }
    console.log(`已写入 ${cfgFile}`);
  } else {
    console.log('\nDRY-RUN：加 --apply 写入 harness/harness.config.json');
  }
  process.exit(0);
}

// ---------- handoff ----------
/** 构建 HANDOFF.md 文本（不落盘）。init / gate / handoff 三处共用 ——
 *  原来只有手工 `handoff --write` 才写，换工具接手读到的是 20 小时前的旧快照。 */
function buildHandoff(reqId) {
  const reqDir = path.join(ROOT, 'harness', 'requirements', reqId);
  const stateFile = path.join(reqDir, 'state.md');
  const stateText = fs.existsSync(stateFile) ? fs.readFileSync(stateFile, 'utf8') : '';
  const stage = (stateText.match(/^stage:\s*(.+)$/m) || [, 'unknown'])[1].trim();
  const reqBody = readBody(path.join(reqDir, '00-requirement.md'));
  const testBody = readBody(path.join(reqDir, '02-test.md'));
  const acs = collectAcs(reqBody);
  const testLines = testBody.split(/\r?\n/).filter(l => !l.includes('<'));
  const results = collectAcResults(acs, testLines);
  const { values } = parseScopes(reqBody);
  const valRows = values.map(v => {
    const line = testLines.find(l => normVal(l).includes(normVal(v.value)) && /(PASS|FAIL|BLOCKED)/.test(l));
    return `${v.dim}/${v.value} = ${line ? (line.match(/PASS|FAIL|BLOCKED/) || [''])[0] : '未做'}`;
  });
  const blockers = section(stateText, '阻塞').split(/\r?\n/).filter(l => l.trim().startsWith('|'))
    .filter(l => !/^\|\s*-/.test(l) && !/阻塞项/.test(l) && !/^\|\s*无\s*\|/.test(l));
  // 模板占位行（含 <…>）不是「下一步」—— 刚 init 的包不该把占位写进现场快照
  const next = section(stateText, '下一步').split(/\r?\n/)
    .filter(l => /^\d+\./.test(l.trim()) && !l.includes('<')).slice(0, 5);
  const gates = (stateText.match(/^(?:G\d|V):\s*[^|\n]+/gm) || []).map(s => s.trim());
  // state.md 的 G 行是「人确认过的结论」，gate.log 是「实际跑出来的结果」——
  // 两处都给，接手才分得清「跑过什么」和「谁签字了」，也不会出现执行完仍显示「未开始」。
  let recent = null;
  try {
    const logFile = path.join(ROOT, 'harness', 'logs', 'gate.log');
    if (fs.existsSync(logFile)) {
      for (const line of fs.readFileSync(logFile, 'utf8').trim().split(/\r?\n/).reverse()) {
        try {
          const r = JSON.parse(line);
          if (r.req === reqId) { recent = r; break; }
        } catch { /* 跳过坏行 */ }
      }
    }
  } catch { /* 日志坏了不影响 HANDOFF 生成 */ }
  const bad = results.filter(r => !r.ok);

  return [
    '# 接手指南（引擎自动生成，勿手改）',
    '',
    `- 生成时间：${stamp()}`,
    `- 需求：${reqId}`,
    `- 当前阶段：${stage}`,
    `- 项目：${ROOT}`,
    '',
    '## 1. 现在做到哪',
    `- 验收点：${results.filter(r => r.ok).length}/${results.length} 有结论与证据`,
    bad.length ? `- 未过：${bad.map(r => `${r.ac}（${r.why}）`).join('、')}` : '- 未过：无',
    valRows.length ? `- 覆盖取值：${valRows.join('；')}` : '- 覆盖取值：未声明',
    '',
    '## 2. 门禁最新结论',
    gates.length ? gates.map(g => `- ${g}`).join('\n') : '- state.md 无门禁记录',
    recent ? `- 最近执行：${recent.stage} ${recent.result}（${recent.at}）` : '- 最近执行：无记录',
    '',
    '## 3. 阻塞',
    blockers.length ? blockers.map(l => `- ${l.trim()}`).join('\n') : '- 无',
    '',
    '## 4. 下一步（来自 state.md）',
    next.length ? next.map(l => `- ${l.trim()}`).join('\n') : '- state.md 未写下一步，先补它',
    '',
    '## 5. 接手三步',
    `1. 读本文件与 \`harness/requirements/${reqId}/state.md\``,
    `2. 跑 \`node harness/engine/harness.mjs status ${reqId}\` 与 \`probe\` 确认通道`,
    '3. 按「下一步」做；门禁只认退出码，禁止凭聊天记忆续接',
    ''
  ].join('\n');
}

/** 原子写 HANDOFF：先写临时文件再替换，并发读到的是完整旧版或完整新版，不会是半截 */
function writeHandoff(reqId) {
  try {
    const out = buildHandoff(reqId);
    const dst = path.join(ROOT, 'harness', 'HANDOFF.md');
    mkdirp(path.dirname(dst));
    const tmp = `${dst}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, out, 'utf8');
    try {
      fs.renameSync(tmp, dst);
    } catch {
      // Windows 上目标已存在时 rename 会失败：用 copy 覆盖而不是先删目标，
      // 否则中间有一段 HANDOFF 不存在的窗口，且第二次 rename 失败会直接丢掉现场。
      fs.copyFileSync(tmp, dst);
      fs.rmSync(tmp, { force: true });
    }
    return dst;
  } catch (e) {
    // 写现场失败不得改变门禁结论 —— 门禁只由判据决定
    console.log(`\nHANDOFF 刷新失败（不影响门禁结论）：${e && e.message ? e.message : e}`);
    return null;
  }
}

function cmdHandoff() {
  const reqId = resolveReq(positionals()[0] || opt('--req', null));
  console.log(buildHandoff(reqId));
  if (argv.includes('--write')) {
    const dst = writeHandoff(reqId);
    if (dst) console.log(`已写入 ${dst}`);
  }
  process.exit(0);
}

// ---------- probe ----------
function cmdProbe() {
  const md = opt('--module-dir', null);
  const caps = detectCapabilities(md ? [path.resolve(md)] : []);
  if (argv.includes('--json')) {
    console.log(JSON.stringify({ root: ROOT, ...caps }, null, 2));
    process.exit(0);
  }
  console.log(`PROBE ${ROOT}`);
  console.log(`  OK   node          ${caps.node}`);
  for (const mod of ['playwright', 'playwright-core', 'puppeteer']) {
    console.log(caps.deps[mod]
      ? `  OK   ${mod.padEnd(13)} ${caps.deps[mod]}`
      : `  NO   ${mod}`);
  }
  console.log(caps.browsers.length
    ? `  OK   系统浏览器      ${caps.browsers[0]}${caps.browsers.length > 1 ? ` 等 ${caps.browsers.length} 个` : ''}`
    : '  NO   系统浏览器      未找到 Chrome/Edge');
  console.log(caps.wechatide.found
    ? `  OK   wechatide       ${caps.wechatide.path}`
    : '  NO   wechatide       未找到微信开发者工具 CLI（设置里开启「命令行」或设 WECHATIDE_CLI）');
  console.log('  --   桌面/computer-use  需宿主支持，引擎无法探测');
  const usable = [];
  if (Object.keys(caps.deps).length || caps.browsers.length) usable.push('浏览器');
  if (caps.wechatide.found) usable.push('小程序');
  console.log(`结论：可用通道 = ${usable.length ? usable.join('、') : '无（装好工具前不能产出真实证据）'}`);
  process.exit(0);
}

// ---------- clean ----------
// 约定：验证沙箱一律建在 os.tmpdir() 下，命名 harness-sandbox-<用途>-<时间戳>。
// 兼容历史前缀（agent-harness-product- / harness-bench / harness_hooks_env），一并回收。
const SANDBOX_RE = /^(harness-sandbox-|harness-bench|harness_hooks_env|agent-harness-product-)/;
// 只处理浏览器进程：占用沙箱的通常是自动化留下的无头浏览器，避免误杀 shell / 编辑器
const BROWSER_NAMES = new Set(['chrome.exe', 'msedge.exe', 'chromium.exe', 'headless_shell.exe', 'chrome', 'msedge', 'chromium', 'headless_shell']);

function dirSize(p) {
  let total = 0;
  let entries;
  try { entries = fs.readdirSync(p, { withFileTypes: true }); } catch { return 0; }
  for (const e of entries) {
    const f = path.join(p, e.name);
    if (e.isDirectory()) total += dirSize(f);
    else { try { total += fs.statSync(f).size; } catch { /* 被占用则跳过 */ } }
  }
  return total;
}

function listSandboxes() {
  const root = os.tmpdir();
  if (!fs.existsSync(root)) return { root, items: [] };
  const items = fs.readdirSync(root, { withFileTypes: true })
    .filter(e => e.isDirectory() && SANDBOX_RE.test(e.name))
    .map(e => {
      const p = path.join(root, e.name);
      return { name: e.name, path: p, bytes: dirSize(p) };
    });
  return { root, items };
}

/** 找出命令行里引用了沙箱路径的浏览器进程 */
function findOrphanBrowsers(sandboxes) {
  const needles = sandboxes.map(s => s.path.toLowerCase());
  if (!needles.length) return [];
  let list = [];
  if (process.platform === 'win32') {
    const ps = 'Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress';
    const r = spawnSync('powershell', ['-NoProfile', '-NonInteractive', '-Command', ps],
      { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
    if (r.status !== 0 || !r.stdout) return [];
    try {
      const parsed = JSON.parse(r.stdout);
      list = Array.isArray(parsed) ? parsed : [parsed];
    } catch { return []; }
    return list
      .filter(p => BROWSER_NAMES.has(String(p.Name || '').toLowerCase()))
      .filter(p => needles.some(n => String(p.CommandLine || '').toLowerCase().includes(n)))
      .map(p => ({ pid: p.ProcessId, name: p.Name }));
  }
  const r = spawnSync('ps', ['-eo', 'pid=,comm=,args='], { encoding: 'utf8' });
  if (r.status !== 0 || !r.stdout) return [];
  return r.stdout.split(/\r?\n/)
    .filter(l => BROWSER_NAMES.has((l.trim().split(/\s+/)[1] || '').toLowerCase()))
    .filter(l => needles.some(n => l.toLowerCase().includes(n)))
    .map(l => {
      const parts = l.trim().split(/\s+/);
      return { pid: Number(parts[0]), name: parts[1] };
    });
}

function killPid(pid) {
  if (process.platform === 'win32') {
    return spawnSync('taskkill', ['/PID', String(pid), '/F', '/T'], { encoding: 'utf8' }).status === 0;
  }
  try { process.kill(pid, 'SIGKILL'); return true; } catch { return false; }
}

function sleepSync(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

// ---------- begin ----------
// 一条命令把开工要的判断一次给全：档位 / 红灯 / 工具 / 相关经验。
// 只读：不建任务单、不改任何文件。目的是把 init+selfcheck+probe+memory 四次往返合成一次。
function cmdInstallHook() {
  const force = argv.includes('--force');
  const MARK = '# ai-workflow-generated';
  // 不能硬拼 ROOT/.git —— worktree 里 .git 是个文件（内容是一行 gitdir: ...），
  // 主仓库里才是目录。所以问 git 要真实 hooks 路径。
  const hooksReal = (() => {
    try {
      const r = spawnSync('git', ['rev-parse', '--git-path', 'hooks'], { cwd: ROOT, encoding: 'utf8', timeout: 15000 });
      const out = (r.stdout || '').trim();
      return r.status === 0 && out ? path.resolve(ROOT, out) : null;
    } catch { return null; }
  })();
  if (!hooksReal) {
    console.log('SKIP 不是 git 仓库（没有 .git 目录）—— hook 装不了');
    console.log('  hook 依赖 git 触发。没有 git 时照样用 begin 手动检查，机制不是没用了');
    console.log('  要装的话：手动把下面两行存成 .git/hooks/{pre-commit,post-commit} 并 chmod +x');
    console.log('    exec node harness/scripts/hook-precommit.mjs --brief');
    console.log('    exec node harness/scripts/hook-postcommit.mjs');
    process.exit(0);
  }

  const hooksDir = hooksReal;
  mkdirp(hooksDir);
  const HOOKS = [
    ['pre-commit', 'harness/scripts/hook-precommit.mjs', '--brief'],
    ['post-commit', 'harness/scripts/hook-postcommit.mjs'],
    // git merge 不触发 pre-commit —— 合并提交从未被检查过，靠 post-merge 补一条痕
    ['post-merge', 'harness/scripts/hook-postmerge.mjs']
  ];
  const written = [], skipped = [];
  for (const [name, target, ...hookArgs] of HOOKS) {
    const dst = path.join(hooksDir, name);
    if (fs.existsSync(dst) && !force) {
      const cur = fs.readFileSync(dst, 'utf8');
      // 不覆盖别人装的 hook —— 那是用户的资产，覆盖了可能把人家整套检查废掉
      if (!cur.includes(MARK)) { skipped.push(name); continue; }
    }
    const body = [
      '#!/bin/sh',
      `${MARK} —— 删掉本文件即撤掉检查；重装：node harness/engine/harness.mjs install-hook`,
      'ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0',
      'cd "$ROOT" || exit 0',
      // 没 node 就放行：装了 hook 却跑不起来时，绝不能把提交全堵死
      'command -v node >/dev/null 2>&1 || { echo "[harness] 找不到 node，跳过检查（不阻断提交）" >&2; exit 0; }',
      `if [ -f "${target}" ]; then exec node "${target}"${hookArgs.length ? ' ' + hookArgs.join(' ') : ''}; fi`,
      'exit 0',
      ''
    ].join('\n');
    fs.writeFileSync(dst, body, 'utf8');
    try { fs.chmodSync(dst, 0o755); } catch { /* Windows 上 chmod 可能无效，git 仍会执行 */ }
    written.push(name);
  }

  console.log(`INSTALL-HOOK ${ROOT}`);
  for (const w of written) console.log(`  OK    已写 .git/hooks/${w}`);
  for (const s of skipped) console.log(`  SKIP  .git/hooks/${s} 已存在且不是本项目生成的 —— 没覆盖（要覆盖加 --force，但先看清原来那份在做什么）`);
  if (skipped.length) {
    console.log('\n结论：部分装上了。被跳过的 hook 需要手动把这一行加进去：');
    for (const s of skipped) {
      const t = s === 'pre-commit' ? 'harness/scripts/hook-precommit.mjs'
        : s === 'post-merge' ? 'harness/scripts/hook-postmerge.mjs'
        : 'harness/scripts/hook-postcommit.mjs';
      console.log(`  exec node ${t}`);
    }
    process.exit(0);
  }
  console.log('\n结论：装好了。下次 git commit 会自动跑自检（约 3 秒），不通过即阻断。');
  console.log(`  绕过：git commit --no-verify（会被 post-commit 记下来，下次 begin 提示）`);
  console.log(`  撤掉：rm "${hooksDir}/pre-commit" "${hooksDir}/post-commit" "${hooksDir}/post-merge"`);
  process.exit(0);
}

/**
 * 从技能包里找脚本。route.mjs 依赖 catalog.json，拷到工程里跑不了 ——
 * 所以只能在包内跑：skillSource 指向 <pkg>/skills，上一级就是包根。
 */
function findPkgScript(name) {
  const cfg = readConfig();
  const cands = [
    cfg.skillSource ? path.join(path.dirname(cfg.skillSource), 'scripts', name) : null,
    path.join(ROOT, 'build', 'v4-staging', 'scripts', name)
  ].filter(Boolean);
  for (const c of cands) { try { if (fs.existsSync(c)) return c; } catch { } }
  return null;
}

/** 引擎自身产物：本包运行 / 维护时会被自己改写，不算「需求引用的代码被别人改过」。 */
const ENGINE_ARTIFACT_DIRS = ['harness/engine/', 'harness/scripts/', 'harness/templates/', 'build/v4-staging/'];
const ENGINE_ARTIFACT_FILES = new Set(['harness/NEXT.md', 'harness/HANDOFF.md']);

/** 当前 ROOT 是不是 git 工作树。bare 仓库 / 未初始化时不是 —— 那种现场里 harness 与
 *  build/ 的改动是本包维护动作，不是「别人改了需求引用的文件」，报成漂移只会误导接手的人。 */
function isGitWorkTree() {
  try {
    const r = spawnSync('git', ['rev-parse', '--is-inside-work-tree'], { cwd: ROOT, encoding: 'utf8', timeout: 15000 });
    return String(r.stdout || '').trim() === 'true';
  } catch { return true; }   // 查不了时保持原有严格度
}

/** 上下文漂移：state.md 写完之后，它提到的文件有没有被改过。
 *  换人接手时最危险的假设是「state 说的还是现状」—— 文件早被改过而 state 没更新，
 *  接手的人就会照着过期计划做，而且做得越认真错得越远。
 *  基线取 state.md 自身的 mtime（精度到秒），比取 frontmatter 的 updated 日期靠谱：
 *  日期只到天，当天内的改动分辨不出来。 */
function detectDrift(reqId) {
  const statePath = path.join(ROOT, 'harness', 'requirements', reqId, 'state.md');
  if (!fs.existsSync(statePath)) return null;
  const base = fs.statSync(statePath).mtimeMs;
  const txt = fs.readFileSync(statePath, 'utf8');
  const paths = [...new Set(txt.match(/[A-Za-z0-9_\-./]+\.(mjs|cjs|js|ts|tsx|json|md|css|scss|html|vue|py|go|rs|sh|yml|yaml)/g) || [])]
    .filter(p => !p.includes('<') && !/^https?:/i.test(p));
  const inWorkTree = isGitWorkTree();
  const newer = [];
  let skippedEngine = 0;
  for (const p of paths) {
    if (!inWorkTree && (ENGINE_ARTIFACT_FILES.has(p) || ENGINE_ARTIFACT_DIRS.some(d => p.startsWith(d)))) {
      skippedEngine++;
      continue;
    }
    try {
      const f = path.join(ROOT, p);
      if (fs.existsSync(f) && fs.statSync(f).mtimeMs > base + 1500) newer.push({ path: p, mtime: fs.statSync(f).mtime });
    } catch { }
  }
  return { base, checked: paths.length, newer, skippedEngine };
}

function cmdDrift() {
  const want = positionals()[0] || null;
  const baseDir = path.join(ROOT, 'harness', 'requirements');
  if (!fs.existsSync(baseDir)) die(1, '没有需求包目录 —— 先在当前工程 init');
  let reqId = want;
  if (!reqId) {
    const cands = fs.readdirSync(baseDir)
      .filter(d => /^REQ-/.test(d) && fs.existsSync(path.join(baseDir, d, 'state.md')))
      .map(d => ({ d, m: fs.statSync(path.join(baseDir, d, 'state.md')).mtimeMs }))
      .sort((a, b) => b.m - a.m);
    if (!cands.length) die(1, '没有可检查的需求包');
    reqId = cands[0].d;
  }
  const r = detectDrift(reqId);
  if (!r) die(1, `${reqId} 没有 state.md，无法判断漂移`);
  if (!r.checked) {
    console.log(`DRIFT-UNKNOWN ${reqId} · state.md 里没有可核对的文件路径 —— 把「下一步」写成「打开 X，改 Y」才能判断`);
    process.exit(0);
  }
  if (!r.newer.length) {
    console.log(`DRIFT-CLEAN ${reqId} · 核对 ${r.checked} 个文件，没有比 state.md 更新的`);
    process.exit(0);
  }
  console.log(`DRIFT ${reqId} · 核对 ${r.checked} 个文件，${r.newer.length} 个比 state.md 新`
    + (r.skippedEngine ? `（另排除引擎产物 ${r.skippedEngine} 个）` : '') + '：');
  for (const x of r.newer.slice(0, 8)) {
    console.log(`  ${x.path}（${localDate(x.mtime)} ${String(x.mtime.getHours()).padStart(2, '0')}:${String(x.mtime.getMinutes()).padStart(2, '0')}）`);
  }
  if (r.newer.length > 8) console.log(`  … 另有 ${r.newer.length - 8} 个`);
  console.log('  → 可能已被他人改过：先核对改动再继续，别照着过期计划做');
}

function cmdBegin() {
  const need = positionals().join(' ').trim();
  if (!need) die(1, '用法：harness.mjs begin "<一句话需求>" [--size L0|L1|L2]');
  const dir = opt('--dir', null);
  if (dir && path.resolve(dir) !== path.resolve(ROOT)) {
    die(1, `begin 只检查当前工程（${ROOT}）；要看别的工程请先切过去，传 --dir 容易让人误以为查过了`);
  }

  const forced = opt('--size', null);
  const risky = /上线|发布|迁移|删库|删除数据|重构|全量|多端|支付|权限|架构/.test(need);
  const tiny = /错别字|文案|注释|改个名|重命名变量|格式化/.test(need);
  const size = forced ? forced.toUpperCase() : (risky ? 'L2' : tiny ? 'L0' : 'L1');
  const why = forced ? '手动指定' : risky ? '需求含高风险信号词' : tiny ? '属单点低风险修改' : '默认：多文件但行为局部';

  const sc = findScript('selfcheck.mjs');
  let reds = null;
  if (sc) {
    const r = spawnSync(process.execPath, [sc], { cwd: ROOT, encoding: 'utf8', timeout: 180000 });
    const out = `${r.stdout || ''}${r.stderr || ''}`;
    const m = out.match(/阻断\s*(\d+)/);
    reds = m ? Number(m[1]) : (r.status === 0 ? 0 : -1);
  }

  const caps = detectCapabilities([]);
  const channels = [];
  if (Object.keys(caps.deps).length || caps.browsers.length) channels.push('浏览器');
  if (caps.wechatide.found) channels.push('小程序');

  const mem = findScript('memory.mjs');
  let brief = '';
  if (mem) {
    const r = spawnSync(process.execPath, [mem, 'brief', need], { cwd: ROOT, encoding: 'utf8', timeout: 30000 });
    brief = (r.stdout || '').trim();
  }

  const ok = reds === 0;
  const redText = reds === null ? '未查（脚本缺失）'
    : reds === 0 ? '0'
    : reds > 0 ? String(reds)
    : '运行失败';

  // 最近有没有绕过自检的提交 —— post-commit 留的痕（--no-verify 跳过 pre-commit，但不跳过 post-commit）
  let bypassNote = '';
  {
    const obsFile = path.join(ROOT, 'harness', 'memory', 'observe.md');
    if (fs.existsSync(obsFile)) {
      const hits = fs.readFileSync(obsFile, 'utf8').split(/\r?\n/).filter(l => l.includes('提交绕过了自检'));
      const last = hits[hits.length - 1];
      const m = last && last.match(/^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})/);
      if (m) {
        const days = Math.floor((Date.now() - new Date(`${m[1]}T${m[2]}:00`).getTime()) / 86400000);
        if (days <= 7) {
          bypassNote = `WARN 上次提交绕过了自检（${m[1]} ${m[2]}，${days === 0 ? '今天' : days + ' 天前'}）`
            + ' —— 那次没跑自检。这次开工前自己跑一遍：node harness/scripts/selfcheck.mjs';
        }
      }
    }
  }

  // 漂移：只在不一致时出声。一致或没有需求包时静默 —— 每次开工都报「没问题」是噪音。
  // 只看未收口的包：已过 G4 的包早就冻结了，它的 state 里提到的文件后来被别人改是正常的。
  let driftNote = '';
  {
    const reqBase = path.join(ROOT, 'harness', 'requirements');
    if (fs.existsSync(reqBase)) {
      const packs = fs.readdirSync(reqBase)
        .filter(d => /^REQ-/.test(d) && fs.existsSync(path.join(reqBase, d, 'state.md')));
      const active = packs.filter(d => {
        const t = fs.readFileSync(path.join(reqBase, d, 'state.md'), 'utf8');
        return !/G4:\s*PASS/.test(t) && !/^stage:\s*(archive|done|released)/m.test(t);
      });
      const hits = [];
      for (const d of active) {
        const r = detectDrift(d);
        if (r && r.newer.length) hits.push({ d, r });
      }
      if (hits.length) {
        const total = hits.reduce((n, h) => n + h.r.newer.length, 0);
        driftNote = `WARN ${hits.length} 个未收口需求包的 state 之后有 ${total} 个文件被改过`
          + `（${hits[0].d}: ${hits[0].r.newer.slice(0, 2).map(x => x.path).join('、')}）`
          + ' —— 先 node harness/engine/harness.mjs drift 核对，别照着过期计划做';
      }
    }
  }

  // 重复任务识别 —— 同一个任务做第二次要认得出来。
  // 只在有可比历史且输入关系明确时才说话；没有就静默（宁可不提，也不给错的复用建议）。
  let reuseNote = '';
  {
    const sigScript = findScript('task-signature.mjs');
    if (sigScript) {
      const r = spawnSync(process.execPath, [sigScript, 'check', '--desc', need], { cwd: ROOT, encoding: 'utf8', timeout: 20000 });
      reuseNote = (r.stdout || '').trim();
    }
  }

  console.log(`${ok ? 'READY' : 'NOT-READY'} · ${size} · ${path.basename(ROOT)} · 红灯 ${redText} · 通道 ${channels.length ? channels.join('/') : '无'}`);
  console.log(`  档位 ${size}（依据：${why}）`);
  console.log(`  ${reds === 0 ? 'OK' : 'NO'}  自检 ${reds === 0 ? '0 阻断'
    : reds > 0 ? `${reds} 个红灯，先修`
    : reds === null ? '脚本缺失，无法判断' : '运行失败，手动跑一次 selfcheck'}`);
  console.log(`  ${channels.length ? 'OK' : 'NO'}  工具 ${channels.length ? channels.join('、') + ' 可用' : '无可用通道（要真证据就得先装工具）'}`);

  // 当前需求包：换工具接手第一件事是知道「做到哪」。不给这一行，人就只能凭聊天记忆猜。
  {
    const { active, latest } = activeReqs();
    const pick = latest;
    if (!pick) {
      console.log('  需求包 无 —— 还没有任务单；要开工就说「开始」，我跑 init 建一个');
    } else {
      const sf = path.join(ROOT, 'harness', 'requirements', pick.name, 'state.md');
      const text = fs.existsSync(sf) ? fs.readFileSync(sf, 'utf8') : '';
      const nextLine = section(text, '下一步').split(/\r?\n/)
        .filter(l => /^\d+\./.test(l.trim()) && !l.includes('<'))[0];
      const tag = pick.closed ? '最近收口' : '当前需求包';
      console.log(`  ${tag} ${pick.name}（阶段 ${pick.stage}）`
        + (nextLine ? ` · 下一步：${nextLine.trim().replace(/^\d+\.\s*/, '')}` : ''));
      const others = active.filter(x => x.name !== pick.name);
      if (others.length) {
        console.log(`       另有未收口：${others.slice(0, 3).map(x => x.name).join('、')}${others.length > 3 ? ' …' : ''}`);
      }
      if (nextLine) console.log(`       → 详情读 harness/requirements/${pick.name}/state.md`);
    }
  }

  // 范围排除清单：只报条数与路径。逐条刷屏会挤掉真正该看的红灯与经验。
  {
    const oos = path.join(ROOT, 'OUT-OF-SCOPE.md');
    if (!fs.existsSync(oos)) {
      console.log('  范围排除 未配置（OUT-OF-SCOPE.md 不存在）');
    } else {
      // 与 selfcheck 同一套解析：剥首尾竖线、去掉分隔行与表头；读取失败不让 begin 崩
      try {
        const rows = fs.readFileSync(oos, 'utf8').split(/\r?\n/)
          .filter(l => /^\|/.test(l))
          .map(l => l.trim().replace(/^\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim()))
          .filter(c => !c.every(x => /^:?-{3,}:?$/.test(x) || x === ''));
        if (rows.length && rows[0].some(x => x === '类别' || x === '不收什么')) rows.shift();
        console.log(`  范围排除 ${rows.length} 条 · OUT-OF-SCOPE.md（明确不做什么，避免重复提议）`);
      } catch {
        console.log('  范围排除 读取失败（OUT-OF-SCOPE.md 存在但读不了）');
      }
    }
  }

  // 决策记录：跨需求决策只报条数与路径，避免把索引内容挤进开工输出。
  {
    const adrDir = path.join(ROOT, 'docs', 'adr');
    if (!fs.existsSync(adrDir)) {
      console.log('  决策记录 未配置（docs/adr/ 不存在）');
    } else {
      try {
        const n = fs.readdirSync(adrDir)
          .filter(f => /^\d{4}-[a-z0-9-]+\.md$/.test(f) && !/^0000-/.test(f)).length;
        console.log(n
          ? `  决策记录 ${n} 条 · docs/adr（跨需求决策，含被否方案）`
          : '  决策记录 0 条 · docs/adr（尚无决策文件）');
      } catch {
        console.log('  决策记录 读取失败（docs/adr 存在但读不了）');
      }
    }
  }

  // 待消费技能 changeset：一个技能变更一条 .changeset/*.md；为空时只报 0。
  // 落在技能包根（skill-changeset.mjs 写那里），不是工程根 —— 工程副本里没有 .changeset。
  {
    const skillsRoot = findSkillsRoot();
    const changeDir = skillsRoot
      ? path.join(path.dirname(skillsRoot), '.changeset')
      : path.join(ROOT, '.changeset');
    let pending = [];
    try {
      if (fs.existsSync(changeDir)) {
        pending = fs.readdirSync(changeDir).filter(f => f.endsWith('.md') && f !== 'README.md').sort();
      }
      console.log(`  技能变更 ${pending.length} 条待消费${pending.length ? ' · .changeset' : ' · .changeset（无待消费条目）'}`);
    } catch {
      console.log('  技能变更 .changeset 读取失败，跳过');
    }
  }

  // 技能建议：不给这一行，agent 就只会用最眼熟的那一两个技能，
  // 其余几十个优化过的技能永远进不来（用户原话：「优化了太多技能，你一样都不按工作流走」）
  {
    const routeScript = findPkgScript('route.mjs');
    let picked = [];
    if (routeScript) {
      const r = spawnSync(process.execPath, [routeScript, need, '--json'], { cwd: path.dirname(path.dirname(routeScript)), encoding: 'utf8', timeout: 20000 });
      try { picked = (JSON.parse((r.stdout || '{}')).route) || []; } catch { picked = []; }
    }
    // harness 是常驻，不用在这儿重复推荐
    picked = picked.filter(x => x.name !== 'harness').slice(0, 3);
    if (picked.length) {
      console.log(`  技能 ${picked.map(x => x.name + '(' + x.score + ')').join(' → ')}`);
      console.log(`       → 先跑 node harness/engine/harness.mjs skill ${picked[0].name}，读完正文再动手（没读不得输出它的内容）`);
    } else if (!routeScript) {
      console.log('  技能 路由脚本缺失，自己按需求判断该读哪个（别因为没建议就跳过）');
    } else {
      console.log('  技能 没命中明确技能 —— 读需求自己判断该加载哪个，别跳过');
    }
  }
  // 未结事项：docs 里定了但没执行的事 + 等人拍板的决策。
  // 不给这一行，「文档里写了」就等于「从没发生」—— 用户原话：「这些都没执行，你也记不住了」。
  let openNote = '';
  {
    const oiFile = path.join(ROOT, 'harness', 'memory', 'open-items.md');
    if (fs.existsSync(oiFile)) {
      const rows = fs.readFileSync(oiFile, 'utf8').split(/\r?\n/).filter(l => /^\|\s*OI-\d+/.test(l));
      const cnt = st => rows.filter(l => (l.split('|')[5] || '').trim() === st).length;
      const wait = cnt('待决'), todo = cnt('未做'), part = cnt('部分');
      if (wait + todo + part) {
        openNote = `欠账 待拍板 ${wait} · 未做 ${todo} · 部分 ${part}（清单在 harness/memory/open-items.md，每条带来源）`;
      }
    }
  }

  if (brief) console.log(brief.split('\n').map(l => '  ' + l).join('\n'));
  if (openNote) console.log(`  ${openNote}`);
  if (driftNote) console.log(`  ${driftNote}`);
  if (reuseNote) console.log(reuseNote.split('\n').map(l => '  ' + l).join('\n'));
  if (bypassNote) console.log(`  ${bypassNote}`);

  if (!ok) {
    console.log('→ 先修红灯再开工（带着红灯动手，等于把问题带进代码）');
  } else if (size === 'L0') {
    console.log('→ 直接改即可，不用建单；改完说清改了什么。');
  } else {
    console.log(`→ 可以开工：node harness/engine/harness.mjs init "${need}"`);
  }
  // 未读回执：跨会话信箱（REQ-20261123-03）。读失败跳过，不阻断开工。
  {
    let unread = [];
    try { unread = unreadMail(); }
    catch { console.log('  未读回执 读取失败（harness/mailbox.jsonl 损坏），跳过'); }
    if (unread.length) {
      console.log(`  未读回执（${unread.length}）· harness/mailbox.jsonl`);
      for (const m of unread.slice(0, 5)) {
        const sum = String(m.text || '').replace(/\s+/g, ' ').slice(0, 60);
        console.log(`    - [${m.from || '?'} → ${m.to || '?'} ${m.ts || ''}] ${sum}`);
      }
      if (unread.length > 5) console.log(`    … 另有 ${unread.length - 5} 条`);
      console.log('       → 回执已读：node harness/engine/harness.mjs msg --read');
    }
  }

  process.exit(ok ? 0 : 2);
}

function cmdClean() {
  const apply = argv.includes('--apply');
  const { root, items } = listSandboxes();
  console.log(`CLEAN ${root}`);
  if (!items.length) console.log('  OK    没有匹配的验证沙箱');

  const orphans = findOrphanBrowsers(items);
  for (const o of orphans) console.log(`  ${apply ? 'KILL ' : 'PLAN '}${o.name} pid=${o.pid}（仍在占用沙箱）`);
  if (apply && orphans.length) {
    for (const o of orphans) killPid(o.pid);
    sleepSync(1500);
  }

  let freed = 0;
  const remain = [];
  for (const it of items) {
    const mb = (it.bytes / 1048576).toFixed(2);
    if (!apply) { console.log(`  PLAN  ${it.name}  ${mb}MB`); continue; }
    try {
      fs.rmSync(it.path, { recursive: true, force: true, maxRetries: 5, retryDelay: 400 });
      freed += it.bytes;
      console.log(`  DEL   ${it.name}  ${mb}MB`);
    } catch (e) {
      remain.push(`${it.name}（${e.code || 'ERROR'}）`);
      console.log(`  FAIL  ${it.name}  未释放：${e.message.split(/\r?\n/)[0]}`);
    }
  }

  // ---- 包目录里的垃圾：孤儿分发包 / .bak 残留 / 空目录壳 ----
  // 自检一直在报这三类（info 级），但没人动手。**报而不收 = 等于没报**，
  // 所以收敛动作放在这里，自检只负责发现与催。
  {
    // 引擎里没有 PKG 变量（那是 selfcheck 的写法）—— 按「含 SKILL.md + skills/ 的目录」认包根
    const pkgRoot = (() => {
      const cands = [
        path.dirname(path.dirname(path.dirname(SELF_DIR))),   // pkg/skills/harness/engine → pkg
        path.join(ROOT || '', 'build', 'v4-staging'),
        ROOT || ''
      ];
      for (const c of cands) {
        if (c && fs.existsSync(path.join(c, 'SKILL.md')) && fs.existsSync(path.join(c, 'skills'))) return c;
      }
      return cands[0];
    })();
    const pkgMd = path.join(pkgRoot, 'SKILL.md');
    const pkgVer = fs.existsSync(pkgMd)
      ? ((fs.readFileSync(pkgMd, 'utf8').match(/^version:\s*([\d.]+)/m) || [])[1] || '')
      : '';
    // 默认不保留历史：zip 随时能重新打，回滚靠 git tag。留着只是占地方、看着像没收拾
    const keepN = Math.max(0, Number(opt('--keep-zips', '0')) || 0);

    // 1) 孤儿分发包：当前版本永远保留，历史版本只留最近 keepN 个
    const zipDir = pkgRoot;
    if (fs.existsSync(zipDir)) {
      const zips = fs.readdirSync(zipDir)
        .filter(f => /^ai-workflow-v\d+\.\d+\.\d+\.zip$/i.test(f))
        .map(f => ({ f, ver: f.match(/v(\d+\.\d+\.\d+)/i)[1], bytes: fs.statSync(path.join(zipDir, f)).size }))
        .filter(z => z.ver !== pkgVer)
        .sort((a, b) => b.ver.localeCompare(a.ver, undefined, { numeric: true }));
      const keep = zips.slice(0, keepN);
      const drop = zips.slice(keepN);
      if (keep.length) console.log(`  KEEP  历史分发包 ${keep.length} 个（--keep-zips ${keepN}）：${keep.map(z => 'v' + z.ver).join('、')}`);
      if (!drop.length) console.log('  OK    没有要清的孤儿分发包');
      for (const z of drop) {
        const mb = (z.bytes / 1048576).toFixed(1);
        if (!apply) { console.log(`  PLAN  孤儿分发包 v${z.ver}  ${mb}MB`); continue; }
        try {
          fs.rmSync(path.join(zipDir, z.f), { force: true });
          freed += z.bytes;
          console.log(`  DEL   孤儿分发包 v${z.ver}  ${mb}MB`);
        } catch (e) {
          remain.push(`v${z.ver}（${e.code || 'ERROR'}）`);
          console.log(`  FAIL  孤儿分发包 v${z.ver}  未删：${String(e.message || e).split(/\r?\n/)[0]}`);
        }
      }
    }

    // 2) .bak 残留（编辑器与脚本备份，可再生成）
    const baks = [];
    (function walkBak(d, depth) {
      if (depth > 4) return;
      let ents = [];
      try { ents = fs.readdirSync(d, { withFileTypes: true }); } catch { return; }
      for (const e of ents) {
        if (e.name === '.git' || e.name === 'node_modules') continue;
        const abs = path.join(d, e.name);
        if (e.isDirectory()) { walkBak(abs, depth + 1); continue; }
        if (/\.bak[-.][0-9T-]+/i.test(e.name)) { // 兼容两种命名：memory 的 .bak.<时间戳>、bootstrap 的 .bak-<时间戳>
          try { baks.push({ abs, bytes: fs.statSync(abs).size }); } catch { }
        }
      }
    })(ROOT || pkgRoot, 0);
    if (!baks.length) console.log('  OK    没有 .bak 残留');
    else if (!apply) {
      const mb = (baks.reduce((a, b) => a + b.bytes, 0) / 1048576).toFixed(1);
      console.log(`  PLAN  .bak 残留 ${baks.length} 个  ${mb}MB`);
    } else {
      let n = 0, bytes = 0;
      for (const b of baks) {
        try { fs.rmSync(b.abs, { force: true }); n++; bytes += b.bytes; } catch { remain.push(path.basename(b.abs)); }
      }
      freed += bytes;
      console.log(`  DEL   .bak 残留 ${n} 个  ${(bytes / 1048576).toFixed(1)}MB`);
    }

    // 3) 空目录壳：只清「验收截图占位」这类自动生成的空壳，不动业务目录
    const emptyShells = [];
    const reqRoot = path.join(ROOT || pkgRoot, 'harness', 'requirements');
    if (fs.existsSync(reqRoot)) {
      for (const req of fs.readdirSync(reqRoot, { withFileTypes: true })) {
        if (!req.isDirectory()) continue;
        const vis = path.join(reqRoot, req.name, 'visual');
        if (!fs.existsSync(vis)) continue;
        // 自底向上收：先 ref/shot 这种子壳，再 visual 本身
        const subs = fs.existsSync(vis) ? fs.readdirSync(vis, { withFileTypes: true }).filter(e => e.isDirectory()) : [];
        for (const sub of subs) {
          const p2 = path.join(vis, sub.name);
          if (fs.readdirSync(p2).length === 0) emptyShells.push(p2);
        }
        const leftSubs = fs.readdirSync(vis, { withFileTypes: true }).filter(e => e.isDirectory());
        if (fs.readdirSync(vis).length === 0 || (leftSubs.length === subs.length && subs.length > 0)) {
          // 只有全空才把 visual 也列入
          if (fs.readdirSync(vis).every(n2 => { const a = path.join(vis, n2); return fs.statSync(a).isDirectory() && fs.readdirSync(a).length === 0; })) {
            emptyShells.push(vis);
          }
        }
      }
    }
    if (!emptyShells.length) console.log('  OK    没有空目录壳');
    else if (!apply) console.log(`  PLAN  验收截图空目录壳 ${emptyShells.length} 个`);
    else {
      let n = 0;
      for (const d2 of emptyShells.sort((a, b) => b.length - a.length)) {
        try { if (fs.readdirSync(d2).length === 0) { fs.rmdirSync(d2); n++; } } catch { }
      }
      console.log(`  DEL   验收截图空目录壳 ${n} 个`);
    }
  }

  if (!apply) {
    console.log('\nDRY-RUN：加 --apply 执行清理');
    console.log('约定：验证沙箱一律建在 os.tmpdir() 下，命名 harness-sandbox-<用途>-<时间戳>');
    console.log('分发包默认保留当前版本 + 最近 1 个历史版本；--keep-zips 0 可一个不留');
    process.exit(0);
  }
  console.log(`\n结论：${remain.length ? 'FAIL' : 'PASS'}  释放≈${(freed / 1048576).toFixed(2)}MB`
    + (remain.length ? `  未清：${remain.join('、')}` : ''));
  process.exit(remain.length ? 2 : 0);
}

// ---------- help ----------
function help() {
  console.log(`harness engine v0.9

用法：
  node harness/engine/harness.mjs init "一句话需求" [--dir <项目>]
  node harness/engine/harness.mjs refresh [--force] [--dir <项目>]
  node harness/engine/harness.mjs layout [--dir <项目>]
  node harness/engine/harness.mjs status [REQ-ID] [--dir <项目>]
  node harness/engine/harness.mjs probe [--json] [--module-dir <装了 playwright 的项目>] [--dir <项目>]
  node harness/engine/harness.mjs inventory [--from <原型目录>] [--json] [--write] [--check] [--dir <项目>]
  node harness/engine/harness.mjs scaffold [--from miniprogram] [--states default,guest,logged] [--apply] [--dir <项目>]
  node harness/engine/harness.mjs handoff [REQ-ID] [--write] [--dir <项目>]
  node harness/engine/harness.mjs gate [REQ-ID] [--stage G1|G2|G3|G4|V] [--approve] [--dir <项目>]
  node harness/engine/harness.mjs msg --to <REQ|all> --text "<正文>" [--dir <项目>]
  node harness/engine/harness.mjs msg --read [--to <REQ|all>] [--dir <项目>]
  node harness/engine/harness.mjs visual [--dir <项目>]
  node harness/engine/harness.mjs clean [--apply] [--dir <项目>]

退出码：0 通过 / 1 用法或 IO 错 / 2 门禁或清理不通过 / 3 能力未实现
当前范围：init、refresh、layout、status、probe、inventory、scaffold、handoff、G1、G2、G3、G4、V、visual、clean。`);
}

if (cmd === 'init') cmdInit();
else if (cmd === 'refresh') cmdRefresh();
else if (cmd === 'layout') cmdLayout();
else if (cmd === 'status') cmdStatus();
else if (cmd === 'domain') cmdDomain();
else if (cmd === 'skill') cmdSkill();
else if (cmd === 'probe') cmdProbe();
else if (cmd === 'begin') cmdBegin();
else if (cmd === 'msg') cmdMsg();
else if (cmd === 'drift') cmdDrift();
else if (cmd === 'install-hook') cmdInstallHook();
else if (cmd === 'inventory') cmdInventory();
else if (cmd === 'scaffold') cmdScaffold();
else if (cmd === 'handoff') cmdHandoff();
else if (cmd === 'gate') cmdGate();
else if (cmd === 'visual') cmdVisual();
else if (cmd === 'clean') cmdClean();
else help();
