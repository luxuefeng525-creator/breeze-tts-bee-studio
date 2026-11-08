/* 前端布局 / 可访问性审计：在页面上下文里跑一次，返回 JSON。
 *
 * 为什么量而不是看：这个仓库出过的 UI 事故几乎都不是「不好看」，而是
 *   - grid 少一列 → 删除按钮折到第二行被下一条盖住（elementFromPoint 命中别人）
 *   - inline span 上写 text-overflow:ellipsis → 完全不生效，文字压住右边控件
 *   - 容器 flex:0 0 32px 却允许换行 → 第二行没有高度，溢出压住下面的标题
 * 这些肉眼要放大才看得出，量一次就现形。
 *
 * 用法见 tools/ui_audit.sh；单独调试：agent-browser eval --stdin < tools/ui_audit.js
 */
(() => {
  const issues = [];
  const W = (el) => el.getBoundingClientRect();
  const label = (el) => {
    let cls = el.className;
    // SVG 的 className 是 SVGAnimatedString，直接 String() 会得到 [object ...]
    if (cls && typeof cls === "object" && "baseVal" in cls) cls = cls.baseVal;
    return el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") + (cls ? "." + String(cls).trim().split(/\s+/).join(".") : "");
  };

  const all = [...document.querySelectorAll("body *")].filter((el) => {
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });

  // A. 子元素溢出父容器、父容器又不可滚（会被裁掉或压住邻居）
  all.forEach((el) => {
    const cs = getComputedStyle(el);
    const r = W(el);
    if (el.ownerSVGElement || el.tagName.toLowerCase() === "svg") return;   // SVG 内部不按盒模型算
    const kids = [...el.children].filter((c) => getComputedStyle(c).display !== "none");
    if (!kids.length) return;
    const lowest = kids.reduce((a, b) => (W(a).bottom >= W(b).bottom ? a : b));
    const rightmost = kids.reduce((a, b) => (W(a).right >= W(b).right ? a : b));
    if (W(lowest).bottom > r.bottom + 1.5 && cs.overflowY !== "auto" && cs.overflowY !== "scroll") {
      issues.push({ type: "spill-y", el: label(el), kid: label(lowest), over: Math.round(W(lowest).bottom - r.bottom), h: Math.round(r.height) });
    }
    if (W(rightmost).right > r.right + 1.5 && cs.overflowX !== "auto" && cs.overflowX !== "scroll") {
      issues.push({ type: "spill-x", el: label(el), kid: label(rightmost), over: Math.round(W(rightmost).right - r.right), w: Math.round(r.width) });
    }
  });

  // B. grid 折行：按纵向是否重叠聚成「视觉行」再比对列数。
  //    同一行元素高度不同（垂直居中）时 top 天然不同，不能拿 top 去重。
  all.forEach((el) => {
    const cs = getComputedStyle(el);
    if (!cs.display.includes("grid")) return;
    const kids = [...el.children].filter((c) => {
      const k = getComputedStyle(c);
      if (k.display === "none") return false;
      // 分页条这类「1 / -1」跨整行的元素本来就该另起一行
      return !/^1\s*\/\s*-1$/.test((k.gridColumnStart || "") + " / " + (k.gridColumnEnd || ""));
    });
    if (kids.length < 2) return;
    const ncols = (cs.gridTemplateColumns || "").trim().split(/\s+/).filter(Boolean).length || 1;
    const boxes = kids.map((c) => W(c)).sort((a, b) => a.top - b.top);
    let rows = 0;
    let rowBottom = -1e9;
    boxes.forEach((b) => {
      if (b.top >= rowBottom - 0.5) { rows++; rowBottom = b.bottom; }
      else rowBottom = Math.max(rowBottom, b.bottom);
    });
    if (rows > Math.max(1, Math.ceil(kids.length / ncols))) {
      issues.push({ type: "grid-wrap", el: label(el), cols: cs.gridTemplateColumns, kids: kids.length, rows });
    }
  });

  // C. 交互热区太小
  all.forEach((el) => {
    const tag = el.tagName.toLowerCase();
    if (!(tag === "button" || tag === "a" || tag === "select" || el.getAttribute("role") === "button")) return;
    const r = W(el);
    if (r.width < 20 || r.height < 20) {
      issues.push({ type: "tiny-target", el: label(el), w: Math.round(r.width), h: Math.round(r.height) });
    }
  });

  // D. 文字被裁却没有省略号提示
  all.forEach((el) => {
    const cs = getComputedStyle(el);
    if (el.classList.contains("sr-only")) return;
    // inline 元素没有自己的盒子（clientWidth 恒为 0），也就不会裁字；
    // 只有 block / inline-block / flex 这些才谈得上 text-overflow
    if (cs.display === "inline" || el.clientWidth <= 0) return;
    if (!el.children.length && el.textContent.trim() && el.scrollWidth > el.clientWidth + 1) {
      if (cs.textOverflow !== "ellipsis") {
        issues.push({ type: "clip-text", el: label(el), text: el.textContent.trim().slice(0, 18),
                      sw: el.scrollWidth, cw: el.clientWidth });
      }
    }
  });

  // E. 交互元素互相压住：用「实际可见矩形」（与所有 overflow 祖先求交），
  //    滚动容器里露半截的行几何上压到页脚，视觉上并不重叠
  const visibleRect = (el) => {
    const r = W(el);
    const box = { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
    for (let p = el.parentElement; p; p = p.parentElement) {
      const cs = getComputedStyle(p);
      if (cs.overflowX === "visible" && cs.overflowY === "visible") continue;
      const pr = W(p);
      box.left = Math.max(box.left, pr.left);
      box.top = Math.max(box.top, pr.top);
      box.right = Math.min(box.right, pr.right);
      box.bottom = Math.min(box.bottom, pr.bottom);
    }
    return box;
  };
  const its = all.filter((el) => ["BUTTON", "A", "SELECT", "INPUT"].includes(el.tagName) || el.getAttribute("role") === "button");
  for (let i = 0; i < its.length; i++) {
    for (let j = i + 1; j < its.length; j++) {
      if (its[i].contains(its[j]) || its[j].contains(its[i])) continue;
      const va = visibleRect(its[i]);
      const vb = visibleRect(its[j]);
      const ha = va.right - va.left;
      const hb = vb.right - vb.left;
      if (ha < 4 || va.bottom - va.top < 4 || hb < 4 || vb.bottom - vb.top < 4) continue;
      const ox = Math.min(va.right, vb.right) - Math.max(va.left, vb.left);
      const oy = Math.min(va.bottom, vb.bottom) - Math.max(va.top, vb.top);
      const need = 0.25 * Math.min(ha * (va.bottom - va.top), hb * (vb.bottom - vb.top));
      if (ox > 2 && oy > 2 && ox * oy > need) {
        issues.push({ type: "overlap", a: label(its[i]), b: label(its[j]) });
      }
    }
  }

  // F. 可访问性：交互元素没有可读名称 / 焦点不可见
  const hasName = (el) => {
    if (el.getAttribute("aria-label") || el.getAttribute("title")) return true;
    const by = el.getAttribute("aria-labelledby");
    if (by && document.getElementById(by)) return true;
    if ((el.textContent || "").trim()) return true;
    if (el.id && document.querySelector('label[for="' + el.id + '"]')) return true;
    return !!el.closest("label");
  };
  const a11y = [];
  document.querySelectorAll("button, a[href], input:not([type=hidden]), select, textarea, [role=button], [tabindex='0']").forEach((el) => {
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden" || !el.getClientRects().length) return;
    if (!hasName(el)) a11y.push({ type: "no-name", el: label(el) });
  });
  document.querySelectorAll("button, a[href], input, select, textarea").forEach((el) => {
    const cs = getComputedStyle(el);
    if (cs.display === "none" || !el.getClientRects().length) return;
    if (cs.outlineStyle === "none" && cs.outlineWidth === "0px" && cs.boxShadow === "none") {
      a11y.push({ type: "no-focus", el: label(el) });
    }
  });

  return JSON.stringify({
    screen: document.body.dataset.screen,
    state: document.body.dataset.state,
    vw: innerWidth,
    vh: innerHeight,
    pageOverflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    h1: [...document.querySelectorAll("h1")].map((h) => h.textContent.trim()),
    layout: issues,
    a11y,
  });
})()
