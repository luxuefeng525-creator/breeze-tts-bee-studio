#!/usr/bin/env node
// 开放标准合规检查（OI-20）：调用官方 skills-ref 校验全部技能，输出机器可读结果。
//
// 定位：**补充校验，不是主判据**。
// - 官方 skills-ref（agentskills/agentskills，Apache-2.0）自称 demonstration only，不适合生产；
// - 它只认规范里的六个 frontmatter 字段，而本包有意使用宿主扩展字段
//   （version / author / tags / user-invocable / argument-hint / model / tools 等），
//   这些字段删了会破坏宿主行为 —— 所以不合规项只报告、不阻断。
// - 自建 audit-skills.mjs 仍是发布闸门的主判据。
//
// 工具发现顺序：--cli <路径> > SKILLS_REF_CLI 环境变量 > 项目约定路径 > PATH。
// 找不到工具时输出 STANDARD tool=missing 且退出码 0（保持零依赖可移植），由调用方决定提示口径。
//
// 用法：
//   node scripts/skills-standard.mjs [--root <包根>] [--cli <skills-ref 路径>] [--json]
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);
const opt = (n, d) => { const i = args.indexOf(n); return i >= 0 && args[i + 1] !== undefined ? args[i + 1] : d; };
const ROOT = opt('--root', path.join(path.dirname(fileURLToPath(import.meta.url)), '..'));
const AS_JSON = args.includes('--json');
// 与安装步骤对应：官方仓库 clone 到 ~/.workbuddy/tools/skills-ref-official，venv 内 CLI。
// Windows 的 venv 可执行文件在 Scripts/ 且带 .exe；macOS/Linux 在 bin/ 且无后缀。
const VENV_CLI = process.platform === 'win32'
  ? path.join('.venv', 'Scripts', 'skills-ref.exe')
  : path.join('.venv', 'bin', 'skills-ref');
const DEFAULT_CLI = path.join(os.homedir(), '.workbuddy', 'tools', 'skills-ref-official', 'skills-ref', VENV_CLI);
const CLI = opt('--cli', process.env.SKILLS_REF_CLI || (fs.existsSync(DEFAULT_CLI) ? DEFAULT_CLI : 'skills-ref'));

function resolveCli() {
  if (fs.existsSync(CLI)) return CLI;
  const probe = spawnSync(CLI, ['--version'], { encoding: 'utf8', timeout: 10000 });
  return probe.status === 0 ? CLI : null;
}

function run() {
  const cli = resolveCli();
  if (!cli) return { tool: 'missing', total: 0, invalid: [], detail: '未找到官方 skills-ref；标准合规校验跳过' };
  const skillRoot = path.join(ROOT, 'skills');
  const dirs = fs.readdirSync(skillRoot, { withFileTypes: true })
    .filter(e => e.isDirectory() && !['in-progress', 'deprecated', '.releases'].includes(e.name))
    .map(e => e.name).sort();
  const invalid = [];
  const errors = [];
  let version = '';
  const vp = spawnSync(cli, ['--version'], { encoding: 'utf8', timeout: 10000 });
  version = (vp.stdout || '').trim() || 'unknown';
  for (const name of dirs) {
    const r = spawnSync(cli, ['validate', path.join(skillRoot, name)], { encoding: 'utf8', timeout: 30000 });
    // 超时 / 进程崩溃不是「不合规」—— 归错类会诱导别人删掉本该保留的宿主扩展字段
    if (r.error || r.status === null) { errors.push(`${name}: ${r.error ? r.error.code || r.error.message : 'no exit status'}`); continue; }
    if (r.status === 0) continue;
    const msg = ((r.stdout || '') + (r.stderr || '')).trim().split('\n').map(s => s.trim()).filter(Boolean).slice(-1)[0] || 'unknown error';
    invalid.push({ name, reason: msg });
  }
  if (errors.length) return { tool: 'error', version, total: dirs.length, invalid, errors, detail: `${errors.length} 个技能校验进程异常，未计入不合规` };
  return { tool: 'ok', version, total: dirs.length, invalid, detail: `${dirs.length - invalid.length}/${dirs.length} 合规` };
}

const r = run();
if (AS_JSON) console.log(JSON.stringify(r));
else {
  if (r.tool === 'missing') console.log('STANDARD tool=missing · ' + r.detail);
  else {
    console.log(`STANDARD tool=${r.tool} version=${r.version} · ${r.detail}`);
    for (const x of r.invalid) console.log(`  WARN ${x.name}: ${x.reason}`);
    for (const e of r.errors || []) console.log(`  ERROR ${e}`);
    if (r.invalid.length) console.log(`  ↑ 宿主扩展字段（version/tags/model 等）非规范字段，仅报告不阻断；删字段会破坏宿主行为`);
  }
}
// 工具缺失或存在不合规都返回 0 —— 本脚本是报告器，判定权在 release 闸门
process.exit(0);
