(function () {
  const $ = (id) => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  function el(tag, attrs, text) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = text;
    return n;
  }
  // pts: [[x, y]] in data units; o.extra: dashed projection points; o.ref: a horizontal reference value
  function chart(host, pts, o) {
    host.textContent = "";
    if (pts.length < 2) { host.textContent = "Not enough history yet."; return; }
    const W = 640, H = 200, L = 40, R = 10, T = 10, B = 24;
    const all = pts.concat(o.extra || []);
    const x0 = Math.min(...all.map((p) => p[0])), x1 = Math.max(...all.map((p) => p[0]));
    const X = (x) => L + ((x - x0) / (x1 - x0 || 1)) * (W - L - R);
    const Y = (y) => T + (1 - (y - o.y0) / (o.y1 - o.y0 || 1)) * (H - T - B);
    const svg = el("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": o.label });
    for (const v of [o.y0, (o.y0 + o.y1) / 2, o.y1]) {
      svg.append(el("line", { x1: L, x2: W - R, y1: Y(v), y2: Y(v), class: "rf", opacity: 0.35 }), el("text", { x: 2, y: Y(v) + 4 }, String(Math.round(v))));
    }
    if (o.ref != null) svg.append(el("line", { x1: L, x2: W - R, y1: Y(o.ref), y2: Y(o.ref), class: "rf" }), el("text", { x: L + 4, y: Y(o.ref) - 4 }, o.refLabel));
    const path = (a) => a.map((p, i) => (i ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1)).join(" ");
    svg.append(el("path", { d: path(pts), class: "ln" }));
    if (o.extra) svg.append(el("path", { d: path([pts[pts.length - 1]].concat(o.extra)), class: "pj" }));
    svg.append(el("text", { x: L, y: H - 6 }, o.fromLabel), el("text", { x: W - R, y: H - 6, "text-anchor": "end" }, o.toLabel));
    host.append(svg);
  }
  const day = (s) => Date.parse(s + "T12:00:00Z") / 864e5;
  const fmt = (iso) => new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

  function tile(host, big, small) {
    const t = document.createElement("div"); t.className = "ins-tile";
    const b = document.createElement("b"); b.textContent = big;
    const s = document.createElement("span"); s.textContent = small;
    t.append(b, s); host.append(t);
  }

  function forecast(f) {
    const series = f.series || [];
    const line = $("fcLine");
    if (f.state === "short") line.textContent = "Needs about a week of daily readings before a forecast means anything. " + f.points + " so far.";
    else if (f.state === "flat") line.textContent = "Holding steady at " + f.now_pct + "% used. No fill date.";
    else line.textContent = "At " + f.now_pct + "% used and growing " + f.slope_per_day + " points a day, the disk is full around " + f.full_on +
      " (" + f.full_in_days + " days)." + (f.ninety_on ? " It crosses 90% around " + f.ninety_on + "." : "");
    const pts = series.map((p) => [day(p.day), p.v]);
    let extra = null;
    if (f.state === "filling" && pts.length) {
      const last = pts[pts.length - 1], span = Math.min(f.full_in_days, 90);
      extra = [[last[0] + span, Math.min(100, last[1] + f.slope_per_day * span)]];
    }
    chart($("fcChart"), pts, { y0: 0, y1: 100, ref: 100, refLabel: "full", extra: extra, label: "NAS percent used by day",
      fromLabel: series.length ? series[0].day : "", toLabel: extra ? "projected" : (series.length ? series[series.length - 1].day : "") });
  }

  function isp(r) {
    $("grade").firstElementChild.textContent = r.grade;
    const t = $("tiles"); t.textContent = "";
    tile(t, r.held_pct == null ? "-" : r.held_pct + "%", "download vs plan (" + r.plan.down + " Mbps)");
    tile(t, r.avg_down == null ? "-" : String(r.avg_down), "average down, Mbps");
    tile(t, r.avg_up == null ? "-" : String(r.avg_up), "average up, Mbps (plan " + r.plan.up + ")");
    tile(t, String(r.outage_minutes), "outage minutes");
    tile(t, String(r.slow_days), "slow speed events");
    const pts = r.speed.map((p) => [day(p.day), p.down]);
    chart($("spChart"), pts, { y0: 0, y1: Math.max(r.plan.down, ...r.speed.map((p) => p.down)), ref: r.plan.down, refLabel: "plan", label: "Download speed tests",
      fromLabel: r.speed.length ? r.speed[0].day : "", toLabel: r.speed.length ? r.speed[r.speed.length - 1].day : "" });
    const body = $("outages"); body.textContent = "";
    if (!r.outages.length) { const td = body.insertRow().insertCell(); td.colSpan = 3; td.textContent = "None in the last 30 days."; }
    for (const o of r.outages) {
      const tr = body.insertRow();
      tr.insertCell().textContent = fmt(o.opened_at);
      tr.insertCell().textContent = o.minutes + " min" + (o.open ? " (ongoing)" : "");
      tr.insertCell().textContent = o.saw;
    }
  }

  fetch("/api/insights").then((r) => r.json()).then((d) => { forecast(d.forecast); isp(d.isp); })
    .catch(() => { $("fcLine").textContent = "Could not load insights."; });
  $("ispPrint").addEventListener("click", () => window.print());
  const sw = $("dgOn");
  if (sw) sw.addEventListener("change", () => {
    fetch("/api/insights/digest", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: sw.checked }) })
      .then((r) => { if (!r.ok) throw new Error("save"); $("dgMsg").textContent = sw.checked ? "On: sends Mondays at 7 AM." : "Off."; })
      .catch(() => { sw.checked = !sw.checked; $("dgMsg").textContent = "Could not save."; });
  });
})();
