/* Rain Bridge · 雨幕桥接 admin panel – dependency-free single page app. */
(function () {
  "use strict";

  // ------------------------------------------------------------ utils
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtTime = (ts) => { if (!ts) return "-"; const d = new Date(ts * 1000); return d.toLocaleString("zh-CN", { hour12: false }); };
  const fmtAgo = (ts) => { if (!ts) return "-"; const s = Math.max(0, Date.now() / 1000 - ts); if (s < 60) return `${Math.floor(s)} 秒前`; if (s < 3600) return `${Math.floor(s / 60)} 分钟前`; if (s < 86400) return `${Math.floor(s / 3600)} 小时前`; return `${Math.floor(s / 86400)} 天前`; };
  const fmtDur = (s) => { s = Math.floor(s || 0); const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60); return d ? `${d}天${h}小时` : h ? `${h}小时${m}分` : `${m}分${s % 60}秒`; };
  const fel = (form, name) => form.elements.namedItem(name);
  const platLabel = (p) => ({ telegram: "Telegram" }[p] || (p ? String(p).toUpperCase() : "-"));
  const DIR = { both: "双向", a_to_b: "A → B", b_to_a: "B → A" };
  const ARROW = { both: "⇄", a_to_b: "→", b_to_a: "←" };
  const STATUS_CLS = { authorized: "ok", pending: "warn", discovered: "", rejected: "bad", disabled: "", left: "bad", limited: "warn", error: "bad" };
  const MSG_STATUS = { pending: ["排队", ""], processing: ["处理中", "info"], sending: ["发送中", "info"], sent: ["成功", "ok"], failed: ["重试中", "warn"], dead: ["失败", "bad"], dropped: ["已丢弃", ""] };
  const MEDIA_LABEL = { text: "文本", photo: "图片", animation: "GIF/动画", video: "视频", audio: "音频", voice: "语音", document: "文件", sticker: "贴纸", forward: "转发消息" };
  const DROPLET = `<svg width="14" height="14" viewBox="0 0 32 32" fill="none"><path d="M16 5.5c4.2 5.4 6.7 8.9 6.7 12a6.7 6.7 0 1 1-13.4 0c0-3.1 2.5-6.6 6.7-12z" stroke="#007aff" stroke-width="2.6" stroke-linejoin="round"/></svg>`;

  function toast(msg, type) {
    const el = document.createElement("div");
    el.className = "toast " + (type || "");
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), 3800);
  }

  function modal(html, opts) {
    opts = opts || {};
    const root = $("#modal-root");
    root.innerHTML = `<div class="modal-bg"><div class="modal">${html}</div></div>`;
    const close = () => { root.innerHTML = ""; };
    $(".modal-bg", root).addEventListener("click", (e) => { if (e.target.classList.contains("modal-bg") && !opts.sticky) close(); });
    return { root, close, el: $(".modal", root) };
  }

  function confirmDialog(title, body, okLabel, danger) {
    return new Promise((resolve) => {
      const m = modal(`<h3>${esc(title)}</h3><div>${body}</div><div class="foot"><button class="btn" data-x>取消</button><button class="btn ${danger ? "danger" : "primary"}" data-ok>${esc(okLabel || "确定")}</button></div>`);
      $("[data-x]", m.el).onclick = () => { m.close(); resolve(false); };
      $("[data-ok]", m.el).onclick = () => { m.close(); resolve(true); };
    });
  }

  const state = { session: null, csrf: null, timers: [], title: "雨幕桥接 · Rain Bridge" };

  async function api(method, path, body) {
    const headers = { "Accept": "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (state.csrf) headers["X-CSRF-Token"] = state.csrf;
    const res = await fetch("/api" + path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined, credentials: "same-origin" });
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (res.status === 401 && path !== "/session" && path !== "/login") { state.session = null; render(); throw new Error("未登录"); }
    if (!res.ok) throw new Error((data && data.error) || `请求失败 (${res.status})`);
    return data;
  }
  const get = (p) => api("GET", p);
  const post = (p, b) => api("POST", p, b || {});
  const put = (p, b) => api("PUT", p, b || {});
  const patch = (p, b) => api("PATCH", p, b || {});
  const del = (p) => api("DELETE", p);

  function clearTimers() { state.timers.forEach(clearInterval); state.timers = []; }
  function every(ms, fn) { state.timers.push(setInterval(fn, ms)); }
  const can = (role) => { const lv = { viewer: 0, admin: 1, owner: 2 }; return state.session && lv[state.session.user.role] >= lv[role]; };

  // ------------------------------------------------------------ shell
  const NAV = [
    ["#/", "总览"], ["#/connections", "连接"], ["#/chats", "群组"], ["#/bridges", "桥接"],
    ["#/messages", "消息"], ["#/logs", "日志"], ["#/settings", "系统"],
  ];

  function shell(content, active) {
    const u = state.session.user;
    return `
    <div class="nav-shell">
      <div class="nav-top">
        <div class="brand"><span class="logo">${DROPLET}</span>${esc(state.title)}</div>
        <nav class="nav-pills">${NAV.map(([h, n]) => `<a href="${h}" class="${active === h ? "active" : ""}">${n}${h === "#/chats" && state.pendingCount ? `<span class="badge warn">${state.pendingCount}</span>` : ""}</a>`).join("")}</nav>
        <div class="user-chip"><span class="uname">${esc(u.username)}</span><span class="badge">${u.role}</span><a href="#" data-logout>退出</a></div>
      </div>
    </div>
    <div class="container">${content}</div>`;
  }

  function mount(html) {
    $("#app").innerHTML = html;
    const lo = $("[data-logout]");
    if (lo) lo.onclick = async (e) => { e.preventDefault(); await post("/logout"); state.session = null; state.csrf = null; render(); };
  }

  // ------------------------------------------------------------ auth views
  function authFrame(title, inner) {
    return `<div class="auth"><div class="plate glass">
      <div class="brand"><span class="logo">${DROPLET}</span>${esc(title)}</div>
      <div class="tagline">RAIN BRIDGE · 雨幕凝光</div>
      <div class="poem">雨夜隔着车窗，看整座喧嚣的城市。<br>晶片之内，是两个世界安静的呼吸。</div>
      <div class="line-flow"></div>
      ${inner}
    </div></div>`;
  }

  function viewSetup() {
    mount(authFrame(state.title, `
      <form id="f">
        <div class="field"><label>初始化令牌</label><input class="input mono" name="setup_token" required autocomplete="off" placeholder="安装脚本输出的 Setup Token"><div class="hint">丢失了？在服务器执行 <code>qqtg setup-token</code></div></div>
        <div class="field"><label>用户名</label><input class="input" name="username" required minlength="3" autocomplete="username"></div>
        <div class="field"><label>密码</label><input class="input" type="password" name="password" required minlength="8" autocomplete="new-password"><div class="hint">至少 8 位</div></div>
        <button class="btn primary" style="width:100%;padding:12px">创建并进入面板</button>
      </form>`));
    $("#f").onsubmit = async (e) => {
      e.preventDefault();
      const fd = Object.fromEntries(new FormData(e.target).entries());
      try { const r = await post("/setup", fd); state.csrf = r.csrf; await loadSession(); render(); toast("初始化完成，欢迎！", "ok"); } catch (err) { toast(err.message, "bad"); }
    };
  }

  function viewLogin() {
    mount(authFrame(state.title, `
      <form id="f">
        <div class="field"><label>用户名</label><input class="input" name="username" required autocomplete="username" autofocus></div>
        <div class="field"><label>密码</label><input class="input" type="password" name="password" required autocomplete="current-password"></div>
        <button class="btn primary" style="width:100%;padding:12px">登录</button>
      </form>`));
    $("#f").onsubmit = async (e) => {
      e.preventDefault();
      const fd = Object.fromEntries(new FormData(e.target).entries());
      try { const r = await post("/login", fd); state.csrf = r.csrf; await loadSession(); render(); } catch (err) { toast(err.message, "bad"); }
    };
  }

  // ------------------------------------------------------------ dashboard
  async function viewDashboard() {
    const [d, conns] = await Promise.all([get("/overview"), get("/connections")]);
    state.pendingCount = d.pending_chats.length;
    const h = d.health, s = d.stats.today;
    const tgConn = conns.telegram || { configured: false, status: {} };
    const hasTG = !!tgConn.configured, tgOk = !!tgConn.status.connected;
    const enabled = d.bridges.filter((b) => b.enabled).length;
    const onboardStep = !hasTG ? 1 : d.bridges.length === 0 ? 2 : 3;
    const onboard = onboardStep < 3 ? `<div class="card glass"><div class="card-head"><h2>快速开始</h2><span class="muted small">完成 3 步即可开始桥接</span></div>
      <div class="onboard">
        ${[["连接 Telegram", "#/connections"], ["创建桥接", "#/bridges/new"], ["完成", "#/"]].map(([n, href], i) => `<a class="ob ${i + 1 < onboardStep ? "done" : i + 1 === onboardStep ? "active" : ""}" href="${href}"><div class="n">${i + 1 < onboardStep ? "✓" : i + 1}</div><div>${n}</div></a>`).join("")}
      </div></div>` : "";
    const week = d.stats.week || [];
    const max = Math.max(1, ...week.map((w) => (w.sent || 0) + (w.failed || 0)));
    const issues = d.stats.issues || [];
    mount(shell(`
      <div class="page-head"><div><h1>总览</h1><div class="sub">运行 ${fmtDur(h.uptime)} · v${esc(h.version)}</div></div>
        <div class="actions"><a class="btn" href="#/bridges/new">＋ 创建桥接</a><button class="btn" data-diag>一键诊断</button></div></div>
      ${onboard}
      <div class="grid cols-4" style="margin-top:16px">
        <div class="card glass stat"><div class="label">今日消息</div><div class="value">${s.sent}</div><div class="delta">A→B ${s.a_to_b} · B→A ${s.b_to_a}</div></div>
        <div class="card glass stat"><div class="label">今日媒体</div><div class="value">${Object.entries(s.kinds).filter(([k]) => k !== "text").reduce((a, [, v]) => a + v, 0)}</div><div class="delta">${Object.entries(s.kinds).filter(([k]) => k !== "text").map(([k, v]) => `${MEDIA_LABEL[k] || k} ${v}`).join(" · ") || "—"}</div></div>
        <div class="card glass stat"><div class="label">今日失败</div><div class="value" style="color:${s.failed ? "var(--bad)" : "inherit"}">${s.failed}</div><div class="delta">队列 ${h.queue.depth} · 等待重试 ${h.queue.retries_waiting}</div></div>
        <div class="card glass stat"><div class="label">桥接</div><div class="value">${enabled}<span class="muted" style="font-size:14px;font-weight:500"> / ${d.bridges.length}</span></div><div class="delta">已启用 / 总数</div></div>
      </div>
      <div class="grid cols-2" style="margin-top:16px">
        <div class="card glass"><div class="card-head"><h2>System Health</h2><span class="muted small">每 10 秒刷新</span></div>
          <div class="health">${Object.values(h.components).map((c) => `<div class="item"><span class="dot ${c.ok ? "ok" : "bad"}"></span><div><div class="name">${esc(c.label)}</div><div class="detail" title="${esc(c.detail)}">${esc(c.detail || (c.ok ? "正常" : "异常"))}</div></div></div>`).join("")}</div>
          <hr class="hr">
          <div class="row">
            <span><span class="dot ${tgOk ? "ok" : "bad"}"></span> <b>Telegram</b> ${hasTG ? (tgConn.status.self_name ? "@" + esc(tgConn.status.self_name) : "") : "未配置"} ${hasTG && !tgOk ? `<span class="badge bad">${esc(tgConn.status.last_error || "离线")}</span>` : ""}</span>
            <a class="btn sm" href="#/connections">管理连接 →</a>
          </div>
        </div>
        <div class="card glass"><div class="card-head"><h2>Issues</h2><a href="#/messages?status=failed" class="small">查看详情</a></div>
          ${issues.length ? `<div class="alert warn">⚠ 最近 24 小时有 ${issues.reduce((a, i) => a + i.n, 0)} 条消息发送失败</div><table>${issues.map((i) => `<tr><td>${i.n} ×</td><td>${esc(errorLabel(i.error_code))}</td><td class="muted mono">${esc(i.error_code || "")}</td></tr>`).join("")}</table>` : `<div class="empty">最近 24 小时没有失败消息 🌤</div>`}
          ${d.pending_chats.length ? `<div class="alert warn" style="margin-bottom:0">有 ${d.pending_chats.length} 个群组在等待授权 → <a href="#/chats">前往处理</a></div>` : ""}
          <div style="margin-top:14px"><div class="muted small" style="margin-bottom:6px">近 7 天消息量</div><div class="sparkline">${week.map((w) => `<div class="bar" title="${w.day}: ${w.sent} 成功 / ${w.failed} 失败" style="height:100%"><i style="height:${Math.round(((w.sent || 0) / max) * 100)}%"></i></div>`).join("") || '<span class="muted small">暂无数据</span>'}</div></div>
        </div>
      </div>
      <div class="card-head" style="margin:24px 2px 12px"><h2>Bridges</h2><a href="#/bridges" class="small">管理全部</a></div>
      ${d.bridges.length ? `<div class="grid cols-2">${d.bridges.map(bridgeCard).join("")}</div>` : `<div class="card glass empty">还没有桥接。<a href="#/bridges/new">创建第一条桥接</a></div>`}
    `, "#/"));
    $("[data-diag]").onclick = runDiagnose;
    bindBridgeCardActions(viewDashboard);
    every(10000, async () => { if (location.hash === "#/" || location.hash === "") { try { await viewDashboard(); } catch (e) { } } });
  }

  function errorLabel(code) {
    return { FILE_TOO_LARGE: "文件过大", CHAT_FORBIDDEN: "机器人被移出或无权限", NO_PERMISSION: "权限不足", CHAT_NOT_FOUND: "找不到目标群",
      NOT_IN_GROUP: "机器人不在目标群", TARGET_OFFLINE: "目标平台未连接", NETWORK: "网络错误", RATE_LIMIT: "限流", MEDIA_CONVERT_FAILED: "媒体转换失败", MEDIA_TIMEOUT: "媒体处理超时",
      FFMPEG_MISSING: "缺少 FFmpeg", DOWNLOAD_FAILED: "媒体下载失败", API_FAILED: "平台接口失败", TIMEOUT: "接口超时", INTERNAL: "内部错误", EMPTY: "空消息", BAD_MEDIA: "媒体不被接受" }[code] || (code || "未知错误");
  }

  function bridgeCard(b) {
    const warns = b.warnings || [];
    const st = !b.enabled ? ["○ 已停用", ""] : warns.length ? ["● 运行中（有告警）", "warn"] : ["● 正常运行", "ok"];
    return `<div class="card glass bridge-card">
      <div class="title"><h3>${esc(b.name)}</h3><span class="badge ${st[1]}">${st[0]}</span></div>
      <div class="pair">
        <div class="side"><div class="plat">A · ${esc(b.a_platform_label || platLabel(b.a_platform))}</div><div class="name" title="${esc(b.a_chat)}">${esc(b.a_title)}</div></div>
        <div class="arrow">${ARROW[b.direction] || "⇄"}</div>
        <div class="side" style="text-align:right"><div class="plat">B · ${esc(b.b_platform_label || platLabel(b.b_platform))}</div><div class="name" title="${esc(b.b_chat)}">${esc(b.b_title)}</div></div>
      </div>
      <div class="meta"><span>今日消息 ${b.today.sent}</span><span>今日媒体 ${b.today.media}</span>${b.today.failed ? `<span style="color:var(--bad)">失败 ${b.today.failed}</span>` : ""}<span>${DIR[b.direction] || b.direction}</span></div>
      ${warns.length ? `<div class="warns">${warns.map((w) => `<span class="badge warn">⚠ ${esc(w)}</span>`).join("")}</div>` : ""}
      <div class="actions"><a class="btn sm" href="#/bridges/${b.id}">管理</a>${can("admin") ? `<button class="btn sm" data-toggle="${b.id}" data-on="${b.enabled ? 0 : 1}">${b.enabled ? "停用" : "启用"}</button><button class="btn sm" data-test="${b.id}">发送测试消息</button>` : ""}</div>
    </div>`;
  }

  function bindBridgeCardActions(refresh) {
    $$("[data-toggle]").forEach((btn) => btn.onclick = async () => { try { await patch(`/bridges/${btn.dataset.toggle}`, { enabled: btn.dataset.on === "1" }); toast(btn.dataset.on === "1" ? "桥接已启用" : "桥接已停用", "ok"); refresh(); } catch (e) { toast(e.message, "bad"); } });
    $$("[data-test]").forEach((btn) => btn.onclick = async () => { btn.disabled = true; try { const r = await post(`/bridges/${btn.dataset.test}/test`); showTestResult(r.results); } catch (e) { toast(e.message, "bad"); } btn.disabled = false; });
  }

  function showTestResult(results) {
    const rows = Object.entries(results).map(([k, v]) => `<div class="ci ${v.ok ? "ok" : "bad"}"><span class="mark">${v.ok ? "✓" : "✗"}</span><span>${DIR[k] || k}</span><span class="muted">${esc(v.error || "")}</span></div>`).join("");
    modal(`<h3>测试结果</h3><div class="checklist">${rows}</div><div class="foot"><button class="btn primary" onclick="document.getElementById('modal-root').innerHTML=''">关闭</button></div>`);
  }

  async function runDiagnose() {
    const m = modal(`<h3>一键诊断</h3><div class="muted"><span class="spin"></span> 正在检查连接、权限与媒体环境…</div>`, { sticky: true });
    try {
      const r = await post("/diagnose");
      m.el.innerHTML = `<h3>一键诊断</h3><div class="checklist">${r.checks.map((c) => `<div class="ci ${c.status === "PASS" ? "ok" : c.status === "WARN" ? "warn" : "bad"}"><span class="mark">${c.status === "PASS" ? "✓" : c.status === "WARN" ? "!" : "✗"}</span><span style="min-width:180px">${esc(c.name)}</span><span class="mono" style="min-width:44px">${c.status}</span><span class="muted small">${esc(c.detail || "")}</span></div>`).join("")}</div><div class="foot"><button class="btn primary" data-x>关闭</button></div>`;
      $("[data-x]", m.el).onclick = m.close;
    } catch (e) { m.close(); toast(e.message, "bad"); }
  }

  // ------------------------------------------------------------ connections
  async function viewConnections() {
    const c = await get("/connections");
    const tg = c.telegram;
    const owner = can("owner");
    const future = `<div class="card glass" style="border-style:dashed">
      <div class="card-head"><h2>更多平台</h2><span class="badge">规划中</span></div>
      <div class="muted" style="line-height:1.9">QQ 接入已经移除。桥接的数据模型是通用的「A 端 ↔ B 端」，未来决定接入的新平台（任意 IM / 频道）只需要新增一个适配器即可复用整套路由、媒体与权限体系。</div>
    </div>`;
    mount(shell(`
      <div class="page-head"><div><h1>连接</h1><div class="sub">一个机器人即可服务任意数量的群组与桥接</div></div></div>
      <div class="grid">
        <div class="card glass">
          <div class="card-head"><h2>Telegram Bot</h2>${connBadge(tg)}</div>
          ${tg.configured ? `<dl class="kv"><dt>Bot</dt><dd>${tg.status.self_name ? "@" + esc(tg.status.self_name) : "-"} ${tg.status.self_id ? "(" + esc(tg.status.self_id) + ")" : ""}</dd><dt>Token</dt><dd class="mono">${esc(tg.config.token_masked)}</dd><dt>API</dt><dd class="mono">${esc(tg.config.api_base || "api.telegram.org")}</dd>${tg.status.last_error ? `<dt>最近错误</dt><dd style="color:var(--bad)">${esc(tg.status.last_error)}</dd>` : ""}</dl><hr class="hr">` : `<div class="alert">在 Telegram 中找 <b>@BotFather</b> 创建机器人并获取 Token。建议在 BotFather 中关闭 <code>/setprivacy</code>（Group Privacy = Disabled），否则机器人收不到普通群消息。</div>`}
          ${owner ? `<form id="ftg">
            <div class="row">
              <div class="field"><label>Bot Token</label><input class="input mono" name="token" placeholder="${tg.configured ? "已保存，留空保持不变" : "123456789:AAH..."}" autocomplete="off"></div>
              <div class="field"><label>Bot API 地址（可选）</label><input class="input mono" name="api_base" value="${esc(tg.config ? tg.config.api_base : "")}" placeholder="https://api.telegram.org"></div>
            </div>
            <div class="actions"><button class="btn primary">验证并保存</button><button class="btn" type="button" data-tgtest>仅验证 Token</button>${tg.configured ? `<button class="btn" type="button" data-restart>重新连接</button><button class="btn danger" type="button" data-del>移除</button>` : ""}</div>
          </form>` : ""}
          <div class="muted small" style="margin-top:14px">保存后，请把机器人添加到目标群组并发送 <code>/bridge</code>，群组会自动出现在「群组」页面。自建 Bot API Server 可突破 50MB 上传限制。</div>
        </div>
        ${future}
      </div>
    `, "#/connections"));
    const ftg = $("#ftg");
    if (ftg) {
      ftg.onsubmit = async (e) => { e.preventDefault(); const fd = Object.fromEntries(new FormData(ftg).entries()); const btn = $("button.primary", ftg); btn.disabled = true; try { const r = await put("/connections/telegram", fd); toast(`已连接 @${r.bot.username}`, "ok"); viewConnections(); } catch (err) { toast(err.message, "bad"); } btn.disabled = false; };
      $("[data-tgtest]").onclick = async () => { try { const r = await post("/connections/telegram/test", { token: fel(ftg, "token").value, api_base: fel(ftg, "api_base").value }); if (r.ok) toast(`✓ @${r.username} (${r.name})`, "ok"); else toast(r.error, "bad"); } catch (err) { toast(err.message, "bad"); } };
      const rs = $("[data-restart]"); if (rs) rs.onclick = async () => { rs.disabled = true; try { await post("/connections/telegram/restart"); toast("正在重新连接…", "ok"); setTimeout(viewConnections, 1500); } catch (e) { toast(e.message, "bad"); rs.disabled = false; } };
      const dl = $("[data-del]"); if (dl) dl.onclick = async () => { if (await confirmDialog("移除连接", "确定移除 Telegram 连接？相关桥接将停止工作。", "移除", true)) { try { await del("/connections/telegram"); viewConnections(); } catch (e) { toast(e.message, "bad"); } } };
    }
    every(8000, () => { if (location.hash === "#/connections" && !document.activeElement.closest("form")) viewConnections(); });
  }

  function connBadge(c) {
    if (!c.configured) return `<span class="badge">未配置</span>`;
    return c.status.connected ? `<span class="badge ok">● Connected</span>` : `<span class="badge bad">● Disconnected</span>`;
  }

  // ------------------------------------------------------------ chats
  async function viewChats() {
    const r = await get("/chats");
    const chats = r.chats;
    state.pendingCount = chats.filter((c) => c.status === "pending").length;
    const owner = can("owner"), admin = can("admin");
    const section = (platform, list) => {
      const label = (list[0] && list[0].platform_label) || platLabel(platform);
      return `<div class="card glass"><div class="card-head"><h2>${esc(label)} 群组 <span class="muted small">${list.length}</span></h2></div>
        ${list.length ? `<div class="table-wrap"><table><thead><tr><th>名称</th><th>ID</th><th>成员</th><th>状态</th><th>桥接</th><th>最近活动</th><th></th></tr></thead><tbody>
        ${list.map((c) => `<tr>
          <td><b>${esc(c.title)}</b>${c.status_reason ? `<div class="muted small">${esc(c.status_reason)}</div>` : ""}${permSummary(c)}</td>
          <td class="mono muted">${esc(c.chat_id)}</td><td>${c.member_count == null ? "-" : c.member_count}</td>
          <td><span class="badge ${STATUS_CLS[c.status] || ""}">${esc(c.status_label)}</span></td>
          <td>${c.bridges.map((b) => `<a href="#/bridges/${b.id}">${esc(b.name)}</a>`).join("<br>") || '<span class="muted">—</span>'}</td>
          <td class="muted small">${fmtAgo(c.last_seen_at)}</td>
          <td><div class="actions" style="justify-content:flex-end">
            ${owner && (c.status === "pending" || c.status === "discovered" || c.status === "rejected" || c.status === "disabled") ? `<button class="btn sm primary" data-st="${c.id}" data-v="authorized">允许</button>` : ""}
            ${owner && (c.status === "pending") ? `<button class="btn sm" data-st="${c.id}" data-v="rejected">拒绝</button>` : ""}
            ${owner && (c.status === "authorized" || c.status === "limited") ? `<button class="btn sm" data-st="${c.id}" data-v="disabled">禁用</button>` : ""}
            ${admin ? `<button class="btn sm" data-check="${c.id}">检测权限</button>` : ""}
            ${owner && !c.bridges.length ? `<button class="btn sm ghost danger" data-rm="${c.id}">删除</button>` : ""}
          </div></td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">尚未发现 ${esc(label)} 群组。请把机器人加入群组，并在群里发送 /bridge。</div>`}
      </div>`;
    };
    const byPlatform = {};
    for (const c of chats) (byPlatform[c.platform] = byPlatform[c.platform] || []).push(c);
    const platforms = Object.keys(byPlatform).length ? Object.keys(byPlatform).sort() : ["telegram"];
    mount(shell(`
      <div class="page-head"><div><h1>群组</h1><div class="sub">机器人所在的群组会自动出现在这里。新群默认<b>不转发</b>，需要创建桥接后才会生效。</div></div>
        <div class="actions">${admin ? `<button class="btn" data-refresh>↻ 刷新群列表</button>` : ""}${owner ? `<button class="btn" data-manual>手动添加</button>` : ""}</div></div>
      ${state.pendingCount ? `<div class="alert warn">有 ${state.pendingCount} 个群组通过 <code>/bridge</code> 申请了绑定，请审核。</div>` : ""}
      ${platforms.map((p) => section(p, byPlatform[p] || [])).join("")}
    `, "#/chats"));
    $$("[data-st]").forEach((b) => b.onclick = async () => { try { await post(`/chats/${b.dataset.st}/status`, { status: b.dataset.v }); toast("已更新", "ok"); viewChats(); } catch (e) { toast(e.message, "bad"); } });
    $$("[data-check]").forEach((b) => b.onclick = async () => { b.disabled = true; try { const r = await post(`/chats/${b.dataset.check}/check`); showPermReport(r); viewChats(); } catch (e) { toast(e.message, "bad"); } b.disabled = false; });
    $$("[data-rm]").forEach((b) => b.onclick = async () => { if (await confirmDialog("删除群组记录", "仅删除记录，机器人不会退群。", "删除", true)) { await del(`/chats/${b.dataset.rm}`); viewChats(); } });
    const rf = $("[data-refresh]"); if (rf) rf.onclick = async () => { rf.disabled = true; try { const r = await post("/chats/refresh"); toast(`已刷新：${Object.entries(r.counts).map(([k, v]) => `${platLabel(k)} ${v}`).join("，") || "无已连接平台"}`, "ok"); viewChats(); } catch (e) { toast(e.message, "bad"); rf.disabled = false; } };
    const mn = $("[data-manual]"); if (mn) mn.onclick = () => {
      const m = modal(`<h3>手动添加群组</h3><form id="fm"><div class="field"><label>平台</label><select class="input" name="platform"><option value="telegram">Telegram</option></select></div><div class="field"><label>群 ID</label><input class="input mono" name="chat_id" required placeholder="Telegram chat id（如 -1001234567890）"></div><div class="field"><label>名称（可选）</label><input class="input" name="title"></div><div class="foot"><button class="btn" type="button" data-x>取消</button><button class="btn primary">添加</button></div></form>`);
      $("[data-x]", m.el).onclick = m.close;
      $("#fm").onsubmit = async (e) => { e.preventDefault(); try { await post("/chats/manual", Object.fromEntries(new FormData(e.target).entries())); m.close(); viewChats(); } catch (err) { toast(err.message, "bad"); } };
    };
  }

  const PERM_LABEL = { send_text: "发送消息", send_photo: "发送图片", send_video: "发送视频", send_audio: "发送音频", send_voice: "发送语音", send_document: "发送文件", send_other: "发送贴纸/动画", delete_messages: "删除消息" };
  function permSummary(c) {
    const p = c.permissions && c.permissions.checks;
    if (!p) return "";
    const missing = Object.entries(p).filter(([k, v]) => !v && k !== "delete_messages").map(([k]) => PERM_LABEL[k] || k);
    return missing.length ? `<div class="small" style="color:var(--warn)">缺少：${esc(missing.join("、"))}</div>` : `<div class="small muted">权限 ✓ ${fmtAgo(c.permissions.checked_at)}</div>`;
  }
  function showPermReport(r) {
    const rows = Object.entries(r.checks || {}).map(([k, v]) => `<div class="ci ${v ? "ok" : "bad"}"><span class="mark">${v ? "✓" : "✗"}</span>${esc(PERM_LABEL[k] || k)}</div>`).join("");
    modal(`<h3>权限检测</h3>${r.present ? "" : `<div class="alert bad">机器人不在该群组</div>`}${r.reason ? `<div class="alert warn">${esc(r.reason)}</div>` : ""}<div class="checklist">${rows || '<div class="muted">无详细权限信息</div>'}</div><div class="foot"><button class="btn primary" onclick="document.getElementById('modal-root').innerHTML=''">关闭</button></div>`);
  }

  // ------------------------------------------------------------ bridges
  async function viewBridges() {
    const r = await get("/bridges");
    mount(shell(`
      <div class="page-head"><div><h1>桥接</h1><div class="sub">一条桥接 = 两个群组之间的消息通道（A ↔ B）。一个机器人可以挂任意多条桥接。</div></div>
        <div class="actions">${can("owner") ? `<a class="btn primary" href="#/bridges/new">＋ 创建桥接</a>` : ""}</div></div>
      ${r.bridges.length ? `<div class="grid cols-2">${r.bridges.map(bridgeCard).join("")}</div>` : `<div class="card glass empty">还没有桥接。<a href="#/bridges/new">创建第一条桥接</a></div>`}
    `, "#/bridges"));
    bindBridgeCardActions(viewBridges);
  }

  async function viewBridgeNew() {
    const r = await get("/chats");
    const chats = r.chats.filter((c) => !["rejected", "disabled"].includes(c.status));
    const w = { step: 1, a: null, b: null, name: "", direction: "both", media: Object.fromEntries(Object.keys(MEDIA_LABEL).map((k) => [k, true])), reply_sync: true, recall_sync: false, edit_sync: false, event_sync: false, display_mode: "", bridgeId: null, verify: null, test: null, enabledNow: false };
    const steps = ["选择 A 端群组", "选择 B 端群组", "桥接选项", "检查与测试", "完成"];
    const choiceList = (list, sel, exclude) => {
      const usable = list.filter((c) => c.id !== exclude);
      return usable.length ? `<div class="choice-list">${usable.map((c) => `<div class="choice ${sel === c.id ? "selected" : ""}" data-pick="${c.id}"><div class="radio"></div><div class="info"><div class="t">${esc(c.title)}</div><div class="s mono">${c.platform_label || platLabel(c.platform)} · ${esc(c.chat_id)}${c.member_count != null ? " · " + c.member_count + " 人" : ""} · ${esc(c.status_label)}</div></div>${c.bridges.length ? `<span class="badge info">已有 ${c.bridges.length} 条桥接</span>` : ""}</div>`).join("")}</div>` : `<div class="empty">没有可用的群组。请先在「连接」页面连接机器人，并在「群组」页面添加群组。</div>`;
    };
    const pairBox = (ca, cb) => `<div class="pair" style="margin-bottom:16px"><div class="side"><div class="plat">A · ${esc(ca.platform_label || platLabel(ca.platform))}</div><div class="name">${esc(ca.title)}</b></div></div><div class="arrow">⇄</div><div class="side" style="text-align:right"><div class="plat">B · ${esc(cb.platform_label || platLabel(cb.platform))}</div><div class="name">${esc(cb.title)}</div></div></div>`;
    const draw = () => {
      const ca = chats.find((c) => c.id === w.a), cb = chats.find((c) => c.id === w.b);
      let body = "";
      if (w.step === 1) body = `<h2 style="margin-bottom:12px">选择 A 端群组</h2>${choiceList(chats, w.a)}<div class="actions" style="margin-top:16px;justify-content:flex-end"><button class="btn primary" data-next ${w.a ? "" : "disabled"}>下一步</button></div>`;
      else if (w.step === 2) body = `<h2 style="margin-bottom:12px">选择 B 端群组</h2>${choiceList(chats, w.b, w.a)}<div class="actions" style="margin-top:16px;justify-content:space-between"><button class="btn" data-prev>上一步</button><button class="btn primary" data-next ${w.b ? "" : "disabled"}>下一步</button></div>`;
      else if (w.step === 3) body = `<h2 style="margin-bottom:12px">桥接选项</h2>
        ${pairBox(ca, cb)}
        <div class="field"><label>名称</label><input class="input" id="w-name" value="${esc(w.name || (ca.title + " ↔ " + cb.title))}"></div>
        <div class="field"><label>消息方向</label><div class="checks">
          <label class="check"><input type="radio" name="dir" value="both" ${w.direction === "both" ? "checked" : ""}> 双向</label>
          <label class="check"><input type="radio" name="dir" value="a_to_b" ${w.direction === "a_to_b" ? "checked" : ""}> 仅 ${esc(ca.title)} → ${esc(cb.title)}</label>
          <label class="check"><input type="radio" name="dir" value="b_to_a" ${w.direction === "b_to_a" ? "checked" : ""}> 仅 ${esc(cb.title)} → ${esc(ca.title)}</label></div></div>
        <div class="field"><label>转发的消息类型</label><div class="checks">${Object.entries(MEDIA_LABEL).map(([k, v]) => `<label class="check"><input type="checkbox" data-media="${k}" ${w.media[k] ? "checked" : ""}> ${v}</label>`).join("")}</div></div>
        <div class="field"><label>显示模式</label><select class="input" id="w-mode"><option value="">跟随全局设置</option><option value="simple">简洁（张三 / 内容）</option><option value="standard">标准（TG · 张三 / 内容）</option><option value="full">完整（含群名与时间）</option></select></div>
        <div class="field"><label>高级</label><div class="checks">
          <label class="check"><input type="checkbox" data-opt="reply_sync" ${w.reply_sync ? "checked" : ""}> 回复同步</label>
          <label class="check"><input type="checkbox" data-opt="recall_sync" ${w.recall_sync ? "checked" : ""}> 撤回同步</label>
          <label class="check"><input type="checkbox" data-opt="edit_sync" ${w.edit_sync ? "checked" : ""}> 编辑同步</label>
          <label class="check"><input type="checkbox" data-opt="event_sync" ${w.event_sync ? "checked" : ""}> 同步入群/退群事件</label></div></div>
        <div class="actions" style="justify-content:space-between"><button class="btn" data-prev>上一步</button><button class="btn primary" data-create>创建并检查</button></div>`;
      else if (w.step === 4) {
        const v = w.verify;
        const side = (label, title, s) => `<div><h3 style="margin-bottom:8px">${label} · ${esc(title)} <span class="badge ${s.ok ? "ok" : "bad"}">${s.ok ? "条件满足" : "有问题"}</span></h3>${s.present === false ? `<div class="alert bad">机器人不在该群</div>` : ""}${s.reason ? `<div class="alert warn">${esc(s.reason)}</div>` : ""}<div class="checklist">${Object.entries(s.checks || {}).filter(([k]) => k !== "delete_messages").map(([k, ok]) => `<div class="ci ${ok ? "ok" : "bad"}"><span class="mark">${ok ? "✓" : "✗"}</span>${esc(PERM_LABEL[k] || k)}</div>`).join("") || '<div class="muted small">无详细信息</div>'}</div></div>`;
        body = `<h2 style="margin-bottom:12px">检查与测试</h2>${v ? `<div class="grid cols-2">${side("A", ca.title, v.a)}${side("B", cb.title, v.b)}</div>${v.ok ? `<div class="alert ok" style="margin-top:14px">✓ 桥接条件满足</div>` : `<div class="alert warn" style="margin-top:14px">部分检查未通过。你仍然可以启用桥接，但相关消息可能发送失败。</div>`}` : `<div class="muted"><span class="spin"></span> 正在检查…</div>`}
          ${w.test ? `<h3 style="margin:16px 0 8px">测试消息</h3><div class="checklist">${Object.entries(w.test).map(([k, r]) => `<div class="ci ${r.ok ? "ok" : "bad"}"><span class="mark">${r.ok ? "✓" : "✗"}</span>${DIR[k] || k}<span class="muted small">${esc(r.error || "")}</span></div>`).join("")}</div>` : ""}
          <div class="actions" style="margin-top:16px;justify-content:space-between"><button class="btn" data-recheck>重新检查</button><div class="actions"><button class="btn" data-sendtest>发送测试消息</button><button class="btn primary" data-enable>启用桥接</button><button class="btn ghost" data-later>稍后启用</button></div></div>`;
      } else body = `<div style="text-align:center;padding:30px 0"><div style="font-size:44px">☂️</div><h2 style="margin:10px 0 6px">桥接已${w.enabledNow ? "启用" : "创建"}</h2><div class="muted">${esc(w.name)}${w.enabledNow ? " 正在运行，消息将开始同步。" : " 已保存但未启用，可随时在桥接页面启用。"}</div><div class="actions" style="justify-content:center;margin-top:20px"><a class="btn primary" href="#/bridges/${w.bridgeId}">管理桥接</a><a class="btn" href="#/bridges">返回列表</a></div></div>`;
      mount(shell(`<div class="page-head"><div><h1>创建桥接</h1></div><a class="btn ghost" href="#/bridges">取消</a></div>
        <div class="steps">${steps.map((s, i) => `<div class="step ${i + 1 === w.step ? "active" : i + 1 < w.step ? "done" : ""}"><span class="n"><span>${i + 1 < w.step ? "✓" : i + 1}</span></span>${s}</div>`).join("")}</div>
        <div class="card glass">${body}</div>`, "#/bridges"));
      $$("[data-pick]").forEach((el) => el.onclick = () => { const id = parseInt(el.dataset.pick, 10); if (w.step === 1) w.a = id; else w.b = id; draw(); });
      const nx = $("[data-next]"); if (nx) nx.onclick = () => { w.step++; draw(); };
      const pv = $("[data-prev]"); if (pv) pv.onclick = () => { w.step--; draw(); };
      const cr = $("[data-create]"); if (cr) cr.onclick = async () => {
        w.name = $("#w-name").value.trim(); w.direction = $("input[name=dir]:checked").value; w.display_mode = $("#w-mode").value;
        $$("[data-media]").forEach((i) => w.media[i.dataset.media] = i.checked); $$("[data-opt]").forEach((i) => w[i.dataset.opt] = i.checked);
        const ok = await confirmDialog("确认创建桥接？", `<dl class="kv"><dt>A 端</dt><dd>${esc(ca.title)}</dd><dt>B 端</dt><dd>${esc(cb.title)}</dd><dt>消息方向</dt><dd>${w.direction === "both" ? "双向" : w.direction === "a_to_b" ? `${esc(ca.title)} → ${esc(cb.title)}` : `${esc(cb.title)} → ${esc(ca.title)}`}</dd><dt>媒体</dt><dd>${Object.values(w.media).every(Boolean) ? "全部" : Object.entries(w.media).filter(([, v]) => v).map(([k]) => MEDIA_LABEL[k]).join("、")}</dd></dl><div class="muted small" style="margin-top:10px">创建后会先检查双方权限并可发送测试消息，确认无误后再启用。</div>`, "确认并检查");
        if (!ok) return;
        cr.disabled = true;
        try {
          const res = await post("/bridges", { name: w.name, a_chat_id: w.a, b_chat_id: w.b, direction: w.direction, enabled: false, options: { media: w.media, reply_sync: w.reply_sync, recall_sync: w.recall_sync, edit_sync: w.edit_sync, event_sync: w.event_sync, display_mode: w.display_mode || null } });
          w.bridgeId = res.id; w.step = 4; draw();
          w.verify = await post(`/bridges/${w.bridgeId}/verify`); draw();
        } catch (e) { toast(e.message, "bad"); cr.disabled = false; }
      };
      const rc = $("[data-recheck]"); if (rc) rc.onclick = async () => { w.verify = null; draw(); w.verify = await post(`/bridges/${w.bridgeId}/verify`); draw(); };
      const st = $("[data-sendtest]"); if (st) st.onclick = async () => { st.disabled = true; try { w.test = (await post(`/bridges/${w.bridgeId}/test`)).results; } catch (e) { toast(e.message, "bad"); } draw(); };
      const en = $("[data-enable]"); if (en) en.onclick = async () => { try { await patch(`/bridges/${w.bridgeId}`, { enabled: true }); w.enabledNow = true; w.step = 5; draw(); } catch (e) { toast(e.message, "bad"); } };
      const lt = $("[data-later]"); if (lt) lt.onclick = () => { w.enabledNow = false; w.step = 5; draw(); };
    };
    draw();
  }

  async function viewBridgeDetail(id) {
    const r = await get(`/bridges/${id}`);
    const b = r.bridge, o = b.options, owner = can("owner"), admin = can("admin");
    const dis = owner ? "" : "disabled";
    const dirName = (d) => d === "both" ? "双向" : d === "a_to_b" ? `仅 A → B（${b.a_title} → ${b.b_title}）` : `仅 B → A（${b.b_title} → ${b.a_title}）`;
    mount(shell(`
      <div class="page-head"><div><span class="crumb"><a href="#/bridges">← 桥接列表</a></span><h1>${esc(b.name)}</h1><div class="sub">#${b.id} · 创建于 ${fmtTime(b.created_at)}</div></div>
        <div class="actions">${admin ? `<button class="btn" data-test>发送测试消息</button><button class="btn" data-verify>检查权限</button><button class="btn ${b.enabled ? "" : "primary"}" data-toggle>${b.enabled ? "停用" : "启用"}</button>` : ""}${owner ? `<button class="btn danger" data-del>删除</button>` : ""}</div></div>
      ${b.warnings.length ? `<div class="alert warn">⚠ ${b.warnings.map(esc).join(" · ")}</div>` : ""}
      <div class="grid cols-2">
        <div class="card glass">
          <div class="card-head"><h2>路由</h2><span class="badge ${b.enabled ? (b.warnings.length ? "warn" : "ok") : ""}">${b.enabled ? "● Running" : "○ Stopped"}</span></div>
          <div class="pair">
            <div class="side"><div class="plat">A · ${esc(b.a_platform_label || platLabel(b.a_platform))}</div><div class="name">${esc(b.a_title)}</div><div class="cid mono">${esc(b.a_chat)}</div><span class="badge ${STATUS_CLS[b.a_status] || ""}">${esc(b.a_status)}</span></div>
            <div class="arrow">${ARROW[b.direction] || "⇄"}</div>
            <div class="side" style="text-align:right"><div class="plat">B · ${esc(b.b_platform_label || platLabel(b.b_platform))}</div><div class="name">${esc(b.b_title)}</div><div class="cid mono">${esc(b.b_chat)}</div><span class="badge ${STATUS_CLS[b.b_status] || ""}">${esc(b.b_status)}</span></div>
          </div>
          <form id="fb" style="margin-top:16px">
            <div class="field"><label>名称</label><input class="input" name="name" value="${esc(b.name)}" ${dis}></div>
            <div class="field"><label>方向</label><div class="checks">${["both", "a_to_b", "b_to_a"].map((d) => `<label class="check"><input type="radio" name="direction" value="${d}" ${b.direction === d ? "checked" : ""} ${dis}> ${esc(dirName(d))}</label>`).join("")}</div></div>
            <div class="field"><label>显示模式</label><select class="input" name="display_mode" ${dis}><option value="">跟随全局设置</option><option value="simple" ${o.display_mode === "simple" ? "selected" : ""}>简洁</option><option value="standard" ${o.display_mode === "standard" ? "selected" : ""}>标准</option><option value="full" ${o.display_mode === "full" ? "selected" : ""}>完整</option></select></div>
            <div class="field"><label>消息类型</label><div class="checks">${Object.entries(MEDIA_LABEL).map(([k, v]) => `<label class="check"><input type="checkbox" data-media="${k}" ${o.media[k] ? "checked" : ""} ${dis}> ${v}</label>`).join("")}</div></div>
            <div class="field"><label>高级</label>
              ${[["reply_sync", "回复同步", "跨端保留回复关系"], ["recall_sync", "撤回同步", "一端撤回后删除另一端上的副本"], ["edit_sync", "编辑同步", "一端编辑消息后向另一端发送 [消息已修改]"], ["event_sync", "群事件同步", "入群 / 退群提示（默认关闭以减少噪音）"]].map(([k, n, d]) => `<div class="toggle-row"><div><div>${n}</div><div class="desc">${d}</div></div><label class="switch"><input type="checkbox" data-opt="${k}" ${o[k] ? "checked" : ""} ${dis}><span class="track"></span></label></div>`).join("")}
            </div>
            ${owner ? `<button class="btn primary">保存</button>` : ""}
          </form>
        </div>
        <div>
          <div class="card glass"><div class="card-head"><h2>今日统计</h2></div><div class="grid cols-3"><div class="stat" style="padding:4px 8px"><div class="label">消息</div><div class="value">${b.today.sent}</div></div><div class="stat" style="padding:4px 8px"><div class="label">媒体</div><div class="value">${b.today.media}</div></div><div class="stat" style="padding:4px 8px"><div class="label">失败</div><div class="value" style="color:${b.today.failed ? "var(--bad)" : "inherit"}">${b.today.failed}</div></div></div><div class="muted small">A → B ${b.today.a_to_b} · B → A ${b.today.b_to_a}</div></div>
          <div class="card glass"><div class="card-head"><h2>最近消息</h2><a class="small" href="#/messages?bridge_id=${b.id}">全部</a></div><div id="recent"><span class="spin"></span></div></div>
        </div>
      </div>
    `, "#/bridges"));
    const fb = $("#fb");
    if (owner) fb.onsubmit = async (e) => {
      e.preventDefault();
      const media = {}; $$("[data-media]", fb).forEach((i) => media[i.dataset.media] = i.checked);
      const opts = { media, display_mode: fel(fb, "display_mode").value || null }; $$("[data-opt]", fb).forEach((i) => opts[i.dataset.opt] = i.checked);
      const dirEl = $("input[name=direction]:checked", fb);
      try { await patch(`/bridges/${id}`, { name: fel(fb, "name").value, direction: dirEl ? dirEl.value : b.direction, options: opts }); toast("已保存", "ok"); viewBridgeDetail(id); } catch (err) { toast(err.message, "bad"); }
    };
    const tg = $("[data-toggle]"); if (tg) tg.onclick = async () => { try { await patch(`/bridges/${id}`, { enabled: !b.enabled }); toast(b.enabled ? "已停用" : "已启用", "ok"); viewBridgeDetail(id); } catch (e) { toast(e.message, "bad"); } };
    const ts = $("[data-test]"); if (ts) ts.onclick = async () => { ts.disabled = true; try { showTestResult((await post(`/bridges/${id}/test`)).results); } catch (e) { toast(e.message, "bad"); } ts.disabled = false; };
    const vf = $("[data-verify]"); if (vf) vf.onclick = async () => { vf.disabled = true; try { const v = await post(`/bridges/${id}/verify`); modal(`<h3>权限检查</h3><div class="grid cols-2">${[["a", b.a_title], ["b", b.b_title]].map(([k, t]) => `<div><h3>${k.toUpperCase()} · ${esc(t)} <span class="badge ${v[k].ok ? "ok" : "bad"}">${v[k].ok ? "OK" : "问题"}</span></h3>${v[k].reason ? `<div class="alert warn small">${esc(v[k].reason)}</div>` : ""}<div class="checklist">${Object.entries(v[k].checks || {}).map(([kk, ok]) => `<div class="ci ${ok ? "ok" : "bad"}"><span class="mark">${ok ? "✓" : "✗"}</span>${esc(PERM_LABEL[kk] || kk)}</div>`).join("")}</div></div>`).join("")}</div><div class="foot"><button class="btn primary" onclick="document.getElementById('modal-root').innerHTML=''">关闭</button></div>`); } catch (e) { toast(e.message, "bad"); } vf.disabled = false; };
    const dl = $("[data-del]"); if (dl) dl.onclick = async () => { if (await confirmDialog("删除桥接", `确定删除「${esc(b.name)}」？消息记录会保留，但不再转发。`, "删除", true)) { await del(`/bridges/${id}`); location.hash = "#/bridges"; } };
    try { const m = await get(`/messages?bridge_id=${id}&limit=15`); $("#recent").innerHTML = m.messages.length ? `<table>${m.messages.map((x) => `<tr><td class="muted small">${fmtTime(x.created_at).slice(5)}</td><td>${x.direction === "a_to_b" ? "A→B" : "B→A"}</td><td>${esc(x.source_user_name || "")}</td><td class="muted">${esc(x.summary)}</td><td><span class="badge ${MSG_STATUS[x.status][1]}">${MSG_STATUS[x.status][0]}</span></td></tr>`).join("")}</table>` : `<div class="empty">暂无消息</div>`; } catch (e) { }
  }

  // ------------------------------------------------------------ messages
  async function viewMessages(query) {
    const params = new URLSearchParams(query || "");
    const status = params.get("status") || "", bridgeId = params.get("bridge_id") || "";
    const r = await get(`/messages?limit=100${status ? "&status=" + status : ""}${bridgeId ? "&bridge_id=" + bridgeId : ""}`);
    const br = await get("/bridges");
    const bname = Object.fromEntries(br.bridges.map((b) => [b.id, b.name]));
    const dirLabel = (m) => {
      const b = br.bridges.find((x) => x.id === m.bridge_id);
      if (!b) return m.direction;
      return m.direction === "a_to_b" ? `${b.a_title} → ${b.b_title}` : `${b.b_title} → ${b.a_title}`;
    };
    mount(shell(`
      <div class="page-head"><div><h1>消息</h1><div class="sub">每条消息的处理状态与耗时。点击行查看技术详情。</div></div>
        <div class="actions"><select class="input" id="f-status" style="width:auto"><option value="">全部状态</option><option value="sent" ${status === "sent" ? "selected" : ""}>成功</option><option value="failed" ${status === "failed" ? "selected" : ""}>失败/重试中</option><option value="pending" ${status === "pending" ? "selected" : ""}>排队</option></select>
        <select class="input" id="f-bridge" style="width:auto"><option value="">全部桥接</option>${br.bridges.map((b) => `<option value="${b.id}" ${String(b.id) === bridgeId ? "selected" : ""}>${esc(b.name)}</option>`).join("")}</select><button class="btn" data-reload>↻</button></div></div>
      <div class="card glass"><div class="table-wrap">${r.messages.length ? `<table><thead><tr><th>#</th><th>时间</th><th>方向</th><th>桥接</th><th>发送者</th><th>内容</th><th>类型</th><th>状态</th><th>耗时</th><th></th></tr></thead><tbody>
        ${r.messages.map((m) => `<tr class="clickable" data-row="${m.id}"><td class="muted">${m.id}</td><td class="muted small">${fmtTime(m.created_at)}</td><td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(dirLabel(m))}</td><td>${esc(bname[m.bridge_id] || "-")}</td><td>${esc(m.source_user_name || "")}</td><td class="muted" style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(m.summary)}</td><td>${MEDIA_LABEL[m.kind] || m.kind}</td><td><span class="badge ${MSG_STATUS[m.status][1]}" title="${esc(m.error || "")}">${MSG_STATUS[m.status][0]}</span>${m.error ? `<div class="small" style="color:var(--bad);max-width:200px">${esc(errorLabel(m.error_code))}</div>` : ""}</td><td class="muted small">${m.duration_ms != null ? (m.duration_ms / 1000).toFixed(2) + "s" : "-"}</td><td>${can("admin") && (m.status === "dead") ? `<button class="btn sm" data-retry="${m.id}">重试</button>` : ""}</td></tr>
        <tr data-detail="${m.id}" style="display:none"><td colspan="10"><div class="timeline">${(m.steps || []).map((s) => `<div class="tl"><span class="muted">+${s.t}s</span><span class="${s.ok ? "ok" : "bad"}">${esc(s.name)}</span><span class="muted">${esc(s.detail || "")}</span></div>`).join("") || '<span class="muted small">无处理记录</span>'}${m.error ? `<div style="margin-top:6px"><b>技术详情：</b><code>${esc(m.error_code || "")}</code> ${esc(m.error)}</div>` : ""}<div class="muted small" style="margin-top:4px">源消息 ${esc(platLabel(m.source_platform))} ${esc(m.source_chat_id)}/${esc(m.source_message_id)} · 尝试 ${m.attempts} 次</div></div></td></tr>`).join("")}</tbody></table>` : `<div class="empty">没有符合条件的消息</div>`}</div></div>
    `, "#/messages"));
    const nav = () => { const s = $("#f-status").value, b = $("#f-bridge").value; location.hash = `#/messages?${s ? "status=" + s + "&" : ""}${b ? "bridge_id=" + b : ""}`; };
    $("#f-status").onchange = nav; $("#f-bridge").onchange = nav; $("[data-reload]").onclick = () => viewMessages(query);
    $$("[data-row]").forEach((tr) => tr.onclick = (e) => { if (e.target.closest("button")) return; const d = $(`[data-detail="${tr.dataset.row}"]`); d.style.display = d.style.display === "none" ? "" : "none"; });
    $$("[data-retry]").forEach((b) => b.onclick = async () => { try { await post(`/messages/${b.dataset.retry}/retry`); toast("已重新排队", "ok"); setTimeout(() => viewMessages(query), 800); } catch (e) { toast(e.message, "bad"); } });
  }

  // ------------------------------------------------------------ logs
  async function viewLogs(query) {
    const params = new URLSearchParams(query || "");
    const cat = params.get("category") || "all", level = params.get("level") || "", q = params.get("q") || "";
    const r = await get(`/logs?category=${cat}&level=${level}&limit=300${q ? "&q=" + encodeURIComponent(q) : ""}`);
    const cats = [["all", "全部"], ["system", "系统"], ["conn", "连接"], ["message", "消息"], ["media", "媒体"], ["error", "错误"], ["security", "安全"]];
    mount(shell(`
      <div class="page-head"><div><h1>日志</h1><div class="sub">凭据已自动脱敏。文件日志位于 logs/bridge.log</div></div>
        <div class="actions"><input class="input" id="l-q" placeholder="搜索…" value="${esc(q)}" style="width:200px"><select class="input" id="l-level" style="width:auto"><option value="">全部级别</option><option value="WARNING" ${level === "WARNING" ? "selected" : ""}>警告及以上</option><option value="ERROR" ${level === "ERROR" ? "selected" : ""}>仅错误</option></select><label class="check"><input type="checkbox" id="l-auto" checked> 自动刷新</label></div></div>
      <div class="card glass"><div class="tabs">${cats.map(([k, v]) => `<div class="tab ${cat === k ? "active" : ""}" data-cat="${k}">${v}</div>`).join("")}</div>
        <div id="loglist">${r.logs.length ? r.logs.map((l) => `<div class="log-line"><span class="muted">${fmtTime(l.ts)}</span><span class="lvl ${l.level}">${l.level}</span><span class="muted">${esc(l.category)}</span><span class="msg">${esc(l.message)}</span></div>`).join("") : `<div class="empty">暂无日志</div>`}</div></div>
    `, "#/logs"));
    const nav = () => { location.hash = `#/logs?category=${$(".tab.active").dataset.cat}&level=${$("#l-level").value}${$("#l-q").value ? "&q=" + encodeURIComponent($("#l-q").value) : ""}`; };
    $$("[data-cat]").forEach((t) => t.onclick = () => { $$(".tab").forEach((x) => x.classList.remove("active")); t.classList.add("active"); nav(); });
    $("#l-level").onchange = nav; $("#l-q").onkeydown = (e) => { if (e.key === "Enter") nav(); };
    every(5000, () => { if (location.hash.startsWith("#/logs") && $("#l-auto") && $("#l-auto").checked && document.activeElement.id !== "l-q") viewLogs(query); });
  }

  // ------------------------------------------------------------ settings
  async function viewSettings() {
    const s = await get("/settings");
    const owner = can("owner");
    const users = owner ? (await get("/users")).users : [];
    const sys = await get("/system");
    const groups = [
      ["显示", ["display_mode", "timezone", "panel_title"]],
      ["性能保护", ["media_workers", "send_workers", "media_timeout_sec", "tmp_quota_mb", "tmp_ttl_min"]],
      ["文件大小策略 (MB)", ["direct_send_limit_mb", "tg_upload_limit_mb", "tg_download_limit_mb"]],
      ["限流", ["tg_rate_per_chat_per_min", "tg_rate_global_per_sec"]],
      ["行为", ["bridge_other_bots", "event_sync_default", "telegram_api_base"]],
      ["保留", ["log_retention_rows", "message_retention_days", "media_cache_days", "session_hours"]],
    ];
    const field = (k) => { const sp = s.schema[k], v = s.settings[k]; if (!sp) return ""; let input;
      if (sp.type === "bool") input = `<label class="switch"><input type="checkbox" name="${k}" ${v ? "checked" : ""} ${owner ? "" : "disabled"}><span class="track"></span></label>`;
      else if (sp.type === "choice") input = `<select class="input" name="${k}" ${owner ? "" : "disabled"}>${sp.choices.map((c) => `<option value="${c}" ${v === c ? "selected" : ""}>${c}</option>`).join("")}</select>`;
      else input = `<input class="input" name="${k}" value="${esc(v)}" ${sp.type === "int" || sp.type === "float" ? `type="number" step="${sp.type === "float" ? "0.1" : "1"}" min="${sp.min}" max="${sp.max}"` : ""} ${owner ? "" : "disabled"}>`;
      return `<div class="toggle-row"><div><div>${esc(sp.label)}</div><div class="desc mono">${k}</div></div><div style="min-width:220px">${input}</div></div>`; };
    mount(shell(`
      <div class="page-head"><div><h1>系统</h1><div class="sub">v${esc(sys.version)} · ${esc(sys.home)} · 监听 ${esc(sys.bind)}</div></div><div class="actions"><button class="btn" data-diag>一键诊断</button><button class="btn" data-pw>修改密码</button></div></div>
      <div class="grid cols-2">
        <div>
          <form id="fs" class="card glass"><div class="card-head"><h2>设置</h2>${owner ? `<button class="btn primary sm">保存</button>` : ""}</div>
            ${groups.map(([g, keys]) => `<div class="group-label">${g}</div>${keys.map(field).join("")}`).join("")}
          </form>
        </div>
        <div>
          ${owner ? `<div class="card glass"><div class="card-head"><h2>用户与权限</h2><button class="btn sm" data-adduser>＋ 添加</button></div>
            <table><thead><tr><th>用户</th><th>角色</th><th>最近登录</th><th></th></tr></thead><tbody>${users.map((u) => `<tr><td>${esc(u.username)}</td><td><span class="badge ${u.role === "owner" ? "info" : ""}">${u.role}</span></td><td class="muted small">${u.last_login_at ? fmtAgo(u.last_login_at) : "-"}</td><td><div class="actions" style="justify-content:flex-end"><button class="btn sm" data-upw="${u.id}">重置密码</button>${u.id !== state.session.user.id ? `<button class="btn sm ghost danger" data-udel="${u.id}">删除</button>` : ""}</div></td></tr>`).join("")}</tbody></table>
            <div class="muted small" style="margin-top:10px">Owner：全部权限 · Admin：启停桥接、检测、重试 · Viewer：只读</div></div>` : ""}
          ${owner ? `<div class="card glass"><div class="card-head"><h2>备份与恢复</h2></div>
            <div class="actions"><a class="btn" href="/api/backup" download>导出配置（不含 Token）</a><a class="btn" href="/api/backup?with_secrets=1" download>导出（含加密凭据）</a><button class="btn" data-restore>从备份恢复…</button></div>
            <div class="muted small" style="margin-top:10px">命令行：<code>qqtg backup</code> / <code>qqtg restore 文件.json</code>；升级前脚本会自动备份数据库。</div></div>` : ""}
          <div class="card glass"><div class="card-head"><h2>关于</h2></div><dl class="kv"><dt>版本</dt><dd>${esc(sys.version)}</dd><dt>Python</dt><dd>${esc(sys.python)}</dd><dt>FFmpeg</dt><dd class="mono">${esc(sys.ffmpeg || "未安装")}</dd><dt>安装目录</dt><dd class="mono">${esc(sys.home)}</dd><dt>服务</dt><dd class="mono">systemctl status qqtg-bridge</dd><dt>更新</dt><dd class="mono">qqtg-install update</dd></dl></div>
        </div>
      </div>
    `, "#/settings"));
    $("[data-diag]").onclick = runDiagnose;
    $("[data-pw]").onclick = () => { const m = modal(`<h3>修改密码</h3><form id="fp"><div class="field"><label>原密码</label><input class="input" type="password" name="old_password" required></div><div class="field"><label>新密码</label><input class="input" type="password" name="new_password" required minlength="8"></div><div class="foot"><button class="btn" type="button" data-x>取消</button><button class="btn primary">保存</button></div></form>`); $("[data-x]", m.el).onclick = m.close; $("#fp").onsubmit = async (e) => { e.preventDefault(); try { await post("/me/password", Object.fromEntries(new FormData(e.target).entries())); m.close(); toast("密码已修改", "ok"); } catch (err) { toast(err.message, "bad"); } }; };
    const fs = $("#fs");
    if (owner) fs.onsubmit = async (e) => { e.preventDefault(); const data = {}; for (const k of Object.keys(s.schema)) { const el = fs.elements[k]; if (!el) continue; data[k] = s.schema[k].type === "bool" ? el.checked : el.value; } try { await put("/settings", data); toast("设置已保存", "ok"); } catch (err) { toast(err.message, "bad"); } };
    const au = $("[data-adduser]"); if (au) au.onclick = () => { const m = modal(`<h3>添加用户</h3><form id="fu"><div class="field"><label>用户名</label><input class="input" name="username" required minlength="3"></div><div class="field"><label>密码</label><input class="input" type="password" name="password" required minlength="8"></div><div class="field"><label>角色</label><select class="input" name="role"><option value="viewer">Viewer（只读）</option><option value="admin">Admin</option><option value="owner">Owner</option></select></div><div class="foot"><button class="btn" type="button" data-x>取消</button><button class="btn primary">创建</button></div></form>`); $("[data-x]", m.el).onclick = m.close; $("#fu").onsubmit = async (e) => { e.preventDefault(); try { await post("/users", Object.fromEntries(new FormData(e.target).entries())); m.close(); viewSettings(); } catch (err) { toast(err.message, "bad"); } }; };
    $$("[data-upw]").forEach((b) => b.onclick = () => { const m = modal(`<h3>重置密码</h3><form id="fr"><div class="field"><label>新密码</label><input class="input" type="password" name="password" required minlength="8"></div><div class="foot"><button class="btn" type="button" data-x>取消</button><button class="btn primary">重置</button></div></form>`); $("[data-x]", m.el).onclick = m.close; $("#fr").onsubmit = async (e) => { e.preventDefault(); try { await post(`/users/${b.dataset.upw}/password`, Object.fromEntries(new FormData(e.target).entries())); m.close(); toast("已重置", "ok"); } catch (err) { toast(err.message, "bad"); } }; });
    $$("[data-udel]").forEach((b) => b.onclick = async () => { if (await confirmDialog("删除用户", "确定删除该用户？", "删除", true)) { try { await del(`/users/${b.dataset.udel}`); viewSettings(); } catch (e) { toast(e.message, "bad"); } } });
    const rs = $("[data-restore]"); if (rs) rs.onclick = () => { const m = modal(`<h3>从备份恢复</h3><div class="field"><label>选择备份文件 (JSON)</label><input type="file" id="rf" accept="application/json"></div><div class="muted small">恢复会合并桥接/群组/设置。Token 需与当前密钥匹配才会恢复，否则请重新输入。</div><div class="foot"><button class="btn" data-x>取消</button><button class="btn primary" data-ok>恢复</button></div>`); $("[data-x]", m.el).onclick = m.close; $("[data-ok]", m.el).onclick = async () => { const f = $("#rf").files[0]; if (!f) return; try { const data = JSON.parse(await f.text()); const r = await post("/restore", data); m.close(); toast(`恢复完成：${Object.entries(r.counts).map(([k, v]) => k + " " + v).join("，")}`, "ok"); } catch (e) { toast(e.message, "bad"); } }; };
  }

  // ------------------------------------------------------------ router
  async function loadSession() {
    const s = await get("/session");
    state.version = s.version;
    state.needsSetup = s.needs_setup;
    state.session = s.authenticated ? s : null;
    state.csrf = s.csrf;
    state.title = s.title || state.title;
    document.title = state.title;
  }

  async function render() {
    clearTimers();
    try {
      if (state.needsSetup) return viewSetup();
      if (!state.session) return viewLogin();
      const hash = location.hash || "#/";
      const [path, query] = hash.split("?");
      if (path === "#/" || path === "#") return await viewDashboard();
      if (path === "#/connections") return await viewConnections();
      if (path === "#/chats") return await viewChats();
      if (path === "#/bridges") return await viewBridges();
      if (path === "#/bridges/new") return await viewBridgeNew();
      if (path.startsWith("#/bridges/")) return await viewBridgeDetail(parseInt(path.split("/")[2], 10));
      if (path === "#/messages") return await viewMessages(query);
      if (path === "#/logs") return await viewLogs(query);
      if (path === "#/settings") return await viewSettings();
      location.hash = "#/";
    } catch (e) {
      if (e.message !== "未登录") { $("#app").innerHTML = `<div class="boot">加载失败：${esc(e.message)}<br><br><button class="btn" onclick="location.reload()">重试</button></div>`; }
    }
  }

  window.addEventListener("hashchange", render);
  loadSession().then(render).catch((e) => { $("#app").innerHTML = `<div class="boot">无法连接到服务：${esc(e.message)}</div>`; });
})();
