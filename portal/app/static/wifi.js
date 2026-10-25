// /wifi: coverage estimate, device signal and interference. Data: GET /api/wifi (history summary from the collector, node pins from the floorplan).
// Coverage is an ESTIMATE: a log-distance model from the pinned Eero nodes with wall losses read off the drawing's dark pixels, then shifted
// to match the signals devices actually report. Admins place devices by tapping the map (PUT /api/wifi/places). DOM calls only, no innerHTML.
(function () {
  "use strict";
  const P = window.Plugs, h = P.h, s = P.s, clear = P.clear;
  const ADMIN = !!window.HOMELAB_ADMIN;
  const $ = (id) => document.getElementById(id);
  const img = $("wfImg"), heat = $("wfHeat"), svg = $("wfSvg"), side = $("wfSide"), bar = $("wfBar"), msg = $("wfMsg");

  const WIDTH_M = 18;           // assumed width of the drawing in meters (scales distance in the model)
  const GW = 200;               // coverage grid width in cells
  const WEAK = -75, FAIR = -65;
  const BAND_BASE = { "2.4 GHz": [-32, 3.5], "5 GHz": [-38, 6] };   // [signal at 1 m, loss per wall]
  const NAMES = { good: "good", fair: "fair", weak: "weak" };
  const cls = (v) => (v == null ? "gone" : v >= FAIR ? "good" : v >= WEAK ? "fair" : "weak");
  const fmtH = (hr) => `${hr % 12 || 12}:00 ${hr < 12 ? "AM" : "PM"}`;
  const ago = (iso) => { const m = Math.round((Date.now() - Date.parse(iso)) / 60000); return m < 2 ? "just now" : m < 120 ? m + " min ago" : Math.round(m / 60) + " h ago"; };

  let D = null, band = "5 GHz", hour = new Date().getHours(), replay = false, sel = null, placing = null, walls = null, GH = 0;
  let grid = null, spots = [];
  const layers = { coverage: true, devices: true, weak: true };
  const say = (t, bad) => { msg.textContent = t || ""; msg.className = "fp-msg" + (bad ? " bad" : ""); };

  async function api(method, path, body) {
    const r = await fetch(path, { method, credentials: "same-origin", headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : "That did not work.");
    return j;
  }

  // ---- the model ----
  function buildWalls() {
    if (!img) return;
    GH = Math.round(GW * img.naturalHeight / img.naturalWidth);
    try {
      const c = document.createElement("canvas"); c.width = GW; c.height = GH;
      const x = c.getContext("2d"); x.drawImage(img, 0, 0, GW, GH);
      const d = x.getImageData(0, 0, GW, GH).data; walls = new Uint8Array(GW * GH);
      for (let i = 0; i < GW * GH; i++) walls[i] = d[i * 4] < 120 ? 1 : 0;
    } catch (e) { walls = null; }   // a tainted canvas: coverage ignores walls
  }
  const nodes = () => D.nodes.filter((n) => n.x != null);
  function modelFrom(n, fx, fy) {   // dBm at a point from one node
    const [base, perWall] = BAND_BASE[band];
    const aspect = img.naturalWidth / img.naturalHeight;
    const dm = Math.max(.5, Math.hypot((fx - n.x) * WIDTH_M, (fy - n.y) * WIDTH_M / aspect));
    let wc = 0;
    if (walls) {
      let prev = 0;
      for (let k = 1; k < 40; k++) {
        const gx = Math.round((n.x + (fx - n.x) * k / 40) * (GW - 1)), gy = Math.round((n.y + (fy - n.y) * k / 40) * (GH - 1));
        const w = walls[gy * GW + gx]; if (w && !prev) wc++; prev = w;
      }
    }
    return base - 30 * Math.log10(dm) - wc * perWall;
  }
  function sigAt(d) { return replay ? d.hod[hour] : d.connected ? d.dbm : null; }
  function offset() {   // how far the model is from what placed devices measured, on this band (median, limited to +-15 dB)
    const res = [];
    (D.devices || []).forEach((d) => {
      const p = (D.places || {})[d.id], n = nodes().find((x) => x.name === d.node), v = sigAt(d);
      if (p && n && v != null && d.band === band) res.push(v - modelFrom(n, p.x, p.y));
    });
    if (!res.length) return 0;
    res.sort((a, b) => a - b);
    return Math.max(-15, Math.min(15, res[Math.floor(res.length / 2)]));
  }
  function box() {   // area worth estimating: around the nodes and placed devices
    const pts = nodes().map((n) => [n.x, n.y]).concat(Object.values(D.places || {}).map((p) => [p.x, p.y]));
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]), pad = .14;
    return { x0: Math.max(0, Math.min(...xs) - pad), x1: Math.min(1, Math.max(...xs) + pad), y0: Math.max(0, Math.min(...ys) - pad), y1: Math.min(1, Math.max(...ys) + pad) };
  }
  function drawHeat() {
    heat.width = GW; heat.height = GH;
    grid = null; spots = [];
    if (!nodes().length) { heat.style.opacity = "0"; return; }
    const off = offset(), b = box(), im = heat.getContext("2d").createImageData(GW, GH), dark = document.documentElement.dataset.theme === "midnight";
    grid = new Float32Array(GW * GH).fill(NaN);
    for (let gy = Math.floor(b.y0 * GH); gy < Math.ceil(b.y1 * GH); gy++) for (let gx = Math.floor(b.x0 * GW); gx < Math.ceil(b.x1 * GW); gx++) {
      const i = gy * GW + gx;
      if (walls && walls[i]) continue;   // wall pixels themselves are not floor
      let r = -120;
      nodes().forEach((n) => { r = Math.max(r, modelFrom(n, gx / GW, gy / GH)); });
      r += off; grid[i] = r;
      const t = Math.min(1, Math.max(0, (r + 82) / 34)), o = i * 4;
      if (r < WEAK) { im.data[o] = 226; im.data[o + 1] = 123; im.data[o + 2] = 12; im.data[o + 3] = dark ? 120 : 95; }
      else if (dark) { im.data[o] = 44; im.data[o + 1] = 198; im.data[o + 2] = 200; im.data[o + 3] = 30 + t * 110; }
      else { im.data[o] = 11; im.data[o + 1] = 123; im.data[o + 2] = 126; im.data[o + 3] = 12 + t * 120; }
    }
    heat.getContext("2d").putImageData(im, 0, 0);
    heat.style.opacity = layers.coverage ? "" : "0";
    spots = findSpots();
  }
  function findSpots() {   // the biggest connected patches below -75 dBm (up to 3)
    const seen = new Uint8Array(GW * GH), out = [];
    for (let i = 0; i < GW * GH; i++) {
      if (seen[i] || !(grid[i] < WEAK)) continue;
      const q = [i]; seen[i] = 1; let n = 0, sx = 0, sy = 0, sv = 0;
      while (q.length) {
        const c = q.pop(), x = c % GW, y = (c - x) / GW; n++; sx += x; sy += y; sv += grid[c];
        [[1, 0], [-1, 0], [0, 1], [0, -1]].forEach(([dx, dy]) => {
          const nx = x + dx, ny = y + dy, j = ny * GW + nx;
          if (nx >= 0 && nx < GW && ny >= 0 && ny < GH && !seen[j] && grid[j] < WEAK) { seen[j] = 1; q.push(j); }
        });
      }
      if (n >= 60) out.push({ x: sx / n / GW, y: sy / n / GH, dbm: Math.round(sv / n), n });
    }
    return out.sort((a, b) => b.n - a.n).slice(0, 3);
  }

  // ---- map overlay ----
  function label(x, y, t1, t2) {
    const w = Math.max(t1.length * 7.2, (t2 || "").length * 7) + 14;
    return s("g", { class: "lab", transform: `translate(${x + 14},${y - 12})` }, [s("rect", { width: w, height: t2 ? 36 : 22, rx: 6 }), s("text", { x: 7, y: 15, text: t1 }), t2 ? s("text", { class: "sub", x: 7, y: 29, text: t2 }) : null]);
  }
  function drawOverlay() {
    if (!svg) return;
    const fr = $("wfFrame"), W = fr.clientWidth, H = fr.clientHeight;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`); clear(svg);
    svg.classList.toggle("placing", !!placing);
    if (layers.weak) spots.forEach((p) => svg.append(s("g", { class: "weak" }, [s("circle", { cx: p.x * W, cy: p.y * H, r: 22 }), label(p.x * W + 10, p.y * H + 4, "Weak spot", "about " + p.dbm + " dBm")])));
    nodes().forEach((n) => {
      const x = n.x * W, y = n.y * H, cnt = (D.devices || []).filter((d) => d.node === n.name && d.connected).length;
      svg.append(s("g", { class: "node" + (sel === "n:" + n.name ? " sel" : ""), tabindex: 0, role: "button", "aria-label": n.name + " node",
        onclick: () => pick("n:" + n.name), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick("n:" + n.name); } } },
        [s("path", { d: `M${x - 13} ${y + 9} h26 v-10 a13 13 0 0 0 -26 0z` }), label(x, y - 6, n.name, ADMIN ? cnt + " on Wi-Fi" : "")]));
    });
    if (layers.devices) (D.devices || []).forEach((d) => {
      const p = (D.places || {})[d.id]; if (!p) return;
      const v = sigAt(d), x = p.x * W, y = p.y * H;
      const g = s("g", { class: "dev " + cls(v) + (sel === d.id ? " sel" : ""), tabindex: 0, role: "button", "aria-label": `${d.name}, ${v == null ? "not connected" : v + " dBm"}`,
        onclick: (e) => { e.stopPropagation(); pick(d.id); }, onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(d.id); } } },
        [s("circle", { class: "ring", cx: x, cy: y, r: 13 }), s("circle", { class: "b", cx: x, cy: y, r: 8 })]);
      if (sel === d.id) g.append(label(x, y, d.name, v == null ? "not on Wi-Fi" : v + " dBm"));
      svg.append(g);
    });
  }
  function onMapClick(e) {
    if (!placing) return;
    const r = svg.getBoundingClientRect(), x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
    const id = placing; placing = null;
    D.places = { ...(D.places || {}), [id]: { x: +x.toFixed(4), y: +y.toFixed(4) } };
    api("PUT", "/api/wifi/places", { places: D.places }).then(() => say("Placed.")).catch((er) => say(er.message, true));
    sel = id; redraw(); drawSide();
  }

  // ---- side panel ----
  const card = (title, kids) => h("section", { class: "wf-card" }, [h("h3", { text: title })].concat(kids));
  function spark(d) {   // 7 days of hourly averages
    const W = 340, Ht = 70, now = Date.now() / 1000, t0 = now - 7 * 86400;
    const y = (v) => 8 + ((-45 - v) / 45) * 50, x = (t) => 30 + ((t - t0) / (now - t0)) * (W - 34);
    const sv = s("svg", { class: "spark", viewBox: `0 0 ${W} ${Ht}`, role: "img", "aria-label": "Signal over the last 7 days" });
    sv.append(s("line", { class: "ref", x1: 30, x2: W, y1: y(WEAK), y2: y(WEAK) }), s("text", { x: 0, y: y(WEAK) + 4, text: "-75" }), s("text", { x: 0, y: y(-50) + 4, text: "-50" }));
    let path = "", prev = null;
    d.hourly.forEach(([t, v]) => { path += (prev != null && t - prev <= 7200 ? "L" : "M") + x(t).toFixed(1) + " " + y(v).toFixed(1); prev = t; });
    sv.append(s("path", { class: "ln", d: path }));
    return sv;
  }
  function deviceCard(d) {
    const v = sigAt(d), p = (D.places || {})[d.id];
    const kids = [h("p", { class: "sub", text: `${d.node || "unknown node"} · ${d.band || "unknown band"}` }),
      h("dl", { class: "kv" }, [h("dt", { text: replay ? "Typical at " + fmtH(hour) : "Signal now" }), h("dd", { text: v == null ? "Not on Wi-Fi" : `${v} dBm, ${NAMES[cls(v)]}` }),
        h("dt", { text: "Last 24 h" }), h("dd", { text: d.low_24h == null ? "n/a" : `${d.low_24h} to ${d.high_24h} dBm` }),
        h("dt", { text: "Rate" }), h("dd", { text: "Not provided by Eero" })]), spark(d),
      h("p", { class: "hint", text: "Dashed line: weak below -75 dBm. Gaps: not on Wi-Fi." })];
    if (ADMIN) kids.push(h("div", { class: "row" }, [h("button", { class: "btn small", type: "button", text: p ? "Move on map" : "Place on map", onclick: () => startPlace(d.id) }),
      p ? h("button", { class: "btn small", type: "button", text: "Remove pin", onclick: () => removePlace(d.id) }) : null]));
    return card(d.name, kids);
  }
  function startPlace(id) { placing = id; say("Tap the map where this device sits."); redraw(); }
  function removePlace(id) {
    const pl = { ...(D.places || {}) }; delete pl[id]; D.places = pl;
    api("PUT", "/api/wifi/places", { places: pl }).catch((er) => say(er.message, true)); redraw(); drawSide();
  }
  function nodeCard(n) {
    const rows = [];
    Object.entries(n.radios).forEach(([b, r]) => rows.push(h("dt", { text: b }), h("dd", { text: `channel ${r.channel ?? "?"}${r.width ? ", " + r.width + " MHz" : ""}, ${r.utilization == null ? "busy n/a" : r.utilization + "% busy"}` })));
    return card(n.name + " node", [h("p", { class: "sub", text: `${n.model || "Eero"} · ${n.status || "status unknown"}${n.mesh_bars != null ? " · link " + n.mesh_bars + " of 5" : ""}` }), h("dl", { class: "kv" }, rows)]);
  }
  function chanChart(itf) {
    const sv = s("svg", { class: "chan", viewBox: "0 0 340 120", role: "img", "aria-label": "Neighbor networks per 2.4 GHz channel" });
    const max = Math.max(3, ...Object.values(itf.channels_24)), bw = 20, gap = 4.6;
    for (let c = 1; c <= 13; c++) {
      const n = itf.channels_24[c] || 0, hh = Math.max(2, n / max * 80), x = 8 + (c - 1) * (bw + gap), ours = itf.ours_24.includes(c);
      sv.append(s("rect", { class: "bar" + (ours ? " ours" : ""), x, y: 96 - hh, width: bw, height: hh, rx: 4 }, s("title", { text: `Channel ${c}: ${n} neighbor network${n === 1 ? "" : "s"}${ours ? " (ours)" : ""}` })),
        s("text", { x: x + bw / 2, y: 112, "text-anchor": "middle", text: c }));
      if (n) sv.append(s("text", { class: "v", x: x + bw / 2, y: 92 - hh, "text-anchor": "middle", text: n }));
    }
    return sv;
  }
  function interferenceCard() {
    const itf = D.interference, kids = [];
    kids.push(h("p", { class: "hint", text: "What Eero reports for our own radios, plus an optional one-time scan of neighbor networks." }));
    const busy = D.busy_by_hour;
    if (busy.some((v) => v != null)) {
      const mx = Math.max(10, ...busy.filter((v) => v != null));
      kids.push(h("div", { class: "sec", text: "Our 2.4 GHz channel busy, by hour" }),
        h("div", { class: "crowd", title: "Average channel utilization by hour of day" }, busy.map((v, i) => h("i", { style: `opacity:${v == null ? .08 : .2 + .8 * v / mx}`, title: `${fmtH(i)}: ${v == null ? "no data" : v + "% busy"}` }))),
        h("div", { class: "crowd-l" }, [h("span", { text: "12 AM" }), h("span", { text: "11 PM" })]));
    } else kids.push(h("p", { class: "hint", text: "Channel history starts building after the next collector run." }));
    if (itf.scanned) {
      kids.push(h("div", { class: "sec", text: "Neighbors per 2.4 GHz channel" }), chanChart(itf));
      const sc = D.scan;
      if (sc) kids.push(h("p", { class: "hint", text: `Scan from ${ago(sc.at)}: ${sc.count} networks. Ours: channel ${itf.ours_24.join(", ") || "unknown"}.` }));
      if (itf.suggestion) kids.push(h("p", { class: "note", text: itf.suggestion }));
      else kids.push(h("p", { class: "hint", text: "Nothing clearly better than our current 2.4 GHz channel." }));
      if (sc && sc.networks) kids.push(h("div", { class: "tablewrap" }, h("table", { class: "nets" }, [h("thead", {}, h("tr", {}, ["Neighbor network", "Ch", "Signal"].map((t) => h("th", { text: t })))),
        h("tbody", {}, sc.networks.map((n) => h("tr", {}, [h("td", { text: n.ssid }), h("td", { class: "num", text: n.channel }), h("td", { class: "num", text: n.dbm == null ? "?" : n.dbm + " dBm" })])))])));
    }
    if (ADMIN) kids.push(scanForm(itf.scanned));
    kids.push(h("div", { class: "sec", text: "What hubbah can know" }), h("ul", { class: "know" }, [
      h("li", { class: "y", text: "Signal, band and node for every device (Eero)" }),
      h("li", { class: "y", text: "Our channel, width and busy level; roams and band changes (Eero history)" }),
      h("li", { class: "m", text: "Neighbor networks: only from a scan you upload from the PC" }),
      h("li", { class: "n", text: "Rates, retries and airtime per device (Eero does not share them)" }),
      h("li", { class: "n", text: "Microwaves, baby monitors and other non Wi-Fi noise" })]));
    return card("Interference", kids);
  }
  function scanForm(has) {
    const ta = h("textarea", { rows: 4, placeholder: "Paste the output of: netsh wlan show networks mode=bssid", "aria-label": "Scan output", maxlength: 60000 });
    const send = () => api("POST", "/api/wifi/scan", { text: ta.value }).then((r) => { say(`Scan saved: ${r.count} networks.`); return load(); }).catch((er) => say(er.message, true));
    return h("details", { class: "scan" }, [h("summary", { text: has ? "Replace the neighbor scan" : "Add a neighbor scan" }),
      h("p", { class: "hint", text: "On the Windows PC, run the command below in a terminal, then paste or upload what it prints. Access point addresses are dropped; only network name, channel and signal are kept." }),
      h("code", { class: "cmd", text: "netsh wlan show networks mode=bssid > scan.txt" }), ta,
      h("div", { class: "row" }, [h("button", { class: "btn small primary", type: "button", text: "Save scan", onclick: send }),
        h("label", { class: "btn small" }, ["Upload file", h("input", { type: "file", accept: ".txt,text/plain", hidden: true, onchange: (e) => { const f = e.target.files[0]; if (f) f.text().then((t) => { ta.value = t.slice(0, 60000); }); } })]),
        has ? h("button", { class: "btn small", type: "button", text: "Clear scan", onclick: () => api("DELETE", "/api/wifi/scan").then(() => { say("Scan cleared."); return load(); }).catch((er) => say(er.message, true)) }) : null])]);
  }
  function listCard() {
    const devs = D.devices || [];
    return card(`Devices on Wi-Fi (${devs.filter((d) => d.connected).length})`, [
      h("ul", { class: "wf-list" }, devs.slice(0, 60).map((d) => { const v = sigAt(d), placed = (D.places || {})[d.id];
        return h("li", {}, h("button", { class: "wf-row " + cls(v), type: "button", onclick: () => pick(d.id) }, [h("i", { class: "d" }), h("span", { class: "nm", text: d.name }),
          h("span", { class: "mono", text: v == null ? "away" : v + " dBm" }), ADMIN && !placed ? h("small", { text: "not placed" }) : null])); }))]);
  }
  function eventsCard() {
    if (!D.events.length) return null;
    return card("Recent changes", [h("ul", { class: "wf-ev" }, D.events.slice(0, 10).map((e) => h("li", {}, [h("span", { class: "mono", text: ago(new Date(e.at * 1000).toISOString()) + " " }), e.text])))]);
  }
  function summaryCard() {
    const kids = [];
    if (D.stale) kids.push(h("p", { class: "note", text: "The Eero data is out of date, so this shows the last known history." }));
    if (!nodes().length) kids.push(h("p", { class: "note", text: ADMIN ? "No Eero node is pinned on the Floorplan yet. Pin the nodes there and the coverage estimate appears here." : "The coverage estimate needs the Eero nodes pinned on the Floorplan." }));
    else if (spots.length) kids.push(h("p", { class: "hint", text: `Estimated weak spots: ${spots.length}. Dashed rings on the map.` }));
    (D.weak || []).forEach((d) => kids.push(h("p", { class: "note", text: `${d.name} is weak (${d.dbm} dBm) on ${d.node || "its node"}. A closer node or a different spot would help.` })));
    if (ADMIN && (D.devices || []).length && !Object.keys(D.places || {}).length) kids.push(h("p", { class: "hint", text: "Choose a device below and Place on map: placed devices sharpen the estimate." }));
    return kids.length ? card("Coverage", kids) : null;
  }
  function drawSide() {
    clear(side);
    const dev = sel && (D.devices || []).find((d) => d.id === sel), nd = sel && sel.startsWith("n:") && D.nodes.find((n) => "n:" + n.name === sel);
    [dev ? deviceCard(dev) : null, nd ? nodeCard(nd) : null, summaryCard(), ADMIN ? listCard() : null, interferenceCard(), eventsCard()].forEach((c) => { if (c) side.append(c); });
    if (!ADMIN) side.append(h("p", { class: "hint", text: "Device signals are shown to admins." }));
  }
  function pick(id) { sel = sel === id ? null : id; placing = null; redraw(); drawSide(); }

  // ---- controls ----
  function drawBar() {
    clear(bar);
    bar.append(h("span", { class: "seg", role: "group", "aria-label": "Band" }, ["2.4 GHz", "5 GHz"].map((b) => h("button", { class: "btn", type: "button", "aria-pressed": String(b === band), text: b,
      onclick: () => { band = b; drawBar(); redraw(); drawSide(); } }))));
    [["coverage", "Coverage"], ["devices", "Devices"], ["weak", "Weak spots"]].forEach(([k, l]) => bar.append(h("label", { class: "wf-layer" }, [h("input", { type: "checkbox", checked: layers[k], onchange: (e) => { layers[k] = e.target.checked; redraw(); } }), l])));
  }
  function setHour(v, on) {
    hour = Number(v); replay = on;
    $("wfHour").value = hour;
    $("wfHourTxt").textContent = replay ? "Typical " + fmtH(hour) : "Now";
    redraw(); drawSide();
  }
  let timer = null;
  function wireTime() {
    $("wfTime").hidden = false;
    $("wfHourTxt").textContent = "Now"; $("wfHour").value = hour;
    $("wfHour").addEventListener("input", (e) => setHour(e.target.value, true));
    $("wfPlay").addEventListener("click", (e) => {
      const b = e.currentTarget;
      if (timer) { clearInterval(timer); timer = null; b.setAttribute("aria-pressed", "false"); b.textContent = "Play"; return; }
      b.setAttribute("aria-pressed", "true"); b.textContent = "Pause";
      timer = setInterval(() => setHour((hour + 1) % 24, true), 800);
    });
    $("wfLegend").append(...[["good", "Good, -65 dBm or better"], ["fair", "Fair"], ["weak", "Weak, below -75 dBm"]].map(([c, t]) => h("span", {}, [h("i", { class: "dot " + c }), t])),
      h("span", {}, [h("i", { class: "ramp" }), "Estimated coverage, weak to strong"]));
  }
  function redraw() { if (!svg) return; if (D) { drawHeat(); drawOverlay(); } }

  async function load() {
    try { D = await api("GET", "/api/wifi"); } catch (e) { say("Could not load Wi-Fi data.", true); return; }
    if (placing == null) say(D.stale ? "Eero data is out of date." : "");
    redraw(); drawSide();
  }

  drawBar();
  if (img) {
    svg.addEventListener("click", onMapClick);
    wireTime();
    window.addEventListener("resize", () => D && drawOverlay());
    window.addEventListener("hb-theme", () => D && drawHeat());
    const go = () => { buildWalls(); load(); };
    if (img.complete && img.naturalWidth) go(); else img.addEventListener("load", go);
  } else load();
  setInterval(() => { if (!placing && !replay) load(); }, 60000);
})();
