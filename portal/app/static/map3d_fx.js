// ---------- 3D view extras: real sun, atmosphere, weather, night glow, soft contact shadows, tree lights ----------
// Loaded by map3d.js. Everything here is cosmetic and reads the same snapshot (S.weather) and the shared plug store (window.Plugs).
// Two quality tiers: "high" (laptop, iPad) and "low" (phones). A slow first second of frames drops high to low by itself.
import * as THREE from "three";

const LAT = 42.36 * Math.PI / 180, LON = -71.06;   // Boston
const clamp01 = (v) => Math.max(0, Math.min(1, v));

export function pickTier() {
  const q = new URLSearchParams(location.search).get("tier");
  if (q === "low" || q === "high") return q;
  return Math.min(screen.width, screen.height) < 600 ? "low" : "high";
}

// Sun direction for Boston right now (low precision is plenty): returns { e, up, s } unit-ish components (east, up, south) and sinEl.
export function sunNow(d = new Date()) {
  const n = Math.floor((d - Date.UTC(d.getUTCFullYear(), 0, 0)) / 864e5);
  const dec = 23.44 * Math.PI / 180 * Math.sin(2 * Math.PI * (284 + n) / 365);
  const solar = d.getUTCHours() + d.getUTCMinutes() / 60 + LON / 15;   // mean solar time, hours
  const ha = (solar - 12) * 15 * Math.PI / 180;
  const sinEl = Math.sin(LAT) * Math.sin(dec) + Math.cos(LAT) * Math.cos(dec) * Math.cos(ha);
  const cosEl = Math.sqrt(1 - sinEl * sinEl) || 1e-6;
  const east = -Math.cos(dec) * Math.sin(ha) / cosEl;
  const north = (Math.cos(LAT) * Math.sin(dec) - Math.sin(LAT) * Math.cos(dec) * Math.cos(ha)) / cosEl;
  return { e: east * cosEl, up: sinEl, s: -north * cosEl, sinEl };
}

// WMO weather code to the same few kinds the weather chip uses.
export function wxKind(code) {
  if (code === 0 || code === 1) return "clear";
  if (code === 2) return "part";
  if (code === 45 || code === 48) return "fog";
  if ((code >= 51 && code <= 67) || (code >= 80 && code <= 82)) return "rain";
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "snow";
  if (code >= 95) return "storm";
  return "cloud";
}
const SUN_DIM = { clear: 1, part: 0.9, cloud: 0.65, fog: 0.5, rain: 0.5, snow: 0.6, storm: 0.38 };

function radial(stops, size = 64) {
  const c = document.createElement("canvas"); c.width = c.height = size;
  const g = c.getContext("2d"), r = size / 2, gr = g.createRadialGradient(r, r, 0, r, r, r);
  stops.forEach(([o, col]) => gr.addColorStop(o, col));
  g.fillStyle = gr; g.fillRect(0, 0, size, size);
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

function disposeTree(o) {
  o.traverse((x) => { if (x.geometry) x.geometry.dispose(); if (x.material) [].concat(x.material).forEach((m) => m.dispose()); });
}

// ctx: { renderer, scene, hemi, sun, rim, H, UC, G, isWater, riverZ }
export function createFx(ctx) {
  const { renderer, scene, hemi, sun, rim, H, UC, G, isWater } = ctx;
  let tier = pickTier(), P = null, wx = "clear", treeOn = false, flash = 0, nextFlash = 8;
  const glowTex = radial([[0, "rgba(255,255,255,1)"], [0.35, "rgba(255,255,255,0.45)"], [1, "rgba(255,255,255,0)"]]);
  const blobTex = radial([[0, "rgba(0,0,0,0.5)"], [0.55, "rgba(0,0,0,0.28)"], [1, "rgba(0,0,0,0)"]]);
  const base = { hemi: 0, sun: 0, rim: 0 };       // the palette's own intensities; time and weather scale these
  const fxg = new THREE.Group();                  // rebuilt with the world
  const weather = new THREE.Group();              // persistent
  scene.add(fxg, weather);
  let rain = null, snow = null, sparkle = null, bulbs = null, halo = null, haloMat = null, glow = null;

  function applyTier() {
    const low = tier === "low";
    renderer.setPixelRatio(Math.min(low ? 1.5 : 2, window.devicePixelRatio || 1));
    const size = low ? 1536 : 4096;
    if (sun.shadow.mapSize.x !== size) {
      sun.shadow.mapSize.set(size, size);
      if (sun.shadow.map) { sun.shadow.map.dispose(); sun.shadow.map = null; }
    }
    sun.shadow.radius = low ? 3 : 4;
    buildWeather();
  }

  // ----- weather particles (rain streaks, snow flakes) -----
  const BOX = { x: 75, z: 65, y: 42 };
  function buildWeather() {
    weather.children.slice().forEach((c) => { weather.remove(c); disposeTree(c); });
    rain = snow = null;
    const n = tier === "low" ? 450 : 1400;
    const pos = new Float32Array(n * 6);
    for (let i = 0; i < n; i++) {
      const x = UC.x + (Math.random() * 2 - 1) * BOX.x, z = UC.z + (Math.random() * 2 - 1) * BOX.z, y = Math.random() * BOX.y;
      pos.set([x, y, z, x - 0.12, y + 1.4, z], i * 6);
    }
    rain = new THREE.LineSegments(new THREE.BufferGeometry().setAttribute("position", new THREE.BufferAttribute(pos, 3)),
      new THREE.LineBasicMaterial({ color: "#9fb4c8", transparent: true, opacity: 0.5, depthWrite: false, fog: false }));
    rain.frustumCulled = false; rain.visible = false;
    const m = tier === "low" ? 350 : 900, sp = new Float32Array(m * 3);
    for (let i = 0; i < m; i++) sp.set([UC.x + (Math.random() * 2 - 1) * BOX.x, Math.random() * BOX.y, UC.z + (Math.random() * 2 - 1) * BOX.z], i * 3);
    snow = new THREE.Points(new THREE.BufferGeometry().setAttribute("position", new THREE.BufferAttribute(sp, 3)),
      new THREE.PointsMaterial({ color: "#ffffff", size: 3.2, sizeAttenuation: false, map: glowTex, transparent: true, opacity: 0.9, depthWrite: false, fog: false }));
    snow.frustumCulled = false; snow.visible = false;
    weather.add(rain, snow);
  }

  function stepWeather(dt, t) {
    if (rain.visible) {
      const a = rain.geometry.attributes.position, v = a.array, sp = (wx === "storm" ? 52 : 40) * dt;
      for (let i = 0; i < v.length; i += 6) {
        v[i + 1] -= sp; v[i + 4] -= sp;
        v[i] -= sp * 0.08; v[i + 3] -= sp * 0.08;
        if (v[i + 1] < 0) {
          const x = UC.x + (Math.random() * 2 - 1) * BOX.x, z = UC.z + (Math.random() * 2 - 1) * BOX.z;
          v[i] = x; v[i + 1] = BOX.y; v[i + 2] = z; v[i + 3] = x - 0.12; v[i + 4] = BOX.y + 1.4; v[i + 5] = z;
        }
      }
      a.needsUpdate = true;
    }
    if (snow.visible) {
      const a = snow.geometry.attributes.position, v = a.array;
      for (let i = 0; i < v.length; i += 3) {
        v[i + 1] -= 4.5 * dt;
        v[i] += Math.sin(t * 0.9 + i) * 1.1 * dt;
        v[i + 2] += Math.cos(t * 0.7 + i * 0.5) * 0.8 * dt;
        if (v[i + 1] < 0) { v[i] = UC.x + (Math.random() * 2 - 1) * BOX.x; v[i + 1] = BOX.y; v[i + 2] = UC.z + (Math.random() * 2 - 1) * BOX.z; }
      }
      a.needsUpdate = true;
    }
  }

  // ----- lighting from the real sun, dimmed and fogged by the real weather -----
  function lighting() {
    if (!P) return;
    const s = sunNow(), el = Math.max(s.sinEl, 0.32);   // keep shadows sane near the horizon
    const hor = Math.sqrt(Math.max(0, 1 - el * el)), hn = Math.hypot(s.e, s.s) || 1;
    const day = H.state.theme !== "midnight";
    if (day) sun.position.set(UC.x + (s.e / hn) * hor * 110, el * 110, UC.z + (s.s / hn) * hor * 110);
    const dim = SUN_DIM[wx] || 1;
    const k = day ? (s.sinEl > 0 ? 0.6 + 0.4 * clamp01(s.sinEl * 2.5) : 0.5) : 1;   // dusk and night dim the daytime theme a little, never to black
    sun.intensity = base.sun * k * dim;
    hemi.intensity = base.hemi * (day ? 0.85 + 0.15 * k : 1) * (wx === "clear" ? 1 : 0.95);
    rim.intensity = base.rim;
    if (day) {
      const warm = clamp01(1 - s.sinEl * 3.5) * (s.sinEl > -0.1 ? 0.65 : 0);
      sun.color.set(P.sun[0]).lerp(new THREE.Color("#ff9a5a"), warm);
    }
    // Atmosphere: a light fog that thickens with fog, rain and snow. The fog color is the sky/haze color so the edges melt away.
    const thick = { fog: 1, rain: 0.55, storm: 0.7, snow: 0.6, cloud: 0.2 }[wx] || 0;
    const haze = new THREE.Color(P.haze);
    if (wx !== "clear" && wx !== "part" && day) haze.lerp(new THREE.Color("#b4bcc4"), 0.35 * Math.max(thick, 0.3));
    scene.background = haze;
    scene.fog = new THREE.Fog(haze, 250 - thick * 130, 560 - thick * 230);
  }

  function sync() {
    const w = H.S.weather, nx = w && w.current ? wxKind(w.current.code) : "clear";
    wx = nx;
    if (weather.children.length) {
      rain.visible = wx === "rain" || wx === "storm";
      snow.visible = wx === "snow";
      rain.material.color.set(H.state.theme === "midnight" ? "#b9c9ff" : "#9fb4c8");
    }
    const pl = window.Plugs, plug = pl && pl.list().find((p) => /tree/i.test(p.name || p.nickname || ""));
    treeOn = !!plug && pl.state(plug) === "on";
    lighting();
  }

  // ----- pieces that depend on the built world (called at the end of buildWorld) -----
  function afterWorld(pal, data) {
    P = pal;
    base.hemi = pal.hemi[2] * Math.PI; base.sun = pal.sun[1] * Math.PI; base.rim = pal.rim[1] * Math.PI;
    fxg.children.slice().forEach((c) => { fxg.remove(c); disposeTree(c); });
    sparkle = bulbs = halo = glow = null;
    const night = pal.lit > 0;

    // Soft contact shadow under every building (cheap ambient occlusion).
    const walls = data.walls;
    if (walls.length) {
      const g = new THREE.PlaneGeometry(1, 1); g.rotateX(-Math.PI / 2);
      const im = new THREE.InstancedMesh(g, new THREE.MeshBasicMaterial({ map: blobTex, transparent: true, depthWrite: false, opacity: night ? 0.55 : 0.8 }), walls.length);
      const m4 = new THREE.Matrix4(), q = new THREE.Quaternion(), p = new THREE.Vector3(), sc = new THREE.Vector3();
      walls.forEach((b, i) => {
        q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), b.turn);
        im.setMatrixAt(i, m4.compose(p.set(b.x, 0.03, b.z), q, sc.set(b.w + 1.5, 1, b.d + 1.5)));
      });
      im.renderOrder = 1;
      fxg.add(im);
    }

    // Night: a soft additive halo on lit windows (stands in for bloom without a post pass).
    if (night && data.bands.length) {
      const step = tier === "low" ? 3 : 1, pts = [];
      data.bands.forEach((it, i) => { if (i % step === 0) pts.push(it.b.x, it.y + 0.15, it.b.z); });
      glow = new THREE.Points(new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(pts, 3)),
        new THREE.PointsMaterial({ color: pal.band, size: tier === "low" ? 14 : 18, sizeAttenuation: false, map: glowTex, transparent: true, opacity: 0.55,
          depthWrite: false, blending: THREE.AdditiveBlending, fog: false }));
      fxg.add(glow);
    }

    // Sparkles on the water: sun glints by day, moonlight by night.
    const ns = tier === "low" ? 70 : 170, sp = [];
    for (let i = 0, tries = 0; i < ns && tries < 6000; tries++) {
      const x = G.x0 + Math.random() * (G.x1 - G.x0), z = G.z0 + Math.random() * (G.z1 - G.z0);
      if (isWater(x, z)) { sp.push(x, 0.06, z); i++; }
    }
    if (sp.length) {
      const geo = new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(sp, 3))
        .setAttribute("color", new THREE.BufferAttribute(new Float32Array(sp.length).fill(0.5), 3));
      sparkle = new THREE.Points(geo, new THREE.PointsMaterial({ color: night ? "#9fc0ff" : "#fff6dd", size: 4, sizeAttenuation: false, map: glowTex, vertexColors: true,
        transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, fog: false }));
      fxg.add(sparkle);
    }

    // The Common's tree: glows and twinkles when the real "tree" plug is on.
    const tree = new THREE.Group(); tree.position.set(-6.2, 0, -8.2);
    const green = new THREE.MeshStandardMaterial({ color: pal.crowns[0], roughness: 0.9 });
    const trunk = new THREE.Mesh(new THREE.CylinderGeometry(0.12, 0.16, 0.5, 8), new THREE.MeshStandardMaterial({ color: pal.trunk, roughness: 1 }));
    trunk.position.y = 0.25; tree.add(trunk);
    [[0.95, 1.2, 0.5], [0.75, 1.1, 1.25], [0.52, 1.0, 1.95]].forEach(([r, h, y]) => {
      const c = new THREE.Mesh(new THREE.ConeGeometry(r, h, 14), green); c.position.y = y + h / 2; c.castShadow = true; tree.add(c);
    });
    const bp = [], bc = [];
    for (let i = 0; i < 26; i++) {
      const u = i / 26, y = 0.7 + u * 2.2, rr = 0.9 * (1 - u) + 0.08, a = u * 17;
      bp.push(Math.cos(a) * rr, y, Math.sin(a) * rr); bc.push(0.2, 0.2, 0.2);
    }
    bulbs = new THREE.Points(new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(bp, 3))
      .setAttribute("color", new THREE.Float32BufferAttribute(bc, 3)),
      new THREE.PointsMaterial({ size: 6, sizeAttenuation: false, map: glowTex, vertexColors: true, transparent: true, depthWrite: false, fog: false }));
    tree.add(bulbs);
    haloMat = new THREE.PointsMaterial({ color: "#ffd98a", size: 70, sizeAttenuation: false, map: glowTex, transparent: true, opacity: 0, depthWrite: false,
      blending: THREE.AdditiveBlending, fog: false });
    halo = new THREE.Points(new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute([0, 1.7, 0], 3)), haloMat);
    tree.add(halo);
    fxg.add(tree);
    sync();
  }

  const BULB = [[1, 0.25, 0.2], [1, 0.8, 0.25], [0.35, 0.9, 0.4], [0.4, 0.65, 1], [1, 1, 0.85]];
  let lastSun = 0, frames = 0, slow = 0, tAcc = 0;
  function frame(t, dt, motion, now) {
    if (now - lastSun > 60000) { lastSun = now; lighting(); }
    // Quality governor: if the first ~1.5 s of frames are slow (software GL, old phone), drop to the low tier once.
    if (tier === "high" && frames < 90) { frames++; tAcc += dt; if (dt > 0.034) slow++; if (frames === 90 && slow > 45) { tier = "low"; applyTier(); sync(); } }
    if (motion) stepWeather(dt, t);
    if (wx === "storm" && motion) {
      nextFlash -= dt;
      if (nextFlash < 0) { flash = 0.12; nextFlash = 6 + Math.random() * 9; }
    }
    if (flash > 0) { flash -= dt; hemi.intensity = base.hemi * (flash > 0 ? 2.2 : 1); if (flash <= 0) lighting(); }
    if (sparkle) {
      const c = sparkle.geometry.attributes.color, v = c.array;
      for (let i = 0; i < v.length; i += 3) { const k = 0.5 + 0.5 * Math.sin(t * 1.7 + i * 12.9); const s = (k * k) * (wx === "clear" || wx === "part" ? 1 : 0.45); v[i] = v[i + 1] = v[i + 2] = s; }
      c.needsUpdate = true;
    }
    if (bulbs) {
      const c = bulbs.geometry.attributes.color, v = c.array;
      for (let i = 0; i < v.length; i += 3) {
        if (!treeOn) { v[i] = v[i + 1] = v[i + 2] = 0.18; continue; }
        const b = BULB[(i / 3 + (motion ? Math.floor(t * 1.5) : 0)) % BULB.length], k = motion ? 0.75 + 0.25 * Math.sin(t * 3 + i) : 1;
        v[i] = b[0] * k; v[i + 1] = b[1] * k; v[i + 2] = b[2] * k;
      }
      c.needsUpdate = true;
      haloMat.opacity += ((treeOn ? 0.55 : 0) - haloMat.opacity) * Math.min(1, dt * 4);
    }
  }

  applyTier();
  if (window.Plugs) window.Plugs.on(sync);
  return { afterWorld, sync, frame, get tier() { return tier; } };
}
