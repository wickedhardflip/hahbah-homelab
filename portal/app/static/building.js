// ---------- /building: an architect's presentation model (Three.js, same vendored copy and import map as the 3D map) ----------
// Chipboard plates, foam core walls, a basswood roof and clear acrylic windows on a site plan sheet. The roof lifts, the floors
// separate, and our unit's floorplan sits on its plate with plugs and Eero nodes as small pieces that light up with /api/plugs.
// Model units: x east, y south (the footprint's), up is height. Render on demand: the loop only draws while something moves.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const P = window.Plugs;
const $ = (id) => document.getElementById(id);
const stage = $("bdStage"), canvas = $("bdCanvas"), labelBox = $("bdLabels");
const PLATE = 0.9, WALL = 2.6, ROOF_H = 5.2, LIFT = 7;
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

let cfg, layout = { items: [] }, hasImage = false;
let ex = reduced ? 1 : 0, exT = 1, roofUp = reduced ? 1 : 0, roofT = 1;
const lift = [], liftT = [];
let dirty = true;

// ---------- procedural textures (no image files, nothing to host) ----------
function tex(w, h, paint, rep) {
  const c = document.createElement("canvas"); c.width = w; c.height = h;
  paint(c.getContext("2d"), w, h);
  const t = new THREE.CanvasTexture(c); t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(rep, rep); t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4; return t;
}
const rnd = (() => { let s = 7; return () => (s = (s * 1664525 + 1013904223) >>> 0) / 4294967296; })();
const chipTex = tex(128, 128, (g, w, h) => {
  g.fillStyle = "#b49a74"; g.fillRect(0, 0, w, h);
  for (let i = 0; i < 900; i++) { g.fillStyle = ["#8a7151", "#d2bd98", "#9c825e"][i % 3]; g.globalAlpha = 0.55; g.fillRect(rnd() * w, rnd() * h, 1 + rnd() * 1.6, 1 + rnd() * 1.2); }
}, 0.06);
const bassTex = tex(128, 128, (g, w, h) => {
  g.fillStyle = "#e6d3a9"; g.fillRect(0, 0, w, h);
  for (let y = 0; y < h; y += 9) { g.fillStyle = "#d4bd8c"; g.globalAlpha = 0.6; g.fillRect(0, y, w, 1.4); g.globalAlpha = 0.25; g.fillRect(0, y + 4 + rnd() * 3, w, 1); }
}, 0.1);
const MAT = {
  chip: new THREE.MeshStandardMaterial({ map: chipTex, roughness: 1 }),
  chipEdge: new THREE.MeshStandardMaterial({ color: "#8f7552", roughness: 1 }),
  foam: new THREE.MeshStandardMaterial({ color: "#fbfaf6", roughness: 1 }),
  bass: new THREE.MeshStandardMaterial({ map: bassTex, roughness: 0.95, side: THREE.DoubleSide }),
  glass: new THREE.MeshStandardMaterial({ color: "#78aaa5", transparent: true, opacity: 0.45, roughness: 0.2 }),
  sheet: new THREE.MeshStandardMaterial({ color: "#d8d2c2", roughness: 1 }),
  felt: new THREE.MeshStandardMaterial({ color: "#7f9a6c", roughness: 1 }),
  unit: new THREE.MeshStandardMaterial({ color: "#4e6e58", transparent: true, opacity: 0.55, roughness: 1 }),
};

// ---------- renderer, camera, light ----------
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(32, 1, 1, 600);
const sun = new THREE.DirectionalLight("#fff4e0", 2.2);
sun.castShadow = true; sun.shadow.mapSize.set(1024, 1024); sun.shadow.radius = 6; sun.shadow.bias = -0.0005;
scene.add(sun, new THREE.HemisphereLight("#ffffff", "#c9c0aa", 2.4));
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true; controls.maxPolarAngle = Math.PI * 0.49; controls.enablePan = false;
controls.addEventListener("change", () => { dirty = true; });

// ---------- geometry helpers ----------
// A plate from the footprint ring (x, y), thickness h, sitting on y = 0 of its parent.
function slab(ring, h, mat) {
  const sh = new THREE.Shape(ring.map(([x, y]) => new THREE.Vector2(x, y)));
  const g = new THREE.ExtrudeGeometry(sh, { depth: h, bevelEnabled: false });
  g.rotateX(Math.PI / 2); g.translate(0, h, 0);   // model y (south) becomes +z, extrusion goes up from 0
  const m = new THREE.Mesh(g, [mat, mat]);
  m.castShadow = m.receiveShadow = true; return m;
}
const box = (x0, y0, x1, y1, z0, z1, mat) => {
  const m = new THREE.Mesh(new THREE.BoxGeometry(x1 - x0, z1 - z0, y1 - y0), mat);
  m.position.set((x0 + x1) / 2, (z0 + z1) / 2, (y0 + y1) / 2); m.castShadow = m.receiveShadow = true; return m;
};

// ---------- the model ----------
const levels = [], pieces = [], hit = [];
let roof, sheet, unitMesh, unitPlane, B, unitRect;

function build() {
  const fp = cfg.footprint;
  const xs = fp.map((p) => p[0]), ys = fp.map((p) => p[1]);
  B = { x0: Math.min(...xs), x1: Math.max(...xs), y0: Math.min(...ys), y1: Math.max(...ys) };
  const cx = (B.x0 + B.x1) / 2, cy = (B.y0 + B.y1) / 2, wid = B.x1 - B.x0, dep = B.y1 - B.y0;
  const root = new THREE.Group(); root.position.set(-cx, 0, -cy); scene.add(root);

  // site plan sheet with parking and felt ball trees
  const pad = Math.max(wid, dep) * 0.14;
  sheet = box(B.x0 - pad, B.y0 - pad, B.x1 + pad, B.y1 + pad * 2.4, -1.2, 0, MAT.sheet);
  root.add(sheet);
  const lot = new THREE.Mesh(new THREE.PlaneGeometry(wid + pad, pad * 1.4), new THREE.MeshStandardMaterial({ color: "#c4beae", roughness: 1 }));
  lot.rotation.x = -Math.PI / 2; lot.position.set(cx, 0.02, B.y1 + pad * 1.5); lot.receiveShadow = true; root.add(lot);
  for (let x = B.x0; x < B.x1; x += 4) root.add(box(x, B.y1 + pad * 1.15, x + 0.25, B.y1 + pad * 1.85, 0, 0.04, MAT.foam));
  [[B.x0 - pad * 0.6, B.y0 - pad * 0.5], [B.x0 - pad * 0.6, cy], [B.x1 + pad * 0.6, B.y0 - pad * 0.4], [B.x1 + pad * 0.6, cy], [cx, B.y0 - pad * 0.6], [B.x0 + wid * 0.25, B.y0 - pad * 0.6]].forEach(([x, y]) => {
    const t = new THREE.Mesh(new THREE.SphereGeometry(2.2, 20, 14), MAT.felt); t.position.set(x, 2.4, y); t.castShadow = true; root.add(t);
  });

  const [ux0, uy0, ux1, uy1] = cfg.unit; unitRect = { x0: ux0, y0: uy0, x1: ux1, y1: uy1 };
  for (let i = 0; i < cfg.levels; i++) {
    const g = new THREE.Group(); g.userData.level = i + 1;
    const plate = slab(fp, PLATE, MAT.chip); g.add(plate);
    // foam core back walls (cutaway: north and west), with acrylic windows along the north wall
    g.add(box(B.x0, B.y0, B.x1, B.y0 + 0.45, PLATE, PLATE + WALL, MAT.foam), box(B.x0, B.y0, B.x0 + 0.45, B.y1, PLATE, PLATE + WALL, MAT.foam));
    for (let wx = B.x0 + 3; wx < B.x1 - 3; wx += 6.5) g.add(box(wx, B.y0 + 0.46, wx + 2.6, B.y0 + 0.52, PLATE + 0.9, PLATE + 2, MAT.glass));
    hit.push(plate); plate.userData.level = i + 1;
    if (i + 1 === cfg.unit_level) buildUnit(g);
    root.add(g); levels.push(g); lift.push(0); liftT.push(0);
  }

  // gable roof: eave plate + two basswood slopes lifted as one piece
  roof = new THREE.Group();
  const ev = 0.8, rx0 = B.x0 - ev, rx1 = B.x1 + ev, ry0 = B.y0 - ev, ry1 = B.y1 + ev, ym = (ry0 + ry1) / 2;
  roof.add(box(rx0, ry0, rx1, ry1, 0, 0.5, MAT.bass));
  const slope = (ya, yb, h0, h1) => {
    const v = new Float32Array([rx0, h0, ya, rx1, h0, ya, rx1, h1, yb, rx0, h1, yb]);
    const gm = new THREE.BufferGeometry(); gm.setAttribute("position", new THREE.BufferAttribute(v, 3));
    gm.setAttribute("uv", new THREE.BufferAttribute(new Float32Array([0, 0, 40, 0, 40, 12, 0, 12]), 2));
    gm.setIndex([0, 1, 2, 0, 2, 3]); gm.computeVertexNormals();
    const m = new THREE.Mesh(gm, MAT.bass); m.castShadow = m.receiveShadow = true; return m;
  };
  roof.add(slope(ry0, ym, 0.5, 0.5 + ROOF_H), slope(ry1, ym, 0.5, 0.5 + ROOF_H));
  const gab = (x) => { const v = new Float32Array([x, 0.5, ry0, x, 0.5, ry1, x, 0.5 + ROOF_H, ym]); const gm = new THREE.BufferGeometry(); gm.setAttribute("position", new THREE.BufferAttribute(v, 3)); gm.computeVertexNormals(); const m = new THREE.Mesh(gm, MAT.bass); m.castShadow = true; return m; };
  roof.add(gab(rx0), gab(rx1));
  root.add(roof);

  // camera and light framing from the model size
  const R = Math.max(wid, dep, 30);
  camera.position.set(R * 1.15, R * 1.3, R * 1.5); controls.target.set(0, cfg.levels * 7, 0);
  sun.position.set(-R * 0.6, R * 1.3, R * 0.5);
  const sc = sun.shadow.camera; sc.left = sc.bottom = -R * 1.1; sc.right = sc.top = R * 1.1; sc.near = 1; sc.far = R * 4; sc.updateProjectionMatrix();
  controls.minDistance = R * 0.8; controls.maxDistance = R * 3.2; controls.update();
}

// Our unit: green tint on the plate, the floorplan drawing (from /media, never in git), and a piece per placed plug and Eero node.
function buildUnit(g) {
  const { x0, y0, x1, y1 } = unitRect, w = x1 - x0, d = y1 - y0, top = PLATE + 0.02;
  const plane = (mat, dy) => { const m = new THREE.Mesh(new THREE.PlaneGeometry(w, d), mat); m.rotation.x = -Math.PI / 2; m.position.set((x0 + x1) / 2, top + dy, (y0 + y1) / 2); m.receiveShadow = true; return m; };
  unitMesh = plane(MAT.unit, 0); unitMesh.userData.unit = true; g.add(unitMesh); hit.push(unitMesh);
  if (hasImage) {
    new THREE.TextureLoader().load("/media/floorplan.jpg", (t) => {
      t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 4;
      const img = t.image, k = Math.min(w / img.width, d / img.height);   // contain, keep the drawing's proportions
      unitPlane = new THREE.Mesh(new THREE.PlaneGeometry(img.width * k, img.height * k), new THREE.MeshBasicMaterial({ map: t, transparent: true, opacity: 0.88, depthWrite: false }));
      unitPlane.rotation.x = -Math.PI / 2; unitPlane.position.set((x0 + x1) / 2, top + 0.03, (y0 + y1) / 2); unitPlane.userData.unit = true;
      unitPlane.userData.fit = { w: img.width * k, d: img.height * k };
      g.add(unitPlane); hit.push(unitPlane); placePieces(g); dirty = true;
    });
  }
  placePieces(g);
}
// Layout items are fractions of the drawing; map them onto the drawn image (or the whole unit rect without one).
function placePieces(g) {
  pieces.splice(0).forEach((p) => g.remove(p.mesh));
  const { x0, y0, x1, y1 } = unitRect, fit = unitPlane && unitPlane.userData.fit;
  const w = fit ? fit.w : x1 - x0, d = fit ? fit.d : y1 - y0, cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  for (const it of layout.items) {
    if (it.kind !== "plug" && it.kind !== "eero") continue;
    const x = cx - w / 2 + it.x * w, y = cy - d / 2 + it.y * d;
    let mesh;
    if (it.kind === "plug") {
      mesh = new THREE.Mesh(new THREE.BoxGeometry(0.9, 0.9, 0.9), new THREE.MeshStandardMaterial({ color: "#d9d3c4", emissive: "#000", roughness: 0.8 }));
      mesh.position.set(x, PLATE + 0.5, y);
      mesh.add(new THREE.PointLight("#ffd36b", 0, 7, 2));
    } else {
      mesh = new THREE.Mesh(new THREE.CylinderGeometry(1.2, 1.2, 0.5, 24), new THREE.MeshStandardMaterial({ color: "#fbfaf6", roughness: 0.8 }));
      mesh.position.set(x, PLATE + 0.3, y);
    }
    mesh.castShadow = true; mesh.userData.unit = true; g.add(mesh); hit.push(mesh); pieces.push({ it, mesh });
  }
  paintPieces();
}
function paintPieces() {
  let on = 0;
  for (const { it, mesh } of pieces) {
    if (it.kind !== "plug") continue;
    const p = P.byId(it.id.slice(5)), lit = !!(p && p.online && p.on);
    if (lit) on++;
    mesh.material.color.set(lit ? "#ffd36b" : "#d9d3c4"); mesh.material.emissive.set(lit ? "#e0a020" : "#000"); mesh.material.emissiveIntensity = lit ? 0.9 : 0;
    mesh.children[0].intensity = lit ? 60 : 0;
  }
  const plugs = pieces.filter((p) => p.it.kind === "plug").length;
  $("bdSub").textContent = plugs ? `Our unit: ${on} of ${plugs} plugs on` : "";
  dirty = true; return on;
}

// ---------- layout of parts (explode, lift) ----------
function arrange() {
  const gap = 9 * ex, step = PLATE + WALL;
  levels.forEach((g, i) => { g.position.y = i * (step + gap) + lift[i] * LIFT; });
  roof.position.y = levels.length * step + (levels.length - 1) * gap + 9 * roofUp + gap * 0.4 + Math.max(0, ...lift) * LIFT;
}
function frame() {
  requestAnimationFrame(frame);
  if (document.hidden) return;
  const ease = (a, b) => (Math.abs(b - a) < 0.002 ? b : a + (b - a) * 0.12);
  const was = [ex, roofUp, ...lift];
  ex = ease(ex, exT); roofUp = ease(roofUp, roofT); lift.forEach((v, i) => { lift[i] = ease(v, liftT[i]); });
  if (was.some((v, i) => v !== [ex, roofUp, ...lift][i])) { arrange(); dirty = true; }
  if (controls.update()) dirty = true;
  if (!dirty) return;
  dirty = false; renderer.render(scene, camera); placeLabels();
}

// ---------- leader labels (HTML, positioned from the projected anchor) ----------
const labels = [];
function mkLabels() {
  const defs = [{ id: "roof", t: "Roof", s: "basswood, lifts off" }];
  for (let i = cfg.levels; i >= 1; i--) defs.push({ id: "l" + i, t: "Level " + i, s: i === cfg.unit_level ? "our unit" : "neighbors", lvl: i });
  for (const d of defs) {
    const el = document.createElement("div"); el.className = "bd-lbl";
    const dot = document.createElement("i"), rule = document.createElement("u"), txt = document.createElement("span"), b = document.createElement("b"), sm = document.createElement("small");
    b.textContent = d.t; sm.textContent = d.s; txt.append(b, sm); el.append(dot, rule, txt); labelBox.append(el);
    labels.push({ ...d, el, sm });
  }
}
const v3 = new THREE.Vector3();
function placeLabels() {
  const w = stage.clientWidth, h = stage.clientHeight;
  for (const L of labels) {
    const g = L.lvl ? levels[L.lvl - 1] : roof;
    v3.set((B.x1 - B.x0) / 2, g.position.y + (L.lvl ? PLATE : 0.5 + ROOF_H / 2), 0);   // east edge, middle of the depth, in scene space
    v3.project(camera);
    const x = (v3.x * 0.5 + 0.5) * w, y = (-v3.y * 0.5 + 0.5) * h;
    L.el.style.transform = `translate(${Math.round(x)}px, ${Math.round(y - 14)}px)`;
    L.el.style.display = x < 0 || x > w - 60 || y < 0 || y > h ? "none" : "flex";
  }
}

// ---------- input ----------
const ray = new THREE.Raycaster(), ndc = new THREE.Vector2();
let down = null;
canvas.addEventListener("pointerdown", (e) => { down = [e.clientX, e.clientY]; });
canvas.addEventListener("pointerup", (e) => {
  if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 5) { down = null; return; }   // a drag turns the model, only a tap picks
  down = null;
  const r = canvas.getBoundingClientRect();
  ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(ndc, camera);
  const h = ray.intersectObjects(hit.filter((o) => o.visible), false)[0];
  if (!h) return;
  if (h.object.userData.unit) { location.href = "/floorplan"; return; }   // our unit: its plugs and drawing open the floorplan page
  const lv = h.object.userData.level || (h.object.parent && h.object.parent.userData.level);
  if (lv) { liftT[lv - 1] = liftT[lv - 1] ? 0 : 1; dirty = true; }
});
$("bdExplode").addEventListener("input", (e) => { exT = ex = e.target.value / 100; arrange(); dirty = true; });
$("bdRoof").addEventListener("change", (e) => { roofT = e.target.checked ? 1 : 0; });
$("bdUnit").addEventListener("change", (e) => { const on = e.target.checked; unitMesh.visible = on; if (unitPlane) unitPlane.visible = on; pieces.forEach((p) => { p.mesh.visible = on; }); dirty = true; });
$("bdReplay").addEventListener("click", () => { ex = roofUp = 0; exT = roofT = 1; liftT.fill(0); $("bdExplode").value = 100; $("bdRoof").checked = true; });
function fit() {
  const w = stage.clientWidth, h = stage.clientHeight;
  renderer.setSize(w, h, false); camera.aspect = w / h; camera.fov = w < 640 ? 46 : 32; camera.updateProjectionMatrix(); dirty = true;
}
new ResizeObserver(fit).observe(stage);

// ---------- start ----------
async function getJSON(url) { const r = await fetch(url, { credentials: "same-origin", cache: "no-store" }); if (r.status === 401) { location.href = "/login"; return null; } return r.ok ? r.json() : null; }
(async () => {
  cfg = await getJSON("/api/building");
  if (!cfg) { $("bdLoad").textContent = "The building model could not be loaded."; return; }
  hasImage = !!cfg.has_image;
  const fl = await getJSON("/api/floorplan"); if (fl && fl.layout) layout = fl.layout;
  $("bdCap").textContent = cfg.caption + (cfg.placeholder ? " (placeholder footprint)" : "");
  build(); mkLabels(); fit(); arrange();
  P.on(() => paintPieces()); P.poll();
  $("bdLoad").hidden = true;
  const unitLabel = labels.find((l) => l.lvl === cfg.unit_level);
  P.on(() => { const on = pieces.filter((p) => p.it.kind === "plug" && p.mesh.material.emissiveIntensity > 0).length; unitLabel.sm.textContent = on ? `our unit, ${on} plug${on === 1 ? "" : "s"} on` : "our unit"; });
  frame();
})();
