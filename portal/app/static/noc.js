// NOC view: a traditional operations board over the same snapshot the map uses.
// Plain words only (UP / DOWN / WARN), no transit theme. Reads window.HLT, set up by index.html.
(function () {
  "use strict";
  const H = window.HLT, S = H.S, h = H.h, s = H.s;
  const root = document.getElementById("noc");
  const acked = new Set();      // fire-drill alarms only (this browser); real alarms are acked on the server
  const isAcked = (a) => (a.drill ? acked.has(a.id) : H.quiet(a));   // acked or in maintenance: quiet row
  const hist = {};              // link id -> recent total Mbps samples, taken in this browser
  const HIST_N = 45, SAMPLE_MS = 2000;
  let drillAt = null, active = false, linksBox = null;

  const WORD = { good: "UP", warn: "WARN", crit: "DOWN", unknown: "NO CHECK" };
  const WORD_STORE = { good: "OK", warn: "WARN", crit: "FAIL", unknown: "NO CHECK" };
  const SEV_WORD = { crit: "Critical", warn: "Warning", info: "Info" };
  const fmtClock = (d, utc) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: utc ? "UTC" : undefined });
  const fmtShort = (d) => d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const clamp = (v) => Math.max(0, Math.min(100, v));

  function clickable(el, id) {
    el.setAttribute("tabindex", "0");
    el.setAttribute("role", "button");
    el.addEventListener("click", () => H.select(id));
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") H.select(id); });
    return el;
  }

  function meter(label, p, val) {
    const cls = p >= 90 ? "crit" : p >= 80 ? "warn" : "";
    return h("div", { class: "meter" }, [
      h("span", { text: label }),
      h("div", { class: "tb", title: "Caution at 80%" }, [h("i", { class: cls, style: `width:${clamp(p)}%` }), h("u", { style: "left:80%" })]),
      h("span", { class: "v", text: val })
    ]);
  }

  // ---------- counters ----------
  function tally() {
    const c = { good: 0, warn: 0, crit: 0, unknown: 0 };
    S.nodes.forEach((n) => { c[H.nodeState(n)]++; });
    return c;
  }

  function bar() {
    const c = tally();
    const open = H.alerts().filter((a) => a.severity !== "info" && !isAcked(a)).length;
    const sev = c.crit ? "crit" : c.warn ? "warn" : "good";
    const head = { crit: "Service outage", warn: "Degraded", good: "All systems operational" }[sev];
    const monitored = c.good + c.warn + c.crit;
    const wrap = h("div", { class: "noc-bar" });
    wrap.append(h("div", { class: "noc-overall " + sev }, [
      h("b", { text: head + (H.state.drill ? " (drill)" : "") }),
      h("span", { text: `${c.good} of ${monitored} monitored checks passing · ${c.unknown} not monitored` })
    ]));
    [[c.good, "Up", "good"], [c.warn, "Warning", c.warn ? "warn" : ""], [c.crit, "Down", c.crit ? "crit" : ""],
     [c.unknown, "No check", "unknown"], [open, "Open alarms", open ? "crit" : ""]]
      .forEach(([v, k, cls]) => wrap.append(h("div", { class: "noc-count " + cls }, [h("b", { text: String(v) }), h("span", { text: k })])));
    const now = new Date();
    wrap.append(h("div", { class: "noc-clock" }, [
      h("div", {}, [h("b", { id: "nocLocal", text: fmtClock(now) }), " local"]),
      h("div", {}, [h("b", { id: "nocUtc", text: fmtClock(now, true) }), " UTC"]),
      h("span", { text: `Data ${fmtShort(H.tsDate)}` })
    ]));
    return wrap;
  }

  // ---------- hosts ----------
  const HOST_ORDER = ["isp", "router", "central", "nas", "pc"];
  const HOST_KEYS = ["Ping", "Uptime", "Last speed test", "RAID", "Checked", "Tested", "CPU"];

  function hostPanel(n) {
    const st = H.nodeState(n);
    const el = clickable(h("div", { class: "hostp " + st, "aria-label": `${n.label}: ${H.plainState(st)}` }), n.id);
    el.append(h("div", { class: "hd" }, [h("span", { class: "led " + st }), n.label, h("span", { class: "stw " + st, text: WORD[st] })]));
    el.append(h("div", { class: "role", text: [n.sublabel, n.meta && n.meta.Role].filter(Boolean).join(" · ") || H.kindName(n.kind) }));
    const x = n.stats;
    if (x && x.cpu_pct != null) {
      el.append(meter("CPU", x.cpu_pct, `${x.cpu_pct}%`));
      const mp = Math.round((x.mem.used_gb / x.mem.total_gb) * 100);
      el.append(meter("RAM", mp, `${x.mem.used_gb.toFixed(1)}/${x.mem.total_gb.toFixed(1)} GB`));
      el.append(meter("Swap", Math.round((x.swap.used_gb / x.swap.total_gb) * 100), `${x.swap.used_gb.toFixed(1)}/${x.swap.total_gb.toFixed(1)} GB`));
    }
    if (x && x.disks) x.disks.filter((d) => d.size_gb > 5).forEach((d) => el.append(meter(d.mount.length > 7 ? "Disk" : d.mount, d.pct, `${d.pct}% · ${H.fmtGB(d.size_gb)}`)));
    if (!x && st === "unknown") el.append(h("div", { class: "nodata", text: "NO DATA · not monitored yet" }));
    const kv = h("div", { class: "kvs" });
    const meta = n.meta || {};
    const rows = HOST_KEYS.filter((k) => meta[k]).slice(0, 3).map((k) => [k, meta[k]]);
    if (x && x.load) rows.push(["Load", x.load.join(" ")]);
    if (x && x.temps_c) rows.push(["Temp", x.temps_c.map((t) => t.toFixed(0) + "°C").join(" / ")]);
    if (n.id === "nas") {
      const drives = S.nodes.filter((d) => d.kind === "drive");
      rows.push(["Drives", `${drives.filter((d) => H.nodeState(d) === "good").length}/${drives.length} healthy`]);
    }
    rows.forEach(([k, v]) => kv.append(h("div", {}, [`${k} `, h("b", { text: v })])));
    if (rows.length) el.append(kv);
    return el;
  }

  // ---------- edge & domain ----------
  function edgeCard(id, title, st, lines, meterSpec) {
    const el = clickable(h("div", { class: "hostp " + st, "aria-label": `${title}: ${WORD[st]}` }), id);
    el.append(h("div", { class: "hd" }, [h("span", { class: "led " + st }), title, h("span", { class: "stw " + st, text: st === "good" ? "OK" : WORD[st] })]));
    if (meterSpec) el.append(meter(meterSpec[0], meterSpec[1], meterSpec[2]));
    const kv = h("div", { class: "kvs" });
    lines.forEach(([k, v]) => kv.append(h("div", {}, [`${k} `, h("b", { text: v })])));
    el.append(kv);
    return el;
  }
  function edge() {
    const E = S.edge, cd = H.certDays(), dd = H.daysUntil(E.domain.expires);
    const alertOn = (id, kind) => H.alerts().find((a) => a.target === id && a.kind === kind);
    const certSt = cd < 7 ? "crit" : cd < 21 ? "warn" : "good";
    const domSt = dd < 7 ? "crit" : dd < 30 || !E.domain.auto_renew ? "warn" : "good";
    const dnsAlert = alertOn("domain", "dns_drift");
    const dnsSt = dnsAlert ? dnsAlert.severity : E.dns.missing.length || E.dns.wrong.length || !E.dns.via_eero_ok ? "warn" : "good";
    const caddySt = H.nodeState(H.nodes.caddy);
    const ok = E.dns.expected.length - E.dns.wrong.length - E.dns.missing.length;
    return h("div", { class: "noc-hosts" }, [
      edgeCard("caddy", "HTTPS certificate", certSt, [["Names", E.cert.names.join(", ")], ["Issuer", E.cert.issuer], ["Expires", new Date(E.cert.expires).toLocaleDateString()]],
        ["Used", Math.round(((90 - cd) / 90) * 100), `${cd} days left`]),
      edgeCard("domain", "Domain", domSt, [["Name", E.domain.name], ["Auto-renew", E.domain.auto_renew ? "on" : "OFF"], ["Lock / privacy", `${E.domain.transfer_lock ? "on" : "off"} / ${E.domain.whois_privacy ? "on" : "off"}`]],
        ["Used", Math.round(((365 - dd) / 365) * 100), `${dd} days left`]),
      edgeCard("domain", "DNS records", dnsSt, [["App names", `${ok} of ${E.dns.expected.length} correct`], ["Via Eero", E.dns.via_eero_ok ? "resolves" : "blocked"],
        ["Issue", dnsAlert ? dnsAlert.message.split(",")[0] : "none"]]),
      edgeCard("caddy", "Front door + deploys", caddySt === "good" && E.deploy.ok ? "good" : caddySt === "good" ? "warn" : caddySt,
        [["Caddy", WORD[caddySt]], ["Last deploy", `${E.deploy.version ? "v" + E.deploy.version : E.deploy.commit} · ${E.deploy.ok ? "OK" : "FAILED"}`], ["At", new Date(E.deploy.at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })]])
    ]);
  }

  // ---------- Wi-Fi & devices (Eero cloud API) ----------
  const fmtBytes = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b >= 1e6 ? `${Math.round(b / 1e6)} MB` : `${Math.round((b || 0) / 1e3)} KB`);
  const fmtUp = (sec) => (sec == null ? "—" : sec >= 86400 ? `${Math.floor(sec / 86400)} d` : `${Math.floor(sec / 3600)} h`);
  const tbl = (heads, rows) => h("div", { class: "console" }, [h("table", {}, [
    h("thead", {}, [h("tr", {}, heads.map((x) => h("th", { text: x })))]), h("tbody", {}, rows)])]);
  const td = (v, cls) => h("td", { class: cls || "", text: v == null || v === "" ? "—" : String(v) });
  function hourBars(down, up, w = 240, label = "Data use per hour, last 24 hours") {
    const ht = 36, n = Math.max(down.length, 1), max = Math.max(...down, ...up, 1);
    const svg = s("svg", { class: "spark", width: w, height: ht, viewBox: `0 0 ${w} ${ht}`, role: "img", "aria-label": label });
    down.forEach((v, i) => svg.append(s("rect", { x: (i / n) * w + 1, y: ht - (v / max) * (ht - 2), width: w / n - 2, height: (v / max) * (ht - 2), fill: "var(--info)" })));
    return svg;
  }
  function nodeCard(n) {
    const st = n.status === "green" ? "good" : n.status ? "crit" : "unknown";
    const el = edgeCard("router", `${n.name}${n.gateway ? " (gateway)" : ""}`, n.update_available && st === "good" ? "warn" : st,
      [["Model", `${n.model} · v${n.firmware}`], ["Clients", n.clients], ["Mesh", n.wired ? "wired" : `${n.mesh_bars} of 5 bars`],
       ["Up", fmtUp(n.uptime_s)], ["Firmware", n.update_available ? "update available" : "current"]]);
    const kv = el.querySelector(".kvs");
    Object.entries(n.channels || {}).forEach(([band, c]) => kv && el.insertBefore(
      meter(band.includes("2_4") ? "2.4G" : band.includes("6") ? "6G" : "5G", clamp(c.utilization || 0), `ch ${c.channel} · ${c.utilization ?? 0}% busy`), kv));
    return el;
  }
  function wifi() {
    const E = S.eero || {}, L = E.live, U = E.usage, out = [];
    const use = (id, key) => {   // total bytes for one device over a window: [down, up] or null
      const m = key === "d24" ? U && U.devices : Hs && Hs[key];
      return m && m[id] ? m[id] : null;
    };
    const Hs = E.history;
    const bytes = (v) => (v ? `↓${fmtBytes(v[0])} ↑${fmtBytes(v[1])}` : "—");
    if (L) {
      out.push(h("div", { class: "noc-hosts" }, L.nodes.map(nodeCard)));
      const tot = (c) => { const v = use(c.id, "d24"); return v ? v[0] + v[1] : -1; };
      const rows = L.clients.slice().sort((a, b) => tot(b) - tot(a) || a.name.localeCompare(b.name)).map((c) =>
        h("tr", {}, [h("td", {}, [h("b", { text: c.name }), c.new ? " NEW" : "", c.guest ? " (guest)" : "", c.paused ? " (paused)" : ""]), td(c.node),
          td(c.wired ? "wired" : [c.band, c.signal_dbm].filter(Boolean).join(" ")), td(c.ip),
          td(bytes(use(c.id, "d24")), "num"), td(bytes(use(c.id, "d7")), "num"), td(bytes(use(c.id, "d30")), "num")]));
      out.push(h("div", { class: "noc-note", text: `${L.devices.connected} connected · ${L.devices.wired} wired · ${L.devices.known} known · data use is the total over each window, busiest first${L.stale ? " · stale" : ""}` }));
      out.push(tbl(["Device", "Node", "Link", "IP", "24 h", "7 days", "30 days"], rows));
    }
    const charts = [];   // the two usage charts sit at the bottom of the Network settings card, side by side
    if (Hs && Hs.daily && Hs.daily.length) {
      const dn = Hs.daily.map((d) => d.down + d.up), sum = dn.reduce((a, b) => a + b, 0);
      charts.push(h("div", {}, [h("div", { class: "noc-note", text: `Whole network, last ${Hs.daily.length} days (daily total, down + up): ${fmtBytes(sum)} · busiest day ${fmtBytes(Math.max(...dn))}${Hs.stale ? " · stale" : ""}` }),
        hourBars(dn, [], 300, "Data use per day, last 30 days")]));
    }
    if (U) {
      const u = U.usage_24h;
      charts.push(h("div", {}, [h("div", { class: "noc-note", text: `Last 24 h, per hour: ↓${fmtBytes(u.down_bytes)} ↑${fmtBytes(u.up_bytes)}${U.stale ? " · stale" : ""}` }), hourBars(u.hourly_down, u.hourly_up)]));
      const kv = h("div", { class: "kvs" });
      [["Profiles", U.profiles.map((p) => `${p.name} (${p.devices})${p.paused ? " paused" : ""}`).join(", ")],
       ["Guest network", U.guest.enabled ? `on (${U.guest.name})` : "off"],
       ["DHCP reservations", U.reservations.map((r) => `${r.name || "?"} ${r.ip}`).join(", ") || "none"],
       ["Port forwards", U.forwards.map((f) => `${f.name || "?"} ${f.port}${f.enabled ? "" : " (off)"}`).join(", ") || "none"],
       ["Firmware", U.updates.has_update ? `update to ${U.updates.target} pending` : "up to date"],
       ["Eero speed tests", U.speed_tests.slice(0, 5).map((t) => Math.round(t.down_mbps)).join(" · ") + " Mbps down (newest first)"]]
        .forEach(([k, v]) => kv.append(h("div", {}, [`${k} `, h("b", { text: v })])));
      out.push(h("div", { class: "hostp good" }, [h("div", { class: "hd" }, ["Network settings"]), kv, h("div", { class: "noc-pair" }, charts)]));
    } else if (charts.length) {
      out.push(h("div", { class: "noc-pair" }, charts));
    }
    return h("div", { class: "noc-wifi" }, out.length ? out : [h("div", { class: "noc-note", text: "The Eero cloud API hasn't reported yet." })]);
  }

  // ---------- services ----------
  const GROUPS = [["app", "Applications"], ["mount", "Storage mounts (NFS)"], ["drive", "NAS drives"]];
  function tileMetric(n) {
    const m = n.meta || {};
    if (n.id === "replexon" && H.S.backup) {   // backups are this station's real job
      const ok = H.S.backup.last_success, last = H.S.backup.last_run || {};
      if (last.status === "failure") return "✗ last backup failed";
      if (ok && ok.finished_at) return `✓ backed up ${new Date(ok.finished_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
    }
    if (n.kind === "app") return [m.Port && `:${m.Port.split(" ")[0]}`, m["Health check"]].filter(Boolean).join(" · ");
    if (n.kind === "mount") return m.Path || "";
    if (n.kind === "drive") return [m.Temp, m["Reallocated sectors"] != null && `realloc ${m["Reallocated sectors"]}`].filter(Boolean).join(" · ");
    return "";
  }
  function services() {
    const box = h("div");
    GROUPS.forEach(([kind, title]) => {
      const list = S.nodes.filter((n) => n.kind === kind);
      if (!list.length) return;
      box.append(h("div", { class: "tile-group", text: `${title} · ${list.length}` }));
      const grid = h("div", { class: "tiles" });
      list.forEach((n) => {
        const st = H.nodeState(n);
        const word = (kind === "app" ? WORD : WORD_STORE)[st];
        const metric = tileMetric(n);
        const kids = [
          h("div", { class: "t1" }, [h("span", { text: n.label }), h("span", { class: "stw " + st, text: word })]),
          h("div", { class: "m", text: metric || "—" })
        ];
        const P = S.plex;
        if (n.id === "plex" && P && P.available && P.live.stream_count > 0) {
          kids.push(h("div", { class: "m watching" }, [
            document.createTextNode(`▶ ${P.live.stream_count} watching`),
            P.live.transcode_count ? h("span", { class: "tx", text: ` · ${P.live.transcode_count} transcoding` }) : null
          ]));
        }
        grid.append(clickable(h("div", { class: "tile " + st, title: metric, "aria-label": `${n.label}: ${H.plainState(st)}` }, kids), n.id));
      });
      box.append(grid);
    });
    return box;
  }

  // ---------- alarm console ----------
  function consoleBox() {
    const list = H.alerts();
    const box = h("div", { class: "console" });
    if (!list.length) { box.append(h("div", { class: "empty", text: "No active alarms." })); return box; }
    const tb = h("tbody");
    list.forEach((a) => {
      const raised = a.drill && drillAt ? drillAt : H.tsDate;
      const isAck = isAcked(a);
      const tr = h("tr", { class: a.severity + (isAck ? " acked" : ""), tabindex: 0,
        onclick: () => H.select(a.target), onkeydown: (e) => { if (e.key === "Enter") H.select(a.target); } }, [
        h("td", {}, [h("span", { class: "pill " + a.severity, text: SEV_WORD[a.severity] })]),
        h("td", { class: "num", text: fmtShort(raised) }),
        h("td", {}, [h("b", { text: H.targetLabel(a.target) })]),
        h("td", { text: (H.THEME[a.kind] || { plain: a.kind }).plain + (a.drill ? " (drill)" : "") }),
        h("td", { text: a.message }),
        h("td", {}, [a.severity === "info" ? null : !a.drill ? H.ackControl(a) : h("button", { class: "ack", text: isAck ? "Unack" : "Ack",
          "aria-label": `${isAck ? "Unacknowledge" : "Acknowledge"} alarm on ${H.targetLabel(a.target)}`,
          onclick: (e) => { e.stopPropagation(); if (isAck) acked.delete(a.id); else acked.add(a.id); render(); } })])
      ]);
      tb.append(tr);
    });
    box.append(h("table", {}, [h("thead", {}, [h("tr", {}, ["Severity", "Raised", "Object", "Condition", "Message", ""].map((x) => h("th", { text: x })))]), tb]));
    return box;
  }

  // ---------- interfaces ----------
  function isMeasured(l) { return H.state.demo ? !l.planned : typeof l.a_to_b_mbps === "number"; }
  function sample() {
    S.links.forEach((l) => {
      if (!isMeasured(l)) { delete hist[l.id]; return; }
      const { ab, ba } = H.measured(l);
      const jit = H.state.demo ? 0.55 + Math.random() * 0.9 : 1;
      const arr = hist[l.id] || (hist[l.id] = []);
      arr.push((ab + ba) * jit);
      if (arr.length > HIST_N) arr.shift();
    });
  }
  function spark(arr) {
    const w = 110, ht = 22;
    const svg = s("svg", { class: "spark", width: w, height: ht, viewBox: `0 0 ${w} ${ht}`, "aria-hidden": "true" });
    svg.append(s("line", { x1: 0, y1: ht - 1, x2: w, y2: ht - 1, stroke: "var(--border)" }));
    if (!arr || arr.length < 2) return svg;
    const max = Math.max(...arr, 1e-6);
    const pts = arr.map((v, i) => `${(i / (HIST_N - 1)) * w},${ht - 2 - (v / max) * (ht - 5)}`).join(" ");
    svg.append(s("polyline", { points: pts, fill: "none", stroke: "var(--info)", "stroke-width": 1.6, "stroke-linejoin": "round" }));
    return svg;
  }
  function interfaces() {
    const rows = S.links.filter((l) => l.capacity_mbps);
    const tb = h("tbody");
    rows.forEach((l) => {
      const m = isMeasured(l);
      const { ab, ba } = H.measured(l);
      const arr = hist[l.id];
      const cur = m && arr && arr.length ? arr[arr.length - 1] : null;
      const util = cur == null ? null : (cur / l.capacity_mbps) * 100;
      tb.append(h("tr", { tabindex: 0, onclick: () => H.select(l.id), onkeydown: (e) => { if (e.key === "Enter") H.select(l.id); } }, [
        h("td", {}, [h("b", { text: `${H.nodes[l.a].label} ↔ ${H.nodes[l.b].label}` })]),
        h("td", { class: "num", text: l.capacity_mbps >= 1000 ? `${l.capacity_mbps / 1000} Gb` : `${l.capacity_mbps} Mb` }),
        h("td", { class: "num", text: m ? `↓${H.fmtRate(ab)} ↑${H.fmtRate(ba)}` : "not measured" }),
        h("td", {}, [m ? h("div", { class: "utilbar", title: `${util < 0.1 ? "< 0.1" : util.toFixed(1)}%` }, [h("i", { style: `width:${clamp(util)}%` })]) : "—"]),
        h("td", {}, [m ? spark(arr) : "—"])
      ]));
    });
    const box = h("div", { class: "console" }, [h("table", {}, [
      h("thead", {}, [h("tr", {}, ["Interface", "Speed", "Now", "Util", "Last 90 s"].map((x) => h("th", { text: x })))]), tb])]);
    return box;
  }

  // ---------- page ----------
  function section(title, note, body, key) {   // key = a section the viewer can collapse (remembered in this browser)
    const closed = key ? H.pref.get("noc." + key, false) : false;
    const head = key ? h("button", { class: "noc-toggle", type: "button", "aria-expanded": String(!closed),
      onclick: () => { H.pref.set("noc." + key, !closed); render(); } }, [`${closed ? "▸" : "▾"} ${title}`]) : title;
    return h("section", { class: "noc-sec" }, [h("h2", {}, [head, note ? h("small", { text: note }) : null]), closed ? null : body]);
  }
  function render() {
    if (!active) return;
    H.clear(root);
    root.append(bar());
    root.append(section("Alarm console", `${H.alerts().length} active`, consoleBox(), "alarms"));
    root.append(section("Hosts", null, h("div", { class: "noc-hosts" }, HOST_ORDER.map((id) => H.nodes[id]).filter(Boolean).map(hostPanel))));
    root.append(section("Edge & domain", "hahbah.com", edge()));
    root.append(section("Services", null, services()));
    linksBox = h("div");
    linksBox.append(interfaces());
    root.append(section("Interfaces", H.state.demo ? "simulated" : null, h("div", {}, [linksBox,
      h("div", { class: "noc-note", text: H.state.demo
        ? "Demo traffic is on: these rates are simulated."
        : "Only Central's and the NAS's wired ports are measured today. The trend is sampled in this browser; the portal will serve real history." })])));
    if (S.eero) root.append(section("Wi-Fi & devices", "Eero cloud", wifi()));
  }

  H.listeners.push(() => {
    if (H.state.drill && !drillAt) drillAt = new Date();
    if (!H.state.drill) { drillAt = null; ["d1", "d2", "d3", "d4", "d5", "d6", "d7"].forEach((id) => acked.delete(id)); }
    render();
  });
  setInterval(() => {
    sample();
    if (active && linksBox) { H.clear(linksBox); linksBox.append(interfaces()); }
  }, SAMPLE_MS);
  setInterval(() => {
    if (!active) return;
    const now = new Date(), a = document.getElementById("nocLocal"), b = document.getElementById("nocUtc");
    if (a) a.textContent = fmtClock(now);
    if (b) b.textContent = fmtClock(now, true);
  }, 1000);
  sample();

  H.views.noc = { show() { active = true; render(); }, hide() { active = false; } };
})();
