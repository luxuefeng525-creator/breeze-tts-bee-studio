/* BreezeTTS Studio · 制声台 —— 前端逻辑（W2-5）
 *
 * 设计约定：
 *  - 屏幕状态由 body[data-screen] 驱动，状态态由 body[data-state] 驱动；
 *  - 首屏不出现开发者指标（模型摘要/fps/token 一律收进「设置与诊断」）；
 *  - 所有错误文案遵循「发生了什么 + 为什么 + 你可以怎么做」；
 *  - 文本规范化与发音词典由后端 /api/norm/preview 提供，前端只做预览与透传。
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const body = document.body;
  const audio = $("audioEl");

  const state = {
    screen: "workbench",
    voices: [],
    roles: [],
    history: [],
    voiceId: null,
    roleId: null,
    filter: "all",
    query: "",
    emotion: "",
    accent: "",
    speed: 1,
    cfg: 4,
    seed: 42,
    segLen: 60,
    pause: 0.35,
    busy: false,
    abort: null,
    segFailed: [],
    segDone: [],
    curSeg: 0,
    bytesGot: 0,
    bytesTotalEst: 0,
    rate: 0,
    lastResult: null,
    lastText: "",
    playerSpeed: 1,
    clone: { file: null, url: null, quality: null, duration: 0, type: "clone" },
    editId: null,
    model: null,
    libVoicePage: 0,
    libHistPage: 0,
    libRolePage: 0,
  };
  const LIB_VOICE_PAGE = 6;   // 每页音色卡片数（受 1080p 视口高度约束）
  const LIB_HIST_PAGE = 4;    // 每页历史条数
  const LIB_ROLE_PAGE = 4;    // 每页角色卡片数

  // ---------------------------------------------------------------- 工具
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  function fmtTime(sec) {
    if (!isFinite(sec) || sec < 0) sec = 0;
    const m = Math.floor(sec / 60);
    const s = sec - m * 60;
    return String(m).padStart(2, "0") + ":" + (s < 10 ? "0" : "") + s.toFixed(1);
  }

  function fmtDur(sec) {
    const m = Math.floor(sec / 60);
    const s = Math.round(sec % 60);
    return m > 0 ? `${m} 分 ${String(s).padStart(2, "0")} 秒` : `${s} 秒`;
  }

  function toast(msg, isError) {
    const t = $("toast");
    t.textContent = msg;
    t.hidden = false;
    t.classList.add("show");
    t.classList.toggle("toast-error", !!isError);
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => {
      t.classList.remove("show");
      setTimeout(() => { t.hidden = true; }, 220);
    }, isError ? 6000 : 3200);
  }

  async function api(path, opts) {
    const res = await fetch(path, opts);
    const ct = res.headers.get("content-type") || "";
    let data = {};
    if (ct.includes("application/json")) {
      data = await res.json().catch(() => ({}));
    }
    if (!res.ok) {
      const detail = (data && (data.detail || data.message)) || `请求失败（${res.status}）`;
      const err = new Error(detail);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function setScreen(name) {
    state.screen = name;
    body.dataset.screen = name;
    body.dataset.state = "default";
    document.querySelectorAll(".nav [data-screen]").forEach((b) => {
      if (b.dataset.screen === name) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    try {
      const u = new URL(location.href);
      u.searchParams.set("screen", name);
      history.replaceState(null, "", u);
    } catch (e) { /* 忽略 */ }
    if (name === "library") renderLibrary();
    if (name === "clone") refreshCloneState();
    if (name === "workbench") {
      // 从隐藏状态切回来，textarea 在 display:none 时量不出高度，必须重算
      requestAnimationFrame(() => updatePaper());
    }
  }

  function setWorkbenchState(s) {
    body.dataset.state = s;
    const empty = s === "empty";
    $("paperEmpty").hidden = !empty;
    $("actionDefault").hidden = s !== "default";
    $("actionLoading").hidden = s !== "loading";
    $("actionError").hidden = s !== "error";
    $("playerEmpty").hidden = !empty;
    $("playerLive").hidden = empty;
    $("playerLoading").hidden = s !== "loading";
    $("playerFoot").hidden = empty || s === "loading";
    $("historyEmpty").hidden = state.history.length > 0;
    $("historyScroll").hidden = state.history.length === 0;
  }

  // ---------------------------------------------------------------- 分段
  function splitText(text, maxLen) {
    const limit = Math.max(20, maxLen || 60);
    if (!text) return [];
    const out = [];
    let buf = "";
    const parts = text.split(/(?<=[。！？；\n.!?;])/);
    for (const raw of parts) {
      if (!raw) continue;
      if ((buf + raw).length <= limit || !buf) {
        buf += raw;
      } else {
        out.push(buf.trim());
        buf = raw;
      }
      while (buf.length > limit) {
        out.push(buf.slice(0, limit));
        buf = buf.slice(limit);
      }
    }
    if (buf.trim()) out.push(buf.trim());
    return out.filter((s) => s.length);
  }

  function updatePaper() {
    const text = $("synthText").value;
    const segs = splitText(text, state.segLen);
    const chars = text.replace(/\s/g, "").length;
    $("paperChars").textContent = `${chars} 字 · ${segs.length} 段`;
    const est = chars / 4.6;   // 约 4.6 字/秒
    $("paperEstimate").textContent = chars ? `约 ${fmtDur(est)}` : "等待输入";
    $("estimateTitle").textContent = chars ? `预计 ${fmtDur(est)}` : "等待输入";
    $("estimateSub").textContent = segs.length
      ? `${segs.length} 段 · 本地生成 · 失败可只重试单段`
      : "本地生成 · 失败可只重试单段";
    const _v = state.voices.find((x) => x.id === state.voiceId);
    const _role = state.roles.find((x) => x.id === state.roleId);
    const _roleBad = !!_role && !(_role.voice_id || "").trim();
    const _bad = _roleBad || (_v && _v.type === "clone" && refTextState(_v) === "missing");
    $("generateBtn").disabled = !chars || state.busy || !!_bad;
    $("paperSaved").textContent = chars ? "草稿已保存在本机" : "草稿已保存在本机";

    // 稿纸自适应高度：textarea 自身不滚动（overflow:hidden 会裁掉超出部分，
    // 而 .paper-body 只有被内容撑高才出现滚动条——长文本因此完全看不到后面）。
    const _ta = $("synthText");
    _ta.style.height = "auto";
    _ta.style.height = Math.max(_ta.scrollHeight, 120) + "px";

    // 段号栏
    const gutter = $("segGutter");
    gutter.innerHTML = segs.length
      ? segs.map((_, i) =>
          `<span class="seg-no${i === 0 ? " is-current" : ""}">${String(i + 1).padStart(2, "0")}<span class="seg-dot"></span></span>`
        ).join("")
      : "";

    if (!chars && !state.busy) setWorkbenchState("empty");
    else if (!state.busy && state.lastResult) setWorkbenchState("default");
    else if (!state.busy) setWorkbenchState("default");
    saveDraft(text);
  }

  function saveDraft(text) {
    try { localStorage.setItem("breeze_draft", text); } catch (e) { /* 忽略 */ }
  }

  // ---------------------------------------------------------------- 音色
  // 参考文稿状态：缺失/占位符/已填（audit A-07）
  const PLACEHOLDER_REF = ["参考文字稿", "参考文案", "占位", "placeholder", "test", "测试文字稿"];
  function refTextState(v) {
    const t = (v.ref_text || "").trim();
    if (!t) return "missing";
    const low = t.toLowerCase();
    if (PLACEHOLDER_REF.some((p) => low === p.toLowerCase() || low.startsWith(p.toLowerCase()))) {
      return "missing";
    }
    return "ok";
  }

  function voiceSubtitle(v) {
    const type = { clone: "克隆", design: "设计", direction: "导向" }[v.type] || v.type;
    const desc = (v.instruction || "").slice(0, 14);
    const extra = v.type === "clone"
      ? (refTextState(v) === "missing" ? " · 文稿待补充" : "")
      : "";
    return `${type}${desc ? " · " + desc : ""}${extra}`;
  }

  function renderVoices() {
    const q = state.query.trim().toLowerCase();
    const list = state.voices.filter((v) => {
      if (state.filter !== "all" && v.type !== state.filter) return false;
      if (!q) return true;
      return ((v.name || "") + (v.instruction || "") + (v.ref_text || "")).toLowerCase().includes(q);
    });
    $("voiceCount").textContent = `${state.voices.length} 个`;
    $("voiceGroupCount").textContent = `${list.length} 个`;
    $("cntAll").textContent = state.voices.length;
    $("cntClone").textContent = state.voices.filter((v) => v.type === "clone").length;
    $("cntDesign").textContent = state.voices.filter((v) => v.type === "design").length;
    $("cntDirection").textContent = state.voices.filter((v) => v.type === "direction").length;

    const box = $("voiceList");
    if (!list.length) {
      box.innerHTML = `<div class="empty-hint">${state.voices.length ? "没有匹配的音色" : "还没有音色，去「克隆」创建第一个"}</div>`;
      return;
    }
    box.innerHTML = list.map((v) => `
      <button class="voice-item${state.voiceId === v.id ? " active" : ""}" type="button"
              data-voice="${esc(v.id)}" aria-pressed="${state.voiceId === v.id}">
        <span class="voice-avatar">${esc((v.name || "?").slice(0, 1))}</span>
        <span class="voice-meta">
          <span class="voice-name">${esc(v.name)}${state.voiceId === v.id ? ' <span class="current-pill">当前</span>' : ""}</span>
          <span class="voice-sub">${esc(voiceSubtitle(v))}</span>
        </span>
        <span class="voice-play" aria-hidden="true"><svg class="icon"><use href="#i-play"/></svg></span>
        <span class="voice-actions">
          <span class="icon-btn" role="button" tabindex="0" data-edit="${esc(v.id)}" aria-label="编辑音色 ${esc(v.name)}" title="编辑"><svg class="icon icon-xs"><use href="#i-edit"/></svg></span>
          <span class="icon-btn del" role="button" tabindex="0" data-del="${esc(v.id)}" aria-label="删除音色 ${esc(v.name)}" title="删除"><svg class="icon icon-xs"><use href="#i-trash"/></svg></span>
        </span>
      </button>`).join("");
  }

  // ---------------------------------------------------------------- 角色
  function roleVoiceName(id) {
    const v = state.voices.find((x) => x.id === id);
    return v ? v.name : "未绑定音色";
  }

  function renderRoles() {
    const chips = $("roleChips");
    const label = $("roleGroupLabel");
    if (!state.roles.length) {
      chips.hidden = true; label.hidden = true;
    } else {
      label.hidden = false; chips.hidden = false;
      $("roleCount").textContent = `${state.roles.length} 个`;
      chips.innerHTML = state.roles.map((r) => `
        <button class="role-chip${state.roleId === r.id ? " active" : ""}" type="button"
                data-role="${esc(r.id)}" aria-pressed="${state.roleId === r.id}"
                title="${esc(r.description || roleVoiceName(r.voice_id))}">
          ${esc(r.name)}
        </button>`).join("");
    }

    const grid = $("roleGrid");
    $("roleEmpty").hidden = state.roles.length > 0;
    const rShown = state.roles.slice(state.libRolePage * LIB_ROLE_PAGE,
                                     (state.libRolePage + 1) * LIB_ROLE_PAGE);
    grid.innerHTML = rShown.map((r) => `
      <article class="role-card" data-rolecard="${esc(r.id)}">
        <header>
          <span class="voice-avatar">${esc((r.name || "?").slice(0, 1))}</span>
          <div>
            <h3>${esc(r.name)}</h3>
            <span class="card-type">${esc(roleVoiceName(r.voice_id))}</span>
          </div>
          <button class="icon-btn" type="button" data-roleedit="${esc(r.id)}" aria-label="编辑角色 ${esc(r.name)}"><svg class="icon icon-sm"><use href="#i-edit"/></svg></button>
          <button class="icon-btn del" type="button" data-roledel="${esc(r.id)}" aria-label="删除角色 ${esc(r.name)}"><svg class="icon icon-sm"><use href="#i-trash"/></svg></button>
        </header>
        <p class="card-desc">${esc(r.description || "（没有简介）")}</p>
      </article>`).join("");
    const rPages = Math.ceil(state.roles.length / LIB_ROLE_PAGE);
    if (rPages > 1) {
      grid.insertAdjacentHTML("beforeend",
        `<div class="lib-pager">
           <button type="button" class="btn btn-sm" data-librole="${state.libRolePage - 1}" ${state.libRolePage === 0 ? "disabled" : ""}>上一页</button>
           <span>第 ${state.libRolePage + 1} / ${rPages} 页 · 共 ${state.roles.length} 个角色</span>
           <button type="button" class="btn btn-sm" data-librole="${state.libRolePage + 1}" ${state.libRolePage >= rPages - 1 ? "disabled" : ""}>下一页</button>
         </div>`);
    }
  }

  function selectRole(id) {
    if (state.roleId === id) {
      state.roleId = null;
      renderRoles();
      syncVoiceReadiness();
      return;
    }
    state.roleId = id;
    const r = state.roles.find((x) => x.id === id);
    if (r) {
      if (r.voice_id) state.voiceId = r.voice_id;
      const d = r.defaults || {};
      if (d.cfg_scale != null) { state.cfg = d.cfg_scale; $("cfg").value = d.cfg_scale; $("cfgVal").textContent = Number(d.cfg_scale).toFixed(1); }
      if (d.seed != null) { state.seed = d.seed; $("seed").value = d.seed; $("seedVal").textContent = String(d.seed); }
      if (d.emotion != null) {
        state.emotion = d.emotion;
        $("emotionChips").querySelectorAll(".chip").forEach((x) => {
          x.setAttribute("aria-pressed", String(x.dataset.emotion === d.emotion));
        });
      }
      if (d.accent != null) { state.accent = d.accent; $("accentSelect").value = d.accent; }
    }
    renderVoices();
    renderRoles();
    syncVoiceReadiness();
    toast(r ? `已切换到角色「${r.name}」` : "已取消角色");
  }

  function openRoleDialog(id) {
    state.editRoleId = id || null;
    const r = id ? state.roles.find((x) => x.id === id) : null;
    $("roleDlgTitle").textContent = r ? "编辑角色" : "新建角色";
    $("roleName").value = r ? r.name : "";
    $("roleDesc").value = r ? (r.description || "") : "";
    const sel = $("roleVoice");
    sel.innerHTML = '<option value="">（不绑定）</option>' +
      state.voices.map((v) => `<option value="${esc(v.id)}">${esc(v.name)}</option>`).join("");
    sel.value = r && r.voice_id ? r.voice_id : "";
    $("roleDlg").showModal();
  }

  async function saveRole() {
    const name = $("roleName").value.trim();
    if (!name) { toast("请填写角色名", true); return; }
    const body = {
      name,
      description: $("roleDesc").value.trim(),
      voice_id: $("roleVoice").value,
    };
    try {
      if (state.editRoleId) {
        await api("/api/roles/" + state.editRoleId, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } else {
        await api("/api/roles", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      }
      $("roleDlg").close();
      await refreshRoles();
      toast("角色已保存");
    } catch (err) { toast(err.message, true); }
  }

  async function deleteRole(id) {
    const r = state.roles.find((x) => x.id === id);
    if (!r) return;
    if (!confirm(`删除角色「${r.name}」？`)) return;
    try {
      await api("/api/roles/" + id, { method: "DELETE" });
      if (state.roleId === id) state.roleId = null;
      await refreshRoles();
      toast("已删除");
    } catch (err) { toast(err.message, true); }
  }

  function selectVoice(id) {
    state.voiceId = state.voiceId === id ? null : id;
    renderVoices();
    syncVoiceReadiness();
  }

  // 克隆音色缺真实文稿时提前拦截（PRD 5.2：合成按钮禁用并点名缺项）
  function syncVoiceReadiness() {
    const v = state.voices.find((x) => x.id === state.voiceId);
    const bad = v && v.type === "clone" && refTextState(v) === "missing";
    const warn = $("voiceWarn");
    const role = state.roles.find((x) => x.id === state.roleId);

    // 角色没绑音色：先讲清楚，否则用户听到的是默认发音人却以为角色生效了
    if (role && !(role.voice_id || "").trim()) {
      warn.hidden = false;
      warn.innerHTML =
        `<svg class="icon icon-sm" aria-hidden="true"><use href="#i-alert"/></svg>` +
        `<span>角色「${esc(role.name)}」还没绑定音色，现在只会用默认发音人</span>` +
        `<button type="button" class="btn btn-sm" data-fixrole="${esc(role.id)}">去绑定音色</button>`;
      $("generateBtn").disabled = true;
      return;
    }
    if (!v || !bad) {
      warn.hidden = true;
      $("generateBtn").disabled = state.busy || !$("synthText").value.trim();
      return;
    }
    warn.hidden = false;
    warn.innerHTML =
      `<svg class="icon icon-sm" aria-hidden="true"><use href="#i-alert"/></svg>` +
      `<span>音色「${esc(v.name)}」的参考文稿还是占位符，补齐后才能合成（否则声音对不上）</span>` +
      `<button type="button" class="btn btn-sm" data-fixref="${esc(v.id)}">去补文稿</button>`;
    $("generateBtn").disabled = true;
  }

  // ---------------------------------------------------------------- 历史
  function groupLabel(ts) {
    const d = new Date(ts.replace(/-/g, "/"));
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const day = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    const diff = Math.round((today - day) / 86400000);
    if (diff === 0) return "今天";
    if (diff === 1) return "昨天";
    if (diff < 7) return "本周更早";
    return d.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
  }

  function voiceName(id) {
    const v = state.voices.find((x) => x.id === id);
    return v ? v.name : "默认音色";
  }

  function renderHistory() {
    const box = $("historyScroll");
    const items = state.history.slice().reverse();
    if (!items.length) {
      box.innerHTML = "";
      $("historyEmpty").hidden = false;
      $("historyScroll").hidden = true;
      return;
    }
    $("historyEmpty").hidden = true;
    $("historyScroll").hidden = false;
    let lastGroup = null;
    let html = "";
    // 只渲染最近 8 条：更早的去「音色库 → 合成历史」看（避免滚动容器内元素越界）
    items.slice(0, 8).forEach((h) => {
      const g = groupLabel(h.created_at || "");
      if (g !== lastGroup) {
        html += `<div class="hist-group"><span>${esc(g)}</span></div>`;
        lastGroup = g;
      }
      const title = (h.text || "").replace(/\s+/g, " ").slice(0, 22) || "（无文本）";
      html += `
        <div class="hist-row${state.lastResult && state.lastResult.id === h.id ? " current" : ""}">
          <span class="mini-wave" data-wave="10" data-seed="${(h.text || "").length % 17}" data-progress="0.3"></span>
          <span class="hist-main">
            <span class="hist-title">${esc(title)}</span>
            <span class="hist-sub">${esc(voiceName(h.voice_id))} · ${fmtTime(h.duration || 0)} · ${esc((h.created_at || "").slice(11, 16))}</span>
          </span>
          <span class="hist-dur">${fmtTime(h.duration || 0).slice(0, 5)}</span>
          <button class="hist-play" type="button" data-hplay="${esc(h.wav_url || "")}" aria-label="播放：${esc(title)}"><svg class="icon"><use href="#i-play"/></svg></button>
          <button class="icon-btn del" type="button" data-hdel="${esc(h.id)}" aria-label="删除这条记录" title="删除"><svg class="icon icon-xs"><use href="#i-trash"/></svg></button>
        </div>`;
    });
    box.innerHTML = html;
    const more = items.length > 8;
    $("historyAllBtn").hidden = !more;
    $("historyAllBtn").innerHTML = more
      ? `查看全部 ${items.length} 条历史 <svg class="icon icon-xs" aria-hidden="true" style="margin-left:5px"><use href="#i-arrow"/></svg>`
      : "查看全部历史";
  }

  // ---------------------------------------------------------------- 合成
  function buildInstruction() {
    const parts = [];
    const v = state.voices.find((x) => x.id === state.voiceId);
    if (v && v.instruction) parts.push(v.instruction);
    if (state.emotion) parts.push(state.emotion);
    if (state.accent) parts.push(state.accent);
    if (!parts.length) parts.push("用标准普通话，发音标准，字正腔圆，吐字清晰");
    return parts.join("，");
  }

  function currentParams() {
    return {
      voice_id: state.voiceId || "",
      cfg_scale: state.cfg,
      seed: state.seed,
      speed: state.speed,
      emotion: state.emotion,
      accent: state.accent,
      seg_len: state.segLen,
      pause: state.pause,
    };
  }

  // ---- 进度估算：中文约 5 字/秒，24kHz s16le = 48000 字节/秒 ----
  const CHARS_PER_SEC = 5.0;
  const BYTES_PER_CHAR = (24000 * 2) / CHARS_PER_SEC;

  function fmtEta(sec) {
    if (!isFinite(sec) || sec < 0) return "—";
    if (sec < 60) return `${Math.ceil(sec)} 秒`;
    const m = Math.floor(sec / 60);
    const s = Math.ceil(sec % 60);
    return `${m} 分 ${String(s).padStart(2, "0")} 秒`;
  }

  // ---- 渐进试听：WebAudio 按段排程，连续播放 ----
  let actx = null;
  let nextTime = 0;
  function ensureAudioCtx() {
    if (!actx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      actx = new AC();
    }
    if (actx.state === "suspended") actx.resume();
    return actx;
  }

  function scheduleSegment(pcmBytes) {
    const ctx = ensureAudioCtx();
    if (!ctx || !pcmBytes || pcmBytes.length < 2) return;
    const samples = new Int16Array(pcmBytes.buffer, pcmBytes.byteOffset,
                                   pcmBytes.byteLength / 2);
    const buf = ctx.createBuffer(1, samples.length, 24000);
    const ch = buf.getChannelData(0);
    for (let i = 0; i < samples.length; i++) ch[i] = samples[i] / 32768;
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    const startAt = Math.max(nextTime, ctx.currentTime + 0.02);
    src.start(startAt);
    nextTime = startAt + buf.duration;
  }

  function resetPlaybackQueue() {
    nextTime = 0;
    if (actx) { try { actx.close(); } catch (e) { /* 忽略 */ } actx = null; }
  }

  // ---- 段号栏状态：当前段 / 已完成 / 失败 ----
  function renderSegStatus(segments) {
    const gutter = $("segGutter");
    gutter.innerHTML = segments.map((_, i) => {
      const cls = state.segFailed.includes(i) ? " is-failed"
        : (state.segDone.includes(i) ? " is-done"
        : (i === state.curSeg ? " is-current" : ""));
      const mark = state.segFailed.includes(i) ? "✕"
        : (state.segDone.includes(i) ? "✓" : "");
      return `<span class="seg-no${cls}">${String(i + 1).padStart(2, "0")}<span class="seg-dot">${mark}</span></span>`;
    }).join("");
  }

  async function generate() {
    if (state.busy) return;
    const text = $("synthText").value.trim();
    if (!text) { toast("先写一点要合成的文字", true); return; }

    // 按钮的 disabled 挡不住 Ctrl+Enter 和「重试」按钮，这里必须再判一次，
    // 否则会绕过后端守卫、静默用错音色。
    const _rv = state.voices.find((x) => x.id === state.voiceId);
    const _rr = state.roles.find((x) => x.id === state.roleId);
    if (_rr && !(_rr.voice_id || "").trim()) {
      toast(`角色「${_rr.name}」还没绑定音色，先绑定再生成`, true);
      return;
    }
    if (_rv && _rv.type === "clone" && refTextState(_rv) === "missing") {
      toast(`音色「${_rv.name}」的参考文稿还没补齐，补齐后再生成`, true);
      return;
    }

    const segments = splitText(text, state.segLen);
    if (!segments.length) { toast("文本太短，无法分段", true); return; }

    state.busy = true;
    state.lastText = text;
    state.segFailed = [];
    state.segDone = [];
    state.curSeg = 0;
    state.bytesGot = 0;
    state.rate = 0;
    state.bytesTotalEst = Math.max(1, text.replace(/\s/g, "").length * BYTES_PER_CHAR);

    $("generateBtn").disabled = true;
    setWorkbenchState("loading");
    $("playerLoadingText").textContent = "正在合成第 1 段…";
    $("errorAlert").hidden = true;
    $("failTitle").textContent = "生成未完成";
    $("failSub").textContent = "";
    resetPlaybackQueue();

    const t0 = Date.now();
    const savedFiles = [];
    const instr = buildInstruction();
    const stamp = Date.now();

    try {
      for (let i = 0; i < segments.length; i++) {
        state.curSeg = i;
        const seg = segments[i];
        const preview = seg.replace(/\s+/g, " ").slice(0, 20);
        setProgress(pctFromBytes(), `第 ${i + 1}/${segments.length} 段 · 正在念：${preview}`);
        $("progressSub").textContent = etaText();
        $("playerLoadingText").textContent = `正在合成第 ${i + 1} 段，共 ${segments.length} 段`;
        renderSegStatus(segments);

        const fd = new FormData();
        fd.append("text", seg);
        fd.append("instruction", instr);
        fd.append("cfg_scale", String(state.cfg));
        fd.append("seed", String(state.seed));
        fd.append("norm", "2");
        if (state.voiceId) fd.append("voice_id", state.voiceId);
        // 角色也要发：仅靠 voice_id 无法让后端区分"角色未绑音色"与"没选角色"
        if (state.roleId) fd.append("role_id", state.roleId);
        const segName = `seg_${stamp}_${i}.wav`;
        fd.append("save_as", segName);

        const resp = await fetch("/api/synthesize/stream", { method: "POST", body: fd });
        if (!resp.ok) {
          clearTimeout(waitTick);
          let detail = `第 ${i + 1} 段失败（HTTP ${resp.status}）`;
          try {
            const j = await resp.json();
            if (j && j.detail) detail = `第 ${i + 1} 段失败：${j.detail}`;
          } catch (e) { /* 忽略 */ }
          state.segFailed.push(i);
          renderSegStatus(segments);
          continue;
        }

        const reader = resp.body.getReader();
        const parts = [];
        let segBytes = 0;
        let waitTick = null;
        // 引擎忙时（上游排队）给出明确提示，避免 0% 干等看起来像卡死
        waitTick = setTimeout(() => {
          if (!state.bytesGot) {
            $("progressTitle").textContent =
              `第 ${i + 1}/${segments.length} 段 · 等待推理引擎空闲…`;
            $("progressSub").textContent = "上一条合成还没结束，已自动排队";
          }
        }, 4000);
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          if (!value || !value.length) continue;
          clearTimeout(waitTick);
          parts.push(value);
          segBytes += value.length;
          state.bytesGot += value.length;
          // 速率估算（字节/秒），用于倒推剩余时间
          const elapsed = (Date.now() - t0) / 1000;
          if (elapsed > 0.4) state.rate = state.bytesGot / elapsed;
          setProgress(pctFromBytes(), `第 ${i + 1}/${segments.length} 段 · 正在念：${preview}`);
          $("progressSub").textContent = etaText();
        }

        const pcm = concatBytes(parts);
        scheduleSegment(pcm);
        savedFiles.push(segName);
        state.segDone.push(i);
        renderSegStatus(segments);
      }

      if (!savedFiles.length) {
        throw new Error("所有段落都没有生成成功，可以点击「重新生成」再试一次");
      }

      // 合并成最终文件（同时落一条历史）
      const gaps = [];
      for (let i = 0; i < savedFiles.length - 1; i++) gaps.push(state.pause);
      const merged = await api("/api/concat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          files: savedFiles,
          text,
          gaps,
          speed: state.speed,
          voice_id: state.voiceId || "",
          role_id: state.roleId || "",
        }),
      });

      const secs = ((Date.now() - t0) / 1000).toFixed(1);
      state.lastResult = merged;
      setProgress(100, "已完成");
      $("progressSub").textContent = `耗时 ${secs} 秒`;
      showResult(merged);
      if (state.segFailed.length) {
        toast(`完成，但第 ${state.segFailed.map((n) => n + 1).join("、")} 段失败，可重试`, true);
      } else {
        toast(`合成完成：${merged.duration.toFixed(1)} 秒音频 · 耗时 ${secs} 秒`);
      }
      await refreshHistory();
    } catch (err) {
      showError(err);
    } finally {
      state.busy = false;
      $("generateBtn").disabled = !$("synthText").value.trim();
      await refreshVoices();
    }
  }

  function concatBytes(list) {
    let total = 0;
    for (const b of list) total += b.length;
    const out = new Uint8Array(total);
    let off = 0;
    for (const b of list) { out.set(b, off); off += b.length; }
    return out;
  }

  function pctFromBytes() {
    if (!state.bytesTotalEst) return 0;
    // 未完成前最多显示 99%，避免进度条提前满格
    return Math.min(99, Math.round((state.bytesGot / state.bytesTotalEst) * 100));
  }

  function etaText() {
    if (!state.rate) return "正在估算剩余时间…";
    const remain = Math.max(0, state.bytesTotalEst - state.bytesGot);
    const eta = remain / state.rate;
    return `预计还需 ${fmtEta(eta)}`;
  }

  function setProgress(pct, text) {
    const bar = $("progressTrack");
    bar.setAttribute("aria-valuenow", String(Math.round(pct)));
    bar.setAttribute("aria-valuetext", text || "");
    bar.firstElementChild.style.width = Math.max(2, Math.min(100, pct)) + "%";
    $("progressTitle").textContent = text || "正在生成";
    const label = $("progressLabel");
    if (label) label.textContent = Math.round(pct) + "%";
  }

  function showResult(res) {
    setWorkbenchState("default");
    $("playerEmpty").hidden = true;
    $("playerLive").hidden = false;
    $("playerFoot").hidden = false;
    const v = state.voices.find((x) => x.id === state.voiceId);
    $("playerTitle").textContent = (state.lastText || "").replace(/\s+/g, " ").slice(0, 24) || "最新生成";
    $("playerSub").textContent = `${v ? v.name : "默认音色"} · ${state.speed.toFixed(2)}× · ${new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`;
    $("playerTotal").textContent = fmtTime(res.duration);
    $("playerNow").textContent = fmtTime(0);
    audio.src = res.wav_url;
    audio.playbackRate = state.playerSpeed;
    $("playerWave").dataset.progress = "0";
    $("playBtn").setAttribute("aria-pressed", "false");
    $("playBtn").innerHTML = '<svg class="icon"><use href="#i-play"/></svg>';
  }

  function showError(err) {
    const detail = err.message || "未知错误";
    $("errorTitle").textContent = "合成没有完成";
    $("errorDetail").textContent = detail;
    $("errorAlert").hidden = false;
    $("failTitle").textContent = "生成未完成";
    $("failSub").textContent = detail;
    setWorkbenchState("error");
    toast(detail, true);
  }

  // ---------------------------------------------------------------- 词典
  async function refreshDict() {
    const items = await api("/api/dict").catch(() => []);
    const box = $("dictList");
    if (!items.length) {
      box.innerHTML = '<div class="empty-hint">还没有词条。遇到读错的多音字时，添加一条试试。</div>';
    } else {
      box.innerHTML = items.map((d) => `
        <div class="dict-item">
          <span class="dict-word">${esc(d.word)}</span>
          <span class="dict-arrow">→</span>
          <span class="dict-reading">${esc(d.reading)}</span>
          <span class="dict-scope">${d.scope === "global" ? "全局" : "音色"}</span>
          <button class="icon-btn del" type="button" data-dictdel="${esc(d.word)}" data-dictscope="${esc(d.scope)}" aria-label="删除词条 ${esc(d.word)}"><svg class="icon icon-xs"><use href="#i-trash"/></svg></button>
        </div>`).join("");
    }
    const scopeSel = $("dictScope");
    const cur = scopeSel.value;
    scopeSel.innerHTML = '<option value="global">全局</option>' +
      state.voices.map((v) => `<option value="${esc(v.id)}">${esc(v.name)}</option>`).join("");
    scopeSel.value = cur;
    return items;
  }

  async function previewNorm() {
    const text = $("synthText").value;
    if (!text.trim()) { $("dictBar").hidden = true; return; }
    try {
      const res = await api("/api/norm/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text,
          voice_id: state.voiceId || "",
          instruction: buildInstruction(),
          accent: state.accent,
          emotion: state.emotion,
          cfg_scale: state.cfg,
        }),
      });
      const hits = res.changes || [];
      if (!hits.length) { $("dictBar").hidden = true; return; }
      const dictHits = hits.filter((h) => String(h.kind || "").startsWith("dict-"));
      const other = hits.filter((h) => !String(h.kind || "").startsWith("dict-"));
      const parts = [];
      if (other.length) parts.push(`已规范化 ${other.length} 处（数字、日期、单位等）`);
      if (dictHits.length) parts.push(`发音词典命中 ${dictHits.length} 处`);
      $("dictText").textContent = parts.join(" · ");
      $("dictBar").hidden = false;
    } catch (e) {
      $("dictBar").hidden = true;
    }
  }

  // ---------------------------------------------------------------- 克隆
  function refreshCloneState() {
    const c = state.clone;
    const hasFile = !!c.file;
    $("refUpload").hidden = hasFile;
    $("refPlayer").hidden = !hasFile;
    $("refText").disabled = !hasFile;

    const type = c.type;
    const isClone = type === "clone";
    const isDirection = type === "direction";
    const isDesign = type === "design";
    // 声音描述：克隆可选（声音来自参考音频）；设计与导向必填
    $("voiceInstruction").disabled = false;
    $("consentBox").disabled = isDesign;
    $("refText").disabled = isDesign || !hasFile;
    $("abPlayBtn").disabled = !hasFile;

    const nameOk = $("voiceName").value.trim().length > 0;
    const instrOk = isClone || $("voiceInstruction").value.trim().length > 0;
    const fileOk = isDesign || hasFile;
    const textOk = isDesign || $("refText").value.trim().length > 0;
    const consentOk = isDesign || $("consentBox").checked;

    const ok = nameOk && instrOk && fileOk && textOk && consentOk;
    const btn = $("saveVoiceBtn");
    btn.disabled = !ok;

    // 禁用也必须说清缺什么——否则用户只会看到"点了没反应"
    let missing = "";
    if (!nameOk) missing = "先给这个音色起个名字";
    else if (!fileOk) missing = "先上传一段参考音频（10–15 秒干净人声）";
    else if (!textOk) missing = "填写与录音逐字一致的文稿——这是相似度的关键";
    else if (!consentOk) missing = "勾选授权声明";
    else if (!instrOk) missing = "填写一句声音描述（设计与导向音色必填）";
    btn.title = ok ? "保存到音色库" : ("还不能保存：" + missing);
    $("profileHint").textContent = ok
      ? "可以保存了，保存后可在合成台直接选用。"
      : ("还不能保存：" + missing + "。");
    $("profileHint").classList.toggle("is-blocked", !ok);
  }

  async function uploadRef(file) {
    state.clone.file = file;
    state.clone.url = URL.createObjectURL(file);
    const fd = new FormData();
    fd.append("ref_audio", file, file.name);
    try {
      const res = await api("/api/voices", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: $("voiceName").value.trim() || "临时音色",
          type: state.clone.type,
          instruction: $("voiceInstruction").value.trim(),
          ref_text: $("refText").value.trim(),
        }),
      });
      state.clone.pendingId = res.id;
      const up = await api(`/api/voices/${res.id}/ref`, { method: "POST", body: fd });
      state.clone.duration = up.ref_duration || 0;
      state.clone.quality = up.quality || null;
      state.clone.audioUrl = `/api/refs/${(up.ref_audio || "").split("/").pop()}`;
      renderRefQuality(up);
      $("refTime").textContent = fmtTime(state.clone.duration);
      $("abDuration").textContent = fmtTime(state.clone.duration);
      $("abPlayBtn").disabled = false;
      toast("参考音频已上传");
      await refreshVoices();
    } catch (err) {
      toast("上传失败：" + err.message, true);
      // 回收刚创建的草稿音色，避免留下孤儿记录
      if (state.clone.pendingId) {
        try {
          await api("/api/voices/" + state.clone.pendingId, { method: "DELETE" });
          state.clone.pendingId = null;
          await refreshVoices();
        } catch (e) { /* 忽略 */ }
      }
    }
    refreshCloneState();
  }

  function renderRefQuality(up) {
    const q = up.quality;
    const meta = $("refMeta");
    const checks = $("refChecks");
    const status = $("refStatus");
    if (!q) { meta.hidden = true; checks.innerHTML = ""; status.hidden = true; return; }

    meta.hidden = false;
    const bits = [];
    if (q.sample_rate) bits.push(`${Math.round(q.sample_rate / 1000)} kHz`);
    if (q.channels) bits.push(q.channels === 1 ? "单声道" : `${q.channels} 声道`);
    if (up.ref_duration) bits.push(`${up.ref_duration.toFixed(1)} 秒`);
    if (q.peak_dbfs != null) bits.push(`峰值 ${q.peak_dbfs.toFixed(1)} dB`);
    meta.innerHTML = bits.map((b) => `<span>${esc(b)}</span>`).join("");

    const issues = q.issues || [];
    checks.innerHTML = issues.length
      ? issues.map((i) => `<div class="check${i.severity === "block" ? " bad" : (i.severity === "warn" ? " warn" : "")}">
          <svg class="icon"><use href="#${i.severity === "ok" ? "i-check" : "i-alert"}"/></svg>
          <span>${esc(i.message)}</span></div>`).join("")
      : `<div class="check"><svg class="icon"><use href="#i-check"/></svg><span>时长、响度、削波均在建议范围内</span></div>`;

    status.hidden = false;
    if (q.score != null) {
      status.className = "status-pill " + (q.ready ? (q.score >= 70 ? "ok" : "warn") : "bad");
      status.innerHTML = `<svg class="icon"><use href="#i-check"/></svg>质检 ${q.score} 分`;
    }
  }

  async function saveCloneVoice() {
    const name = $("voiceName").value.trim();
    if (!name) { toast("请填写音色名称", true); return; }
    const id = state.clone.pendingId;
    if (!id) { toast("请先上传参考音频", true); return; }
    try {
      await api(`/api/voices/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          type: state.clone.type,
          instruction: $("voiceInstruction").value.trim(),
          ref_text: $("refText").value.trim(),
        }),
      });
      toast("已保存到音色库");
      // 重置表单，避免下一次克隆复用上一条的状态
      state.clone = { file: null, url: null, quality: null, duration: 0,
                      type: state.clone.type, pendingId: null, audioUrl: null };
      $("voiceName").value = "";
      $("refText").value = "";
      $("voiceInstruction").value = "";
      $("consentBox").checked = false;
      $("refFile").value = "";
      $("refStatus").hidden = true;
      $("refMeta").hidden = true;
      $("refChecks").innerHTML = "";
      $("abDuration").textContent = "—";
      await refreshVoices();
      setScreen("workbench");
    } catch (err) {
      toast("保存失败：" + err.message, true);
    }
  }

  // ---------------------------------------------------------------- 音色库
  function renderLibrary() {
    const q = ($("libSearch").value || "").trim().toLowerCase();
    const list = state.voices.filter((v) =>
      !q || ((v.name || "") + (v.instruction || "") + (v.ref_text || "")).toLowerCase().includes(q));
    const clones = state.voices.filter((v) => v.type === "clone").length;
    $("libraryDesc").textContent =
      `${state.voices.length} 个音色，${clones} 个来自你的克隆。最近使用、标签和授权都集中在这里。`;

    const grid = $("libraryGrid");
    $("libraryEmpty").hidden = list.length > 0;
    const vStart = state.libVoicePage * LIB_VOICE_PAGE;
    const vShown = list.slice(vStart, vStart + LIB_VOICE_PAGE);
    grid.innerHTML = vShown.map((v) => `
      <article class="voice-card" data-voice="${esc(v.id)}">
        <header>
          <span class="voice-avatar">${esc((v.name || "?").slice(0, 1))}</span>
          <div>
            <h3>${esc(v.name)}</h3>
            <span class="card-type">${({ clone: "克隆", design: "设计", direction: "导向" })[v.type] || v.type}</span>
          </div>
          <button class="icon-btn" type="button" data-edit="${esc(v.id)}" aria-label="编辑 ${esc(v.name)}"><svg class="icon icon-sm"><use href="#i-edit"/></svg></button>
        </header>
        <p class="card-desc">${esc(v.instruction || (v.type === "clone" ? "" : v.ref_text) || "（没有描述）")}</p>
        ${v.type === "clone" ? (refTextState(v) === "missing"
            ? '<span class="card-warn">文稿待补充</span>'
            : '<span class="card-ok">文稿已校对</span>') : ""}
        <footer>
          <span>${v.ref_duration ? v.ref_duration.toFixed(1) + " 秒参考" : "无参考音频"}</span>
          <span>${esc((v.created_at || "").slice(0, 10))}</span>
        </footer>
      </article>`).join("");

    // 分页控件
    const vPages = Math.ceil(list.length / LIB_VOICE_PAGE);
    if (vPages > 1) {
      grid.insertAdjacentHTML("beforeend",
        `<div class="lib-pager">
           <button type="button" class="btn btn-sm" data-libvoice="${state.libVoicePage - 1}" ${state.libVoicePage === 0 ? "disabled" : ""}>上一页</button>
           <span>第 ${state.libVoicePage + 1} / ${vPages} 页 · 共 ${list.length} 个音色</span>
           <button type="button" class="btn btn-sm" data-libvoice="${state.libVoicePage + 1}" ${state.libVoicePage >= vPages - 1 ? "disabled" : ""}>下一页</button>
         </div>`);
    }

    const hist = state.history.slice().reverse();
    $("libHistoryCount").textContent = `${hist.length} 条`;
    const hStart = state.libHistPage * LIB_HIST_PAGE;
    const shown = hist.slice(hStart, hStart + LIB_HIST_PAGE);
    const hPages = Math.ceil(hist.length / LIB_HIST_PAGE);
    $("libraryHistory").innerHTML = hist.length
      ? shown.map((h) => `
        <div class="lib-hist-row">
          <span class="hist-main">
            <span class="hist-title">${esc((h.text || "").replace(/\s+/g, " ").slice(0, 40) || "（无文本）")}</span>
            <span class="hist-sub">${esc(voiceName(h.voice_id))} · ${fmtTime(h.duration || 0)} · ${esc(h.created_at || "")}</span>
          </span>
          <button class="hist-play" type="button" data-hplay="${esc(h.wav_url || "")}" aria-label="播放"><svg class="icon"><use href="#i-play"/></svg></button>
          <button class="icon-btn del" type="button" data-hdel="${esc(h.id)}" aria-label="删除"><svg class="icon icon-xs"><use href="#i-trash"/></svg></button>
        </div>`).join("")
      : '<div class="empty-hint">还没有合成记录。</div>';
    if (hPages > 1) {
      $("libraryHistory").insertAdjacentHTML("beforeend",
        `<div class="lib-pager">
           <button type="button" class="btn btn-sm" data-libhist="${state.libHistPage - 1}" ${state.libHistPage === 0 ? "disabled" : ""}>上一页</button>
           <span>第 ${state.libHistPage + 1} / ${hPages} 页 · 共 ${hist.length} 条</span>
           <button type="button" class="btn btn-sm" data-libhist="${state.libHistPage + 1}" ${state.libHistPage >= hPages - 1 ? "disabled" : ""}>下一页</button>
         </div>`);
    }
  }

  // ---------------------------------------------------------------- 数据刷新
  async function refreshVoices() {
    state.voices = await api("/api/voices").catch(() => []);
    if (state.voiceId && !state.voices.some((v) => v.id === state.voiceId)) state.voiceId = null;
    renderVoices();
    syncVoiceReadiness();
    if (state.screen === "library") renderLibrary();
  }

  async function refreshRoles() {
    state.roles = await api("/api/roles").catch(() => []);
    renderRoles();
    syncVoiceReadiness();   // 保存/删除角色都会走这里，是唯一的收敛点
  }

  async function refreshHistory() {
    state.history = await api("/api/history").catch(() => []);
    renderHistory();
    if (state.screen === "library") renderLibrary();
  }

  async function refreshModel() {
    try {
      state.model = await api("/api/model");
      $("engineText").textContent = "本地引擎 · 就绪";
      $("engineState").classList.add("ok");
    } catch (e) {
      state.model = null;
      $("engineText").textContent = "本地引擎 · 未连接";
      $("engineState").classList.add("bad");
    }
  }

  // ---------------------------------------------------------------- 播放
  function togglePlay() {
    if (!audio.src) { toast("还没有可播放的音频", true); return; }
    if (audio.paused) { audio.play(); } else { audio.pause(); }
  }

  audio.addEventListener("play", () => {
    $("playBtn").setAttribute("aria-pressed", "true");
    $("playBtn").innerHTML = '<svg class="icon"><use href="#i-pause"/></svg>';
  });
  audio.addEventListener("pause", () => {
    $("playBtn").setAttribute("aria-pressed", "false");
    $("playBtn").innerHTML = '<svg class="icon"><use href="#i-play"/></svg>';
  });
  audio.addEventListener("timeupdate", () => {
    $("playerNow").textContent = fmtTime(audio.currentTime);
    const pct = audio.duration ? audio.currentTime / audio.duration : 0;
    $("playerWave").dataset.progress = String(pct);
    $("playerWave").setAttribute("aria-valuenow", String(Math.round(pct * 100)));
  });
  audio.addEventListener("ended", () => {
    $("playerWave").dataset.progress = "0";
  });

  // ---------------------------------------------------------------- 事件绑定
  function bind() {
    // 导航
    document.querySelectorAll(".nav [data-screen]").forEach((b) => {
      b.addEventListener("click", () => setScreen(b.dataset.screen));
    });
    document.querySelectorAll("[data-goto]").forEach((b) => {
      b.addEventListener("click", () => setScreen(b.dataset.goto));
    });

    // 文本
    $("synthText").addEventListener("input", () => {
      updatePaper();
      clearTimeout(bind._t);
      bind._t = setTimeout(previewNorm, 400);
    });
    // 稿纸高度依赖可用宽度：窗口变化后必须重算。
    // 只在工作台屏算——隐藏屏的 scrollHeight 恒为 0，会把高度钉在下限，
    // 切回工作台时看起来就是"长文本又被裁掉了"。防抖避免拖拽窗口时抖动。
    let _rszTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(_rszTimer);
      _rszTimer = setTimeout(() => {
        if (state.screen === "workbench") updatePaper();
      }, 120);
    });
    $("loadSampleBtn").addEventListener("click", () => {
      $("synthText").value = "雨落在旧书店的玻璃上，像有人在很轻地翻书。老板娘把最后一盏灯留到九点，她说，总有人赶在打烊前，来找一本已经绝版的书。";
      updatePaper(); previewNorm(); $("synthText").focus();
    });
    $("pasteBtn").addEventListener("click", () => {
      $("synthText").focus();
      toast("把文本粘贴到稿纸里就行");
    });

    // 音色
    $("voiceWarn").addEventListener("click", (e) => {
      const ref = e.target.closest("[data-fixref]");
      if (ref) { openEdit(ref.dataset.fixref); return; }
      const role = e.target.closest("[data-fixrole]");
      if (role) openRoleDialog(role.dataset.fixrole);
    });
    $("voiceList").addEventListener("click", async (e) => {
      const edit = e.target.closest("[data-edit]");
      if (edit) { e.stopPropagation(); openEdit(edit.dataset.edit); return; }
      const del = e.target.closest("[data-del]");
      if (del) { e.stopPropagation(); await deleteVoice(del.dataset.del); return; }
      const item = e.target.closest("[data-voice]");
      if (item) { selectVoice(item.dataset.voice); previewNorm(); }
    });
    $("voiceSearch").addEventListener("input", (e) => {
      state.query = e.target.value; renderVoices();
    });
    $("voiceFilters").addEventListener("click", (e) => {
      const b = e.target.closest("[data-filter]");
      if (!b) return;
      state.filter = b.dataset.filter;
      $("voiceFilters").querySelectorAll(".filter").forEach((x) => {
        x.setAttribute("aria-pressed", String(x === b));
      });
      renderVoices();
    });
    $("newVoiceBtn").addEventListener("click", () => setScreen("clone"));
    $("roleChips").addEventListener("click", (e) => {
      const b = e.target.closest("[data-role]");
      if (b) selectRole(b.dataset.role);
    });
    $("newRoleBtn").addEventListener("click", () => openRoleDialog(null));
    $("saveRoleBtn").addEventListener("click", saveRole);
    $("roleGrid").addEventListener("click", async (e) => {
      const ed = e.target.closest("[data-roleedit]");
      if (ed) { openRoleDialog(ed.dataset.roleedit); return; }
      const dl = e.target.closest("[data-roledel]");
      if (dl) { await deleteRole(dl.dataset.roledel); return; }
      const card = e.target.closest("[data-rolecard]");
      if (card) {
        selectRole(card.dataset.rolecard);
        setScreen("workbench");
      }
    });

    // 参数
    $("emotionChips").addEventListener("click", (e) => {
      const b = e.target.closest("[data-emotion]");
      if (!b) return;
      state.emotion = b.dataset.emotion;
      $("emotionChips").querySelectorAll(".chip").forEach((x) => {
        x.setAttribute("aria-pressed", String(x === b));
      });
      previewNorm();
    });
    $("accentSelect").addEventListener("change", (e) => {
      state.accent = e.target.value;
      $("accentNote").textContent = state.accent
        ? (e.target.selectedOptions[0] ? e.target.selectedOptions[0].text : "自定义口音")
        : "默认标准普通话";
      previewNorm();
    });
    $("speedSeg").addEventListener("click", (e) => {
      const b = e.target.closest("[data-speed]");
      if (!b) return;
      state.speed = parseFloat(b.dataset.speed);
      $("speedSeg").querySelectorAll("button").forEach((x) => {
        x.setAttribute("aria-pressed", String(x === b));
      });
      $("speedNote").textContent = state.speed.toFixed(2) + "×";
      previewNorm();
    });
    $("advancedBtn").addEventListener("click", () => {
      const p = $("advancedPanel");
      p.hidden = !p.hidden;
      $("advancedBtn").setAttribute("aria-expanded", String(!p.hidden));
    });
    $("closeAdv").addEventListener("click", () => {
      $("advancedPanel").hidden = true;
      $("advancedBtn").setAttribute("aria-expanded", "false");
    });
    $("cfg").addEventListener("input", (e) => {
      state.cfg = parseFloat(e.target.value);
      $("cfgVal").textContent = state.cfg.toFixed(1);
    });
    $("seed").addEventListener("input", (e) => {
      state.seed = parseInt(e.target.value || "42", 10);
      $("seedVal").textContent = String(state.seed);
    });
    $("seg").addEventListener("input", (e) => {
      state.segLen = parseInt(e.target.value || "60", 10);
      $("segVal").textContent = state.segLen + " 字";
      updatePaper();
    });
    $("pause").addEventListener("input", (e) => {
      state.pause = parseFloat(e.target.value || "0.35");
      $("pauseVal").textContent = state.pause.toFixed(2) + " 秒";
    });
    $("resetAdv").addEventListener("click", () => {
      state.cfg = 4; state.seed = 42; state.segLen = 60; state.pause = 0.35;
      $("cfg").value = 4; $("cfgVal").textContent = "4.0";
      $("seed").value = 42; $("seedVal").textContent = "42";
      $("seg").value = 60; $("segVal").textContent = "60 字";
      $("pause").value = 0.35; $("pauseVal").textContent = "0.35 秒";
      updatePaper();
    });

    // 生成
    $("generateBtn").addEventListener("click", generate);
    $("retryBtn").addEventListener("click", generate);
    $("retryFailBtn").addEventListener("click", generate);
    $("regenBtn").addEventListener("click", generate);
    $("stopBtn").addEventListener("click", () => {
      if (state.abort) state.abort.abort();
      state.busy = false;
      setWorkbenchState("default");
      toast("已停止");
    });
    $("errorClose").addEventListener("click", () => { $("errorAlert").hidden = true; });
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        if (state.screen === "workbench") generate();
      }
      if (e.key === "Escape") {
        document.querySelectorAll("dialog[open]").forEach((d) => d.close());
      }
    });

    // 播放器
    $("playBtn").addEventListener("click", togglePlay);
    $("rewBtn").addEventListener("click", () => { audio.currentTime = Math.max(0, audio.currentTime - 15); });
    $("ffBtn").addEventListener("click", () => { audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 15); });
    $("playSpeed").addEventListener("click", () => {
      const steps = [1, 1.25, 1.5, 0.75];
      const i = (steps.indexOf(state.playerSpeed) + 1) % steps.length;
      state.playerSpeed = steps[i];
      audio.playbackRate = state.playerSpeed;
      $("playSpeed").textContent = state.playerSpeed.toFixed(2).replace(/0$/, "") + "×";
      $("playSpeed").setAttribute("aria-label", `播放速度 ${state.playerSpeed} 倍`);
    });
    $("downloadBtn").addEventListener("click", () => {
      if (!audio.src) { toast("还没有可下载的音频", true); return; }
      const a = document.createElement("a");
      a.href = audio.src;
      a.download = `breezetts_${new Date().toISOString().slice(0, 10)}.wav`;
      a.click();
    });

    // 历史
    $("historyScroll").addEventListener("click", async (e) => {
      const play = e.target.closest("[data-hplay]");
      if (play) { audio.src = play.dataset.hplay; audio.play(); return; }
      const del = e.target.closest("[data-hdel]");
      if (del) {
        try {
          await api("/api/history/" + del.dataset.hdel, { method: "DELETE" });
          await refreshHistory();
          toast("已删除");
        } catch (err) { toast(err.message, true); }
      }
    });
    $("historyAllBtn").addEventListener("click", () => setScreen("library"));

    // 词典
    $("dictManageBtn").addEventListener("click", async () => {
      await refreshDict();
      $("dictDlg").showModal();
    });
    $("dictForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const word = $("dictWord").value.trim();
      const reading = $("dictReading").value.trim();
      if (!word || !reading) return;
      try {
        await api("/api/dict", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ word, reading, scope: $("dictScope").value }),
        });
        $("dictWord").value = ""; $("dictReading").value = "";
        await refreshDict();
        previewNorm();
        toast("已添加：" + word);
      } catch (err) { toast(err.message, true); }
    });
    $("dictList").addEventListener("click", async (e) => {
      const del = e.target.closest("[data-dictdel]");
      if (!del) return;
      try {
        await api("/api/dict/" + encodeURIComponent(del.dataset.dictdel) +
          "?scope=" + encodeURIComponent(del.dataset.dictscope), { method: "DELETE" });
        await refreshDict();
        previewNorm();
      } catch (err) { toast(err.message, true); }
    });

    // 克隆
    $("refPickBtn").addEventListener("click", () => $("refFile").click());
    $("refReplaceBtn").addEventListener("click", () => $("refFile").click());
    $("refFile").addEventListener("change", (e) => {
      const f = e.target.files && e.target.files[0];
      if (f) uploadRef(f);
      e.target.value = "";
    });
    $("refUpload").addEventListener("dragover", (e) => { e.preventDefault(); });
    $("refUpload").addEventListener("drop", (e) => {
      e.preventDefault();
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) uploadRef(f);
    });
    $("refText").addEventListener("input", refreshCloneState);
    $("voiceName").addEventListener("input", refreshCloneState);
    $("voiceInstruction").addEventListener("input", refreshCloneState);
    $("consentBox").addEventListener("change", refreshCloneState);
    $("cloneTypeChips").addEventListener("click", (e) => {
      const b = e.target.closest("[data-vtype]");
      if (!b) return;
      state.clone.type = b.dataset.vtype;
      $("voiceInstruction").placeholder = state.clone.type === "design"
        ? "例如：一位温柔自信的年轻女性，声音清晰"
        : "例如：克隆自己的声音，但说话更慢、更温和";
      $("cloneTypeChips").querySelectorAll(".chip").forEach((x) => {
        x.setAttribute("aria-pressed", String(x === b));
      });
      refreshCloneState();
    });
    $("saveVoiceBtn").addEventListener("click", saveCloneVoice);
    $("refPlayBtn").addEventListener("click", () => {
      if (!state.clone.audioUrl) return;
      audio.src = state.clone.audioUrl; audio.play();
    });
    $("abPlayBtn").addEventListener("click", () => {
      if (!state.clone.audioUrl) return;
      audio.src = state.clone.audioUrl; audio.play();
    });

    // 音色库
    $("libraryGrid").addEventListener("click", (e) => {
      const edit = e.target.closest("[data-edit]");
      if (edit) { openEdit(edit.dataset.edit); return; }
      const card = e.target.closest("[data-voice]");
      if (card) {
        state.voiceId = card.dataset.voice;
        renderVoices();
        setScreen("workbench");
      }
    });
    $("libraryHistory").addEventListener("click", async (e) => {
      const play = e.target.closest("[data-hplay]");
      if (play) { audio.src = play.dataset.hplay; audio.play(); return; }
      const del = e.target.closest("[data-hdel]");
      if (del) {
        try {
          await api("/api/history/" + del.dataset.hdel, { method: "DELETE" });
          await refreshHistory();
        } catch (err) { toast(err.message, true); }
      }
    });
    $("libSearch").addEventListener("input", () => {
      state.libVoicePage = 0; state.libHistPage = 0; renderLibrary();
    });
    $("libraryGrid").addEventListener("click", (e) => {
      const pg = e.target.closest("[data-libvoice]");
      if (!pg) return;
      const n = parseInt(pg.dataset.libvoice, 10);
      if (n < 0) return;
      state.libVoicePage = n;
      renderLibrary();
    });
    $("roleGrid").addEventListener("click", (e) => {
      const pg = e.target.closest("[data-librole]");
      if (pg) {
        const n = parseInt(pg.dataset.librole, 10);
        if (n >= 0) { state.libRolePage = n; renderLibrary(); }
        return;
      }
    });
    $("libraryHistory").addEventListener("click", (e) => {
      const pg = e.target.closest("[data-libhist]");
      if (!pg) return;
      const n = parseInt(pg.dataset.libhist, 10);
      if (n < 0) return;
      state.libHistPage = n;
      renderLibrary();
    });

    // 弹层
    document.querySelectorAll("[data-close]").forEach((b) => {
      b.addEventListener("click", () => {
        const d = $(b.dataset.close);
        if (d) d.close();
      });
    });
    $("helpBtn").addEventListener("click", () => $("helpDlg").showModal());
    $("settingsBtn").addEventListener("click", async () => {
      await refreshModel();
      const m = state.model;
      $("diagBody").innerHTML = m
        ? `<div class="diag-row"><span>推理服务</span><b>7860 · 已连接</b></div>
           <div class="diag-row"><span>模型摘要</span><code>${esc((m.model_digest || "").slice(0, 16))}…</code></div>
           <div class="diag-row"><span>帧率</span><b>${esc(m.frame_rate)} fps</b></div>
           <div class="diag-row"><span>单次上限</span><b>${esc(m.max_new_tokens)} tokens</b></div>
           <div class="diag-row"><span>批量上限</span><b>${esc(m.max_batch_texts)} 条</b></div>
           <div class="diag-row"><span>面板端口</span><b>7861</b></div>
           <p class="help-note">这些是排查用的技术信息。数据目录：<code>data/</code></p>`
        : `<p class="help-note">推理服务（7860）当前未连接。请先启动推理服务，面板才能合成。</p>`;
      $("settingsDlg").showModal();
    });
    $("saveEditBtn").addEventListener("click", saveEdit);
  }

  async function openEdit(id) {
    const v = state.voices.find((x) => x.id === id);
    if (!v) return;
    state.editId = id;
    $("editName").value = v.name || "";
    $("editInstruction").value = v.instruction || "";
    $("editRefText").value = v.ref_text || "";
    $("editDlg").showModal();
  }

  async function saveEdit() {
    try {
      await api("/api/voices/" + state.editId, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: $("editName").value.trim(),
          instruction: $("editInstruction").value.trim(),
          ref_text: $("editRefText").value.trim(),
        }),
      });
      $("editDlg").close();
      await refreshVoices();
      syncVoiceReadiness();
      toast("已保存");
    } catch (err) { toast(err.message, true); }
  }

  async function deleteVoice(id) {
    const v = state.voices.find((x) => x.id === id);
    if (!v) return;
    if (!confirm(`删除音色「${v.name}」？\n参考音频与文稿会一起删除，历史记录会保留但无法用它重新生成。`)) return;
    try {
      await api("/api/voices/" + id, { method: "DELETE" });
      await refreshVoices();
      toast("已删除");
    } catch (err) { toast(err.message, true); }
  }

  // ---------------------------------------------------------------- 启动
  async function boot() {
    bind();
    try {
      const draft = localStorage.getItem("breeze_draft") || "";
      if (draft) $("synthText").value = draft;
    } catch (e) { /* 忽略 */ }
    updatePaper();
    setWorkbenchState("empty");
    await Promise.all([refreshVoices(), refreshRoles(), refreshHistory(), refreshModel()]);
    await refreshDict();
    setInterval(refreshModel, 15000);
    // 深链：?screen=clone|library
    try {
      const s = new URLSearchParams(location.search).get("screen");
      if (s === "clone" || s === "library" || s === "workbench") setScreen(s);
    } catch (e) { /* 忽略 */ }
    // 波形装饰
    document.querySelectorAll("[data-wave]").forEach((el) => {
      const n = parseInt(el.dataset.wave || "20", 10);
      const seed = parseInt(el.dataset.seed || "1", 10);
      el.innerHTML = Array.from({ length: n }, (_, i) =>
        `<span style="height:${18 + ((i * 37 + seed * 13) % 64)}%"></span>`).join("");
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
