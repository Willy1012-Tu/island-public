import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const TAU = Math.PI * 2;
const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
const hash = (x, y = 0) => {
  const value = Math.sin(x * 127.1 + y * 311.7 + 19.19) * 43758.5453;
  return value - Math.floor(value);
};
const ease = (t) => t * t * (3 - 2 * t);
const keyOf = (x, y) => `${x},${y}`;

/** Procedural island view. Python remains authoritative for all game state. */
export class IslandScene {
  constructor(container) {
    if (!(container instanceof HTMLElement)) throw new TypeError('IslandScene requires a container.');
    this.container = container;
    this.scene = new THREE.Scene();
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.06;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.canvas = this.renderer.domElement;
    this.canvas.className = 'island-canvas';
    this.canvas.setAttribute('role', 'img');
    this.canvas.setAttribute('aria-label', '可旋轉、縮放的立體浮島與探索者');
    Object.assign(this.canvas.style, { display: 'block', width: '100%', height: '100%', touchAction: 'none' });
    container.appendChild(this.canvas);

    this.camera = new THREE.OrthographicCamera(-16, 16, 14, -14, 0.1, 150);
    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.075;
    this.controls.minZoom = 0.65;
    this.controls.maxZoom = 2.6;
    this.controls.minPolarAngle = 0.24;
    this.controls.maxPolarAngle = Math.PI * 0.48;
    this.controls.rotateSpeed = 0.65;
    this.controls.zoomSpeed = 0.7;
    this.controls.panSpeed = 0.75;
    this.controls.screenSpacePanning = true;

    this._geometries = new Set();
    this._materials = new Set();
    this._resources = new Map();
    this._seenEvents = new Set();
    this._bursts = [];
    this._terrainSignature = '';
    this._pathSignature = '';
    this._size = 20;
    this._time = 0;
    this._lastFrame = 0;
    this._tick = -1;
    this._disposed = false;
    this._started = false;
    this._paused = false;
    this._reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    this.controls.enableDamping = !this._reducedMotion;
    this._streamResetPending = true;
    this._worldSeed = undefined;
    this._state = null;
    this._agentTween = null;
    this._agentGrid = null;
    this._targetYaw = 0;

    this.island = new THREE.Group();
    this.scene.add(this.island);
    this.terrain = new THREE.Group();
    this.resourceGroup = new THREE.Group();
    this.pathGroup = new THREE.Group();
    this.effects = new THREE.Group();
    this.island.add(this.terrain, this.resourceGroup, this.pathGroup, this.effects);
    this._createSharedAssets();
    this._createLighting();
    this._createExplorer();
    this.resetCamera();
    this._resizeObserver = new ResizeObserver(() => this._resize());
    this._resizeObserver.observe(container);
    this._resize();
  }

  _geometry(geometry) {
    this._geometries.add(geometry);
    return geometry;
  }

  _material(parameters, basic = false) {
    const material = basic ? new THREE.MeshBasicMaterial(parameters) : new THREE.MeshStandardMaterial(parameters);
    this._materials.add(material);
    return material;
  }

  _createSharedAssets() {
    this.assets = {
      box: this._geometry(new THREE.BoxGeometry(1, 1, 1)),
      trunk: this._geometry(new THREE.CylinderGeometry(0.085, 0.12, 0.75, 5)),
      pine: this._geometry(new THREE.ConeGeometry(1, 1, 5)),
      rock: this._geometry(new THREE.DodecahedronGeometry(0.48, 0)),
      dot: this._geometry(new THREE.SphereGeometry(0.065, 6, 4)),
      ring: this._geometry(new THREE.TorusGeometry(0.29, 0.024, 5, 24)),
      trunkMat: this._material({ color: 0x78533b, roughness: 1, flatShading: true }),
      pineMats: [0x254e40, 0x39775a, 0x589664].map(color => this._material({ color, roughness: 0.95, flatShading: true })),
      rockMat: this._material({ color: 0x8c9da5, roughness: 0.97, flatShading: true }),
      landMat: this._material({ color: 0xffffff, roughness: 0.95 }),
      baseMat: this._material({ vertexColors: true, roughness: 1, flatShading: true }),
      pathMat: this._material({ color: 0xa2f1e5, transparent: true, opacity: 0.95, depthWrite: false }, true),
    };
    this.assets.waterMat = new THREE.MeshPhysicalMaterial({
      color: 0x3eabb8, roughness: 0.28, metalness: 0.16,
      transparent: true, opacity: 0.73, depthWrite: false,
      emissive: 0x126573, emissiveIntensity: 0.1,
      clearcoat: 0.8, clearcoatRoughness: 0.2,
    });
    this._materials.add(this.assets.waterMat);
    this.assets.waveMat = new THREE.LineBasicMaterial({ color: 0xb2eef0, transparent: true, opacity: 0.35, depthWrite: false });
    this.assets.lineMat = new THREE.LineBasicMaterial({ color: 0xa2f1e5, transparent: true, opacity: 0.8, depthWrite: false });
    this._materials.add(this.assets.waveMat);
    this._materials.add(this.assets.lineMat);
  }

  _createLighting() {
    this.scene.add(new THREE.HemisphereLight(0xb5ddf5, 0x4d3a43, 2.2));
    const sun = new THREE.DirectionalLight(0xffdeb0, 3.2);
    sun.position.set(-12, 24, 10);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    Object.assign(sun.shadow.camera, { left: -20, right: 20, top: 20, bottom: -20, near: 1, far: 65 });
    sun.shadow.normalBias = 0.035;
    sun.shadow.bias = -0.00025;
    sun.shadow.radius = 3;
    this.scene.add(sun);
    const rim = new THREE.DirectionalLight(0x74bcd7, 1.3);
    rim.position.set(10, 7, -15);
    this.scene.add(rim);
  }

  _mesh(geometry, material, parent, position, scale = null) {
    const mesh = new THREE.Mesh(geometry, material);
    if (position) mesh.position.set(...position);
    if (scale) mesh.scale.set(...scale);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    parent.add(mesh);
    return mesh;
  }

  _createExplorer() {
    this.explorer = new THREE.Group();
    this.island.add(this.explorer);
    this.explorer.visible = false;
    const jacket = this._material({ color: 0xe89259, roughness: 0.85 });
    const skin = this._material({ color: 0xffd3a0, roughness: 0.9 });
    const boots = this._material({ color: 0x33474f, roughness: 1 });
    const hat = this._material({ color: 0xe7c27d, roughness: 0.95 });
    const hatBand = this._material({ color: 0x847250, roughness: 1 });
    const pack = this._material({ color: 0x3b8488, roughness: 0.9 });
    const eyes = this._material({ color: 0x283c45, roughness: 0.8 });
    const faceGeo = this._geometry(new THREE.SphereGeometry(0.29, 10, 7));
    this._mesh(this.assets.box, jacket, this.explorer, [0, 0.76, 0], [0.48, 0.52, 0.35]);
    this._mesh(faceGeo, skin, this.explorer, [0, 1.22, 0.015], [1, 1, 0.94]);
    this._mesh(this.assets.box, pack, this.explorer, [0, 0.78, -0.27], [0.4, 0.43, 0.21]);
    const brimGeo = this._geometry(new THREE.CylinderGeometry(0.42, 0.42, 0.065, 12));
    const crownGeo = this._geometry(new THREE.CylinderGeometry(0.265, 0.29, 0.22, 10));
    this._mesh(brimGeo, hat, this.explorer, [0, 1.45, 0]);
    this._mesh(crownGeo, hat, this.explorer, [0, 1.57, 0]);
    this._mesh(this.assets.box, hatBand, this.explorer, [0, 1.49, 0.28], [0.42, 0.055, 0.035]);
    const eyeGeo = this._geometry(new THREE.SphereGeometry(0.034, 6, 5));
    this._mesh(eyeGeo, eyes, this.explorer, [-0.105, 1.25, 0.263]);
    this._mesh(eyeGeo, eyes, this.explorer, [0.105, 1.25, 0.263]);
    this._mesh(this.assets.box, eyes, this.explorer, [0, 1.12, 0.268], [0.08, 0.023, 0.015]);
    this._legs = [];
    this._arms = [];
    for (const side of [-1, 1]) {
      const leg = new THREE.Group();
      leg.position.set(side * 0.14, 0.48, 0);
      this.explorer.add(leg);
      this._mesh(this.assets.box, boots, leg, [0, -0.2, 0.025], [0.17, 0.4, 0.23]);
      this._legs.push(leg);
      const arm = new THREE.Group();
      arm.position.set(side * 0.3, 0.94, 0);
      this.explorer.add(arm);
      this._mesh(this.assets.box, jacket, arm, [0, -0.16, 0], [0.15, 0.31, 0.2]);
      this._mesh(this.assets.box, skin, arm, [0, -0.34, 0.005], [0.14, 0.13, 0.17]);
      this._arms.push(arm);
    }
    const locator = this._mesh(this.assets.ring, this.assets.pathMat, this.explorer, [0, 0.035, 0]);
    locator.rotation.x = -Math.PI / 2;
    locator.scale.setScalar(1.35);
    locator.castShadow = false;
  }

  _heightAt(x, y) {
    const terrain = this._state?.world?.tiles?.[Math.round(y)]?.[Math.round(x)]?.terrain;
    return terrain === 'sand' ? 0.3 : terrain === 'water' ? 0 : 0.6;
  }

  _point(x, y, above = 0) {
    const offset = (this._size - 1) / 2;
    return new THREE.Vector3(x - offset, this._heightAt(x, y) + above, y - offset);
  }

  _clearOwnedGroup(group) {
    group.traverse(child => {
      if (child.isInstancedMesh) child.dispose();
      if (child.userData.ownedGeometry && child.geometry) {
        child.geometry.dispose();
        this._geometries.delete(child.geometry);
      }
    });
    group.clear();
  }

  _buildTerrain(world) {
    this._clearOwnedGroup(this.terrain);
    const batches = { grass: [], sand: [], water: [] };
    world.tiles.forEach((row, y) => row.forEach((tile, x) => {
      if (batches[tile.terrain]) batches[tile.terrain].push({ x, y });
    }));
    const matrix = new THREE.Matrix4();
    const quaternion = new THREE.Quaternion();
    const offset = (this._size - 1) / 2;
    for (const [terrain, cells] of Object.entries(batches)) {
      if (!cells.length) continue;
      const material = terrain === 'water' ? this.assets.waterMat : this.assets.landMat;
      const batch = new THREE.InstancedMesh(this.assets.box, material, cells.length);
      const height = terrain === 'grass' ? 0.55 : terrain === 'sand' ? 0.25 : 0.14;
      const top = terrain === 'grass' ? 0.6 : terrain === 'sand' ? 0.3 : 0;
      const base = new THREE.Color(terrain === 'grass' ? 0x75a76b : terrain === 'sand' ? 0xe6cf98 : 0x72c9d0);
      cells.forEach(({ x, y }, index) => {
        matrix.compose(new THREE.Vector3(x - offset, top - height / 2, y - offset), quaternion, new THREE.Vector3(1, height, 1));
        batch.setMatrixAt(index, matrix);
        batch.setColorAt(index, base.clone().multiplyScalar(0.94 + hash(x, y) * 0.13));
      });
      batch.castShadow = terrain !== 'water';
      batch.receiveShadow = true;
      batch.instanceMatrix.needsUpdate = true;
      if (batch.instanceColor) batch.instanceColor.needsUpdate = true;
      this.terrain.add(batch);
    }
    this._buildFloatingBase();
    this._buildWaterSheen();
  }

  _buildFloatingBase() {
    const edge = (this._size - 2) / 2;
    const outline = [
      [-1, -1], [-1 / 3, -1], [1 / 3, -1], [1, -1],
      [1, -1 / 3], [1, 1 / 3], [1, 1], [1 / 3, 1],
      [-1 / 3, 1], [-1, 1], [-1, 1 / 3], [-1, -1 / 3],
    ];
    const rings = [
      { y: 0.055, scale: 1 }, { y: -0.72, scale: 0.99 },
      { y: -1.65, scale: 0.89 }, { y: -3.6, scale: 0.67 },
      { y: -5.65, scale: 0.3 }, { y: -6.3, scale: 0.035 },
    ];
    const palettes = [0x947455, 0x735b48, 0x697580, 0x536473, 0x424f63];
    const positions = [];
    const colors = [];
    const ringPoint = (ring, index) => {
      const [x, z] = outline[index % outline.length];
      const irregularity = ring < 2 ? 1 : 0.91 + hash(index, ring) * 0.18;
      const data = rings[ring];
      return [x * edge * data.scale * irregularity, data.y + (ring > 1 ? (hash(index + 5, ring) - 0.5) * 0.38 : 0), z * edge * data.scale * irregularity];
    };
    const triangle = (a, b, c, color) => {
      positions.push(...a, ...b, ...c);
      for (let index = 0; index < 3; index++) colors.push(color.r, color.g, color.b);
    };
    for (let ring = 0; ring < rings.length - 1; ring++) {
      for (let index = 0; index < outline.length; index++) {
        const color = new THREE.Color(palettes[ring]).multiplyScalar(0.84 + hash(index, ring + 7) * 0.3);
        const a = ringPoint(ring, index);
        const b = ringPoint(ring, index + 1);
        const c = ringPoint(ring + 1, index);
        const d = ringPoint(ring + 1, index + 1);
        triangle(a, b, c, color);
        triangle(b, d, c, color.clone().multiplyScalar(0.95));
      }
    }
    const geometry = this._geometry(new THREE.BufferGeometry());
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    geometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));
    geometry.computeVertexNormals();
    const base = this._mesh(geometry, this.assets.baseMat, this.terrain);
    base.userData.ownedGeometry = true;
  }

  _buildWaterSheen() {
    const positions = [];
    const outer = this._size / 2 - 0.5;
    for (let side = 0; side < 4; side++) {
      for (let index = 0; index < this._size - 1; index++) {
        const along = index - (this._size - 1) / 2 + hash(index, side) * 0.32;
        const across = outer + (hash(side, index) - 0.5) * 0.45;
        const start = side % 2 ? [across * (side === 1 ? 1 : -1), 0.022, along] : [along, 0.022, across * (side === 0 ? -1 : 1)];
        const end = [...start];
        end[side % 2 ? 2 : 0] += 0.25 + hash(index + 2, side) * 0.28;
        positions.push(...start, ...end);
      }
    }
    const geometry = this._geometry(new THREE.BufferGeometry());
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    this.waterSheen = new THREE.LineSegments(geometry, this.assets.waveMat);
    this.waterSheen.userData.ownedGeometry = true;
    this.terrain.add(this.waterSheen);
  }

  _makeResource(x, y, type) {
    const group = new THREE.Group();
    group.position.copy(this._point(x, y));
    group.rotation.y = hash(x, y) * TAU;
    const size = 0.85 + hash(y, x + 2) * 0.3;
    group.userData = { x, y, type, size, harvest: null };
    group.scale.setScalar(size);
    if (type === 'tree') {
      this._mesh(this.assets.trunk, this.assets.trunkMat, group, [0, 0.36, 0]);
      [[0.56, 1.12, 1.02], [0.45, 0.95, 1.52], [0.32, 0.75, 1.97]].forEach(([radius, height, center], index) => {
        this._mesh(this.assets.pine, this.assets.pineMats[index], group, [0, center, 0], [radius, height, radius]);
      });
    } else {
      const rock = this._mesh(this.assets.rock, this.assets.rockMat, group, [0, 0.32, 0], [0.95, 0.75 + hash(x, y) * 0.28, 0.85]);
      rock.rotation.set(hash(x, y) * 0.45, 0, hash(y, x) * 0.35);
    }
    this.resourceGroup.add(group);
    this._resources.set(keyOf(x, y), group);
    return group;
  }

  _syncResources(world) {
    const present = new Set();
    world.tiles.forEach((row, y) => row.forEach((tile, x) => {
      if (!tile.entity) return;
      const key = keyOf(x, y);
      present.add(key);
      const existing = this._resources.get(key);
      if (existing?.userData.type === tile.entity) return;
      if (existing) this.resourceGroup.remove(existing);
      this._makeResource(x, y, tile.entity);
    }));
    for (const [key, group] of this._resources) {
      if (!present.has(key) && !group.userData.harvest) {
        this.resourceGroup.remove(group);
        this._resources.delete(key);
      }
    }
  }

  _receiveEvents(events) {
    for (const event of events || []) {
      if (event.id === undefined || this._seenEvents.has(event.id)) continue;
      this._seenEvents.add(event.id);
      if (this._seenEvents.size > 2048) this._seenEvents.delete(this._seenEvents.values().next().value);
      if (event.kind === 'harvest' && event.target) {
        const group = this._resources.get(keyOf(event.target.x, event.target.y));
        if (group) group.userData.harvest = { start: this._time, duration: 0.44 };
        this._burst(event.target.x, event.target.y, event.resource === 'rock' ? 0xbad1dd : 0xc9d98c);
      } else if (event.kind === 'game' && this._agentGrid) {
        this._burst(this._agentGrid.x, this._agentGrid.y, 0xffd27b, 1.2);
      }
    }
  }

  /** Tell the view that the next snapshot is a new connection baseline. */
  beginStream() {
    this._streamResetPending = true;
  }

  _burst(x, y, color, height = 0.7) {
    if (this._reducedMotion || this._bursts.length >= 12) return;
    const origin = this._point(x, y, height);
    const count = 18;
    const positions = new Float32Array(count * 3);
    const velocities = [];
    for (let index = 0; index < count; index++) {
      positions.set([origin.x, origin.y, origin.z], index * 3);
      const angle = index * TAU / count;
      const speed = 0.55 + hash(index, x + y) * 0.85;
      velocities.push(new THREE.Vector3(Math.cos(angle) * speed, 1.5 + hash(index, y) * 1.4, Math.sin(angle) * speed));
    }
    const geometry = this._geometry(new THREE.BufferGeometry());
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    const material = new THREE.PointsMaterial({ color, size: 0.085, transparent: true, opacity: 1, depthWrite: false });
    this._materials.add(material);
    const points = new THREE.Points(geometry, material);
    this.effects.add(points);
    this._bursts.push({ points, origin, velocities, start: this._time, duration: 0.8 });
  }

  _updatePath(path, agent) {
    const signature = `${agent.x},${agent.y}|${path.map(point => `${point.x},${point.y}`).join(';')}`;
    if (signature === this._pathSignature) return;
    this._pathSignature = signature;
    this._clearOwnedGroup(this.pathGroup);
    if (!path.length) return;
    const points = [this._point(agent.x, agent.y, 0.075), ...path.map(point => this._point(point.x, point.y, 0.075))];
    const geometry = this._geometry(new THREE.BufferGeometry().setFromPoints(points));
    const line = new THREE.Line(geometry, this.assets.lineMat);
    line.userData.ownedGeometry = true;
    this.pathGroup.add(line);
    const dots = new THREE.InstancedMesh(this.assets.dot, this.assets.pathMat, points.length - 1);
    const matrix = new THREE.Matrix4();
    points.slice(1).forEach((point, index) => {
      matrix.makeTranslation(point.x, point.y, point.z);
      dots.setMatrixAt(index, matrix);
    });
    dots.instanceMatrix.needsUpdate = true;
    this.pathGroup.add(dots);
    const goal = new THREE.Mesh(this.assets.ring, this.assets.pathMat);
    goal.position.copy(points[points.length - 1]);
    goal.rotation.x = -Math.PI / 2;
    this.pathGroup.add(goal);
  }

  applyState(state) {
    if (this._disposed) return;
    if (!state?.world?.tiles || !state.agent) throw new TypeError('Invalid island snapshot.');
    const reset = this._streamResetPending || (this._tick >= 0 && (
      state.tick < this._tick || state.world.seed !== this._worldSeed
      || (state.world.size || state.world.tiles.length) !== this._size
    ));
    this._state = state;
    this._size = state.world.size || state.world.tiles.length;
    this._tick = state.tick;
    this._worldSeed = state.world.seed;
    this._paused = Boolean(state.paused);
    if (reset) {
      this.resourceGroup.clear();
      this._resources.clear();
      this._seenEvents.clear();
      this._agentGrid = null;
      this._agentTween = null;
      for (const burst of this._bursts) this._removeBurst(burst);
      this._bursts = [];
    }
    const signature = `${this._size}|${state.world.tiles.map(row => row.map(tile => tile.terrain[0]).join('')).join('/')}`;
    if (signature !== this._terrainSignature) {
      this._terrainSignature = signature;
      this._buildTerrain(state.world);
    }
    const agent = state.agent;
    const nextPosition = this._point(agent.x, agent.y);
    if (!this._agentGrid) {
      this.explorer.position.copy(nextPosition);
      this.explorer.visible = true;
    } else if (agent.x !== this._agentGrid.x || agent.y !== this._agentGrid.y) {
      this._sampleExplorer();
      const difference = nextPosition.clone().sub(this.explorer.position);
      if (difference.x || difference.z) this._targetYaw = Math.atan2(difference.x, difference.z);
      this._agentTween = {
        from: this.explorer.position.clone(), to: nextPosition,
        start: this._time, duration: this._reducedMotion ? 0 : clamp(Number(state.tick_seconds) || 0.5, 0.08, 0.9) * 0.88,
      };
    }
    this._agentGrid = { x: agent.x, y: agent.y };
    if (reset) {
      // A ring contains recent history, not events that happened after this
      // client joined. Reconnects establish a fresh authoritative baseline too.
      for (const event of state.events || []) {
        if (event.id !== undefined) this._seenEvents.add(event.id);
      }
      this._streamResetPending = false;
    } else {
      // For live updates, mark the existing resource before its removal in the
      // authoritative snapshot so the short harvest animation can finish.
      this._receiveEvents(state.events);
    }
    this._syncResources(state.world);
    this._updatePath(state.path || [], agent);
    if (!this._started) {
      this._started = true;
      this._lastFrame = performance.now();
      this._frame = requestAnimationFrame(now => this._animate(now));
    }
  }

  _sampleExplorer() {
    const tween = this._agentTween;
    if (!tween) return false;
    const progress = tween.duration ? clamp((this._time - tween.start) / tween.duration, 0, 1) : 1;
    this.explorer.position.lerpVectors(tween.from, tween.to, ease(progress));
    if (!this._reducedMotion) this.explorer.position.y += Math.sin(progress * Math.PI) * 0.13;
    const stride = Math.sin(progress * TAU) * 0.42;
    this._legs.forEach((leg, index) => { leg.rotation.x = progress < 1 ? stride * (index ? -1 : 1) : 0; });
    this._arms.forEach((arm, index) => { arm.rotation.x = progress < 1 ? stride * (index ? 0.7 : -0.7) : 0; });
    if (progress >= 1) this._agentTween = null;
    return progress < 1;
  }

  _removeBurst(burst) {
    this.effects.remove(burst.points);
    burst.points.geometry.dispose();
    this._geometries.delete(burst.points.geometry);
    burst.points.material.dispose();
    this._materials.delete(burst.points.material);
  }

  _animate(now) {
    if (this._disposed) return;
    const delta = Math.min((now - this._lastFrame) / 1000, 0.05);
    this._lastFrame = now;
    if (!this._paused) this._time += delta;
    this._sampleExplorer();
    const angle = Math.atan2(Math.sin(this._targetYaw - this.explorer.rotation.y), Math.cos(this._targetYaw - this.explorer.rotation.y));
    if (!this._paused) {
      if (this._reducedMotion || Math.abs(angle) < 0.0001) this.explorer.rotation.y = this._targetYaw;
      else this.explorer.rotation.y += angle * Math.min(1, delta * 13);
    }
    if (!this._reducedMotion) {
      this.island.position.y = Math.sin(this._time * 0.7) * 0.055;
      this.assets.waterMat.opacity = 0.73 + Math.sin(this._time * 0.8) * 0.025;
      this.assets.waveMat.opacity = 0.3 + Math.sin(this._time * 1.25) * 0.075;
      this.assets.pathMat.opacity = 0.82 + Math.sin(this._time * 2.2) * 0.16;
    }
    for (const [key, group] of this._resources) {
      const harvest = group.userData.harvest;
      if (!harvest) continue;
      const progress = clamp((this._time - harvest.start) / harvest.duration, 0, 1);
      if (progress >= 1) {
        this.resourceGroup.remove(group);
        this._resources.delete(key);
      } else {
        const originalSize = group.userData.size;
        const shake = this._reducedMotion ? 0 : Math.sin(progress * Math.PI * 9) * 0.13 * (1 - progress);
        group.rotation.z = shake;
        group.scale.set(originalSize * (1 + progress * 0.15), originalSize * Math.max(0.03, 1 - progress * progress), originalSize * (1 + progress * 0.15));
      }
    }
    this._bursts = this._bursts.filter(burst => {
      const age = this._time - burst.start;
      if (age >= burst.duration) {
        this._removeBurst(burst);
        return false;
      }
      const array = burst.points.geometry.attributes.position.array;
      burst.velocities.forEach((velocity, index) => {
        array[index * 3] = burst.origin.x + velocity.x * age;
        array[index * 3 + 1] = burst.origin.y + velocity.y * age - age * age * 2.2;
        array[index * 3 + 2] = burst.origin.z + velocity.z * age;
      });
      burst.points.geometry.attributes.position.needsUpdate = true;
      burst.points.material.opacity = 1 - age / burst.duration;
      return true;
    });
    this.controls.update(delta);
    this.renderer.render(this.scene, this.camera);
    this._frame = requestAnimationFrame(time => this._animate(time));
  }

  _resize() {
    if (this._disposed) return;
    const width = Math.max(1, this.container.clientWidth);
    const height = Math.max(1, this.container.clientHeight);
    const aspect = width / height;
    const halfHeight = Math.max(13.2, 15.2 / aspect);
    this.camera.left = -halfHeight * aspect;
    this.camera.right = halfHeight * aspect;
    this.camera.top = halfHeight;
    this.camera.bottom = -halfHeight;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height, false);
  }

  resetCamera() {
    // One undamped update consumes OrbitControls' pending rotation/pan deltas.
    // Otherwise resetting during inertia immediately drifts off the new view.
    const damping = this.controls.enableDamping;
    const autoRotate = this.controls.autoRotate;
    this.controls.enableDamping = false;
    this.controls.autoRotate = false;
    this.controls.update(0);
    this.camera.position.set(25, 25 * Math.SQRT2 - 1.3, 25);
    this.camera.zoom = 1;
    this.controls.target.set(0, -1.3, 0);
    this.controls.update(0);
    this.camera.updateProjectionMatrix();
    this.controls.saveState();
    this.controls.reset();
    this.controls.enableDamping = damping;
    this.controls.autoRotate = autoRotate;
  }

  get telemetry() {
    return {
      initialized: this._started, tick: this._tick,
      drawCalls: this.renderer.info.render.calls,
      triangles: this.renderer.info.render.triangles,
      geometries: this.renderer.info.memory.geometries,
      textures: this.renderer.info.memory.textures,
      resources: this._resources.size,
      activeHarvests: [...this._resources.values()].filter(group => group.userData.harvest).length,
      particleBursts: this._bursts.length,
      paused: this._paused,
      agent: this._agentGrid ? {
        x: this.explorer.position.x + (this._size - 1) / 2,
        y: this.explorer.position.z + (this._size - 1) / 2,
        height: this.explorer.position.y,
      } : null,
    };
  }

  dispose() {
    if (this._disposed) return;
    this._disposed = true;
    cancelAnimationFrame(this._frame);
    this._resizeObserver.disconnect();
    this.controls.dispose();
    this.scene.traverse(child => { if (child.isInstancedMesh) child.dispose(); });
    this._geometries.forEach(geometry => geometry.dispose());
    this._materials.forEach(material => material.dispose());
    this.renderer.dispose();
    this.renderer.forceContextLoss();
    this.canvas.remove();
    this.scene.clear();
  }
}
