// /automations: a sentence-style rule builder ("When [nobody is home] and [...], then [turn on until] [Hall lamp] [11:00 PM]").
// Data: GET /api/automations (rules, plugs, people, status), PUT to save, POST /api/automations/{id}/test, GET /api/automations/log.
// Dropdowns only, no JSON on screen. Admins edit; everyone else reads. DOM helpers from plugs.js, no innerHTML.
(function () {
  "use strict";
  const P = window.Plugs, h = P.h, clear = P.clear, ADMIN = !!window.HOMELAB_ADMIN;
  const $ = (id) => document.getElementById(id);
  const box = $("rules"), msg = $("auMsg"), logBox = $("log");
  const say = (t, bad) => { msg.textContent = t || ""; msg.classList.toggle("bad", !!bad); };
  const when = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }); };
  const rid = () => "r" + Math.random().toString(36).slice(2, 8);
  const copy = (x) => JSON.parse(JSON.stringify(x));
  const DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"], DAY_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const FOR = [[0, "right now"], [5, "for 5 min"], [10, "for 10 min"], [15, "for 15 min"], [30, "for 30 min"], [60, "for 1 hour"], [120, "for 2 hours"]];
  const OFFSETS = [[-60, "1 h before"], [-30, "30 min before"], [-15, "15 min before"], [0, "at"], [15, "15 min after"], [30, "30 min after"], [60, "1 h after"]];
  const TYPES = [["people", "People"], ["house", "Household"], ["time", "Time"], ["sun", "Sun"], ["days", "Day"], ["plug", "Plug"]];
  const DO = [["on", "turn on"], ["off", "turn off"], ["on_until", "turn on until"], ["gate", "run the normal schedule of"]];

  let meta = { plugs: [], people: [], status: {} }, rules = [], saved = "[]", tests = {};
  const dirty = () => ADMIN && JSON.stringify(rules) !== saved;
  const firstPlug = () => (meta.plugs[0] || {}).key || "";
  const plugName = (k) => (meta.plugs.find((p) => p.key === k) || {}).name || "a plug that is gone";

  function blank(type) {
    return {
      people: { type, people: meta.people.slice(0, 1).map((p) => p.id), match: "any", state: "home", for_min: 0 },
      house: { type, who: "anyone", for_min: 0 }, time: { type, from: "17:00", to: "23:00" }, sun: { type, after: "sunset", offset_min: 0 },
      days: { type, days: [0, 1, 2, 3, 4] }, plug: { type, plug: firstPlug(), state: "on" } }[type];
  }
  const EXAMPLES = [
    ["Lamp only when home", () => ({ when: [{ type: "people", people: meta.people.map((p) => p.id).slice(0, 12), match: "any", state: "home", for_min: 0 }],
      then: { do: "gate", plug: firstPlug() } })],
    ["Away lamp", () => ({ when: [{ type: "house", who: "nobody", for_min: 0 }], then: { do: "on_until", plug: firstPlug(), until: "23:00" } })],
    ["Lights at sunset", () => ({ when: [{ type: "sun", after: "sunset", offset_min: 0 }],
      then: { do: "on_until", plug: (meta.plugs[1] || meta.plugs[0] || {}).key || "", until: "23:00" } })],
  ];

  // ---------- small controls (every one carries data-k so focus survives a redraw) ----------
  function select(k, label, options, value, set) {
    return h("select", { "data-k": k, "aria-label": label, disabled: !ADMIN, onchange: (e) => { const o = options.find((x) => String(x[0]) === e.target.value); set(o ? o[0] : e.target.value); changed(); } },
      options.map(([v, t]) => h("option", { value: String(v), text: t, selected: String(v) === String(value) })));
  }
  function timeInput(k, label, value, set) {
    return h("input", { type: "time", step: "60", value, "data-k": k, "aria-label": label, disabled: !ADMIN, required: true,
      onchange: (e) => { if (/^\d\d:\d\d$/.test(e.target.value)) { set(e.target.value); changed(); } } });
  }
  const word = (t) => h("span", { class: "au-w", text: t });
  const plain = (t) => h("span", { text: t });
  const plugOptions = (k) => {
    const o = meta.plugs.map((p) => [p.key, p.name + (p.online ? "" : " (offline)")]);
    return k && !meta.plugs.some((p) => p.key === k) ? o.concat([[k, "a plug that is gone"]]) : o;
  };

  function condEl(r, i, c, j) {
    const k = `${i}:c${j}:`, kids = [select(k + "type", "Condition type", TYPES, c.type, (t) => { r.when[j] = blank(t); })];
    if (c.type === "people") {
      kids.push(select(k + "match", "Any or all", [["any", "any of"], ["all", "all of"]], c.match, (v) => { c.match = v; }));
      (meta.people.length ? meta.people : []).forEach((p) => kids.push(h("label", { class: "au-chk" }, [
        h("input", { type: "checkbox", checked: c.people.includes(p.id), disabled: !ADMIN, "data-k": k + "p" + p.id,
          onchange: (e) => { c.people = e.target.checked ? c.people.concat(p.id) : c.people.filter((x) => x !== p.id); changed(); } }), p.name])));
      if (!meta.people.length) kids.push(h("a", { href: "/presence", text: "add people first" }));
      kids.push(select(k + "state", "Home or away", [["home", "is home"], ["away", "is away"]], c.state, (v) => { c.state = v; }));
      kids.push(select(k + "for", "For how long", FOR, c.for_min, (v) => { c.for_min = v; }));
    } else if (c.type === "house") {
      kids.push(select(k + "who", "Who", [["anyone", "anyone is home"], ["everyone", "everyone is home"], ["nobody", "nobody is home"]], c.who, (v) => { c.who = v; }));
      kids.push(select(k + "for", "For how long", FOR, c.for_min, (v) => { c.for_min = v; }));
    } else if (c.type === "time") {
      kids.push(plain("between"), timeInput(k + "from", "From", c.from, (v) => { c.from = v; }), plain("and"), timeInput(k + "to", "To", c.to, (v) => { c.to = v; }));
    } else if (c.type === "sun") {
      kids.push(plain("it is"), select(k + "off", "Offset", OFFSETS, c.offset_min, (v) => { c.offset_min = v; }),
        select(k + "after", "Sunset or sunrise", [["sunset", "sunset"], ["sunrise", "sunrise"]], c.after, (v) => { c.after = v; }),
        plain(c.after === "sunset" ? "or later (until sunrise)" : "or later (until sunset)"));
    } else if (c.type === "days") {
      kids.push(plain("on"));
      DAYS.forEach((d, n) => kids.push(h("button", { type: "button", class: "day", "aria-pressed": String(c.days.includes(n)), "aria-label": DAY_FULL[n], text: d, disabled: !ADMIN, "data-k": k + "d" + n,
        onclick: () => { const on = c.days.includes(n); if (on && c.days.length === 1) return; c.days = on ? c.days.filter((x) => x !== n) : c.days.concat(n).sort(); changed(); } })));
    } else if (c.type === "plug") {
      kids.push(select(k + "plug", "Plug", plugOptions(c.plug), c.plug, (v) => { c.plug = v; }), plain("is"),
        select(k + "pstate", "On or off", [["on", "on"], ["off", "off"]], c.state, (v) => { c.state = v; }));
    }
    if (ADMIN && r.when.length > 1) kids.push(h("button", { type: "button", class: "au-x", text: "×", "aria-label": "Remove this condition", "data-k": k + "x",
      onclick: () => { r.when.splice(j, 1); changed(); } }));
    return h("span", { class: "au-cond" }, kids);
  }

  function actEl(r, i) {
    const t = r.then, k = `${i}:a:`;
    const kids = [select(k + "do", "Action", DO, t.do, (v) => { t.do = v; if (v === "on_until") t.until = t.until || "23:00"; else delete t.until; }),
      select(k + "plug", "Plug", plugOptions(t.plug), t.plug, (v) => { t.plug = v; })];
    if (t.do === "on_until") kids.push(timeInput(k + "until", "Until", t.until, (v) => { t.until = v; }), plain("then off"));
    if (t.do === "gate") kids.push(plain("only then; otherwise skip it"));
    return h("span", { class: "au-act" }, kids);
  }

  function testEl(r) {
    const t = tests[r.id];
    if (!t) return h("div", { class: "au-test", hidden: true });
    if (t.error) return h("div", { class: "au-test", role: "status" }, [h("p", { class: "bad", text: t.error })]);
    return h("div", { class: "au-test", role: "status" }, [
      h("ul", {}, t.conditions.map((c) => h("li", {}, [h("span", { class: c.ok ? "au-yes" : "au-no", text: c.ok ? "Yes" : "No" }), h("span", { text: c.text })]))),
      h("p", {}, [h("b", { text: t.live ? "Right now it would: " : "Right now, in dry run, it would: " }), t.would]),
      h("p", { class: "au-sub", text: t.note })]);
  }

  function ruleEl(r, i) {
    const live = r.mode === "live", st = meta.status[r.id] || {}, k = `${i}:`;
    const head = h("div", { class: "au-rhead" }, [
      h("input", { type: "text", class: "au-name", value: r.name, maxlength: "30", "aria-label": "Rule name", disabled: !ADMIN, "data-k": k + "name",
        oninput: (e) => { r.name = e.target.value; delete tests[r.id]; mark(); } }),
      h("span", { class: "au-badge" + (live ? " live" : ""), text: live ? "Live" : "Dry run" }),
      h("label", { class: "au-tg" }, [h("input", { type: "checkbox", role: "switch", checked: live, disabled: !ADMIN, "data-k": k + "live",
        "aria-label": "Live: switch the plug for real", onchange: (e) => { r.mode = e.target.checked ? "live" : "dry_run"; changed(); } }), "Live"]),
      h("label", { class: "au-tg" }, [h("input", { type: "checkbox", checked: r.enabled, disabled: !ADMIN, "data-k": k + "on",
        onchange: (e) => { r.enabled = e.target.checked; changed(); } }), "On"])]);
    const sent = [word("When")];
    r.when.forEach((c, j) => { if (j) sent.push(word("and")); sent.push(condEl(r, i, c, j)); });
    if (ADMIN && r.when.length < 6) sent.push(h("button", { type: "button", class: "btn small", text: "+ condition", "data-k": k + "add",
      onclick: () => { r.when.push(blank("time")); changed(); } }));
    sent.push(word("then"), actEl(r, i));
    if (r.then.do === "gate") sent.push(h("p", { class: "au-note", text: "Checked each time one of the plug's own schedules is due. It only matters while that plug uses hubbah schedules (Floorplan, plug panel)." }));
    else sent.push(h("p", { class: "au-note", text: "Acts once when all of this turns true, not again while it stays true, so switching the plug by hand is never undone." }));
    const last = st.last ? `Last: ${st.last.text} ${when(st.last.at)}` : r.enabled ? "Nothing yet." : "Switched off.";
    const foot = h("div", { class: "au-foot" }, [h("p", { class: "au-sub" + (st.last && st.last.ok === false ? " bad" : ""), text: last })].concat(ADMIN ? [
      h("button", { type: "button", class: "btn small", text: "Test now", "data-k": k + "test", onclick: () => test(r) }),
      h("button", { type: "button", class: "btn small", text: "Up", "aria-label": "Move up (higher priority)", disabled: i === 0, "data-k": k + "up", onclick: () => move(i, -1) }),
      h("button", { type: "button", class: "btn small", text: "Down", "aria-label": "Move down", disabled: i === rules.length - 1, "data-k": k + "down", onclick: () => move(i, 1) }),
      h("button", { type: "button", class: "btn small", text: "Delete", "data-k": k + "del", onclick: () => { if (confirm(`Delete the rule "${r.name || "untitled"}"?`)) { rules.splice(i, 1); changed(); } } })] : []));
    return h("article", { class: "card au-rule" + (live ? " live" : "") + (r.enabled ? "" : " off"), "aria-label": r.name || "Untitled rule" },
      [head, h("div", { class: "au-sentence" }, sent), foot, testEl(r)]);
  }

  function draw() {
    const f = document.activeElement, fk = f && box.contains(f) ? f.dataset.k : null;
    clear(box);
    if (!rules.length) box.append(h("div", { class: "card" }, [h("p", { class: "au-empty", text: ADMIN ? "No rules yet. Add one, or start from an example." : "No rules yet." })]));
    rules.forEach((r, i) => box.append(ruleEl(r, i)));
    if (fk) { const el = box.querySelector(`[data-k="${CSS.escape(fk)}"]`); if (el) el.focus(); }
    mark();
  }
  function changed() { rules.forEach((r) => delete tests[r.id]); draw(); }
  function move(i, d) { const [r] = rules.splice(i, 1); rules.splice(i + d, 0, r); changed(); }
  function mark() {
    if (!ADMIN) return;
    const d = dirty();
    $("save").disabled = !d;
    $("dirty").textContent = d ? "Unsaved changes" : "";
    $("save").parentElement.classList.toggle("dirty", d);   // the bar sticks to the bottom only while there is something to save
  }

  async function test(r) {
    const res = await P.api("POST", `/api/automations/${encodeURIComponent(r.id)}/test`, { rule: { ...r, name: r.name.trim() } });
    tests[r.id] = res.ok ? res.data : { error: res.msg };
    draw();
  }

  async function save() {
    $("save").disabled = true;
    const res = await P.api("PUT", "/api/automations", { rules: rules.map((r) => ({ ...r, name: r.name.trim() })) });
    if (!res.ok) { say(res.msg, true); mark(); return; }
    say("Saved. Rules take effect within 30 seconds.");
    rules = copy(res.data.rules); saved = JSON.stringify(rules);
    draw();
  }

  async function load() {
    const res = await P.api("GET", "/api/automations");
    if (!res.ok || !res.data) { if (res.msg) say(res.msg, true); return; }
    const d = res.data;
    meta = { plugs: d.plugs, people: d.people, status: d.status };
    if (!dirty()) { rules = copy(d.rules); saved = JSON.stringify(rules); }
    $("sun").textContent = [d.sun.sunrise && d.sun.sunset ? `Today: sunrise ${d.sun.sunrise}, sunset ${d.sun.sunset}.` : "",
      d.presence_stale ? "Presence is unknown right now (no fresh Wi-Fi data), so presence rules wait." : ""].filter(Boolean).join(" ");
    if (!d.configured) say("Automations are not set up on this server.", true);
    else if (!d.plugs.length) say("No smart plugs in the live data yet.");
    draw();
  }

  async function loadLog() {
    const res = await P.api("GET", "/api/automations/log?limit=100");
    if (!res.ok || !res.data) return;
    clear(logBox);
    if (!res.data.entries.length) { logBox.append(h("li", {}, [h("span", { class: "au-empty", text: "Nothing yet. What rules do (or would do) shows up here." })])); return; }
    res.data.entries.forEach((e) => logBox.append(h("li", {}, [h("time", { datetime: e.at, text: when(e.at) }),
      h("span", { class: e.ok === false ? "bad" : "" }, [h("span", { class: "au-badge" + (e.mode === "live" ? " live" : ""), text: e.mode === "live" ? "Live" : "Dry run" }),
        h("b", { text: e.name + ": " }), e.text])])));
  }

  if (ADMIN) {
    const ex = $("example");
    ex.append(h("option", { value: "", text: "an example" }));
    EXAMPLES.forEach(([name], n) => ex.append(h("option", { value: String(n), text: name })));
    const add = (name, body) => { rules.push({ id: rid(), name, enabled: true, mode: "dry_run", ...body }); changed();
      const names = box.querySelectorAll(".au-name"); if (names.length) { names[names.length - 1].focus(); names[names.length - 1].scrollIntoView({ block: "center" }); } };
    ex.addEventListener("change", () => { if (ex.value !== "") { const [name, make] = EXAMPLES[Number(ex.value)]; add(name, make()); ex.value = ""; } });
    $("addRule").addEventListener("click", () => add("New rule", { when: [blank("house")], then: { do: "on", plug: firstPlug() } }));
    $("save").addEventListener("click", save);
    window.addEventListener("beforeunload", (e) => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } });
  }
  let timer = 0;
  const tick = () => { clearTimeout(timer); Promise.all([load(), loadLog()]).finally(() => { timer = setTimeout(tick, document.hidden ? 120000 : 30000); }); };
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
  tick();
})();
