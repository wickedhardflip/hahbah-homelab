// ---------- 3D view: the transit map on a small hand-drawn Boston (Three.js, same snapshot) ----------
// Everything here is authored for the homelab: scenery shapes are typed in by hand in world units
// (1 unit = 10 px of the 2D layout), the street plan and town are procedural with a seeded RNG.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { createFx, pickTier } from "./map3d_fx.js";

const H = window.HLT;
// Team colors and names for the Garden and the bus stop, from the snapshot (our own tables on the server); defaults are the original look.
const TEAM_ART = { arena: { city: "Boston", short: "Celtics", abbr: "BOS", color: "#17803F", color2: "#0F4D26" },
                   busstop: { city: "Charlotte", short: "Hornets", abbr: "CHA", color: "#00788C", color2: "#1D1160" } };
function teamArt(key) {
  const t = ((H.S.sports || {})[key] || {}).team || {}, d = TEAM_ART[key];
  const hex = (v, f) => (typeof v === "string" && /^#[0-9a-f]{6}$/i.test(v) ? v : f), str = (v, f) => (typeof v === "string" && v ? v : f);
  return { city: str(t.city, d.city), short: str(t.short, d.short), abbr: str(t.abbr, d.abbr), color: hex(t.color, d.color), color2: hex(t.color2, d.color2) };
}
// Draw `text` centred at (x, y), shrinking the font until it fits `maxW`.
function fitText(g, text, x, y, maxW, px, weight, family) {
  for (; px > 10; px -= 2) { g.font = `${weight} ${px}px ${family}`; if (g.measureText(text).width <= maxW) break; }
  g.fillText(text, x, y);
}
const host = document.getElementById("view3d");
const W = ([x, y], hgt = 0) => new THREE.Vector3((x - 620) / 10, hgt, (y - 400) / 10);
const col = (c) => new THREE.Color(c || "#888");
const ELEV = 0.76, AZ = 0.62, DIST = 220;
const clamp01 = (v) => Math.max(0, Math.min(1, v));
const rng = (seed) => () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };
const hash3 = (a, b, c) => (((a * 73856093) ^ (b * 19349663) ^ (c * 83492791)) >>> 0) / 4294967296;

// ---------- areas ----------
const SP = H.S.nodes.map((n) => W(H.P(n.id)));
const U = { x0: Math.min(...SP.map((p) => p.x)) - 11, x1: Math.max(...SP.map((p) => p.x)) + 11,
            z0: Math.min(...SP.map((p) => p.z)) - 6, z1: Math.max(...SP.map((p) => p.z)) + 5 };   // usable area
const UC = new THREE.Vector3((U.x0 + U.x1) / 2, 0, (U.z0 + U.z1) / 2);
const G = { x0: U.x0 - 32, x1: U.x1 + 32, z0: U.z0 - 32, z1: U.z1 + 32 };                          // painted ground
const hazeAt = (x, z) => 0.92 * clamp01((Math.max(U.x0 - x, x - U.x1, U.z0 - z, z - U.z1, 0) - 1) / 23);

// ---------- hand-drawn Boston (world units: x east, z south) ----------
const SC = {
  river: { width: 3.2, pts: [[-130, -26.5], [-80, -29.6], [-50, -28.6], [-26, -28], [-4, -28.7], [16, -28.1], [34, -28.5], [56, -27.6]] },
  harbor: [[47, -140], [44, -46], [46, -33], [50, -25.5], [54, -14], [53, -2], [56, 9], [54, 21], [57, 33], [55, 46], [59, 140], [220, 140], [220, -140]],
  airport: [[61, -38], [73, -43], [87, -33], [89, -13], [79, -4], [66, -8], [60, -21]],
  runways: [{ x: 74, z: -23, len: 24, w: 1.5, a: 0.42 }, { x: 72, z: -19, len: 17, w: 1.2, a: -1.05 }],
  islands: [[75, 19, 6], [88, 40, 8], [67, 47, 5]],
  parks: [{ id: "common", x: -4, z: -10, w: 9, d: 7 }, { id: "garden", x: -13, z: -19, w: 6, d: 4.5 }],
  landmarks: [
    { kind: "statehouse", x: -4, z: -15.5, r: 2.2 }, { kind: "customhouse", x: 25, z: 7, r: 2 },
    { kind: "faneuil", x: 24, z: -8, r: 2.1 }, { kind: "oldnorth", x: 46, z: -16, r: 2 },
    { kind: "mit", x: -44, z: -34.5, r: 2.4 }, { kind: "bunker", x: 34, z: -41, r: 2.6 },
    { kind: "fenway", x: -31, z: 19, r: 5.5, id: "sports-ballpark" }, { kind: "citgo", x: -28, z: 7, r: 1.6 },
    { kind: "hatch", x: -40, z: -25.2, r: 1.8, fixed: true }, { kind: "bandstand", x: -2, z: -11, r: 0.8, fixed: true },
    { kind: "garden", x: 36, z: -15.5, r: 3.4, id: "sports-arena" },   // old Boston Garden, North Station beside the Zakim
    { kind: "zakim", x: 40, z: null, r: 0, fixed: true }, { kind: "constitution", x: 48.2, z: -31.4, r: 0, fixed: true, water: true },
    { kind: "drivein", x: 6, z: -44, r: 7, align: "river" },
    { kind: "busstop", x: 22, z: U.z1 + 5, r: 5, id: "sports-busstop", align: "river" }   // A lone bus stop past the south edge, well clear of the rest
  ]
};

// River centreline as a uniform lookup (Catmull-Rom through the hand points).
const RIV = (() => {
  const curve = new THREE.CatmullRomCurve3(SC.river.pts.map(([x, z]) => new THREE.Vector3(x, 0, z)));
  const pts = curve.getSpacedPoints(1200);
  const x0 = SC.river.pts[0][0], x1 = SC.river.pts[SC.river.pts.length - 1][0], step = 0.25;
  const lut = new Float32Array(Math.ceil((x1 - x0) / step) + 1);
  let j = 0;
  for (let i = 0; i < lut.length; i++) {
    const x = x0 + i * step;
    while (j < pts.length - 2 && pts[j + 1].x < x) j++;
    const a = pts[j], b = pts[j + 1], t = b.x === a.x ? 0 : clamp01((x - a.x) / (b.x - a.x));
    lut[i] = a.z + (b.z - a.z) * t;
  }
  return { x0, x1, step, lut };
})();
const riverZ = (x) => RIV.lut[Math.max(0, Math.min(RIV.lut.length - 1, Math.round((x - RIV.x0) / RIV.step)))];
const inRiver = (x, z) => x >= RIV.x0 && x <= RIV.x1 && Math.abs(z - riverZ(x)) < SC.river.width / 2;
function inPoly(x, z, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, zi] = poly[i], [xj, zj] = poly[j];
    if ((zi > z) !== (zj > z) && x < ((xj - xi) * (z - zi)) / (zj - zi) + xi) inside = !inside;
  }
  return inside;
}
const inIsland = (x, z) => SC.islands.some(([cx, cz, r]) => (x - cx) ** 2 + (z - cz) ** 2 < r * r);
const isWater = (x, z) => !inPoly(x, z, SC.airport) && !inIsland(x, z) && (inRiver(x, z) || inPoly(x, z, SC.harbor));
const inPark = (x, z, pad = 0) => SC.parks.some((p) => Math.abs(x - p.x) < p.w / 2 + pad && Math.abs(z - p.z) < p.d / 2 + pad);
function inRunway(x, z, rw) {
  const c = Math.cos(rw.a), s = Math.sin(rw.a), dx = x - rw.x, dz = z - rw.z;
  const u = dx * c + dz * s, v = -dx * s + dz * c;
  return Math.abs(u) < rw.len / 2 && Math.abs(v) < rw.w / 2 ? { u, v } : null;
}

// ---------- palettes ----------
const PAL = {
  day: { haze: "#dce6ec", land: "#f1ede3", street: "#fcfbf7", sand: "#e9ddbf", shallow: "#a8d6e9", deep: "#5c98c6", foam: "#e8f4f9",
    park: "#cedfba", parkDot: "#b4ce9c", path: "#e4d8bd", parking: "#dcd8cf", cars: ["#c0392b", "#2c3e50", "#ecf0f1", "#7f8c8d", "#2e86c1"],
    tints: ["#efe9dd", "#ece5d6", "#f2ece2", "#e9e2d2", "#eee7da", "#e6dfcf"], grass: "#cddcb4", grass2: "#c3d4a8", runway: "#9097a0", mark: "#f7f7f2",
    walls: ["#c66a4e", "#a55c47", "#ebdfc8", "#d4d8de", "#e3c79b"], glass: "#a9c1d6", roof: "#8c6d5a", glassRoof: "#7f93a6", band: "#f6f1e6",
    crowns: ["#5e9a4b", "#4e8a42"], trunk: "#7a5a3c", brick: "#b5553c", stone: "#e3dfd3", white: "#f4efe6", gold: "#d4a53c",
    hemi: ["#eaf2ff", "#a59275", 0.58], sun: ["#fff1dc", 0.72], rim: ["#cfe0ff", 0.18], station: "#ffffff", hubRim: "#141b2b", lit: 0 },
  midnight: { haze: "#0a1022", land: "#121a31", street: "#1b2544", sand: "#1d2740", shallow: "#1b4775", deep: "#0c2346", foam: "#3a6ea3",
    park: "#13301f", parkDot: "#1b4029", path: "#26304c", parking: "#171f38", cars: ["#ff5d57", "#7aa7ff", "#e9edf6", "#6f7890", "#ffb547"],
    tints: ["#131c35", "#141e38", "#111931", "#16203a", "#121b33", "#101830"], grass: "#15301f", grass2: "#122a1b", runway: "#2b3450", mark: "#8e97ad",
    walls: ["#2a2440", "#262038", "#2b3350", "#27304a", "#302b44"], glass: "#26395a", roof: "#1b2238", glassRoof: "#1f2c45", band: "#ffcf7a",
    crowns: ["#1c4a33", "#173f2b"], trunk: "#2a2a33", brick: "#5a3040", stone: "#3a4260", white: "#8e97ad", gold: "#c9a24a",
    hemi: ["#7aa7ff", "#1a1830", 0.5], sun: ["#aabfff", 0.35], rim: ["#7aa7ff", 0.25], station: "#0a1022", hubRim: "#e9edf6", lit: 1 }
};
// Three r169 uses physical light units; the legacy intensities above are scaled by PI to match.
const LU = Math.PI;

// ---------- state ----------
let renderer = null, scene, camera, controls, world, net, labelLayer, failed = false, worldKey = null;
let hemi, sun, rim, fx = null, fxData = null;
let labels = [], trains = [], pulses = [], pickables = [], risers = [], life = [];
let active = false, hoverId = null, anim = null, riseT0 = -Infinity;
let MASK = null;
const curves = {};
const clock = new THREE.Clock();
const MC = new Map();

// ---------- setup ----------
function init() {
  try { renderer = new THREE.WebGLRenderer({ antialias: pickTier() === "high" || (window.devicePixelRatio || 1) < 2 }); }
  catch (e) {
    failed = true;
    host.append(H.h("div", { class: "gl-fail", text: "3D needs WebGL, and this browser didn't provide it. The 2D map and NOC views still work." }));
    return;
  }
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  host.append(renderer.domElement, H.h("div", { class: "vignette" }));
  labelLayer = H.h("div", { class: "lbl-layer" });
  host.append(labelLayer);

  scene = new THREE.Scene();
  camera = new THREE.OrthographicCamera(-50, 50, 30, -30, 1, 1200);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.12;
  controls.mouseButtons = { LEFT: THREE.MOUSE.PAN, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.ROTATE };
  controls.touches = { ONE: THREE.TOUCH.PAN, TWO: THREE.TOUCH.DOLLY_ROTATE };
  controls.screenSpacePanning = false;
  controls.minPolarAngle = 0;
  controls.maxPolarAngle = 1.15;
  controls.minZoom = 0.6;
  controls.maxZoom = 5;

  hemi = new THREE.HemisphereLight();
  sun = new THREE.DirectionalLight();
  rim = new THREE.DirectionalLight();
  sun.position.set(UC.x - 50, 90, UC.z + 60);
  sun.target.position.copy(UC);
  rim.position.set(UC.x + 50, 60, UC.z - 60);
  rim.target.position.copy(UC);
  sun.castShadow = true;
  sun.shadow.mapSize.set(4096, 4096);
  Object.assign(sun.shadow.camera, { left: -95, right: 95, top: 95, bottom: -95, near: 10, far: 320 });
  sun.shadow.bias = -0.0005;
  sun.shadow.normalBias = 0.03;
  sun.shadow.radius = 4;
  scene.add(hemi, sun, sun.target, rim, rim.target);
  fx = createFx({ renderer, scene, hemi, sun, rim, H, UC, G, isWater });
  world = new THREE.Group();
  net = new THREE.Group();
  scene.add(world, net);

  const ray = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  const hit = (e) => {
    const r = renderer.domElement.getBoundingClientRect();
    ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, camera);
    let o = (ray.intersectObjects(pickables, true)[0] || {}).object;
    while (o && !o.userData.id) o = o.parent;
    return o ? o.userData.id : null;
  };
  let down = null;
  renderer.domElement.addEventListener("pointerdown", (e) => { down = [e.clientX, e.clientY]; });
  renderer.domElement.addEventListener("pointerup", (e) => {
    const moved = !down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 5;
    down = null;
    if (moved || e.button !== 0) return;
    const id = hit(e);
    if (id) H.select(id);
  });
  renderer.domElement.addEventListener("pointermove", (e) => {
    if (e.buttons) return;
    const id = hit(e);
    if (id !== hoverId) { hoverId = id; host.classList.toggle("pick", !!id); }
  });
  new ResizeObserver(() => { resize(); fit(); }).observe(host);
}

function resize() {
  if (!renderer) return;
  const w = host.clientWidth, h = host.clientHeight;
  if (w && h) renderer.setSize(w, h, false);
}

// Size the orthographic frustum so the usable area fits for the current rotation.
const CORNERS = [[U.x0, U.z0], [U.x1, U.z0], [U.x0, U.z1], [U.x1, U.z1]].map(([x, z]) => new THREE.Vector3(x, 0, z));
const tv = new THREE.Vector3();
function fit() {
  if (!renderer || !host.clientWidth) return;
  camera.updateMatrixWorld();
  let hx = 1, hy = 1;
  const uc = UC.clone().applyMatrix4(camera.matrixWorldInverse);
  CORNERS.forEach((c) => {
    tv.copy(c).applyMatrix4(camera.matrixWorldInverse);
    hx = Math.max(hx, Math.abs(tv.x - uc.x)); hy = Math.max(hy, Math.abs(tv.y - uc.y));
  });
  const aspect = host.clientWidth / host.clientHeight;
  hx *= 1.04; hy *= 1.08;
  if (hx / hy < aspect) hx = hy * aspect; else hy = hx / aspect;
  Object.assign(camera, { left: -hx, right: hx, top: hy, bottom: -hy });
  camera.updateProjectionMatrix();
}
function place(phi, theta) {
  camera.position.setFromSpherical(new THREE.Spherical(DIST, Math.max(0.0005, phi), theta)).add(controls.target);
  camera.lookAt(controls.target);
}
function reset() {
  anim = null;
  controls.enabled = true;
  controls.target.copy(UC);
  camera.zoom = 1;
  place(Math.PI / 2 - ELEV, AZ);
  fit();
  controls.update();
}
function currentAngles() {
  const s = new THREE.Spherical().setFromVector3(camera.position.clone().sub(controls.target));
  return { phi: s.phi, theta: s.theta };
}
// Fold from straight-down into the tilted view (and grow the town) each time 3D opens.
function fold() {
  controls.target.copy(UC);
  camera.zoom = 1;
  if (!H.state.motion) { riseT0 = -Infinity; reset(); return; }
  place(0.0005, 0);
  fit();
  anim = { from: { phi: 0.0005, theta: 0 }, to: { phi: Math.PI / 2 - ELEV, theta: AZ }, t0: performance.now() + 250, ms: 1300 };
  riseT0 = performance.now() + 250;
  controls.enabled = false;
}
function rotate(dir) {
  if (!renderer) return;
  const a = currentAngles();
  const to = (anim ? anim.to.theta : a.theta) + dir * Math.PI / 2;
  anim = { from: a, to: { phi: a.phi, theta: to }, t0: performance.now(), ms: H.state.motion ? 600 : 0 };
  controls.enabled = false;
}
function stepAnim(now) {
  if (!anim) return;
  const u = anim.ms ? clamp01((now - anim.t0) / anim.ms) : 1;
  const e = u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2;
  place(anim.from.phi + (anim.to.phi - anim.from.phi) * e, anim.from.theta + (anim.to.theta - anim.from.theta) * e);
  fit();
  if (u >= 1) { anim = null; controls.enabled = true; }
}

// ---------- helpers ----------
function roundedCurve(pts, r) {
  const path = new THREE.CurvePath();
  const line = (a, b) => { if (a.distanceTo(b) > 1e-4) path.add(new THREE.LineCurve3(a.clone(), b.clone())); };
  let cur = pts[0];
  for (let i = 1; i < pts.length - 1; i++) {
    const p0 = pts[i - 1], p1 = pts[i], p2 = pts[i + 1];
    const rr = Math.min(r, p1.distanceTo(p0) / 2, p2.distanceTo(p1) / 2);
    const a = p1.clone().sub(p0).normalize().multiplyScalar(-rr).add(p1);
    const b = p2.clone().sub(p1).normalize().multiplyScalar(rr).add(p1);
    line(cur, a);
    path.add(new THREE.QuadraticBezierCurve3(a, p1.clone(), b));
    cur = b;
  }
  line(cur, pts[pts.length - 1]);
  return path;
}
const curveFor = (l) => curves[l.id] || (curves[l.id] = roundedCurve(H.linkPoints(l).map((p) => W(p, 0)), 2.4));
const TRACK_PTS = [];
H.S.links.forEach((l) => { const c = curveFor(l), n = Math.ceil(c.getLength() / 0.8); for (let i = 0; i <= n; i++) { const p = c.getPointAt(i / n); TRACK_PTS.push([p.x, p.z]); } });
const nearTrack = (x, z, r) => TRACK_PTS.some(([a, b]) => (a - x) ** 2 + (b - z) ** 2 < r * r);
const nearStation = (x, z, r) => SP.some((p) => (p.x - x) ** 2 + (p.z - z) ** 2 < r * r);

function dispose(obj) {
  obj.traverse((o) => {
    if (o.geometry) o.geometry.dispose();
    if (o.material) [].concat(o.material).forEach((m) => { if (m.map) m.map.dispose(); m.dispose(); });
  });
}
function empty(group) { group.children.slice().forEach((c) => { group.remove(c); dispose(c); }); }
const mat = (color, extra) => new THREE.MeshStandardMaterial(Object.assign({ color: col(color), roughness: 0.78, metalness: 0 }, extra || {}));
function M(color, o) {
  const key = color + "|" + (o ? JSON.stringify(o) : "");
  if (!MC.has(key)) MC.set(key, mat(color, o));
  return MC.get(key);
}
function part(g, geo, color, x, y, z, o) {
  const m = new THREE.Mesh(geo, typeof color === "string" ? M(color, o) : color);
  m.position.set(x, y, z);
  m.castShadow = true; m.receiveShadow = true;
  g.add(m);
  return m;
}
const box = (g, w, h, d, c, x, y, z, o) => part(g, new THREE.BoxGeometry(w, h, d), c, x, y + h / 2, z, o);
const cyl = (g, rt, rb, h, c, x, y, z, seg = 16, o) => part(g, new THREE.CylinderGeometry(rt, rb, h, seg), c, x, y + h / 2, z, o);
const dome = (g, r, c, x, y, z, o) => part(g, new THREE.SphereGeometry(r, 24, 10, 0, Math.PI * 2, 0, Math.PI / 2), c, x, y, z, o);
function gable(g, w, h, d, c, x, y, z) {
  const geo = new THREE.ExtrudeGeometry(new THREE.Shape([new THREE.Vector2(-w / 2, 0), new THREE.Vector2(w / 2, 0), new THREE.Vector2(0, h)]), { depth: d, bevelEnabled: false });
  geo.translate(0, 0, -d / 2);
  return part(g, geo, c, x, y, z);
}
const rgb = (hex) => { const n = parseInt(hex.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; };
const mixHex = (a, b, t) => "#" + col(a).lerp(col(b), t).getHexString();

// ---------- ground painting ----------
function chamfer(mask, mw, mh, target) {
  const d = new Float32Array(mw * mh), S2 = 1.4142;
  for (let k = 0; k < d.length; k++) d[k] = mask[k] === target ? 1e9 : 0;
  for (let j = 0; j < mh; j++) for (let i = 0; i < mw; i++) {
    const k = j * mw + i; let v = d[k]; if (!v) continue;
    if (i > 0) v = Math.min(v, d[k - 1] + 1);
    if (j > 0) { v = Math.min(v, d[k - mw] + 1); if (i > 0) v = Math.min(v, d[k - mw - 1] + S2); if (i < mw - 1) v = Math.min(v, d[k - mw + 1] + S2); }
    d[k] = v;
  }
  for (let j = mh - 1; j >= 0; j--) for (let i = mw - 1; i >= 0; i--) {
    const k = j * mw + i; let v = d[k]; if (!v) continue;
    if (i < mw - 1) v = Math.min(v, d[k + 1] + 1);
    if (j < mh - 1) { v = Math.min(v, d[k + mw] + 1); if (i < mw - 1) v = Math.min(v, d[k + mw + 1] + S2); if (i > 0) v = Math.min(v, d[k + mw - 1] + S2); }
    d[k] = v;
  }
  return d;
}
function buildMask() {
  if (MASK) return MASK;
  const MG = 4, mw = Math.round((G.x1 - G.x0) * MG), mh = Math.round((G.z1 - G.z0) * MG);
  const water = new Uint8Array(mw * mh);
  for (let j = 0; j < mh; j++) for (let i = 0; i < mw; i++) water[j * mw + i] = isWater(G.x0 + (i + 0.5) / MG, G.z0 + (j + 0.5) / MG) ? 1 : 0;
  MASK = { MG, mw, mh, water, dW: chamfer(water, mw, mh, 1), dL: chamfer(water, mw, mh, 0) };
  return MASK;
}
const maskIdx = (x, z) => {
  const m = MASK, i = Math.min(m.mw - 1, Math.max(0, Math.floor((x - G.x0) * m.MG))), j = Math.min(m.mh - 1, Math.max(0, Math.floor((z - G.z0) * m.MG)));
  return j * m.mw + i;
};
const shoreDist = (x, z) => { const k = maskIdx(x, z); return MASK.water[k] ? 0 : MASK.dL[k] / MASK.MG; };

function paintGround(P) {
  const m = buildMask();
  const PX = 8, cw = Math.round((G.x1 - G.x0) * PX), ch = Math.round((G.z1 - G.z0) * PX);
  const cv = document.createElement("canvas");
  cv.width = cw; cv.height = ch;
  const g = cv.getContext("2d");
  const img = g.createImageData(cw, ch), D = img.data;
  const C = {}; ["land", "street", "sand", "shallow", "deep", "foam", "park", "parkDot", "path", "parking", "grass", "grass2", "runway", "mark", "haze"].forEach((k) => { C[k] = rgb(P[k]); });
  const tints = P.tints.map(rgb), cars = P.cars.map(rgb);
  // Street plan: a Voronoi patchwork of grids at odd angles, like neighbourhoods meeting.
  const r = rng(23);
  const seeds = Array.from({ length: 14 }, () => {
    const a = (r() - 0.5) * 1.04;
    return { x: G.x0 + r() * (G.x1 - G.x0), z: G.z0 + r() * (G.z1 - G.z0), c: Math.cos(a), s: Math.sin(a), bs: 3 + r() * 1.4, ar: 1.5 + r() * 0.6 };
  });
  const T = [0, 0, 0];
  const set = (c) => { T[0] = c[0]; T[1] = c[1]; T[2] = c[2]; };
  const mix = (c, t) => { T[0] += (c[0] - T[0]) * t; T[1] += (c[1] - T[1]) * t; T[2] += (c[2] - T[2]) * t; };
  const [common, garden] = SC.parks;
  const AB = { x0: Math.min(...SC.airport.map((p) => p[0])), x1: Math.max(...SC.airport.map((p) => p[0])), z0: Math.min(...SC.airport.map((p) => p[1])), z1: Math.max(...SC.airport.map((p) => p[1])) };

  function landPixel(x, z) {
    if (x > AB.x0 && x < AB.x1 && z > AB.z0 && z < AB.z1 && inPoly(x, z, SC.airport)) {
      for (const rw of SC.runways) {
        const q = inRunway(x, z, rw);
        if (q) { set(Math.abs(q.v) < 0.07 && Math.floor(q.u / 1.1) % 2 === 0 ? C.mark : C.runway); return; }
      }
      set((Math.floor(x * 0.7 + z * 0.35) & 1) ? C.grass : C.grass2);
      return;
    }
    if (inIsland(x, z)) { set(C.park); if (hash3(Math.floor(x * 2), Math.floor(z * 2), 5) < 0.12) set(C.parkDot); return; }
    for (const p of SC.parks) {
      const dx = x - p.x, dz = z - p.z;
      if (Math.abs(dx) > p.w / 2 || Math.abs(dz) > p.d / 2) continue;
      set(C.park);
      if (hash3(Math.floor(x * 2.5), Math.floor(z * 2.5), 9) < 0.1) set(C.parkDot);
      if (p === common) {
        const k = p.d / p.w;
        if (Math.abs(dz - dx * k) < 0.2 || Math.abs(dz + dx * k) < 0.2 || Math.abs(dz) < 0.15) set(C.path);
        if ((dx - 2) ** 2 + (dz - 1.6) ** 2 < 1.1) { set(C.shallow); }
      } else if (p === garden) {
        if (((dx + 0.3) / 2.1) ** 2 + (dz / 0.9) ** 2 < 1) set(C.shallow);
        if (Math.abs(dx - 0.2) < 0.12 && Math.abs(dz) < 1.1) set(C.path);
      }
      return;
    }
    let b1 = 1e9, b2 = 1e9, si = 0;
    for (let i = 0; i < seeds.length; i++) {
      const dx = x - seeds[i].x, dz = z - seeds[i].z, d = dx * dx + dz * dz;
      if (d < b1) { b2 = b1; b1 = d; si = i; } else if (d < b2) b2 = d;
    }
    const sd = seeds[si];
    const u = x * sd.c + z * sd.s, v = -x * sd.s + z * sd.c;
    const bu = sd.bs * sd.ar, bv = sd.bs;
    const cu = Math.floor(u / bu), cvv = Math.floor(v / bv);
    const eu = u - cu * bu, ev = v - cvv * bv;
    if (Math.sqrt(b2) - Math.sqrt(b1) < 0.4 || eu < 0.17 || ev < 0.17) { set(C.street); }
    else {
      const hh = hash3(cu, cvv, si);
      if (hh < 0.07) { set(C.park); if (hash3(Math.floor(x * 3), Math.floor(z * 3), si) < 0.15) set(C.parkDot); }
      else if (hh < 0.1) { set(C.parking); const q = hash3(Math.floor(u * 5), Math.floor(v * 9), si + 3); if (q < 0.22 && eu > 0.5 && ev > 0.5 && eu < bu - 0.4 && ev < bv - 0.4) { set(cars[Math.floor(q * 40) % cars.length]); mix(C.parking, 0.35); } }
      else { set(tints[Math.floor(hh * 977) % tints.length]); if (eu < 0.42 || ev < 0.42 || eu > bu - 0.25 || ev > bv - 0.25) mix(C.street, 0.35); }
    }
    mix(C.land, 0.5);
  }

  for (let py = 0; py < ch; py++) {
    const z = G.z0 + (py + 0.5) / PX;
    const mj = Math.min(m.mh - 1, Math.floor((z - G.z0) * m.MG));
    for (let px = 0; px < cw; px++) {
      const x = G.x0 + (px + 0.5) / PX;
      const k = mj * m.mw + Math.min(m.mw - 1, Math.floor((x - G.x0) * m.MG));
      if (m.water[k]) {
        const d = m.dW[k] / m.MG;
        set(C.shallow); mix(C.deep, clamp01((d - 0.3) / 6));
        const rip = 1 + 0.035 * Math.sin(x * 1.9 + Math.sin(z * 0.8) * 2.2) * Math.sin(z * 2.3 + x * 0.3);
        T[0] *= rip; T[1] *= rip; T[2] *= rip;
        if (d < 0.35) set(C.foam);
      } else if (m.dL[k] / m.MG < 0.8) set(C.sand);
      else landPixel(x, z);
      mix(C.haze, hazeAt(x, z));
      const o = (py * cw + px) * 4;
      D[o] = T[0]; D[o + 1] = T[1]; D[o + 2] = T[2]; D[o + 3] = 255;
    }
  }
  g.putImageData(img, 0, 0);
  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = renderer.capabilities.getMaxAnisotropy();
  return tex;
}

// ---------- landmarks (primitives only) ----------
function signTexture() {
  const c = document.createElement("canvas"); c.width = 256; c.height = 160;
  const g = c.getContext("2d");
  g.fillStyle = "#f5f5f2"; g.fillRect(0, 0, 256, 160);
  g.fillStyle = "#d8261c"; g.beginPath(); g.moveTo(128, 12); g.lineTo(176, 88); g.lineTo(80, 88); g.closePath(); g.fill();
  g.fillStyle = "#f5f5f2"; g.beginPath(); g.moveTo(128, 38); g.lineTo(152, 76); g.lineTo(104, 76); g.closePath(); g.fill();
  g.fillStyle = "#1c3d8f"; g.font = "bold 44px Arial, sans-serif"; g.textAlign = "center"; g.fillText("CITGO", 128, 140);
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; return t;
}
function marqueeTexture() {
  const c = document.createElement("canvas"); c.width = 256; c.height = 96;
  const g = c.getContext("2d");
  g.fillStyle = "#b8322a"; g.fillRect(0, 0, 256, 96);
  g.fillStyle = "#f6e7c4"; g.fillRect(8, 8, 240, 80);
  g.fillStyle = "#ffd36b"; for (let x = 16; x < 248; x += 16) { g.beginPath(); g.arc(x, 4, 2.5, 0, 7); g.arc(x, 92, 2.5, 0, 7); g.fill(); }
  g.fillStyle = "#b8322a"; g.font = "bold 46px Arial, sans-serif"; g.textAlign = "center"; g.textBaseline = "middle"; g.fillText("DRIVE-IN", 128, 50);
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; return t;
}

function busSignTexture(art) {
  const c = document.createElement("canvas"); c.width = 256; c.height = 128;
  const g = c.getContext("2d");
  g.fillStyle = art.color; g.fillRect(0, 0, 256, 128);
  g.fillStyle = art.color2; g.fillRect(0, 0, 256, 12); g.fillRect(0, 116, 256, 12);
  g.fillStyle = "#ffffff"; g.textAlign = "center"; g.textBaseline = "middle";
  fitText(g, "BUS", 128, 50, 236, 76, "bold", "Arial, sans-serif");
  fitText(g, art.city.toUpperCase(), 128, 94, 232, 34, "bold", "Arial, sans-serif");
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; return t;
}

function gardenTexture(art) {
  const c = document.createElement("canvas"); c.width = 256; c.height = 80;
  const g = c.getContext("2d");
  g.fillStyle = "#f4efe0"; g.fillRect(0, 0, 256, 80);
  g.fillStyle = art.color; g.fillRect(0, 0, 256, 8); g.fillRect(0, 72, 256, 8);
  g.textAlign = "center"; g.textBaseline = "middle";
  fitText(g, art.abbr === "BOS" ? "GARDEN" : art.short.toUpperCase(), 128, 42, 236, 40, "bold", "Georgia, serif");   // Boston keeps its old Garden
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; return t;
}

// Drive-in screen: black when nothing plays; admins see the poster + title, everyone else a warm NOW SHOWING + count.
let drive = null;
const posters = new Map();
function wrapText(g, text, x, y, maxW, lineH, maxLines) {
  const words = String(text).split(/\s+/), lines = [];
  let cur = "";
  words.forEach((w) => { const t = cur ? cur + " " + w : w; if (g.measureText(t).width > maxW && cur) { lines.push(cur); cur = w; } else cur = t; });
  if (cur) lines.push(cur);
  if (lines.length > maxLines) { lines.length = maxLines; lines[maxLines - 1] = lines[maxLines - 1].replace(/\s*\S*$/, "") + "…"; }
  lines.forEach((l, i) => g.fillText(l, x, y + i * lineH));
}
function paintDriveIn() {
  if (!drive) return;
  const P = H.S.plex || {}, L = (P.available && P.live) || {};
  const n = L.stream_count || 0, st = (L.streams || [])[0], admin = window.HOMELAB_ADMIN === true && !!st;
  const key = [n, admin, st && st.title, st && st.thumb].join("|");
  if (drive.key === key) return;
  drive.key = key;
  drive.cone.visible = n > 0;
  drive.glow.intensity = n > 0 ? (drive.night ? 9 : 2) : 0;
  const draw = (img) => {
    const g = drive.ctx, W2 = 512, H2 = 288;
    g.textAlign = "left"; g.textBaseline = "alphabetic";
    if (!n) { g.fillStyle = "#050506"; g.fillRect(0, 0, W2, H2); drive.tex.needsUpdate = true; return; }
    const count = n === 1 ? "1 watching" : `${n} watching`;
    if (admin) {
      g.fillStyle = "#141210"; g.fillRect(0, 0, W2, H2);
      if (img) g.drawImage(img, 20, 58, 140, 210); else { g.fillStyle = "#2a2622"; g.fillRect(20, 58, 140, 210); }
      g.fillStyle = "#ffcf7a"; g.fillRect(0, 0, W2, 42);
      g.fillStyle = "#1b1d22"; g.font = "800 24px Overpass, Arial, sans-serif"; g.fillText("NOW SHOWING", 20, 30);
      g.textAlign = "right"; g.fillText(count, W2 - 20, 30); g.textAlign = "left";
      g.fillStyle = "#fbf6ea"; g.font = "800 30px Overpass, Arial, sans-serif";
      wrapText(g, st.title || "Untitled", 184, 100, W2 - 204, 36, 4);
      if (n > 1) { g.fillStyle = "#c9bfae"; g.font = "600 20px Overpass, Arial, sans-serif"; g.fillText(`+ ${n - 1} more`, 184, 258); }
    } else {
      const grad = g.createLinearGradient(0, 0, 0, H2);
      grad.addColorStop(0, "#ffb347"); grad.addColorStop(1, "#8a3214");
      g.fillStyle = grad; g.fillRect(0, 0, W2, H2);
      g.textAlign = "center"; g.fillStyle = "#fff6e2";
      g.font = "900 64px Overpass, Arial, sans-serif"; g.fillText("NOW SHOWING", W2 / 2, 150);
      g.font = "700 30px Overpass, Arial, sans-serif"; g.fillText(count, W2 / 2, 204);
    }
    drive.tex.needsUpdate = true;
  };
  draw(admin && st.thumb ? posters.get(st.thumb) : null);
  if (admin && st.thumb && !posters.has(st.thumb)) {
    const img = new Image();
    img.onload = () => { posters.set(st.thumb, img); if (drive && drive.key === key) draw(img); };
    img.src = `/api/plex/thumb?path=${encodeURIComponent(st.thumb)}&w=200`;
  }
}

const BUILD = {
  statehouse(g, P) {
    box(g, 3.4, 1.5, 2.2, P.brick, 0, 0, 0);
    [-1, -0.5, 0, 0.5, 1].forEach((i) => cyl(g, 0.09, 0.09, 1.2, P.white, i * 0.7, 0.2, 1.25, 8));
    gable(g, 1.9, 0.5, 0.35, P.white, 0, 1.4, 1.25);
    cyl(g, 0.62, 0.66, 0.45, P.white, 0, 1.5, -0.1, 24);
    dome(g, 0.64, P.gold, 0, 1.95, -0.1, { metalness: 0.8, roughness: 0.3 });
    cyl(g, 0.07, 0.07, 0.4, P.gold, 0, 2.55, -0.1, 8, { metalness: 0.8, roughness: 0.3 });
  },
  customhouse(g, P) {
    box(g, 3, 1.4, 3, "#d8ccb0", 0, 0, 0);
    box(g, 1.4, 5.4, 1.4, "#d8ccb0", 0, 1.4, 0);
    [[0, 1], [0, -1], [1, 0], [-1, 0]].forEach(([dx, dz]) => {
      const f = part(g, new THREE.CylinderGeometry(0.4, 0.4, 0.06, 20), "#fbf6e8", dx * 0.72, 5.9, dz * 0.72, { emissive: "#fff3cf", emissiveIntensity: P.lit * 0.8 });
      if (dz) f.rotation.x = Math.PI / 2; else f.rotation.z = Math.PI / 2;
    });
    part(g, new THREE.ConeGeometry(1.05, 1.5, 4), "#5f9f86", 0, 6.8 + 0.75, 0).rotation.y = Math.PI / 4;
  },
  oldnorth(g, P) {
    box(g, 1.6, 1.4, 3.2, "#a4523b", 0, 0, 0.5);
    gable(g, 1.8, 0.7, 3.3, "#5b4035", 0, 1.4, 0.5);
    box(g, 1, 1.7, 1, "#a4523b", 0, 0, -1.6);
    box(g, 0.8, 0.9, 0.8, P.white, 0, 1.7, -1.6);
    box(g, 0.6, 0.7, 0.6, P.white, 0, 2.6, -1.6);
    box(g, 0.44, 0.5, 0.44, P.white, 0, 3.3, -1.6);
    part(g, new THREE.ConeGeometry(0.28, 1.7, 8), P.white, 0, 3.8 + 0.85, -1.6);
    part(g, new THREE.SphereGeometry(0.13, 12, 8), P.gold, 0, 5.7, -1.6, { metalness: 0.8, roughness: 0.3 });
  },
  faneuil(g, P) {
    box(g, 3.2, 1.3, 1.7, P.brick, 0, 0, 0);
    box(g, 3.24, 0.14, 1.74, P.band, 0, 0.35, 0, { emissive: P.band, emissiveIntensity: P.lit * 0.6 });
    box(g, 3.24, 0.14, 1.74, P.band, 0, 0.85, 0, { emissive: P.band, emissiveIntensity: P.lit * 0.6 });
    gable(g, 1.85, 0.6, 3.3, "#6b4a3a", 0, 1.3, 0).rotation.y = Math.PI / 2;
    cyl(g, 0.3, 0.3, 0.5, P.white, 0, 1.75, 0, 12);
    dome(g, 0.3, P.gold, 0, 2.25, 0, { metalness: 0.8, roughness: 0.3 });
    part(g, new THREE.ConeGeometry(0.08, 0.35, 6), P.gold, 0, 2.7, 0, { metalness: 0.8, roughness: 0.3 });
  },
  mit(g, P) {
    box(g, 4.6, 1.5, 2.2, P.stone, 0, 0, 0);
    [-1.25, -0.75, -0.25, 0.25, 0.75, 1.25].forEach((x) => cyl(g, 0.1, 0.1, 1.25, P.stone, x, 0.15, 1.25, 8));
    gable(g, 2.9, 0.6, 0.35, P.stone, 0, 1.4, 1.25);
    cyl(g, 0.9, 0.95, 0.6, P.stone, 0, 1.5, -0.2, 28);
    dome(g, 0.95, "#c9ccd1", 0, 2.1, -0.2);
  },
  bunker(g, P) {
    const mound = part(g, new THREE.SphereGeometry(2.6, 28, 8, 0, Math.PI * 2, 0, Math.PI / 2), "#8fbf6e", 0, 0, 0);
    mound.scale.y = 0.25;
    part(g, new THREE.CylinderGeometry(0.28, 0.55, 5, 4), "#d9d4c7", 0, 0.55 + 2.5, 0).rotation.y = Math.PI / 4;
    part(g, new THREE.ConeGeometry(0.4, 0.6, 4), "#d9d4c7", 0, 5.85, 0).rotation.y = Math.PI / 4;
  },
  fenway(g, P) {
    box(g, 8, 0.06, 8, "#6fae55", 0, 0, 0);
    part(g, new THREE.BoxGeometry(2.6, 0.08, 2.6), "#c79a63", 1.8, 0.05, 1.8).rotation.y = Math.PI / 4;
    part(g, new THREE.BoxGeometry(1.9, 0.09, 1.9), "#6fae55", 1.8, 0.06, 1.8).rotation.y = Math.PI / 4;
    cyl(g, 0.25, 0.3, 0.12, "#c79a63", 1.8, 0.05, 1.8, 12);
    box(g, 8.6, 1.6, 0.35, "#2e5e3a", 0, 0, -4.15);              // the Green Monster
    box(g, 0.9, 1.0, 8.6, "#9aa3ad", 4.45, 0, 0);                  // stands
    box(g, 8.6, 1.0, 0.9, "#9aa3ad", 0, 0, 4.45);
    box(g, 0.6, 0.8, 8.6, "#9aa3ad", -4.3, 0, 0);
    [[-4, -4], [4, -4], [-4, 4], [4, 4]].forEach(([x, z]) => {
      cyl(g, 0.07, 0.09, 3.4, "#b9bec4", x, 0, z, 6);
      box(g, 0.55, 0.3, 0.2, "#fbf6e8", x, 3.4, z, { emissive: "#fff6d8", emissiveIntensity: 0.3 + P.lit });
    });
  },
  citgo(g, P) {
    box(g, 2.2, 2.2, 2, "#c9c2b3", 0, 0, 0);
    box(g, 0.1, 0.5, 0.1, "#6b6f76", -0.8, 2.2, 0); box(g, 0.1, 0.5, 0.1, "#6b6f76", 0.8, 2.2, 0);
    const tx = signTexture();
    const sign = new THREE.Mesh(new THREE.PlaneGeometry(2.1, 1.3), new THREE.MeshStandardMaterial({ map: tx, emissiveMap: tx, side: THREE.DoubleSide, roughness: 0.6, emissive: col("#ffffff"), emissiveIntensity: P.lit * 0.7 }));
    sign.position.set(0, 3.35, 0.05); sign.castShadow = true;
    g.add(sign);
  },
  // Drive-in theater: shows what Plex is playing. Lined up with the river; clicking it opens the Plex card.
  drivein(g, P) {
    const night = P.lit > 0;
    g.userData.id = "plex";
    box(g, 10, 0.04, 9.2, P.parking, 0, 0, 1);
    // big screen: white frame on two posts, picture on a canvas texture; faces south across the river
    box(g, 0.3, 6.9, 0.3, "#6b6f76", -3.8, 0, -3.5); box(g, 0.3, 6.9, 0.3, "#6b6f76", 3.8, 0, -3.5);
    box(g, 10.1, 5.9, 0.2, "#f4f1ea", 0, 0.95, -3.32);
    const c = document.createElement("canvas"); c.width = 512; c.height = 288;
    const tex = new THREE.CanvasTexture(c); tex.colorSpace = THREE.SRGBColorSpace;
    const screen = new THREE.Mesh(new THREE.PlaneGeometry(9.6, 5.4), new THREE.MeshBasicMaterial({ map: tex, toneMapped: false }));
    screen.position.set(0, 3.9, -3.2);
    g.add(screen);
    // small lot: three rows of parked cars facing the screen, a speaker post beside each
    const glassC = night ? "#26395a" : "#a9c1d6";
    [0, 1.7, 3.4].forEach((z, row) => [-3.2, -1.1, 1.1, 3.2].forEach((x, i) => {
      if ((row * 4 + i) % 5 === 3) return;   // a few empty bays
      box(g, 0.75, 0.27, 1.35, P.cars[(row * 3 + i) % P.cars.length], x, 0.09, z);
      box(g, 0.62, 0.22, 0.68, glassC, x, 0.36, z + 0.11);
      box(g, 0.06, 0.7, 0.06, "#8a8f98", x + 1.05, 0, z + 0.25);
      box(g, 0.17, 0.12, 0.09, "#5b6068", x + 1.05, 0.7, z + 0.25);
    }));
    // fence with a gap at the front-right entrance
    const fence = "#a08a6a";
    box(g, 10, 0.07, 0.07, fence, 0, 0.55, -3.6); box(g, 0.07, 0.07, 9.2, fence, -5, 0.55, 1);
    box(g, 0.07, 0.07, 9.2, fence, 5, 0.55, 1); box(g, 7.5, 0.07, 0.07, fence, -1.25, 0.55, 5.6);
    for (let x = -5; x <= 2.5; x += 1.25) box(g, 0.08, 0.62, 0.08, fence, x, 0, 5.6);
    for (let z = -3.6; z <= 5.6; z += 1.15) { box(g, 0.08, 0.62, 0.08, fence, -5, 0, z); box(g, 0.08, 0.62, 0.08, fence, 5, 0, z); }
    // projector booth + light cone (cone only while something plays)
    box(g, 1.2, 0.9, 0.9, P.white, 0, 0, 5.0);
    const from = new THREE.Vector3(0, 0.8, 4.5), to = new THREE.Vector3(0, 3.9, -3.15), len = from.distanceTo(to);
    const coneGeo = new THREE.ConeGeometry(4.4, len, 28, 1, true);
    coneGeo.translate(0, -len / 2, 0); coneGeo.rotateX(-Math.PI / 2);
    const cone = new THREE.Mesh(coneGeo, new THREE.MeshBasicMaterial({ color: col("#fff2c4"), transparent: true, opacity: night ? 0.16 : 0.07,
      blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide }));
    cone.position.copy(from);
    cone.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), to.clone().sub(from).normalize());
    g.add(cone);
    const glow = new THREE.PointLight(col("#ffd28a"), 0, 16, 1);
    glow.position.set(0, 3.5, -1.5);
    g.add(glow);
    // DRIVE-IN marquee by the entrance
    box(g, 0.12, 2.2, 0.12, "#6b6f76", 3.4, 0, 6.2); box(g, 0.12, 2.2, 0.12, "#6b6f76", 5.6, 0, 6.2);
    const mt = marqueeTexture();
    const sign = new THREE.Mesh(new THREE.PlaneGeometry(2.4, 0.9), new THREE.MeshStandardMaterial({ map: mt, emissiveMap: mt,
      side: THREE.DoubleSide, roughness: 0.6, emissive: col("#ffffff"), emissiveIntensity: P.lit * 0.8 }));
    sign.position.set(4.5, 2.35, 6.27); sign.castShadow = true;
    g.add(sign);
    drive = { ctx: c.getContext("2d"), tex, cone, glow, night, key: null };
  },
  hatch(g, P) {
    box(g, 2.8, 0.25, 1.8, "#e9e4d8", 0, 0, 0);
    const shell = part(g, new THREE.SphereGeometry(1.35, 24, 10, 0, Math.PI, 0, Math.PI / 2), "#f4f1ea", 0, 0.25, 0.4);
    shell.rotation.y = Math.PI;
  },
  bandstand(g, P) {
    cyl(g, 0.55, 0.6, 0.45, P.white, 0, 0, 0, 12);
    part(g, new THREE.ConeGeometry(0.8, 0.5, 8), "#6b8f71", 0, 0.7, 0);
  },
  // The old Boston Garden: brick arena, team-color band (Celtics green by default), banners, marquee, and North Station's canopy alongside.
  garden(g, P) {
    const art = teamArt("arena");
    box(g, 7.4, 0.06, 5.6, "#c9c2b0", 0, 0, 0);                    // plaza
    box(g, 6, 2.3, 4.2, "#9c5a43", 0, 0.06, -0.2);                 // brick hall
    box(g, 5.2, 0.5, 3.6, "#8a4c38", 0, 2.36, -0.2);               // upper tier
    box(g, 6.04, 0.28, 4.24, art.color, 0, 1.7, -0.2);              // team-color band (Celtic green for Boston)
    box(g, 6.3, 0.16, 4.5, "#6f7378", 0, 2.86, -0.2);              // roof
    cyl(g, 0.45, 0.6, 0.35, "#6f7378", 0, 3.02, -0.2, 10);         // cupola
    const tx = gardenTexture(art);
    const sign = new THREE.Mesh(new THREE.PlaneGeometry(2.6, 0.8), new THREE.MeshStandardMaterial({ map: tx, emissiveMap: tx, roughness: 0.6, emissive: col("#ffffff"), emissiveIntensity: P.lit * 0.6 }));
    sign.position.set(0, 1.25, 1.93); sign.castShadow = true;
    g.add(sign);
    for (let i = 0; i < 5; i++) box(g, 0.34, 0.6, 0.05, i % 2 ? "#f4efe0" : art.color, -1.6 + i * 0.8, 2.4, 1.64);   // banners
    [-0.9, 0.9].forEach((x) => box(g, 0.7, 0.8, 0.06, "#3a2a24", x, 0.06, 1.9));                                  // doors
    [-1, 1].forEach((s) => box(g, 0.1, 1.1, 0.1, "#f4efe0", s * 3.1, 0.06, 1.95));                              // flagpoles' stubs
    box(g, 3.4, 0.14, 1.4, "#5d6168", 0, 0.9, 2.15 + 0.7);          // North Station canopy
    [-1.5, 1.5].forEach((x) => box(g, 0.12, 0.9, 0.12, "#5d6168", x, 0, 2.95));
  },
  // A lone bus stop in the chosen team's colors (Hornets teal + purple by default): shelter, bench, and a pole with a BUS + city blade sign.
  busstop(g, P) {
    const art = teamArt("busstop"), C1 = art.color, C2 = art.color2;
    const b = new THREE.Group(); b.scale.setScalar(2.2); g.add(b);   // oversized so the BUS sign reads at default zoom; the landmark entry aligns it to the river like the drive-in
    box(b, 4.2, 0.06, 2.4, "#bdb8ac", 0, 0, 0.1);                        // pad (room for the bus)
    box(b, 2.2, 0.08, 1.05, C2, -0.5, 1.55, -0.1);              // roof
    box(b, 2.1, 1.45, 0.05, C1, -0.5, 0.06, -0.58, { transparent: true, opacity: 0.55 });   // back panel
    [-1.5, 0.5].forEach((x) => [-0.55, 0.35].forEach((z) => box(b, 0.07, 1.5, 0.07, "#f4f4f2", x, 0.06, z)));
    box(b, 1.5, 0.08, 0.34, "#f4f4f2", -0.5, 0.5, -0.38);              // bench
    const tx = busSignTexture(art);
    const blade = new THREE.Mesh(new THREE.PlaneGeometry(1.8, 0.9), new THREE.MeshStandardMaterial({ map: tx, emissiveMap: tx, roughness: 0.6, emissive: col("#ffffff"), emissiveIntensity: P.lit * 0.5, side: THREE.DoubleSide }));
    blade.position.set(1.55 + 0.95, 2.45, 0.45); blade.castShadow = true;
    b.add(blade);
    cyl(b, 0.055, 0.07, 2.65, C2, 1.55, 0, 0.45, 8);            // pole
    cyl(b, 0.11, 0.11, 0.09, C1, 1.55, 2.6, 0.45, 12);          // cap
    // a small bus parked in front of the shelter, in the team color with a dark stripe
    const bus = new THREE.Group(); bus.position.set(-0.5, 0.06, 0.78); b.add(bus);
    box(bus, 2.1, 0.5, 0.62, C1, 0, 0.12, 0);                   // body
    box(bus, 2.12, 0.1, 0.64, C2, 0, 0.12, 0);                  // purple stripe
    box(bus, 1.9, 0.2, 0.64, "#cfe6ee", 0, 0.38, 0);                   // windows
    box(bus, 2.1, 0.06, 0.66, C2, 0, 0.62, 0);                  // roof
    [-0.65, 0.65].forEach((x) => [-0.3, 0.3].forEach((z) => { const w = cyl(bus, 0.13, 0.13, 0.08, "#1c1c1c", x, 0, z, 12); w.rotation.x = Math.PI / 2; w.position.y = 0.13; }));
  },
  zakim(g, P) {
    box(g, 1.4, 0.25, 15, "#d9dde2", 0, 0.5, 0);
    [-3.6, 3.6].forEach((z) => {
      [-1, 1].forEach((s) => { const leg = box(g, 0.24, 6.6, 0.3, "#f2f4f6", s * 0.55, 0, z); leg.position.y = 3.2; leg.rotation.z = s * 0.17; });
      box(g, 0.22, 1.2, 0.26, "#f2f4f6", 0, 6.3, z);
    });
    const pts = [];
    [-3.6, 3.6].forEach((z) => { for (let i = 1; i <= 6; i++) [-1, 1].forEach((sd) => { pts.push(0, 7.2, z, sd * 0.6, 0.75, z + (z < 0 ? -1 : 1) * i * 0.6 * (i > 3 ? 1.1 : 1)); pts.push(0, 7.0, z, sd * 0.6, 0.75, z - (z < 0 ? -1 : 1) * i * 0.55); }); });
    const geo = new THREE.BufferGeometry(); geo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
    g.add(new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color: col("#f7f8fa") })));
  },
  constitution(g, P) {
    const ship = new THREE.Group(); g.add(ship);
    box(ship, 3.6, 0.6, 0.8, "#2b2b2e", 0, -0.2, 0);
    box(ship, 3.62, 0.12, 0.82, "#e8e2d0", 0, 0.15, 0);
    [[-1.1, 2.6], [0, 3.1], [1.1, 2.4]].forEach(([x, h]) => {
      cyl(ship, 0.05, 0.06, h, "#5a4632", x, 0.4, 0, 6);
      box(ship, 0.05, 0.7, 0.95, "#fbf8f0", x, 0.9, 0);
      box(ship, 0.05, 0.5, 0.7, "#fbf8f0", x, 1.75, 0);
    });
    box(ship, 0.02, 0.22, 0.4, "#c0392b", -1.1, 2.75, 0.2);
    life.push((t) => { ship.position.y = 0.05 * Math.sin(t * 1.3); ship.rotation.x = 0.03 * Math.sin(t * 1.1); });
  }
};

// Where track crosses water: the Red Line gets the Longfellow's "salt and pepper" towers, others a plain deck.
function bridges(P) {
  H.S.links.forEach((l) => {
    const c = curveFor(l), len = c.getLength();
    const wet = [];
    for (let d = 0; d <= len; d += 0.4) { const p = c.getPointAt(d / len); if (isWater(p.x, p.z)) wet.push(d); }
    if (!wet.length) return;
    const d0 = wet[0] - 1.2, d1 = wet[wet.length - 1] + 1.2, mid = c.getPointAt(clamp01((d0 + d1) / 2 / len));
    const t = c.getTangentAt(clamp01((d0 + d1) / 2 / len));
    const g = new THREE.Group();
    g.position.copy(mid);
    g.rotation.y = Math.atan2(t.x, t.z);
    const L = d1 - d0;
    box(g, 2.8, 0.3, L, "#bfb29a", 0, -0.12, 0);
    if (l.kind === "wan") {
      for (let i = 0; i < 4; i++) {
        const z = -L / 2 + (L / 3) * i;
        [-1.45, 1.45].forEach((x) => { cyl(g, 0.32, 0.36, 1.2, "#c8baa0", x, 0, z, 16); dome(g, 0.34, "#c8baa0", x, 1.2, z); });
      }
    }
    world.add(g);
  });
}

// ---------- the town ----------
function riseF(delay, now) {
  if (riseT0 === -Infinity) return 1;
  const u = clamp01((now - riseT0 - delay * 1000) / 600);
  return 1 - Math.pow(1 - u, 3);
}
function instanced(geo, material, items, apply) {
  if (!items.length) return;
  const im = new THREE.InstancedMesh(geo, material, items.length);
  im.castShadow = true; im.receiveShadow = true;
  items.forEach((it, i) => im.setColorAt(i, col(it.color)));
  world.add(im);
  const r = { im, items, apply };
  risers.push(r);
  updateRiser(r, performance.now());
}
const M4 = new THREE.Matrix4(), Q = new THREE.Quaternion(), V3 = new THREE.Vector3(), S3 = new THREE.Vector3(), UP = new THREE.Vector3(0, 1, 0);
function updateRiser(r, now) {
  r.items.forEach((it, i) => { r.apply(it, Math.max(0.001, riseF(it.delay, now))); r.im.setMatrixAt(i, M4.compose(V3, Q, S3)); });
  r.im.instanceMatrix.needsUpdate = true;
}

function buildWorld() {
  const key = H.state.theme;
  if (worldKey === key) return;
  worldKey = key;
  empty(world);
  MC.clear();
  risers = []; life = []; drive = null;
  const P = PAL[key] || PAL.day;
  hemi.color.set(P.hemi[0]); hemi.groundColor.set(P.hemi[1]); hemi.intensity = P.hemi[2] * LU;
  sun.color.set(P.sun[0]); sun.intensity = P.sun[1] * LU;
  rim.color.set(P.rim[0]); rim.intensity = P.rim[1] * LU;
  scene.background = col(P.haze);

  const ground = new THREE.Mesh(new THREE.PlaneGeometry(G.x1 - G.x0, G.z1 - G.z0), mat("#ffffff", { map: paintGround(P), roughness: 1 }));
  ground.rotation.x = -Math.PI / 2;
  ground.position.set((G.x0 + G.x1) / 2, 0, (G.z0 + G.z1) / 2);
  ground.receiveShadow = true;
  world.add(ground);
  const outer = new THREE.Mesh(new THREE.PlaneGeometry(1200, 1200), mat(P.haze, { roughness: 1 }));
  outer.rotation.x = -Math.PI / 2; outer.position.set(UC.x, -0.05, UC.z);
  world.add(outer);
  bridges(P);

  // Landmarks: hand-picked spots, nudged outward if a track or station is in the way.
  const placed = [];
  SC.landmarks.forEach((lm) => {
    let x = lm.x, z = lm.z == null ? riverZ(lm.x) : lm.z;
    if (!lm.fixed) {
      const ok = (px, pz) => !isWater(px, pz) && !nearTrack(px, pz, 3.4 + lm.r * 0.6) && !nearStation(px, pz, 4.6 + lm.r * 0.6);
      search: for (let rad = 0; rad <= 8; rad += 1) for (let a = 0; a < 12; a++) {
        const px = lm.x + Math.cos(a * Math.PI / 6) * rad, pz = lm.z + Math.sin(a * Math.PI / 6) * rad;
        if (ok(px, pz)) { x = px; z = pz; break search; }
        if (rad === 0) break;
      }
    }
    const g = new THREE.Group();
    BUILD[lm.kind](g, P);
    g.position.set(x, 0, z);
    if (lm.align === "river") g.rotation.y = Math.atan2(-(riverZ(x + 5) - riverZ(x - 5)), 10);   // run parallel to the river
    if (lm.id) g.userData.id = lm.id;   // clickable: opens the team card
    g.userData.delay = Math.hypot(x - UC.x, z - UC.z) * 0.011;
    g.userData.rise = true;
    world.add(g);
    placed.push([x, z, lm.r]);
  });

  // Town: rejection-sampled buildings (70 inside the usable area, 190 in the ring), some become trees.
  const r = rng(7), spots = [];
  const okSpot = (x, z) => !isWater(x, z) && shoreDist(x, z) > 1.2 && !inPoly(x, z, SC.airport) && !inPark(x, z, 0.6) &&
    !nearTrack(x, z, 3.4) && !nearStation(x, z, 4.6) && !placed.some(([a, b, rr]) => (a - x) ** 2 + (b - z) ** 2 < (rr + 1.6) ** 2) &&
    !spots.some((s) => (s.x - x) ** 2 + (s.z - z) ** 2 < 2.3 * 2.3);
  const sample = (inside, want, tries, treeP) => {
    let got = 0;
    for (let i = 0; i < tries && got < want; i++) {
      const x = G.x0 + r() * (G.x1 - G.x0), z = G.z0 + r() * (G.z1 - G.z0);
      const isIn = x > U.x0 && x < U.x1 && z > U.z0 && z < U.z1;
      if (isIn !== inside || !okSpot(x, z)) continue;
      spots.push({ x, z, inside, tree: r() < treeP });
      got++;
    }
  };
  sample(true, 70, 4000, 0.22);
  sample(false, 190, 6000, 0.3);

  const walls = [], glass = [], roofs = [], bands = [], trees = [];
  const haze = P.haze;
  spots.forEach((s) => {
    const hz = hazeAt(s.x, s.z), delay = Math.hypot(s.x - UC.x, s.z - UC.z) * 0.011;
    if (s.tree) { trees.push({ x: s.x, z: s.z, s: 0.8 + r() * 0.5, color: mixHex(P.crowns[Math.floor(r() * 2)], haze, hz), delay }); return; }
    const w = 1.3 + r() * 1.1, d = 1.3 + r() * 1.1, turn = Math.floor(r() * 4) * Math.PI / 2;
    const down = 1 + 2.4 * Math.exp(-Math.hypot(s.x, s.z) / 14);
    const h = (0.8 + r() * 1.6) * down;
    const isGlass = h > 3.2 && r() < 0.5;
    const b = { x: s.x, z: s.z, w, d, h, turn, delay, color: mixHex(isGlass ? P.glass : P.walls[Math.floor(r() * P.walls.length)], haze, hz) };
    (isGlass ? glass : walls).push(b);
    roofs.push({ b, color: mixHex(isGlass ? P.glassRoof : P.roof, haze, hz), delay });
    if (s.inside && !isGlass && h < 3.2) for (let y = 0.45; y < h - 0.3; y += 0.6) bands.push({ b, y, color: mixHex(P.band, haze, hz), delay });
  });
  // Trees in the parks and on the islands.
  const [common, garden] = SC.parks;
  [[common, 16], [garden, 7]].forEach(([p, n]) => {
    for (let i = 0; i < n * 4 && n > 0; i++) {
      const x = p.x + (r() - 0.5) * (p.w - 0.8), z = p.z + (r() - 0.5) * (p.d - 0.8);
      if (nearTrack(x, z, 1.4) || isWater(x, z) || placed.some(([a, b, rr]) => (a - x) ** 2 + (b - z) ** 2 < (rr + 0.6) ** 2)) continue;
      if (p === garden && ((x - p.x + 0.3) / 2.3) ** 2 + ((z - p.z) / 1.1) ** 2 < 1) continue;
      if (p === common && (x - p.x - 2) ** 2 + (z - p.z - 1.6) ** 2 < 1.6) continue;
      trees.push({ x, z, s: 0.6 + r() * 0.35, color: P.crowns[Math.floor(r() * 2)], delay: Math.hypot(x - UC.x, z - UC.z) * 0.011 }); n--;
    }
  });
  SC.islands.forEach(([cx, cz, rad]) => {
    for (let i = 0; i < 5; i++) { const a = r() * 6.28, d = r() * rad * 0.6, x = cx + Math.cos(a) * d, z = cz + Math.sin(a) * d;
      trees.push({ x, z, s: 0.7 + r() * 0.4, color: mixHex(P.crowns[i % 2], haze, hazeAt(x, z)), delay: 1 }); }
  });

  const rbox = (() => {
    const a = 0.5, rr = 0.14, s = new THREE.Shape();
    s.moveTo(-a + rr, -a); s.lineTo(a - rr, -a); s.quadraticCurveTo(a, -a, a, -a + rr); s.lineTo(a, a - rr); s.quadraticCurveTo(a, a, a - rr, a);
    s.lineTo(-a + rr, a); s.quadraticCurveTo(-a, a, -a, a - rr); s.lineTo(-a, -a + rr); s.quadraticCurveTo(-a, -a, -a + rr, -a);
    const g = new THREE.ExtrudeGeometry(s, { depth: 0.92, bevelEnabled: true, bevelThickness: 0.04, bevelSize: 0.03, bevelSegments: 2, curveSegments: 3 });
    g.rotateX(-Math.PI / 2); g.translate(0, 0.04, 0);
    return g;
  })();
  const placeWall = (b, f) => { V3.set(b.x, 0, b.z); Q.setFromAxisAngle(UP, b.turn); S3.set(b.w, b.h * f, b.d); };
  instanced(rbox, mat("#ffffff", { roughness: 0.8 }), walls, placeWall);
  instanced(rbox.clone(), mat("#ffffff", { roughness: 0.25, metalness: 0.25 }), glass, placeWall);
  const unit = new THREE.BoxGeometry(1, 1, 1);
  instanced(unit, mat("#ffffff", { roughness: 0.85 }), roofs, (it, f) => { const b = it.b; V3.set(b.x, b.h * f + 0.04, b.z); Q.setFromAxisAngle(UP, b.turn); S3.set(b.w - 0.2, 0.08, b.d - 0.2); });
  instanced(unit.clone(), mat("#ffffff", { roughness: 0.6, emissive: col(P.band), emissiveIntensity: P.lit * 0.55 }), bands,
    (it, f) => { const b = it.b; V3.set(b.x, it.y * f, b.z); Q.setFromAxisAngle(UP, b.turn); S3.set(b.w + 0.03, 0.13 * f, b.d + 0.03); });
  const trunk = new THREE.CylinderGeometry(0.1, 0.13, 0.7, 5); trunk.translate(0, 0.35, 0);
  instanced(trunk, mat("#ffffff"), trees.map((t) => Object.assign({}, t, { color: P.trunk })), (t, f) => { V3.set(t.x, 0, t.z); Q.identity(); S3.set(t.s * f, t.s * f, t.s * f); });
  const smooth = fx.tier === "high";   // rounder, softer crowns on the high tier
  const crown = new THREE.IcosahedronGeometry(0.72, smooth ? 1 : 0); crown.translate(0, 1.2, 0);
  instanced(crown, mat("#ffffff", { flatShading: !smooth, roughness: 0.9 }), trees, (t, f) => { V3.set(t.x, 0, t.z); Q.identity(); S3.set(t.s * f, t.s * 1.1 * f, t.s * f); });

  addLife(P, r);
  fx.afterWorld(P, { walls, bands });
}

// Small moving things: sailboats on the Charles, swan boats, a plane off Logan every 20 s, gulls.
function addLife(P, r) {
  const sailGeo = new THREE.ExtrudeGeometry(new THREE.Shape([new THREE.Vector2(0, 0), new THREE.Vector2(0.7, 0), new THREE.Vector2(0, 1.1)]), { depth: 0.03, bevelEnabled: false });
  for (let i = 0; i < 4; i++) {
    const b = new THREE.Group();
    const hull = part(b, new THREE.CapsuleGeometry(0.13, 0.62, 4, 10), "#fbfaf6", 0, 0.1, 0);   // rounded hull
    hull.rotation.z = Math.PI / 2; hull.scale.set(0.7, 1, 1.15);
    cyl(b, 0.012, 0.012, 1.15, "#8a7a68", -0.12, 0.16, 0, 6);
    part(b, sailGeo, i % 2 ? "#fbfaf6" : "#f3d9c4", -0.2, 0.2, 0.0);
    world.add(b);
    const cx = -62 + i * 22 + r() * 8, amp = 5 + r() * 4, sp = 0.05 + r() * 0.05, ph = r() * 6, off = (r() - 0.5) * 0.8;
    life.push((t) => { const x = cx + amp * Math.sin(t * sp + ph); b.position.set(x, 0.02 + 0.03 * Math.sin(t * 2 + i), riverZ(x) + off); b.rotation.y = Math.cos(t * sp + ph) > 0 ? 0 : Math.PI; });
  }
  const gd = SC.parks[1];
  for (let i = 0; i < 2; i++) {
    const s = new THREE.Group(); box(s, 0.4, 0.14, 0.22, "#ffffff", 0, 0, 0); box(s, 0.1, 0.2, 0.1, "#ffffff", 0.15, 0.14, 0); world.add(s);
    life.push((t) => { const a = t * 0.25 + i * Math.PI; s.position.set(gd.x - 0.3 + Math.cos(a) * 1.4, 0.02, gd.z + Math.sin(a) * 0.55); s.rotation.y = -a; });
  }
  const rw = SC.runways[0], plane = new THREE.Group();
  part(plane, new THREE.CylinderGeometry(0.16, 0.16, 2, 10), "#f7f8fa", 0, 0, 0).rotation.z = Math.PI / 2;
  box(plane, 0.5, 0.05, 2.2, "#f7f8fa", 0.1, -0.03, 0);
  box(plane, 0.3, 0.5, 0.05, "#d8261c", -0.85, 0.05, 0);
  world.add(plane);
  const dir = new THREE.Vector3(Math.cos(rw.a), 0, Math.sin(rw.a)), start = new THREE.Vector3(rw.x, 0.25, rw.z).addScaledVector(dir, -rw.len / 2 + 1.5);
  plane.rotation.y = -rw.a;
  life.push((t) => {
    const c = t % 20;
    const d = 0.9 * c * c;
    plane.visible = c < 7.5;
    plane.position.copy(start).addScaledVector(dir, d);
    plane.position.y = 0.25 + Math.max(0, d - rw.len * 0.55) * 0.22;
    plane.rotation.z = d > rw.len * 0.55 ? 0.12 : 0;
  });
  for (let i = 0; i < 4; i++) {
    const gl = new THREE.Group();
    const wl = box(gl, 0.45, 0.03, 0.12, "#fbfbfb", -0.2, 0, 0), wr = box(gl, 0.45, 0.03, 0.12, "#fbfbfb", 0.2, 0, 0);
    world.add(gl);
    life.push((t) => { const a = t * 0.35 + i * 1.6, rr = 6 + i * 1.5; gl.position.set(60 + Math.cos(a) * rr, 7 + i * 0.6, -6 + Math.sin(a) * rr); gl.rotation.y = -a; const fl = 0.4 * Math.sin(t * 8 + i); wl.rotation.z = fl; wr.rotation.z = -fl; });
  }
}

// ---------- the network (rebuilt on every change) ----------
const SEV_VAR = { good: "--good", warn: "--warn", crit: "--crit", unknown: "--unknown" };
function flatTube(curve, radius, color, yScale, y, o) {
  const len = curve.getLength();
  const m = new THREE.Mesh(new THREE.TubeGeometry(curve, Math.max(24, Math.round(len * 3)), radius, 10, false), mat(color, o));
  m.scale.y = yScale; m.position.y = y;
  m.receiveShadow = true;
  return m;
}
function buildNet() {
  const keep = {};
  trains.forEach((t) => { keep[t.key] = t.t; });
  empty(net);
  labels.forEach((l) => l.el.remove());
  labels = []; trains = []; pulses = []; pickables = [];
  const P = PAL[H.state.theme] || PAL.day;
  const night = H.state.theme === "midnight";

  H.S.links.forEach((l) => {
    const lineId = H.lineOf[l.id], c = H.cssColor(lineId);
    const curve = curveFor(l), len = curve.getLength();
    const w = (l.capacity_mbps || 0) >= 1000 ? 1.0 : 0.9;
    const sel = H.state.selected === l.id;
    const g = new THREE.Group();
    g.userData.id = l.id;
    if (l.planned) {
      const dash = mat(c, { transparent: true, opacity: 0.7 });
      for (let d = 0.6; d < len - 0.6; d += 1.6) {
        const p = curve.getPointAt(d / len), t = curve.getTangentAt(d / len);
        const m = new THREE.Mesh(new THREE.BoxGeometry(0.35, 0.06, 0.8), dash);
        m.position.set(p.x, 0.04, p.z); m.rotation.y = Math.atan2(t.x, t.z);
        g.add(m);
      }
    } else {
      const casing = flatTube(curve, w / 2 + 0.17, night ? "#dfe5f2" : "#ffffff", 0.5, 0.12, { roughness: 0.6 });
      casing.castShadow = true;
      g.add(casing, flatTube(curve, w / 2 * 0.86, c, 0.32, 0.38, { roughness: 0.45, emissive: col(c), emissiveIntensity: sel ? 0.7 : night ? 0.4 : 0.04 }));
    }
    net.add(g);
    pickables.push(g);

    const lstate = H.worst(H.alertsFor(l.id));
    if (lstate === "crit" || lstate === "warn") {
      const sc = H.cssVar(SEV_VAR[lstate]);
      const glow = flatTube(curve, w / 2 + 0.6, sc, 0.4, 0.15, { transparent: true, opacity: 0.45, emissive: col(sc), emissiveIntensity: 0.8, depthWrite: false });
      net.add(glow); pulses.push(glow);
    }
    if (lstate === "crit" || H.nodeState(H.nodes[l.a]) === "crit" || H.nodeState(H.nodes[l.b]) === "crit") return;
    const { ab, ba } = H.measured(l);
    [[ab, 1], [ba, -1]].forEach(([rate, dir]) => {
      if (!(rate > 0)) return;
      const count = Math.max(1, Math.min(5, Math.round(1 + Math.log10(1 + rate * 20))));
      const speed = (22 + 16 * Math.log10(1 + rate * 10)) / 10;
      for (let i = 0; i < count; i++) {
        const car = new THREE.Group();
        const body = new THREE.Mesh(new THREE.BoxGeometry(1.8, 0.5, 0.75), mat(night ? "#e9edf6" : "#ffffff", { roughness: 0.35, emissive: col(night ? "#ffffff" : "#000000"), emissiveIntensity: night ? 0.2 : 0 }));
        body.castShadow = true;
        const stripe = new THREE.Mesh(new THREE.BoxGeometry(1.84, 0.14, 0.8), mat(c, { emissive: col(c), emissiveIntensity: 0.3 }));
        stripe.position.y = -0.05;
        car.add(body, stripe);
        net.add(car);
        const key = `${l.id}:${dir}:${i}`;
        trains.push({ key, obj: car, curve, len, dir, speed, t: key in keep ? keep[key] : (i / count) * len + (dir < 0 ? len / (2 * count) : 0) });
      }
    });
  });

  H.S.nodes.forEach((n) => {
    const p = W(H.P(n.id)), st = H.nodeState(n), hub = H.isHub(n), sel = H.state.selected === n.id;
    const served = H.S.links.filter((l) => l.a === n.id || l.b === n.id);
    const unknown = st === "unknown";
    const r = hub ? (n.id === "central" ? 2.1 : 1.7) : 0.95;
    const rimC = st === "crit" ? H.cssVar("--crit") : unknown ? H.cssVar("--unknown") : hub ? P.hubRim : H.cssColor(H.lineOf[served[0].id]);
    const g = new THREE.Group();
    g.position.set(p.x, 0, p.z);
    g.userData.id = n.id;
    const base = new THREE.Mesh(new THREE.CylinderGeometry(r, r, 0.8, 40), mat(rimC, { roughness: 0.5 }));
    base.position.y = 0.4; base.castShadow = true;
    const cap = new THREE.Mesh(new THREE.CylinderGeometry(r * (hub ? 0.62 : 0.55), r * (hub ? 0.62 : 0.55), 0.84, 40),
      mat(st === "crit" ? H.cssVar("--crit") : P.station, { roughness: 0.4, transparent: unknown, opacity: unknown ? 0.75 : 1 }));
    cap.position.y = 0.42;
    g.add(base, cap);
    if (st === "crit") {
      const xm = mat("#ffffff", { emissive: col("#ffffff"), emissiveIntensity: 0.5 });
      [Math.PI / 4, -Math.PI / 4].forEach((a) => { const bar = new THREE.Mesh(new THREE.BoxGeometry(r * 0.85, 0.1, 0.2), xm); bar.position.y = 0.88; bar.rotation.y = a; g.add(bar); });
    }
    if (st === "warn" || st === "crit") {
      const sc = H.cssVar(SEV_VAR[st]);
      const halo = new THREE.Mesh(new THREE.RingGeometry(r + 0.35, r + 1.3, 40), mat(sc, { transparent: true, opacity: 0.5, emissive: col(sc), emissiveIntensity: 0.8, depthWrite: false, side: THREE.DoubleSide }));
      halo.rotation.x = -Math.PI / 2; halo.position.y = 0.06;
      g.add(halo); pulses.push(halo);
    }
    if (sel) {
      const ring = new THREE.Mesh(new THREE.TorusGeometry(r + 0.6, 0.18, 10, 48), mat(H.cssVar("--info"), { emissive: col(H.cssVar("--info")), emissiveIntensity: 0.8 }));
      ring.rotation.x = Math.PI / 2; ring.position.y = 0.3;
      g.add(ring);
    }
    net.add(g);
    pickables.push(g);

    const el = H.h("div", { class: "lbl3d" + (hub ? " hub" : "") }, [n.label]);
    if (n.sublabel && hub) el.append(H.h("small", { text: n.sublabel }));
    const a = H.alertsFor(n.id).filter((x) => x.severity !== "info")[0];
    if (a) el.append(H.h("div", {}, [H.h("span", { class: "chip3d " + a.severity + (H.quiet(a) ? " acked" : "") }, [H.h("i", { text: a.severity === "crit" ? "×" : "!" }), H.theme(a.kind).t])]));
    labelLayer.append(el);
    // Directions of the track leaving this station, used to put the label on the emptiest side.
    const dirs = served.map((l) => { const c = curveFor(l), len = c.getLength(), u = l.a === n.id ? Math.min(1, 2.5 / len) : Math.max(0, 1 - 2.5 / len); return c.getPointAt(u); });
    labels.push({ el, pos: new THREE.Vector3(p.x, 0.8, p.z), dirs, hub, r });
  });
  labels.sort((a, b) => (b.hub ? 1 : 0) - (a.hub ? 1 : 0));
  placeLabels();
}

// ---------- labels ----------
const pv = new THREE.Vector3();
const SIDES = [["right", 0], ["above", -Math.PI / 2], ["left", Math.PI], ["below", Math.PI / 2]];
function toScreen(v3, w, h) { pv.copy(v3).project(camera); return [((pv.x + 1) / 2) * w, ((1 - pv.y) / 2) * h]; }
function placeLabels() {
  const w = host.clientWidth, h = host.clientHeight;
  const ppu = (h / (camera.top - camera.bottom)) * camera.zoom;
  const taken = [];
  const overlaps = (r) => taken.some((t) => r.x0 < t.x1 && r.x1 > t.x0 && r.y0 < t.y1 && r.y1 > t.y0);
  labels.forEach((l) => {
    const [sx, sy] = toScreen(l.pos, w, h);
    if (!l.hub && ppu < 3.2) { l.el.style.display = "none"; return; }
    l.el.style.display = "";
    const angs = l.dirs.map((d) => { const [dx, dy] = toScreen(d, w, h); return Math.atan2(dy - sy, dx - sx); });
    const ranked = SIDES.map((sd, i) => {
      let m = Math.PI;
      angs.forEach((a) => { let d = Math.abs(a - sd[1]) % (2 * Math.PI); if (d > Math.PI) d = 2 * Math.PI - d; m = Math.min(m, d); });
      return { side: sd[0], score: m - i * 0.01 };
    }).sort((a, b) => b.score - a.score);
    const off = l.r * ppu + 6;
    const lw = l.el.offsetWidth || 60, lh = l.el.offsetHeight || 16;
    const rect = (sd) => sd === "right" ? { x0: sx + off, x1: sx + off + lw, y0: sy - lh / 2, y1: sy + lh / 2 } :
      sd === "left" ? { x0: sx - off - lw, x1: sx - off, y0: sy - lh / 2, y1: sy + lh / 2 } :
      sd === "above" ? { x0: sx - lw / 2, x1: sx + lw / 2, y0: sy - off - lh, y1: sy - off } : { x0: sx - lw / 2, x1: sx + lw / 2, y0: sy + off, y1: sy + off + lh };
    const pick = ranked.find((c) => c.score > 0.5 && !overlaps(rect(c.side))) || ranked[0];
    const side = pick.side;
    taken.push(rect(side));
    l.el.dataset.side = side;
    l.el.style.transform =
      side === "right" ? `translate(${sx + off}px, ${sy}px) translate(0, -50%)` :
      side === "left" ? `translate(${sx - off}px, ${sy}px) translate(-100%, -50%)` :
      side === "above" ? `translate(${sx}px, ${sy - off}px) translate(-50%, -100%)` :
      `translate(${sx}px, ${sy + off}px) translate(-50%, 0)`;
  });
}

// ---------- loop ----------
const tmp = new THREE.Vector3();
function loop() {
  if (!active) return;
  requestAnimationFrame(loop);
  const now = performance.now();
  const dt = Math.min(0.1, clock.getDelta()), t = clock.elapsedTime;
  stepAnim(now);
  controls.update();
  if (riseT0 !== -Infinity) {
    if (now - riseT0 > 2600) riseT0 = -Infinity;
    risers.forEach((r) => updateRiser(r, now));
    world.children.forEach((g) => { if (g.userData.rise) g.scale.y = Math.max(0.001, riseF(g.userData.delay, now)); });
  }
  if (H.state.motion) {
    trains.forEach((tr) => { tr.t = (tr.t + tr.dir * tr.speed * dt + tr.len) % tr.len; });
    const k = 0.55 + 0.45 * Math.sin(t * 4);
    pulses.forEach((m) => { m.material.opacity = 0.2 + 0.45 * k; });
    life.forEach((f) => f(t));
  }
  fx.frame(t, dt, H.state.motion, now);
  trains.forEach((tr) => {
    const u = tr.t / tr.len, p = tr.curve.getPointAt(u);
    tr.obj.position.set(p.x, 0.62, p.z);
    tmp.copy(tr.curve.getTangentAt(u)).add(tr.obj.position); tmp.y = 0.62;
    tr.obj.lookAt(tmp);
    tr.obj.rotateY(Math.PI / 2);
  });
  renderer.render(scene, camera);
  placeLabels();
}

function build() {
  if (!renderer) return;
  buildWorld();
  buildNet();
  fx.sync();
  const stop = (H.S.sports || {}).busstop;
  const hasStop = !!stop && !stop.error && Array.isArray(stop.last);   // switched off in Settings, or no data: the bus stop simply isn't there
  world.children.forEach((g) => {
    if (g.userData.id === "sports-busstop") g.visible = hasStop;
    if (g.userData.id && g.visible) pickables.push(g);
  });
  paintDriveIn();
}

H.views["3d"] = {
  show() {
    const first = !renderer;
    if (first && !failed) init();
    if (failed) return;
    resize();
    riseT0 = -Infinity;
    build();
    requestAnimationFrame(() => { resize(); fold(); });
    if (!active) { active = true; clock.getDelta(); loop(); }
  },
  hide() { active = false; },
  reset() { if (renderer) reset(); },
  rotate
};
H.listeners.push(() => { if (active) build(); });
if (H.state.view === "3d" && !active) H.views["3d"].show();
