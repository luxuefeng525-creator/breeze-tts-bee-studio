#!/usr/bin/env node
// UI 交付门禁：把「好看」里能机器判的部分判掉。
// 用法：node ui-gate.mjs --css <文件或目录> [--tokens tokens.json] [--shots <截图目录>] [--json]
//
// 只管下限，不管手感：
//   ① 正文对比度 ≥ 4.5:1（大字号 ≥ 3:1）—— 可读性，不是审美
//   ② 字阶成阶梯（相邻比 1.1~1.7，字号种类 ≤ 8）—— 不是随手 13/14/15/17
//   ③ 间距在 4 的倍数上，且优先 8/12/16/24/32 —— 节奏感来自克制
//   ④ 颜色只用约束卡声明的 token（给了 --tokens 才判）
//   ⑤ 三视口截图证据齐全（给了 --shots 才判），且不是旧图
//
// 明确不做的：布局是否好看、层级是否清楚、动效手感 —— 这些要人看真实截图。
// 只做静态检查，不启动浏览器（零依赖是这套东西的底线）。
import fs from 'node:fs';
import path from 'node:path';

const argv = process.argv.slice(2);
const flag = n => argv.includes(n);
const opt = (n, d) => { const i = argv.indexOf(n); return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : d; };
const asJson = flag('--json');
const cssArg = opt('--css', null);
const tokensArg = opt('--tokens', null);
const shotsArg = opt('--shots', null);
const maxAgeH = Number(opt('--max-age-hours', '24'));

if (!cssArg) {
  console.log('用法：ui-gate.mjs --css <文件或目录> [--tokens tokens.json] [--shots <截图目录>] [--json]');
  process.exit(2);
}

/* ---------- 收集文件 ---------- */
function walk(p, out = []) {
  if (!fs.existsSync(p)) return out;
  const st = fs.statSync(p);
  if (st.isDirectory()) {
    for (const x of fs.readdirSync(p)) {
      if (/^(node_modules|\.git|dist|build)$/.test(x)) continue;
      walk(path.join(p, x), out);
    }
  } else if (/\.(css|scss|less|html?)$/i.test(p)) out.push(p);
  return out;
}
const files = walk(cssArg);

/* ---------- 颜色 ---------- */
const NAMED = {
  white: '#ffffff', black: '#000000', red: '#ff0000', green: '#008000', blue: '#0000ff',
  gray: '#808080', grey: '#808080', silver: '#c0c0c0', maroon: '#800000', olive: '#808000',
  lime: '#00ff00', aqua: '#00ffff', teal: '#008080', navy: '#000080', fuchsia: '#ff00ff',
  purple: '#800080', yellow: '#ffff00', orange: '#ffa500'
};
function parseColor(s) {
  if (!s) return null;
  let v = String(s).trim().toLowerCase();
  const nm = NAMED[v];
  if (nm) v = nm;
  let m = v.match(/^#([0-9a-f]{3,8})$/);
  if (m) {
    let h = m[1];
    if (h.length === 3) h = h.split('').map(c => c + c).join('');
    if (h.length === 4) h = h.slice(0, 3).split('').map(c => c + c).join('');   // #rgba -> 忽略 alpha
    if (h.length < 6) return null;
    return { r: parseInt(h.slice(0, 2), 16), g: parseInt(h.slice(2, 4), 16), b: parseInt(h.slice(4, 6), 16), a: h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1, raw: v };
  }
  m = v.match(/^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:[\s,/]+([\d.]+%?))?\s*\)$/);
  if (m) {
    const a = m[4] === undefined ? 1 : (m[4].endsWith('%') ? parseFloat(m[4]) / 100 : parseFloat(m[4]));
    return { r: +m[1], g: +m[2], b: +m[3], a, raw: v };
  }
  return null;
}
const srgb = c => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
const lum = c => 0.2126 * srgb(c.r) + 0.7152 * srgb(c.g) + 0.0722 * srgb(c.b);
// 把可能半透明的颜色合成到不透明底上；alpha >= 1 原样返回。静态分析拿不到真实层叠，
// 「半透明落回页面底色」是能做到的最近似 —— 否则 rgba(255,255,255,0.02) 会被当成不透明浅色，
// 深色主题上一次报几十处「浅底深字」误报。
function over(top, base) {
  const a = top.a === undefined ? 1 : top.a;
  if (a >= 1) return { r: top.r, g: top.g, b: top.b, a: 1, raw: top.raw };
  const u = base || { r: 255, g: 255, b: 255, a: 1, raw: '衬底' };
  return {
    r: Math.round(top.r * a + u.r * (1 - a)),
    g: Math.round(top.g * a + u.g * (1 - a)),
    b: Math.round(top.b * a + u.b * (1 - a)), a: 1,
    raw: `${top.raw}→${u.raw}`
  };
}
function contrast(fg, bg, under) {
  // 背景先落到衬底（不传就默认白），前景半透明再与合成后的背景比
  const base = over(bg, under);
  const mix = over(fg, base);
  const l1 = lum(mix), l2 = lum(base);
  const hi = Math.max(l1, l2), lo = Math.min(l1, l2);
  return (hi + 0.05) / (lo + 0.05);
}

/* ---------- 解析 CSS 规则 ---------- */
function rules(text, file) {
  const out = [];
  const clean = text.replace(/\/\*[\s\S]*?\*\//g, '');
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(clean))) {
    const sel = m[1].trim().replace(/\s+/g, ' ');
    const body = m[2];
    const decl = {};
    for (const part of body.split(';')) {
      const i = part.indexOf(':');
      if (i < 0) continue;
      const k = part.slice(0, i).trim().toLowerCase();
      const v = part.slice(i + 1).trim();
      if (k && v) decl[k] = v;
    }
    if (!Object.keys(decl).length) continue;
    const line = clean.slice(0, m.index).split('\n').length;
    out.push({ sel, decl, file, line });
  }
  return out;
}

const allRules = [];
for (const f of files) {
  try {
    let txt = fs.readFileSync(f, 'utf8');
    // HTML 里只取 style 块：否则标签会和选择器粘在一起，变量表与颜色声明全部落空
    if (/\.html?$/i.test(f)) {
      txt = [...txt.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/gi)].map(m => m[1]).join('\n');
    }
    if (txt.trim()) allRules.push(...rules(txt, f));
  } catch { }
}

// ---------- 变量解析：现代 CSS 都在用 var(--x)，不解析等于什么都没查 ----------
// 只从 :root / html / body 收变量（深度 <=4，避免循环引用）。
// 不用  之类的边界符 —— 之前被写成退格符，条件永远不成立、变量表恒空
const VARS = {};
const isRootSel = sel => sel.split(',').some(x => {
  const t = x.trim();
  return t === ':root' || t === 'html' || t === 'body';
});
for (const r of allRules) {
  if (!isRootSel(r.sel)) continue;
  for (const k of Object.keys(r.decl)) if (k.startsWith('--')) VARS[k] = r.decl[k];
}
function resolveV(v, depth = 0) {
  if (typeof v !== 'string' || depth > 4 || v.indexOf('var(') < 0) return v;
  return v.replace(/var\(\s*(--[\w-]+)\s*(?:,\s*([^)]*))?\)/g, (_, name, fb) => {
    const val = VARS[name] !== undefined ? VARS[name] : (fb || '');
    return resolveV(String(val).trim(), depth + 1);
  });
}
for (const r of allRules) {
  for (const k of Object.keys(r.decl)) r.decl[k] = resolveV(r.decl[k]);
}
if (!Object.keys(VARS).length) {
  // 没有变量不影响判定，但让人知道「这条链路没被覆盖」
  process.stderr.write('');
}

const results = [];
const add = (name, ok, detail, severity = 'fatal') => results.push({ name, ok, detail, severity });

/* ---------- ① 对比度 ---------- */
{
  const pageBg = (() => {
    // 复用 isRootSel：`html, body { ... }` 是分组选择器，整串不等于 html/body/:root，
    // 用 /^(html|body|:root)$/ 匹配会落空、兜底成默认白 —— 深色页就此整页误报
    const WHITE = { r: 255, g: 255, b: 255, a: 1, raw: '默认白' };
    for (const r of allRules) {
      if (!isRootSel(r.sel)) continue;
      const c = parseColor(r.decl['background-color'] || r.decl['background']);
      if (c) return over(c, WHITE);      // 页面底色本身半透明也落到白，保证衬底不透明
    }
    return WHITE;
  })();

  const bad = [];
  for (const r of allRules) {
    const fg = parseColor(r.decl['color']);
    if (!fg) continue;
    const ownBg = parseColor(r.decl['background-color'] || r.decl['background']);
    const bg = ownBg || pageBg;
    if (!bg) continue;
    const fs_ = parseFloat(r.decl['font-size'] || '16');
    const bold = /(bold|[6-9]00)/.test(r.decl['font-weight'] || '');
    const large = fs_ >= 24 || (bold && fs_ >= 18.66);
    const need = large ? 3 : 4.5;
    const c = contrast(fg, bg, pageBg);
    if (c + 1e-9 < need) {
      // 报合成后的前景与衬底，不报声明值：印成 rgba(...) 会让人以为没合成，等于把刚修掉的误报叙事又写回去
      const base = over(bg, pageBg);
      const mix = over(fg, base);
      bad.push(`${path.basename(r.file)}:${r.line} ${r.sel} — ${mix.raw} on ${base.raw} = ${c.toFixed(2)}:1（需 ≥${need}）`);
    }
  }
  add('正文对比度达标', bad.length === 0,
    bad.length ? `${bad.length} 处不达标：${bad.slice(0, 4).join('；')}${bad.length > 4 ? ' …' : ''}`
      : `${allRules.filter(r => r.decl['color']).length} 条声明了前景色，全部 ≥4.5:1（大字号 ≥3:1）`);
}

/* ---------- ② 字阶 ---------- */
{
  const sizes = new Map();
  for (const r of allRules) {
    const v = r.decl['font-size'];
    if (!v) continue;
    const px = parseFloat(v);
    if (!isFinite(px)) continue;
    if (!sizes.has(px)) sizes.set(px, `${path.basename(r.file)}:${r.line}`);
  }
  const list = [...sizes.keys()].sort((a, b) => a - b);
  const problems = [];
  if (list.length > 8) problems.push(`字号种类 ${list.length} 个（≤8 才有阶梯感）：${list.join('/')}`);
  for (let i = 1; i < list.length; i++) {
    const ratio = list[i] / list[i - 1];
    if (ratio < 1.1 && list[i] !== list[i - 1]) problems.push(`${list[i - 1]}→${list[i]} 比值 ${ratio.toFixed(2)}（相邻太近，分不出层级）`);
    if (ratio > 1.8) problems.push(`${list[i - 1]}→${list[i]} 比值 ${ratio.toFixed(2)}（跳跃过大）`);
  }
  const halfPixel = list.filter(x => !Number.isInteger(x));
  if (halfPixel.length) problems.push(`非整数 px：${halfPixel.join('/')}（渲染会取整，不如直接给整数）`);
  add('字阶成阶梯', problems.length === 0, problems.length ? problems.slice(0, 4).join('；') : `${list.length} 级：${list.join(' / ')}`, 'warn');
}

/* ---------- ③ 间距节奏 ---------- */
{
  const GRID = new Set([0, 2, 4, 8, 12, 16, 20, 24, 32, 40, 48, 56, 64, 80, 96, 120]);
  const off = new Map();
  const props = ['margin', 'margin-top', 'margin-right', 'margin-bottom', 'margin-left',
    'padding', 'padding-top', 'padding-right', 'padding-bottom', 'padding-left', 'gap', 'row-gap', 'column-gap'];
  for (const r of allRules) {
    for (const p of props) {
      const v = r.decl[p];
      if (!v) continue;
      for (const tok of String(v).split(/\s+/)) {
        const m = tok.match(/^(-?[\d.]+)px$/);
        if (!m) continue;
        const n = Math.abs(parseFloat(m[1]));
        if (n <= 1) continue;                       // 1px 描边不算节奏
        if (!GRID.has(n)) {
          const key = `${n}px`;
          if (!off.has(key)) off.set(key, `${path.basename(r.file)}:${r.line}`);
        }
      }
    }
  }
  add('间距在节奏上', off.size === 0,
    off.size ? `${off.size} 个值不在 4 的倍数 / 8-12-16-24-32 阶梯上：${[...off.keys()].join('/')}（首次出现在 ${[...off.values()][0]}）`
      : '所有 margin/padding/gap 都在 0/2/4/8/12/16/20/24/32… 上', 'warn');
}

/* ---------- ④ token 一致性 ---------- */
if (tokensArg) {
  let tokens = null;
  try { tokens = JSON.parse(fs.readFileSync(tokensArg, 'utf8')); } catch (e) {
    add('约束卡可读', false, `读不了 ${tokensArg}：${e.message}`);
  }
  if (tokens) {
    const norm = c => { const p = parseColor(c); return p ? `${p.r},${p.g},${p.b}` : null; };
    const flat = [];
    const dig = (v) => {
      if (typeof v === 'string') flat.push(v);
      else if (v && typeof v === 'object') for (const k of Object.keys(v)) dig(v[k]);
    };
    dig(tokens);
    const allowed = new Set(flat.map(norm).filter(Boolean));
    const stray = [];
    for (const r of allRules) {
      for (const p of ['color', 'background-color', 'border-color', 'fill', 'stroke']) {
        const c = parseColor(r.decl[p]);
        if (!c) continue;
        if (c.a === 0) continue;                     // 透明不算颜色
        if (!allowed.has(`${c.r},${c.g},${c.b}`)) stray.push(`${path.basename(r.file)}:${r.line} ${r.sel} ${p}: ${r.decl[p]}`);
      }
    }
    add('颜色只用约束卡的 token', stray.length === 0,
      stray.length ? `${stray.length} 处硬编码色：${stray.slice(0, 4).join('；')}${stray.length > 4 ? ' …' : ''}`
        : `约束卡 ${allowed.size} 个颜色，代码里没有额外色值`);
  }
} else {
  add('颜色只用约束卡的 token', true, '未给 --tokens，跳过（有约束卡时请给）', 'skip');
}

/* ---------- ⑤ 截图证据 ---------- */
if (shotsArg) {
  const need = ['desktop', 'tablet', 'mobile'];
  const missing = [], stale = [];
  for (const v of need) {
    const hit = fs.existsSync(shotsArg)
      ? fs.readdirSync(shotsArg).filter(f => new RegExp(`^${v}[._-]|${v}\\.(png|jpe?g|webp)$`, 'i').test(f))
      : [];
    if (!hit.length) { missing.push(v); continue; }
    const newest = Math.max(...hit.map(f => fs.statSync(path.join(shotsArg, f)).mtimeMs));
    const ageH = (Date.now() - newest) / 3600000;
    if (ageH > maxAgeH) stale.push(`${v}（${ageH.toFixed(0)} 小时前）`);
  }
  const bad = missing.length || stale.length;
  add('截图证据齐全', !bad,
    bad ? [missing.length ? `缺 ${missing.join('/')}` : '', stale.length ? `过期 ${stale.join('/')}` : ''].filter(Boolean).join('；') + `（目录 ${shotsArg}）`
      : `desktop / tablet / mobile 三张都在，且 ${maxAgeH} 小时内`);
} else {
  add('截图证据齐全', true, '未给 --shots，跳过（交付前必须补）', 'skip');
}

/* ---------- 输出 ---------- */
const fatal = results.filter(r => !r.ok && r.severity === 'fatal');
const warn = results.filter(r => !r.ok && r.severity === 'warn');

if (asJson) {
  console.log(JSON.stringify({ files: files.length, results, fatal: fatal.length, warn: warn.length }, null, 2));
} else {
  console.log(`UI 交付门禁 · 扫了 ${files.length} 个文件`);
  console.log('');
  for (const r of results) {
    const tag = r.severity === 'skip' ? 'SKIP' : (r.ok ? 'PASS' : (r.severity === 'fatal' ? 'FAIL' : 'WARN'));
    console.log(`  ${tag}  ${r.name} — ${r.detail}`);
  }
  console.log('');
  if (fatal.length) {
    console.log(`结论：不合格（${fatal.length} 项致命 / ${warn.length} 项提示）—— 不合格不许交付。`);
    console.log('修法：对比度调前景或底色；字阶收敛到不超过 8 级；间距归到 8/12/16/24/32；色值换回 token。');
  } else if (warn.length) {
    console.log(`结论：可交付（${warn.length} 项提示，不阻断）—— 提示项在交付说明里写清取舍。`);
  } else {
    console.log('结论：合格。');
  }
  console.log('注：静态检查只管下限。布局是否好看、层级是否清楚、动效手感，要看真实截图。');
}

process.exit(fatal.length ? 1 : 0);
