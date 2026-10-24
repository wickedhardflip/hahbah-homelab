// Smart plugs: one shared store over GET /api/plugs, used by the home-page strip (below), the NOC section (noc.js) and /floorplan.
// Polls every 10 s while the tab is visible and every 60 s while hidden. Writes are admin-only on the server; the buttons here only mirror that.
// Built with DOM calls only, no innerHTML.
(function () {
  "use strict";
  const SVGNS = "http://www.w3.org/2000/svg";
  function build(ns, tag, attrs, kids) {
    const el = ns ? document.createElementNS(ns, tag) : document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "href" && !/^(https?:\/\/|\/(?![\/\\]))/i.test(v)) continue;   // same rule as the dashboard helper
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of [].concat(kids || [])) if (c != null) el.append(c);
    return el;
  }
  const h = (t, a, k) => build(null, t, a, k);
  const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); };

  const P = { data: null, error: "", pending: {}, admin: !!window.HOMELAB_ADMIN, h, s: (t, a, k) => build(SVGNS, t, a, k), clear };
  const subs = [];
  P.on = (fn) => subs.push(fn);
  const emit = () => subs.forEach((fn) => { try { fn(P); } catch (e) { /* one listener must not stop the others */ } });
  P.list = () => (P.data && P.data.plugs) || [];
  P.byId = (id) => P.list().find((p) => p.id === id);
  P.visible = () => !!(P.data && P.data.configured && P.list().length);
  P.state = (p) => (!p.online ? "offline" : p.on ? "on" : "off");
  P.word = (p) => ({ offline: "Offline", on: "On", off: "Off" })[P.state(p)];
  P.when = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }); };
  P.cmdText = (c) => c ? `${c.action} ${c.by ? "by " + c.by + " " : ""}${P.when(c.at)}${c.ok === false ? ", failed" : ""}${c.error ? ": " + c.error : ""}` : "none yet";

  const WHY = { 401: "Please sign in again.", 403: "Only an admin can change plugs.", 404: "That plug is not known.", 429: "One command at a time, try again in a moment." };
  async function api(method, url, body) {
    try {
      const r = await fetch(url, { method, credentials: "same-origin", cache: "no-store",
        headers: body === undefined ? {} : { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
      if (r.status === 401 && method === "GET") { location.href = "/login"; return { ok: false, status: 401, msg: "" }; }
      let data = null;
      try { data = await r.json(); } catch (e) { /* 202 or an empty body */ }
      let msg = "";
      if (!r.ok) {
        const d = data && (data.detail || data.message);
        msg = typeof d === "string" ? d : Array.isArray(d) && d[0] && d[0].msg ? d[0].msg : WHY[r.status] || "That did not work. Try again.";
      }
      return { ok: r.ok, status: r.status, data, msg };
    } catch (e) { return { ok: false, status: 0, msg: "Can't reach the portal. Check your connection." }; }
  }
  P.api = api;

  P.poll = async () => {
    const r = await api("GET", "/api/plugs");
    if (!r.ok || !r.data) return;   // offline or restarting: keep the last data
    P.data = r.data;
    const now = Date.now();
    for (const [id, pend] of Object.entries(P.pending)) {
      const p = P.byId(id);
      if (!p || p.on === pend.want || now - pend.at > 20000) delete P.pending[id];
    }
    emit();
  };

  // Optimistic: show "..." right away, then let the next polls (a few seconds later, after the collector ran the command) settle it.
  async function command(id, what, body, want) {
    P.error = "";
    if (want !== undefined) P.pending[id] = { want, at: Date.now() };
    emit();
    const r = await api("POST", `/api/plugs/${encodeURIComponent(id)}/${what}`, body);
    if (!r.ok) { delete P.pending[id]; P.error = r.msg; emit(); return r; }
    setTimeout(P.poll, 2500); setTimeout(P.poll, 6000);
    return r;
  }
  P.power = (id, on) => command(id, "power", { on }, on);
  P.away = (id, on) => command(id, "away", { on });
  P.saveConfig = async (id, cfg) => {
    const r = await api("PUT", `/api/plugs/${encodeURIComponent(id)}/config`, cfg);
    if (r.ok) await P.poll();
    return r;
  };

  let timer = 0;
  const tick = () => { clearTimeout(timer); P.poll().finally(() => { timer = setTimeout(tick, document.hidden ? 60000 : 10000); }); };
  document.addEventListener("visibilitychange", () => { clearTimeout(timer); if (document.hidden) timer = setTimeout(tick, 60000); else tick(); });

  // ---------- the strip under the header (Map, 3D and NOC share this one element) ----------
  const box = document.getElementById("plugstrip");
  function chip(p) {
    const st = P.state(p), pend = P.pending[p.id], name = p.name || p.nickname || p.id;
    const can = P.admin && p.online && !pend;
    return h("span", { class: "ps-chip " + st }, [
      h("i", { class: "ps-dot", "aria-hidden": "true" }),
      h("span", { class: "ps-name", text: name }),
      h("button", { type: "button", class: "ps-btn", role: "switch", "aria-checked": String(!!p.on), "data-id": p.id, disabled: !can,
        "aria-label": `${name}: ${P.word(p)}${p.online ? (P.admin ? ", press to turn " + (p.on ? "off" : "on") : "") : ""}`,
        title: !p.online ? "Offline" : P.admin ? "" : "Only an admin can switch plugs",
        onclick: () => P.power(p.id, !p.on), text: pend ? "..." : p.online ? (p.on ? "On" : "Off") : "Offline" })
    ]);
  }
  function strip() {
    if (!box) return;
    if (!P.visible()) { box.hidden = true; return; }
    const f = document.activeElement, fid = f && box.contains(f) ? f.dataset.id : null;   // keep keyboard focus across the 10 s redraw
    clear(box);
    box.hidden = false;
    box.append(h("span", { class: "ps-title", text: "Smart plugs" }));
    P.list().forEach((p) => box.append(chip(p)));
    if (P.data.stale) box.append(h("span", { class: "ps-note", text: "Status is out of date" }));
    if (P.error) box.append(h("span", { class: "ps-note bad", role: "status", text: P.error }));
    box.append(h("a", { class: "ps-link", href: "/floorplan", text: "Floorplan" }));
    if (fid) { const b = [...box.querySelectorAll(".ps-btn")].find((x) => x.dataset.id === fid); if (b && !b.disabled) b.focus(); }
  }
  P.on(strip);

  window.Plugs = P;
  tick();
})();
