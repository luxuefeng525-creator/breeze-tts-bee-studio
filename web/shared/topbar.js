/* ============================================================
   小蜜蜂 · 共享顶栏组件 <bee-topbar>
   用法：
     <bee-topbar active="video"></bee-topbar>
     <bee-topbar active="image" theme="off">
       <span slot="right" class="topbar-status">…</span>   （自定义右侧内容会原样保留）
     </bee-topbar>
   active 取值：video | image | workflow | enhance | home
   ============================================================ */
(function () {
  "use strict";

  const TABS = [
    { id: "video", href: "/bee-studio.html", icon: "🎬", label: "视频工作台" },
    { id: "image", href: "/image-studio.html", icon: "🖼️", label: "图片工坊" },
    { id: "workflow", href: "/image-studio.html#templates", icon: "🧩", label: "工作流" },
    { id: "enhance", href: "/video-enhance.html", icon: "✨", label: "视频超分" },
    { id: "home", href: "/", icon: "🏠", label: "首页" },
  ];
  const TOOLS = [
    { href: "/?screen=workbench", label: "合成台" },
    { href: "/?screen=clone", label: "克隆" },
    { href: "/?screen=script", label: "剧本台" },
    { href: "/?screen=library", label: "音色库" },
  ];
  // 制声台（index.html）的页内导航：切换 body[data-screen]，不是跳页
  const SCREENS = [
    { id: "workbench", icon: "i-wave", label: "合成台" },
    { id: "clone", icon: "i-mic", label: "克隆" },
    { id: "script", icon: "i-book", label: "剧本" },
    { id: "library", icon: "i-grid", label: "音色库" },
  ];
  const SITE_LINKS = [
    { href: "/bee-studio.html", label: "视频工作台" },
    { href: "/image-studio.html", label: "图片工坊" },
    { href: "/image-studio.html#templates", label: "工作流" },
    { href: "/video-enhance.html", label: "视频超分" },
  ];

  class BeeTopbar extends HTMLElement {
    connectedCallback() {
      if (this._done) return;
      this._done = true;
      // 自定义元素默认 inline，会让 64px 的 header 掉出文档流
      this.style.display = "block";

      const active = this.getAttribute("active") || "";
      const showTheme = this.getAttribute("theme") !== "off";
      const keep = Array.from(this.children); // 先保住调用方塞进来的内容

      if (this.getAttribute("mode") === "app") {
        this._renderApp(keep);
        return;
      }
      this._renderSite(active, showTheme, keep);
      this._bindGlider();
      // 图片工坊里打开「工作流」视图时，顶栏高亮应跟着走，否则用户以为点错了
      this._syncActive();
      window.addEventListener("hashchange", () => this._syncActive(), { passive: true });
    }

    /** 制声台顶栏：深色皮肤由 index 自己的 app.css 提供，组件只负责结构。 */
    _renderApp(keep) {
      this.innerHTML = `
        <header class="topbar">
          <div class="brand-slot"></div>
          <nav class="nav" aria-label="主导航">
            ${SCREENS.map(s => `
              <button type="button" data-screen="${s.id}">
                <svg class="icon" aria-hidden="true"><use href="#${s.icon}"/></svg>${s.label}
              </button>`).join("")}
            ${SITE_LINKS.map(l => `<a class="nav-link" href="${l.href}">${l.label}</a>`).join("")}
          </nav>
          <div class="top-actions"></div>
        </header>`;

      const brandChild = keep.find(n => n.getAttribute && n.getAttribute("slot") === "brand");
      const slot = this.querySelector(".brand-slot");
      if (brandChild) {
        brandChild.removeAttribute("slot");
        slot.replaceWith(brandChild);
      } else {
        slot.className = "brand";
        slot.innerHTML = '<span class="brand-name">BreezeTTS</span>';
      }

      const actions = this.querySelector(".top-actions");
      keep.filter(n => n !== brandChild).forEach(n => actions.appendChild(n));

      this._syncScreen();
      // app.js 用 body[data-screen] 驱动页面切换；顶栏只跟状态，不改状态
      new MutationObserver(() => this._syncScreen()).observe(document.body, {
        attributes: true, attributeFilter: ["data-screen"],
      });
    }

    _syncScreen() {
      const screen = document.body.dataset.screen || "workbench";
      this.querySelectorAll(".nav [data-screen]").forEach(b => {
        if (b.dataset.screen === screen) b.setAttribute("aria-current", "page");
        else b.removeAttribute("aria-current");
      });
    }

    _renderSite(active, showTheme, keep) {
      this.innerHTML = `
        <nav class="topbar" aria-label="主导航">
          <a class="topbar-logo" href="/">
            <span class="emoji" aria-hidden="true">🐝</span>
            <span class="brand">小蜜蜂</span>
            <span class="logo-sub">Bee Studio</span>
          </a>
          <div class="topbar-nav" role="tablist">
            ${TABS.map(t => `
              <a class="topbar-tab${t.id === active ? " active" : ""}" href="${t.href}"
                 role="tab" aria-selected="${t.id === active}"
                 data-tab="${t.id}"><span aria-hidden="true">${t.icon}</span>${t.label}</a>`).join("")}
            <span class="topbar-glider" aria-hidden="true"></span>
          </div>
          <div class="topbar-right">
            <span class="topbar-tools-label">关联工具</span>
            ${TOOLS.map(t => `<a class="topbar-tab external" href="${t.href}">${t.label}</a>`).join("")}
            <span class="topbar-divider" aria-hidden="true"></span>
          </div>
        </nav>`;

      const right = this.querySelector(".topbar-right");
      keep.forEach(node => right.appendChild(node));

      if (showTheme) {
        const btn = document.createElement("button");
        btn.className = "icon-btn";
        btn.type = "button";
        btn.setAttribute("aria-label", "切换深色模式");
        btn.title = "切换深色模式";
        btn.textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "☀️" : "🌙";
        btn.addEventListener("click", () => {
          const dark = document.documentElement.getAttribute("data-theme") !== "dark";
          document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
          btn.textContent = dark ? "☀️" : "🌙";
          try { localStorage.setItem("bee-theme", dark ? "dark" : "light"); } catch (_) {}
          this.dispatchEvent(new CustomEvent("bee-theme", { detail: { dark }, bubbles: true }));
        });
        right.appendChild(btn);
      }
    }

    _syncActive() {
      const declared = this.getAttribute("active") || "";
      const resolved = (declared === "image" && location.hash === "#templates") ? "workflow" : declared;
      const tabs = Array.from(this.querySelectorAll(".topbar-tab[data-tab]"));
      tabs.forEach(t => {
        const on = t.dataset.tab === resolved;
        t.classList.toggle("active", on);
        t.setAttribute("aria-selected", String(on));
        if (on) t.setAttribute("aria-current", "page");
        else t.removeAttribute("aria-current");
      });
      if (this._settle) this._settle();
    }

    _bindGlider() {
      const nav = this.querySelector(".topbar-nav");
      const glider = this.querySelector(".topbar-glider");
      const tabs = Array.from(this.querySelectorAll(".topbar-tab[data-tab]"));
      if (!nav || !glider) return;
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

      const moveTo = (el) => {
        if (!el) { glider.style.opacity = "0"; return; }
        const n = nav.getBoundingClientRect();
        const r = el.getBoundingClientRect();
        glider.style.left = (r.left - n.left) + "px";
        glider.style.width = r.width + "px";
        glider.style.opacity = "1";
      };
      const settle = () => requestAnimationFrame(() => moveTo(this.querySelector(".topbar-tab.active")));
      this._settle = settle;
      settle();
      // 字体加载完宽度会变，再校准一次
      if (document.fonts && document.fonts.ready) document.fonts.ready.then(settle);
      window.addEventListener("resize", settle, { passive: true });

      if (!reduced) {
        tabs.forEach(t => {
          t.addEventListener("mouseenter", () => moveTo(t));
          t.addEventListener("focus", () => moveTo(t));
        });
        nav.addEventListener("mouseleave", settle);
      }
    }
  }

  if (!customElements.get("bee-topbar")) customElements.define("bee-topbar", BeeTopbar);
})();
