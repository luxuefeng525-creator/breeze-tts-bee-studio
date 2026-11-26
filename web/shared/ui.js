/* ============================================================
   小蜜蜂 · 共享交互层
   - 主题：脚本加载即应用，避免首屏闪白
   - toast / 点击涟漪 / 视图切换过渡
   ============================================================ */
(function () {
  "use strict";

  // 首屏立即应用主题（本脚本在 <head> 同步加载）
  try {
    var dark = localStorage.getItem("bee-theme") === "dark";
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  } catch (_) {}

  function host() {
    var el = document.querySelector(".toast-host");
    if (!el) {
      el = document.createElement("div");
      el.className = "toast-host";
      el.setAttribute("aria-live", "polite");
      document.body.appendChild(el);
    }
    return el;
  }

  function toast(msg, isErr) {
    var el = document.createElement("div");
    el.className = "toast" + (isErr ? " err" : "");
    el.textContent = String(msg == null ? "" : msg);
    host().appendChild(el);
    setTimeout(function () { el.remove(); }, 3800);
  }

  function ripple(event) {
    var target = event.currentTarget;
    if (!target) return;
    var r = target.getBoundingClientRect();
    var d = Math.max(r.width, r.height);
    var ink = document.createElement("span");
    ink.className = "ripple-ink";
    ink.style.width = ink.style.height = d + "px";
    ink.style.left = (event.clientX - r.left - d / 2) + "px";
    ink.style.top = (event.clientY - r.top - d / 2) + "px";
    target.appendChild(ink);
    setTimeout(function () { ink.remove(); }, 600);
  }

  // 全局委托：所有 .btn-primary 自动带涟漪，页面无需逐个绑定
  document.addEventListener("pointerdown", function (e) {
    var btn = e.target.closest && e.target.closest(".btn-primary, .chip, .btn-ghost");
    if (btn) ripple({ currentTarget: btn, clientX: e.clientX, clientY: e.clientY });
  }, { passive: true });

  function viewTransition(fn) {
    var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (document.startViewTransition && !reduced) {
      document.startViewTransition(fn);
    } else {
      fn();
    }
  }

  window.BeeUI = { toast: toast, ripple: ripple, viewTransition: viewTransition };
})();
