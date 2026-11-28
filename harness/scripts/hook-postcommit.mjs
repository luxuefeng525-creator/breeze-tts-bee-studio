#!/usr/bin/env node
// 提交后判定是否绕过了自检 —— 由 `harness.mjs install-hook` 写入的 post-commit 调用。
//
// 为什么这个办法成立：git 的 `--no-verify` 只跳过 pre-commit 与 commit-msg，
// **不跳过 post-commit**。所以「post-commit 跑了，但标记与 HEAD 对不上」就等价于
// 「这次提交没经过自检」。
//
// 标记格式：`<ok|pass> <tree> <毫秒时间戳> <原因>`
//   ok   = 自检跑过且通过
//   pass = 自检被跳过但已留痕（超时 / 脚本自身出错 / 脚本缺失）—— 这不算「绕过」，
//          若记成绕过会冤枉正常提交，所以分开记。
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = process.cwd();
const NODE = process.execPath;
const observe = path.join(ROOT, 'harness', 'scripts', 'observe.mjs');
const STALE_MS = 10 * 60 * 1000;   // 超过 10 分钟的标记不算数（防上一次提交的残留顶包）

/** worktree 里 .git 是文件不是目录 —— 必须问 git 真实路径，不能硬拼 */
function gitPath(what) {
  try {
    const r = spawnSync('git', ['rev-parse', '--git-path', what], { cwd: ROOT, encoding: 'utf8', timeout: 10000 });
    const p = (r.stdout || '').trim();
    return p ? path.resolve(ROOT, p) : path.resolve(ROOT, '.git', what);
  } catch { return path.resolve(ROOT, '.git', what); }
}

function note(saw, evidence, conclusion) {
  if (!fs.existsSync(observe)) {
    console.error('[harness] 没找到 observe.mjs，本次留痕跳过（观察池里不会有记录）');
    return;
  }
  try {
    const r = spawnSync(NODE, [observe, 'record', '--saw', saw, '--evidence', evidence, '--conclusion', conclusion],
      { cwd: ROOT, encoding: 'utf8', timeout: 8000 });
    // 静默失败会让「记录丢了」看起来像「事情没发生」—— 必须说一声
    if (r.status !== 0) {
      console.error('[harness] 留痕被拒：' + ((r.stderr || r.stdout || '').trim().split('\n').slice(-1)[0] || '未知原因'));
    }
  } catch (e) {
    console.error('[harness] 留痕异常：' + String((e && e.message) || e));
  }
}

const markFile = gitPath('harness-last-check');
let kind = '', tree = '', ts = 0, reason = '-';
if (fs.existsSync(markFile)) {
  const parts = fs.readFileSync(markFile, 'utf8').trim().split(/\s+/);
  kind = parts[0] || '';
  tree = parts[1] || '';
  ts = Number(parts[2]) || 0;
  reason = parts.slice(3).join(' ') || '-';
}

let headTree = '';
try {
  const r = spawnSync('git', ['rev-parse', 'HEAD^{tree}'], { cwd: ROOT, encoding: 'utf8', timeout: 10000 });
  headTree = (r.stdout || '').trim();
} catch { }

const fresh = ts > 0 && (Date.now() - ts) < STALE_MS;
const matched = !!kind && !!headTree && tree === headTree && fresh;

// 正常路径：标记对得上且新鲜
if (matched) {
  if (kind === 'pass') {
    // 放行不是通过 —— 如实记，但不冤枉成「绕过」
    note('提交时自检被放行（未真正检查）',
      `原因：${reason}；复核：git show ${headTree}；post-commit exit=0`,
      '放行不等于通过，这次提交的质量是未知的；同类原因出现多次要修掉而不是习惯它');
  }
  try { fs.rmSync(markFile, { force: true }); } catch { }
  process.exit(0);
}

// 其余情况：判定为绕过，并把原因写准
let why;
if (!kind) why = '没有自检标记（--no-verify、或 hook 未装、或 git merge 产生的合并提交）';
else if (!fresh) why = `标记过期（${Math.round((Date.now() - ts) / 60000)} 分钟前），本次提交没有新的检查记录`;
else if (tree !== headTree) why = '标记与 HEAD 的 tree 不一致（暂存区在检查后被改过，或用了 --amend）';
else why = '未知';

note('提交绕过了自检',
  `复核：git show ${headTree || 'HEAD'}；标记 kind=${kind || '(无)'} tree=${tree || '(无)'}；原因：${why}；post-commit exit=0`,
  '下次 begin 会提示；绕过需在 PR 里写明理由');

try { fs.rmSync(markFile, { force: true }); } catch { }
process.exit(0);
