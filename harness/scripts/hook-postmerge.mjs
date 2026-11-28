#!/usr/bin/env node
// 合并后留痕 —— 由 `harness.mjs install-hook` 写入的 post-merge 调用。
//
// 为什么需要它：git 的 `git merge` **不触发 pre-commit**（这是 git 的设计，
// 合并提交不是"你写的提交"）。所以合并进来的改动从未经过自检，而 post-commit
// 也不会为此报警 —— 形成静默绕过。这里补上：合并后如实记一条。
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = process.cwd();
const NODE = process.execPath;
const observe = path.join(ROOT, 'harness', 'scripts', 'observe.mjs');

if (!fs.existsSync(observe)) process.exit(0);

// 只在真的产生了合并提交时记（快进的 merge 不产生新提交，也就没"未检查的提交"）
let parents = '';
try {
  const r = spawnSync('git', ['rev-list', '--parents', '-1', 'HEAD'], { cwd: ROOT, encoding: 'utf8', timeout: 10000 });
  parents = (r.stdout || '').trim();
} catch { process.exit(0); }

const n = parents.split(/\s+/).filter(Boolean).length;
if (n < 3) process.exit(0);   // 只有 1 个父提交 = 不是合并提交

try {
  spawnSync(NODE, [observe, 'record',
    '--saw', '合并提交未经过自检',
    '--evidence', `post-merge: HEAD=${parents.split(/\s+/)[0]} 有 ${n - 1} 个父提交（git merge 不触发 pre-commit）`,
    '--conclusion', '合并后要自己跑一次 selfcheck；合进来的改动从未被检查过'
  ], { cwd: ROOT, encoding: 'utf8', timeout: 8000 });
} catch { }
process.exit(0);
