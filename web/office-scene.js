import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const IDS = ['codex', 'claude', 'antigravity'];
const COLORS = { codex: 0xb6d6a5, claude: 0xe5b293, antigravity: 0x9bcbdc };
const NAMES = { codex: 'Codex', claude: 'Claude', antigravity: 'Antigravity' };
const TAU = Math.PI * 2;
const clamp = (number, low, high) => Math.max(low, Math.min(high, number));
const safeText = (value) => typeof value === 'string' ? value : '';
const hash = (value) => { const n = Math.sin(value * 127.1 + 3.1) * 43758.5453; return n - Math.floor(n); };

/** Three offices show only public, authoritative CLI telemetry. Idle is scripted. */
export class OfficeScene {
  constructor(container, { onSelect } = {}) {
    if (!(container instanceof HTMLElement)) throw new TypeError('OfficeScene requires a container.');
    this.container = container;
    this.onSelect = typeof onSelect === 'function' ? onSelect : () => {};
    this._geometries = new Set();
    this._materials = new Set();
    this._labels = new Map();
    this._rooms = new Map();
    this._selected = 'codex';
    this._connected = false;
    this._time = 0;
    this._disposed = false;
    this._reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    this.scene = new THREE.Scene();
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setClearColor(0, 0);
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.canvas = this.renderer.domElement;
    this.canvas.className = 'office-canvas';
    this.canvas.setAttribute('role', 'img');
    this.canvas.setAttribute('aria-label', '三間可旋轉、縮放與點選的模型辦公室');
    Object.assign(this.canvas.style, { display: 'block', width: '100%', height: '100%', touchAction: 'none' });
    if (getComputedStyle(container).position === 'static') container.style.position = 'relative';
    container.appendChild(this.canvas);
    this.labelLayer = document.createElement('div');
    this.labelLayer.className = 'office-label-layer';
    Object.assign(this.labelLayer.style, { position: 'absolute', inset: '0', pointerEvents: 'none', overflow: 'hidden' });
    container.appendChild(this.labelLayer);
    this.camera = new THREE.OrthographicCamera(-16, 16, 11, -11, 0.1, 160);
    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.enableDamping = !this._reducedMotion;
    this.controls.dampingFactor = 0.075;
    this.controls.minZoom = 0.7;
    this.controls.maxZoom = 3;
    this.controls.minPolarAngle = 0.24;
    this.controls.maxPolarAngle = Math.PI * 0.47;
    this.controls.rotateSpeed = 0.6;
    this.controls.zoomSpeed = 0.7;
    this.controls.panSpeed = 0.7;
    this.controls.screenSpacePanning = true;
    this._createAssets();
    this._createLighting();
    this._createPlatform();
    IDS.forEach((id, index) => this._createOffice(id, (index - 1) * 7.2));
    this.selectProvider('codex');
    this.resetCamera();
    this._resizeObserver = new ResizeObserver(() => this._resize());
    this._resizeObserver.observe(container);
    this._resize();
    this._raycaster = new THREE.Raycaster();
    this._pointer = new THREE.Vector2();
    this._pointers = new Set();
    this._onPointerDown = (event) => {
      this._pointers.add(event.pointerId);
      this._down = this._pointers.size === 1 ? { x: event.clientX, y: event.clientY, at: performance.now(), button: event.button } : null;
    };
    this._onPointerUp = (event) => {
      const single = this._pointers.size === 1;
      this._pointers.delete(event.pointerId);
      if (single) this._pick(event); else this._down = null;
    };
    this._onPointerCancel = (event) => { this._pointers.delete(event.pointerId); this._down = null; };
    this.canvas.addEventListener('pointerdown', this._onPointerDown);
    this.canvas.addEventListener('pointerup', this._onPointerUp);
    this.canvas.addEventListener('pointercancel', this._onPointerCancel);
    this._lastFrame = performance.now();
    this._frame = requestAnimationFrame(time => this._animate(time));
  }

  _geometry(geometry) { this._geometries.add(geometry); return geometry; }
  _material(parameters, basic = false) {
    const material = basic ? new THREE.MeshBasicMaterial(parameters) : new THREE.MeshStandardMaterial(parameters);
    this._materials.add(material);
    return material;
  }

  _createAssets() {
    this.assets = {
      box: this._geometry(new THREE.BoxGeometry(1, 1, 1)),
      cylinder: this._geometry(new THREE.CylinderGeometry(1, 1, 1, 12)),
      cone: this._geometry(new THREE.ConeGeometry(1, 1, 10)),
      sphere: this._geometry(new THREE.SphereGeometry(1, 10, 7)),
      leaf: this._geometry(new THREE.SphereGeometry(1, 6, 4)),
      star: this._geometry(new THREE.IcosahedronGeometry(0.1, 0)),
      torus: this._geometry(new THREE.TorusGeometry(1, 0.025, 5, 36)),
      wood: this._material({ color: 0xc49b6e, roughness: 0.86 }),
      woodDark: this._material({ color: 0x8a7059, roughness: 0.95 }),
      metal: this._material({ color: 0x465963, roughness: 0.55, metalness: 0.2 }),
      cream: this._material({ color: 0xeee3c8, roughness: 0.95 }),
      leafMat: this._material({ color: 0x608868, roughness: 0.94, flatShading: true }),
      leafLight: this._material({ color: 0x8ca678, roughness: 0.95, flatShading: true }),
      terracotta: this._material({ color: 0xb78262, roughness: 1 }),
      eyes: this._material({ color: 0x293c44, roughness: 0.9 }),
      skin: this._material({ color: 0xffd3a4, roughness: 0.9 }),
    };
    this.assets.glass = new THREE.MeshPhysicalMaterial({ color: 0xb8d7d1, transparent: true, opacity: 0.18, roughness: 0.16, metalness: 0, depthWrite: false, side: THREE.DoubleSide });
    this._materials.add(this.assets.glass);
  }

  _createLighting() {
    this.scene.add(new THREE.HemisphereLight(0xc1e0ec, 0x64534c, 2.5));
    const sun = new THREE.DirectionalLight(0xffdfb3, 3.3);
    sun.position.set(-9, 23, -11);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    Object.assign(sun.shadow.camera, { left: -20, right: 20, top: 15, bottom: -15, near: 1, far: 60 });
    sun.shadow.camera.updateProjectionMatrix();
    sun.shadow.normalBias = 0.025;
    sun.shadow.bias = -0.0002;
    sun.shadow.radius = 3;
    this.scene.add(sun);
    const rim = new THREE.DirectionalLight(0x91c8de, 1.1);
    rim.position.set(8, 10, 14);
    this.scene.add(rim);
  }

  _mesh(geometry, material, parent, position = [0, 0, 0], scale = [1, 1, 1]) {
    const mesh = new THREE.Mesh(geometry, material);
    mesh.position.set(...position);
    mesh.scale.set(...scale);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    parent.add(mesh);
    return mesh;
  }
  _box(parent, material, position, scale) { return this._mesh(this.assets.box, material, parent, position, scale); }
  _cylinder(parent, material, position, scale) { return this._mesh(this.assets.cylinder, material, parent, position, scale); }

  _createPlatform() {
    const platform = new THREE.Group();
    this.scene.add(platform);
    this._box(platform, this._material({ color: 0x6b736d, roughness: 1 }), [0, -0.22, 0], [23.6, 0.4, 8.9]);
    this._box(platform, this._material({ color: 0x92968a, roughness: 1 }), [0, -0.5, 0], [23.15, 0.2, 8.45]);
    const floorMat = this._material({ color: 0xffffff, roughness: 0.96 });
    const planks = new THREE.InstancedMesh(this.assets.box, floorMat, 32);
    const matrix = new THREE.Matrix4();
    const quaternion = new THREE.Quaternion();
    for (let index = 0; index < 32; index++) {
      matrix.compose(new THREE.Vector3((index - 15.5) * 0.728, 0.045, 0), quaternion, new THREE.Vector3(0.718, 0.13, 8.65));
      planks.setMatrixAt(index, matrix);
      planks.setColorAt(index, new THREE.Color(0xbda584).multiplyScalar(0.94 + hash(index) * 0.13));
    }
    planks.receiveShadow = true;
    planks.instanceMatrix.needsUpdate = true;
    if (planks.instanceColor) planks.instanceColor.needsUpdate = true;
    platform.add(planks);
    // Three small entry steps make the rooms feel separate but part of one desk.
    for (const x of [-7.2, 0, 7.2]) this._box(platform, this.assets.woodDark, [x, -0.035, -4.45], [2.1, 0.2, 0.45]);
  }

  _createOffice(id, x) {
    const group = new THREE.Group();
    group.position.x = x;
    group.userData.provider = id;
    this.scene.add(group);
    const accent = this._material({ color: COLORS[id], roughness: 0.9 });
    const roomFloor = this._material({ color: id === 'codex' ? 0xa0b19b : id === 'claude' ? 0xc6ac94 : 0x9bb4bb, roughness: 1 });
    const wallMat = this._material({ color: id === 'codex' ? 0xd4decb : id === 'claude' ? 0xe3cdbb : 0xc5dce0, roughness: 1 });
    this._box(group, roomFloor, [0, 0.145, 0], [6.6, 0.065, 6.35]);
    this._box(group, wallMat, [0, 1.015, 3.15], [6.6, 1.68, 0.15]);
    this._box(group, this.assets.woodDark, [0, 0.24, 3.02], [6.5, 0.12, 0.1]);
    this._box(group, this.assets.glass, [3.26, 0.84, 0.15], [0.055, 1.3, 5.9]).castShadow = false;
    this._box(group, accent, [3.26, 1.5, 0.15], [0.075, 0.035, 5.9]);
    for (const z of [-2.8, 3.08]) this._box(group, this.assets.metal, [3.26, 0.85, z], [0.045, 1.35, 0.045]);
    const outlineMaterial = this._material({ color: COLORS[id], emissive: COLORS[id], emissiveIntensity: 0.45, transparent: true, opacity: 0.15, depthWrite: false });
    const selection = new THREE.Group();
    group.add(selection);
    for (const z of [-3.2, 3.2]) this._box(selection, outlineMaterial, [0, 0.195, z], [6.65, 0.025, 0.035]);
    for (const side of [-1, 1]) this._box(selection, outlineMaterial, [side * 3.32, 0.195, 0], [0.035, 0.025, 6.4]);
    const room = { id, group, x, accent, outlineMaterial, phase: 'loading', provider: null, job: null, selection, idleObjects: [], screen: null, avatar: null, tool: '' };
    this._rooms.set(id, room);
    this._createDesk(room);
    this._createWorker(room);
    this._plant(group, -2.58, -2.35, id === 'claude' ? 1.05 : 0.85);
    this._createDecor(room);
    this._createLabel(room);
  }

  _createDesk(room) {
    const group = room.group;
    this._box(group, this.assets.wood, [0, 1.04, -0.63], [2.75, 0.13, 1.18]);
    for (const x of [-1.16, 1.16]) for (const z of [-1.08, -0.17]) this._box(group, this.assets.metal, [x, 0.605, z], [0.065, 0.8, 0.065]);
    this._box(group, room.accent, [-0.12, 1.115, -0.54], [1.55, 0.015, 0.74]);
    this._box(group, this.assets.metal, [0, 1.145, -0.57], [0.92, 0.045, 0.57]);
    const lid = this._box(group, this.assets.metal, [0, 1.435, -0.85], [0.96, 0.55, 0.055]);
    lid.rotation.x = -0.12;
    const screenMat = this._material({ color: 0x263b42, emissive: COLORS[room.id], emissiveIntensity: 0.06, roughness: 0.45 });
    const screen = this._box(lid, screenMat, [0, 0, 0.53], [0.88, 0.84, 0.02]);
    screen.castShadow = false;
    room.screen = screenMat;
    // Static geometric UI marks are decoration; no invented activity/progress.
    for (let row = 0; row < 3; row++) this._box(lid, room.accent, [-0.16, 0.22 - row * 0.21, 0.56], [0.5 + (row % 2) * 0.13, 0.055, 0.015]);
    this._box(group, this.assets.cream, [0.72, 1.15, -0.47], [0.2, 0.06, 0.31]);
    this._cylinder(group, this.assets.cream, [1.03, 1.24, -0.3], [0.09, 0.23, 0.09]);
    const coffee = this._material({ color: 0x6a4a36, roughness: 1 });
    this._cylinder(group, coffee, [1.03, 1.36, -0.3], [0.072, 0.006, 0.072]);
    this._lamp(group, 1.08, -0.99, room.accent);
    // The chair stays behind its worker so the open front never hides a face.
    this._cylinder(group, this.assets.metal, [0, 0.42, 0.6], [0.045, 0.47, 0.045]);
    this._cylinder(group, this.assets.metal, [0, 0.205, 0.6], [0.38, 0.04, 0.38]);
    this._box(group, room.accent, [0, 0.69, 0.6], [0.68, 0.13, 0.64]);
    this._box(group, room.accent, [0, 1.05, 0.93], [0.67, 0.68, 0.11]);
    for (const side of [-1, 1]) this._box(group, this.assets.metal, [side * 0.4, 0.88, 0.57], [0.05, 0.06, 0.44]);
  }

  _lamp(parent, x, z, accent) {
    this._cylinder(parent, this.assets.metal, [x, 1.14, z], [0.18, 0.045, 0.18]);
    this._cylinder(parent, this.assets.metal, [x, 1.48, z], [0.025, 0.64, 0.025]);
    const shade = this._mesh(this.assets.cone, accent, parent, [x - 0.1, 1.82, z], [0.24, 0.25, 0.24]);
    shade.rotation.z = 0.45;
    this._cylinder(parent, this._material({ color: 0xffe2b2, emissive: 0xffd895, emissiveIntensity: 0.4 }), [x - 0.13, 1.72, z], [0.15, 0.02, 0.15]);
  }

  _createWorker(room) {
    const avatar = new THREE.Group();
    avatar.position.set(0, 0.185, 0.27);
    avatar.rotation.y = Math.PI;
    room.group.add(avatar);
    this._box(avatar, room.accent, [0, 0.93, 0], [0.5, 0.52, 0.36]);
    for (const side of [-1, 1]) this._box(avatar, this.assets.metal, [side * 0.14, 0.52, 0.13], [0.18, 0.32, 0.35]);
    const head = new THREE.Group();
    head.position.y = 1.36;
    avatar.add(head);
    this._mesh(this.assets.sphere, this.assets.skin, head, [0, 0, 0], [0.29, 0.29, 0.27]);
    this._cylinder(head, this.assets.woodDark, [0, 0.22, 0], [0.36, 0.055, 0.36]);
    this._cylinder(head, room.accent, [0, 0.32, 0], [0.26, 0.18, 0.26]);
    for (const side of [-1, 1]) this._mesh(this.assets.sphere, this.assets.eyes, head, [side * 0.105, 0.025, 0.25], [0.034, 0.04, 0.024]);
    this._box(head, this.assets.eyes, [0, -0.095, 0.258], [0.075, 0.018, 0.014]);
    const arms = [];
    for (const side of [-1, 1]) {
      const arm = new THREE.Group();
      arm.position.set(side * 0.31, 1.08, 0);
      avatar.add(arm);
      this._box(arm, room.accent, [0, -0.14, 0.035], [0.14, 0.3, 0.18]);
      this._box(arm, this.assets.skin, [0, -0.33, 0.065], [0.14, 0.13, 0.16]);
      arms.push(arm);
    }
    room.avatar = { group: avatar, head, arms, baseY: avatar.position.y };
  }

  _plant(parent, x, z, scale = 1) {
    const plant = new THREE.Group();
    plant.position.set(x, 0.18, z);
    plant.scale.setScalar(scale);
    parent.add(plant);
    this._cylinder(plant, this.assets.terracotta, [0, 0.22, 0], [0.28, 0.43, 0.28]);
    this._cylinder(plant, this.assets.woodDark, [0, 0.44, 0], [0.23, 0.02, 0.23]);
    for (let index = 0; index < 6; index++) {
      const angle = index * TAU / 6;
      const leaf = this._mesh(this.assets.leaf, index % 2 ? this.assets.leafMat : this.assets.leafLight, plant,
        [Math.sin(angle) * 0.18, 0.75 + (index % 2) * 0.1, Math.cos(angle) * 0.18], [0.16, 0.4, 0.11]);
      leaf.rotation.set(Math.cos(angle) * 0.48, angle, -Math.sin(angle) * 0.48);
    }
  }

  _books(parent, x, y, z, count, accent) {
    for (let index = 0; index < count; index++) {
      const material = index % 3 === 0 ? accent : index % 3 === 1 ? this.assets.cream : this.assets.woodDark;
      this._box(parent, material, [x + index * 0.16, y + (index % 3) * 0.025, z], [0.12, 0.38 + (index % 3) * 0.05, 0.29]);
    }
  }

  _createDecor(room) {
    const group = room.group;
    if (room.id === 'codex') {
      this._box(group, this.assets.woodDark, [1.55, 1.12, 3.025], [2.25, 1.05, 0.055]);
      for (let column = 0; column < 7; column++) for (let row = 0; row < 3; row++) this._box(group, room.accent, [0.65 + column * 0.3, 0.78 + row * 0.31, 2.98], [0.035, 0.035, 0.025]);
      this._box(group, this.assets.cream, [1.06, 1.12, 2.92], [0.1, 0.6, 0.06]);
      this._box(group, this.assets.metal, [1.8, 1.04, 2.91], [0.4, 0.1, 0.08]).rotation.z = -0.35;
      this._box(group, this.assets.wood, [-1.85, 0.9, 2.82], [1.75, 0.08, 0.5]);
      this._books(group, -2.5, 1.16, 2.8, 5, room.accent);
      for (let index = 0; index < 3; index++) {
        const block = this._box(group, index === 1 ? this.assets.cream : room.accent, [-1.04 + (index % 2) * 0.27, 1.22 + (index === 2 ? 0.22 : 0), -0.47], [0.2, 0.2, 0.2]);
        block.userData.base = block.position.clone();
        room.idleObjects.push(block);
      }
    } else if (room.id === 'claude') {
      for (const x of [-2.35, 2.13]) {
        this._box(group, this.assets.woodDark, [x, 0.95, 2.73], [1.25, 1.5, 0.55]);
        for (const y of [0.53, 1.1]) {
          this._box(group, this.assets.wood, [x, y, 2.36], [1.3, 0.055, 0.65]);
          this._books(group, x - 0.43, y + 0.23, 2.34, 6, room.accent);
        }
      }
      const book = new THREE.Group();
      book.position.set(-0.98, 1.14, -0.48);
      group.add(book);
      this._box(book, room.accent, [0, 0.02, 0], [0.55, 0.04, 0.4]);
      const page = this._box(book, this.assets.cream, [0.135, 0.065, 0], [0.25, 0.03, 0.36]);
      this._box(book, this.assets.cream, [-0.135, 0.065, 0], [0.25, 0.03, 0.36]).rotation.z = -0.12;
      room.bookPage = page;
      const art = this._box(group, room.accent, [0, 1.2, 3.025], [1.5, 0.83, 0.045]);
      this._box(art, this.assets.cream, [0, 0, -0.6], [0.84, 0.76, 0.08]);
      for (const [x, y, radius] of [[-0.18, 0.06, 0.2], [0.07, 0.14, 0.25], [0.27, 0.04, 0.18]]) this._mesh(this.assets.sphere, this.assets.leafLight, group, [x, 1.2 + y, 2.96], [radius, radius * 0.5, 0.035]);
    } else {
      this._box(group, this.assets.cream, [0.6, 1.15, 3.025], [2.2, 1.05, 0.045]);
      const route = this._geometry(new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(-0.3, 0.93, 2.98), new THREE.Vector3(0.28, 1.36, 2.98), new THREE.Vector3(0.83, 1.02, 2.98), new THREE.Vector3(1.44, 1.4, 2.98),
      ]));
      const routeMat = new THREE.LineBasicMaterial({ color: 0x6d9baf });
      this._materials.add(routeMat);
      group.add(new THREE.Line(route, routeMat));
      this._box(group, this.assets.woodDark, [-2.18, 0.56, 2.53], [1.45, 0.76, 0.85]);
      this._box(group, room.accent, [-2.18, 0.96, 2.53], [1.53, 0.06, 0.9]);
      this._books(group, -2.65, 1.2, 2.54, 5, room.accent);
      const mobile = new THREE.Group();
      mobile.position.set(-0.99, 1.43, -0.5);
      group.add(mobile);
      this._cylinder(group, this.assets.metal, [-0.99, 1.24, -0.5], [0.02, 0.23, 0.02]);
      const ring = this._mesh(this.assets.torus, room.accent, mobile, [0, 0, 0], [0.25, 0.25, 0.25]);
      ring.rotation.set(Math.PI / 3, 0, -0.3);
      this._mesh(this.assets.sphere, this.assets.cream, mobile, [0, 0, 0], [0.09, 0.09, 0.09]);
      room.orbitStar = this._mesh(this.assets.star, room.accent, mobile, [0.25, 0, 0], [0.65, 0.65, 0.65]);
      // A small telescope is static decoration, never invented tool activity.
      for (let index = 0; index < 3; index++) {
        const angle = index * TAU / 3;
        const leg = this._cylinder(group, this.assets.metal, [2.1 + Math.cos(angle) * 0.14, 0.65, 1.5 + Math.sin(angle) * 0.14], [0.025, 0.95, 0.025]);
        leg.rotation.set(Math.sin(angle) * 0.25, 0, -Math.cos(angle) * 0.25);
      }
      const scope = this._cylinder(group, room.accent, [2.1, 1.32, 1.5], [0.14, 0.76, 0.14]);
      scope.rotation.z = -Math.PI / 3;
    }
  }

  _createLabel(room) {
    const button = document.createElement('button');
    button.className = 'office-room-label';
    button.type = 'button';
    button.setAttribute('data-provider', room.id);
    Object.assign(button.style, {
      position: 'absolute', left: '0', top: '0', pointerEvents: 'auto', cursor: 'pointer',
      color: '#e9efdf', background: 'rgba(16,33,36,.86)', border: '1px solid rgba(177,218,204,.2)',
      borderRadius: '10px', padding: '7px 11px', width: '166px', textAlign: 'left',
      font: 'inherit', lineHeight: '1.45', boxShadow: '0 5px 22px rgba(0,0,0,.13)',
      backdropFilter: 'blur(10px)', transformOrigin: 'center',
    });
    const name = document.createElement('strong');
    name.className = 'office-label-name';
    Object.assign(name.style, { display: 'block', fontSize: '11px', fontWeight: '500', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' });
    name.textContent = NAMES[room.id];
    const model = document.createElement('span');
    model.className = 'office-label-model';
    Object.assign(model.style, { display: 'block', color: '#9eb7ab', fontSize: '8px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' });
    model.textContent = '等待模型設定';
    const status = document.createElement('span');
    status.className = 'office-label-status';
    Object.assign(status.style, { display: 'block', color: '#789b8b', fontSize: '8px', marginTop: '2px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' });
    status.textContent = '等待狀態';
    button.append(name, model, status);
    button.addEventListener('click', () => { this.selectProvider(room.id); this.onSelect(room.id); });
    this.labelLayer.appendChild(button);
    this._labels.set(room.id, { button, name, model, status, anchor: new THREE.Vector3(room.x, 3.3, 0) });
  }

  _phase(provider, job, connected) {
    if (!connected) return 'offline';
    if (!provider) return 'loading';
    if (job?.cancel_requested) return 'stopping';
    if (job?.status === 'running') return 'running';
    if (job?.status === 'queued') return 'queued';
    if (provider.team_run_id) return 'waiting';
    if (!provider.available) return 'missing';
    return provider.phase === 'error' || provider.auth === 'required' ? 'error' : 'idle';
  }

  _modelName(provider, job) {
    const model = job?.model || job?.configured_model || provider?.model || provider?.configured_model;
    if (!model) return '模型由 CLI 選擇';
    return safeText(provider?.model_options?.find(option => option.id === model)?.name) || safeText(model);
  }

  applyState(state) {
    if (this._disposed || !Array.isArray(state?.providers) || !Array.isArray(state?.jobs)) return;
    this._connected = state.view_connected !== false;
    for (const [id, room] of this._rooms) {
      const provider = state.providers.find(item => item.id === id);
      const active = state.jobs.filter(job => job.provider === id && job.kind !== 'collaboration' && ['queued', 'running'].includes(job.status));
      const job = active.find(item => item.id === provider?.active_job_id) || active[0] || null;
      room.provider = provider;
      room.job = job;
      room.phase = this._phase(provider, job, this._connected);
      room.tool = typeof job?.active_tool === 'string' ? job.active_tool : safeText(job?.active_tool?.name);
      const label = this._labels.get(id);
      const displayName = safeText(provider?.name) || NAMES[id];
      label.name.textContent = displayName;
      label.model.textContent = this._modelName(provider, job);
      const statuses = {
        offline: '連線中斷 · 等待同步', loading: '等待狀態', missing: '未偵測到 CLI',
        idle: '閒置演出 · 非 LLM', error: provider?.auth === 'required' ? '需要登入' : 'CLI 狀態異常',
        queued: '任務已排隊', waiting: '團隊協作 · 等待交接', stopping: '正在停止任務', running: room.tool ? `工具 · ${room.tool}` : 'CLI 任務執行中',
      };
      label.status.textContent = statuses[room.phase];
      label.button.setAttribute('data-phase', room.phase);
      label.button.title = [displayName, label.model.textContent, statuses[room.phase], safeText(provider?.transport) ? `執行介面：${provider.transport}` : ''].filter(Boolean).join('\n');
      label.status.style.color = room.phase === 'running' ? '#d1dfa8' : room.phase === 'error' ? '#dfae8a' : '#789b8b';
      room.screen.emissiveIntensity = room.phase === 'running' ? 0.7 : room.phase === 'idle' ? 0.08 : 0;
      this._resetPose(room);
    }
    this._updateLabels();
  }

  _resetPose(room) {
    const { group, head, arms, baseY } = room.avatar;
    group.position.y = baseY;
    group.rotation.z = 0;
    head.rotation.set(0, -0.15, 0);
    arms.forEach(arm => arm.rotation.set(0, 0, 0));
    for (const block of room.idleObjects) block.position.copy(block.userData.base);
    if (room.bookPage) room.bookPage.rotation.z = 0.12;
    if (room.orbitStar) room.orbitStar.position.set(0.25, 0, 0);
  }

  _animateRoom(room) {
    if (this._reducedMotion || !this._connected) return;
    const { group, head, arms, baseY } = room.avatar;
    const time = this._time;
    if (room.phase === 'running') {
      head.rotation.x = 0.12;
      head.rotation.y = 0;
      room.screen.emissiveIntensity = 0.68 + Math.sin(time * 2) * 0.12;
      arms.forEach((arm, index) => { arm.rotation.x = room.tool ? -0.9 + Math.sin(time * 9 + index * Math.PI) * 0.08 : -0.55; });
    } else if (room.phase === 'idle') {
      group.position.y = baseY + Math.sin(time * 1.2) * 0.012;
      if (room.id === 'codex') {
        const reach = Math.max(0, Math.sin(time * TAU / 7));
        head.rotation.y = -0.15 + reach * 0.28;
        arms[1].rotation.x = -reach * 0.4;
        const block = room.idleObjects[0];
        block.position.y = block.userData.base.y + reach * 0.12;
        block.position.x = block.userData.base.x + reach * 0.15;
      } else if (room.id === 'claude') {
        head.rotation.z = Math.sin(time * 0.7) * 0.09;
        head.rotation.y = -0.15 + Math.sin(time * 0.45) * 0.22;
        room.bookPage.rotation.z = 0.12 + Math.max(0, Math.sin(time * 0.6)) * 0.45;
      } else {
        room.orbitStar.position.set(Math.cos(time * 0.9) * 0.25, Math.sin(time * 0.9) * 0.12, Math.sin(time * 0.9) * 0.2);
        room.orbitStar.rotation.y = time * 0.7;
        const stretch = Math.max(0, Math.sin(time * 0.48));
        arms.forEach((arm, index) => { arm.rotation.z = stretch * (index ? -0.23 : 0.23); });
        head.rotation.y = -0.15 + Math.sin(time * 0.4) * 0.2;
      }
    }
  }

  selectProvider(id) {
    if (!IDS.includes(id) || this._disposed) return;
    this._selected = id;
    for (const [key, room] of this._rooms) {
      const selected = key === id;
      room.outlineMaterial.opacity = selected ? 0.8 : 0.13;
      room.outlineMaterial.emissiveIntensity = selected ? 0.65 : 0.15;
      const label = this._labels.get(key);
      label.button.setAttribute('data-selected', String(selected));
      label.button.setAttribute('aria-pressed', String(selected));
      label.button.style.borderColor = selected ? `#${COLORS[key].toString(16)}` : 'rgba(177,218,204,.2)';
      label.button.style.background = selected ? 'rgba(25,48,45,.94)' : 'rgba(16,33,36,.86)';
    }
    this._updateLabels();
  }

  _pick(event) {
    const down = this._down;
    this._down = null;
    if (!down || down.button !== 0 || Math.hypot(event.clientX - down.x, event.clientY - down.y) > 5 || performance.now() - down.at > 900) return;
    const rect = this.canvas.getBoundingClientRect();
    this.camera.updateMatrixWorld();
    this._pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
    this._raycaster.setFromCamera(this._pointer, this.camera);
    const hits = this._raycaster.intersectObjects([...this._rooms.values()].map(room => room.group), true);
    for (const hit of hits) {
      let object = hit.object;
      while (object && !object.userData.provider) object = object.parent;
      if (object?.userData.provider) {
        this.selectProvider(object.userData.provider);
        this.onSelect(object.userData.provider);
        return;
      }
    }
  }

  _updateLabels() {
    if (!this.camera || !this._width) return;
    this.camera.updateMatrixWorld();
    const compact = this._width < 640;
    for (const [id, label] of this._labels) {
      const point = label.anchor.clone().project(this.camera);
      const x = (point.x * 0.5 + 0.5) * this._width;
      const y = (-point.y * 0.5 + 0.5) * this._height;
      label.button.style.transform = `translate(-50%,-50%) translate(${x}px,${y}px)`;
      label.button.style.visibility = point.z < -1 || point.z > 1 || point.x < -1.2 || point.x > 1.2 || point.y < -1.2 || point.y > 1.2 ? 'hidden' : 'visible';
      label.button.style.width = compact ? (id === this._selected ? '142px' : '108px') : '166px';
      label.button.style.padding = compact ? '5px 8px' : '7px 11px';
      label.model.style.display = compact && id !== this._selected ? 'none' : 'block';
      label.name.style.fontSize = compact ? '9px' : '11px';
      label.status.style.fontSize = compact ? '7px' : '8px';
    }
  }

  _resize() {
    if (this._disposed) return;
    this._width = Math.max(1, this.container.clientWidth);
    this._height = Math.max(1, this.container.clientHeight);
    const aspect = this._width / this._height;
    const halfHeight = Math.max(10.25, 13.6 / aspect);
    this.camera.left = -halfHeight * aspect;
    this.camera.right = halfHeight * aspect;
    this.camera.top = halfHeight;
    this.camera.bottom = -halfHeight;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(this._width, this._height, false);
    this._updateLabels();
  }

  resetCamera() {
    const damping = this.controls.enableDamping;
    this.controls.enableDamping = false;
    this.controls.update(0);
    this.camera.position.set(24, 24 * Math.SQRT2 + 0.6, -24);
    this.camera.zoom = 1;
    this.controls.target.set(0, 0.6, 0);
    this.controls.update(0);
    this.camera.updateProjectionMatrix();
    this.controls.saveState();
    this.controls.reset();
    this.controls.enableDamping = damping;
    this._updateLabels();
  }

  _animate(now) {
    if (this._disposed) return;
    const delta = clamp((now - this._lastFrame) / 1000, 0, 0.05);
    this._lastFrame = now;
    if (this._connected && !this._reducedMotion) this._time += delta;
    for (const room of this._rooms.values()) this._animateRoom(room);
    this.controls.update(delta);
    this._updateLabels();
    this.renderer.render(this.scene, this.camera);
    this._frame = requestAnimationFrame(time => this._animate(time));
  }

  get telemetry() {
    return {
      connected: this._connected, selected: this._selected,
      drawCalls: this.renderer.info.render.calls, triangles: this.renderer.info.render.triangles,
      geometries: this.renderer.info.memory.geometries,
      rooms: [...this._rooms.values()].map(room => ({ id: room.id, phase: room.phase, model: this._modelName(room.provider, room.job), tool: room.tool })),
    };
  }

  dispose() {
    if (this._disposed) return;
    this._disposed = true;
    cancelAnimationFrame(this._frame);
    this._resizeObserver.disconnect();
    this.canvas.removeEventListener('pointerdown', this._onPointerDown);
    this.canvas.removeEventListener('pointerup', this._onPointerUp);
    this.canvas.removeEventListener('pointercancel', this._onPointerCancel);
    this.controls.dispose();
    this.scene.traverse(object => { if (object.isInstancedMesh) object.dispose(); });
    this._geometries.forEach(geometry => geometry.dispose());
    this._materials.forEach(material => material.dispose());
    this.renderer.dispose();
    this.renderer.forceContextLoss();
    this.labelLayer.remove();
    this.canvas.remove();
    this._labels.clear();
    this._rooms.clear();
    this.scene.clear();
  }
}
