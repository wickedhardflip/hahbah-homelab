// "Who's home" chips at the end of the Smart plugs strip (home page). Reads GET /api/presence every 60 s while visible and
// hands plugs.js a small drawer (Plugs.extra) so the strip stays one element. Hidden when nobody is set up or presence is unknown.
(function () {
  "use strict";
  const P = window.Plugs;
  if (!P) return;
  let people = [];
  P.extra = (box) => {
    if (!people.length) return;
    box.append(P.h("span", { class: "ps-title", text: "Home" }));
    people.forEach((p) => box.append(P.h("a", { class: "ps-chip " + (p.home ? "on" : "off"), href: "/presence",
      title: `${p.name} is ${p.home ? "home" : "away"}`, "aria-label": `${p.name}: ${p.home ? "home" : "away"}` }, [
      P.h("i", { class: "ps-dot", "aria-hidden": "true" }), P.h("span", { class: "ps-name", text: p.name })])));
  };
  async function load() {
    const r = await P.api("GET", "/api/presence");
    if (r.ok && r.data) {
      people = r.data.stale ? [] : r.data.people.filter((p) => p.home != null);
      if (P.data) P.redraw();
    }
  }
  let timer = 0;
  const tick = () => { clearTimeout(timer); load().finally(() => { timer = setTimeout(tick, document.hidden ? 300000 : 60000); }); };
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
  tick();
})();
