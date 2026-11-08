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
  const LIB_HIST_PAGE = 4;    // 每页历史条数（视口不够高时自动减半，见 histPageSize）
  const LIB_ROLE_PAGE = 4;    // 每页角色卡片数
  // 历史区是 flex:1 的内部滚动容器；900 高的屏放不下 4 行 + 分页条，
  // 每页少放两行，避免「最后一行和分页按钮被裁在视口外」。
  const histPageSize = () => (window.innerHeight < 960 ? 2 : LIB_HIST_PAGE);

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
    if (name === "library") {
      // 角色可能刚被「保存即自动配角」改过（新剧本会建专属角色），
      // 进库先拉一次最新，否则角色卡上缺刚建的那几个
      refreshRoles().then(renderLibrary);
    }
    if (name === "clone") refreshCloneState();
    if (name === "script") {
      refreshScripts();
      // 素材还没加载过就先拉一次：场次铺底的下拉要显示素材名，不能只有「不铺」
      if (!S.assets.length) refreshAssets().then(renderSceneMoods);
    }
    // 切走就别再轮询：后台还在打接口、改的是隐藏 DOM，用户也看不到。
    // 同时要放开「出片中」闸门：轮询已经停了，标志还留着的话，切回来
    // 点「一键出片」会被 S.producing 直接挡掉，看起来像按钮坏了。
    if (name !== "script") {
      if (S.timer) { clearTimeout(S.timer); S.timer = null; }
      if (S.producing) { S.producing = false; finishJob(); }
    }
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
    $("paperSaved").textContent = chars ? "草稿已保存在本机" : "还没有内容";

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
    // 工作台不隶属任何剧本，这里只列全局「人设」；剧本专属角色在各剧本的
    // cast 里自动生效（去角色库看），不然几个剧本下来左栏会被同名角色淹没
    const chipRoles = state.roles.filter((r) => !r.script_id);
    // 这个计数就挂在工作台左栏「角色库」分组上，跟着 chips 走；
    // 用全量数字会出现「说 12 个却只列出 6 个」
    $("roleCount").textContent = `${chipRoles.length} 个`;
    if (!chipRoles.length) {
      chips.hidden = true; label.hidden = true;
    } else {
      label.hidden = false; chips.hidden = false;
      chips.innerHTML = chipRoles.map((r) => `
        <button class="role-chip${state.roleId === r.id ? " active" : ""}" type="button"
                data-role="${esc(r.id)}" aria-pressed="${state.roleId === r.id}"
                title="${esc(r.description || roleVoiceName(r.voice_id))}">
          ${esc(r.name)}
        </button>`).join("");
    }

    const grid = $("roleGrid");
    // 搜索框管的是整个音色库：角色卡也要跟着过滤，否则输入后只有音色变，
    // 用户会以为「全库可搜」而实际找不到第 20 个角色。
    const q = (($("libSearch") && $("libSearch").value) || "").trim().toLowerCase();
    const roles = q ? state.roles.filter((r) =>
      ((r.name || "") + (r.description || "") + roleVoiceName(r.voice_id))
        .toLowerCase().includes(q)) : state.roles;
    $("roleEmpty").hidden = roles.length > 0;
    // 数据被删或视口变化后页数会变小；页码必须收敛，否则出现
    // 「第 66 / 33 页」这种空白页，且分页栏可能整个消失无法回退。
    const rTotal = Math.max(1, Math.ceil(roles.length / LIB_ROLE_PAGE));
    state.libRolePage = Math.min(state.libRolePage, rTotal - 1);
    const rShown = roles.slice(state.libRolePage * LIB_ROLE_PAGE,
                               (state.libRolePage + 1) * LIB_ROLE_PAGE);
    grid.innerHTML = rShown.map((r) => {
      const arch = S.archetypes.find((a) => a.key === (r.archetype || "custom"));
      const spd = Number(r.speed || 1);
      return `
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
        <div class="role-meta">
          <span>${esc((arch && arch.label) || "自定义")}</span>
          ${r.script_id
            ? `<span class="role-scope" title="只属于《${esc(r.script_title || "已删剧本")}》：改它不影响别的剧本">${esc(String(r.script_title || "已删剧本").slice(0, 8))}${String(r.script_title || "").length > 8 ? "…" : ""}</span>`
            : ""}
          ${r.default_emotion ? `<span>默认 ${esc(r.default_emotion)}</span>` : ""}
          ${spd !== 1 ? `<span>语速 ×${spd.toFixed(2).replace(/0$/, "")}</span>` : ""}
          ${r.accent ? `<span>${esc(String(r.accent).slice(0, 12))}</span>` : ""}
        </div>
        <p class="card-desc">${esc(r.description || "（没有简介）")}</p>
      </article>`;
    }).join("");
    const rPages = rTotal;
    if (rPages > 1) {
      const far = rPages > 3;
      const first = far ? `<button type="button" class="btn btn-sm" data-librole="0" ${state.libRolePage === 0 ? "disabled" : ""}>« 首页</button>` : "";
      const last = far ? `<button type="button" class="btn btn-sm" data-librole="${rPages - 1}" ${state.libRolePage >= rPages - 1 ? "disabled" : ""}>末页 »</button>` : "";
      grid.insertAdjacentHTML("beforeend",
        `<div class="lib-pager">
           ${first}
           <button type="button" class="btn btn-sm" data-librole="${state.libRolePage - 1}" ${state.libRolePage === 0 ? "disabled" : ""}>上一页</button>
           <span>第 ${state.libRolePage + 1} / ${rPages} 页 · 共 ${roles.length} 个角色${q ? "（已筛选）" : ""}</span>
           <button type="button" class="btn btn-sm" data-librole="${state.libRolePage + 1}" ${state.libRolePage >= rPages - 1 ? "disabled" : ""}>下一页</button>
           ${last}
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
    $("roleArchetype").value = (r && r.archetype) || "custom";
    $("roleEmotion").value = (r && r.default_emotion) || "";
    $("roleSpeed").value = r && r.speed ? Number(r.speed).toFixed(2).replace(/0$/, "") : "1";
    // 口音来自固定下拉；老数据里可能有自定义值，补一个选项，
    // 否则下拉显示为空、一保存就把原来的口音冲掉。
    const accSel = $("roleAccent");
    const acc = (r && r.accent) || "";
    accSel.querySelectorAll("option[data-custom]").forEach((o) => o.remove());
    if (acc && !Array.from(accSel.options).some((o) => o.value === acc)) {
      const o = document.createElement("option");
      o.value = acc;
      o.textContent = acc;
      o.dataset.custom = "1";
      accSel.appendChild(o);
    }
    accSel.value = acc;
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
      archetype: $("roleArchetype").value || "custom",
      default_emotion: $("roleEmotion").value || "",
      speed: parseFloat($("roleSpeed").value) || 1,
      accent: $("roleAccent").value || "",
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
    // 专属角色在剧本里是有归属的：说清删掉之后会发生什么，别让人以为
    // 「角色没了剧本就报错」或者「剧本会静默换一个音色」都无所谓
    const scope = r.script_title
      ? `\n它只属于《${r.script_title}》；删除后若角色库里有同名角色，那个剧本会改用它，否则那一行会变成「未配」`
      : "";
    if (!confirm(`删除角色「${r.name}」？${scope}`)) return;
    try {
      const res = await api("/api/roles/" + id, { method: "DELETE" });
      if (state.roleId === id) state.roleId = null;
      await refreshRoles();
      if (res && res.warning) { toast(res.warning, true); return; }
      const cleaned = Number((res && res.cleaned) || 0);
      toast(cleaned ? `已删除，${cleaned} 个剧本的角色表已同步清理` : "已删除");
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
    const vPagesAll = Math.max(1, Math.ceil(list.length / LIB_VOICE_PAGE));
    state.libVoicePage = Math.min(state.libVoicePage, vPagesAll - 1);
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
    const vPages = vPagesAll;
    if (vPages > 1) {
      const far = vPages > 3;
      const first = far ? `<button type="button" class="btn btn-sm" data-libvoice="0" ${state.libVoicePage === 0 ? "disabled" : ""}>« 首页</button>` : "";
      const last = far ? `<button type="button" class="btn btn-sm" data-libvoice="${vPages - 1}" ${state.libVoicePage >= vPages - 1 ? "disabled" : ""}>末页 »</button>` : "";
      grid.insertAdjacentHTML("beforeend",
        `<div class="lib-pager">
           ${first}
           <button type="button" class="btn btn-sm" data-libvoice="${state.libVoicePage - 1}" ${state.libVoicePage === 0 ? "disabled" : ""}>上一页</button>
           <span>第 ${state.libVoicePage + 1} / ${vPages} 页 · 共 ${list.length} 个音色</span>
           <button type="button" class="btn btn-sm" data-libvoice="${state.libVoicePage + 1}" ${state.libVoicePage >= vPages - 1 ? "disabled" : ""}>下一页</button>
           ${last}
         </div>`);
    }

    const histAll = state.history.slice().reverse();
    // 历史 131 条、每页 4 条 = 34 页。搜索必须真的过滤历史，否则用户
    // 找一条旧配音要点 30 多次「下一页」，搜索框只是个错觉。
    const hist = q ? histAll.filter((h) =>
      ((h.text || "") + voiceName(h.voice_id) + (h.created_at || ""))
        .toLowerCase().includes(q)) : histAll;
    $("libHistoryCount").textContent = q
      ? `${hist.length} / ${histAll.length} 条` : `${hist.length} 条`;
    const hPageSize = histPageSize();
    const hPagesAll = Math.max(1, Math.ceil(hist.length / hPageSize));
    state.libHistPage = Math.min(state.libHistPage, hPagesAll - 1);
    const hStart = state.libHistPage * hPageSize;
    const shown = hist.slice(hStart, hStart + hPageSize);
    const hPages = hPagesAll;
    $("libraryHistory").innerHTML = hist.length
      ? shown.map((h) => `
        <div class="lib-hist-row">
          <span class="hist-main">
            <span class="hist-title" title="${esc(h.text || "")}">${esc((h.text || "").replace(/\s+/g, " ").slice(0, 40) || "（无文本）")}</span>
            <span class="hist-sub">${esc(voiceName(h.voice_id))} · ${fmtTime(h.duration || 0)} · ${esc(h.created_at || "")}</span>
          </span>
          <button class="hist-play" type="button" data-hplay="${esc(h.wav_url || "")}" aria-label="播放"><svg class="icon"><use href="#i-play"/></svg></button>
          <button class="icon-btn del" type="button" data-hdel="${esc(h.id)}" aria-label="删除"><svg class="icon icon-xs"><use href="#i-trash"/></svg></button>
        </div>`).join("")
      : '<div class="empty-hint">还没有合成记录。</div>';
    if (hPages > 1) {
      const far = hPages > 3;
      const first = far ? `<button type="button" class="btn btn-sm" data-libhist="0" ${state.libHistPage === 0 ? "disabled" : ""}>« 首页</button>` : "";
      const last = far ? `<button type="button" class="btn btn-sm" data-libhist="${hPages - 1}" ${state.libHistPage >= hPages - 1 ? "disabled" : ""}>末页 »</button>` : "";
      $("libraryHistory").insertAdjacentHTML("beforeend",
        `<div class="lib-pager">
           ${first}
           <button type="button" class="btn btn-sm" data-libhist="${state.libHistPage - 1}" ${state.libHistPage === 0 ? "disabled" : ""}>上一页</button>
           <span>第 ${state.libHistPage + 1} / ${hPages} 页 · 共 ${hist.length} 条${q ? "（已筛选）" : ""}</span>
           <button type="button" class="btn btn-sm" data-libhist="${state.libHistPage + 1}" ${state.libHistPage >= hPages - 1 ? "disabled" : ""}>下一页</button>
           ${last}
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
    try {
      state.roles = await api("/api/roles");
    } catch (e) {
      // 拉取失败保留上一次的列表：清空会让工作台和角色库突然
      // 变成「一个角色都没有」，用户会以为角色被删了
    }
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
    $("roleArchetype").addEventListener("change", prefillArchetypeEmotion);
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
      // 音色条目外层是 <button>，里面的编辑/删除不能用嵌套 button，
      // 只能是 role="button" 的 span：键盘聚焦得到、按 Enter/Space 也得能用。
      const kb = e.target && e.target.closest
        ? e.target.closest('[role="button"][tabindex="0"]') : null;
      if (kb && !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey && !e.repeat
          && (e.key === "Enter" || e.key === " " || e.key === "Spacebar")) {
        e.preventDefault();
        kb.click();
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
      state.libVoicePage = 0; state.libHistPage = 0; state.libRolePage = 0;
      renderLibrary(); renderRoles();
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
        // 原来调的是 renderLibrary()，它根本不画角色卡 → 点「下一页」页码
        // 会变、卡片却不动。必须重渲染角色区本身。
        if (n >= 0) { state.libRolePage = n; renderRoles(); }
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
    bindScript();
    await loadMeta();
    setInterval(refreshModel, 15000);
    // 深链：?screen=clone|library
    try {
      const s = new URLSearchParams(location.search).get("screen");
      if (s === "clone" || s === "library" || s === "workbench" || s === "script") setScreen(s);
    } catch (e) { /* 忽略 */ }
    // 波形装饰
    document.querySelectorAll("[data-wave]").forEach((el) => {
      const n = parseInt(el.dataset.wave || "20", 10);
      const seed = parseInt(el.dataset.seed || "1", 10);
      el.innerHTML = Array.from({ length: n }, (_, i) =>
        `<span style="height:${18 + ((i * 37 + seed * 13) % 64)}%"></span>`).join("");
    });
  }


  // ============================================================ 剧本台
  const S = {
    list: [], current: null, lines: [], job: null, timer: null,
    gap: 0.35, archetypes: [], emotions: [], moods: [], scenes: [], assets: [],
    missingRoles: [], lineFilter: "all", lineRole: "", jumpIdx: 0,
    lastRender: null, producing: false, produceParts: [],
  };

  // 后端存的是相对 data/ 的路径（audio/scripts/<id>/full.wav），
  // 音频路由挂在 /api/audio/ 下且支持子路径。
  const audioUrl = (rel) => (rel ? "/api/audio/" + String(rel).replace(/^audio\//, "") : "");

  const EMOTION_SOURCE = {
    explicit: "标注", role: "角色默认", auto: "自动推断", neutral: "中性",
  };

  function roleHue(name) {
    let h = 0;
    for (const ch of String(name || "")) h = (h * 31 + ch.charCodeAt(0)) % 360;
    return h;
  }

  // 只刷新左栏列表；传 id 才顺便打开。注意不要默认重开当前剧本——
  // 那会用服务端数据覆盖用户刚输入还没保存的内容。
  async function refreshScripts(selectId) {
    try {
      S.list = await api("/api/scripts");
    } catch (e) {
      S.list = [];
    }
    renderScriptList();
    if (selectId && S.list.some((s) => s.id === selectId)) await openScript(selectId);
  }

  function renderScriptList() {
    const box = $("scriptList");
    $("scriptCount").textContent = `${S.list.length} 个`;
    if (!S.list.length) {
      box.innerHTML = '<div class="empty-hint">还没有剧本<br>点右上角「新建剧本」开始</div>';
      return;
    }
    box.innerHTML = S.list.map((s) => `
      <button type="button" class="script-item${S.current && S.current.id === s.id ? " active" : ""}"
              data-script="${esc(s.id)}">
        <strong>${esc(s.title || "未命名剧本")}</strong>
        <span>${s.line_count || 0} 行 · ${s.duration ? s.duration.toFixed(1) + " 秒" : "未合成"}</span>
      </button>`).join("");
  }

  async function openScript(id) {
    let d;
    try {
      d = await api("/api/scripts/" + id);
    } catch (e) {
      toast(e.message, true);
      return;
    }
    S.current = d;
    S.lines = d.lines || [];
    S.scenes = d.scenes || [];
    // 换剧本时清掉上一部的筛选条件，别让新剧本看起来「少了几行」
    S.lineFilter = "all"; S.lineRole = ""; S.jumpIdx = 0;
    S.gap = d.gap == null ? 0.35 : d.gap;
    $("scriptRaw").value = d.raw || "";
    $("scriptGap").value = S.gap;
    $("scriptTitleInput").value = d.title || "";
    $("scriptTitle").textContent = d.title || "未命名剧本";
    $("synthScriptBtn").disabled = false;
    $("produceScriptBtn").disabled = false;
    $("exportScriptBtn").disabled = false;
    renderLines(d);
    renderSceneMoods();
    renderScriptList();
    const full = audioUrl(d.full_wav);
    const audio = $("scriptAudio");
    if (full) { audio.src = full; audio.hidden = false; } else { audio.hidden = true; audio.removeAttribute("src"); }
    // 打开/重载剧本一律回到干声源，别让上一部剧的「试听混音」状态留在播放器上
    audio.dataset.src = "full";
    $("playMixBtn").textContent = "试听混音";
  }

  function renderLines(d) {
    const box = $("scriptLines");
    const lines = (d && d.lines) || S.lines || [];
    // 筛选/跳转会重绘，记住最近一次解析结果，别为了改个下拉再发一次请求。
    // 只有真拿到数据才覆盖缓存：否则一次进度刷新就会把场次/报错从缓存里抹掉。
    if (d) S.lastRender = d;
    $("lineCount").textContent = `${lines.length} 行`;
    $("scriptMeta").textContent = lines.length
      ? `${lines.length} 行 · 约 ${Math.round((d && d.estimate) || 0)} 秒${S.current && S.current.duration ? " · 已合成 " + S.current.duration.toFixed(1) + " 秒" : ""}`
      : "还没有台词";

    if (!lines.length) {
      // 空剧本也要清掉上一次的「缺角色」缓存，否则后面合成会被过期值拦住
      S.missingRoles = [];
      box.innerHTML = '<div class="empty-hint">解析后会在这里逐行显示：谁说的、什么情绪、念到第几句。</div>';
      return;
    }
    const errs = (d && d.errors) || [];
    // 保存时后端已自动配角，这块通常不会出现；留作手动兜底入口
    const missing = [...new Set(errs.filter((e) => e.role_name)
      .map((e) => String(e.role_name).trim()).filter(Boolean))];
    const other = errs.filter((e) => !e.role_name);
    const castBox = missing.length
      ? `<div class="script-cast">
           <div class="script-cast-title">还有 ${missing.length} 个角色没配好</div>
           <div class="script-cast-names">${missing.map((n) => `<span>${esc(n)}</span>`).join("")}</div>
           <button class="btn-primary btn-sm" type="button" id="castBtn">立即补齐角色与音色</button>
           <p>保存剧本时会自动配好；如果这里是空的，点一下即可补上。按名字判断老人 / 孩子 / 旁白，各配一个声音。</p>
         </div>`
      : "";
    const errBox = other.length
      ? `<div class="script-errors">${other.slice(0, 4).map((e) =>
          `<div>第 ${e.line} 行：${esc(e.reason)}</div>`).join("")}</div>`
      : "";
    S.missingRoles = missing;
    const synthBtn = $("synthScriptBtn");
    if (synthBtn) synthBtn.title = missing.length
      ? `还有 ${missing.length} 个角色没配好，先点上方「立即补齐」`
      : "按当前角色逐行合成并拼成整轨";

    // 100 行剧本平铺会找不到人：按状态/角色筛选 + 场次分组 + 跳到第 N 句
    const roleNames = [...new Set(lines.map((l) => l.role_name))];
    const statusOf = (l) => l.status === "done" ? "done"
      : l.status === "failed" ? "failed" : "pending";
    const shown = lines.filter((l) => {
      if (S.lineFilter !== "all" && statusOf(l) !== S.lineFilter) return false;
      if (S.lineRole && l.role_name !== S.lineRole) return false;
      return true;
    });
    const opt = (v, label, cur) => `<option value="${esc(v)}"${cur === v ? " selected" : ""}>${esc(label)}</option>`;
    const filterBar = `<div class="line-filters">
        <select class="input input-select" id="lineFilterSel" aria-label="按状态筛选">
          ${opt("all", "全部状态", S.lineFilter)}${opt("pending", "待合成", S.lineFilter)}
          ${opt("failed", "失败", S.lineFilter)}${opt("done", "已完成", S.lineFilter)}
        </select>
        <select class="input input-select" id="lineRoleSel" aria-label="按角色筛选">
          ${opt("", "全部角色", S.lineRole || "")}
          ${roleNames.map((n) => opt(n, n, S.lineRole || "")).join("")}
        </select>
        <label class="line-jump">第 <input class="input" id="lineJump" type="number" min="1" max="${lines.length}" value="${S.jumpIdx || ""}" placeholder="—"> 句</label>
        <span class="line-filter-count">${shown.length} / ${lines.length} 行</span>
      </div>`;

    let lastScene = null;
    const body = shown.map((l) => {
      const hue = roleHue(l.role_name);
      const stat = statusOf(l);
      const mark = stat === "done" ? "✓" : stat === "failed" ? "✕" : "";
      const sceneHead = l.scene !== lastScene
        ? `<div class="line-scene">${esc(l.scene || "未分场")}</div>` : "";
      lastScene = l.scene;
      // Qwen 的克隆路线忽略 instruction：把「看起来智能化、实际没生效」
      // 直接标在台词旁，而不是让用户对着情绪标签以为生效了
      const routeMark = l.instruction_effective === false
        ? `<em class="line-route" title="这个角色用的是克隆音色，引擎会忽略情绪 / 氛围 / 口音指令；角色语速仍生效。想让情绪生效，请把角色改成设计型音色">克隆音色 · 情绪指令不生效</em>`
        : "";
      // 多版本：这一行重录过几次就列几版，选中的那版进整轨与导出
      const takes = l.takes || [];
      const takeSel = takes.length > 1
        ? `<select class="line-take" data-takeline="${l.idx}" title="这一行有 ${takes.length} 个版本，选一个当成品">
             ${takes.map((t) => `<option value="${t.n}"${t.n === l.take ? " selected" : ""}>v${t.n}</option>`).join("")}
           </select>`
        : "";
      return `${sceneHead}
        <div class="script-line ${stat}" data-line-idx="${l.idx}"
             title="${esc(l.instruction ? "下发的指令：" + l.instruction : "没有额外指令（用默认语气）")}">
          <span class="line-no">${String(l.idx + 1).padStart(2, "0")}</span>
          <span class="line-role" style="--h:${hue}">${esc(l.role_name)}</span>
          <span class="line-emotion" title="情绪来源：${esc(EMOTION_SOURCE[l.emotion_source] || "")}">
            ${esc(l.emotion)}<em>${esc(EMOTION_SOURCE[l.emotion_source] || "")}</em>
          </span>
          <span class="line-text">${esc(l.text)}${routeMark}</span>
          ${takeSel}
          <span class="line-state ${stat}">${mark}</span>
          <span class="line-acts">
            <button class="icon-btn" type="button" data-playline="${l.idx}" title="试听这一行" ${l.wav ? "" : "disabled"}>
              <svg class="icon icon-sm"><use href="#i-play"/></svg>
            </button>
            <button class="icon-btn" type="button" data-redoline="${l.idx}" title="${takes.length ? `再录一版（当前 v${l.take}）` : "只重做这一行"}">
              <svg class="icon icon-sm"><use href="#i-refresh"/></svg>
            </button>
          </span>
        </div>`;
    }).join("");

    box.innerHTML = castBox + errBox + filterBar + body;
    if (S.jumpIdx) {
      const target = box.querySelector(`[data-line-idx="${S.jumpIdx - 1}"]`);
      if (target) {
        target.classList.add("is-target");
        target.scrollIntoView({ block: "center" });
      }
    }

    const bad = errs.length;
    $("scriptHint").textContent = missing.length
      ? `${missing.length} 个角色还没配好，点上方「立即补齐」就能开合成`
      : bad ? `有 ${bad} 处需要修一下（见下方分镜顶部）`
      : "用 ## 开头的行表示场次；[情绪] 或（情绪）标注语气";
    $("scriptHint").classList.toggle("is-warn", bad > 0);
  }

  // 自动推断的氛围不能因为界面把 scenes 整组回传就被固化成「作者选的」：
  // 发送时把 auto 项的 mood 清空，后端会按场次名重新推断（结果一致）。
  function scenesPayload() {
    // 铺底字段照发：后端按「有没有这个键」判断，只传 name/mood 时它会保留
    // 旧值——但如果界面刚把 BGM 清成「不铺」，不传就等于清不掉。
    return (S.scenes || []).map((s) => ({
      name: s.name, mood: s.auto ? "" : (s.mood || ""),
      ambience: s.ambience || "", bgm: s.bgm || "",
      gain: s.gain == null ? 0.35 : s.gain,
    }));
  }

  // ---------------------------------------------------------------- 素材库
  // 环境声 / BGM 都来自这里：既可以是程序生成的噪声底（无第三方版权），
  // 也可以是用户自己上传的音频（上传时必须勾授权，版权自负）。
  const ASSET_KIND = {
    white: "白噪声", brown: "棕噪声", wind: "风声", hum: "低频嗡鸣", upload: "上传",
  };

  function assetName(a) {
    const kind = ASSET_KIND[a.kind] || a.kind || "素材";
    const dur = a.duration ? `${Number(a.duration).toFixed(1)}s` : "";
    return `${a.name || "未命名素材"}（${kind}${dur ? " · " + dur : ""}）`;
  }

  async function refreshAssets() {
    try {
      S.assets = await api("/api/assets");
    } catch (e) {
      // 拉取失败就保留上一次的列表：清空会让所有已配好的铺底变成
      // 「（素材已删除）」，看着像配置丢了
    }
    return S.assets;
  }

  function renderAssets() {
    const box = $("assetList");
    if (!S.assets.length) {
      box.innerHTML = '<div class="empty-hint">还没有素材。生成一段噪声底，或上传自己的环境声。</div>';
      return;
    }
    box.innerHTML = S.assets.map((a) => `
      <div class="asset-item">
        <span class="asset-kind">${esc(ASSET_KIND[a.kind] || "素材")}</span>
        <strong class="asset-name" title="${esc(a.name || "")}">${esc(a.name || "未命名素材")}</strong>
        <span class="asset-dur">${a.duration ? Number(a.duration).toFixed(1) + " 秒" : "—"}</span>
        <span class="asset-src">${a.source === "generated" ? "生成" : "上传"}</span>
        <button class="icon-btn" type="button" data-delasset="${esc(a.id)}" title="删除这个素材">
          <svg class="icon icon-sm"><use href="#i-x"/></svg>
        </button>
      </div>`).join("");
  }

  async function openAssetsDialog() {
    const dlg = $("assetsDlg");
    if (dlg.open) return;          // 双击按钮会第二次 showModal 抛 InvalidStateError
    await refreshAssets();
    renderAssets();
    dlg.showModal();
  }

  async function generateAsset() {
    const btn = $("assetGenBtn");
    const seconds = Math.min(600, Math.max(1, parseFloat($("assetSeconds").value) || 30));
    btn.disabled = true;
    btn.textContent = "生成中…";
    try {
      const d = await api("/api/assets/generate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: $("assetKindSel").value,
          seconds,
          name: $("assetGenName").value.trim(),
        }),
      });
      $("assetGenName").value = "";
      await refreshAssets();
      renderAssets();
      // 新素材要能立刻在下面的场次下拉里选到，否则用户会以为没生成成功
      renderSceneMoods();
      toast(`已生成「${d.asset ? d.asset.name : "素材"}」，回场次里选它就能铺底`);
    } catch (e) {
      toast("生成失败：" + e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "生成";
    }
  }

  async function uploadAsset() {
    const input = $("assetFile");
    const file = input.files && input.files[0];
    if (!file) { toast("先选一个音频文件", true); return; }
    // 前端先拦一道：让用户看到「缺的是什么」，而不是等 400 回来再猜
    if (!$("assetLicense").checked) {
      toast("请先确认你拥有该素材的使用授权", true);
      return;
    }
    const btn = $("assetUploadBtn");
    const fd = new FormData();
    fd.append("file", file);
    fd.append("name", $("assetUpName").value.trim());
    fd.append("license_confirmed", "1");
    btn.disabled = true;
    btn.textContent = "上传中…";
    try {
      await api("/api/assets", { method: "POST", body: fd });
      input.value = "";
      $("assetUpName").value = "";
      await refreshAssets();
      renderAssets();
      renderSceneMoods();
      toast("素材已存入素材库");
    } catch (e) {
      toast("上传失败：" + e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "上传";
    }
  }

  async function deleteAsset(id) {
    const a = S.assets.find((x) => x.id === id);
    if (!a) return;
    if (!confirm(`删除素材「${a.name || a.id}」？\n当前剧本里用到它的场次会变回「不铺」；别的剧本引用了同一素材的，需要各自重新配一下。`)) return;
    try {
      await api("/api/assets/" + encodeURIComponent(id), { method: "DELETE" });
      await refreshAssets();
      renderAssets();
      // 场次里指向已删素材的引用要一起清掉：留着一个空 id 会让用户
      // 看着「已铺」但实际没声音
      let touched = false;
      (S.scenes || []).forEach((s) => {
        if (s.ambience === id) { s.ambience = ""; touched = true; }
        if (s.bgm === id) { s.bgm = ""; touched = true; }
      });
      renderSceneMoods();
      if (touched && S.current) {
        if (await saveScript(true)) await remixScript(true);
      }
      toast("已删除素材");
    } catch (e) {
      toast("删除失败：" + e.message, true);
    }
  }

  // ---------------------------------------------------------------- 混音
  // 铺底是拼接之后的后处理：只重算混音，不推理、不占显存。
  function renderMixState() {
    const hasMix = !!(S.current && S.current.mix_wav);
    const play = $("playMixBtn");
    if (play) play.hidden = !hasMix;
    const remix = $("remixBtn");
    if (remix) remix.disabled = !(S.current && S.current.full_wav);
  }

  // 混音串行化：连续改两个场次会并发 POST /mix，两次各自读「开始那一刻」的
  // 场景快照，后落盘的可能是旧快照，却把 mix_stale 标成最新——导出就拿到
  // 与配置不符的成片。和 saveScript 用同一套排队思路。
  let _remixChain = Promise.resolve();

  function remixScript(quiet) {
    const run = async () => {
      if (!S.current) return false;
      if (!S.current.full_wav) {
        if (!quiet) toast("还没有整轨音频，先点「合成全剧」或「一键出片」", true);
        return false;
      }
      try {
        const r = await api(`/api/scripts/${S.current.id}/mix`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
        });
        // 后端在变更铺底时已把旧 mix.wav 作废，这里拿到的是本次结果
        S.current.mix_wav = r.mix_wav || "";
        S.current.mix_stale = false;
        renderMixState();
        if (!quiet) {
          toast(r.mix_wav ? "混音已更新，干声整轨没有改动"
                          : "这个剧本没有可铺的环境声 / BGM，未生成混音");
        }
        return !!r.mix_wav;
      } catch (e) {
        toast("混音失败（干声整轨不受影响）：" + e.message, true);
        return false;
      }
    };
    _remixChain = _remixChain.then(run, run);
    return _remixChain;
  }

  // 场次铺底：以解析出的场次为准，一行一个场次卡片。
  // 氛围（mood）影响语调、需要重新合成；环境声 / BGM / 音量只影响混音，
  // 改完存盘再重算混音即可——不要把「加个背景雨声」变成重跑整部剧。
  function assetOptions(sel) {
    const opts = ['<option value="">不铺</option>'];
    S.assets.forEach((a) => {
      opts.push(`<option value="${esc(a.id)}"${a.id === sel ? " selected" : ""}>${esc(assetName(a))}</option>`);
    });
    // 素材被删了但场次还引用着：如实显示，别静默变回「不铺」
    if (sel && !S.assets.some((a) => a.id === sel)) {
      opts.push(`<option value="${esc(sel)}" selected>（素材已删除）</option>`);
    }
    return opts.join("");
  }

  function renderSceneMoods() {
    const box = $("sceneMoods");
    const list = $("sceneMoodList");
    const scenes = S.scenes || [];
    box.hidden = false;
    if (!scenes.length) {
      // 没写 ## 场次就没有可配的场次：与其整块藏起来（用户以为没这功能），
      // 不如说清前置条件。试听 / 重算混音按钮仍可用
      list.innerHTML = S.current
        ? '<div class="empty-hint">在剧本里用 <code>## 场次名</code> 分出场景，就能给每一场配环境声与 BGM。</div>'
        : "";
      renderMixState();
      return;
    }

    list.innerHTML = scenes.map((s, i) => {
      const moodOpts = ['<option value="">（无氛围）</option>']
        .concat(S.moods.map((m) => `<option value="${esc(m.key)}">${esc(m.key)}</option>`));
      // 词表之外的自定义氛围也要显示出来，否则下拉看着像「设置丢了」
      if (s.mood && !S.moods.some((m) => m.key === s.mood)) {
        moodOpts.push(`<option value="${esc(s.mood)}">${esc(s.mood)}（自定义）</option>`);
      }
      const gain = Math.round((s.gain == null ? 0.35 : s.gain) * 100);
      const bedOn = (s.ambience || s.bgm)
        ? '<em class="scene-bed-on">已铺</em>' : "";
      return `<div class="scene-bed">
        <div class="scene-bed-head">
          <span class="scene-bed-name" title="${esc(s.name)}">${esc(s.name)}</span>
          ${s.auto && s.mood ? '<em class="mood-auto">自动氛围</em>' : ""}${bedOn}
        </div>
        <label class="scene-bed-field"><span>氛围</span>
          <select class="input input-select" data-scene="${i}" data-field="mood"
            aria-label="场次「${esc(s.name)}」的氛围">${moodOpts.join("")}</select></label>
        <label class="scene-bed-field"><span>环境声</span>
          <select class="input input-select" data-scene="${i}" data-field="ambience"
            aria-label="场次「${esc(s.name)}」的环境声">${assetOptions(s.ambience)}</select></label>
        <label class="scene-bed-field"><span>BGM</span>
          <select class="input input-select" data-scene="${i}" data-field="bgm"
            aria-label="场次「${esc(s.name)}」的 BGM">${assetOptions(s.bgm)}</select></label>
        <label class="scene-bed-field scene-bed-gain"><span>音量</span>
          <input class="input" type="range" min="0" max="100" step="5" value="${gain}"
            data-scene="${i}" data-field="gain" aria-label="场次「${esc(s.name)}」的铺底音量">
          <em>${gain}%</em></label>
      </div>`;
    }).join("");

    // 回填下拉值：mood 可能是词表外的自定义值，靠 option 的 selected 不够稳
    list.querySelectorAll("select[data-scene]").forEach((sel) => {
      const i = parseInt(sel.dataset.scene, 10);
      const s = S.scenes[i];
      if (!s) return;
      sel.value = String(s[sel.dataset.field] || "");
    });

    list.querySelectorAll("[data-scene][data-field]").forEach((el) => {
      const i = parseInt(el.dataset.scene, 10);
      const field = el.dataset.field;
      const apply = async () => {
        const s = S.scenes[i];
        if (!s) return;
        if (field === "gain") {
          s.gain = Math.min(1, Math.max(0, (parseInt(el.value, 10) || 0) / 100));
        } else {
          s[field] = el.value;
          // 作者动过手，氛围之后由作者说了算
          if (field === "mood") s.auto = false;
        }
        const saved = await saveScript(true);
        if (!saved) return;
        if (field === "mood") {
          // 氛围进指令 → 需要重新解析（行内指令提示跟着变），不需要混音。
          // previewScriptNow 自己会重绘场次区，这里不用再来一次。
          await previewScriptNow();
          return;
        }
        if (S.current && S.current.full_wav) {
          // 铺底变了，旧 mix.wav 已被后端作废：立刻重算，别让「导出混音」偷偷用旧文件
          await remixScript(true);
        } else {
          toast("铺底已保存；合成整轨后混音会自动生成");
        }
        renderSceneMoods();
      };
      if (field === "gain") {
        // 拖动过程只更新数字，松手才落盘：不然每 5% 就发一次 PUT
        const out = el.parentElement.querySelector("em");
        el.addEventListener("input", () => { if (out) out.textContent = el.value + "%"; });
        el.addEventListener("change", apply);
      } else {
        el.addEventListener("change", apply);
      }
    });
    renderMixState();
  }

  let _parseTimer = null;
  // 解析序号：保存会顺手把角色配好，而在途的 /parse 是在角色建好**之前**
  // 发的，它回来时 errors 里还带着「角色没配好」。晚到的旧结果一律丢弃，
  // 否则用户保存完看到的是红字，点合成又能过——自我否定。
  let _parseSeq = 0;
  async function previewScriptNow() {
    if (!S.current) return;
    const raw = $("scriptRaw").value;
    const seq = ++_parseSeq;
    try {
      const d = await api(`/api/scripts/${S.current.id}/parse`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ raw, scenes: scenesPayload() }),
      });
      if (seq !== _parseSeq) return;      // 有更新的解析或保存已经覆盖了界面
      S.lines = d.lines || [];
      if (d.scenes) {
        // 只采纳服务端的氛围推断结果；铺底三字段以本地为准——parse 的响应
        // 基于「发请求那一刻」的 scenes 快照，晚到会把刚配好的环境声 / BGM
        // 覆盖成空，而下一次自动保存就把空串写进服务端，配置真丢了。
        const local = new Map((S.scenes || []).map((s) => [s.name, s]));
        S.scenes = d.scenes.map((s) => {
          const mine = local.get(s.name);
          if (!mine) return s;
          return {
            ...s,
            ambience: mine.ambience || "",
            bgm: mine.bgm || "",
            gain: mine.gain == null ? s.gain : mine.gain,
            mood: mine.auto ? s.mood : mine.mood,
            auto: mine.auto ? s.auto : false,
          };
        });
      }
      renderLines(d);
      renderSceneMoods();
    } catch (e) { /* 解析失败保持上一份结果，不打断输入 */ }
  }

  function schedulePreview() {
    clearTimeout(_parseTimer);
    _parseTimer = setTimeout(previewScriptNow, 350);
  }

  // 保存串行化：失焦保存与氛围下拉的 change 几乎同时触发，两个 PUT 都
  // 整数组写 scenes，后到的旧值会把刚选的氛围覆盖掉。排队 + 执行时才读
  // DOM，保证发出去的是最后一版。
  let _saveChain = Promise.resolve();

  function saveScript(quiet) {
    if (!S.current) return Promise.resolve(false);
    const id = S.current.id;
    const run = async () => {
      try {
        const d = await api("/api/scripts/" + id, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            raw: $("scriptRaw").value,
            title: $("scriptTitleInput").value.trim() || "未命名剧本",
            gap: parseFloat($("scriptGap").value) || 0.35,
            scenes: scenesPayload(),
          }),
        });
        // 用户可能已经切到别的剧本：只更新同一个剧本的界面状态
        if (S.current && S.current.id === id) {
          S.current = { ...S.current, ...d };
          if (d.scenes) S.scenes = d.scenes;
          $("scriptTitle").textContent = S.current.title;
          // 保存的返回体是「角色配完之后」的状态：用它重绘分镜。否则先一步
          // 发出的 /parse 会把「N 个角色没配好」的红字留在界面上
          _parseSeq += 1;
          if (d.lines) {
            S.lines = d.lines;
            renderLines(d);
            renderSceneMoods();
          }
        }
        if (!quiet) toast("已保存");
        refreshScripts();
        return true;
      } catch (e) {
        toast("保存失败：" + e.message, true);
        return false;
      }
    };
    _saveChain = _saveChain.then(run, run);
    return _saveChain;
  }

  async function newScript() {
    try {
      const d = await api("/api/scripts", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "未命名剧本", raw: "" }),
      });
      await refreshScripts(d.id);
      $("scriptRaw").focus();
    } catch (e) { toast("新建失败：" + e.message, true); }
  }

  // 开跑前以服务端**此刻**的解析为准：切屏回来后缓存的 missingRoles 会过期，
  // 会把明明能合成的剧本硬拦下来。解析接口挂了就返回 null，交给后端守卫。
  async function probeMissing() {
    if (!S.current) return null;
    try {
      const probe = await api(`/api/scripts/${S.current.id}/parse`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ raw: $("scriptRaw").value, scenes: scenesPayload() }),
      });
      const missing = [...new Set((probe.errors || []).filter((e) => e.role_name)
        .map((e) => e.role_name))];
      S.missingRoles = missing;
      return missing;
    } catch (e) {
      return null;
    }
  }

  async function synthScript() {
    if (!S.current) return;
    // 保存失败还继续，服务端用的就是上一版文本：用户看到的行数与合出来的内容对不上
    const saved = await saveScript(true);
    if (!saved) { toast("文本没保存成功，先解决再合成", true); return; }
    const missing = await probeMissing();
    if (missing && missing.length) {
      toast(`还有 ${missing.length} 个角色没配好：${missing.slice(0, 3).join("、")}，去分镜上方一键补齐`, true);
      return;
    }
    $("synthScriptBtn").disabled = true;
    $("produceScriptBtn").disabled = true;
    $("stopScriptBtn").hidden = false;
    $("scriptProgress").hidden = false;
    $("scriptBar").style.width = "2%";
    $("scriptProgressText").textContent = "正在启动…";
    try {
      const r = await api(`/api/scripts/${S.current.id}/synth`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resume: true }),
      });
      if (r.total === 0) {
        toast("所有行都已合成完成，无需重做");
        finishJob();
        return;
      }
      if (r.skipped) toast(`跳过已完成 ${r.skipped} 行，本次合成 ${r.total} 行`);
      S.job = r.job_id;
      pollJob(r.job_id, r.total);
    } catch (e) {
      toast(e.message, true);
      finishJob();
    }
  }

  // 一键出片：合成没做完的行 → 拼接整轨 → 混音 → 打包下载。
  // 与「合成全剧」的区别是这里必须保证整轨与当前台词一致，所以已完成
  // 的行直接用、要重录的行照样重做，最后连 ZIP 一起给。
  async function produceScript() {
    if (!S.current || S.producing) return;
    const saved = await saveScript(true);
    if (!saved) { toast("文本没保存成功，先解决再出片", true); return; }
    const missing = await probeMissing();
    if (missing && missing.length) {
      toast(`还有 ${missing.length} 个角色没配好：${missing.slice(0, 3).join("、")}，先补齐再出片`, true);
      return;
    }
    S.producing = true;
    $("produceScriptBtn").disabled = true;
    $("synthScriptBtn").disabled = true;
    $("stopScriptBtn").hidden = false;
    $("scriptProgress").hidden = false;
    $("scriptBar").style.width = "2%";
    $("scriptProgressText").textContent = "正在出片…";
    try {
      const r = await api(`/api/scripts/${S.current.id}/produce`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (r.skipped) toast(`复用已完成 ${r.skipped} 行，本次合成 ${r.total} 行`);
      else if (r.total) toast(`本次合成 ${r.total} 行`);
      S.produceParts = r.parts || [];
      S.job = r.job_id;
      // total 可能是 0（全部行都已完成）：这时仍在拼接与混音，不能提前退出
      pollJob(r.job_id, r.total, { produce: true });
    } catch (e) {
      toast(e.message, true);
      S.producing = false;
      finishJob();
    }
  }

  function finishJob() {
    $("synthScriptBtn").disabled = false;
    $("produceScriptBtn").disabled = !S.current;
    $("stopScriptBtn").hidden = true;
    clearTimeout(S.timer);
  }

  function pollJob(jobId, total, opts) {
    const producing = !!(opts && opts.produce);
    let strikes = 0;
    const tick = async () => {
      let d;
      try {
        d = await api("/api/scripts/jobs/" + jobId);
        strikes = 0;
      } catch (e) {
        // 一次网络抖动不该终止整个轮询：合成还在跑，界面却停在原处，
        // 用户会以为卡死。连续失败才判定任务失联。
        strikes += 1;
        if (strikes < 5) {
          S.timer = setTimeout(tick, 2000);
          return;
        }
        toast("与任务失去联系：" + e.message, true);
        S.producing = false;
        finishJob();
        return;
      }
      const done = d.done + d.failed;
      const pct = Math.min(99, Math.round((done / Math.max(1, d.total)) * 100));
      $("scriptBar").style.width = Math.max(2, pct) + "%";
      const cur = d.rows && d.rows[d.current] ? d.rows[d.current] : null;
      const eta = d.eta == null ? "正在估算剩余" : `预计还需 ${Math.round(d.eta)} 秒`;
      if (producing && done >= d.total) {
        // 逐行合成已经收尾、终态还没到：这段时间在拼接与混音，报「第 N/N 行」
        // 会让人以为卡住了
        $("scriptProgressText").textContent = d.total
          ? "全部行已合成，正在拼接整轨并混音…"
          : "台词都已就绪，正在拼接整轨并混音…";
      } else {
        $("scriptProgressText").textContent =
          `${producing ? "出片中 · " : ""}第 ${Math.min(done + 1, d.total)}/${d.total} 行 · `
          + `${cur ? cur.role_name + "：" + (cur.text || "").slice(0, 12) : ""} · ${eta}`;
      }
      renderJobRows(d.rows);
      if (d.state === "running") {
        S.timer = setTimeout(tick, 1500);
      } else {
        $("scriptBar").style.width = "100%";
        const failed = d.failed || 0;
        // 拼接/混音都在终态之前：失败要如实说，别让用户拿着没混音的包当成片
        const tail = d.error ? `，任务失败：${d.error}`
          : d.concat_error ? `，但整轨拼接失败：${d.concat_error}`
            : d.mix_error ? `，但混音失败（干声整轨不受影响）：${d.mix_error}` : "";
        // total=0 表示所有行都已完成（一键出片复用了它们）：这时没有「成功几行」
        // 可说，报「0 行成功」会让人以为什么都没做
        const doneText = d.total === 0
          ? "台词全部就绪，已重新拼接整轨并混音"
          : `完成：${d.done} 行成功${failed ? "，" + failed + " 行失败" : ""}`;
        $("scriptProgressText").textContent = d.state === "stopped"
          ? `已停止（完成 ${d.done} 行，可再次点合成继续）`
          : `${doneText}${tail}`;
        S.producing = false;
        finishJob();
        // 只重载同一个剧本：用户可能已经切到别的剧本并改了内容，
        // 这时整页重开会把未保存的编辑冲掉。
        if (S.current && S.current.id === d.script_id) {
          await openScript(S.current.id);
          if (d.error) {
            toast("任务失败：" + d.error, true);
          } else if (d.concat_error) {
            toast("整轨拼接失败：" + d.concat_error, true);
          } else if (d.mix_error) {
            toast("混音失败（干声整轨不受影响）：" + d.mix_error, true);
          } else if (producing && d.state === "done" && !failed) {
            toast("成片已生成，正在打包下载");
            await deliverZip();
          } else if (d.state === "done" && !failed) {
            toast("整轨已生成，可直接播放或导出");
          }
        } else {
          refreshScripts();
          if (d.state === "done") toast(producing ? "成片已生成" : "剧本已合成完成");
        }
      }
    };
    tick();
  }

  function renderJobRows(rows) {
    if (!rows) return;
    // 按 data-line-idx 定位：续做时 rows 只含本次要做的行（键是 idx），
    // 用 DOM 序号取值会整体错位，把状态标到别的台词上。
    document.querySelectorAll("#scriptLines .script-line").forEach((el) => {
      const row = rows[el.dataset.lineIdx];
      if (!row) return;
      el.classList.remove("pending", "running", "done", "failed");
      el.classList.add(row.state === "pending" ? "pending" : row.state);
      const mark = el.querySelector(".line-state");
      if (mark) {
        mark.className = "line-state " + (row.state === "done" ? "done" : row.state === "failed" ? "failed" : "pending");
        mark.textContent = row.state === "done" ? "✓" : row.state === "failed" ? "✕" : "";
        if (row.error) mark.title = row.error;
      }
    });
  }

  async function redoLine(idx) {
    if (!S.current) return;
    toast("正在重做这一行…");
    try {
      const r = await api(`/api/scripts/${S.current.id}/lines/${idx}/redo`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      await openScript(S.current.id);
      toast(r && r.take > 1 ? `已生成 v${r.take}（旧版本仍可切回）` : "这一行已更新");
    } catch (e) { toast("重做失败：" + e.message, true); }
  }

  // 一键补齐角色：后端按名字配原型/音色，回来后重新打开剧本，
  // 行列表与「未知角色」提示一起刷新，用户立刻能点合成全剧。
  async function autoCast() {
    if (!S.current) return;
    const btn = $("castBtn");
    if (btn) { btn.disabled = true; btn.textContent = "正在配角色…"; }
    try {
      const r = await api(`/api/scripts/${S.current.id}/cast`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      const brief = (r.created || []).slice(0, 3)
        .map((c) => `${c.name}（${c.label}）`).join("、");
      await refreshRoles();          // 新配的角色要立刻出现在角色库（专属角色不进工作台左栏）
      await openScript(S.current.id);
      toast(r.count
        ? `已配好 ${r.count} 个角色：${brief}${r.count > 3 ? " 等" : ""}${r.remaining ? `，还有 ${r.remaining} 处要修` : "，现在可以合成全剧了"}`
        : "角色都配好了");
    } catch (e) {
      toast("补齐失败：" + e.message, true);
    } finally {
      // 成功时 openScript 会重绘、旧按钮已不在 DOM；失败或「cast 成功但
      // 刷新详情失败」时必须复位，否则按钮永远停在「正在配角色…」
      if (btn && btn.isConnected) {
        btn.disabled = false;
        btn.textContent = "一键补齐角色与音色";
      }
    }
  }

  // 切版本：只换「用哪一版」，不重新合成，整轨与导出跟着当前这版走
  async function switchTake(idx, n) {
    if (!S.current) return;
    try {
      await api(`/api/scripts/${S.current.id}/lines/${idx}/take`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ n }),
      });
      await openScript(S.current.id);
      toast(`已切到 v${n}`);
    } catch (e) { toast("切换版本失败：" + e.message, true); }
  }

  // 导出：按勾选拼 parts。没有音频的项直接禁用而不是导出空包——
  // 用户拿到只有 script.txt 的 ZIP 会以为合成丢了。
  const EXPORT_IDS = {
    full: "expFull", lines: "expLines", timeline: "expTimeline",
    script: "expScript", subtitles: "expSubtitles", mix: "expMix",
  };

  // 下载走 fetch 而不是 location.href：后者在 4xx 时会把 JSON 错误当页面
  // 渲染，用户看到一屏 {"detail": "…"}，还不知道怎么回去。
  async function downloadZip(parts) {
    if (!S.current) return false;
    const resp = await fetch(`/api/scripts/${S.current.id}/export?parts=${parts.join(",")}`);
    if (!resp.ok) {
      let msg = `导出失败（HTTP ${resp.status}）`;
      try {
        const j = await resp.json();
        if (j && j.detail) msg = j.detail;
      } catch (e) { /* 非 JSON 响应，保留状态码 */ }
      throw new Error(msg);
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${($("scriptTitleInput").value.trim() || "剧本")}.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    return true;
  }

  // 出片后自动打包：整轨 + 混音（有才带）+ 时间轴 + 字幕 + 剧本。
  // 不带分行音频：一部剧几十个 wav，成片交付用不上，还会把包撑大。
  async function deliverZip() {
    const s = S.current || {};
    // 可交付项以后端 produce 回包的 parts 为准（契约 §3），只去掉分行音频；
    // 再按实际产物收敛——没有 mix_wav 还带 mix 会被后端整包拒绝。
    let parts = (S.produceParts || []).length
      ? S.produceParts.filter((p) => p !== "lines")
      : ["full", "script", "timeline", "subtitles"];
    if (!s.mix_wav) parts = parts.filter((p) => p !== "mix");
    if (!s.full_wav) parts = parts.filter((p) => p !== "full");
    if (!(s.timeline || []).length) parts = parts.filter((p) => p !== "timeline" && p !== "subtitles");
    try {
      await downloadZip(parts);
    } catch (e) {
      toast("成片已生成，但打包下载失败：" + e.message + "（可点「导出 ZIP」重试）", true);
    }
  }

  async function openExportDialog() {
    if (!S.current) return;
    // 导出读的是磁盘上的内容：先把当前编辑存进去，否则 script.txt 是上一版
    if (!(await saveScript(true))) return;
    const hasFull = !!(S.current.full_wav);
    const hasLines = (S.lines || []).some((l) => l.wav);
    // 混音只在真的生成过时可选：没配铺底时后端不会产出「只有干声」的假混音
    const hasMix = !!(S.current.mix_wav);
    const pending = (S.lines || []).filter((l) => l.status !== "done").length;
    const setOpt = (key, ok, def = true) => {
      const el = $(EXPORT_IDS[key]);
      el.disabled = !ok;
      el.checked = ok && def;
      const row = el.closest(".opt-row");
      if (row) row.classList.toggle("is-off", !ok);
    };
    // 有行待重做时整轨与时间轴都是上一版的：宁可禁掉，也不交付文不对音的文件
    const freshFull = hasFull && pending === 0;
    setOpt("full", freshFull);
    setOpt("lines", hasLines);
    // 时间轴由拼接时算出：没有整轨就没有可用的时间轴
    setOpt("timeline", freshFull);
    setOpt("script", true);
    // 字幕与混音都是新增项，默认不勾：老用户导出包内容不悄悄变
    setOpt("subtitles", freshFull, false);
    setOpt("mix", hasMix && freshFull, false);

    const rows = (S.lines || []).length;
    $("exportSub").textContent = pending
      ? `还有 ${pending} 行没合成或台词改过还没重做。整轨与时间轴都是上一版的，先「合成全剧」或「一键出片」再导出。`
      : (hasFull
          ? `${rows} 行 · 整轨 ${Number(S.current.duration || 0).toFixed(1)} 秒`
            + `${hasMix ? " · 混音已就绪" : " · 还没有混音（配好场次铺底后点「重算混音」）"} · 勾选要放进 ZIP 的内容`
          : "这个剧本还没合成，音频、混音与时间轴暂时不可选；现在只能导出剧本文本。");
    $("exportDlg").showModal();
  }

  async function confirmExport() {
    if (!S.current) return;
    const parts = Object.keys(EXPORT_IDS).filter((k) => {
      const el = $(EXPORT_IDS[k]);
      return el.checked && !el.disabled;
    });
    if (!parts.length) { toast("至少勾一项要导出的内容", true); return; }
    const btn = $("exportConfirmBtn");
    btn.disabled = true;
    try {
      await downloadZip(parts);
      $("exportDlg").close();
      toast("已导出 ZIP");
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 原型建音色
  let _protoAutoName = "";
  function fillProtoFromArchetype() {
    const a = S.archetypes.find((x) => x.key === $("protoArchetype").value);
    if (!a) return;
    $("protoInstruction").value = a.instruction || "";
    // 只在名字还是上一次自动填的（或空）时才覆盖：
    // 用户手打了名字再换原型，不该被冲掉。
    const cur = $("protoName").value.trim();
    if (!cur || cur === _protoAutoName || /音色$/.test(cur)) {
      _protoAutoName = (a.label || "原型") + "音色";
      $("protoName").value = _protoAutoName;
    }
  }

  function openProtoDialog() {
    if (!S.archetypes.length) { toast("原型数据还没加载好，稍等一下再试", true); return; }
    const sel = $("protoArchetype");
    sel.innerHTML = S.archetypes.map((a) =>
      `<option value="${esc(a.key)}">${esc(a.label)}</option>`).join("");
    sel.value = S.archetypes.some((a) => a.key === "narrator") ? "narrator"
      : (S.archetypes[0] ? S.archetypes[0].key : "custom");
    _protoAutoName = "";
    fillProtoFromArchetype();
    $("protoVoiceDlg").showModal();
  }

  async function saveProtoVoice() {
    const name = $("protoName").value.trim();
    if (!name) { toast("给这个音色起个名字", true); return; }
    try {
      await api("/api/voices", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          type: "design",
          instruction: $("protoInstruction").value.trim(),
          archetype: $("protoArchetype").value,
        }),
      });
      $("protoVoiceDlg").close();
      await refreshVoices();
      toast(`音色「${name}」已建好，去角色里绑定它就能用`);
    } catch (e) { toast("创建失败：" + e.message, true); }
  }

  async function loadMeta() {
    try {
      const d = await api("/api/archetypes");
      S.archetypes = d.archetypes || [];
      S.emotions = d.emotions || [];
      S.moods = d.moods || [];
      const sel = $("roleArchetype");
      if (sel) {
        sel.innerHTML = S.archetypes.map((a) =>
          `<option value="${esc(a.key)}">${esc(a.label)}</option>`).join("");
      }
      const emo = $("roleEmotion");
      if (emo) {
        // 第一项是「跟随文本」：没有它，每个角色都被钉死一个情绪，
        // 标点/关键词推断就永远没机会生效。
        emo.innerHTML = '<option value="">（跟随文本自动判断）</option>' +
          S.emotions.map((e) => `<option value="${esc(e)}">${esc(e)}</option>`).join("");
      }
      // 角色卡上的原型名来自这份元数据：boot 时角色先于它加载完，
      // 不补一次渲染就会把每个角色都显示成「自定义」。
      if (state.roles.length) renderRoles();
    } catch (e) { /* 元数据缺失不阻塞主流程 */ }
  }

  function prefillArchetypeEmotion() {
    const sel = $("roleArchetype");
    const emo = $("roleEmotion");
    if (!sel || !emo) return;
    const spec = S.archetypes.find((a) => a.key === sel.value);
    // 「中性」等于没有倾向：预填它会让这个角色永远不再走自动推断，
    // 只有疲惫/温柔这种真正有指向的原型才预填。
    const mood = spec && spec.default_emotion;
    emo.value = (mood && mood !== "中性") ? mood : "";
  }

  function bindScript() {
    $("newScriptBtn").addEventListener("click", newScript);
    $("synthScriptBtn").addEventListener("click", synthScript);
    $("produceScriptBtn").addEventListener("click", produceScript);
    $("exportScriptBtn").addEventListener("click", openExportDialog);
    $("exportConfirmBtn").addEventListener("click", confirmExport);
    $("protoVoiceBtn").addEventListener("click", openProtoDialog);
    $("protoArchetype").addEventListener("change", fillProtoFromArchetype);
    $("protoSaveBtn").addEventListener("click", saveProtoVoice);
    // 素材库：生成 / 上传 / 删除都在同一个弹层里完成
    $("openAssetsBtn").addEventListener("click", openAssetsDialog);
    $("assetGenBtn").addEventListener("click", generateAsset);
    $("assetUploadBtn").addEventListener("click", uploadAsset);
    $("assetList").addEventListener("click", (e) => {
      const b = e.target.closest("[data-delasset]");
      if (b) deleteAsset(b.dataset.delasset);
    });
    $("remixBtn").addEventListener("click", () => remixScript(false));
    // 试听混音 / 切回干声：播放器一直是同一个控件，不给个来回切换的话，
    // 用户听完 mix 会以为「干声被改掉了」——而这正是这里最要紧的边界
    $("playMixBtn").addEventListener("click", () => {
      const a = $("scriptAudio");
      const btn = $("playMixBtn");
      if (a.dataset.src === "mix") {
        a.src = audioUrl(S.current && S.current.full_wav);
        a.dataset.src = "full";
        btn.textContent = "试听混音";
        a.play().catch(() => {});
        toast("已切回干声整轨 full.wav");
        return;
      }
      if (!(S.current && S.current.mix_wav)) { toast("还没有混音成片", true); return; }
      a.src = audioUrl(S.current.mix_wav);
      a.dataset.src = "mix";
      a.hidden = false;
      btn.textContent = "切回干声";
      a.play().catch(() => {});
      toast("正在播放混音版；整轨干声 full.wav 没有改动");
    });
    $("stopScriptBtn").addEventListener("click", async () => {
      if (!S.job) return;
      try { await api(`/api/scripts/jobs/${S.job}/stop`, { method: "POST", body: "{}" }); } catch (e) { /* 忽略 */ }
    });
    $("scriptList").addEventListener("click", (e) => {
      const b = e.target.closest("[data-script]");
      if (b) openScript(b.dataset.script);
    });
    $("scriptRaw").addEventListener("input", schedulePreview);
    $("scriptRaw").addEventListener("blur", () => saveScript(true));
    $("scriptTitleInput").addEventListener("blur", () => saveScript(true));
    $("scriptGap").addEventListener("change", () => saveScript(true));
    $("scriptLines").addEventListener("click", (e) => {
      if (e.target.closest("#castBtn")) { autoCast(); return; }
      const play = e.target.closest("[data-playline]");
      if (play) {
        const idx = parseInt(play.dataset.playline, 10);
        const line = (S.lines || [])[idx];
        if (line && line.wav) {
          const a = $("scriptAudio");
          a.src = audioUrl(line.wav);
          a.hidden = false;
          a.play().catch(() => {});
        }
        return;
      }
      const redo = e.target.closest("[data-redoline]");
      if (redo) redoLine(parseInt(redo.dataset.redoline, 10));
    });
    // 版本下拉在行内：用 change 而不是 click，键盘选择也要生效
    $("scriptLines").addEventListener("change", (e) => {
      const sel = e.target.closest("[data-takeline]");
      if (sel) {
        switchTake(parseInt(sel.dataset.takeline, 10), parseInt(sel.value, 10));
        return;
      }
      const f = e.target.closest("#lineFilterSel");
      if (f) { S.lineFilter = f.value; renderLines(S.lastRender); return; }
      const r = e.target.closest("#lineRoleSel");
      if (r) { S.lineRole = r.value; renderLines(S.lastRender); return; }
      const j = e.target.closest("#lineJump");
      if (j) {
        const n = parseInt(j.value, 10);
        S.jumpIdx = Number.isFinite(n) && n >= 1 && n <= (S.lines || []).length ? n : 0;
        renderLines(S.lastRender);
        if (S.jumpIdx && !$("scriptLines").querySelector(`[data-line-idx="${S.jumpIdx - 1}"]`)) {
          toast("这一行被筛选条件藏起来了，切回「全部状态 / 全部角色」再看", true);
        }
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
