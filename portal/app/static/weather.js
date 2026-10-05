// Weather chip + card (top right of the map and 3D view; top right above the stats on NOC). Reads S.weather from the shared snapshot (the collector's Open-Meteo job).
// No data, or data older than an hour (the server drops it), hides the chip. Built with DOM calls only, no innerHTML.
(function () {
  "use strict";
  const H = window.HLT, S = H.S, h = H.h, s = H.s;
  const chip = document.getElementById("wxChip"), card = document.getElementById("wxCard");
  if (!chip || !card) return;

  // WMO weather codes -> one of a few glyphs.
  function kind(code) {
    if (code === 0 || code === 1) return "clear";
    if (code === 2) return "part";
    if (code === 3) return "cloud";
    if (code === 45 || code === 48) return "fog";
    if ((code >= 51 && code <= 67) || (code >= 80 && code <= 82)) return "rain";
    if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "snow";
    if (code >= 95) return "storm";
    return "cloud";
  }
  const WORDS = { clear: "Clear", part: "Partly cloudy", cloud: "Cloudy", fog: "Fog", rain: "Rain", snow: "Snow", storm: "Thunderstorms" };
  const CLOUD = "M7 18h10a4 4 0 0 0 .6-7.95A5.5 5.5 0 0 0 7 9.5 4.3 4.3 0 0 0 7 18z";
  function icon(code, day) {
    const k = kind(code), p = (cls, d) => s("path", { class: cls, d });
    const out = [];
    if (k === "clear") {
      if (day === false) out.push(p("moon", "M19 14.5A7.5 7.5 0 0 1 9.5 5 7.5 7.5 0 1 0 19 14.5z"));
      else {
        out.push(s("circle", { class: "sun", cx: 12, cy: 12, r: 4.2 }));
        out.push(p("rays", "M12 2.5v2.3M12 19.2v2.3M2.5 12h2.3M19.2 12h2.3M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"));
      }
    } else if (k === "part") {
      out.push(s("circle", { class: day === false ? "moon" : "sun", cx: 8.5, cy: 8.5, r: 3.4 }));
      out.push(p("cloud", "M8 19h9.5a3.7 3.7 0 0 0 .5-7.4A5 5 0 0 0 8.6 11 4 4 0 0 0 8 19z"));
    } else {
      out.push(p("cloud", CLOUD));
      if (k === "fog") out.push(p("rain", "M5 21h14M8 23h8"));
      if (k === "rain") out.push(p("rain", "M9 20.5l-1 2.5M13 20.5l-1 2.5M17 20.5l-1 2.5"));
      if (k === "snow") out.push(p("flake", "M9 21.5h.01M13 22.5h.01M17 21.5h.01M11 20h.01M15 20h.01"));
      if (k === "storm") out.push(p("bolt", "M13 15l-3 4h3l-1.5 4 4-5.5h-3z"));
    }
    return s("svg", { class: "wx-ico", viewBox: "0 0 24 24", "aria-hidden": "true" }, out);
  }

  const hour12 = (t) => { const x = +t.slice(11, 13); return (x % 12 || 12) + (x < 12 ? "a" : "p"); };
  const dayName = (d, i) => i === 0 ? "Today" : new Date(d + "T12:00:00").toLocaleDateString([], { weekday: "short" });
  const pop = (v) => v ? v + "%" : "";

  function drawChip(w) {
    H.clear(chip);
    chip.append(icon(w.current.code, w.current.day), h("b", {}, [w.current.temp + "°"]), h("span", {}, [w.place.split(",")[0]]));
    chip.setAttribute("aria-label", `Weather in ${w.place}: ${w.current.temp} degrees, ${WORDS[kind(w.current.code)]}. Open forecast`);
  }
  function drawCard(w) {
    H.clear(card);
    const close = h("button", { class: "close", "aria-label": "Close weather" }, ["×"]);
    close.onclick = () => toggle(false);
    card.append(close, h("h2", {}, [w.place]),
      h("div", { class: "wx-now" }, [icon(w.current.code, w.current.day), h("b", {}, [w.current.temp + "°F"]),
        h("div", {}, [WORDS[kind(w.current.code)], h("small", {}, [`High ${w.daily[0].hi}° · low ${w.daily[0].lo}°`])])]),
      h("div", { class: "sec" }, ["Next 24 hours"]),
      h("div", { class: "wx-hours" }, w.hourly.map((x) => h("div", { class: "wx-hour" }, [h("span", {}, [hour12(x.t)]), icon(x.code, true), h("b", {}, [x.temp + "°"]), h("i", {}, [pop(x.pop)])]))),
      h("div", { class: "sec" }, ["7 days"]),
      h("div", { class: "wx-days" }, w.daily.map((x, i) => h("div", { class: "wx-day" }, [h("span", {}, [dayName(x.d, i)]), icon(x.code, true), h("i", {}, [pop(x.pop)]),
        h("span", { class: "t" }, [x.hi + "° ", h("span", {}, [x.lo + "°"])])]))),
      h("div", { class: "wx-foot" }, ["Open-Meteo · % is the chance of precipitation · updates every 15 min"]));
  }
  function toggle(open) {
    if (open && !S.weather) open = false;
    card.hidden = !open;
    chip.setAttribute("aria-expanded", String(open));
  }
  function render() {
    const w = S.weather;
    chip.hidden = !w;
    if (!w) { card.hidden = true; return; }
    try { drawChip(w); drawCard(w); } catch (e) { chip.hidden = true; card.hidden = true; }   // an odd document hides weather, nothing else
  }
  chip.onclick = () => toggle(card.hidden);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !card.hidden) toggle(false); });
  document.addEventListener("click", (e) => { if (!card.hidden && !card.contains(e.target) && !chip.contains(e.target)) toggle(false); });
  // One chip, two homes: the map's corner (Map and 3D share it) or the strip above the NOC stats.
  const mapSlot = document.getElementById("wxMapSlot"), nocSlot = document.getElementById("wxNocSlot");
  function place() {
    const slot = document.body.dataset.view === "noc" ? nocSlot : mapSlot;
    if (slot && chip.parentNode !== slot) slot.append(chip, card);
  }
  new MutationObserver(place).observe(document.body, { attributes: true, attributeFilter: ["data-view"] });
  place();
  H.listeners.push(render);
  render();
})();
