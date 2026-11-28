#!/usr/bin/env node
// 提交前自检 —— 由 `harness.mjs install-hook` 写入的 pre-commit 调用。
//
// 设计要点（别改坏了）：
// 1. **宁可放行也不把人卡住**。卡住人 = 逼人用 --no-verify = 机制作废。
//    所以超时、脚本缺失、异常，一律放行并留痕，只有「自检明确报不通过」才阻断。
// 2. 逻辑放在 node 里而不是 hook 的 sh 里 —— Git Bash 的 timeout 与 Windows 的
//    timeout.exe 同名不同物，shell 层做超时不可靠；node 的 spawnSync timeout 跨平台一致。
// 3. 通过时写 `.git/harness-last-check`（tree hash），post-commit 用它判断是否被绕过。
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = process.cwd();
// 阈值不能贴着常态耗时设：本机实测 selfcheck 约 14 秒（含多个子进程自测），旧值 30 秒只剩一倍余量，
// 系统负载一高就触顶放行，等于门禁静默失效。取 90 秒 = 常态的 6 倍余量；真卡死时最多等 90 秒才放行。
const TIMEOUT_MS = 90000;
const NODE = process.execPath;
const BRIEF = process.argv.includes('--brief');
const selfcheck = path.join(ROOT, 'harness', 'scripts', 'selfcheck.mjs');
const observe = path.join(ROOT, 'harness', 'scripts', 'observe.mjs');

/** worktree 里 .git 是文件不是目录 —— 必须问 git 真实路径，不能硬拼 */
function gitPath(what) {
  try {
    const r = spawnSync('git', ['rev-parse', '--git-path', what], { cwd: ROOT, encoding: 'utf8', timeout: 10000 });
    const p = (r.stdout || '').trim();
    return p ? path.resolve(ROOT, p) : repoRoot(what);
  } catch { return repoRoot(what); }
}
function repoRoot(what) { return path.resolve(ROOT, '.git', what); }

/** 写标记：ok = 检查通过；pass = 检查被跳过但已留痕（超时/脚本出错/缺脚本）。
 *  pass 必须写，否则 post-commit 会把「放行」误记成「绕过」。 */
function writeMark(kind, tree, reason) {
  try {
    const f = gitPath('harness-last-check');
    fs.mkdirSync(path.dirname(f), { recursive: true });
    fs.writeFileSync(f, [kind, tree || '-', String(Date.now()), reason || '-'].join(' '), 'utf8');
  } catch { }
}

function note(saw, evidence, conclusion) {
  if (!fs.existsSync(observe)) return;
  try {
    const r = spawnSync(NODE, [observe, 'record', '--saw', saw, '--evidence', evidence, '--conclusion', conclusion],
      { cwd: ROOT, encoding: 'utf8', timeout: 8000 });
    // 留痕失败不能反过来阻断提交，但必须让人看见它失败了
    if (r.status !== 0) console.error('[harness] 留痕被拒：' + ((r.stderr || r.stdout || '').trim().split('\n').slice(-1)[0] || '未知原因'));
  } catch (e) {
    console.error('[harness] 留痕异常：' + String((e && e.message) || e));
  }
}

// 找不到自检脚本：放行（新工程刚 init、或用户删了脚本）
if (!fs.existsSync(selfcheck)) {
  console.log('[harness] 没找到 harness/scripts/selfcheck.mjs，本次放行');
  writeMark('pass', '', 'selfcheck 缺失');
  process.exit(0);
}

const r = spawnSync(NODE, [selfcheck, ...(BRIEF ? ['--brief'] : [])], { cwd: ROOT, encoding: 'utf8', timeout: TIMEOUT_MS });
const out = `${r.stdout || ''}${r.stderr || ''}`;

// 超时：放行 + 留痕
const timedOut = r.error && (r.error.code === 'ETIMEDOUT' || r.signal === 'SIGTERM');
if (timedOut) {
  console.log(`[harness] 自检超过 ${TIMEOUT_MS / 1000} 秒，本次放行（已留痕）`);
  note('pre-commit 自检超时', `node harness/scripts/selfcheck.mjs 超过 ${TIMEOUT_MS}ms 未返回`, '某项检查可能卡住，需排查后重装或加白');
  writeMark('pass', '', '自检超时');
  process.exit(0);
}

// 跑崩了（不是「检查不通过」而是脚本自身出错）：放行 + 留痕
if (r.status !== 0 && r.status !== 1) {
  console.log(`[harness] 自检脚本自身出错（exit=${r.status}），本次放行（已留痕）`);
  note('pre-commit 自检脚本自身出错', `selfcheck exit=${r.status}，输出尾部：${out.trim().split('\n').slice(-2).join(' / ')}`, '自检脚本有 bug，需修');
  const tr = spawnSync('git', ['write-tree'], { cwd: ROOT, encoding: 'utf8', timeout: 10000 });
  writeMark('pass', (tr.stdout || '').trim(), '自检脚本自身出错');
  process.exit(0);
}

// 明确不通过：阻断
if (r.status === 1) {
  console.log(BRIEF ? out.trim() : out);
  console.log('');
  console.log('[harness] 自检不通过 —— 提交已被阻断。');
  console.log('[harness] 修完再提交。确需绕过：git commit --no-verify（会被记下来，下次开工提示）');
  process.exit(1);
}

// 通过：记下本次暂存区的 tree hash，供 post-commit 判断是否被绕过
try {
  const t = spawnSync('git', ['write-tree'], { cwd: ROOT, encoding: 'utf8', timeout: 10000 });
  writeMark('ok', (t.stdout || '').trim(), '-');
} catch { /* 标记写不上不影响提交，只是 post-commit 会多留一条痕 */ }
if (BRIEF && out.trim()) console.log(out.trim());

process.exit(0);
