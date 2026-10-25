// /presence: who is home (any signed-in user), plus for admins the people editor and the Wi-Fi device list.
// Data: GET /api/presence (every 30 s while visible); saves with PUT /api/presence/people. DOM helpers from plugs.js, no innerHTML.
(function () {
  "use strict";
  const P = window.Plugs, h = P.h, clear = P.clear, ADMIN = !!window.HOMELAB_ADMIN;
  const $ = (id) => document.getElementById(id);
  const msg = $("auMsg"), who = $("who"), fresh = $("fresh"), hist = $("hist");
  const say = (t, bad) => { msg.textContent = t || ""; msg.classList.toggle("bad", !!bad); };
  const time = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); };
  const when = (iso) => {
    const d = new Date(iso);
    if (isNaN(d)) return "";
    return d.toDateString() === new Date().toDateString() ? "today " + time(iso) : d.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  };
  const newId = () => "p" + Math.random().toString(36).slice(2, 8);

  let data = null, people = [], owner = {}, away = 10, saved = "", filter = "";
  const snapshot = () => JSON.stringify({ people, owner, away });
  const dirty = () => ADMIN && saved !== "" && snapshot() !== saved;

  function drawWho() {
    clear(who);
    if (!data.people.length) {
      who.append(h("p", { class: "au-empty", text: ADMIN ? "Nobody is set up yet. Add a person below and pick their phone." : "Nobody is set up yet." }));
    }
    data.people.forEach((p) => {
      const st = p.home == null ? "unknown" : p.home ? "home" : "away";
      who.append(h("div", { class: "au-person" }, [
        h("i", { class: "au-dot " + st, "aria-hidden": "true" }),
        h("span", {}, [h("b", { text: p.name }),
          h("small", { text: st === "unknown" ? "Not known yet" : `${p.home ? "Home" : "Away"}${p.since ? " since " + when(p.since) : ""}` })])]));
    });
    fresh.textContent = data.stale ? "Wi-Fi data is out of date, so presence is paused. Rules wait until it is back."
      : data.eero_at ? `Wi-Fi checked ${when(data.eero_at)}. Someone counts as away after ${data.away_after_min} min without a device.` : "";
    fresh.classList.toggle("bad", !!data.stale);
  }

  function drawHistory() {
    clear(hist);
    if (!data.history.length) { hist.append(h("li", {}, [h("span", { class: "au-empty", text: "No arrivals or departures yet." })])); return; }
    data.history.forEach((e) => hist.append(h("li", {}, [h("time", { datetime: e.at, text: when(e.at) }),
      h("span", { text: `${e.name} ${e.event === "arrive" ? "arrived" : "left"}` })])));
  }

  // ---------- admin: people + devices ----------
  function drawPeople() {
    const box = $("peopleEd");
    clear(box);
    if (!people.length) box.append(h("p", { class: "au-empty", text: "No people yet." }));
    people.forEach((p, i) => {
      const n = Object.values(owner).filter((o) => o === p.id).length;
      box.append(h("div", { class: "au-pers" }, [
        h("input", { type: "text", value: p.name, maxlength: "30", "aria-label": "Name", placeholder: "Name",
          oninput: (e) => { p.name = e.target.value; mark(); drawDevices(); } }),
        h("span", { class: "au-sub", text: n === 1 ? "1 device" : `${n} devices` }),
        h("button", { class: "btn small", type: "button", text: "Remove", "aria-label": `Remove ${p.name || "person"}`,
          onclick: () => { people.splice(i, 1); for (const k in owner) if (owner[k] === p.id) delete owner[k]; drawPeople(); drawDevices(); mark(); } })]));
    });
    $("addPerson").disabled = people.length >= 12;
  }

  function drawDevices() {
    const box = $("devices");
    clear(box);
    const f = filter.trim().toLowerCase();
    const rows = (data.devices || []).filter((d) => !f || d.name.toLowerCase().includes(f) || d.id.includes(f))
      .sort((a, b) => (!!owner[b.id] - !!owner[a.id]) || (b.connected - a.connected) || a.name.localeCompare(b.name));
    if (!rows.length) { box.append(h("p", { class: "au-empty", text: f ? "No device matches." : "No Wi-Fi devices seen yet." })); return; }
    rows.forEach((d) => {
      const sel = h("select", { "aria-label": `Who owns ${d.name}`, onchange: (e) => { if (e.target.value) owner[d.id] = e.target.value; else delete owner[d.id]; mark(); drawPeople(); drawDevices(); } },
        [h("option", { value: "", text: "Not tracked" })].concat(people.map((p) => h("option", { value: p.id, text: p.name || "Unnamed person", selected: owner[d.id] === p.id }))));
      const bits = [d.wired ? "Wired" : d.band, d.signal_dbm, d.node ? "near " + d.node : "", d.connected ? "on Wi-Fi now" : d.last_seen ? "last seen " + when(d.last_seen) : ""].filter(Boolean);
      box.append(h("div", { class: "au-dev" + (owner[d.id] ? " mine" : "") }, [
        h("span", {}, [h("b", {}, [h("i", { class: "au-dot " + (d.connected ? "home" : ""), "aria-hidden": "true" }), d.name]),
          h("small", {}, [h("code", { text: d.id }), " · " + bits.join(" · ")])]),
        sel]));
    });
  }

  function mark() {
    const d = dirty();
    $("save").disabled = !d;
    $("dirty").textContent = d ? "Unsaved changes" : "";
    $("save").parentElement.classList.toggle("dirty", d);   // the bar sticks to the bottom only while there is something to save
  }

  async function save() {
    const devs = {};
    for (const [dev, pid] of Object.entries(owner)) (devs[pid] = devs[pid] || []).push(dev);
    const body = { people: people.map((p) => ({ id: p.id, name: p.name.trim(), devices: devs[p.id] || [] })), away_after_min: away };
    $("save").disabled = true;
    const r = await P.api("PUT", "/api/presence/people", body);
    if (!r.ok) { say(r.msg, true); mark(); return; }
    say("Saved.");
    saved = "";
    await load(true);
  }

  async function load(reset) {
    const r = await P.api("GET", "/api/presence");
    if (!r.ok || !r.data) { if (r.msg) say(r.msg, true); return; }
    data = r.data;
    drawWho(); drawHistory();
    if (!ADMIN) return;
    if (reset || !dirty()) {   // never overwrite an edit in progress
      people = data.people.map((p) => ({ id: p.id, name: p.name }));
      owner = {};
      data.people.forEach((p) => p.devices.forEach((d) => { owner[d] = p.id; }));
      away = data.away_after_min || 10;
      $("awayMin").value = String(away);
      saved = snapshot();
      drawPeople();
    }
    drawDevices(); mark();
  }

  if (ADMIN) {
    const am = $("awayMin");
    [5, 10, 15, 20, 30, 45, 60, 90, 120].forEach((m) => am.append(h("option", { value: String(m), text: `${m} min` })));
    am.addEventListener("change", () => { away = Number(am.value); mark(); });
    $("addPerson").addEventListener("click", () => { people.push({ id: newId(), name: "" }); drawPeople(); mark(); const ins = $("peopleEd").querySelectorAll("input"); ins[ins.length - 1].focus(); });
    $("devFilter").addEventListener("input", (e) => { filter = e.target.value; drawDevices(); });
    $("save").addEventListener("click", save);
    window.addEventListener("beforeunload", (e) => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } });
  }
  let timer = 0;
  const tick = () => { clearTimeout(timer); load(false).finally(() => { timer = setTimeout(tick, document.hidden ? 120000 : 30000); }); };
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
  tick();
})();
