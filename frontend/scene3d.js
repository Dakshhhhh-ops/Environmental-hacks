// 3D risk view: one pillar per low point. Pillar height is forecast peak as a share of
// that spot's threshold; the white ring marks the threshold, so a pillar through its ring is high risk.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';

const COLORS = { green: 0x54d6a0, amber: 0xf2b94b, red: 0xff6570, idle: 0x5c6e7c };
const CENTER = { lat: 28.625, lon: 77.19 };
const KM_PER_DEG = 111;
const BASE_H = 0.6, RING_H = 9;          // ring sits where peak equals threshold
const YAMUNA = [[28.76, 77.225], [28.71, 77.232], [28.67, 77.243], [28.63, 77.256], [28.59, 77.272], [28.55, 77.305], [28.5, 77.33]];

const project = (lat, lon) => new THREE.Vector3((lon - CENTER.lon) * KM_PER_DEG * Math.cos((CENTER.lat * Math.PI) / 180), 0, -(lat - CENTER.lat) * KM_PER_DEG);
const heightFor = (a) => (a?.data_available ? BASE_H + Math.min(a.risk_ratio, 1.45) * (RING_H - BASE_H) : BASE_H);

export function createScene(container) {
  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x0a0c0d, 0.011);
  const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 400);
  camera.position.set(22, 25, 38);

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  container.appendChild(renderer.domElement);
  const labels = new CSS2DRenderer();
  labels.domElement.className = 'scene-labels';
  container.appendChild(labels.domElement);

  const controls = new OrbitControls(camera, labels.domElement);
  controls.enableDamping = true; controls.autoRotate = true; controls.autoRotateSpeed = 0.55;
  controls.maxPolarAngle = Math.PI / 2.15; controls.minDistance = 12; controls.maxDistance = 110;
  controls.target.set(2, 2, 2);
  controls.addEventListener('start', () => { controls.autoRotate = false; });

  scene.add(new THREE.AmbientLight(0xffffff, 0.55));
  const sun = new THREE.DirectionalLight(0xbfe9ff, 1.3); sun.position.set(20, 40, 10); scene.add(sun);

  const ground = new THREE.Mesh(new THREE.CircleGeometry(60, 64), new THREE.MeshStandardMaterial({ color: 0x0f1315, roughness: 0.35, metalness: 0.6 }));
  ground.rotation.x = -Math.PI / 2; scene.add(ground);
  const grid = new THREE.GridHelper(120, 60, 0x2b3338, 0x1a2023); grid.position.y = 0.01; scene.add(grid);

  const river = new THREE.Mesh(
    new THREE.TubeGeometry(new THREE.CatmullRomCurve3(YAMUNA.map(([la, lo]) => project(la, lo).setY(0.08))), 60, 0.35, 6),
    new THREE.MeshBasicMaterial({ color: 0x3b83b8, transparent: true, opacity: 0.55 }));
  scene.add(river);
  const riverLabel = new CSS2DObject(Object.assign(document.createElement('div'), { className: 'scene-tag muted', textContent: 'Yamuna' }));
  riverLabel.position.copy(project(28.6, 77.268).setY(0.6)); scene.add(riverLabel);

  // rain
  const DROPS = 2200, rainPos = new Float32Array(DROPS * 3);
  for (let i = 0; i < DROPS; i++) { rainPos[i * 3] = (Math.random() - 0.5) * 110; rainPos[i * 3 + 1] = Math.random() * 45; rainPos[i * 3 + 2] = (Math.random() - 0.5) * 110; }
  const rainGeo = new THREE.BufferGeometry(); rainGeo.setAttribute('position', new THREE.BufferAttribute(rainPos, 3));
  const rain = new THREE.Points(rainGeo, new THREE.PointsMaterial({ color: 0x9fd8ff, size: 0.16, transparent: true, opacity: 0.5 }));
  scene.add(rain);
  let rainSpeed = 0.18;

  const pillars = new Map();
  const group = new THREE.Group(); scene.add(group);

  function setData(spots, assessments) {
    const lookup = Object.fromEntries((assessments || []).map((a) => [a.spot_id, a]));
    const peak = Math.max(0, ...(assessments || []).filter((a) => a.data_available).map((a) => a.peak_precipitation_mm_hr));
    rainSpeed = 0.14 + Math.min(peak, 25) * 0.022;
    rain.material.opacity = 0.25 + Math.min(peak, 25) * 0.022;
    // Label only the worst few so names stay readable.
    const labelled = new Set((assessments || []).filter((a) => a.data_available && a.risk_level !== 'green').sort((x, y) => y.risk_ratio - x.risk_ratio).slice(0, 6).map((a) => a.spot_id));
    spots.forEach((s) => {
      let p = pillars.get(s.id);
      if (!p) {
        const mat = new THREE.MeshStandardMaterial({ color: COLORS.idle, emissive: COLORS.idle, emissiveIntensity: 0.35, roughness: 0.3, transparent: true, opacity: 0.92 });
        const mesh = new THREE.Mesh(new THREE.CylinderGeometry(0.7, 0.7, 1, 24), mat);
        const pos = project(s.lat, s.lon);
        mesh.position.copy(pos);
        const ring = new THREE.Mesh(new THREE.TorusGeometry(1.15, 0.05, 8, 40), new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.45 }));
        ring.rotation.x = Math.PI / 2; ring.position.copy(pos).setY(RING_H);
        const halo = new THREE.Mesh(new THREE.RingGeometry(0.9, 1.8, 40), new THREE.MeshBasicMaterial({ color: COLORS.idle, transparent: true, opacity: 0.35, side: THREE.DoubleSide }));
        halo.rotation.x = -Math.PI / 2; halo.position.copy(pos).setY(0.05);
        const el = document.createElement('div'); el.className = 'scene-tag';
        const label = new CSS2DObject(el); label.position.copy(pos);
        group.add(mesh, ring, halo, label);
        p = { mesh, mat, ring, halo, label, el, h: BASE_H, target: BASE_H, level: 'idle', phase: Math.random() * 6 };
        pillars.set(s.id, p);
      }
      const a = lookup[s.id];
      p.target = heightFor(a);
      p.level = a?.data_available ? a.risk_level : 'idle';
      const color = COLORS[p.level];
      p.mat.color.setHex(color); p.mat.emissive.setHex(color); p.halo.material.color.setHex(color);
      const flagged = labelled.has(s.id);
      p.el.textContent = flagged ? `${s.name} · ${Math.round(a.risk_ratio * 100)}%` : '';
      p.el.className = `scene-tag ${p.level}`;
      p.label.visible = flagged;
    });
  }

  function resize() {
    const w = container.clientWidth || 1, h = container.clientHeight || 1;
    camera.aspect = w / h; camera.updateProjectionMatrix();
    renderer.setSize(w, h); labels.setSize(w, h);
  }
  new ResizeObserver(resize).observe(container); resize();

  let running = false;
  const clock = new THREE.Clock();
  function frame() {
    if (!running) return;
    const t = clock.getElapsedTime();
    pillars.forEach((p) => {
      p.h += (p.target - p.h) * 0.07;
      p.mesh.scale.y = p.h; p.mesh.position.y = p.h / 2;
      p.label.position.y = p.h + 1.4;
      const pulse = p.level === 'red' ? 0.75 + 0.45 * Math.sin(t * 4 + p.phase) : p.level === 'amber' ? 0.55 : 0.3;
      p.mat.emissiveIntensity = pulse;
      p.halo.scale.setScalar(p.level === 'red' ? 1 + 0.35 * (0.5 + 0.5 * Math.sin(t * 3 + p.phase)) : 1);
    });
    const pos = rainGeo.attributes.position;
    for (let i = 0; i < DROPS; i++) { let y = pos.getY(i) - rainSpeed; if (y < 0) y = 45; pos.setY(i, y); }
    pos.needsUpdate = true;
    controls.update();
    renderer.render(scene, camera); labels.render(scene, camera);
    requestAnimationFrame(frame);
  }
  return {
    setData,
    start() { if (!running) { running = true; resize(); frame(); } },
    stop() { running = false; },
  };
}
