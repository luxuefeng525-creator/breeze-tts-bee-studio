#!/usr/bin/env node
// ai-workflow 路由 v2：一句话意图 -> Top-K 技能（零依赖、零 LLM、跨宿主一致）。
// 用法：node scripts/route.mjs "<一句话>" [--top 3] [--min 5] [--json] [--explain]
// 设计目标（按优先级）：① 任何模型读同一份确定性信号，命中率不依赖模型语感；② 输出极短省 token。
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const argv = process.argv.slice(2);
const flag = n => argv.includes(n);
function opt(n, d) { const i = argv.indexOf(n); return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : d; }
const asJson = flag('--json'), explain = flag('--explain');
const TOP = Number(opt('--top', '3')), MIN = Number(opt('--min', '5'));
const query = argv.filter((a, i) => !a.startsWith('--') && !(i > 0 && argv[i - 1].startsWith('--')))
  .join(' ').trim();

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PKG = path.join(HERE, '..');

// ---------- 归一化：全角->半角、去标点、小写；保留原串用于负向闸门 ----------
function norm(s) {
  return s.replace(/[\uFF01-\uFF5E]/g, c => String.fromCharCode(c.charCodeAt(0) - 0xFEE0))
    .replace(/\u3000/g, ' ').toLowerCase();
}
const Q = norm(query);

// ---------- 目录：catalog.json（构建产物）优先，缺失则现读 SKILL.md ----------
// 工程副本里没有 skills/ 也没有 catalog.json —— 靠 harness.config.json 的 skillSource 找回包根。
// 不回落的后果：脚本进了工程、词表却是旧的，路由在用户工程里静默退化（新技能与新构式全不生效），
// 而自检只查「命令在不在」，看不出它判不准。
function pkgRoots() {
  const roots = [PKG];
  for (const cfg of [path.join(PKG, 'harness.config.json'), path.join(PKG, 'harness', 'harness.config.json')]) {
    if (!fs.existsSync(cfg)) continue;
    try {
      const c = JSON.parse(fs.readFileSync(cfg, 'utf8'));
      if (c.skillSource) {
        const r = path.dirname(c.skillSource);   // <包>/skills → <包>
        if (r && !roots.includes(r)) roots.push(r);
      }
    } catch {}
  }
  return roots;
}

function loadCatalog() {
  const roots = pkgRoots();
  for (const root of roots) {
    const f = path.join(root, 'catalog.json');
    if (fs.existsSync(f)) {
      try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch {}
    }
  }
  const out = { skills: [], syncs: {}, negatives: null };
  for (const root of roots) {
    const ps = path.join(root, 'skills');
    if (!fs.existsSync(ps)) continue;
    for (const name of fs.readdirSync(ps)) {
      const sk = path.join(ps, name, 'SKILL.md');
      if (!fs.existsSync(sk)) continue;
      const txt = fs.readFileSync(sk, 'utf8');
      const fm = txt.match(/^---\r?\n([\s\S]*?)\r?\n---/);
      const dm = fm && fm[1].match(/description:\s*([\s\S]*?)(?:\n[A-Za-z][\w-]*:|$)/);
      const tl = txt.match(/^触发词[:：]\s*(.+)$/m);
      out.skills.push({
        name,
        desc: dm ? dm[1].replace(/\s+/g, ' ').trim() : '',
        keywords: tl ? tl[1].split(/[\/|,;、，；]/).map(s => s.trim()).filter(Boolean) : [],
      });
    }
    if (out.skills.length) return out;
  }
  return out;
}
const cat = loadCatalog();

// ---------- 负向闸门：结构化，不再靠单个词 ----------
// 命中任一 negative 规则且不含 build 动词 -> ROUTE_NONE（省 token）。用户点名技能时由宿主覆盖。
const NEG = [
  { id: 'text-replace', re: /(改成|改为|替换成|替换为|换成|改成「|改成『|改成"|替换)[^。]{0,20}?(「|『|"|`|'|》|《)/ },
  { id: 'text-replace-quoted', re: /(「|『|"|`)[^」』"`]{1,20}(」|』|"|`)[^。]{0,12}(改成|改为|替换成|替换为|换成)/ },
  { id: 'text-replace2', re: /(把|将)[^。]{0,24}(改成|改为|替换成|替换为)[^。]{0,24}(两个字|几个字|文字|文案|标题)/ },
  { id: 'text-replace3', re: /(两个字|几个字|这几个字|这个词|这行字|这句)[^。]{0,12}(改成|改为|替换成|替换为|换成)/ },
  { id: 'chit-chat', re: /^(为什么(?!不|会报|会崩)|怎么会|哪一年|哪天|天气|今天星期|是谁|历史|翻译|天气怎么样)/ },
  // 参数微调（把超时从 30 调到 60、把限流从 100 改成 200）不是排查也不是设计
  { id: 'param-tweak', re: /(从|由)\s*\d+\s*(调到|改成|改为|调整到|改成)\s*\d+/ },
  { id: 'pure-qa', re: /^(解释|说明|介绍|讲讲|说说|科普|分别是|怎么理解|什么是|什么叫|是什么|区别|为什么是)/ },
  { id: 'define', re: /(的区别|是什么意思|是什么关系|原理是|概念)/ },
  { id: 'handoff', re: /(提交给|发给|转给|发送给|发给老板|邮件给)/ },
  { id: 'paste', re: /(贴给我|原文贴|日志原文|把报错贴|复制给我看看)/ },
  { id: 'report', re: /(写周报|写日报|周报模板|汇报给老板)/ },
  { id: 'naming', re: /(起个名|起名|取个名|命名|叫什么名|怎么称呼|叫什么名字|起.{0,8}(名字|名称|名儿))/ },
];
const BUILD = /(做|建|开发|实现|重构|修复|生成|设计|搭建|迁移|部署|上线|排查|定位|诊断|验收|拆分|拆成|复现)/;
// 结构性"字面替换"永不属于技能活：不论有没有 build 动词都拦。
const HARD_NEG = new Set(['text-replace', 'text-replace2', 'text-replace-quoted', 'text-replace3', 'param-tweak']);
const negHit = query ? NEG.find(r => r.re.test(Q) && (HARD_NEG.has(r.id) || !BUILD.test(Q))) : null;
if (!query) { console.log('用法：route.mjs "<一句话>" [--top 3] [--min 5] [--json]'); process.exit(1); }
if (negHit) {
  if (asJson) console.log(JSON.stringify({ route: [], reason: 'negative:' + negHit.id, query }));
  else console.log(`ROUTE_NONE（非开发意图: ${negHit.id}，走通用纪律省 token）`);
  process.exit(0);
}

// ---------- 入口触发：ai-workflow 本体不在技能目录中 ----------
// 入口技能是包根 SKILL.md，不在 catalog 的 41 个执行技能里；这里显式兜住，避免 ROUTE_NONE。
const ENTRY_TRIGGERS = ['ai-workflow', '进入工作流', '开工流程', '按工作流走', '走流程'];
if (ENTRY_TRIGGERS.some(t => Q.includes(t))) {
  if (asJson) console.log(JSON.stringify({ query, route: [{ name: 'ai-workflow', score: 99, load: 'SKILL.md' }] }));
  else console.log('1. ai-workflow (score=99)');
  process.exit(0);
}

// ---------- 同义词扩展：口语 -> 技能词（提高 LLM 之外的确定性召回） ----------
const SYN = cat.syncs || {};
function expansions(q) {
  const out = new Set([q]);
  for (const [std, alts] of Object.entries(SYN)) {
    if (std.startsWith('__')) continue;
    // 需求包是独立复合词，不展开成 PRD/需求文档，否则 product-manager 会抢走 harness 的路由
    if (std === '需求' && q.includes('需求包')) continue;
    const list = Array.isArray(alts) ? alts : [alts];
    if (q.includes(std) || list.some(a => a && q.includes(String(a).toLowerCase()))) {
      out.add(std); for (const a of list) if (a) out.add(String(a).toLowerCase());
    }
  }
  return [...out];
}
const Q_VARIANTS = expansions(Q);
const Q_SPACED = [Q, ...Q_VARIANTS].map(v => v.replace(/\s+/g, ''));
const hasKw = kk => Q_VARIANTS.some(v => v.includes(kk)) || Q_SPACED.some(v => v.includes(kk));

// 结构加成：关键词表覆盖不到的口语构式（对任何模型都成立，不靠语感）。
const BOOST = [
  { re: /(加|添加|新增|去掉|删掉)[^。]{0,12}(按钮|开关|字段|列|导出|导入|图标|提示|入口|tab)/, w: 6, tag: '构式:小改一处' },
  { re: /(位置|样式|颜色|间距|对齐|字号)[^。]{0,6}(偏|不对|错|乱|难看|改)/, w: 6, tag: '构式:样式问题' },
  { re: /(报错|崩溃|白屏|卡顿|变慢|挂了|跑不起来)[^。]{0,10}(帮我|怎么|为什么|查|看)/, w: 6, tag: '构式:故障' },
  { re: /(大屏|看板|驾驶舱|数字孪生)/, w: 8, tag: '构式:大屏' },
  { re: /(落地页|官网|品牌站|宣传页)/, w: 8, tag: '构式:落地页' },
  { re: /(工业|scada|hmi|上位机|设备|水泵|阀门|启停)/, w: 6, tag: '构式:工业' },
  { re: /(验收|证据|真的完成|自检)/, w: 7, tag: '构式:验收' },
  { re: /(需求包|阶段门禁|四道门禁|三角色|角色隔离|证据链|换人续接)/, w: 8, tag: '构式:交付门禁' },
  { re: /(子代理|并行|分工|多代理)/, w: 7, tag: '构式:并行' },
  { re: /(先定位|先查清|先查原因|看不懂|原因说不清|不确定为什么|偶发|间歇)/, w: 8, tag: '构式:先定位' },
  { re: /(规格|需求澄清|写清楚需求|需求写不清|一句话变)/, w: 8, tag: '构式:规格' },
  { re: /(追问|拷问|挑战方案|质疑|反问我)/, w: 7, tag: '构式:追问' },
  { re: /(零散想法|想法整理|整理成[^。]{0,8}清单|能开工|开得动工|理清需求)/, w: 8, tag: '构式:理需求' },
  { re: /(要不要|该不该|值不值得|拿主意|选哪(个|种)|怎么选)/, w: 8, tag: '构式:拿主意' },
  { re: /(卡住|滑不动|点不动|没反应|不响应|白屏|闪退)/, w: 7, tag: '构式:卡死' },
  // —— 任务语义式：用户不会说技能名，只会说问题 ——
  // 边界纪律：品牌词与泛化角色词必须与「对标/设计意图」或「功能类名词」同句共现，
  // 否则「接入微信支付」「写个客服回复话术」「查一下收货人电话」这类日常句子会被误伤。
  { re: /(放哪|放在哪|摆哪|放左边|放右边|左边还是右边|上面还是下面|入口放|位置放|摆在哪)/, w: 7, tag: '构式:放哪' },
  // 「怎么做」必须落在功能类名词上才归设计；纯技术问法（怎么实现/怎么设计存储）不该被抢
  { re: /(功能|页面|流程|界面|入口|体验|产品|交互)[^。]{0,6}(怎么做|怎么设计|如何设计|怎么落地)/, w: 7, tag: '构式:怎么做' },
  { re: /(应该怎么做|该怎么做|怎么做这个|有没有更好的做法|推荐哪种|方案对比|流程是什么|是什么流程)/, w: 7, tag: '构式:怎么做' },
  { re: /(这个|该|功能|页面|流程|界面)[^。]{0,4}(怎么实现|如何实现)/, w: 7, tag: '构式:怎么做' },
  { re: /(对标|同行|别人怎么做的|行业都|大家都|别人有|竞品怎么做|参考谁)/, w: 8, tag: '构式:对标同行' },
  { re: /(美团|饿了么|滴滴|高德|微信支付|支付宝|百度地图)[^。]{0,10}(怎么做的|怎么设计|对标|同行|怎么实现)/, w: 8, tag: '构式:对标同行' },
  { re: /(会有什么后果|有什么影响|副作用|风险是|值不值|值得吗|取舍|不做的后果|代价是)/, w: 7, tag: '构式:后果取舍' },
  { re: /(骑手|司机|配送员|外卖|快递|客服|商家|患者|学生|老师|销售员|代驾|乘客|收货人|调度员)[^。]{0,10}(功能|流程|页面|界面|入口|按钮|表单|弹窗|布局|交互|工作台|端|模块)/, w: 6, tag: '构式:角色域' },
  { re: /(开发|做|写|加|建|来|搞|设计)(一个|个)?[^。]{0,10}(功能|流程|页面|界面|模块|入口|体验|步骤)/, w: 7, tag: '构式:新功能' },
  // 强 UI 名词才单独构成意图；属性词（配色/排版/字体…）必须与 UI 名词同句共现，
  // 否则「代码排版用什么 formatter」「视觉回归测试怎么跑」这类会被误伤
  { re: /(页面|界面|后台|工作台|前台|列表页|详情页|表单页|仪表盘|侧边栏|空态|加载态|错误态)/, w: 7, tag: '构式:界面' },
  { re: /(页面|界面|后台|工作台|前台|列表页|详情页|表单页|仪表盘|侧边栏|导航栏)[^。]{0,10}(配色|排版|间距|圆角|阴影|字体|视觉|控件|布局|样式)/, w: 7, tag: '构式:界面属性' },
  // 界面类故障症状 → 走排查而不是设计（「页面错位」是 bug，不是设计问题）
  { re: /(错位|错乱|重叠|错开|显示异常|不显示|没显示|排版乱|溢出|挡住|遮住|被裁|被盖|撑破|串行)/, w: 9, tag: '构式:界面故障' },
  // 界面动效：用户说的是具体效果名（聚光/液态玻璃/骨架屏），不会说「我要做动效设计」。
  // 与 frame-smith 分工：这里出的是界面里的实时交互，frame-smith 出的是视频成片。
  { re: /(聚光|光斑|液态玻璃|玻璃拟态|玻璃胶囊|边缘流光|流光|停靠栏|骨架屏|悬停[^。]{0,10}(放大|提亮|倾斜|光|效果)|指示器[^。]{0,8}(切换|滑)|选中态|滚动动画|过渡动画|微交互)/, w: 10, tag: '构式:界面动效' },
  { re: /(加|添|做|要)[^。]{0,8}(个|一个)?[^。]{0,10}(动效|动画效果|效果)$|(动效|动画)[^。]{0,6}(怎么|如何)(做|实现)/, w: 5, tag: '构式:要动效' },
  // 数据建模：用户在说「表怎么定 / 数据存哪」，不会说「我要做数据建模」。与 migration 分工：
  // 这类句子问的是「该怎么设计」，实施类（跑迁移、回滚）仍归 migration
  { re: /(表[^。]{0,4}结构|数据模型|建表|主外键|范式|多对多|一对多|schema)/, w: 8, tag: '构式:数据建模' },
  { re: /(表|库|数据库)[^。]{0,8}(加|改|删|新增)[^。]{0,6}(字段|列|索引)/, w: 8, tag: '构式:数据建模' },
  // 「存储/存哪」是明确的数据意图，权重压过泛化的「怎么做」
  { re: /(数据|字段|信息|状态|记录)[^。]{0,10}(存储|怎么存|存哪|存在哪)/, w: 12, tag: '构式:数据建模' },
  { re: /(数据|字段|信息)[^。]{0,8}(怎么设计|如何设计|该怎么放)/, w: 7, tag: '构式:数据建模' },
  // 项目立项：用户说「我要做项目/搞个项目/项目立项」，指的是还没有方案、要把想法澄清成框架。
  // 与 grilling（压已有方案）、finesse-brief（工作台与网页规格）分工。
  //
  // 排除词表（两条正则共用）：句中若已指向具体产物，那是已有方案或界面域，不是立项。
  // 权重 10：只需压过同长度的泛词命中，不与「需求」类 BOOST（18）争。
  { re: /^(?!.*(?:网站|页面|界面|系统|后台|平台|会员体系|功能))[^\n]{0,16}?(?:我要做|想做|搞个|开个)[^。]{0,6}项目/, w: 10, tag: '构式:立项澄清' },
  // 「我要做个公众号/短剧/小游戏/社区」——泛化形态：动词 + 个 + 具体事物。
  { re: /^(?!.*(?:网站|页面|界面|系统|后台|平台))([^\n]{0,16}?)(?:我要做|想做|搞个|开个)[^。]{0,3}个[^。]{0,8}(?:号|剧|戏|站|课|店|群|产品|工具|应用|小程序|内容|社群|活动)/, w: 10, tag: '构式:立项澄清' },
  { re: /(开工前|开始做之前|动手前)[^。]{0,8}(?:问清楚|确认|澄清)/, w: 10, tag: '构式:立项澄清' },
  // 「先把需求问清楚」「先问清楚需求」是立项信号。BOOST 只加给 task-clarification，
  // 权重必须压过 product-manager 的泛词「需求」（+7）与 desc 重叠（约 +2，合计 17）。
  // 排除已有方案指向：句中若出现具体产物名词（体系/系统/功能/模块/接口），那是已有方案 → grilling。
  // 不锚定句首：用户常带寒暄（「嗯，我想先问清楚需求」），锚定会失配。
  // 三条合并成一条，避免同一句被重复计分（曾出现 18+18=36 的虚高）。
  // 整条从句首匹配（^ 锚定 + 允许任意前缀），排除词才能在「全文」层面判定。
  // 之前排除断言放在尾部，只检查了「匹配点之后」的文本 —— 「设计一套会员体系先把需求拷问清楚」
  // 的排除词在匹配点之前，断言失效，导致它被抢走。
  // 分支收紧：「先X需求问清楚」与「X需求问清楚」才属立项；单说「把想法整理成清单」是 finesse-brief。
  { re: /^(?!.*(?:体系|功能|系统|模块|接口|后台|需求包|清单))[^\n]{0,24}?(?:(?:先)[^。]{0,4}(?:需求|想法)[^。]{0,4}(?:问清楚|理清楚|搞清楚|捋清楚)|(?:先问清楚|问清楚)[^。]{0,4}(?:需求|方向|想法)|(?:需求|想法)[^。]{0,4}(?:问清楚|理清楚|搞清楚|捋清楚)[^。]{0,6}(?:再|才)?(?:开工|动手|做)?)/, w: 18, tag: '构式:立项澄清' },
  // 技能工厂：白名单式匹配「技能」被当作管理对象，而不是句子任意位置出现「技能」。
  // 分支：①创建类动词 + 技能（且技能后即句尾/标点，避免「技能培训计划」）②管理类动词 + 技能 ③技能 + 管理动作 ④技能 + 可管理属性。
  // 排除词仍保留作兜底，但主要靠结构约束（黑名单必漏，白名单才稳）。
  { re: /^(?!.*(?:页面|后台|详情页|文案|培训|课程|考试|市场|商城|系统|功能|算法|游戏|员工|规则|方案|计划|报告|选手|人才|特效|技能栏|技能树|技能点))[^。]*?(?:(?:创建|新建|新增|添加|开发|加|写|做|造)(?:一个|个)?技能(?:$|[，。！？,.\s]|吧|呢|的)|(?:修改|改一下|改下|调整|优化|评测|评估|测一下|废弃|下线|删除|移除|淘汰|退役)(?:一下|下)?(?:这个|该|现有|新)?技能(?:$|[，。！？,.\s]|吧|呢)?|(?:这个|该|现有)?技能(?:名称)?(?:改一下|改下|需要调整|不要了|要改)|技能[^。]{0,4}(?:描述|触发词|质量|边界|SKILL\.md|跑[^。]{0,2}分|评分|打分))/i, w: 12, tag: '构式:技能工厂' },
];
const BOOST_TARGET = {
  '构式:小改一处': ['surgical-patch'], '构式:样式问题': ['surgical-patch', 'diagnose'],
  '构式:故障': ['diagnose', 'investigate-first'], '构式:大屏': ['bigscreen-studio', 'industrial-screen'],
  '构式:落地页': ['finesse-ui'], '构式:工业': ['industrial-screen'], '构式:验收': ['verify-and-stop'],
  '构式:并行': ['cavecrew'], '构式:先定位': ['investigate-first'], '构式:规格': ['finesse-brief'], '构式:追问': ['grilling'], '构式:理需求': ['finesse-brief'], '构式:拿主意': ['product-strategist'], '构式:卡死': ['diagnose'], '构式:交付门禁': ['harness'],
  // 任务语义式四类意图
  '构式:放哪': ['product-designer', 'feature-design'],
  '构式:怎么做': ['feature-design', 'product-designer'],
  '构式:对标同行': ['research', 'ux-researcher', 'feature-design'],
  '构式:后果取舍': ['product-strategist', 'grilling'],
  '构式:角色域': ['feature-design', 'domain-modeling'], '构式:新功能': ['feature-design', 'product-designer'], '构式:界面': ['ui-design'], '构式:界面属性': ['ui-design'], '构式:界面故障': ['diagnose', 'investigate-first'], '构式:数据建模': ['data-modeling'], '构式:界面动效': ['ui-design'], '构式:要动效': ['ui-design'],
  '构式:立项澄清': ['task-clarification'], '构式:技能工厂': ['skill-factory'],
};

function score(s) {
  let sc = 0; const ev = [];
  for (const k of s.keywords || []) {
    const kk = norm(k);
    if (kk.length < 2) continue;
    if (hasKw(kk)) {
      const w = kk.length >= 6 ? 9 : kk.length >= 4 ? 7 : 5;
      sc += w; ev.push(`kw:${k}+${w}`); if (ev.length > 6) break;
    }
  }
  const nm = norm(s.name);
  if (Q.includes(nm)) { sc += 9; ev.push(`name+9`); }
  else for (const part of nm.split('-')) if (part.length >= 4 && Q.includes(part)) { sc += 3; ev.push(`name:${part}+3`); break; }
  for (const b of BOOST) {
    if (b.re.test(Q) && (BOOST_TARGET[b.tag] || []).includes(s.name)) { sc += b.w; ev.push(`${b.tag}+${b.w}`); }
  }
  const desc = norm(s.desc || '');
  let ov = 0;
  for (let i = 0; i < Q.length - 1; i++) { const g = Q.slice(i, i + 2); if (g.trim() && desc.includes(g)) ov++; }
  // desc 双字重叠在短句上噪声极大：短句要求更高阈值，且最多 +2
  const minOv = Q.length >= 12 ? 4 : 6;
  const bonus = ov >= minOv ? Math.min(ov - minOv + 1, 2) : 0;
  sc += bonus; if (bonus) ev.push(`desc+${bonus}`);
  return { sc, ev };
}

// 变体技能（别名）默认折叠到主技能，只有用户点名时才单独出现，避免同类技能互抢。
const REDIRECT = { 'grill-me': 'grilling' };
const all = cat.skills.map(s => ({ ...s, ...score(s) }));
const byName = new Map(all.map(x => [x.name, x]));
const isAlias = x => REDIRECT[x.name] && !Q.includes(norm(x.name));
for (const x of all) {
  const tgt = isAlias(x) ? REDIRECT[x.name] : null;
  if (tgt && byName.has(tgt)) { const t = byName.get(tgt); t.sc += x.sc; t.ev.push(`alias:${x.name}+${x.sc}`); }
}
const ranked = all.filter(x => !isAlias(x) && x.sc >= MIN).sort((a, b) => b.sc - a.sc).slice(0, TOP);

// ---------- 领域兜底：精确词表命不中时，退到大类，而不是退回空白 ----------
// 词表必然漏 —— 新手不会说「液态玻璃」「骨架屏」「粘性融合」，只会说「界面」「好看点」「有点生硬」。
// 这一层不是再加精确词，是让「没命中」也有下一步：先判领域，再交给主 Agent 复核。
// 边界：想的是「宁可粗一层，也别让人自己找技能」——给错大类远好过给空白。
const DOMAIN = [
  { skill: 'ui-design', why: '界面 / 设计',
    re: /(\bui\b|\bux\b|界面|页面|前端|设计|视觉|样式|交互|布局|配色|排版|字体|图标|按钮|菜单|导航|弹窗|表单|卡片|首页|登录页|注册页|详情页|列表页|官网|网站|站点|落地页|后台|工作台|仪表盘|大屏|看板|移动端|桌面端|\bh5\b|响应式|美化|润色|好看|难看|丑|不高级|廉价|土|生硬|突兀|别扭|怪|不舒服|挤|空荡|精致|粗糙)/ },
  { skill: 'data-modeling', why: '数据 / 表结构', re: /(数据库|表结构|建表|主键|外键|多对多|一对多|数据怎么存|存哪|字段怎么定)/ },
  { skill: 'migration', why: '迁移', re: /(迁移|数据修复|灰度|回滚方案)/ },
  { skill: 'diagnose', why: '故障', re: /(报错|崩溃|白屏|卡死|卡住|闪退|没反应|不响应|不能用|跑不起来)/ },
  { skill: 'feature-design', why: '功能 / 需求', re: /(功能|需求|流程|入口)/ },
];
// 技术语境否决：讲代码本身时，「页面/设计/字体」这些词不是在说界面设计。
// 只否决 ui-design，不否决其它领域（否则「接口报错」也会被一起吃）
const UI_VETO = /(代码|函数|方法名|变量|变量名|命名|类名|属性名|字段名|这个类|类设计|文件名|哪个文件|定义在|formatter|格式化|lint|编译|构建|打包|单元测试|测试用例|依赖|包名|\bnpm\b|\bgit\b|分支|commit|接口|\bapi\b|数据库|表结构|字段类型|索引|算法|协议|部署|服务器|日志|命令行|正则|源码|\bsql\b|查询语句|语句|表达式|类型定义|返回值|堆栈|报错信息)/;
function domainFallback(q) {
  for (const d of DOMAIN) {
    if (!d.re.test(q)) continue;
    if (d.skill === 'ui-design' && UI_VETO.test(q)) continue;
    return d;
  }
  return null;
}

if (!ranked.length) {
  const d = domainFallback(Q);
  if (d) {
    if (asJson) console.log(JSON.stringify({
      query, reason: 'domain-fallback',
      route: [{ name: d.skill, score: 'domain', load: `skills/${d.skill}/SKILL.md`, evidence: ['domain:' + d.why] }]
    }));
    else {
      console.log(`1. ${d.skill} (领域兜底)`);
      console.log(`   ↑ 没命中精确词，按「${d.why}」粗分 —— 读一遍需求，不对就换，别当定论。`);
    }
    process.exit(0);
  }
  if (asJson) console.log(JSON.stringify({ route: [], reason: 'no-match', query }));
  else {
    console.log('ROUTE_NONE（没把握，不硬猜）');
    console.log('   ↑ 不是「没有技能可用」：读用户这句话自己判该加载哪个技能，别跳过判断。');
  }
  process.exit(0);
}
if (asJson) {
  console.log(JSON.stringify({ query, route: ranked.map(r => ({ name: r.name, score: r.sc, load: `skills/${r.name}/SKILL.md`, evidence: explain ? r.ev : undefined })) }));
} else {
  ranked.forEach((x, i) => console.log(`${i + 1}. ${x.name} (score=${x.sc})${explain ? ' ' + x.ev.join(',') : ''}`));
}
