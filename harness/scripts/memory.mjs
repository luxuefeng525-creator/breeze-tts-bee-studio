#!/usr/bin/env node
// ai-workflow 长期记忆：结构化条目 + 候选池晋升。零依赖，跨平台。
//
// 记忆模型（两个文件，物理分离）：
//   候选池 inbox.md   —— 自动写入的唯一落点。门禁回流、开发教训都写这里。
//   正式记忆 long-term.md —— 经人确认后生效的条目，被 search 召回。
//
// 条目格式（管道分隔；旧格式 3 段仍可读，缺失字段补空）：
//   正式：日期 | 类别 | 作用域 | 事实 | 证据 | 适用边界 | 复审时间 | hits
//   候选：日期 | 类别 | 作用域 | 事实 | 证据 | 适用边界 | 复审时间
//   旧版：日期 | 类别 | 事实
//
// 晋升通道（ready / promote 共用；命中任一即可）：
//   常规 3 次复现 / 2 个作用域 / 30 天；快速 5 次 / 3 域 / 7 天；
//   担保 有证据的单条经验 + 人写 ≥8 字理由 + --confirm（高价值不等日历，但留担保人痕迹）
//
// 召回记账（OI-33）：brief/search 展示的正式条目记 hits，同日同条只算一次。
//   账本 harness/memory/recall-ledger.md 与正式记忆分离 —— 展示即召回，担保才晋升。
//
// 用法：
//   node scripts/memory.mjs add "<事实>" [--scope <域>] [--source <证据路径>] [--cat <类别>] [--days <复审天数>]
//   node scripts/memory.mjs brief "<这次要做什么>"   开工用：≤3 条最相关经验，带可信度（召回自动记账）
//   node scripts/memory.mjs promote <候选号> [--confirm] [--vouch "<≥8字理由>"]
//   node scripts/memory.mjs search "<关键词>" [--scope <域>] [--limit N]
//   node scripts/memory.mjs hit "<事实片段>"          精确登记「确实用过」（唯一命中才改）
//   node scripts/memory.mjs revise "<事实片段>" --fact "<新内容>" [--source <路径>] [--id <条目id>]  改写留旧值（--id 精确命中）
//   node scripts/memory.mjs reid [--dry-run]             给旧条目补身份证（幂等；改前自动备份）
//   node scripts/memory.mjs history [fact|"片段"]     查改写历史（默认全部）
//   node scripts/memory.mjs prune                      复审 + 按内容过期（证据路径消失即提前待复审）
//   node scripts/memory.mjs index | stats | ready
//   node scripts/memory.mjs --selftest                临时目录跑召回防刷与担保晋升
// 跨项目：--file <正式记忆路径> --inbox <候选池路径>；默认在 <root>/harness/memory/ 下

import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { spawnSync, spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);
const optVal = n => { const i = args.indexOf(n); return i >= 0 && args[i + 1] !== undefined ? args[i + 1] : null; };
const hasFlag = n => args.includes(n);

const ROOT = optVal('--root') || process.cwd();
const MEM_FILE = optVal('--file') || path.join(ROOT, 'harness', 'memory', 'long-term.md');
const INBOX_FILE = optVal('--inbox') || path.join(ROOT, 'harness', 'memory', 'inbox.md');
const LEDGER_FILE = path.join(ROOT, 'harness', 'memory', 'recall-ledger.md');
const HISTORY_FILE = path.join(ROOT, 'harness', 'memory', 'history.md');

const cmd = args[0];
const VALUE_FLAGS = ['--file', '--inbox', '--root', '--scope', '--source', '--cat', '--days', '--limit', '--vouch', '--fact', '--id', '--reason'];
const positional = args.slice(1).filter((a, i) => {
  if (a.startsWith('--')) return false;
  const prev = args[i];
  return !VALUE_FLAGS.some(f => prev === f);
});

const DEFAULT_REVIEW_DAYS = 180;
const MAX_ENTRIES = 500;
const PROMOTE_MIN_RECURRENCES = 3;
const PROMOTE_MIN_SCOPES = 2;
const PROMOTE_MIN_DAYS = 30;
const VOUCH_MIN_CHARS = 8;

// 晋升双通道，任一满足即可。常规通道挡噪声；快速通道让「高频 + 广覆盖」的提前上位
// —— 用更强的证据换更短的时间，不是简单放宽门槛。
// ready 与 promote 必须共用这两处判据，否则会出现「ready 说可以、promote 拒绝」。
const PROMOTE_CHANNELS = [
  { name: '常规', rec: PROMOTE_MIN_RECURRENCES, scope: PROMOTE_MIN_SCOPES, days: PROMOTE_MIN_DAYS },
  { name: '快速', rec: 5, scope: 3, days: 7 }
];
/** 命中哪条通道；都不满足返回 null */
function matchChannel(rec, scope, age) {
  return PROMOTE_CHANNELS.find(ch => rec >= ch.rec && scope >= ch.scope && age !== null && age >= ch.days) || null;
}
/** 某条通道还差什么，写成能直接给人看的中文 */
function gapsOf(ch, rec, scope, age) {
  const g = [];
  if (rec < ch.rec) g.push('还差 ' + (ch.rec - rec) + ' 次复现');
  if (scope < ch.scope) g.push('还差 ' + (ch.scope - scope) + ' 个作用域');
  if (age === null || age < ch.days) g.push('还差 ' + (ch.days - (age === null ? 0 : age)) + ' 天');
  return g;
}

function ensureDir(f) { const d = path.dirname(f); if (!fs.existsSync(d)) fs.mkdirSync(d, { recursive: true }); }
function readLines(f) { try { return fs.readFileSync(f, 'utf8').split(/\r?\n/); } catch { return []; } }
/** 文件的行尾风格（CRLF / LF）—— 写回时必须还原。
 *  整体重写若一律用 LF，Windows 上会把原文件的 CRLF 全换成 LF，
 *  造成「只是加了个 id」却看到满文件行尾变化。 */
function eolOf(f) {
  try {
    const head = fs.readFileSync(f, { encoding: 'utf8' }).slice(0, 4096);
    return head.includes('\r\n') ? '\r\n' : '\n';
  } catch { return '\n'; }
}
/** 用指定行尾拼接行数组 */
const joinLines = (lines, eol) => lines.join(eol);

// ---------- 跨进程锁 ----------
// 借鉴上游 dsh-memory-evolve 的 lib/store.js withLock（只借鉴机制，不引代码）。
// 为什么需要：memory.mjs 有多处「整体重写」记忆文件（writeFileSync），两个进程同时写
// 会互相覆盖 —— 后写的把先写的整份替换掉，条目丢失或 hits 计数回退。
//
// 机制：openSync(lockPath,'wx') 原子创建（已存在则 EEXIST）→ 锁文件里记 pid
//      → 等不到就按 stale 判定接管（先查 pid 存活，再看 mtime 兜底）。

// 等锁上限。可用 MEMORY_LOCK_TIMEOUT_MS 覆盖（自测用短超时，不必真等满 5 秒）。
const LOCK_TIMEOUT_MS = (() => {
  const v = parseInt(process.env.MEMORY_LOCK_TIMEOUT_MS || '', 10);
  return Number.isInteger(v) && v > 0 ? v : 5000;
})();
const LOCK_POLL_MS = 25;            // 轮询间隔
let lockDepth = 0;                  // 进程内重入计数（同进程嵌套不重复加锁）

/** 同步睡眠 —— Atomics.wait 不占事件循环（本脚本是同步的，不能用 setTimeout） */
function sleepSync(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

/** 锁是否已失效（持有者进程已死）。
 *  先查 pid 存活 —— 只看 mtime 的话，崩溃后要干等很久。
 *  EPERM（存在但无权限）也算**活进程** → 锁有效，不能抢别人正在写的锁。
 *  pid 缺失或非法 → 判 stale（锁内容写坏时不该干等）。
 *
 *  注意：锁的发布方式是「先在 staging 写好 owner，再 rename 原子发布」——
 *  不能「先创建空内容再填」：Windows 上 writeFileSync(flag:'wx') / mkdir 都会先建出
 *  空内容，别的进程在这个窗口里读到空内容 → 误判 stale → 抢锁（实测会丢条目）。 */
function readOwner(lockDir) {
  let content = '';
  try { content = fs.readFileSync(path.join(lockDir, 'owner'), 'utf8').trim(); } catch { return null; }
  if (!content) return null;
  const [pidStr, token] = content.split(/\s+/);
  const pid = parseInt(pidStr, 10);
  return { pid: Number.isInteger(pid) ? pid : null, token: token || null, raw: content };
}
function isStaleLock(lockDir) {
  // 锁目录不存在 = 没人持锁（上一家刚释放，或还没人抢到），**不是 stale**。
  // 判 stale 会让接管分支去 rmSync —— 若这期间别人刚 rename 发布，活锁会被删掉，
  // 两个进程同时进临界区、后写覆盖先写（实测丢 [promoted] 标记）。
  if (!fs.existsSync(lockDir)) return false;
  const owner = readOwner(lockDir);
  if (!owner || !owner.pid || owner.pid <= 0) {
    // 锁内容不是 pid —— 写坏了或不是我们写的锁。保守起见按 stale 处理：
    // 干等不会解决问题（内容不会自己变好），而锁本来就是保护性机制。
    return true;
  }
  try { process.kill(owner.pid, 0); return false; }          // 活进程 → 锁有效
  catch (e) { if (e.code === 'EPERM') return false; }        // 存在但无权限 → 也是活进程
  // ESRCH → 进程没了 → stale
  return true;
}

/** 拿锁执行 fn，结束必释放。超时抛错（不静默跳过锁 —— 跳过等于没锁）。
 *
 *  锁的发布必须是原子的，且**发布时内容就已就绪**：
 *  mkdir 后再写 owner 会有窗口（别人看到锁目录却读不到 owner → 误判 stale → 抢锁，实测会丢条目）。
 *  所以：先在临时目录写好 owner，再用 rename 原子发布到锁路径 ——
 *  rename 到已存在的目录会失败（EPERM/EEXIST），正好当作「锁被占」的信号。 */
function withLock(lockDir, fn) {
  if (lockDepth > 0) { lockDepth++; try { return fn(); } finally { lockDepth--; } }  // 同进程重入
  ensureDir(path.dirname(lockDir));
  const deadline = Date.now() + LOCK_TIMEOUT_MS;
  const staging = lockDir + '.tmp-' + process.pid + '-' + Date.now();
  const myToken = crypto.randomBytes(8).toString('hex');
  for (;;) {
    // 准备带 owner 的暂存目录（内容先就绪，再发布）
    try {
      fs.rmSync(staging, { recursive: true, force: true });
      fs.mkdirSync(staging);
      fs.writeFileSync(path.join(staging, 'owner'), `${process.pid} ${myToken}`, 'utf8');
    } catch (e) {
      try { fs.rmSync(staging, { recursive: true, force: true }); } catch { /* ignore */ }
      throw e;
    }
    // 原子发布：成功即拿到锁；失败说明别人占着
    try {
      fs.renameSync(staging, lockDir);
      break;
    }
    catch (e) {
      try { fs.rmSync(staging, { recursive: true, force: true }); } catch { /* ignore */ }
      const busy = ['EEXIST', 'EPERM', 'ENOTEMPTY', 'EACCES'].includes(e.code);
      if (!busy) throw e;
      // 超时检查放最前 —— 否则下面 stale 分支的 continue 会绕过它，变成无上限热自旋
      if (Date.now() >= deadline) {
        const holder = (() => { try { return fs.readFileSync(path.join(lockDir, 'owner'), 'utf8').trim(); } catch { return '?'; } })();
        throw new Error(`获取记忆库锁超时（${LOCK_TIMEOUT_MS}ms，持有者 ${holder}）—— 可能有另一个进程正在写记忆库，或它崩了但锁没清`);
      }
      if (isStaleLock(lockDir)) {
        // 接管前必须复核：判定 stale 之后、删除之前，锁可能已被别人接管（内容易主）。
        // 只有「内容仍与判定时一致」才删 —— 变了就放弃，回去重试（否则会删掉刚发布的活锁）。
        const victim = readOwner(lockDir);
        if (!victim) { sleepSync(LOCK_POLL_MS); continue; }    // 锁已消失，回去重试
        if (victim.pid && victim.pid > 0) {
          let alive = false;
          try { process.kill(victim.pid, 0); alive = true; } catch (e) { if (e.code === 'EPERM') alive = true; }
          if (alive) { sleepSync(LOCK_POLL_MS); continue; }    // 易主成活锁，不删
          const now = readOwner(lockDir);
          if (!now || now.raw !== victim.raw) { sleepSync(LOCK_POLL_MS); continue; }  // 内容易主，不删
        }
        try { fs.rmSync(lockDir, { recursive: true, force: true }); } catch { /* 别人可能刚清了 */ }
        sleepSync(LOCK_POLL_MS);   // 避免连续 rm 失败时热自旋
        continue;
      }
      sleepSync(LOCK_POLL_MS);
    }
  }
  lockDepth = 1;
  try { return fn(); }
  finally {
    lockDepth = 0;
    try { fs.rmSync(lockDir, { recursive: true, force: true }); } catch { /* 已被人清掉 */ }
  }
}

/** 记忆库锁路径（与记忆文件同目录） */
/** 记忆库锁路径 —— **统一一把**，不按被写文件的目录推导。
 *  理由：正式记忆与候选池是同一份记忆库的两个文件，必须共用一把锁；
 *  若按文件目录推导，--file 与 --inbox 分目录时就会变成两把锁，候选池仍会互相覆盖。 */
const lockPathFor = () => path.join(ROOT, 'harness', 'memory', '.memory.lock');

/** withLock 的命令级包装：拿不到锁就友好报错退出，不要抛栈给用户看。
 *  静默跳过锁等于没锁 —— 所以这里必须硬失败。
 *  区分两类错误：**抢锁失败**（LOCK-FAILED）与**临界区内部异常**（原样抛出，别伪装成锁问题）。 */
function withLockOrExit(lockPath, fn) {
  try { return withLock(lockPath, fn); }
  catch (e) {
    const msg = e && e.message ? e.message : String(e);
    if (!/^获取记忆库锁/.test(msg)) throw e;   // 临界区内部的错误，原样抛
    console.error('LOCK-FAILED ' + msg);
    console.error('  另一个进程可能正在写记忆库。等它写完再重试；若确认没有进程在写，删掉 ' + path.basename(lockPath) + ' 后重跑。');
    process.exit(1);
  }
}

/** 本地日期（不用 toISOString —— 它取 UTC，本地凌晨会差一天） */
function localDate(d = new Date()) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function today() { return localDate(); }
function daysFromNow(n) { const d = new Date(); d.setDate(d.getDate() + n); return localDate(d); }
/** 两个 YYYY-MM-DD 之间的整天数（按本地零点算，避免时区偏移） */
function daysBetween(fromStr, toStr) {
  const a = Date.parse(fromStr + 'T00:00:00');
  const b = Date.parse(toStr + 'T00:00:00');
  if (isNaN(a) || isNaN(b)) return null;
  return Math.round((b - a) / 864e5);
}
function daysSince(dateStr) { return daysBetween(dateStr, today()); }

/** 行尾状态标记（可能多个）：[stale] / [stale-content] / [promoted] / [dropped]。
 *  改写与召回必须原样带回，否则提醒会消失。
 *  [promoted] 是「已晋升」、[dropped] 是「已判定不晋升」—— 都必须一起剥离，
 *  否则它会粘在最后一个字段（复审期）上，把这一行误判成形状异常。 */
const tailFlags = line => (String(line).match(/(\s*\[(?:stale|stale-content|promoted|dropped)\]\s*)+$/) || [''])[0];

/** 正文里的管道符转义 —— 格式是单行 `A | B | C`，正文若含 `|` 会让字段整体错位。
 *  约定：写入时正文里的 `|` 写成 `\|`，解析时还原。不换分隔符（会牵连全部既有条目）。 */
const escapeField = s => String(s == null ? '' : s).replace(/\|/g, '\\|');
const unescapeField = s => String(s == null ? '' : s).replace(/\\\|/g, '|');

/** 按分隔符切分，但跳过被转义的 \| —— 转义的管道符不算分隔符 */
function splitFields(raw) {
  const out = [];
  let cur = '';
  for (let i = 0; i < raw.length; i++) {
    const ch = raw[i];
    if (ch === '\\' && raw[i + 1] === '|') { cur += '\\|'; i++; continue; }
    if (ch === '|') { out.push(cur.trim()); cur = ''; continue; }
    cur += ch;
  }
  out.push(cur.trim());
  return out.map(unescapeField);
}

/** 形状校验：正式条目 7 段（+可选 hits 第 8 段），旧格式 ≤3 段。
 *  段数不对说明这条被改坏或正文漏转义 —— 标 malformed，不静默接受。
 *  静默接受的后果：整条左移，证据路径被读成普通文本，prune 会把正常记忆误判成 stale-content。
 *
 *  段数对 ≠ 字段对：漏掉中间一段、后面又补了别的，段数照样是 8。
 *  所以还要逐字段看形状 —— 漏段的典型特征是「适用边界」位跑进了日期。
 *
 *  判断依据是「每个字段长成它该长的样子」，不是「读起来顺不顺」。 */
const FORMAL_FIELDS = 7;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
/** 空位占位符：半角 - 与中文破折号 — 都算「无」 */
const isBlank = s => !s || s === '-' || s === '—' || s === '–' || s === 'N/A';

function shapeProblem(parts) {
  if (parts.length <= 3) return null;                       // 旧格式，兼容
  if (parts.length !== FORMAL_FIELDS && parts.length !== FORMAL_FIELDS + 1) {
    return `段数 ${parts.length}，正式条目应为 ${FORMAL_FIELDS} 或 ${FORMAL_FIELDS + 1} 段`;
  }
  const body = parts.length === FORMAL_FIELDS + 1 ? parts.slice(0, FORMAL_FIELDS) : parts;
  if (parts.length === FORMAL_FIELDS + 1 && !/^\d+$/.test(parts[FORMAL_FIELDS])) {
    return `段数 ${parts.length}，第 8 段应为 hits（数字），实际是「${parts[FORMAL_FIELDS]}」`;
  }
  const [date, cat, scope, fact, source, bound, review] = body;
  // 逐字段形状：日期必须是日期；复审期必须是日期；证据位必须是路径/URL/空位。
  // 任一条不满足 → 这行的字段已经错位（漏段或正文含未转义的 |），不能按位置解构。
  if (!DATE_RE.test(date)) return `第 1 段应为日期（YYYY-MM-DD），实际是「${date}」—— 字段可能已错位`;
  if (!cat) return '第 2 段（类别）为空';
  if (!scope) return '第 3 段（作用域）为空';
  if (!fact) return '第 4 段（事实）为空';
  if (!isBlank(review) && !DATE_RE.test(review)) {
    return `第 7 段（复审期）应为日期，实际是「${review}」—— 字段可能已错位`;
  }
  // 适用边界位跑进日期 = 典型的漏段（左移一位）。真正的边界是描述句，不是日期。
  if (!isBlank(bound) && DATE_RE.test(bound)) {
    return `第 6 段（适用边界）是日期「${bound}」—— 多半是漏了一段导致字段左移`;
  }
  // 证据位：空位、URL、含路径分隔符的路径都算合法；裸文件名也允许（无空格）。
  // 只排除「含空格的一整句中文」—— 那是描述句，不是路径。
  if (!isBlank(source) && !/^https?:\/\//i.test(source) && /[\s一-龥]/.test(source) && !/[\\/]/.test(source)) {
    return `第 5 段（证据路径）是描述句「${source}」—— 字段可能已错位`;
  }
  return null;
}

/** 条目身份证（entryId）：行首 `[id:xxxxxxxx] `，8 位小写 hex。
 *  借鉴上游 dsh-memory-evolve 的 lib/sync/entryid.js（只借鉴机制，不引代码）。
 *  存在的理由：时间戳是程序盖的、replace 会刷新，不能当身份；整条文本在两台设备
 *  各自改同一条时无法判定「谁是谁」。纯随机 8 位 hex —— 不编码时间/内容/机器，不可反推。 */
const ENTRY_ID_RE = /^\[id:([0-9a-f]{8})\]\s*/;
const genEntryId = () => crypto.randomBytes(4).toString('hex');

/** 解析一行条目 → 结构化对象；旧格式补空字段 */
function parseLine(line) {
  const raw = (line || '').trim();
  // 注释行、引言行、HTML 注释都不是条目
  if (!raw || raw.startsWith('#') || raw.startsWith('>') || raw.startsWith('<!--')) return null;
  // id 前缀先剥离 —— 它在最前面，与尾标记（最后面）互不干扰，但顺序固定下来免得以后改串
  const idMatch = ENTRY_ID_RE.exec(raw);
  const id = idMatch ? idMatch[1] : null;
  const rawNoId = idMatch ? raw.slice(idMatch[0].length) : raw;
  // 尾标记（[stale] / [stale-content]）先剥离，再判形状 ——
  // 否则「3 [stale-content]」这一段既不是纯数字也不是空位，正常条目会被误判成形状异常。
  const flags = tailFlags(rawNoId);
  const body0 = flags ? rawNoId.slice(0, rawNoId.length - flags.length).trim() : rawNoId;
  const parts = splitFields(body0);
  const hitsIdx = parts.length - 1;
  let hits = 0;
  let body = parts;
  if (parts.length >= 8 && /^\d+$/.test(parts[hitsIdx])) { hits = parseInt(parts[hitsIdx], 10); body = parts.slice(0, -1); }
  const [date = '', cat = '', scope = '', fact = '', source = '', bound = '', review = ''] = body;
  // 旧格式（3 段）：日期 | 类别 | 事实
  const legacy = body.length <= 3;
  // 形状校验：段数不对 → 标 malformed。这条不能再按位置解构（会整体左移）。
  const malformed = shapeProblem(parts);
  // 往返无损校验：段数可能合法但字段在读写之间挪位（正文含未转义的 | 时最典型）。
  // 这一层不能省 —— 它是上游 isCanonical 的真正含义，不是「格式好不好看」。
  let rtProblem = null;
  // 只对 8 段正式条目做往返校验 —— formatEntry 固定输出 8 段（含 hits），
  // 候选池的 7 段条目往返必然多一段，比出来的差异是格式差异不是字段错位。
  if (!malformed && !legacy && parts.length === 8) {
    const rebuilt = formatEntry({ date, cat, scope, fact, source, bound, review, hits }) + flags;
    const again = splitFields(flags ? rebuilt.slice(0, rebuilt.length - flags.length).trim() : rebuilt);
    if (again.length !== parts.length) {
      rtProblem = `往返后段数变了（${parts.length} → ${again.length}）—— 正文里的 | 可能没转义`;
    } else {
      for (let i = 0; i < again.length; i++) {
        if (again[i] !== parts[i]) { rtProblem = `往返后第 ${i + 1} 段变了（「${parts[i]}」→「${again[i]}」）—— 字段在读写之间挪位了`; break; }
      }
    }
  }
  const problem = malformed || rtProblem;
  const safe = problem ? { date, cat, scope: scope || '-', fact: fact || body[body.length - 1] || '', source: '-', bound: '-', review: '' } : {};
  return {
    raw,
    flags,
    id,
    date, cat,
    scope: legacy ? '-' : (scope || '-'),
    fact: legacy ? (body[2] || '') : fact,
    source: legacy ? '-' : (source || '-'),
    bound: legacy ? '-' : (bound || '-'),
    review: legacy ? '' : review,
    hits,
    legacy,
    malformed: problem || null,
    // 形状不对的条目，source 一律不给（否则 prune 会拿错位字段去查路径，误判 stale-content）
    ...safe,
    promoted: /\[promoted\]/.test(raw),
    // 已判定不晋升：保留在候选池留痕，但不再参与 ready/stats 的待办计数
    dropped: /\[dropped\]/.test(raw)
  };
}

/** 写出一行条目（8 字段正式格式）—— 正文里的 | 必须转义，否则写出去就错位。
 *  有 id 时前置 `[id:xxxxxxxx] `（无 id 的旧条目照旧输出，向后兼容）。 */
function formatEntry(e) {
  const body = [e.date, e.cat, e.scope, e.fact, e.source, e.bound, e.review || daysFromNow(DEFAULT_REVIEW_DAYS), e.hits ?? 0]
    .map(escapeField).join(' | ');
  return e.id ? `[id:${e.id}] ${body}` : body;
}

/** 往返校验：写回时必须与原行语义一致。
 *  借鉴上游 store.js:349 的 isCanonical（serialize(parse(text)) === text）——
 *  目的不是「格式漂亮」，是「字段不会在读写之间悄悄挪位」。 */
function roundTripOk(entry, rawLine) {
  if (!entry || entry.legacy) return true;
  if (entry.malformed) return false;
  const back = parseLine(formatEntry(entry));
  if (!back || back.malformed) return false;
  return back.date === entry.date && back.fact === entry.fact && back.source === entry.source;
}

/** 事实指纹：账本只存指纹，不存全文 —— 账本是记账用的，不是第二份记忆 */
const factKey = fact => crypto.createHash('sha256').update(String(fact)).digest('hex').slice(0, 16);

/** 旧值进历史：一行一条，带改写时间与原因；历史文件只追加，不回写条目本身 */
const logHistory = (oldLine, reason) => {
  ensureDir(HISTORY_FILE);
  fs.appendFileSync(HISTORY_FILE, `${today()} | ${reason || '改写'} | 旧值：${oldLine.trim()}\n`, 'utf8');
};

/** 召回记账（OI-33）：同日同一条只算一次。返回本次新增的命中条数。
 *  只给正式记忆加 hits；候选池还没晋升，不配计数。
 *
 *  brief/search 是**只读意图** —— 记账失败（拿不到锁等）不能让它整个失败：
 *  降级为警告，输出照常，退出码照常。 */
function recordRecall(shownFacts) {
  const facts = [...new Set((shownFacts || []).filter(Boolean))];
  if (!facts.length) return 0;
  // 整段持锁：读 → 加 hits → 写回。只在写时加锁不够 ——
  // 读到的是旧快照，写回会把别人刚加的条目覆盖掉。
  try {
    return withLock(lockPathFor(), () => {
    const ledger = readLines(LEDGER_FILE).filter(l => /^\d{4}-\d{2}-\d{2}\s*\|/.test(l));
    const todayKeys = new Set(ledger.filter(l => l.split('|')[0].trim() === today()).map(l => l.split('|')[1].trim()));
    const fresh = facts.filter(f => !todayKeys.has(factKey(f)));
    if (!fresh.length) return 0;
    const memRaw = readLines(MEM_FILE);
    const hitFacts = new Set(fresh);
    let changed = false;
    memRaw.forEach((line, i) => {
      const e = parseLine(line);
      if (!e || e.legacy || !hitFacts.has(e.fact)) return;
      // 形状异常的条目不写回 —— formatEntry 用的是解析后的字段，
      // 而 malformed 条目的字段已被占位值覆盖（source/bound/review/hits 都会丢）。
      // 一次 brief 召回就销毁真实证据路径，是不可逆损失。
      if (e.malformed) return;
      memRaw[i] = formatEntry({ ...e, hits: e.hits + 1 }) + (e.flags || tailFlags(line));
      changed = true;
    });
    if (!changed) return 0;
    ensureDir(MEM_FILE);
    fs.writeFileSync(MEM_FILE, joinLines(memRaw, eolOf(MEM_FILE)), 'utf8');
    ensureDir(LEDGER_FILE);
    const newLines = fresh.filter(f => memRaw.some(l => { const e = parseLine(l); return e && e.fact === f; }))
      .map(f => `${today()} | ${factKey(f)}`);
    if (newLines.length) fs.appendFileSync(LEDGER_FILE, newLines.join('\n') + '\n', 'utf8');
    return newLines.length;
    });
  }
  catch (e) {
    // 记账失败不影响只读命令的输出 —— 降级为警告
    console.error(`  ⚠ 召回记账没成功（${e && e.message ? e.message : e}）—— 不影响本次召回结果`);
    return 0;
  }
}

/** 关键词命中分词（中英混合）：英文按词，中文按二元组 */
function tokens(s) {
  const out = [];
  const low = (s || '').toLowerCase();
  for (const m of low.matchAll(/[a-z0-9_][a-z0-9_.-]{1,}/g)) out.push(m[0]);
  for (const seg of low.match(/[\u4e00-\u9fff]{2,}/g) || []) {
    for (let i = 0; i + 2 <= seg.length; i++) out.push(seg.slice(i, i + 2));
  }
  return out;
}

function scoreEntry(e, kws) {
  const hay = `${e.fact} ${e.cat} ${e.scope} ${e.bound}`.toLowerCase();
  let hit = 0;
  for (const k of kws) if (hay.includes(k)) hit++;
  let score = hit * 3 + Math.min(e.hits, 10) * 0.5;
  if (e.source && !isBlank(e.source)) score += 1;
  if (e.review && daysSince(e.review) <= 0) score += 1;
  if (e.promoted) score += 0.5;
  return { score, hit };
}

if (cmd === 'add') {
  const fact = positional.join(' ').trim();
  if (!fact) { console.error('用法：memory.mjs add "<事实>" [--scope <域>] [--source <证据路径>]'); process.exit(1); }
  const scope = optVal('--scope') || 'global';
  const cat = optVal('--cat') || '经验';
  const source = optVal('--source') || '-';
  const days = parseInt(optVal('--days') || String(DEFAULT_REVIEW_DAYS), 10);

  // ---- 写入校验（OI-12）----
  // 记忆库最容易烂的方式不是「写错」，是「什么都往里写」：一句口号、一个没证据的结论，
  // 进来之后就再没人能复核，还占着 BRIEF 的位置。所以宁可在写入时拒绝。
  const problems = [];
  if (fact.length < 8) {
    problems.push(`事实太短（${fact.length} 字）—— 写清「什么情况下该怎么做」，不是写标题`);
  }
  if (cat !== '事件' && isBlank(source)) {
    problems.push('经验类必须给 --source（证据路径或 URL）—— 没有证据的经验，下次没法复核也没法推翻');
  }
  if (!isBlank(source) && !/^https?:\/\//i.test(source)) {
    const p = path.isAbsolute(source) ? source : path.join(ROOT, source);
    if (!fs.existsSync(p)) {
      problems.push(`证据路径不存在（${source}）—— 写一个不存在的路径，等于制造假证据`);
    }
  }
  if (problems.length) {
    console.error('拒绝写入：');
    for (const p of problems) console.error('  - ' + p);
    console.error('这条会污染候选池：它会被 brief 召回、占位置，却提供不了任何可复核的东西。');
    process.exit(1);
  }

  // 整段持锁：读候选池 → 去重判断 → 追加。
  // 注意：process.exit 不能放在锁回调里 —— 那样会跳过释放，锁文件残留。
  // 所以回调只返回结果，退出与打印都在锁外。
  const outcome = withLockOrExit(lockPathFor(), () => {
    ensureDir(INBOX_FILE);
    const existing = readLines(INBOX_FILE).map(parseLine).filter(Boolean);
    // 复现要能累计：同一天同作用域重复写入按「同日只记一次」去重；
    // 换个日子再遇到同一条，追加一行——这才叫「又碰到一次」。
    const sameDay = existing.some(e => e.fact === fact && e.scope === scope && e.date === today());
    if (sameDay) return { skipped: true };
    const repeats = existing.filter(e => e.fact === fact).length;
    // 正文里的 | 必须转义 —— 格式是单行管道分隔，不转义写出去就字段错位
    const entry = [today(), cat, scope, fact, source, '-', daysFromNow(days)].map(escapeField).join(' | ');
    // 写前自检：解析回来必须字段一致（往返校验），不一致就拒绝写。
    // 两种失败要分开报：形状不合规 vs 往返不一致 —— 混报会让人以为转义坏了，其实是自己没按格式填。
    const probe = parseLine(entry);
    if (!probe) return { error: '写入后无法解析成条目。', entry };
    if (probe.malformed) return { error: `条目形状不合规（${probe.malformed}）`, entry };
    if (probe.fact !== fact || probe.source !== source) {
      return { error: '写入后解析回来字段不一致（往返校验不通过）', entry };
    }
    fs.appendFileSync(INBOX_FILE, entry + '\n', 'utf8');
    return { entry, repeats };
  });
  if (outcome.skipped) {
    console.log('SKIP 今天已记过同作用域的相同事实（换一天再遇到会自动 +1）');
    process.exit(0);
  }
  if (outcome.error) {
    console.error(`拒绝写入：${outcome.error}`);
    console.error(`  ${outcome.entry}`);
    if (/形状不合规/.test(outcome.error)) console.error('  修法：正文里的 | 写成 \\| ，或补齐缺失的字段段。');
    process.exit(1);
  }
  console.log('ADDED(候选池) ' + outcome.entry + (outcome.repeats ? `（第 ${outcome.repeats + 1} 次遇到）` : ''));
  console.log('提示：晋升正式记忆需满足三条件并由人确认 —— memory.mjs ready 查看进度，promote <候选号> --confirm');
}

else if (cmd === 'ready') {
  const raw = readLines(INBOX_FILE);
  const parsed = raw.map(parseLine);
  const entries = parsed.filter(Boolean).filter(e => !e.promoted && !e.dropped);
  const events = entries.filter(e => e.cat === '事件');
  const lessons = entries.filter(e => e.cat !== '事件');
  const groups = new Map();
  for (const e of lessons) {
    if (!groups.has(e.fact)) groups.set(e.fact, []);
    groups.get(e.fact).push(e);
  }
  // 候选号必须与 promote 用的一致 —— promote 按「候选池原始行号」定位，
  // 这里也用原始行号（1 起），否则真实 inbox（含注释头与事件行）下编号会错位
  const lineNo = e => parsed.indexOf(e) + 1;
  if (events.length) {
    console.log(`待提炼事件 ${events.length} 条（门禁自动回流，需人工提炼成可复用经验后才参与晋升）：`);
    for (const e of events.slice(0, 10)) console.log(`  · [${e.scope}] ${e.fact}${e.source && !isBlank(e.source) ? ' — ' + e.source : ''}`);
    if (events.length > 10) console.log(`  … 另有 ${events.length - 10} 条`);
  }
  // 复审提醒（OI-12）：到期不等于失效，但不看就会一直用着过期结论
  const overdue = entries.filter(e => e.review && daysSince(e.review) > 0);
  if (overdue.length) {
    console.log(`\n复审提醒 ${overdue.length} 条已过复审期 —— prune 会标 [stale]；确认后改写，或写清为什么仍然成立`);
    for (const e of overdue.slice(0, 3)) console.log(`  · ${e.date} ${e.fact.slice(0, 40)}…（复审日 ${e.review}）`);
  }

  const ready = [], near = [], early = [];
  for (const [fact, list] of groups) {
    const rec = list.length;
    const scopes = new Set(list.map(e => e.scope));
    const scope = scopes.size;
    const age = daysSince(list.map(e => e.date).sort()[0]);
    const hit = matchChannel(rec, scope, age);
    if (hit) { ready.push({ fact, list, rec, scope, age, ch: hit }); continue; }
    // 没满足任何通道：找「最接近」的那条。先比差几条，再比差多少（按各自目标归一化求和）——
    // 只看 gaps.length 会让「差 1 次复现」和「差 29 天」被当成一样近。
    const score = (ch) => {
      let t = 0;
      if (rec < ch.rec) t += (ch.rec - rec) / ch.rec;
      if (scope < ch.scope) t += (ch.scope - scope) / ch.scope;
      if (age === null || age < ch.days) t += (ch.days - (age === null ? 0 : age)) / ch.days;
      return t;
    };
    const best = PROMOTE_CHANNELS
      .map(ch => ({ ch, gaps: gapsOf(ch, rec, scope, age), score: score(ch) }))
      .sort((a, b) => a.gaps.length - b.gaps.length || a.score - b.score)[0];
    const item = { fact, list, rec, scope, age, ch: best.ch, gaps: best.gaps };
    (best.gaps.length === 1 ? near : early).push(item);
  }

  const pad = n => 'C' + String(n).padStart(3, '0');

  if (ready.length) {
    console.log('READY ' + ready.length + ' 条（满足晋升条件，可 promote）：');
    for (const it of ready) {
      console.log('  READY ' + pad(lineNo(it.list[0])) + ' [' + it.ch.name + '通道 ' + it.rec + ' 次复现 / ' + it.scope + ' 个作用域 / ' + it.age + ' 天]');
      console.log('     事实：' + it.fact);
      console.log('     证据：' + (it.list.map(e => e.source).filter(x => !isBlank(x)).join(' ; ') || '无'));
      console.log('     晋升命令：node harness/scripts/memory.mjs promote ' + pad(lineNo(it.list[0])) + ' --confirm');
    }
  }
  if (near.length) {
    console.log('NEAR ' + near.length + ' 条（只差一条，补上就能晋升）：');
    for (const it of near) {
      console.log('  NEAR  ' + pad(lineNo(it.list[0])) + ' [' + it.ch.name + '通道] 缺：' + it.gaps[0]);
      console.log('     事实：' + String(it.fact).slice(0, 60));
    }
  }
  if (early.length) {
    console.log('EARLY ' + early.length + ' 条（还差两条以上）：');
    for (const it of early) {
      console.log('  EARLY ' + pad(lineNo(it.list[0])) + ' [' + it.ch.name + '通道] ' + it.gaps.join('、'));
    }
  }
  if (!ready.length && !near.length && !early.length && !events.length) {
    console.log('NONE 候选池为空 —— 做事时用 memory.mjs add 记一条，或让门禁自动回流');
  } else if (!ready.length) {
    console.log(near.length
      ? '提示：NEAR 那几条补齐就够，不用干等 30 天（快速通道 5 次复现 / 3 个作用域 / 7 天）。'
      : '提示：还没有接近门槛的。同类任务多做几次、跨两个以上作用域再记，才有机会晋升。');
  }
}

else if (cmd === 'promote') {
  const idRaw = positional[0] || '';
  const m = /^c?(\d+)$/i.exec(idRaw);
  if (!m) { console.error('用法：memory.mjs promote <候选号> [--confirm]（候选号见 ready 或候选池行号）'); process.exit(1); }
  const idx = parseInt(m[1], 10) - 1;
  // 整段持锁：读候选池 → 判定 → 写正式记忆 → 标记候选已晋升。
  // promote 要改两个文件（正式记忆 + 候选池），必须在同一把锁里完成 ——
  // 否则另一个 promote 插进来，两个进程会互相覆盖对方的候选标记。
  // 回调只返回结果，不在锁内 process.exit（那会跳过释放锁）。
  const pres = withLockOrExit(lockPathFor(), () => {
    const raw = readLines(INBOX_FILE);
    const parsed = raw.map(parseLine);
    const target = parsed[idx];
    if (!target || !target.fact) return { err: `候选 C${m[1]} 不存在` };
    if (target.cat === '事件') {
      return { err: 'REJECT 事件类条目不可直接晋升 —— 先人工提炼成可复用经验，再用 memory.mjs add 写入（类别不要用「事件」）' };
    }
    const same = parsed.filter(e => e && e.fact === target.fact && e.cat !== '事件');
    const scopes = new Set(same.map(e => e.scope));
    const oldest = same.map(e => e.date).sort()[0];
    const age = daysSince(oldest);
    // 判据必须与 ready 一致 —— 用同一个 matchChannel，避免「ready 说可以、promote 拒绝」
    let hit = matchChannel(same.length, scopes.size, age);
    const vouch = (optVal('--vouch') || '').trim();
    if (!hit && vouch) {
      // 人工担保：用「谁在什么时候、为什么担保这条值得进正式记忆」换掉复现次数。
      // 仍要 --confirm；担保理由写进正式条目的边界字段，日后可追溯。
      if (vouch.length < VOUCH_MIN_CHARS) {
        return { err: `REJECT 担保理由太短（${vouch.length} 字，需 ≥${VOUCH_MIN_CHARS}）—— 写清「为什么这条现在就该生效」` };
      }
      if (!target.source || target.source === '-') {
        return { err: 'REJECT 无证据的条目不可担保晋升 —— 先补 --source，担保不能替代证据' };
      }
      hit = { name: '人工担保', rec: same.length, scope: scopes.size, days: age === null ? -1 : age };
    }
    if (!hit) {
      const score2 = (ch) => {
        let t = 0;
        if (same.length < ch.rec) t += (ch.rec - same.length) / ch.rec;
        if (scopes.size < ch.scope) t += (ch.scope - scopes.size) / ch.scope;
        if (age === null || age < ch.days) t += (ch.days - (age === null ? 0 : age)) / ch.days;
        return t;
      };
      const best = PROMOTE_CHANNELS
        .map(ch => ({ ch, gaps: gapsOf(ch, same.length, scopes.size, age), score: score2(ch) }))
        .sort((a, b) => a.gaps.length - b.gaps.length || a.score - b.score)[0];
      return {
        err: 'REJECT 两条通道都未满足（最接近的是' + best.ch.name + '通道）：' + best.gaps.join('、'),
        hint: '      常规 3 次复现 / 2 个作用域 / 30 天；快速 5 次复现 / 3 个作用域 / 7 天'
      };
    }
    if (!hasFlag('--confirm')) {
      return {
        needConfirm: true,
        msg: 'NEED-CONFIRM 满足' + hit.name + '通道（' + same.length + ' 次复现 / ' + scopes.size + ' 个作用域 / ' + age + ' 天），但晋升需人确认。确认无误后加 --confirm 重跑：',
        fact: target.fact,
        evidence: same.map(e => e.source).filter(s => !isBlank(s)).join(' ; ') || '无',
        showVouch: !vouch
      };
    }
    ensureDir(MEM_FILE);
    const bound = hit.name === '人工担保'
      ? `${target.bound === '-' ? '' : target.bound + '；'}人工担保 ${today()}：${vouch}`.slice(0, 200)
      : target.bound;
    // 字段必须转义 —— 候选 fact 里若有未转义的 |，原样落盘会立刻产生一条形状异常的正式记忆
    // 新正式条目生成身份证 —— 它是这条记忆的稳定身份，后续 revise 继承它
    const formalId = genEntryId();
    const formal = `[id:${formalId}] ` + [target.date, target.cat, target.scope, target.fact, target.source, bound, target.review || daysFromNow(DEFAULT_REVIEW_DAYS), 0]
      .map(escapeField).join(' | ');
    // 写前自检：解析回来字段一致才落正式记忆
    const fprobe = parseLine(formal);
    if (!fprobe || fprobe.malformed || fprobe.fact !== target.fact || fprobe.source !== target.source || fprobe.id !== formalId) {
      return { err: '拒绝晋升：正式条目写回去解析不一致（往返校验不通过）', detail: formal };
    }
    fs.appendFileSync(MEM_FILE, formal + '\n', 'utf8');
    // 同事实的候选全部标记，避免同一经验在候选池里重复冒出来
    const before = raw[idx]; // 留痕取改写前的原文，否则「旧值」字段记成新值
    raw.forEach((l, i) => {
      const e = parsed[i];
      if (e && e.fact === target.fact && !e.promoted) raw[i] = l + ' [promoted]';
    });
    fs.writeFileSync(INBOX_FILE, joinLines(raw, eolOf(INBOX_FILE)), 'utf8');
    // 与 drop/revise 对齐：晋升也留痕（旧值=候选行原文，可回溯走哪条通道、对应哪个正式 id）
    logHistory(before, `晋升（${hit.name}通道）：[id:${formalId}]`);
    return { formal, channel: hit.name };
  });
  if (pres.err) {
    console.error(pres.err);
    if (pres.detail) console.error(`  ${pres.detail}`);
    if (pres.hint) console.error(pres.hint);
    process.exit(1);
  }
  if (pres.needConfirm) {
    console.log(pres.msg);
    console.log(`  事实：${pres.fact}`);
    console.log(`  证据：${pres.evidence}`);
    if (pres.showVouch) console.log('  高价值但复现不够？补 --vouch "<≥8 字理由>" 走人工担保，理由会写进正式条目可追溯。');
    process.exit(0);
  }
  console.log('PROMOTED [' + pres.channel + '通道] ' + pres.formal);
}

else if (cmd === 'search') {
  const q = positional.join(' ').trim();
  if (!q) { console.error('用法：memory.mjs search "<关键词>" [--scope <域>] [--limit N]'); process.exit(1); }
  const scopeFilter = optVal('--scope');
  const limit = parseInt(optVal('--limit') || '8', 10);
  const kws = tokens(q);
  const pool = [
    ...readLines(MEM_FILE).map(parseLine),
    ...readLines(INBOX_FILE).map(parseLine)
  ].filter(Boolean).filter(e => !e.promoted);
  let cand = pool.map(e => ({ e, ...scoreEntry(e, kws) })).filter(x => x.hit > 0);
  if (scopeFilter) cand = cand.filter(x => x.e.scope === scopeFilter || x.e.scope === 'global');
  cand.sort((a, b) => b.score - a.score || (b.e.hits - a.e.hits));
  if (!cand.length) { console.log('NONE'); process.exit(0); }
  cand.slice(0, limit).forEach((x, i) => {
    const mark = x.e.legacy ? '旧格式' : '条目';
    console.log(`${i + 1}. [${mark}] ${x.e.date} | ${x.e.cat} | 作用域=${x.e.scope} | 命中=${x.e.hits}`);
    console.log(`   ${x.e.fact}`);
    if (x.e.source && !isBlank(x.e.source)) console.log(`   证据：${x.e.source}`);
    if (x.e.bound && !isBlank(x.e.bound)) console.log(`   边界：${x.e.bound}`);
    if (x.e.review) console.log(`   复审：${x.e.review}${daysSince(x.e.review) > 0 ? '（已过期）' : ''}`);
  });
  if (cand.length > limit) console.log(`… 另有 ${cand.length - limit} 条，缩小关键词或加 --limit`);
  // 召回记账：brief/search 展示的正式条目同日只计一次；候选池不计（还没晋升）
  const recalled = recordRecall(cand.slice(0, limit).map(x => x.e.fact));
  if (recalled) console.log(`  召回记账：${recalled} 条正式记忆 +1（同日同条不重复）`);
}

else if (cmd === 'brief') {
  // 开工时用：只给最相关的 ≤3 条，带可信度，不刷屏。
  // 当天写入的条目当天就能被召回到 —— 这就是「学到了」；
  // 要不要把它变成硬规则（promote）另说，那一步仍要人确认，防一次误判被固化。
  const q = positional.join(' ').trim();
  const kws = tokens(q);
  const pool = [
    ...readLines(MEM_FILE).map(parseLine),
    ...readLines(INBOX_FILE).map(parseLine)
  ].filter(Boolean).filter(e => !e.promoted);

  let cand = pool.map(e => ({ e, ...scoreEntry(e, kws) }));
  if (kws.length) cand = cand.filter(x => x.hit > 0);
  cand.sort((a, b) => b.score - a.score || (a.e.date < b.e.date ? 1 : -1));

  const seen = new Set();
  const picked = [];
  for (const x of cand) {
    if (seen.has(x.e.fact)) continue;
    seen.add(x.e.fact);
    picked.push({ ...x, n: pool.filter(p => p.fact === x.e.fact).length });
    if (picked.length >= 3) break;
  }

  if (!picked.length) { console.log('BRIEF 0 条（没有相关经验，按新问题处理）'); process.exit(0); }
  console.log(`BRIEF ${picked.length} 条`);
  for (const p of picked) {
    const conf = p.n >= 3 ? '可信' : p.n === 2 ? '可用' : '待验证';
    const ev = p.e.source && !isBlank(p.e.source) ? `　证据：${p.e.source}` : '';
    console.log(`  [${conf} ×${p.n}] ${p.e.fact}${ev}`);
  }
  const recalled = recordRecall(picked.map(p => p.e.fact));
  console.log(`  ↑ 已展示即召回：${recalled} 条正式记忆 +1（同日同条只算一次）；` +
    '确实用过还能登记：memory.mjs hit "<事实片段>"（唯一命中才改）');
}

else if (cmd === 'hit') {
  // 手工记账（OI-33）：hits 只由 search 递增，而实际大家用的是 brief 展示。
  // 展示 ≠ 使用 —— 所以不自动 +1（那会虚高），而是留一个「确实用了这条」的登记口。
  const key = positional.join(' ').trim();
  if (!key) {
    console.error('用法：memory.mjs hit "<事实片段>"　—— 片段必须唯一命中一条，否则会改错行');
    process.exit(1);
  }
  // 整段持锁：读 → 定位 → hits+1 → 写回。
  const hres = withLockOrExit(lockPathFor(), () => {
    const lines = readLines(MEM_FILE);
    const matched = [];
    lines.forEach((l, i) => {
      const e = parseLine(l);
      if (e && e.fact.includes(key)) matched.push({ i, e });
    });
    if (matched.length !== 1) {
      return { err: `匹配到 ${matched.length} 条 —— 用更长的片段让它唯一命中；批量改 hits 不是这个命令的用途`, near: matched.slice(0, 5).map(m => m.e.fact.slice(0, 50)) };
    }
    const raw = lines[matched[0].i];
    const flags = tailFlags(raw);
    const clean = raw.replace(flags, '');
    const next = clean.replace(/\|\s*(\d+)\s*$/, (m, n) => `| ${Number(n) + 1}`);
    if (next === clean) {
      return { err: '这一行最后不是命中次数（格式可能被改过）—— 先看一眼原文再动手：' + raw.slice(0, 80) };
    }
    lines[matched[0].i] = next + flags;
    fs.writeFileSync(MEM_FILE, joinLines(lines, eolOf(MEM_FILE)), 'utf8');
    return { fact: matched[0].e.fact.slice(0, 50) };
  });
  if (hres.err) {
    console.error(hres.err);
    if (hres.near) for (const f of hres.near) console.error(`  · ${f}`);
    process.exit(1);
  }
  console.log(`HIT +1 ${hres.fact}`);
  console.log('这是「确实用过」的登记，不是「看到过」——别为了让指标好看而刷。');
}

else if (cmd === 'revise') {
  // OI-14：条目改写必须留旧值。直接 Edit 正式记忆会让历史消失 —— 这条命令是唯一受支持的改写口。
  const key = positional.join(' ').trim();
  const fact = (optVal('--fact') || '').trim();
  const wantId = (optVal('--id') || '').trim().toLowerCase();
  if ((!key && !wantId) || !fact) {
    console.error('用法：memory.mjs revise "<事实片段>" --fact "<新内容>" [--source <证据路径>] [--id <条目id>]');
    console.error('      memory.mjs revise --id <条目id> --fact "<新内容>"   # 事实片段相似时用 id 精确命中');
    process.exit(1);
  }
  if (fact.length < 8) { console.error('新事实太短（<8 字）—— 写清「什么情况下该怎么做」'); process.exit(1); }
  // 整段持锁：读 → 定位 → 改写 → 写回。定位与写入之间若有别的进程插入，
  // 就会改到别人的版本上（甚至把别人的改动覆盖掉）。
  const rres = withLockOrExit(lockPathFor(), () => {
    const lines = readLines(MEM_FILE);
    const all = lines.map((l, i) => ({ l, i, e: parseLine(l) })).filter(x => x.e);
    // 按 id 命中优先：文本片段在相似前缀 / 含转义符时不可靠
    let matched;
    if (wantId) {
      const byId = all.filter(x => x.e.id === wantId);
      if (byId.length === 0) {
        return { err: `没有 id 为 ${wantId} 的条目 —— 用 memory.mjs stats 或直接看记忆文件核对 id` };
      }
      if (byId.length > 1) {
        return { err: `id ${wantId} 命中 ${byId.length} 条（记忆文件里有重复 id）—— 不能任选一条改写，请先手工修掉重复`, dup: byId.map(x => `  第 ${x.i + 1} 行：${x.l}`) };
      }
      matched = byId;
      // 同时给了事实片段但指向别的条目 → 二义，不静默取其一。
      // 判据是「片段命中集与 id 命中集无交集」：片段命中了别的条目就是二义；
      // 片段谁都不匹配（只是拼错了）不算二义，按 id 走。
      if (key) {
        const byText = all.filter(x => x.e.fact.includes(key));
        if (byText.length && !byText.some(x => x.e.id === wantId)) {
          return { err: '事实片段与 --id 指向不同条目 —— 二义，不猜。用哪个就只给哪个。' };
        }
      }
    } else {
      matched = all.filter(x => x.e.fact.includes(key));
      if (matched.length !== 1) {
        return { err: `匹配到 ${matched.length} 条 —— 用更长的片段让它唯一命中，或用 --id 精确指定；批量改写不是这个命令的用途` };
      }
    }
    const m = matched[0];
    // 形状异常的条目拒绝改写 —— 它的字段已错位，重写会把证据路径/边界/复审期/hits 全部抹成占位值。
    // 要修它，先用 memory.mjs stats 看明细，手工改对行再改写。
    if (m.e.malformed) {
      return { err: `这条形状不对（${m.e.malformed}），拒绝改写 —— 重写会把已错位的字段覆盖成占位值。`, detail: `  先看：${m.l}`, fix: '  修法：正文里的 | 写成 \\| ，或补齐缺失的字段段。' };
    }
    // id 继承：改写不换身份。旧条目没有 id 时补一个（不报错、不丢内容）。
    const nextId = m.e.id || genEntryId();
    const next = { ...m.e, id: nextId, fact, source: optVal('--source') || m.e.source, hits: m.e.hits };
    lines[m.i] = formatEntry(next) + (m.e.flags || tailFlags(m.l));
    logHistory(m.l, `revise：${key || wantId}`);
    fs.writeFileSync(MEM_FILE, joinLines(lines, eolOf(MEM_FILE)), 'utf8');
    return { line: lines[m.i], hadId: !!m.e.id, nextId };
  });
  if (rres.err) {
    console.error(rres.err);
    if (rres.detail) console.error(rres.detail);
    if (rres.fix) console.error(rres.fix);
    if (rres.dup) for (const d of rres.dup) console.error(d);
    process.exit(1);
  }
  console.log('REVISED ' + rres.line);
  if (!rres.hadId) console.log(`这条原来没有 id，已补上 [id:${rres.nextId}] —— 以后可用 --id 精确改写它`);
  console.log(`旧值已写入 ${path.relative(ROOT, HISTORY_FILE)} —— history 可查，不覆盖历史。`);
}

else if (cmd === 'history') {
  const key = positional.join(' ').trim();
  const rows = readLines(HISTORY_FILE).filter(Boolean);
  const hit = key ? rows.filter(l => l.includes(key)) : rows;
  if (!hit.length) { console.log('NONE（还没有改写历史）'); process.exit(0); }
  hit.forEach((l, i) => console.log(`${i + 1}. ${l}`));
}

else if (cmd === 'drop') {
  // 把候选池里的条目标成「已判定不晋升」：保留原行（历史不删），加 [dropped]，
  // ready/stats 不再把它算作待办。原因写进 history.md，可追溯。
  const ids = positional.filter(Boolean);
  const reason = (optVal('--reason') || '').trim();
  if (!ids.length) {
    console.error('用法：memory.mjs drop <候选号...> --reason "<为什么不晋升>"');
    process.exit(1);
  }
  if (reason.length < 4) {
    console.error(`REJECT 不晋升理由太短（${reason.length} 字，需 ≥4）—— 写清「为什么这条不值得进正式记忆」`);
    process.exit(1);
  }
  const done = withLockOrExit(lockPathFor(), () => {
    const raw = readLines(INBOX_FILE);
    const parsed = raw.map(parseLine);
    const out = [];
    for (const idRaw of ids) {
      const m = /^c?(\d+)$/i.exec(idRaw);
      if (!m) continue;
      const n = Number(m[1]);
      const i = n - 1;
      const e = parsed[i];
      if (!e) continue;
      if (e.promoted) continue;
      const before = raw[i]; // 同上：留痕取改写前的原文
      if (!/\[dropped\]/.test(raw[i])) raw[i] = raw[i] + ' [dropped]';
      out.push({ n, fact: e.fact });
      logHistory(before, `不晋升（drop）：${reason}`);
    }
    fs.writeFileSync(INBOX_FILE, joinLines(raw, eolOf(INBOX_FILE)), 'utf8');
    return out;
  });
  if (!done.length) {
    console.error('没有可标记的条目（编号不存在或已晋升）');
    process.exit(1);
  }
  for (const d of done) console.log(`DROPPED C${String(d.n).padStart(3, '0')} ${String(d.fact).slice(0, 60)}`);
  console.log(`已标记 ${done.length} 条不晋升；原因写入 ${path.relative(ROOT, HISTORY_FILE)}`);
}

else if (cmd === 'prune') {
  // 整段持锁：读 → 判定 → 写回。prune 会整体重写记忆文件，
  // 与另一个 promote/add 并发时会把对方刚写的条目覆盖掉。
  const pres2 = withLockOrExit(lockPathFor(), () => {
    const lines = readLines(MEM_FILE);
    let stale = 0, kept = 0, contentStale = 0, malformed = 0;
    const next = lines.map(l => {
      const e = parseLine(l);
      if (!e) return l;
      // 形状不对的条目：字段可能已错位，不能拿它做任何判定（原样保留 + 计数上报）。
      // 原先的 bug：漏一段或正文含 | → source 被读成「适用于所有」→ 查路径不存在
      // → 正常记忆被误标 stale-content。
      if (e.malformed) { malformed++; return l; }
    // 时间过期：复审日期到了
    if (e.review && daysSince(e.review) > 0) { stale++; return l + ' [stale]'; }
    // 内容过期（OI-15）：证据路径已不存在 —— 依赖变了的记忆不能等到日历到期才复审
    // 空位判断用 isBlank：半角 - 与中文破折号 — 都算「无」，否则会把占位符当路径去查
    if (e.source && !isBlank(e.source) && !/^https?:\/\//i.test(e.source)) {
      const p = path.isAbsolute(e.source) ? e.source : path.join(ROOT, e.source);
      if (!fs.existsSync(p)) {
        contentStale++;
        return l.replace(/\s*\[stale-content\]\s*$/, '') + ' [stale-content]';
      }
    }
    kept++; return l.replace(/\s*\[stale-content\]\s*$/, '');
  // 保留空行 —— 文件里的空行是结构（标题与说明之间、条目块之间、末尾收尾），滤掉会造成无意义 diff。
  // prune 只该改条目内容，不该顺手重排文件结构。
    }).slice(-MAX_ENTRIES);
    ensureDir(MEM_FILE);
    // readLines 的 split 已保留文件末尾的空串，这里不再补 '\n' —— 补了每次 prune 都多一行
    fs.writeFileSync(MEM_FILE, joinLines(next, eolOf(MEM_FILE)), 'utf8');
    return { stale, kept, contentStale, malformed, total: next.length };
  });
  console.log(`PRUNED kept=${pres2.kept} stale=${pres2.stale} content-stale=${pres2.contentStale} malformed=${pres2.malformed} total=${pres2.total}`);
  if (pres2.malformed) console.log(`⚠ ${pres2.malformed} 条形状不对（段数不是 7/8），已原样保留、未做判定。用 memory.mjs stats 看明细。`);
}

else if (cmd === 'reid') {
  // 一次性给旧条目补身份证。幂等：已有 id 的不动。
  // 只做「前置 [id:xxxxxxxx] 」这一件事 —— 不改任何事实文本，不改字段顺序。
  const dry = hasFlag('--dry-run');
  const files = [MEM_FILE, INBOX_FILE];
  const report = [];
  withLockOrExit(lockPathFor(), () => {
    for (const f of files) {
      const lines = readLines(f);
      let added = 0, skipped = 0, malformedCnt = 0;
      const out = lines.map(l => {
        const e = parseLine(l);
        if (!e) return l;                       // 注释/引言/空行
        if (e.malformed) { malformedCnt++; return l; }  // 形状不对的先不动
        if (e.id) { skipped++; return l; }       // 已有 id
        const newId = genEntryId();
        added++;
        return `[id:${newId}] ${l}`;
      });
      report.push({ file: path.relative(ROOT, f), added, skipped, malformed: malformedCnt, lines: out });
    }
    if (dry) return;
    // 真写：先备份 —— 这是记忆文件，改坏了得能回滚
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    for (const r of report) {
      const abs = path.join(ROOT, r.file);
      if (!fs.existsSync(abs)) continue;
      fs.copyFileSync(abs, abs + '.bak.' + stamp);
      fs.writeFileSync(abs, joinLines(r.lines, eolOf(abs)), 'utf8');
    }
  });
  for (const r of report) {
    console.log(`${dry ? '[试运行] ' : ''}${r.file}：补 id ${r.added} 条，已有 id 跳过 ${r.skipped} 条，形状异常不动 ${r.malformed} 条`);
  }
  if (dry) console.log('这是试运行，没改任何文件。去掉 --dry-run 才会真写（真写前会自动备份 .bak.<时间戳>）。');
  else console.log('已补 id 并备份（.bak.<时间戳>）。已有 id 的条目保持不变。');
}

else if (cmd === 'index') {
  const cats = {};  for (const f of [MEM_FILE, INBOX_FILE]) {
    for (const e of readLines(f).map(parseLine).filter(Boolean)) {
      cats[e.cat || '?'] = (cats[e.cat || '?'] || 0) + 1;
    }
  }
  console.log('INDEX ' + JSON.stringify(cats));
}


else if (hasFlag('--selftest')) {
  // 自测（临时目录，不碰真实记忆）：
  // ① brief 展示两次 → 同日同条只 +1；② 短理由不可担保；③ 有证据的担保晋升成功且留痕（正式条目与 history 都要有）。
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'mem-selftest-'));
  // 记忆文件放在 ROOT/harness/memory/ 下 —— 与真实布局一致（锁路径从 ROOT 推导，
  // 统一一把锁；--file/--inbox 若指向别处，就会和锁/历史文件分属不同目录）
  const memDir = path.join(tmp, 'harness', 'memory');
  fs.mkdirSync(memDir, { recursive: true });
  const mem = path.join(memDir, 'long-term.md');
  const inbox = path.join(memDir, 'inbox.md');
  const proof = path.join(tmp, 'proof.md');
  fs.writeFileSync(proof, '证据\n', 'utf8');
  fs.writeFileSync(mem, `2026-11-21 | 经验 | 测试 | 自测用的召回防刷条目内容 | ${proof} | — | 2027-05-20 | 0\n`, 'utf8');
  fs.writeFileSync(inbox, [
    `2026-11-21 | 经验 | 测试 | 自测用的担保晋升条目内容 | ${proof} | — | 2027-05-20`,
    `2026-11-21 | 经验 | 测试 | 自测用的不晋升条目内容 | ${proof} | — | 2027-05-20`,
    `2026-11-21 | 经验 | 测试 | 自测用的第二条不晋升条目内容 | ${proof} | — | 2027-05-20`
  ].join('\n') + '\n', 'utf8');
  const self = fileURLToPath(import.meta.url);
  const run = (...a) => spawnSync(process.execPath, [self, ...a, '--root', tmp, '--file', mem, '--inbox', inbox], { encoding: 'utf8' });
  // 活锁测试要等锁超时 —— 用短超时（默认 5s 会让整个自测多等 5 秒）
  const runShortLock = (...a) => spawnSync(process.execPath, [self, ...a, '--root', tmp, '--file', mem, '--inbox', inbox],
    { encoding: 'utf8', env: { ...process.env, MEMORY_LOCK_TIMEOUT_MS: '800' } });
  const bad = [];
  run('brief', '召回防刷');
  run('brief', '召回防刷');
  const after = fs.readFileSync(mem, 'utf8').trim().split('|').pop().trim();
  if (Number(after) !== 1) bad.push(`同日两次 brief 后 hits=${after}，应为 1`);
  const short = run('promote', 'C001', '--vouch', '太短', '--confirm');
  if (short.status === 0) bad.push('短担保理由竟然通过');
  const ok = run('promote', 'C001', '--vouch', '本条已在三个项目复核且证据可追溯', '--confirm');
  if (ok.status !== 0 || !/PROMOTED \[人工担保通道\]/.test(ok.stdout || '')) bad.push('担保晋升失败：' + (ok.stdout || ok.stderr || ''));
  if (!/人工担保/.test(fs.readFileSync(mem, 'utf8'))) bad.push('担保理由未写进正式记忆');
  // B：改写留旧值 + 证据路径消失触发提前待复审
  const rv = run('revise', '自测用的召回防刷条目', '--fact', '自测改写后的新事实内容足够长');
  if (rv.status !== 0) bad.push('revise 失败：' + (rv.stderr || rv.stdout || ''));
  const HIST = path.join(memDir, 'history.md');
  // 留痕断言（参数化）：每个写命令执行完都要能在 history 里查到自己的记录，否则审计链断节
  const expectHist = (re, label, r) => {
    if (r && r.status !== 0) bad.push(`${label}失败：` + (r.stderr || r.stdout || ''));
    else if (!re.test(fs.readFileSync(HIST, 'utf8'))) bad.push(`${label}未写入 history`);
  };
  expectHist(/旧值：.*自测用的召回防刷条目/, '改写', rv);
  expectHist(/晋升（人工担保通道）：\[id:/, '晋升', ok);
  const dr = run('drop', 'C002', 'C003', '--reason', '自测不晋升理由'); // 批量：一次多号
  expectHist(/不晋升（drop）：/, 'drop', dr);
  if ((fs.readFileSync(inbox, 'utf8').match(/\[dropped\]/g) || []).length < 2) bad.push('批量 drop 未全部标记');
  if ((fs.readFileSync(HIST, 'utf8').match(/不晋升（drop）：/g) || []).length < 2) bad.push('批量 drop 未逐条留痕');
  // 弱断言：history 的「旧值」必须是改写前的原文，不能带上刚追加的 [promoted]/[dropped]
  if (/旧值：.*\[(promoted|dropped)\]/.test(fs.readFileSync(HIST, 'utf8'))) bad.push('history 的旧值记成了改写后的新值');
  fs.unlinkSync(proof);
  const pr = run('prune');
  if (!/content-stale=/.test(pr.stdout || '')) bad.push('prune 未报内容过期数：' + (pr.stdout || ''));
  if (!/\[stale-content\]/.test(fs.readFileSync(mem, 'utf8'))) bad.push('证据消失的记忆未提前待复审');
  // 审查发现的交互缺陷：召回/改写不能清掉 [stale-content]，否则提醒在两次 prune 之间丢失
  // 用改写后的关键词，确保 brief 真的召回这条（否则 BRIEF 0 条提前退出，测不到 recordRecall）
  run('brief', '自测改写后的新事实');
  if (!/\[stale-content\]/.test(fs.readFileSync(mem, 'utf8'))) bad.push('brief 清掉了 [stale-content]');
  const rv2 = run('revise', '自测改写后的新事实', '--fact', '再次改写后的新事实内容足够长');
  if (rv2.status !== 0 || !/\[stale-content\]/.test(fs.readFileSync(mem, 'utf8'))) bad.push('revise 清掉了 [stale-content]');
  // 审查发现的口径缺陷：search --limit 1 只能给展示的那条 +1
  fs.writeFileSync(mem, [
    `2026-11-21 | 经验 | 测试 | 限额测试第一条内容 | ${proof} | — | 2027-05-20 | 0`,
    `2026-11-21 | 经验 | 测试 | 限额测试第二条内容 | ${proof} | — | 2027-05-20 | 0`
  ].join('\n') + '\n', 'utf8');
  fs.writeFileSync(path.join(tmp, 'harness', 'memory', 'recall-ledger.md'), '', 'utf8');
  run('search', '限额测试', '--limit', '1');
  const limits = fs.readFileSync(mem, 'utf8').trim().split('\n').map(l => Number(l.split('|').pop().trim()));
  if (limits[0] !== 1 || limits[1] !== 0) bad.push(`search --limit 1 记账错误：${JSON.stringify(limits)}，应为 [1,0]`);
  // ⑧ 字段错位防护（本轮修的真 bug）：
  //    漏一段 → source 被读成「适用于所有」→ prune 拿它查路径 → 正常记忆被误判 stale-content。
  //    正文含未转义的 | → 后面字段整体右移，同样错位。
  fs.writeFileSync(mem, [
    `2026-11-21 | 经验 | 测试 | 错位测试漏段内容 | — | — | 2027-05-20 | 0`,           // 8 段但少了 source 语义位
    `2026-11-21 | 经验 | 测试 | 错位测试含管道符内容 | 用 A | B | — | 2027-05-20 | 0`     // 正文含未转义 |
  ].join('\n') + '\n', 'utf8');
  fs.writeFileSync(path.join(tmp, 'harness', 'memory', 'recall-ledger.md'), '', 'utf8');
  const pr2 = run('prune');
  if (!/malformed=[1-9]/.test(pr2.stdout || '')) bad.push('prune 未报 malformed 数：' + (pr2.stdout || ''));
  if (/\[stale-content\]/.test(fs.readFileSync(mem, 'utf8'))) bad.push('错位条目被误标 stale-content（正是本 bug）');
  const st = run('stats');
  if (!/"形状异常条目":[1-9]/.test(st.stdout || '')) bad.push('stats 未报形状异常条目：' + (st.stdout || ''));
  // 已转义的正文 | 必须能正确还原，且不被判成 malformed
  fs.writeFileSync(mem, `2026-11-21 | 经验 | 测试 | 转义测试含 A \\| B 的内容 | ${proof} | — | 2027-05-20 | 0\n`, 'utf8');
  const st2 = run('stats');
  if (!/"形状异常条目":0/.test(st2.stdout || '')) bad.push('已转义的正文被误判形状异常：' + (st2.stdout || ''));
  // ⑨ 带尾标记的正常条目不算形状异常（尾标记附在 hits 之后，不是第 8 段的一部分）
  fs.writeFileSync(mem, `2026-11-21 | 经验 | 测试 | 带尾标记的正常条目 | ${proof} | — | 2027-05-20 | 3 [stale-content]\n`, 'utf8');
  const st3 = run('stats');
  if (!/"形状异常条目":0/.test(st3.stdout || '')) bad.push('带尾标记的正常条目被误判形状异常：' + (st3.stdout || ''));
  // ⑩ 写回路径不能丢字段：brief 召回（+hits）后，证据路径与复审期必须还在
  fs.writeFileSync(mem, `2026-11-21 | 经验 | 测试 | 写回不丢字段测试条目 | ${proof} | 重要边界说明 | 2031-12-31 | 0\n`, 'utf8');
  fs.writeFileSync(path.join(tmp, 'harness', 'memory', 'recall-ledger.md'), '', 'utf8');
  run('brief', '写回不丢字段');
  const back = fs.readFileSync(mem, 'utf8').trim();
  if (!back.includes(proof)) bad.push('brief 召回后证据路径被抹掉：' + back);
  if (!/2031-12-31/.test(back)) bad.push('brief 召回后复审期被抹掉：' + back);
  if (!/\| 1(\s|$)/.test(back)) bad.push('brief 召回后 hits 没 +1：' + back);
  // ⑪ revise 也不能把字段抹成占位值
  fs.writeFileSync(mem, `2026-11-21 | 经验 | 测试 | 改写不丢字段测试条目 | ${proof} | 重要边界说明 | 2031-12-31 | 0\n`, 'utf8');
  const rv3 = run('revise', '改写不丢字段测试条目', '--fact', '改写后的新事实内容足够长');
  const back3 = fs.readFileSync(mem, 'utf8');
  if (rv3.status !== 0) bad.push('revise 失败：' + (rv3.stderr || rv3.stdout || ''));
  else if (!back3.includes(proof) || !/2031-12-31/.test(back3)) bad.push('revise 把字段抹成占位值：' + back3.trim());
  // ⑫ promote 必须转义候选正文：候选里已转义的 | 晋升到正式记忆后仍是好的
  //     （候选池由 add 写入时已转义；手工写候选也该用同样约定）
  fs.writeFileSync(mem, '', 'utf8');
  fs.writeFileSync(inbox,
    `2026-11-21 | 经验 | 测试 | 晋升转义测试含 A \\| B 的内容 | ${proof} | - | 2027-05-20\n`, 'utf8');
  const pr3 = run('promote', 'C001', '--vouch', '本条已在三个项目复核且证据可追溯', '--confirm');
  if (pr3.status !== 0) bad.push('晋升失败：' + (pr3.stderr || pr3.stdout || ''));
  else {
    const st4 = run('stats');
    if (!/"形状异常条目":0/.test(st4.stdout || '')) bad.push('晋升产生形状异常条目（未转义）：' + (st4.stdout || ''));
    if (!/晋升转义测试含 A/.test(fs.readFileSync(mem, 'utf8'))) bad.push('晋升后事实内容丢了：' + fs.readFileSync(mem, 'utf8'));
  }

  // ══ 第 9 组：条目身份证（AC-01 / AC-02 / AC-05）══
  // ① promote 产生的正式记忆带 [id:xxxxxxxx]；revise 继承它（改写不换身份）
  fs.writeFileSync(mem, '', 'utf8');
  fs.writeFileSync(inbox,
    `2026-11-21 | 经验 | 测试 | 身份证测试条目内容 | ${proof} | - | 2027-05-20\n`, 'utf8');
  const pm = run('promote', 'C001', '--vouch', '本条已在三个项目复核且证据可追溯', '--confirm');
  if (pm.status !== 0) bad.push('晋升失败：' + (pm.stderr || pm.stdout || ''));
  const promotedLine = fs.readFileSync(mem, 'utf8').trim();
  const idm = /^\[id:([0-9a-f]{8})\] /.exec(promotedLine);
  if (!idm) bad.push('晋升的正式记忆没有 [id:xxxxxxxx] 前缀：' + promotedLine);
  else {
    const pid = idm[1];
    const rvId = run('revise', '--id', pid, '--fact', '身份证测试条目改写后的内容');
    if (rvId.status !== 0) bad.push('按 id 改写失败：' + (rvId.stderr || rvId.stdout || ''));
    const afterId = fs.readFileSync(mem, 'utf8').trim();
    if (!afterId.includes(`[id:${pid}]`)) bad.push('revise 后 id 变了（应该继承）：' + afterId);
    if (!/身份证测试条目改写后的内容/.test(afterId)) bad.push('revise --id 没改到内容：' + afterId);
  }
  // ② 按 id 命中：事实文本相似时也能精确命中
  fs.writeFileSync(mem, [
    `[id:aaaa1111] 2026-11-21 | 经验 | 测试 | 相似前缀的第一条记忆内容 | ${proof} | - | 2027-05-20 | 0`,
    `[id:bbbb2222] 2026-11-21 | 经验 | 测试 | 相似前缀的第二条记忆内容 | ${proof} | - | 2027-05-20 | 0`
  ].join('\n') + '\n', 'utf8');
  const rvDup = run('revise', '相似前缀', '--fact', '这条改写不该发生');
  if (rvDup.status === 0) bad.push('相似前缀按文本匹配竟通过了（应报错要求用 --id）');
  const rvById = run('revise', '--id', 'bbbb2222', '--fact', '按 id 精确改写后的内容');
  if (rvById.status !== 0) bad.push('按 id 精确改写失败：' + (rvById.stderr || rvById.stdout || ''));
  const byIdOut = fs.readFileSync(mem, 'utf8');
  if (!/\[id:bbbb2222\].*按 id 精确改写后的内容/.test(byIdOut)) bad.push('按 id 改写没生效：' + byIdOut.trim());
  if (!/\[id:aaaa1111\] 2026-11-21 \| 经验 \| 测试 \| 相似前缀的第一条记忆内容/.test(byIdOut)) bad.push('按 id 改写误伤了别的条目：' + byIdOut.trim());
  // ③ id 命中 0 条 / 重复 id / 二义 —— 各自报错，不静默
  //    断言必须看**具体文案**：只判退出码会被「新事实太短」提前拦下而假阳性（审审查发现过这个坑）
  const rvNone = run('revise', '--id', 'deadbeef', '--fact', '这条内容不应该被改写掉');
  if (rvNone.status === 0) bad.push('不存在的 id 竟通过了');
  if (!/没有 id 为 deadbeef/.test(rvNone.stderr || '')) bad.push('不存在的 id 没报「没有这个 id」：' + (rvNone.stderr || rvNone.stdout || ''));
  fs.writeFileSync(mem, [
    `[id:cccc3333] 2026-11-21 | 经验 | 测试 | 重复 id 的第一条记忆内容 | ${proof} | - | 2027-05-20 | 0`,
    `[id:cccc3333] 2026-11-21 | 经验 | 测试 | 重复 id 的第二条记忆内容 | ${proof} | - | 2027-05-20 | 0`
  ].join('\n') + '\n', 'utf8');
  const rvTwo = run('revise', '--id', 'cccc3333', '--fact', '重复 id 的条目不应该被改写掉');
  if (rvTwo.status === 0) bad.push('重复 id 竟通过了（应报错）');
  if (!/命中 2 条/.test(rvTwo.stderr || '')) bad.push('重复 id 没报「命中多条」：' + (rvTwo.stderr || rvTwo.stdout || ''));
  fs.writeFileSync(mem, [
    `[id:dddd4444] 2026-11-21 | 经验 | 测试 | 二义测试的第一条记忆内容 | ${proof} | - | 2027-05-20 | 0`,
    `[id:eeee6666] 2026-11-21 | 经验 | 测试 | 二义测试的第二条记忆内容 | ${proof} | - | 2027-05-20 | 0`
  ].join('\n') + '\n', 'utf8');
  // 片段命中第二条（eeee6666），而 --id 指向第一条（dddd4444）→ 二义
  const rvAmb = run('revise', '第二条记忆内容', '--id', 'dddd4444', '--fact', '二义的时候不应该被改写掉');
  if (rvAmb.status === 0) bad.push('二义（片段与 id 不一致）竟通过了');
  if (!/二义/.test(rvAmb.stderr || '')) bad.push('二义没报「二义」：' + (rvAmb.stderr || rvAmb.stdout || ''));
  // ④ 兼容无 id 的旧条目：revise 时补 id，不报错不丢内容
  fs.writeFileSync(mem, `2026-11-21 | 经验 | 测试 | 没有 id 的旧条目内容 | ${proof} | 旧边界说明 | 2031-12-31 | 2\n`, 'utf8');
  const rvOld = run('revise', '没有 id 的旧条目', '--fact', '旧条目改写后的新内容');
  if (rvOld.status !== 0) bad.push('旧条目（无 id）改写失败：' + (rvOld.stderr || rvOld.stdout || ''));
  const oldOut = fs.readFileSync(mem, 'utf8');
  if (!/\[id:[0-9a-f]{8}\]/.test(oldOut)) bad.push('旧条目改写后没补上 id：' + oldOut.trim());
  if (!oldOut.includes(proof) || !/2031-12-31/.test(oldOut)) bad.push('旧条目改写丢了字段：' + oldOut.trim());
  if (!/\| 2(\s|$)/.test(oldOut.trim())) bad.push('旧条目改写丢了 hits：' + oldOut.trim());
  // ⑤ 有 id 与无 id 混存都能解析，且形状异常为 0
  fs.writeFileSync(mem, [
    `[id:eeee5555] 2026-11-21 | 经验 | 测试 | 有 id 的条目内容 | ${proof} | - | 2027-05-20 | 0`,
    `2026-11-21 | 经验 | 测试 | 没有 id 的条目内容 | ${proof} | - | 2027-05-20 | 0`
  ].join('\n') + '\n', 'utf8');
  const stMix = run('stats');
  if (!/"形状异常条目":0/.test(stMix.stdout || '')) bad.push('混存时形状异常不为 0：' + (stMix.stdout || ''));
  if (!/"正式条目":2/.test(stMix.stdout || '')) bad.push('混存时条目数不对：' + (stMix.stdout || ''));
  // ⑥ reid 幂等：跑两次，第二次不该再补
  fs.writeFileSync(mem, `2026-11-21 | 经验 | 测试 | 待补 id 的条目内容 | ${proof} | - | 2027-05-20 | 0\n`, 'utf8');
  const re1 = run('reid');
  const re2 = run('reid');
  if (!/补 id 1 条/.test(re1.stdout || '')) bad.push('reid 第一次没补 id：' + (re1.stdout || ''));
  if (!/补 id 0 条，已有 id 跳过 1 条/.test(re2.stdout || '')) bad.push('reid 不幂等：' + (re2.stdout || ''));
  const afterReid = fs.readFileSync(mem, 'utf8');
  if ((afterReid.match(/\[id:[0-9a-f]{8}\]/g) || []).length !== 1) bad.push('reid 后 id 数量不对：' + afterReid.trim());
  const stReid = run('stats');
  if (!/"形状异常条目":0/.test(stReid.stdout || '')) bad.push('reid 后形状异常不为 0：' + (stReid.stdout || ''));

  // ══ 第 10 组：跨进程锁（AC-03 / AC-04）══
  // ① 并发 promote 不互相覆盖：两个进程同时晋升不同候选，最终两条都在
  fs.writeFileSync(mem, '', 'utf8');
  fs.writeFileSync(inbox, [
    `2026-11-21 | 经验 | 测试 | 并发晋升的第一条记忆内容 | ${proof} | - | 2027-05-20`,
    `2026-11-21 | 经验 | 测试 | 并发晋升的第二条记忆内容 | ${proof} | - | 2027-05-20`
  ].join('\n') + '\n', 'utf8');
  // 必须真并行：spawnSync 是顺序执行，两个两个来等于没测并发（原来这里就是顺序的，名不副实）
  const kidA = spawn(process.execPath, [self, 'promote', 'C001', '--vouch', '本条已在三个项目复核且证据可追溯', '--confirm', '--root', tmp, '--file', mem, '--inbox', inbox], { stdio: 'ignore' });
  const kidB = spawn(process.execPath, [self, 'promote', 'C002', '--vouch', '本条已在三个项目复核且证据可追溯', '--confirm', '--root', tmp, '--file', mem, '--inbox', inbox], { stdio: 'ignore' });
  // sleepSync 阻塞事件循环，子进程 exitCode 不会更新 —— 只能轮询产物落盘
  const parDeadline = Date.now() + 5000;
  let concurrent = '';
  while (Date.now() < parDeadline) {
    concurrent = fs.readFileSync(mem, 'utf8');
    if (/并发晋升的第一条/.test(concurrent) && /并发晋升的第二条/.test(concurrent)) break;
    sleepSync(25);
  }
  for (const k of [kidA, kidB]) { try { k.kill(); } catch { /* 已退出 */ } }
  const keptBoth = /并发晋升的第一条/.test(concurrent) && /并发晋升的第二条/.test(concurrent);
  if (!keptBoth) bad.push('并发晋升丢条目（锁没生效）：' + concurrent.trim());
  // ② 持锁进程被强杀 → 锁目录残留 → 下一个进程能识别 stale 并接管
  fs.writeFileSync(mem, `2026-11-21 | 经验 | 测试 | stale 锁测试的记忆条目内容 | ${proof} | - | 2027-05-20 | 0\n`, 'utf8');
  const lockPath = path.join(memDir, '.memory.lock');
  fs.mkdirSync(lockPath, { recursive: true });
  fs.writeFileSync(path.join(lockPath, 'owner'), '999999', 'utf8');   // 一个几乎不可能活着的 pid
  const afterStale = run('hit', 'stale 锁测试的记忆条目');
  if (afterStale.status !== 0) bad.push('stale 锁没被接管，命令失败：' + (afterStale.stderr || afterStale.stdout || ''));
  // ③ 活进程持锁 → 不能抢。用会写库的命令测（stats 是纯读，不参与锁）
  //    holder 必须真的活着 —— spawnSync 的 timeout 会立刻杀掉它，pid 一死锁就被判 stale，测不到「活锁」。
  fs.writeFileSync(mem, `2026-11-21 | 经验 | 测试 | 活锁测试的记忆条目内容 | ${proof} | - | 2027-05-20 | 0\n`, 'utf8');
  const holderProc = spawn(process.execPath, ['-e', `
    const fs=require('fs'), path=require('path');
    const dir=${JSON.stringify(lockPath)};
    fs.mkdirSync(dir, {recursive:true});
    fs.writeFileSync(path.join(dir,'owner'), String(process.pid));
    setTimeout(()=>{}, 20000);
  `], { stdio: 'ignore', detached: false });
  // 等 holder 真的把锁写上（最多 2s）
  let holderReady = false;
  for (let i = 0; i < 80; i++) {
    if (fs.existsSync(path.join(lockPath, 'owner'))) {
      const hp = parseInt((fs.readFileSync(path.join(lockPath, 'owner'), 'utf8') || '').trim(), 10);
      if (Number.isInteger(hp) && hp > 0) { try { process.kill(hp, 0); holderReady = true; } catch { /* 还没好 */ } }
      if (holderReady) break;
    }
    sleepSync(25);
  }
  if (!holderReady) bad.push('活锁测试的 holder 进程没起来，测不到「活进程持锁」');
  else {
    const t0 = Date.now();
    // 用短超时（800ms）—— 默认 5s 会让整个自测多等 5 秒
    const blocked = runShortLock('hit', '活锁测试的记忆条目');
    const waited = Date.now() - t0;
    if (blocked.status === 0) bad.push('活进程持锁时命令仍成功（应等锁后失败，而不是并发写）');
    if (waited < 400) bad.push('活进程持锁时几乎没等待（说明没在等锁）：' + waited + 'ms');
    if (!/LOCK-FAILED/.test(blocked.stderr || '')) bad.push('活锁时没报 LOCK-FAILED：' + (blocked.stderr || blocked.stdout || ''));
  }
  try { holderProc.kill(); } catch { /* ignore */ }
  try { fs.rmSync(lockPath, { recursive: true, force: true }); } catch { /* ignore */ }
  // ④ 锁内容写坏 → 不崩溃（立即当 stale 接管）
  fs.mkdirSync(lockPath, { recursive: true });
  fs.writeFileSync(path.join(lockPath, 'owner'), 'not-a-pid', 'utf8');
  const broken = run('hit', '活锁测试的记忆条目');
  if (broken.status !== 0) bad.push('锁内容写坏导致命令失败：' + (broken.stderr || broken.stdout || ''));
  try { fs.rmSync(lockPath, { recursive: true, force: true }); } catch { /* ignore */ }
  // ⑤ 正常路径不留锁残留
  run('hit', '活锁测试的记忆条目');
  if (fs.existsSync(lockPath)) bad.push('命令结束后锁目录残留了');
  // ⑥ 真并发：N 个进程同时晋升不同候选，最终一条都不能丢
  //    （这是本单的核心验收 —— 之前用「先建文件再填 pid」的实现会偶发丢条目，实测 8 次里有 7 次丢。
  //     根因：Windows 上先创建再填内容不是原子的，别人在窗口里读到空内容会误判 stale 抢锁。）
  //     stderr 必须重定向到文件：sleepSync（Atomics.wait）阻塞事件循环，'stderr data' 事件不会被派发。
  const CONC = 10;
  const CONC_ROUNDS = 3;
  let concBad = '';
  for (let round = 0; round < CONC_ROUNDS; round++) {
    fs.writeFileSync(mem, '', 'utf8');
    const concCands = [];
    for (let i = 0; i < CONC; i++) concCands.push(`2026-11-21 | 经验 | 测试 | 并发条目${round}_${i}的内容 | ${proof} | - | 2027-05-20`);
    fs.writeFileSync(inbox, concCands.join('\n') + '\n', 'utf8');
    const concKids = [];
    for (let i = 0; i < CONC; i++) {
      const logFd = fs.openSync(path.join(tmp, `kid-${round}-${i}.log`), 'w');
      concKids.push({ kid: spawn(process.execPath, [self, 'promote', 'C' + (i + 1), '--vouch', '本条已在三个项目复核且证据可追溯', '--confirm', '--root', tmp, '--file', mem, '--inbox', inbox], { stdio: ['ignore', 'ignore', logFd] }), log: path.join(tmp, `kid-${round}-${i}.log`) });
      fs.closeSync(logFd);   // 父进程不留句柄，否则 Windows 删不掉临时目录
    }
    // 同步等待：不能用 sleepSync 等 exit 事件 —— Atomics.wait 阻塞事件循环，回调永远不触发。
    // 改成轮询「正式记忆里的条目数」到达 CONC。
    const concDeadline = Date.now() + 30000;
    let concGot = 0;
    while (Date.now() < concDeadline) {
      concGot = (fs.readFileSync(mem, 'utf8').match(new RegExp(`并发条目${round}_\\d+的内容`, 'g')) || []).length;
      if (concGot >= CONC) break;
      sleepSync(50);
    }
    if (concGot !== CONC) concBad += `第${round + 1}轮丢条目（${concGot}/${CONC}）；`;
    const concMarked = (fs.readFileSync(inbox, 'utf8').match(/\[promoted\]/g) || []).length;
    if (concMarked !== CONC) concBad += `第${round + 1}轮标记数不对（${concMarked}/${CONC}）；`;
    for (const r of concKids) {
      let err = '';
      try { err = fs.readFileSync(r.log, 'utf8').trim(); } catch { /* 无输出 */ }
      if (/候选不存在|LOCK-FAILED|用法：/.test(err)) concBad += `第${round + 1}轮子进程异常：${err.split('\n')[0]}；`;
      try { r.kid.kill(); } catch { /* 已退出 */ }
    }
    sleepSync(100);   // 等被 kill 的子进程真正松手，避免下一轮文件被抢
  }
  if (concBad) bad.push('真并发晋升异常 —— ' + concBad);
  fs.rmSync(tmp, { recursive: true, force: true });
  if (bad.length) { console.error('MEM SELFTEST FAIL: ' + bad.join('；')); process.exit(1); }
  console.log('MEM SELFTEST OK 10 组（同日防刷 / 短理由拒绝 / 担保晋升留痕 / 改写留旧值 / 证据消失提前复审 / 标记保留 / search 限额记账 / 字段错位防护 / 条目身份证 / 跨进程锁）');
  process.exit(0);
}

else if (cmd === 'stats') {
  const formal = readLines(MEM_FILE).map(parseLine).filter(Boolean);
  const inbox = readLines(INBOX_FILE).map(parseLine).filter(Boolean).filter(e => !e.promoted && !e.dropped);
  const droppedCount = readLines(INBOX_FILE).map(parseLine).filter(Boolean).filter(e => e.dropped && !e.promoted).length;
  const events = inbox.filter(e => e.cat === '事件');
  const lessons = inbox.filter(e => e.cat !== '事件');
  const structured = formal.filter(e => !e.legacy);
  const withScope = structured.filter(e => e.scope && !isBlank(e.scope)).length;
  const withSource = structured.filter(e => e.source && !isBlank(e.source)).length;
  const expired = structured.filter(e => e.review && daysSince(e.review) > 0).length;
  const recalled = formal.filter(e => e.hits > 0).length;
  // 形状不对的条目：漏段或正文含未转义的 | —— 字段会错位，不能参与任何判定
  const malformedEntries = formal.filter(e => e.malformed);
  const malformedInbox = inbox.filter(e => e.malformed);
  const ready = new Set();
  for (const fact of new Set(lessons.map(e => e.fact))) {
    const list = lessons.filter(e => e.fact === fact);
    const scopes = new Set(list.map(e => e.scope));
    const age = daysSince(list.map(e => e.date).sort()[0]);
    if (matchChannel(list.length, scopes.size, age)) ready.add(fact);   // 与 ready/promote 共用同一判据
  }
  console.log('STATS ' + JSON.stringify({
    正式条目: formal.length,
    结构化条目: structured.length,
    旧格式条目: formal.length - structured.length,
    形状异常条目: malformedEntries.length + malformedInbox.length,
    候选池条目: inbox.length,
    已标不晋升: droppedCount,
    待提炼事件: events.length,
    经验候选: lessons.length,
    待晋升候选: ready.size,
    带作用域: withScope,
    带证据: withSource,
    已过复审期: expired,
    已召回条目: recalled,
    实时M8: formal.length ? Math.round(recalled / formal.length * 100) + '%' : 'n/a'
  }));
  if (malformedEntries.length || malformedInbox.length) {
    console.log('⚠ 形状异常明细（字段会错位，已排除在统计与判定之外）：');
    for (const e of [...malformedEntries, ...malformedInbox].slice(0, 5)) {
      console.log(`  - ${e.malformed}：${String(e.raw).slice(0, 90)}`);
    }
    console.log('  修法：正文里的 | 写成 \\| ，或补齐缺失的字段段。');
  }
}

else {
  console.log('memory.mjs: add | brief | ready | promote | drop | search | prune | index | stats');
}
