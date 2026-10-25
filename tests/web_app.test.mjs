/* Test the real HUD/transport module without a browser, build step, or CDN. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const html = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
const source = await readFile(new URL('../web/app.js', import.meta.url), 'utf8');

class Element {
  constructor(id = '', tag = 'div') {
    this.id = id;
    this.tag = tag;
    this.children = [];
    this.content = '';
    this.innerHTML = '';
    this.className = '';
    this.hidden = false;
    this.disabled = false;
    this.attributes = {};
    this.style = {};
    this.listeners = new Map();
    const classes = new Set();
    this.classList = {
      add: (name) => classes.add(name),
      contains: (name) => classes.has(name),
      toggle: (name, force) => {
        const add = force ?? !classes.has(name);
        if (add) classes.add(name); else classes.delete(name);
      },
    };
  }
  get textContent() { return this.content + this.children.map(node => node.textContent).join(''); }
  set textContent(text) { this.content = String(text); this.children = []; }
  append(child) { this.children.push(child); }
  replaceChildren(...children) { this.content = ''; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) {
    const callbacks = this.listeners.get(name) || [];
    callbacks.push(callback);
    this.listeners.set(name, callbacks);
  }
  emit(name, event = {}) { for (const callback of this.listeners.get(name) || []) callback(event); }
  closest(selector) { return selector.includes('button') && this.tag === 'button' ? this : null; }
}

async function start({ failScene = false } = {}) {
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Element(match[1])]));
  const document = new Element('document');
  document.getElementById = (id) => {
    assert.ok(elements.has(id), `HUD references an existing HTML element: ${id}`);
    return elements.get(id);
  };
  document.createElement = (tag) => new Element('', tag);
  document.createTextNode = (text) => ({ textContent: text });
  const window = new Element('window');
  const sockets = [];
  class Socket extends Element {
    static OPEN = 1;
    constructor(url) { super(); this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
    open() { this.readyState = 1; this.emit('open'); }
    send(raw) { this.sent.push(JSON.parse(raw)); }
    receive(payload) { this.emit('message', { data: typeof payload === 'string' ? payload : JSON.stringify(payload) }); }
    close() { this.readyState = 3; this.emit('close'); }
  }
  const scenes = [];
  class Scene {
    constructor(container) { this.container = container; this.states = []; this.resets = 0; this.streams = 0; scenes.push(this); }
    applyState(state) { this.states.push(state); }
    beginStream() { this.streams++; }
    resetCamera() { this.resets++; }
    dispose() { this.disposed = true; }
  }
  const timers = new Map();
  const context = vm.createContext({
    document, window, WebSocket: Socket,
    location: { protocol: 'http:', host: '127.0.0.1:8000', reload() {} },
    console: { error() {} },
    setTimeout: (callback) => { const id = Symbol(); timers.set(id, callback); return id; },
    clearTimeout: (id) => timers.delete(id),
  });
  const sceneModule = new vm.SyntheticModule(['IslandScene'], function () { this.setExport('IslandScene', Scene); }, { context });
  await sceneModule.link(() => {});
  await sceneModule.evaluate();
  const module = new vm.SourceTextModule(source, {
    context,
    importModuleDynamically: async () => {
      if (failScene) throw new Error('CDN unavailable');
      return sceneModule;
    },
  });
  await module.link(() => {});
  await module.evaluate();
  await new Promise(setImmediate);
  return { elements, document, window, sockets, scenes, timers };
}

function snapshot(overrides = {}) {
  return {
    type: 'state', version: 1, tick: 8, tick_seconds: 0.5, paused: false,
    brain: 'demo', thinking: false,
    world: { size: 20, seed: 7, tiles: [] },
    agent: { x: 3, y: 4, boredom: 85, energy: 93, inventory: { wood: 2, stone: 1 } },
    path: [], internal_thought: '來玩森林尋寶！', status: 'Walking to (5,4)',
    current_action: { name: 'gather', target: { x: 5, y: 4 } },
    mini_game: { name: '森林尋寶', rules: '走到樹旁，再採一份木材。' },
    counters: { plans: 2, games: 1, decisions: 3 }, events: [],
    ...overrides,
  };
}

test('a pushed snapshot populates Chinese HUD, meters, game and renderer', async () => {
  const app = await start();
  const get = (id) => app.elements.get(id);
  assert.equal(get('pause-button').disabled, true);
  assert.equal(app.sockets[0].url, 'ws://127.0.0.1:8000/ws');
  app.sockets[0].open();
  const state = snapshot();
  app.sockets[0].receive(state);
  assert.equal(get('pause-button').disabled, false);
  assert.equal(get('thought').textContent, state.internal_thought);
  assert.equal(get('boredom-fill').style.width, '85%');
  assert.equal(get('energy-meter').attributes['aria-valuenow'], '93');
  assert.equal(get('boredom-block').classList.contains('elevated'), true);
  assert.equal(get('mini-game').hidden, false);
  assert.equal(get('game-name').textContent, state.mini_game.name);
  assert.equal(get('game-rules').textContent, state.mini_game.rules);
  assert.equal(get('wood-value').textContent, '2');
  assert.equal(get('tick-value').textContent, '0008');
  assert.equal(get('scene-loading').hidden, true);
  assert.equal(app.scenes[0].states.at(-1).agent.boredom, 85);
  app.sockets[0].receive('{broken json');
  assert.equal(get('tick-value').textContent, '0008');
  app.window.emit('beforeunload');
});

test('buttons and keys send only the agreed controls; pause reflects Python state', async () => {
  const app = await start();
  const get = (id) => app.elements.get(id);
  const socket = app.sockets[0];
  socket.open();
  socket.receive(snapshot());
  get('pause-button').emit('click');
  get('boredom-button').emit('click');
  let prevented = 0;
  app.document.emit('keydown', { code: 'Space', target: new Element(), preventDefault() { prevented++; } });
  app.document.emit('keydown', { code: 'KeyB', target: new Element(), preventDefault() { prevented++; } });
  assert.deepEqual(socket.sent.map(message => message.action), ['toggle_pause', 'boredom', 'toggle_pause', 'boredom']);
  assert.ok(socket.sent.every(message => message.type === 'control' && Object.keys(message).length === 2));
  assert.equal(prevented, 2);
  app.document.emit('keydown', { code: 'Space', target: new Element('', 'button'), preventDefault() { throw new Error('Would double-toggle a focused button'); } });
  app.document.emit('keydown', { code: 'KeyB', repeat: true, target: new Element() });
  assert.equal(socket.sent.length, 4);
  socket.receive(snapshot({ paused: true, mini_game: null }));
  assert.equal(get('pause-label').textContent, '繼續探索');
  assert.equal(get('pause-button').attributes['aria-pressed'], 'true');
  assert.equal(get('mini-game').hidden, true);
  get('reset-camera').emit('click');
  assert.equal(app.scenes[0].resets, 1);
  app.window.emit('beforeunload');
});

test('disconnect disables controls, reconnect primes the scene, unload stops resources', async () => {
  const app = await start();
  const first = app.sockets[0];
  first.open();
  first.receive(snapshot());
  first.close();
  assert.equal(app.elements.get('pause-button').disabled, true);
  assert.equal(app.elements.get('connection-banner').hidden, false);
  assert.equal(app.timers.size, 1);
  app.timers.values().next().value();
  assert.equal(app.sockets.length, 2);
  app.sockets[1].open();
  assert.equal(app.scenes[0].streams, 2);
  assert.equal(app.elements.get('connection-banner').hidden, true);
  app.window.emit('beforeunload');
  assert.equal(app.scenes[0].disposed, true);
  assert.equal(app.sockets[1].readyState, 3);
});

test('model text stays text and a CDN failure leaves a usable HUD with an explicit message', async () => {
  const app = await start({ failScene: true });
  const get = (id) => app.elements.get(id);
  app.sockets[0].open();
  const text = '<img src=x onerror=alert(1)>中文心聲';
  app.sockets[0].receive(snapshot({ internal_thought: text }));
  assert.equal(get('thought').textContent, text);
  assert.equal(get('thought').innerHTML, '');
  assert.equal(get('scene-loading').hidden, false);
  assert.equal(get('retry-scene').hidden, false);
  assert.match(get('loading-message').textContent, /CDN/);
  get('pause-button').emit('click');
  assert.equal(app.sockets[0].sent[0].action, 'toggle_pause');
  app.window.emit('beforeunload');
});
