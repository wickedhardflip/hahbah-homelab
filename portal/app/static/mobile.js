// Phone shell (/m): five tabs over the APIs the dashboard already uses. Plug state and switching come from the shared store in plugs.js.
// Built with DOM calls only (Plugs.h / Plugs.s), text via textContent. Offline, the service worker answers with the last cached copy and this page says so.
(function () {
  "use strict";
  const P = window.Plugs, { h, s, clear } = P;
  const $ = (id) => document.getElementById(id);
  const ADMIN = window.HOMELAB_ADMIN === true;
  const ICON = {
    home: "M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
    plugs: "M9 2v5M15 2v5M6 7h12v4a6 6 0 0 1-12 0zM12 17v5",
    alerts: "M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.9 1.9 0 0 0 3.4 0",
    ask: "M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12zM8.5 12h.01M12 12h.01M15.5 12h.01",
    more: "M4 6h16M4 12h16M4 18h16",
  };
  const TABS = [["home", "Home"], ["plugs", "Plugs"], ["alerts", "Alerts"], ["ask", "Ask"], ["more", "More"]];
  const ico = (k) => s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, s("path", { d: ICON[k] }));
  const state = { snap: null, presence: null, cached: false, tab: "home", pushOn: false };

  let toastTimer = 0;
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 3000);
  }

  // ---------- data ----------
  async function jget(url) {
    try {
      const r = await fetch(url, { credentials: "same-origin" });
      if (r.status === 401) { location.href = "/login"; return null; }
      if (!r.ok) return null;
      return { data: await r.json(), cached: r.headers.get("X-Hubbah-Cache") === "1" };
    } catch (e) { return null; }
  }
  async function post(url, body) {
    try {
      const r = await fetch(url, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
      let data = null;
      try { data = await r.json(); } catch (e) { /* empty body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: null }; }
  }
  async function refresh() {
    const [a, b] = await Promise.all([jget("/api/snapshot"), jget("/api/presence")]);
    if (a) { state.snap = a.data; state.cached = a.cached; }
    else state.cached = true;   // unreachable and nothing cached either: keep what is on screen
    if (b) state.presence = b.data;
    draw();
  }

  // ---------- derived ----------
  const alerts = () => ((state.snap && state.snap.alerts) || []).filter((a) => a.severity === "crit" || a.severity === "warn")
    .sort((a, b) => (a.severity === "crit" ? 0 : 1) - (b.severity === "crit" ? 0 : 1));
  const openCount = () => alerts().filter((a) => !a.acked_by && !a.maint_label).length;
  function level() {
    const al = alerts().filter((a) => !a.maint_label);
    if (!state.snap) return { cls: "unknown", word: "Checking", head: "Checking" };
    if (al.some((a) => a.severity === "crit")) return { cls: "crit", word: "Danger", head: "Danger" };
    if (al.length) return { cls: "warn", word: "Caution", head: "Caution" };
    return { cls: "good", word: "Good service", head: "Good service" };
  }
  const peopleList = () => (state.presence && !state.presence.stale ? state.presence.people.filter((p) => p.home != null) : []);

  // ---------- views ----------
  function plugTile(p) {
    const st = P.state(p), pend = P.pending[p.id], can = ADMIN && p.online && !pend;
    return h("button", { class: "plug " + st + (ADMIN ? "" : " locked") + (pend ? " busy" : ""), type: "button", role: "switch", "aria-checked": String(!!p.on),
      "aria-disabled": String(!can),
      "aria-label": `${p.name}: ${P.word(p)}${can ? ", tap to turn " + (p.on ? "off" : "on") : ""}`,
      onclick: () => {
        if (!p.online) { toast(`${p.name} is offline`); return; }
        if (!ADMIN) { toast("Only an admin can switch plugs"); return; }
        if (!pend) P.power(p.id, !p.on).then((r) => { if (!r.ok) toast(r.msg || "That did not work"); });
      } }, [h("span", { class: "knob" }), h("b", { text: p.name }), h("span", { class: "state", text: pend ? "..." : P.word(p) }),
      h("small", { text: p.next || (p.online ? "" : "Not reachable") })]);
  }
  function drawHome() {
    const v = $("vHome"), lv = level(), n = openCount();
    clear(v);
    const sub = state.snap ? (n ? `${n} open alert${n === 1 ? "" : "s"}. ` : "Nothing open. ") + `${(state.snap.nodes || []).length} hosts and apps.` : "Waiting for the portal.";
    v.append(h("div", { class: "hero " + lv.cls }, [h("b", { text: lv.head }), h("p", { text: sub })]));
    const ppl = peopleList();
    if (ppl.length) {
      v.append(h("h2", { class: "m" }, ["Who's home", h("a", { href: "/presence", text: "Presence" })]));
      v.append(h("div", { class: "people" }, ppl.map((p) => h("div", { class: "pers " + (p.home ? "home" : "away") }, [
        h("span", { class: "av", "aria-hidden": "true", text: (p.name || "?").slice(0, 1).toUpperCase() }),
        h("div", {}, [h("b", { text: p.name }), h("small", { text: p.home ? "Home" : "Away" })])]))));
    }
    if (P.visible()) {
      v.append(h("h2", { class: "m" }, ["Plugs", h("a", { href: "#plugs", onclick: (e) => { e.preventDefault(); go("plugs"); }, text: "All" })]));
      v.append(h("div", { class: "plugs" }, P.list().filter((p) => p.online).slice(0, 4).map(plugTile)));
    }
  }
  function drawPlugs() {
    const v = $("vPlugs");
    clear(v);
    if (!P.visible()) { v.append(h("p", { class: "empty", text: P.data ? "No smart plugs are set up." : "Loading plugs..." })); return; }
    v.append(h("h2", { class: "m" }, [ADMIN ? "Tap to switch" : "Plugs", h("a", { href: "/floorplan", text: "Floorplan" })]));
    v.append(h("div", { class: "plugs" }, P.list().map(plugTile)));
    if (P.data.stale) v.append(h("p", { class: "empty", text: "Status is out of date" }));
    if (!ADMIN) v.append(h("p", { class: "empty", text: "Only an admin can switch plugs." }));
    v.append(h("div", { class: "list" }, [h("a", { href: "/automations", text: "Rules and schedules" })]));
  }
  async function ack(a, undo) {
    const r = await post(`/api/incidents/${encodeURIComponent(a.kind + ":" + a.target)}/${undo ? "unack" : "ack"}`);
    toast(r.ok ? (undo ? "Acknowledgement removed" : "Acknowledged") : "That did not work");
    refresh();
  }
  async function mute(hours) {
    const r = await post("/api/mute", { hours });
    toast(r.ok ? (hours ? `Muted ${hours} h` : "Alerts on") : "That did not work");
    refresh();
  }
  function drawAlerts() {
    const v = $("vAlerts"), m = (state.snap && state.snap.mute) || {};
    clear(v);
    v.append(h("h2", { class: "m", text: m.muted ? m.label : "Mute alert emails" }));
    v.append(h("div", { class: "mute" }, [[1, "1 h"], [4, "4 h"], [24, "24 h"], [0, "Unmute"]].map(([hr, l]) =>
      h("button", { class: "btn" + (!hr && m.muted ? " on" : ""), type: "button", onclick: () => mute(hr), text: l }))));
    v.append(h("h2", { class: "m", text: "Alerts" }));
    const al = alerts();
    if (!al.length) v.append(h("p", { class: "empty", text: state.snap ? "All clear." : "Waiting for the portal." }));
    al.forEach((a) => v.append(h("div", { class: "card alert " + a.severity + (a.acked_by ? " acked" : "") }, [
      h("div", { class: "hd", text: `${a.severity === "crit" ? "Danger" : "Caution"}: ${a.target}` }),
      h("p", { text: a.message + (a.acked_by ? ` Acknowledged by ${a.acked_by}.` : "") + (a.maint_label ? " " + a.maint_label + "." : "") }),
      a.incident ? h("button", { class: "btn", type: "button", onclick: () => ack(a, !!a.acked_by), text: a.acked_by ? "Undo acknowledge" : "Acknowledge" })
        : h("small", { text: "Opens as an incident after two checks." })])));
    v.append(h("div", {}, [h("a", { class: "btn", href: "/incidents", style: "display:inline-block;text-decoration:none", text: "All incidents" })]));
  }
  function drawAsk() {
    const v = $("vAsk");
    clear(v);
    v.append(h("div", { class: "ask" }, [h("p", { text: "Ask hubbah about your network, plugs and who is home." }), h("a", { href: "/ask", text: "Open Ask hubbah" })]));
  }

  // ---------- notifications (web push) ----------
  const b64 = (k) => { const p = "=".repeat((4 - (k.length % 4)) % 4), raw = atob((k + p).replace(/-/g, "+").replace(/_/g, "/")); return Uint8Array.from(raw, (c) => c.charCodeAt(0)); };
  async function pushStatus() {
    if (!window.HB_PUSH || !("serviceWorker" in navigator) || !("PushManager" in window)) return;
    try { const reg = await navigator.serviceWorker.ready; state.pushOn = !!(await reg.pushManager.getSubscription()) && Notification.permission === "granted"; } catch (e) { /* keep off */ }
  }
  async function togglePush() {
    try {
      const reg = await navigator.serviceWorker.ready, cur = await reg.pushManager.getSubscription();
      if (cur) {
        await post("/api/push/unsubscribe", cur.toJSON());
        await cur.unsubscribe();
        state.pushOn = false; toast("Notifications off");
      } else {
        if ((await Notification.requestPermission()) !== "granted") { toast("Notifications were not allowed"); return; }
        const cfg = await jget("/api/push/config");
        if (!cfg || !cfg.data.enabled) { toast("Notifications are not set up"); return; }
        const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64(cfg.data.key) });
        const r = await post("/api/push/subscribe", sub.toJSON());
        if (!r.ok) { await sub.unsubscribe(); toast("Could not turn on notifications"); return; }
        state.pushOn = true; toast("Notifications on");
      }
    } catch (e) { toast("Notifications need the app added to the Home Screen"); }
    drawMore();
  }
  async function testPush() {
    const r = await post("/api/push/test");
    toast(r.ok ? `Sent to ${r.data.sent} device${r.data.sent === 1 ? "" : "s"}` : "That did not work");
  }
  function drawMore() {
    const v = $("vMore");
    clear(v);
    const link = (href, text) => h("a", { href, text });
    const rows = [link("/floorplan", "Floorplan"), link("/presence", "Presence"), link("/automations", "Automations"), link("/incidents", "All incidents"),
      link("/maintenance", "Maintenance"), link("/profile", "Profile"), link("/about", "About")];
    if (ADMIN) rows.push(link("/settings", "Settings"));
    v.append(h("div", { class: "list" }, rows));
    v.append(h("h2", { class: "m", text: "This phone" }));
    const mine = [];
    if (window.HB_PUSH && "PushManager" in window) {
      mine.push(h("button", { class: "row", type: "button", role: "switch", "aria-checked": String(state.pushOn), onclick: togglePush }, ["Danger alert notifications", h("small", { text: state.pushOn ? "On" : "Off" })]));
      if (ADMIN && state.pushOn) mine.push(h("button", { class: "row", type: "button", onclick: testPush, text: "Send a test notification" }));
    }
    mine.push(link("/view/desktop", "Desktop view"));
    mine.push(h("form", { method: "post", action: "/logout", onsubmit: () => {
      const c = navigator.serviceWorker && navigator.serviceWorker.controller;
      if (c) c.postMessage("clear");   // forget cached data before the next person uses this phone
    } }, [h("button", { class: "row", type: "submit", text: "Sign out" })]));
    v.append(h("div", { class: "list" }, mine));
    if (window.HB_PUSH && !window.matchMedia("(display-mode: standalone)").matches)
      v.append(h("p", { class: "empty", text: "To get notifications on iPhone, add this page to the Home Screen first (iOS 16.4 or newer)." }));
  }

  // ---------- shell ----------
  function drawBar() {
    const lv = level(), n = openCount();
    $("led").className = "led " + (lv.cls === "good" ? "" : lv.cls);
    $("stword").textContent = lv.word;
    const note = $("notice");
    note.hidden = !state.cached;
    if (state.cached) note.textContent = "Offline. Showing the last known status.";
    const nav = $("tabbar");
    clear(nav);
    TABS.forEach(([k, l]) => nav.append(h("button", { role: "tab", type: "button", "aria-selected": String(k === state.tab), "aria-controls": "v" + l, onclick: () => go(k) },
      [ico(k), l, k === "alerts" && n ? h("span", { class: "badge", text: n }) : null])));
  }
  const DRAW = { home: drawHome, plugs: drawPlugs, alerts: drawAlerts, ask: drawAsk, more: drawMore };
  function draw() {
    drawBar();
    DRAW[state.tab]();
  }
  function go(k) {
    state.tab = k;
    document.querySelectorAll(".view").forEach((v) => { v.hidden = v.dataset.tab !== k; });
    draw();
    try { history.replaceState(null, "", "#" + k); } catch (e) { /* ignore */ }
  }
  P.on(() => { if (state.tab === "home" || state.tab === "plugs") draw(); });   // plug polls and taps redraw only where plugs show

  const start = location.hash.slice(1);
  state.tab = DRAW[start] ? start : "home";
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.dataset.tab !== state.tab; });
  window.addEventListener("hashchange", () => { const k = location.hash.slice(1); if (DRAW[k] && k !== state.tab) go(k); });
  draw();
  let timer = 0;
  const tick = () => { clearTimeout(timer); refresh().finally(() => { timer = setTimeout(tick, document.hidden ? 300000 : 30000); }); };
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
  tick();
  pushStatus().then(() => { if (state.tab === "more") drawMore(); });
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => { /* no offline copy; the page still works */ });
})();
