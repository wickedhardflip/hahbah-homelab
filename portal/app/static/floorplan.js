// /floorplan: the apartment drawing with devices pinned on it. Data: GET /api/floorplan (layout + what can be placed) and the shared
// plug store in plugs.js. Pins are SVG over the <img>, positioned by fractions (0..1) of the image; sizes are screen pixels so they stay tappable.
// Edit mode (admin) drags pins with pointer events and saves the whole layout with PUT /api/floorplan. DOM calls only, no innerHTML.
(function () {
  "use strict";
  const P = window.Plugs, h = P.h, s = P.s, clear = P.clear;
  const ADMIN = !!window.HOMELAB_ADMIN;
  const $ = (id) => document.getElementById(id);
  const img = $("fpImg"), svg = $("fpSvg"), side = $("fpSide"), bar = $("fpBar"), msg = $("fpMsg"), linkBar = $("fpLinkBar"), frame = $("fpFrame"), empty = $("fpEmpty");
  if (!img) return;   // no drawing in the media folder: the page already shows its empty state

  const MAX_ITEMS = 80, MAX_LINKS = 160, MAX_LABEL = 40;
  const KIND_NAME = { plug: "Smart plug", eero: "Wi-Fi node", central: "Central server", nas: "NAS", custom: "Custom item" };
  const LINKS = { power: { name: "Power", color: "#e27b0c", dash: "" }, wifi: { name: "Wi-Fi", color: "#0b7b7e", dash: "8 6" },
    wired: { name: "Wired", color: "#2a2a2a", dash: "" }, other: { name: "Other", color: "#7a838c", dash: "1 6" } };
  const ICONS = {   // 24x24 line icons
    plug: "M9 2v5M15 2v5M6 7h12v4a6 6 0 0 1-12 0zM12 17v5", wifi: "M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0M12 19.5h.01",
    server: "M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01", lamp: "M9 3h6l3 9H6zM12 12v8M8 21h8", tree: "M12 3l6 8h-3l5 7H4l5-7H6zM12 18v3",
    tv: "M3 5h18v12H3zM8 21h8M12 17v4", fan: "M12 12c0-5 1-8 4-8s3 4-4 8zM12 12c5 0 8 1 8 4s-4 3-8-4zM12 12c0 5-1 8-4 8s-3-4 4-8zM12 12c-5 0-8-1-8-4s4-3 8 4z",
    heater: "M5 5h14v14H5zM9 8v8M12 8v8M15 8v8", router: "M4 14h16v6H4zM8 17h.01M12 14V9M8 11a5 5 0 0 1 8 0", speaker: "M7 3h10v18H7zM12 8h.01M12 17a2 2 0 1 0 0-.01",
    other: "M12 3l9 9-9 9-9-9z" };
  const CUSTOM_ICONS = ["lamp", "tree", "tv", "fan", "heater", "router", "speaker", "other"];
  const KIND_ICON = { plug: "plug", eero: "wifi", central: "server", nas: "server" };
  const DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"], DAY_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const icon = (name) => s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, [s("path", { d: ICONS[name] || ICONS.other })]);

  let layout = { items: [], links: [] }, avail = [], saved = "";   // saved = the layout as the server has it (JSON text), for Cancel and the dirty check
  let titleEl = null, editing = false, linkMode = false, linkFrom = null, linkPair = null, sel = null, selLink = -1, hover = null, drag = null, ctlBox = null;
  let W = 0, Hh = 0, R = 14, linkEls = [], justDragged = false;
  const dirty = () => JSON.stringify(layout) !== saved;
  const say = (t, bad) => { msg.textContent = t || ""; msg.classList.toggle("bad", !!bad); };

  // ---------- what an item is called and looks like ----------
  const placed = (id) => layout.items.find((i) => i.id === id);
  const availOf = (id) => avail.find((a) => a.id === id);
  function info(it) {
    if (it.kind === "plug") {
      const p = P.byId(it.id.slice(5)), a = availOf(it.id);
      return { label: p ? p.name || p.nickname : a ? a.label : "Smart plug", detail: "", plug: p };
    }
    if (it.kind === "custom") return { label: it.label || "Item", detail: "" };
    const a = availOf(it.id);
    return { label: a ? a.label : it.label || KIND_NAME[it.kind] || "Item", detail: a ? a.detail || "" : "" };
  }
  const iconOf = (it) => (it.kind === "custom" ? (CUSTOM_ICONS.includes(it.icon) ? it.icon : "other") : KIND_ICON[it.kind] || "other");
  const ariaOf = (it, inf) => `${inf.label}, ${inf.plug ? P.word(inf.plug) : KIND_NAME[it.kind] || "item"}${inf.detail ? ", " + inf.detail : ""}`;

  // ---------- drawing ----------
  function draw() {
    W = img.clientWidth; Hh = img.clientHeight;
    if (!W || !Hh || drag) return;
    const f = document.activeElement, fid = f && svg.contains(f) ? f.dataset.id : null;   // keep keyboard focus across a redraw
    svg.setAttribute("viewBox", `0 0 ${W} ${Hh}`);
    svg.classList.toggle("edit", editing); svg.classList.toggle("linking", linkMode);
    svg.classList.toggle("compact", W < 560);   // phone width: only the selected pin shows its name, so labels never cover other pins
    clear(svg);
    R = clamp(W * 0.018, 12, 18);
    svg.append(s("rect", { width: W, height: Hh, fill: "transparent", onclick: () => { if (!linkMode) pick(null); } }));
    linkEls = [];
    const lg = s("g");
    layout.links.forEach((l, i) => {
      if (!placed(l.a) || !placed(l.b) || !LINKS[l.kind]) return;
      const L = LINKS[l.kind], g = s("g", { class: "lk" }), line = (cls, extra) => s("line", { class: cls, ...extra });
      const els = [line("hit", {}), line("under", {}), line("main", { stroke: L.color, "stroke-dasharray": L.dash || null })];
      els[0].addEventListener("click", () => { if (editing && !linkMode) pick(null, i); });
      els.forEach((e) => g.append(e));
      linkEls.push({ a: l.a, b: l.b, i, g, els });
      lg.append(g);
      placeLink(linkEls[linkEls.length - 1]);
    });
    svg.append(lg);
    layout.items.forEach((it) => pin(it));
    empty.hidden = layout.items.length > 0;
    highlight();
    if (fid) { const el = [...svg.querySelectorAll(".pin")].find((e) => e.dataset.id === fid); if (el) el.focus(); }
  }
  function placeLink(le) {
    const a = placed(le.a), b = placed(le.b);
    le.els.forEach((e) => { e.setAttribute("x1", a.x * W); e.setAttribute("y1", a.y * Hh); e.setAttribute("x2", b.x * W); e.setAttribute("y2", Hh * b.y); });
  }
  function highlight() {
    const on = hover || sel;
    svg.classList.toggle("dim", !!on || selLink >= 0);
    linkEls.forEach((le) => { le.g.classList.toggle("hl", !!on && (le.a === on || le.b === on)); le.g.classList.toggle("sel", le.i === selLink); });
  }
  function pin(it) {
    const inf = info(it), st = inf.plug ? P.state(inf.plug) : "";
    const g = s("g", { class: `pin ${it.kind} ${st}${sel === it.id ? " sel" : ""}${linkFrom === it.id ? " src" : ""}`, transform: `translate(${it.x * W} ${it.y * Hh})`,
      tabindex: "0", role: "button", "data-id": it.id, "aria-label": ariaOf(it, inf) });
    const ic = R * 0.58;
    g.append(s("circle", { r: Math.max(R + 4, 22), fill: "transparent" }), s("circle", { class: "halo", r: R + 2.5 }), s("circle", { class: "body", r: R }),
      s("circle", { class: "ring", r: R + 6 }),
      s("path", { class: "ico", d: ICONS[iconOf(it)], transform: `translate(${-ic} ${-ic}) scale(${(ic * 2) / 24})`, "stroke-width": 2.4 }));
    g.addEventListener("pointerenter", () => { hover = it.id; highlight(); });
    g.addEventListener("pointerleave", () => { hover = null; highlight(); });
    g.addEventListener("focus", () => { hover = it.id; highlight(); });
    g.addEventListener("blur", () => { hover = null; highlight(); });
    g.addEventListener("click", () => { if (justDragged) { justDragged = false; return; } pinClick(it); });
    g.addEventListener("keydown", (e) => pinKey(e, it, g));
    g.addEventListener("pointerdown", (e) => dragStart(e, it, g));
    g.addEventListener("pointermove", (e) => dragMove(e, it, g));
    g.addEventListener("pointerup", () => dragEnd(g));
    g.addEventListener("pointercancel", () => dragEnd(g));
    svg.append(g);
    label(g, it, inf);   // after it is in the page, so the text can be measured
  }
  function label(g, it, inf) {
    const sub = inf.detail && it.kind !== "plug" ? inf.detail : "";
    const lab = s("g", { class: "lab" }), t1 = s("text", { "text-anchor": "middle", text: inf.label }), t2 = sub ? s("text", { class: "sub", "text-anchor": "middle", text: sub }) : null;
    lab.append(t1); if (t2) lab.append(t2);
    g.append(lab);
    const w = Math.ceil(Math.max(t1.getComputedTextLength(), t2 ? t2.getComputedTextLength() : 0)) + 14, ht = t2 ? 34 : 21, x = it.x * W;
    const dx = clamp(x - w / 2, 4, W - 4 - w) - (x - w / 2);                                    // keep the pill inside the drawing
    const top = it.y * Hh + R + 5 + ht > Hh - 2 ? -(R + 5 + ht) : R + 5;                         // below the pin, or above near the bottom edge
    lab.insertBefore(s("rect", { x: -w / 2 + dx, y: top, width: w, height: ht, rx: 6 }), t1);
    t1.setAttribute("x", dx); t1.setAttribute("y", top + 14.5);
    if (t2) { t2.setAttribute("x", dx); t2.setAttribute("y", top + 28); }
  }

  // ---------- picking and moving ----------
  function pick(id, link) {
    sel = id; selLink = link == null ? -1 : link;
    highlight();
    svg.querySelectorAll(".pin").forEach((e) => e.classList.toggle("sel", e.dataset.id === id));
    renderSide();
  }
  function pinClick(it) {
    if (editing && linkMode) return linkClick(it);
    pick(sel === it.id && !editing ? null : it.id);
    if (!editing && window.matchMedia("(max-width: 1000px)").matches) side.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  function move(it, g, x, y) {
    it.x = clamp(x, 0, 1); it.y = clamp(y, 0, 1);
    g.setAttribute("transform", `translate(${it.x * W} ${it.y * Hh})`);
    linkEls.forEach((le) => { if (le.a === it.id || le.b === it.id) placeLink(le); });
    setDirty();
  }
  function dragStart(e, it, g) {
    if (!editing || linkMode || (e.button != null && e.button !== 0)) return;
    drag = { id: it.id, sx: e.clientX, sy: e.clientY, moved: false };
    try { g.setPointerCapture(e.pointerId); } catch (err) { /* pointer already gone */ }
  }
  function dragMove(e, it, g) {
    if (!drag || drag.id !== it.id) return;
    if (!drag.moved && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < 4) return;
    if (!drag.moved) { drag.moved = true; g.classList.add("drag"); }
    const r = svg.getBoundingClientRect();
    move(it, g, (e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height);
  }
  function dragEnd(g) {
    if (!drag) return;
    justDragged = drag.moved;   // the click that follows a drag must not select/toggle
    g.classList.remove("drag");
    const id = drag.id, moved = drag.moved;
    drag = null;
    setTimeout(() => { justDragged = false; }, 0);
    if (moved) pick(id);
  }
  function pinKey(e, it, g) {
    if (editing && !linkMode && e.key.startsWith("Arrow")) {
      e.preventDefault();
      const d = e.shiftKey ? 0.05 : 0.01;
      move(it, g, it.x + (e.key === "ArrowRight" ? d : e.key === "ArrowLeft" ? -d : 0), it.y + (e.key === "ArrowDown" ? d : e.key === "ArrowUp" ? -d : 0));
    } else if (editing && !linkMode && (e.key === "Delete" || e.key === "Backspace")) { e.preventDefault(); removeItem(it.id); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pinClick(it); }
  }

  // ---------- edit mode ----------
  function setDirty() { renderBar(); }
  function enterEdit() { editing = true; sel = null; selLink = -1; say(""); renderAll(); }
  function leaveEdit() { editing = false; linkMode = false; linkFrom = null; linkPair = null; sel = null; selLink = -1; renderAll(); }
  function cancelEdit() {
    if (dirty() && !confirm("Discard your layout changes?")) return;
    layout = JSON.parse(saved);
    leaveEdit();
  }
  async function save() {
    say("Saving...");
    const r = await P.api("PUT", "/api/floorplan", layout);
    if (!r.ok) return say(r.msg, true);
    await load();
    leaveEdit();
    say("Layout saved.");
  }
  function addItem(entry, x, y) {
    if (layout.items.length >= MAX_ITEMS) return say(`The map holds up to ${MAX_ITEMS} items.`, true);
    const n = layout.items.length, it = { id: entry.id, kind: entry.kind, x: x == null ? clamp(0.5 + (n % 6) * 0.03 - 0.07, 0, 1) : x, y: y == null ? clamp(0.5 + (n % 6) * 0.03 - 0.07, 0, 1) : y };
    if (entry.kind === "custom") { it.label = entry.label; it.icon = entry.icon; }
    layout.items.push(it);
    sel = it.id; selLink = -1;
    renderAll();
    return it;
  }
  function removeItem(id) {
    layout.items = layout.items.filter((i) => i.id !== id);
    layout.links = layout.links.filter((l) => l.a !== id && l.b !== id);
    if (sel === id) sel = null;
    renderAll();
  }
  const itemName = (id) => { const it = placed(id); return it ? info(it).label : id; };
  function linkClick(it) {
    if (!linkFrom) { linkFrom = it.id; linkPair = null; }
    else if (linkFrom !== it.id) linkPair = [linkFrom, it.id];
    renderAll();
  }
  function addLink(kind) {
    const [a, b] = linkPair, old = layout.links.find((l) => (l.a === a && l.b === b) || (l.a === b && l.b === a));
    if (old) old.kind = kind;
    else if (layout.links.length >= MAX_LINKS) return say(`The map holds up to ${MAX_LINKS} links.`, true);
    else layout.links.push({ a, b, kind });
    linkMode = false; linkFrom = null; linkPair = null;
    renderAll();
  }

  // ---------- toolbar, link bar, legend ----------
  function renderBar() {
    clear(bar);
    if (!ADMIN) return;
    if (!editing) { bar.append(h("button", { type: "button", class: "btn", text: "Edit layout", onclick: enterEdit })); return; }
    bar.append(h("button", { type: "button", class: "btn", "aria-pressed": String(linkMode), text: "Draw a link",
      onclick: () => { linkMode = !linkMode; linkFrom = null; linkPair = null; renderAll(); } }));
    if (dirty()) bar.append(h("span", { class: "status", text: "Unsaved changes" }));
    bar.append(h("button", { type: "button", class: "btn", text: "Cancel", onclick: cancelEdit }),
      h("button", { type: "button", class: "btn primary", text: "Save layout", disabled: !dirty(), onclick: save }));
  }
  function renderLinkBar() {
    clear(linkBar);
    linkBar.hidden = !(editing && linkMode);
    if (linkBar.hidden) return;
    const cancel = h("button", { type: "button", class: "btn small", text: "Stop", onclick: () => { linkMode = false; linkFrom = null; linkPair = null; renderAll(); } });
    if (linkPair) {
      linkBar.append(h("span", { text: `Link ${itemName(linkPair[0])} to ${itemName(linkPair[1])}:` }),
        ...Object.entries(LINKS).map(([k, L]) => h("button", { type: "button", class: "btn small", text: L.name, onclick: () => addLink(k) })), cancel);
    } else linkBar.append(h("span", { role: "status", text: linkFrom ? `From ${itemName(linkFrom)}. Now choose the second pin.` : "Choose the first pin." }), cancel);
  }
  function renderLegend() {
    const lg = $("fpLegend");
    clear(lg);
    [["on", "On"], ["", "Off"], ["offline", "Offline"]].forEach(([c, t]) => lg.append(h("span", {}, [h("i", { class: "sw " + c }), t])));
    Object.values(LINKS).forEach((L) => lg.append(h("span", {}, [
      s("svg", { viewBox: "0 0 30 8", "aria-hidden": "true" }, [s("line", { x1: 2, y1: 4, x2: 28, y2: 4, stroke: L.color, "stroke-width": 3, "stroke-linecap": "round", "stroke-dasharray": L.dash || null })]), L.name])));
  }

  // ---------- side panel ----------
  const dayText = (d) => (d.length === 7 ? "Every day" : d.join() === "0,1,2,3,4" ? "Weekdays" : d.join() === "5,6" ? "Weekends" : d.map((i) => DAYS[i]).join(" "));
  const schedText = (sc) => `${dayText([...sc.days].sort())} at ${sc.time}, turn ${sc.action}${sc.enabled ? "" : " (paused)"}`;
  function renderSide() {
    clear(side); ctlBox = null;
    if (editing) return editPanel();
    const it = sel && placed(sel);
    if (!it) { side.append(h("p", { class: "hint", text: layout.items.length ? "Select a pin to see details." : "Nothing is placed yet." })); return; }
    const inf = info(it);
    if (it.kind !== "plug" || !inf.plug) {
      side.append(h("h2", { text: inf.label }), h("p", { class: "sub", text: [KIND_NAME[it.kind], inf.detail].filter(Boolean).join(" · ") }));
      if (it.kind === "plug") side.append(h("p", { class: "note", text: "This plug is not in the latest plug list." }));
      return;
    }
    titleEl = h("h2", { text: inf.label });
    side.append(titleEl);
    ctlBox = h("div");
    side.append(ctlBox);
    fillCtl(inf.plug);
    side.append(configForm(inf.plug));
  }
  function fillCtl(p) {
    const f = document.activeElement, fa = f && ctlBox.contains(f) ? f.dataset.act : null;
    clear(ctlBox);
    if (titleEl) titleEl.textContent = info(placed(sel) || {}).label;
    const pend = P.pending[p.id], can = ADMIN && p.online && !pend;
    ctlBox.append(h("p", { class: "sub" }, [h("i", { class: "dot " + P.state(p) }), `${p.model || "Smart plug"} · ${P.word(p)}`]));
    if (!p.online) ctlBox.append(h("p", { class: "note", text: "This plug is offline, so it can't be switched. It is probably unplugged or in storage." }));
    if (ADMIN) {
      ctlBox.append(h("div", { class: "row" }, [
        h("button", { type: "button", class: "btn primary", "data-act": "power", disabled: !can, text: pend ? "..." : p.on ? "Turn off" : "Turn on", onclick: () => P.power(p.id, !p.on) }),
        h("label", { class: "tg" }, [h("input", { type: "checkbox", "data-act": "away", checked: !!p.away, disabled: !(ADMIN && p.online), onchange: (e) => P.away(p.id, e.target.checked) }),
          h("span", {}, ["Away mode", h("span", { class: "sub", text: "The plug flicks on and off at random while you are out." })])])]));
    } else ctlBox.append(h("p", { class: "sub", text: `Away mode: ${p.away ? "on" : "off"}` }));
    if (P.error) ctlBox.append(h("p", { class: "status bad", role: "status", text: P.error }));
    ctlBox.append(h("dl", { class: "kv" }, [h("dt", { text: "Next" }), h("dd", { text: p.schedules_enabled ? p.next || "Nothing scheduled" : "Hubbah schedules are off" }),
      h("dt", { text: "Last command" }), h("dd", { text: P.cmdText(p.last_cmd) }), h("dt", { text: "Last schedule" }), h("dd", { text: p.last_sched ? P.cmdText(p.last_sched) : "none yet" })]));
    if (fa) { const b = ctlBox.querySelector(`[data-act="${fa}"]`); if (b && !b.disabled) b.focus(); }
  }
  const newSchedId = (list) => { let id; do id = "s" + Math.random().toString(36).slice(2, 6); while (list.some((x) => x.id === id)); return id; };
  function configForm(p) {
    const cfg = { label: p.label || "", schedules_enabled: !!p.schedules_enabled, schedules: (p.schedules || []).map((x) => ({ ...x, days: [...x.days] })) };
    const form = h("form", { class: "cfg", novalidate: "" });
    const status = h("span", { class: "status", role: "status" });
    const list = h("div");
    form.append(h("h3", { text: "Name and schedules" }));
    if (!ADMIN) {
      form.append(h("p", { class: "sub", text: `Hubbah schedules: ${cfg.schedules_enabled ? "on" : "off"}` }),
        cfg.schedules.length ? h("ul", { class: "ro-sched" }, cfg.schedules.map((x) => h("li", { text: schedText(x) }))) : h("p", { class: "sub", text: "No schedules." }));
      return form;
    }
    form.append(h("label", { class: "f" }, ["Name on this page", h("input", { type: "text", maxlength: String(MAX_LABEL), value: cfg.label, placeholder: p.nickname || "Plug name", oninput: (e) => { cfg.label = e.target.value; } })]),
      h("p", { class: "hint", text: "Leave empty to use the name from the Wyze app." }),
      h("label", { class: "tg" }, [h("input", { type: "checkbox", checked: cfg.schedules_enabled, onchange: (e) => { cfg.schedules_enabled = e.target.checked; paint(); } }),
        h("span", {}, ["Use hubbah schedules", h("span", { class: "sub", text: "Turn this off if the Wyze app already has its own schedule for this plug." })])]), list);
    function paint() {
      clear(list);
      cfg.schedules.forEach((sc) => list.append(h("div", { class: "sched" + (cfg.schedules_enabled ? "" : " off") }, [
        h("div", { class: "days", role: "group", "aria-label": "Days" }, DAYS.map((d, i) => h("button", { type: "button", class: "day", text: d, "aria-label": DAY_FULL[i], "aria-pressed": String(sc.days.includes(i)),
          onclick: (e) => { sc.days = sc.days.includes(i) ? sc.days.filter((x) => x !== i) : [...sc.days, i]; e.currentTarget.setAttribute("aria-pressed", String(sc.days.includes(i))); } }))),
        h("div", { class: "row" }, [
          h("input", { type: "time", value: sc.time, "aria-label": "Time", oninput: (e) => { sc.time = e.target.value; } }),
          h("select", { "aria-label": "Action", onchange: (e) => { sc.action = e.target.value; } }, [h("option", { value: "on", selected: sc.action === "on", text: "Turn on" }), h("option", { value: "off", selected: sc.action === "off", text: "Turn off" })]),
          h("label", {}, [h("input", { type: "checkbox", checked: sc.enabled, onchange: (e) => { sc.enabled = e.target.checked; } }), "Active"]),
          h("button", { type: "button", class: "btn small push", text: "Remove", onclick: () => { cfg.schedules = cfg.schedules.filter((x) => x !== sc); paint(); } })])])));
      if (!cfg.schedules.length) list.append(h("p", { class: "hint", text: "No schedules yet." }));
    }
    paint();
    form.append(h("div", { class: "row" }, [
      h("button", { type: "button", class: "btn", text: "Add a schedule", onclick: () => {
        if (cfg.schedules.length >= 20) { status.textContent = "Up to 20 schedules per plug."; status.className = "status bad"; return; }
        cfg.schedules.push({ id: newSchedId(cfg.schedules), days: [0, 1, 2, 3, 4], time: "18:00", action: "on", enabled: true }); paint(); } }),
      h("button", { type: "submit", class: "btn primary", text: "Save" }), status]));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const bad = cfg.schedules.find((x) => !x.days.length || !/^\d\d:\d\d$/.test(x.time));
      if (bad) { status.textContent = "Each schedule needs a time and at least one day."; status.className = "status bad"; return; }
      status.textContent = "Saving..."; status.className = "status";
      const r = await P.saveConfig(p.id, { label: cfg.label.trim().slice(0, MAX_LABEL), schedules_enabled: cfg.schedules_enabled,
        schedules: cfg.schedules.map((x) => ({ id: x.id, days: [...x.days].sort((a, b) => a - b), time: x.time, action: x.action, enabled: x.enabled })) });
      if (r.ok) { status.textContent = "Saved."; draw(); } else { status.textContent = r.msg; status.className = "status bad"; }
    });
    return form;
  }

  function editPanel() {
    const free = avail.filter((a) => !placed(a.id));
    side.append(h("h2", { text: "Edit layout" }), h("p", { class: "hint", text: "Drag pins to move them. Arrow keys nudge the selected pin." }));
    side.append(h("h3", { text: "Not placed yet" }));
    if (!free.length) side.append(h("p", { class: "hint", text: "Everything is on the map." }));
    else side.append(h("ul", { class: "tray" }, free.map((a) => h("li", {}, [h("button", { type: "button", class: "btn small", title: "Click to place, or drag onto the drawing",
      onpointerdown: (e) => { try { e.currentTarget.setPointerCapture(e.pointerId); } catch (err) { /* ok */ } trayDown(e, a); }, onclick: () => addItem(a), onpointermove: (e) => trayMove(e, a) }, [a.label, a.detail ? h("small", { text: a.detail }) : null])]))));
    // add a custom item
    let name = "", ico = "lamp";
    const icons = h("div", { class: "icons", role: "group", "aria-label": "Icon" }), nm = h("input", { type: "text", maxlength: String(MAX_LABEL), placeholder: "Tree lights", oninput: (e) => { name = e.target.value; } });
    const paintIcons = () => { clear(icons); CUSTOM_ICONS.forEach((k) => icons.append(h("button", { type: "button", class: "icopt", "aria-label": k, title: k, "aria-pressed": String(ico === k), onclick: () => { ico = k; paintIcons(); } }, [icon(k)]))); };
    paintIcons();
    side.append(h("h3", { text: "Add your own item" }), h("label", { class: "f" }, ["Name", nm]), icons,
      h("div", { class: "row" }, [h("button", { type: "button", class: "btn", text: "Add to map", onclick: () => {
        if (!name.trim()) { nm.focus(); return say("Give the item a name first.", true); }
        say(""); addItem({ id: "custom:" + Math.random().toString(36).slice(2, 6), kind: "custom", label: name.trim().slice(0, MAX_LABEL), icon: ico }); } })]));
    const it = sel && placed(sel);
    if (it) {
      side.append(h("h3", { text: "Selected" }), h("p", { class: "sub", text: `${info(it).label} · ${KIND_NAME[it.kind]}` }));
      if (it.kind === "custom") {
        const sic = h("div", { class: "icons", role: "group", "aria-label": "Icon" }), paint2 = () => { clear(sic); CUSTOM_ICONS.forEach((k) => sic.append(h("button", { type: "button", class: "icopt", "aria-label": k, title: k, "aria-pressed": String(it.icon === k), onclick: () => { it.icon = k; paint2(); draw(); setDirty(); } }, [icon(k)]))); };
        paint2();
        side.append(h("label", { class: "f" }, ["Name", h("input", { type: "text", maxlength: String(MAX_LABEL), value: it.label || "", oninput: (e) => { it.label = e.target.value.slice(0, MAX_LABEL); draw(); setDirty(); } })]), sic);
      }
      side.append(h("div", { class: "row" }, [h("button", { type: "button", class: "btn", text: it.kind === "custom" ? "Delete item" : "Remove from map", onclick: () => removeItem(it.id) })]));
    }
    const l = layout.links[selLink];
    if (l) {
      side.append(h("h3", { text: "Selected link" }), h("p", { class: "sub", text: `${itemName(l.a)} to ${itemName(l.b)}` }),
        h("div", { class: "kinds", role: "group", "aria-label": "Link kind" }, Object.entries(LINKS).map(([k, L]) => h("button", { type: "button", class: "btn small", "aria-pressed": String(l.kind === k), text: L.name,
          onclick: () => { l.kind = k; renderAll(); } }))),
        h("div", { class: "row" }, [h("button", { type: "button", class: "btn", text: "Delete link", onclick: () => { layout.links.splice(selLink, 1); selLink = -1; renderAll(); } })]));
    }
    const mine = layout.links.map((x, i) => [x, i]).filter(([x]) => it && (x.a === it.id || x.b === it.id));
    if (mine.length) side.append(h("h3", { text: "Links on this item" }), h("div", { class: "kinds" }, mine.map(([x, i]) => h("button", { type: "button", class: "btn small",
      text: `${LINKS[x.kind] ? LINKS[x.kind].name : "Link"} to ${itemName(x.a === it.id ? x.b : x.a)}`, onclick: () => pick(null, i) }))));
  }
  // Dragging a tray item onto the drawing: once the pointer moves, the item is added under it and the new pin takes over the drag.
  let trayStart = null;
  function trayDown(e, a) { trayStart = { x: e.clientX, y: e.clientY, id: a.id }; }
  function trayMove(e, a) {
    if (!trayStart || trayStart.id !== a.id || !(e.buttons & 1) || Math.hypot(e.clientX - trayStart.x, e.clientY - trayStart.y) < 8) return;
    trayStart = null;
    const r = svg.getBoundingClientRect(), inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
    const it = addItem(a, inside ? (e.clientX - r.left) / r.width : null, inside ? (e.clientY - r.top) / r.height : null);
    const g = it && [...svg.querySelectorAll(".pin")].find((x) => x.dataset.id === it.id);
    if (g) { drag = { id: it.id, sx: e.clientX, sy: e.clientY, moved: true }; g.classList.add("drag"); try { g.setPointerCapture(e.pointerId); } catch (err) { /* touch pointers may not transfer */ } }
  }
  document.addEventListener("pointerup", () => { trayStart = null; });

  function renderAll() { renderBar(); renderLinkBar(); draw(); renderSide(); }

  // ---------- load ----------
  async function load() {
    const r = await P.api("GET", "/api/floorplan");
    if (!r.ok || !r.data) { say("Could not load the floorplan data.", true); return false; }
    layout = { items: (r.data.layout && r.data.layout.items) || [], links: (r.data.layout && r.data.layout.links) || [] };
    avail = r.data.available || [];
    saved = JSON.stringify(layout);
    return true;
  }
  P.on(() => {
    if (drag) return;
    if (!editing) { draw(); if (ctlBox && sel) { const p = P.byId(sel.slice(5)); if (p) fillCtl(p); } }
    else if (!sel) renderSide();
  });
  window.addEventListener("beforeunload", (e) => { if (editing && dirty()) { e.preventDefault(); e.returnValue = ""; } });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || e.target.matches("input, select, textarea")) return;
    if (editing && linkMode) { linkMode = false; linkFrom = null; linkPair = null; renderAll(); } else if (sel || selLink >= 0) pick(null);
  });
  if (window.ResizeObserver) new ResizeObserver(() => draw()).observe(frame);
  img.addEventListener("load", () => draw());
  if (document.fonts) document.fonts.ready.then(() => draw());   // label widths depend on the web font
  img.addEventListener("error", () => say("The drawing could not be loaded.", true));
  renderBar(); renderLegend(); renderSide();
  load().then((ok) => { if (ok) { renderAll(); P.poll(); } });
})();
