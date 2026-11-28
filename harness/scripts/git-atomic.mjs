#!/usr/bin/env node
// ai-workflow 原子提交 / worktree 隔离助手。零依赖，只调 git。默认输出极简（省 token）。
// 用法：
//   node scripts/git-atomic.mjs plan [--verbose|--json] [repo]
//   node scripts/git-atomic.mjs worktree <tool> [--exec] [repo]   分支 session/<tool>-<yyyymmdd>，工作树 ../<repo>-<tool>
//   node scripts/git-atomic.mjs merge <branch> [REQ] [repo]       只对 stage: done 的需求包给合并命令
//   node scripts/git-atomic.mjs list [repo]
//   node scripts/git-atomic.mjs clean <path> [repo]
// 安全：默认 dry-run；绝不自动 add/commit/merge/push/reset。不可逆动作只给命令。
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const argv = process.argv.slice(2);
const cmd = argv[0] || 'plan';
const verbose = argv.includes('--verbose'), asJson = argv.includes('--json');
const doExec = argv.includes('--exec');
const flags = new Set(['--verbose', '--json', '--exec']);
const pos = argv.slice(1).filter(a => !flags.has(a) && !a.startsWith('--'));

function git(repo, args) {
  // 只去尾部换行。trim() 会吃掉 porcelain 首行 " M path" 的前导空格，
  // 令下方 slice(3) 多切 1 字符（.trae/skills → trae/skills）。
  const out = s => s.replace(/\r?\n+$/, '');
  try { return out(execFileSync('git', args, { cwd: repo, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })); }
  catch (e) { return out((e.stdout || '').toString()); }
}
function isRepo(p) { return fs.existsSync(p) && git(p, ['rev-parse', '--git-dir']).length > 0; }

// 从参数中找仓库：显式给出的最后一个位置参数，否则 cwd
let repo = pos.find(p => isRepo(p)) || (isRepo(process.cwd()) ? process.cwd() : null);
if (!repo && cmd === 'plan') repo = pos[0];

const TYPE_BY_PATH = [
  [/(\/|^)(test|tests|__tests__|spec)(\/|$)/i, 'test'],
  [/\.(test|spec)\.[cm]?[jt]sx?$/i, 'test'],
  [/(\/|^)(docs?|README|CHANGELOG)(\/|$|\.)/i, 'docs'],
  [/\.(md|mdx)$/i, 'docs'],
  [/(\/|^)(migrations?|prisma)(\/|$)/i, 'build'],
  [/(\/|^)(\.github|\.husky)(\/|$)/i, 'ci'],
  [/(package(-lock)?\.json|tsconfig|vite\.config|webpack\.config|ecosystem\.config|\.env\.example)/i, 'chore'],
  [/(\/|^)scripts?\//i, 'chore'],
];
function typeOf(p) {
  for (const [re, t] of TYPE_BY_PATH) if (re.test('/' + p)) return t;
  return 'src'; // 源码：feat/fix 由人定，脚本不猜
}
function groupOf(p) {
  const parts = p.split(/[\\/]/);
  return parts.length > 1 ? parts[0] : '(root)';
}

// ---------- plan ----------
if (cmd === 'plan') {
  if (!repo || !isRepo(repo)) { console.log('NOT_A_GIT_REPO'); process.exit(0); }
  const por = git(repo, ['status', '--porcelain']).split('\n').filter(Boolean);
  const files = por.map(l => l.slice(3).trim()).filter(Boolean);
  const warns = [];
  // 两轮密钥扫描：文件名 + 内容级
  const secretName = /(^|[\\/])(\.env(\.|$)|.*(secret|credential|\.pem|token).*)/i;
  const nameHits = files.filter(f => secretName.test(f));
  if (nameHits.length) warns.push(`SECRET_NAME ${nameHits.join(',')}`);
  const contentHits = [];
  let nulHits = [], debugHits = [];
  // 调试残留：整行须同时含 DEBUG 标记 + 日志调用（避免规则文本/正则定义/示例自命中）
  const DEBUG_RE = /\[DEBUG-[A-Za-z0-9_-]+\]/;
  const LOG_RE = /\b(console\.(log|debug|info|warn|error)|print\(|printf\(|logger\.|NSLog|Log\.)/;
  for (const f of files) {
    const fp = path.join(repo, f);
    if (!fs.existsSync(fp) || fs.statSync(fp).isDirectory()) continue;
    let buf; try { buf = fs.readFileSync(fp); } catch { continue; }
    if (buf.includes(0)) nulHits.push(f);
    const txt = buf.toString('utf8');
    if (/(api[_-]?key|secret|password|token)\s*[:=]\s*["'][^"']{8,}/i.test(txt) || /\bsk-[A-Za-z0-9]{12,}/.test(txt)) contentHits.push(f);
    const isDebug = !/\.mdx?$/i.test(f) && txt.split('\n').some(l => DEBUG_RE.test(l) && LOG_RE.test(l));
    if (isDebug) debugHits.push(f);
  }
  if (contentHits.length) warns.push(`SECRET_CONTENT? ${contentHits.join(',')}`);
  if (nulHits.length) warns.push(`NUL_BYTE ${nulHits.join(',')}`);
  if (debugHits.length) warns.push(`DEBUG_LEFT ${debugHits.join(',')}`);

  // 按「顶层目录 × 类型」聚类 = 关注点分离
  const groups = new Map();
  for (const f of files) {
    const key = `${groupOf(f)}:${typeOf(f)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(f);
  }
  const plan = [...groups.entries()].map(([key, fs2]) => {
    const [grp, type] = key.split(':');
    return { group: grp, type, count: fs2.length, files: fs2 };
  }).sort((a, b) => a.group.localeCompare(b.group) || a.type.localeCompare(b.type));

  const oversize = files.filter(f => {
    const fp = path.join(repo, f);
    if (!fs.existsSync(fp)) return false;
    try { return fs.readFileSync(fp, 'utf8').split('\n').length > 1000; } catch { return false; }
  });

  if (asJson) {
    console.log(JSON.stringify({ repo, fileCount: files.length, atomic: plan.length,
      oversize, warns, plan }, null, 2));
  } else {
    if (!files.length) console.log('CLEAN · 无改动');
    else {
      console.log(`改动 ${files.length} 文件 → 建议 ${plan.length} 个原子提交` + (plan.length > 1 ? '（关注点已分离，勿合成 1 个）' : ''));
      for (const g of plan) {
        const head = `  [${g.type}] ${g.group} · ${g.count} 文件`;
        console.log(verbose ? `${head}\n      git add ${g.files.join(' ')}` : head);
      }
      if (!verbose && plan.length) console.log('  (--verbose 出 git add 清单)');
    }
    if (oversize.length) console.log(`WARN >1000行需拆: ${oversize.join(',')}`);
    warns.forEach(w => console.log(`WARN ${w}`));
    if (!warns.length && files.length) console.log('卫生检查 OK · 无密钥/NUL/DEBUG 残留');
  }
  process.exit(0);
}

// ---------- worktree ----------
// 命名按 docs/adr/0006-worktree-per-session.md：分支 session/<tool>-<yyyymmdd>，工作树 <同级>/<repo>-<tool>
function localDate(d = new Date()) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}`;
}

if (cmd === 'worktree') {
  const tool = pos.find(p => !isRepo(p));
  if (!repo) { console.log('NOT_A_GIT_REPO'); process.exit(0); }
  if (!tool) { console.log('用法：worktree <tool> [--exec]'); process.exit(1); }
  if (!/^[A-Za-z0-9_-]{1,32}$/.test(tool)) {
    console.log(`FAIL 工具名非法（只允许字母数字下划线短横，1-32 位）：${tool}`);
    process.exit(1);
  }
  const branch = `session/${tool}-${localDate()}`;
  const wtRel = `../${path.basename(repo)}-${tool}`;
  const wtPath = path.resolve(repo, '..', `${path.basename(repo)}-${tool}`);
  const registered = git(repo, ['worktree', 'list', '--porcelain']).includes(wtPath);
  const branchExists = git(repo, ['rev-parse', '--verify', '--quiet', branch]).length > 0;
  if (registered || fs.existsSync(wtPath)) {
    console.log(`REUSE · 工作树已存在，复用：${wtRel}`);
    console.log(`  分支 ${branch}${branchExists ? '（已存在）' : '（尚未创建）'}`);
    process.exit(0);
  }
  const addArgs = branchExists ? [wtPath, branch] : [wtPath, '-b', branch, 'main'];
  const cmds = [`git -C "${repo}" worktree add "${wtRel}"${branchExists ? '' : ' -b'} ${branch}${branchExists ? '' : ' main'}`];
  if (asJson) console.log(JSON.stringify({ repo, branch, worktree: wtPath, worktreeRel: wtRel, commands: cmds, executed: false }, null, 2));
  else if (!doExec) {
    console.log(`DRY-RUN · 分支 ${branch} → ${wtRel}`);
    console.log(cmds[0]);
    console.log('加 --exec 真创建；创建后先跑测试基线再动手（见 references/git-workflow.md §2）');
  }
  if (doExec) {
    try {
      execFileSync('git', ['-C', repo, 'worktree', 'add', ...addArgs], { stdio: 'inherit' });
      console.log(`OK ${branch} → ${wtRel}`);
    } catch (e) { console.log(`FAIL ${String(e.message).split('\n')[0]}`); process.exit(1); }
  }
  process.exit(0);
}

// ---------- merge ----------
// 归档门槛：需求包 state.md frontmatter 的 stage 必须是 done；未收口只警示，不给合并命令
function readStage(reqDir) {
  const f = path.join(reqDir, 'state.md');
  if (!fs.existsSync(f)) return null;
  const m = fs.readFileSync(f, 'utf8').match(/^stage:\s*(\S+)\s*$/m);
  return m ? m[1] : null;
}

if (cmd === 'merge') {
  const branch = pos.find(p => !isRepo(p));
  if (!repo) { console.log('NOT_A_GIT_REPO'); process.exit(0); }
  if (!branch) { console.log('用法：merge <branch> [REQ]'); process.exit(1); }
  if (!/^session\//.test(branch)) { console.log(`FAIL 只处理 session/* 分支：${branch}`); process.exit(1); }
  if (git(repo, ['rev-parse', '--verify', '--quiet', branch]).length === 0) {
    console.log(`FAIL 分支不存在：${branch}`); process.exit(1);
  }
  const reqRoot = path.join(repo, 'harness', 'requirements');
  let reqId = pos.filter(p => !isRepo(p))[1];
  if (reqId && !fs.existsSync(path.join(reqRoot, reqId))) {
    console.log(`FAIL 需求包不存在：${reqId}`); process.exit(1);
  }
  if (!reqId) {
    const dones = fs.existsSync(reqRoot) ? fs.readdirSync(reqRoot)
      .filter(n => readStage(path.join(reqRoot, n)) === 'done') : [];
    if (dones.length === 0) {
      console.log('未归档 · 没有 stage: done 的需求包，不给合并命令（先收口：state.md 的 stage 改 done）');
      process.exit(0);
    }
    if (dones.length > 1) {
      console.log(`FAIL 有多个已归档需求包，请指定：merge ${branch} <REQ>（${dones.join(', ')}）`);
      process.exit(1);
    }
    reqId = dones[0];
  }
  const stage = readStage(path.join(reqRoot, reqId));
  if (stage !== 'done') {
    console.log(`未归档 · ${reqId} 的 stage=${stage || '缺失'}，不给合并命令（收口后再来）`);
    process.exit(0);
  }
  const tool = branch.replace(/^session\//, '').replace(/-\d{8}$/, '') || branch;
  const wtRel = `../${path.basename(repo)}-${tool}`;
  console.log(`已归档 · ${reqId}（stage: done）· 只给命令，不代执行`);
  console.log('1) 合入 main（在主工作区执行）：');
  console.log(`   git -C "${repo}" checkout main`);
  console.log(`   git -C "${repo}" pull --ff-only`);
  console.log(`   git -C "${repo}" merge --no-ff ${branch} -m "merge ${branch}"`);
  console.log('   # 冲突按「需求包目录 + ADR-0002 记忆条目」解决；脚本不代执行');
  console.log(`2) 删工作树：git -C "${repo}" worktree remove "${wtRel}"`);
  console.log(`3) 删分支：git -C "${repo}" branch -d ${branch}`);
  process.exit(0);
}

if (cmd === 'list') {
  if (!repo) { console.log('NOT_A_GIT_REPO'); process.exit(0); }
  console.log(git(repo, ['worktree', 'list']) || '(none)');
  process.exit(0);
}

if (cmd === 'clean') {
  if (!repo) { console.log('NOT_A_GIT_REPO'); process.exit(0); }
  const wt = pos.find(p => !isRepo(p));
  if (!wt) { console.log('用法：clean <worktree路径>'); process.exit(1); }
  console.log('不可逆 · 只给命令，不代执行（确认无未提交改动后再跑）：');
  console.log(`  git -C "${repo}" worktree remove "${wt}"`);
  console.log(`  git -C "${repo}" branch -d <分支名>   # 合并后删短分支`);
  process.exit(0);
}

console.log('用法: plan | worktree <tool> [--exec] | merge <branch> [REQ] | list | clean <path>');
