#!/usr/bin/env node
// 写侧守卫：在「写入之前」拦住明确危险的写法，不等事后审查。
//
// 为什么单独一个脚本，不改 agent.mjs 的 guard：
//   1. agent.mjs 不在工程脚本清单里 —— hook 里写 `node scripts/agent.mjs` 在新工程里根本不存在（死链）；
//   2. guard 只读 --cmd，不读 PreToolUse 从 stdin 送的 tool_input，对着 Write/Edit 等于空跑；
//   3. 单独一个文件才能被自己测（--selftest），也才能被 selfcheck 守住。
//
// 分级（关键：假命中比漏命中更糟，所以 block 只给「一眼就是事故」的模式）：
//   block → exit 2，宿主会拦下这次写入/命令（私钥、真实密钥、DROP TABLE、关 TLS 校验）
//   warn  → exit 0，只提醒（rm -rf、force push、无 WHERE 的 DELETE、--no-verify）
//
// 用法：
//   node harness/scripts/write-guard.mjs                     # PreToolUse：stdin 读 JSON
//   node harness/scripts/write-guard.mjs --text "DROP TABLE x" --file a.sql
//   node harness/scripts/write-guard.mjs --selftest          # 跑内置用例，selfcheck 用
import fs from 'node:fs';
import path from 'node:path';

const argv = process.argv.slice(2);
const flag = n => argv.includes(n);
const opt = (n, d) => { const i = argv.indexOf(n); return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : d; };

// 只在「会运行的代码 / 配置 / SQL」里查内容。
// 文档（.md）里出现 DROP TABLE 是举例，拦它只会逼人删解释。
const CODE_EXT = /\.(m?js|cjs|ts|tsx|jsx|py|go|java|kt|php|rb|rs|sql|ya?ml|json|toml|ini|env|sh|ps1)$/i;
// 夹具/基准/示例里有故意写坏的东西，拦了就变成「测试样本不能存在」
const SKIP_PATH = /(^|\/)(fixtures?|bench|examples?|node_modules|dist|vendor)(\/|$)/i;

const RULES = [
  // ---- block：一眼就是事故 ----
  { re: /-----BEGIN [A-Z ]*PRIVATE KEY-----/, msg: '私钥内容写进代码：提交即泄漏。放环境变量或密钥管理，别落文件', level: 'block', scope: 'both' },
  { re: /\b(sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,})\b/, msg: '疑似真实密钥（sk-/AKIA/ghp_/xox）：改用环境变量读取', level: 'block', scope: 'both' },
  { re: /\b(drop\s+(table|database|schema)|truncate\s+table)\b/i, msg: '破坏性 SQL（DROP / TRUNCATE）：先备份、确认目标库，再在事务里执行', level: 'block', scope: 'both' },
  { re: /NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*['"]?0/, msg: '全局关闭 TLS 校验：不得进入代码，改成本地证书或注入 CA', level: 'block', scope: 'content' },
  { re: /\b(rejectUnauthorized|verify)\s*[:=]\s*(false|False)\b/, msg: '关闭证书校验：只允许本地调试，且必须注释说明原因', level: 'block', scope: 'content' },

  // ---- warn：可能是正当操作，但先回答一遍 ----
  { re: /\brm\s+-[a-z]*[rf]/i, msg: '递归删除：先确认路径；优先 git restore / 移到回收站', level: 'warn', scope: 'cmd' },
  // 先整体排除 --force-with-lease：安全版本里含 "-f"，不加这层会把安全用法判成强推（自测抓到）
  { re: /\bgit\s+push\b(?![^\n]*--force-with-lease)[^\n]*(?:--force\b|(?<![\w-])-f(?=\s|$))/i, msg: '强制推送：确认分支与他人提交；优先 --force-with-lease', level: 'warn', scope: 'cmd' },
  { re: /\bDELETE\s+FROM\s+[\w."`\[\]]+\s*(;|$)/im, msg: '无 WHERE 的 DELETE：会清空整表，补条件或改用 TRUNCATE 前先备份', level: 'warn', scope: 'both' },
  { re: /--no-verify/, msg: '绕过提交钩子：会被记进 observe.md，下次开工会提醒', level: 'warn', scope: 'cmd' },
  { re: /\b(curl|wget)\b[^|]*\|\s*(ba|z)?sh\b/, msg: '管道执行远程脚本：先下载读一遍再执行', level: 'warn', scope: 'cmd' },
];

function readStdinSync() {
  try {
    const chunks = [];
    const buf = Buffer.alloc(65536);
    let n;
    // 同步读：hook 的 stdin 会在 JSON 送完后关闭，读到 EOF 即止
    while ((n = fs.readSync(0, buf, 0, buf.length, null)) > 0) chunks.push(Buffer.from(buf.subarray(0, n)));
    return Buffer.concat(chunks).toString('utf8');
  } catch { return ''; }
}

// 敏感载体（OI-18）：这些文件的价值全在「历史还在」。
// 整文件写入会把「已废弃 / [stale]」这类痕迹一起抹掉 —— 那是唯一不可逆的损失：
// 代码改错了能回滚，审计链断了就再也说不清「当时为什么这么定」。
const CARRIERS = [
  { re: /(^|[\\/])harness[\\/]memory[\\/]long-term\.md$/i, why: '长期记忆载体（含作废标记与命中计数）' },
  { re: /(^|[\\/])harness[\\/]memory[\\/]inbox\.md$/i, why: '记忆候选池（追加式，覆写会丢历史）' },
  { re: /(^|[\\/])harness[\\/]logs[\\/]gate\.log$/i, why: '门禁审计日志（只追加）' },
  { re: /(^|[\\/])harness[\\/]metrics\.md$/i, why: '度量流水（只追加，改历史等于伪造趋势）' },
];
const CARRIER_MARK = /已废弃|\[stale\]|作废|SUPERSEDED/i;

/** 纯判断，无副作用 —— 自测和真实运行走同一条路径，避免「测的和跑的不是一回事」
 *  existing / oldStr 由调用方注入，自测时可直接构造，不必真读写磁盘。 */
function check({ tool, file, body, existing = '', oldStr = '' }) {
  const isCmd = tool === 'Bash';
  const skip = !!file && SKIP_PATH.test(file);
  const bodyOk = isCmd || (!!file && CODE_EXT.test(file)) || (!!file && !!CARRIERS.find(c => c.re.test(file)));
  const res = { block: [], warn: [] };
  if (!bodyOk || !body) {
    // 载体文件即使是 .md 也要管，但没内容就没得判
  } else if (!skip) {
    for (const r of RULES) {
      if (r.scope === 'cmd' && !isCmd) continue;
      if (r.scope === 'content' && isCmd) continue;
      if (r.re.test(body)) res[r.level].push(r.msg);
    }
  }

  // 载体保护：只针对写文件（Bash 里用 sed -i 这类不在本守卫的判定范围，另见 §已知边界）
  if (!isCmd && file) {
    const carrier = CARRIERS.find(c => c.re.test(file));
    if (carrier) {
      // ① 整文件写入：文件已存在且有内容，就不是「新建」，而是「替换」
      if (tool === 'Write' && existing.trim()) {
        res.block.push(`${carrier.why}：整文件写入会抹掉历史痕迹 —— 用 Edit 局部改，或追加一行（追加不会被拦）`);
      }
      // ② 编辑时删掉作废标记：旧串里有标记、新串里没有，等于把「已废弃」洗成「没发生过」
      if (tool === 'Edit' && CARRIER_MARK.test(oldStr) && !CARRIER_MARK.test(body)) {
        res.block.push(`${carrier.why}：这次编辑删掉了「已废弃 / stale」标记 —— 审计链会断；保留原文，另起一行写原因`);
      }
    }
  }
  return res;
}

const CASES = [
  { name: '私钥块必拦', tool: 'Write', file: 'src/a.js', body: '-----BEGIN RSA PRIVATE KEY-----\nabc', expect: 'block' },
  { name: 'sk 密钥必拦', tool: 'Write', file: 'src/a.js', body: "const k = 'sk-abcdefghijklmnopqrstuvwxyz123456'", expect: 'block' },
  { name: 'DROP TABLE 必拦', tool: 'Write', file: 'db/001.sql', body: 'DROP TABLE users;', expect: 'block' },
  { name: '关 TLS 校验必拦', tool: 'Write', file: 'src/http.js', body: 'https.request({rejectUnauthorized: false})', expect: 'block' },
  { name: 'Bash 里 DROP 也拦', tool: 'Bash', file: '', body: 'psql -c "DROP TABLE orders"', expect: 'block' },
  { name: 'md 里的示例不拦', tool: 'Write', file: 'docs/x.md', body: '-----BEGIN RSA PRIVATE KEY-----', expect: 'ok' },
  { name: 'fixtures 不拦', tool: 'Write', file: 'bench/fixtures/a.sql', body: 'DROP TABLE t;', expect: 'ok' },
  { name: '普通代码放行', tool: 'Write', file: 'src/b.js', body: 'export const add = (a, b) => a + b;', expect: 'ok' },
  { name: 'env 引用放行', tool: 'Write', file: 'src/c.js', body: 'const k = process.env.API_KEY;', expect: 'ok' },
  { name: 'rm -rf 只提醒', tool: 'Bash', file: '', body: 'rm -rf ./tmp', expect: 'warn' },
  { name: '无 WHERE 的 DELETE 提醒', tool: 'Bash', file: '', body: 'mysql -e "DELETE FROM users"', expect: 'warn' },
  { name: 'force-with-lease 不提醒', tool: 'Bash', file: '', body: 'git push --force-with-lease origin feat', expect: 'ok' },

  // 载体保护（OI-18）：这些 case 决定「审计链会不会断」，必须有正反两侧
  { name: '载体被整写必拦', tool: 'Write', file: 'harness/memory/long-term.md', body: '2026-11-21 | 经验 | 只剩这一条了', existing: '2026-11-20 | 经验 | 旧条目 | src/a.js | - | 2027-01-01 | 3\n', expect: 'block' },
  { name: '载体新建放行', tool: 'Write', file: 'harness/memory/long-term.md', body: '# 长期记忆\n\n> 只收已验证的经验。\n', existing: '', expect: 'ok' },
  { name: '编辑删掉作废标记必拦', tool: 'Edit', file: 'harness/memory/long-term.md', body: '这条结论现在仍然成立', oldStr: '这条结论 已废弃', existing: 'x\n', expect: 'block' },
  { name: '编辑保留作废标记放行', tool: 'Edit', file: 'harness/memory/long-term.md', body: '这条结论 已废弃（2026-11-21 补：因为依赖已变）', oldStr: '这条结论 已废弃', existing: 'x\n', expect: 'ok' },
  { name: '普通文件整写不受影响', tool: 'Write', file: 'docs/readme.md', body: '# 随便写\n', existing: '# 旧内容\n', expect: 'ok' },
];

function selftest() {
  let bad = 0;
  for (const c of CASES) {
    const r = check(c);
    const got = r.block.length ? 'block' : r.warn.length ? 'warn' : 'ok';
    if (got !== c.expect) { bad++; console.log(`FAIL ${c.name}：期望 ${c.expect}，实得 ${got}`); }
  }
  if (bad) { console.log(`SELFTEST FAIL ${bad}/${CASES.length}`); process.exit(1); }
  console.log(`SELFTEST OK ${CASES.length} 条（${CASES.filter(c => c.expect === 'block').length} 必拦 / ${CASES.filter(c => c.expect === 'warn').length} 提醒 / ${CASES.filter(c => c.expect === 'ok').length} 放行）`);
  process.exit(0);
}

if (flag('--selftest')) selftest();

// ---------- 取输入：优先 --text/--file（自测与人手查），否则读 PreToolUse 的 stdin ----------
let payload = null;
const textOpt = opt('--text', null);
if (textOpt === null) {
  const raw = readStdinSync().trim();
  if (raw.startsWith('{')) { try { payload = JSON.parse(raw); } catch { } }
  else if (raw) payload = { tool_name: 'Bash', tool_input: { command: raw } };
}
const tool = (payload && payload.tool_name) || (textOpt !== null ? (flag('--cmd') ? 'Bash' : 'Write') : 'Bash');
const input = (payload && payload.tool_input) || {};
const file = opt('--file', input.file_path || input.path || input.target_file || '');
const body = textOpt !== null ? textOpt
  : (input.command || input.content || input.new_string || input.new_source || '');

const oldStr = String(input.old_string || '');
// 载体保护要判断「是不是在替换已有内容」，所以必须读磁盘现状
let existing = '';
try {
  if (file && tool !== 'Bash') {
    const abs = path.isAbsolute(file) ? file : path.join(process.cwd(), file);
    if (fs.existsSync(abs)) existing = fs.readFileSync(abs, 'utf8');
  }
} catch { }

const r = check({ tool, file, body: String(body), existing, oldStr });

if (r.block.length) {
  const reason = `VERDICT BLOCK（${tool}${file ? ' → ' + file : ''}）：\n` + r.block.map(x => '  - ' + x).join('\n')
    + '\n这不是风格问题，是会造成不可逆后果的写法。改完再写，或用 --text 单独复查。';
  console.log(JSON.stringify({
    hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: reason }
  }));
  console.error(reason);
  process.exit(2);
}

if (r.warn.length) {
  const msg = `VERDICT WARN（${tool}${file ? ' → ' + file : ''}）：` + r.warn.join('；');
  console.log(JSON.stringify({
    hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'allow', permissionDecisionReason: msg }
  }));
  console.error(msg);
  process.exit(0);
}

if (flag('--json')) console.log(JSON.stringify({ ok: true, verdict: 'PASS', tool, file }));
process.exit(0);
