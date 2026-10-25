/* Exercise the actual monitor DOM/transport code without a browser or model. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const html = await readFile(new URL('../web/monitor.html', import.meta.url), 'utf8');
const source = await readFile(new URL('../web/monitor.js', import.meta.url), 'utf8');

class Element {
  constructor(id = '', tag = 'div') {
    Object.assign(this, { id, tag, children: [], content: '', className: '', value: '', hidden: false, disabled: false, attributes: {}, style: {}, listeners: new Map(), scrollTop: 0, clientHeight: 240 });
    this._classes = new Set();
    this.classList = {
      add: name => this._classes.add(name),
      contains: name => this._classes.has(name),
      toggle: (name, force) => {
        const add = force ?? !this._classes.has(name);
        if (add) this._classes.add(name); else this._classes.delete(name);
      },
    };
  }
  get textContent() { return this.content + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.content = String(value); this.children = []; }
  get innerHTML() { return ''; }
  set innerHTML(value) { throw new Error(`Untrusted text must not be inserted as HTML: ${value}`); }
  get scrollHeight() { return this.children.length * 65; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.content = ''; this.children = children; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(name, callback) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(callback);
    this.listeners.set(name, listeners);
  }
  emit(name, event = {}) { for (const callback of this.listeners.get(name) || []) callback(event); }
}

async function start({ sceneFailure = false } = {}) {
  const elements = new Map([...html.matchAll(/<([a-z][\w:-]*)\b[^>]*\bid="([^"]+)"[^>]*>/gi)].map(match => {
    const element = new Element(match[2], match[1]);
    element.hidden = /\bhidden(?:\s|>|=)/.test(match[0]);
    element.disabled = /\bdisabled(?:\s|>|=)/.test(match[0]);
    return [element.id, element];
  }));
  const document = new Element('document');
  document.getElementById = id => {
    assert(elements.has(id), `Monitor references an existing HTML element: ${id}`);
    return elements.get(id);
  };
  document.createElement = tag => new Element('', tag);
  const window = new Element('window');
  const sockets = [];
  class Socket extends Element {
    static OPEN = 1;
    constructor(url) { super(); this.url = url; this.sent = []; this.readyState = 0; sockets.push(this); }
    open() { this.readyState = 1; this.emit('open'); }
    receive(payload) { this.emit('message', { data: typeof payload === 'string' ? payload : JSON.stringify(payload) }); }
    send(raw) { this.sent.push(JSON.parse(raw)); }
    close() { if (this.readyState === 3) return; this.readyState = 3; this.emit('close'); }
  }
  const timers = new Map();
  const intervals = new Map();
  const offices = [];
  const errors = [];
  const requests = [];
  const responses = [];
  let reloads = 0;
  class FakeOfficeScene {
    constructor(container, options) { this.container = container; this.options = options; this.states = []; this.selections = []; this.resets = 0; this.disposed = false; offices.push(this); }
    applyState(value) { this.states.push(JSON.parse(JSON.stringify(value))); }
    selectProvider(id) { this.selections.push(id); }
    resetCamera() { this.resets++; }
    dispose() { this.disposed = true; }
    pick(id) { this.options.onSelect(id); }
  }
  let now = 1000;
  const context = vm.createContext({
    document, window, WebSocket: Socket, location: { protocol: 'http:', host: '127.0.0.1:8000', reload: () => reloads++ },
    performance: { now: () => now }, console: { ...console, error: (...args) => errors.push(args) },
    fetch: async (url, options = {}) => {
      requests.push({ url, options: JSON.parse(JSON.stringify(options)) });
      assert(responses.length, `Unexpected HTTP request: ${url}`);
      const next = responses.shift();
      const reply = typeof next === 'function' ? await next(url, options) : next;
      const status = reply.status ?? 200;
      return { ok: status >= 200 && status < 300, status, json: async () => reply.body };
    },
    setTimeout: (callback, delay) => { const id = Symbol(); timers.set(id, { callback, delay }); return id; },
    clearTimeout: id => timers.delete(id),
    setInterval: callback => { const id = Symbol(); intervals.set(id, callback); return id; },
    clearInterval: id => intervals.delete(id),
  });
  const officeModule = new vm.SyntheticModule(['OfficeScene'], function () {
    this.setExport('OfficeScene', FakeOfficeScene);
  }, { context });
  await officeModule.link(() => {});
  await officeModule.evaluate();
  const module = new vm.SourceTextModule(source, { context, importModuleDynamically: specifier => {
    assert.equal(specifier, './office-scene.js');
    if (sceneFailure) throw new Error('Simulated CDN or WebGL failure');
    return officeModule;
  } });
  await module.link(() => {});
  await module.evaluate();
  await new Promise(resolve => setImmediate(resolve));
  return {
    elements, window, sockets, timers, intervals, offices, errors, requests,
    respond(reply) { responses.push(reply); },
    flush() { return new Promise(resolve => setImmediate(resolve)); },
    get reloads() { return reloads; },
    get: id => elements.get(id),
    advance(milliseconds) { now += milliseconds; },
    pulse() { for (const callback of intervals.values()) callback(); },
    stop() { window.emit('beforeunload'); },
  };
}

function providers() {
  return [
    { id: 'codex', name: 'Codex', transport: 'codex', configured_model: null, model_options: [], available: true, command: 'codex.exe', reason: '', auth: 'unknown', active_job_id: null, phase: 'idle', execution_mode: 'read_only', execution_note: '保留原始檔案。', persona: { name: '整理小方塊', description: '喜歡把東西排整齊。', idle_lines: ['把工具排成一列。', '替下一件事留個空位。'] } },
    { id: 'claude', name: 'Claude 4.6', transport: 'antigravity', configured_model: 'claude-sonnet-4-6', model_options: [
      { id: 'claude-sonnet-4-6', name: 'Claude Sonnet 4.6' }, { id: 'claude-opus-4-6-thinking', name: 'Claude Opus 4.6 Thinking' },
    ], available: false, command: null, reason: '尚未找到 agy', auth: 'unknown', active_job_id: null, phase: 'missing', execution_mode: 'isolated', persona: { name: '看雲的觀察者', idle_lines: ['看一朵雲。'] } },
    { id: 'antigravity', name: 'Antigravity', transport: 'antigravity', configured_model: 'gemini-3.8-flash-high', model_options: [
      { id: 'gemini-3.8-flash-high', name: 'Gemini 3.8 Flash · High' }, { id: 'gemini-3.8-flash-medium', name: 'Gemini 3.8 Flash · Medium' },
      { id: 'gemini-3.8-flash-low', name: 'Gemini 3.8 Flash · Low' }, { id: 'gpt-oss-120b-medium', name: 'GPT OSS 120B · Medium' },
    ], available: false, command: null, reason: '未找到 agy', auth: 'unknown', active_job_id: null, phase: 'missing', execution_mode: 'isolated', execution_note: '在隔離副本中執行。', persona: { name: '軌道巡遊者', idle_lines: ['沿著小小軌道巡遊。'] } },
  ];
}

function job(overrides = {}) {
  return {
    id: 'job-1', provider: 'codex', prompt: '請檢查專案，不要修改檔案。', status: 'running',
    created_at: '2026-10-25T10:00:00Z', started_at: '2026-10-25T10:00:01Z', finished_at: null,
    elapsed_seconds: 7.2, model: 'saved-model', session_id: 'public-session', latest_message: '正在讀取專案說明。', active_tool: 'Read', usage: null, error: null,
    events: [{ id: 1, at: '2026-10-25T10:00:02Z', kind: 'status', text: 'CLI 已啟動' }], ...overrides,
  };
}

function snapshot(overrides = {}) {
  return { type: 'monitor_state', version: 1, revision: 1, workspace: 'C:\\work\\island', providers: providers(), jobs: [], ...overrides };
}

function load(app, state = snapshot()) {
  const socket = app.sockets.at(-1);
  socket.open();
  socket.receive(state);
  return socket;
}

function submit(app, prompt = '檢查專案測試') {
  app.get('task-prompt').value = prompt;
  app.get('task-prompt').emit('input');
  app.get('task-form').emit('submit', { preventDefault() {} });
}

test('authoritative availability, unknown auth and execution modes stay distinct', async () => {
  const app = await start();
  assert.equal(app.sockets[0].url, 'ws://127.0.0.1:8000/ws/monitor');
  app.sockets[0].open();
  assert.equal(app.get('refresh-providers').disabled, true, 'open transport is not a state snapshot');
  app.sockets[0].receive(snapshot());
  assert.equal(app.get('codex-status').textContent, '待命中');
  assert.equal(app.get('codex-auth').textContent, '登入未驗證');
  assert.equal(app.get('codex-mode').textContent, '唯讀工作區');
  assert.equal(app.get('antigravity-mode').textContent, '隔離副本');
  assert.equal(app.get('claude-status').textContent, '未偵測到 CLI');
  assert.equal(app.get('claude-card').attributes['data-phase'], 'missing');
  assert.equal(app.get('codex-idle-label').textContent, '閒置演出 · 非 LLM');
  assert.equal(app.get('claude-idle-label').textContent, '尚未接入');
  assert.equal(app.get('overview-available').textContent, '1');
  assert.equal(app.get('workspace-path').textContent, 'C:\\work\\island');
  app.get('claude-select').emit('click');
  app.get('task-prompt').value = 'test';
  app.get('task-prompt').emit('input');
  assert.equal(app.get('start-task').disabled, true, 'missing provider cannot start');
  app.stop();
});

test('task creation sends exact control once, waits for actual acknowledgement, and rejects busy starts', async () => {
  const app = await start();
  const socket = load(app);
  submit(app, '  檢查專案測試  ');
  assert.deepEqual(socket.sent, [{ type: 'monitor_control', action: 'start', provider: 'codex', prompt: '檢查專案測試' }]);
  assert.equal(app.get('start-task').disabled, true);
  app.get('task-form').emit('submit', { preventDefault() {} });
  assert.equal(socket.sent.length, 1, 'pending start is not duplicated');
  assert.match(app.get('form-status').textContent, /等待伺服器/);
  const running = job({ prompt: '檢查專案測試' });
  const items = providers(); items[0].phase = 'running'; items[0].active_job_id = running.id; items[0].auth = 'verified';
  socket.receive(snapshot({ revision: 2, providers: items, jobs: [running] }));
  assert.equal(app.get('codex-status').textContent, '執行中');
  assert.equal(app.get('codex-auth').textContent, '登入已驗證');
  assert.equal(app.get('codex-tool').textContent, '工具 · Read');
  assert.equal(app.get('codex-idle-label').textContent, '真實 CLI 任務');
  assert.equal(app.get('overview-running').textContent, '1');
  assert.equal(app.get('cancel-task').disabled, false);
  assert.equal(app.get('start-task').disabled, true);
  app.stop();
});

test('an error is not depicted as work, and a live tracked process survives a missing CLI refresh', async () => {
  const app = await start();
  const items = providers();
  items[0].phase = 'error';
  const socket = load(app, snapshot({ providers: items }));
  assert.equal(app.get('codex-card').attributes['data-phase'], 'error');
  assert.equal(app.get('codex-idle-label').textContent, '狀態異常');
  assert.match(app.get('codex-idle-line').textContent, /CLI 狀態異常/);
  assert(!app.get('codex-idle-line').textContent.includes('處理你交辦的事'));
  items[0].available = false;
  items[0].phase = 'missing';
  items[0].active_job_id = 'job-1';
  socket.receive(snapshot({ revision: 2, providers: items, jobs: [job()] }));
  assert.equal(app.get('codex-status').textContent, '執行中');
  assert.equal(app.get('codex-card').attributes['data-phase'], 'running');
  assert.equal(app.get('cancel-task').disabled, false);
  assert.equal(app.get('start-task').disabled, true);
  app.stop();
});

test('redacted public prompts still acknowledge creation, and required login is explicit', async () => {
  const app = await start();
  const socket = load(app);
  submit(app, 'Please explain this secret-shaped example value.');
  socket.receive(snapshot({ revision: 2, jobs: [job({ status: 'completed', prompt: 'Please explain [REDACTED].' })] }));
  assert.equal(app.get('start-label').textContent, '啟動任務');
  assert.equal(app.get('start-task').disabled, false);
  assert.match(app.get('form-status').textContent, /任務已建立/);
  const items = providers();
  items[0].auth = 'required'; items[0].phase = 'error';
  socket.receive(snapshot({ revision: 3, providers: items }));
  assert.equal(app.get('codex-auth').textContent, '需要登入');
  assert.match(app.get('form-note').textContent, /需要先在終端機完成登入/);
  app.stop();
});

test('public delta messages merge, unknown reasoning is ignored, and all text is literal', async () => {
  const app = await start();
  const unsafe = '<img src=x onerror=alert(1)>中文';
  const running = job({ prompt: unsafe, latest_message: unsafe, events: [
    { id: 1, at: '2026-10-25T10:00:02Z', kind: 'message', text: '第一段', delta: true },
    { id: 2, at: '2026-10-25T10:00:03Z', kind: 'message', text: unsafe, delta: true },
    { id: 3, at: '2026-10-25T10:00:04Z', kind: 'private_reasoning', text: 'do not display this' },
    { id: 4, at: '2026-10-25T10:00:05Z', kind: 'tool', text: unsafe, tool: 'Read', status: 'running' },
    { id: 5, at: '2026-10-25T10:00:06Z', kind: 'result', text: '檢查完成。' },
  ] });
  load(app, snapshot({ jobs: [running] }));
  const feed = app.get('event-feed');
  assert.equal(feed.children.length, 3);
  assert.match(feed.children[0].textContent, /第一段<img/);
  assert.equal(feed.children[0].children[1].textContent, '第一段' + unsafe);
  assert.equal(feed.children[1].children[1].textContent, unsafe);
  assert(!feed.textContent.includes('do not display this'));
  assert.equal(app.get('job-prompt').textContent, unsafe);
  assert.equal(app.get('codex-message').textContent, unsafe);
  assert.equal(app.get('job-prompt').innerHTML, '');
  assert.match(feed.children[0].textContent, /18:00:02/, 'UTC timestamps render in Asia/Taipei');
  app.stop();
});

test('task history and cancellation act on the chosen job, never an unrelated active process', async () => {
  const app = await start();
  const older = job({ id: 'old-job', status: 'completed', created_at: '2026-10-25T09:00:00Z', elapsed_seconds: 4, latest_message: '過去的結果', events: [{ id: 1, at: '2026-10-25T09:00:04Z', kind: 'result', text: '已完成舊任務' }] });
  const socket = load(app, snapshot({ jobs: [older, job()] }));
  app.get('job-select').value = older.id;
  app.get('job-select').emit('change');
  assert.equal(app.get('job-status').textContent, '已完成');
  assert.equal(app.get('cancel-task').disabled, true);
  assert.match(app.get('event-feed').textContent, /已完成舊任務/);
  app.get('cancel-task').emit('click');
  assert.equal(socket.sent.length, 0);
  app.get('job-select').value = 'job-1';
  app.get('job-select').emit('change');
  app.get('cancel-task').emit('click');
  assert.deepEqual(socket.sent, [{ type: 'monitor_control', action: 'cancel', job_id: 'job-1' }]);
  assert.equal(app.get('cancel-task').disabled, true);
  socket.receive(snapshot({ revision: 3, jobs: [older, job({ status: 'cancelled', elapsed_seconds: 9, finished_at: '2026-10-25T10:00:10Z' })] }));
  assert.equal(app.get('job-status').textContent, '已停止');
  assert.equal(app.get('cancel-task').disabled, true);
  app.stop();
});

test('elapsed time freezes while disconnected, reconnect requires a new snapshot, latest state wins', async () => {
  const app = await start();
  const first = load(app, snapshot({ jobs: [job()] }));
  app.advance(2000); app.pulse();
  assert.equal(app.get('codex-elapsed').textContent, '00:09');
  first.close();
  assert.equal(app.get('cancel-task').disabled, true);
  assert.equal(app.get('refresh-providers').disabled, true);
  assert.equal(app.get('codex-card').attributes['data-connected'], 'false');
  app.advance(8000); app.pulse();
  assert.equal(app.get('codex-elapsed').textContent, '00:09');
  const retry = app.timers.values().next().value;
  assert.equal(retry.delay, 700);
  retry.callback();
  app.pulse();
  assert.equal(app.get('codex-elapsed').textContent, '00:09', 'connecting does not advance a stale clock');
  const second = app.sockets[1]; second.open();
  assert.equal(app.get('cancel-task').disabled, true);
  second.receive(snapshot({ revision: 4, jobs: [job({ status: 'completed', elapsed_seconds: 12, events: [{ id: 8, at: '2026-10-25T10:00:12Z', kind: 'result', text: '重連後的結果' }] })] }));
  assert.equal(app.get('job-status').textContent, '已完成');
  assert.equal(app.get('job-duration').textContent, '00:12');
  assert.equal(app.get('event-feed').children.length, 1);
  assert.match(app.get('event-feed').textContent, /重連後的結果/);
  first.receive(snapshot({ revision: 0, jobs: [job()] }));
  assert.equal(app.get('job-status').textContent, '已完成', 'old socket cannot overwrite current state');
  app.stop();
});

test('server errors recover pending controls and unknown or malformed frames do not alter state', async () => {
  const app = await start();
  const socket = load(app);
  submit(app);
  socket.receive({ type: 'monitor_error', message: '<b>需要登入</b>' });
  assert.equal(app.get('error-banner').hidden, false);
  assert.equal(app.get('error-message').textContent, '<b>需要登入</b>');
  assert.equal(app.get('start-task').disabled, false, 'a failed request can be corrected and retried');
  socket.receive('{broken JSON');
  socket.receive({ type: 'private_reasoning', text: 'never displayed' });
  socket.receive(snapshot({ version: 99, workspace: 'wrong' }));
  assert.equal(app.get('workspace-path').textContent, 'C:\\work\\island');
  app.get('refresh-providers').emit('click');
  assert.deepEqual(socket.sent.at(-1), { type: 'monitor_control', action: 'refresh' });
  app.get('dismiss-error').emit('click');
  assert.equal(app.get('error-banner').hidden, true);
  app.stop();
});

test('terminal usage and errors use actual reported values and unload clears transport resources', async () => {
  const app = await start();
  const socket = load(app, snapshot({ jobs: [job({ status: 'failed', elapsed_seconds: 63, error: 'CLI 登入已失效', usage: { input_tokens: 10, output_tokens: 3, cost_usd: 0.0005 }, events: [{ id: 1, at: '2026-10-25T10:01:00Z', kind: 'error', text: 'CLI 登入已失效' }] })] }));
  assert.equal(app.get('job-duration').textContent, '01:03');
  assert.equal(app.get('job-error').hidden, false);
  assert.equal(app.get('job-error').textContent, 'CLI 登入已失效');
  assert.equal(app.get('usage-summary').textContent, '輸入 10 tokens · 輸出 3 tokens · CLI 回報 US$0.0005');
  assert.equal(app.intervals.size, 1);
  app.stop();
  assert.equal(socket.readyState, 3);
  assert.equal(app.timers.size, 0);
  assert.equal(app.intervals.size, 0);
});

test('an authoritative cancellation request remains pending after a reconnect', async () => {
  const app = await start();
  const socket = load(app, snapshot({ jobs: [job({ cancel_requested: true })] }));
  assert.equal(app.get('codex-status').textContent, '停止中');
  assert.equal(app.get('job-status').textContent, '停止中');
  assert.equal(app.get('cancel-task').disabled, true);
  assert.equal(app.get('cancel-task').textContent, '停止中…');
  app.get('cancel-task').emit('click');
  assert.equal(socket.sent.length, 0);
  socket.receive(snapshot({ jobs: [job({ status: 'cancelled', cancel_requested: true })] }));
  assert.equal(app.get('job-status').textContent, '已停止');
  app.stop();
});

function allAvailable() {
  return providers().map(item => ({ ...item, available: true, phase: 'idle', reason: '', auth: 'verified' }));
}

test('individual offices receive real state, freeze on disconnect, and share selection with cards', async () => {
  const app = await start();
  assert.equal(app.offices.length, 1);
  const office = app.offices[0];
  assert.equal(office.container.id, 'office-scene');
  const running = job({ provider: 'claude', transport: 'antigravity', configured_model: 'claude-sonnet-4-6', model: 'claude-sonnet-4-6' });
  const socket = load(app, snapshot({ providers: allAvailable(), jobs: [running] }));
  assert.equal(office.states.at(-1).view_connected, true);
  assert.equal(office.states.at(-1).jobs[0].provider, 'claude');
  assert.equal(app.get('office-loading').hidden, true);
  assert.match(app.get('office-summary').textContent, /1 間辦公室/);
  office.pick('claude');
  assert.equal(app.get('provider-select').value, 'claude');
  assert.equal(app.get('claude-card')._classes.has('selected'), true);
  assert.equal(office.selections.at(-1), 'claude');
  assert.equal(app.get('office-selected-label').textContent, 'Claude 4.6 辦公室');
  app.get('codex-select').emit('click');
  assert.equal(office.selections.at(-1), 'codex');
  app.get('office-reset-camera').emit('click');
  assert.equal(office.resets, 1);
  socket.close();
  assert.equal(office.states.at(-1).view_connected, false);
  assert.match(app.get('office-summary').textContent, /保留最後/);
  app.stop();
  assert.equal(office.disposed, true);
});

test('failed 3D loading leaves the real monitor and task controls usable', async () => {
  const app = await start({ sceneFailure: true });
  const socket = load(app);
  assert.equal(app.offices.length, 0);
  assert.equal(app.get('office-loading').hidden, false);
  assert.equal(app.get('office-loading')._classes.has('error'), true);
  assert.match(app.get('office-loading-message').textContent, /仍可使用下方卡片/);
  assert.equal(app.get('office-reset-camera').disabled, true);
  assert.equal(app.get('office-retry').hidden, false);
  assert.equal(app.get('codex-status').textContent, '待命中');
  submit(app, '只檢查連線');
  assert.deepEqual(socket.sent.at(-1), { type: 'monitor_control', action: 'start', provider: 'codex', prompt: '只檢查連線' });
  app.get('office-retry').emit('click');
  assert.equal(app.reloads, 1);
  app.stop();
});

test('Claude 4.6 uses Antigravity and sends only a server-offered selected model', async () => {
  const app = await start();
  const socket = load(app, snapshot({ providers: allAvailable() }));
  app.get('claude-select').emit('click');
  assert.equal(app.get('claude-name').textContent, 'Claude 4.6');
  assert.equal(app.get('claude-transport').textContent, 'Antigravity CLI');
  assert.equal(app.get('claude-mode').textContent, '隔離副本');
  assert.equal(app.get('model-field').hidden, false);
  assert.equal(app.get('model-select').value, 'claude-sonnet-4-6');
  assert.deepEqual(app.get('model-select').children.map(option => option.value), ['claude-sonnet-4-6', 'claude-opus-4-6-thinking']);
  app.get('model-select').value = 'claude-opus-4-6-thinking';
  app.get('model-select').emit('change');
  assert.equal(app.get('office-selected-model').textContent, 'Claude Opus 4.6 Thinking');
  submit(app, '摘要測試策略');
  assert.deepEqual(socket.sent.at(-1), {
    type: 'monitor_control', action: 'start', provider: 'claude', prompt: '摘要測試策略', model: 'claude-opus-4-6-thinking',
  });
  assert.equal(app.get('model-select').disabled, true, 'pending requests keep their chosen model');
  socket.receive(snapshot({ revision: 2, providers: allAvailable(), jobs: [job({
    provider: 'claude', transport: 'antigravity', configured_model: 'claude-opus-4-6-thinking', model: 'claude-opus-4-6-thinking',
  })] }));
  assert.match(app.get('job-model').textContent, /claude-opus-4-6-thinking/);
  assert.equal(app.get('job-transport').textContent, '通道 Antigravity CLI');
  assert(!html.includes('claude auth login'), 'this office does not ask for standalone Claude Code login');
  app.stop();
});

test('valid per-office model choices survive switching and reconnecting, stale options are replaced', async () => {
  const app = await start();
  const first = load(app, snapshot({ providers: allAvailable() }));
  app.get('claude-select').emit('click');
  app.get('model-select').value = 'claude-opus-4-6-thinking';
  app.get('model-select').emit('change');
  app.get('antigravity-select').emit('click');
  assert.equal(app.get('model-select').value, 'gemini-3.8-flash-high');
  app.get('model-select').value = 'gemini-3.8-flash-medium';
  app.get('model-select').emit('change');
  app.get('claude-select').emit('click');
  assert.equal(app.get('model-select').value, 'claude-opus-4-6-thinking');
  first.close();
  assert.equal(app.get('model-select').disabled, true);
  app.timers.values().next().value.callback();
  const second = app.sockets.at(-1);
  second.open();
  second.receive(snapshot({ revision: 3, providers: allAvailable() }));
  assert.equal(app.get('model-select').value, 'claude-opus-4-6-thinking');
  app.get('antigravity-select').emit('click');
  assert.equal(app.get('model-select').value, 'gemini-3.8-flash-medium');
  app.get('claude-select').emit('click');
  const changed = allAvailable();
  changed[1].model_options = changed[1].model_options.slice(0, 1);
  second.receive(snapshot({ revision: 4, providers: changed }));
  assert.equal(app.get('model-select').value, 'claude-sonnet-4-6');
  assert.deepEqual(app.get('model-select').children.map(option => option.value), ['claude-sonnet-4-6']);
  app.stop();
});

test('unsupported model input is rejected and Codex sends its server-offered option', async () => {
  const app = await start();
  const items = allAvailable(); items[0].configured_model = 'the-existing-codex-selection';
  items[0].model_options = [{ id: 'the-existing-codex-selection', name: 'the-existing-codex-selection' }];
  const socket = load(app, snapshot({ providers: items }));
  assert.equal(app.get('model-field').hidden, false);
  assert.deepEqual(app.get('model-select').children.map(option => option.value), ['the-existing-codex-selection']);
  assert.equal(app.get('codex-model').textContent, 'the-existing-codex-selection');
  app.get('claude-select').emit('click');
  app.get('model-select').value = 'invented-model-not-offered';
  submit(app);
  assert.equal(socket.sent.length, 0);
  assert.match(app.get('error-message').textContent, /不在可用選項/);
  assert.equal(app.get('model-select').value, 'claude-sonnet-4-6');
  app.get('codex-select').emit('click');
  submit(app, '保留原有模型設定');
  assert.deepEqual(socket.sent.at(-1), { type: 'monitor_control', action: 'start', provider: 'codex', prompt: '保留原有模型設定', model: 'the-existing-codex-selection' });
  app.stop();
});

function codexCatalog() {
  const items = allAvailable();
  Object.assign(items[0], {
    configured_model: 'codex-main', configured_reasoning_effort: 'high',
    model_options: [
      { id: 'codex-main', name: 'Codex Main', reasoning_efforts: ['low', 'medium', 'high'], default_reasoning_effort: 'medium' },
      { id: 'codex-light', name: 'Codex Light', reasoning_efforts: ['low', 'medium'], default_reasoning_effort: 'low' },
      { id: 'codex-deep', name: 'Codex Deep', reasoning_efforts: ['high', 'xhigh'], default_reasoning_effort: 'xhigh' },
    ],
  });
  return items;
}

function selectModel(app, id) {
  app.get('model-select').value = id;
  app.get('model-select').emit('change');
}

function selectEffort(app, value) {
  app.get('reasoning-effort-select').value = value;
  app.get('reasoning-effort-select').emit('change');
}

test('Codex accepts any account-offered model and sends its supported selected effort', async () => {
  const app = await start();
  const items = codexCatalog();
  const socket = load(app, snapshot({ providers: items }));
  assert.deepEqual(app.get('model-select').children.map(option => option.value), ['codex-main', 'codex-light', 'codex-deep']);
  assert.equal(app.get('reasoning-effort-field').hidden, false);
  assert.equal(app.get('reasoning-effort-select').value, 'high', 'configured effort overrides model default when supported');
  selectModel(app, 'codex-light');
  assert.equal(app.get('reasoning-effort-select').value, 'low', 'unsupported configured effort falls back to selected model default');
  assert.deepEqual(app.get('reasoning-effort-select').children.map(option => option.value), ['low', 'medium']);
  selectEffort(app, 'medium');
  assert.match(app.get('office-selected-model').textContent, /Codex Light.*medium/);
  assert.equal(socket.sent.length, 0, 'choosing a model or effort does not invoke the model');
  submit(app, '使用選定的模型分析');
  assert.deepEqual(socket.sent.at(-1), {
    type: 'monitor_control', action: 'start', provider: 'codex', prompt: '使用選定的模型分析',
    model: 'codex-light', reasoning_effort: 'medium',
  });
  assert.equal(app.get('reasoning-effort-select').disabled, true, 'pending job keeps its chosen effort');
  socket.receive(snapshot({ revision: 2, providers: items, jobs: [job({
    configured_model: 'codex-light', model: 'codex-light', reasoning_effort: 'medium',
  })] }));
  assert.equal(app.get('reasoning-effort-select').disabled, true, 'active job cannot change its frozen parameters');
  assert.equal(app.get('job-reasoning-effort').hidden, false);
  assert.match(app.get('job-reasoning-effort').textContent, /medium/);
  selectModel(app, 'codex-deep');
  assert.match(app.get('job-model').textContent, /codex-light/, 'form choice never rewrites actual job metadata');
  assert.match(app.get('job-reasoning-effort').textContent, /medium/);
  app.stop();
});

test('Codex remembers effort per model through office changes and reconnect, replacing obsolete choices', async () => {
  const app = await start();
  const first = load(app, snapshot({ providers: codexCatalog() }));
  selectEffort(app, 'medium');
  selectModel(app, 'codex-light');
  selectEffort(app, 'low');
  selectModel(app, 'codex-deep');
  assert.equal(app.get('reasoning-effort-select').value, 'high');
  selectEffort(app, 'xhigh');
  selectModel(app, 'codex-main');
  assert.equal(app.get('reasoning-effort-select').value, 'medium');
  app.get('claude-select').emit('click');
  assert.equal(app.get('reasoning-effort-field').hidden, true);
  assert.equal(app.get('reasoning-effort-select').disabled, true);
  app.get('codex-select').emit('click');
  assert.equal(app.get('reasoning-effort-select').value, 'medium');
  first.close();
  assert.equal(app.get('reasoning-effort-select').disabled, true);
  app.timers.values().next().value.callback();
  const second = app.sockets.at(-1);
  second.open();
  assert.equal(app.get('reasoning-effort-select').disabled, true, 'reconnect waits for authoritative catalog');
  second.receive(snapshot({ revision: 3, providers: codexCatalog() }));
  assert.equal(app.get('reasoning-effort-select').value, 'medium');
  assert.equal(app.get('reasoning-effort-select').disabled, false);
  selectModel(app, 'codex-light');
  assert.equal(app.get('reasoning-effort-select').value, 'low');
  selectModel(app, 'codex-deep');
  assert.equal(app.get('reasoning-effort-select').value, 'xhigh');
  const changed = codexCatalog();
  changed[0].model_options[2].reasoning_efforts = ['high'];
  second.receive(snapshot({ revision: 4, providers: changed }));
  assert.equal(app.get('reasoning-effort-select').value, 'high', 'removed effort cannot survive catalog refresh');
  assert.deepEqual(app.get('reasoning-effort-select').children.map(option => option.value), ['high']);
  app.stop();
});

test('unsupported effort is rejected and Antigravity offices never send Codex effort controls', async () => {
  const app = await start();
  const items = codexCatalog();
  const socket = load(app, snapshot({ providers: items }));
  app.get('reasoning-effort-select').value = 'invented-effort';
  submit(app);
  assert.equal(socket.sent.length, 0);
  assert.match(app.get('error-message').textContent, /思考深度.*不在/);
  assert.equal(app.get('reasoning-effort-select').value, 'high');
  selectEffort(app, 'not-offered');
  assert.equal(app.get('reasoning-effort-select').value, 'high', 'invalid change events also restore a supported choice');
  app.get('claude-select').emit('click');
  assert.equal(app.get('reasoning-effort-field').hidden, true);
  assert.equal(app.get('reasoning-effort-select').disabled, true);
  app.get('reasoning-effort-select').value = 'xhigh';
  submit(app, 'Claude 自有模型變體');
  assert.equal(Object.hasOwn(socket.sent.at(-1), 'reasoning_effort'), false);
  assert.equal(socket.sent.at(-1).provider, 'claude');
  socket.receive({ type: 'monitor_error', message: '測試重設待送狀態' });
  app.get('antigravity-select').emit('click');
  assert.equal(app.get('reasoning-effort-field').hidden, true);
  submit(app, 'Gemini 自有模型變體');
  assert.equal(Object.hasOwn(socket.sent.at(-1), 'reasoning_effort'), false);
  assert.equal(socket.sent.at(-1).model, 'gemini-3.8-flash-high');
  socket.receive(snapshot({ revision: 3, providers: items, jobs: [job({ provider: 'antigravity', reasoning_effort: 'high' })] }));
  assert.equal(app.get('job-reasoning-effort').hidden, true);
  app.stop();
});

test('models without advertised efforts omit the control and selected job effort remains literal text', async () => {
  const app = await start();
  const socket = load(app);
  assert.equal(app.get('reasoning-effort-field').hidden, true);
  assert.equal(app.get('reasoning-effort-select').disabled, true);
  submit(app);
  assert.equal(Object.hasOwn(socket.sent.at(-1), 'reasoning_effort'), false);
  socket.receive(snapshot({ revision: 2, jobs: [job({ reasoning_effort: '<b>recorded-value</b>' })] }));
  assert.equal(app.get('job-reasoning-effort').textContent, '思考深度 <b>recorded-value</b>');
  assert.equal(app.get('job-reasoning-effort').innerHTML, '');
  app.stop();
});

function enableTeam(app) {
  app.get('task-mode').value = 'team';
  app.get('task-mode').emit('change');
}

function teamJob(overrides = {}) {
  return job({
    id: 'team-1', kind: 'collaboration', provider: 'antigravity',
    model: 'gemini-3.8-flash-high', configured_model: 'gemini-3.8-flash-high',
    collaboration: { members: [{ provider: 'antigravity' }, { provider: 'codex' }, { provider: 'claude' }], phase: 'working', round: 2, max_rounds: 4 },
    ...overrides,
  });
}

function artifact(parentId = 'team-1', overrides = {}) {
  return {
    id: parentId, changed_files: [{ path: 'src/example.py', status: 'modified', additions: 3, deletions: 1 }],
    diff_url: `/api/monitor/jobs/${parentId}/changes`, download_url: `/api/monitor/jobs/${parentId}/patch`,
    applied: false, ...overrides,
  };
}

test('team mode delegates to selected peers with their remembered models and Codex effort', async () => {
  const app = await start();
  const socket = load(app, snapshot({ providers: codexCatalog() }));
  assert.equal(app.get('team-members').hidden, true);
  selectModel(app, 'codex-light');
  selectEffort(app, 'medium');
  app.get('antigravity-select').emit('click');
  enableTeam(app);
  assert.equal(app.get('team-members').hidden, false);
  assert.equal(app.get('team-row-antigravity').hidden, true);
  assert.equal(app.get('team-member-codex').checked, true);
  assert.equal(app.get('team-member-claude').checked, true);
  assert.match(app.get('team-model-codex').textContent, /Codex Light.*medium/);
  assert.match(app.get('team-lead-note').textContent, /Antigravity/);
  assert.equal(socket.sent.length, 0);
  submit(app, '請分工分析並提出可審查修改');
  assert.deepEqual(socket.sent.at(-1), {
    type: 'monitor_control', action: 'start', provider: 'antigravity', prompt: '請分工分析並提出可審查修改',
    model: 'gemini-3.8-flash-high', collaboration: { members: [
      { provider: 'codex', model: 'codex-light', reasoning_effort: 'medium' },
      { provider: 'claude', model: 'claude-sonnet-4-6' },
    ] },
  });
  assert.equal(app.get('task-mode').disabled, true);
  assert.equal(app.get('team-member-codex').disabled, true);
  app.stop();
});

test('team members default to available peers and unavailable, busy, or empty teams cannot start', async () => {
  const app = await start();
  const socket = load(app);
  enableTeam(app);
  submit(app);
  assert.equal(app.get('start-task').disabled, true);
  assert.equal(socket.sent.length, 0);
  assert.equal(app.get('team-member-claude').disabled, true);
  socket.receive(snapshot({ revision: 2, providers: codexCatalog() }));
  assert.equal(app.get('team-member-claude').checked, true);
  assert.equal(app.get('team-member-antigravity').checked, true);
  for (const id of ['claude', 'antigravity']) {
    app.get('team-member-' + id).checked = false;
    app.get('team-member-' + id).emit('change');
  }
  assert.equal(app.get('start-task').disabled, true);
  assert.match(app.get('team-validation').textContent, /至少/);
  app.get('team-member-antigravity').checked = true;
  app.get('team-member-antigravity').emit('change');
  const items = codexCatalog();
  items[1].active_job_id = 'busy-claude';
  socket.receive(snapshot({ revision: 3, providers: items, jobs: [job({ id: 'busy-claude', provider: 'claude' })] }));
  assert.equal(app.get('team-member-claude').disabled, true);
  app.get('team-member-claude').checked = true; // Tampered DOM cannot bypass authoritative availability.
  app.get('task-form').emit('submit', { preventDefault() {} });
  assert.equal(socket.sent.length, 0);
  assert.match(app.get('error-message').textContent, /空閒/);
  app.get('task-mode').value = 'solo';
  app.get('task-mode').emit('change');
  submit(app, '單人分析');
  assert.equal(Object.hasOwn(socket.sent.at(-1), 'collaboration'), false);
  app.stop();
});

test('parent and child navigation shows real handoffs without portraying reserved offices as working', async () => {
  const app = await start();
  const parent = teamJob();
  Object.assign(parent.collaboration.members[1], { model: 'codex-light', reasoning_effort: 'medium' });
  const planner = job({ id: 'plan-1', kind: 'delegation', parent_job_id: parent.id, provider: 'antigravity', role: 'planning', task_mode: 'read', status: 'completed', events: [{ id: 2, kind: 'result', text: '已決定交給 Codex', at: '2026-10-25T10:00:03Z' }] });
  const worker = job({ id: 'worker-1', kind: 'delegation', parent_job_id: parent.id, role: 'worker', task_mode: 'write', created_at: '2026-10-25T10:00:04Z', events: [{ id: 3, kind: 'message', text: 'Codex 正在隔離副本修改', at: '2026-10-25T10:00:05Z' }] });
  const items = codexCatalog();
  items.forEach(item => { item.team_run_id = parent.id; });
  items[0].active_job_id = worker.id;
  items[2].active_job_id = parent.id;
  const socket = load(app, snapshot({ providers: items, jobs: [parent, planner, worker] }));
  assert.equal(app.get('overview-running').textContent, '1', 'orchestrator parents do not count as another live CLI');
  assert.equal(app.get('codex-card').attributes['data-phase'], 'running');
  assert.equal(app.get('claude-card').attributes['data-phase'], 'reserved');
  assert.equal(app.get('antigravity-card').attributes['data-phase'], 'reserved');
  assert.match(app.get('claude-idle-label').textContent, /未呼叫 CLI/);
  assert.equal(app.get('start-task').disabled, true, 'a reserved provider cannot start unrelated work');
  app.get('antigravity-select').emit('click');
  assert.equal(app.get('team-progress').hidden, false);
  assert.equal(app.get('team-progress-phase').textContent, '夥伴執行');
  assert.match(app.get('team-progress-budget').textContent, /2 \/ 4/);
  assert.match(app.get('team-progress-members').textContent, /Codex Light.*medium/, 'team membership shows frozen server choices');
  assert.equal(app.get('team-job-links').children.length, 3);
  assert.match(app.get('team-job-links').children[2].textContent, /隔離副本修改/);
  socket.receive(snapshot({ revision: 2, providers: items, jobs: [parent, planner, { ...worker, write_strategy: 'proposals', execution_mode: 'read_only' }] }));
  assert.match(app.get('team-job-links').children[2].textContent, /實作提案.*變更套用至副本/);
  app.get('team-job-links').children[2].emit('click');
  assert.equal(app.get('provider-select').value, 'codex');
  assert.equal(app.get('job-role').textContent, '實作提案');
  assert.match(app.get('event-feed').textContent, /Codex 正在隔離副本修改/);
  assert.equal(app.get('cancel-task').textContent, '停止整個團隊');
  app.get('cancel-task').emit('click');
  assert.deepEqual(socket.sent.at(-1), { type: 'monitor_control', action: 'cancel', job_id: parent.id });
  assert.equal(app.get('cancel-task').disabled, true);
  app.get('team-job-links').children[1].emit('click');
  assert.equal(app.get('job-status').textContent, '已完成');
  assert.equal(app.get('cancel-task').disabled, true, 'pending team cancellation remains pending on any child');
  app.get('team-job-links').children[0].emit('click');
  assert.equal(app.get('job-role').textContent, '團隊總任務');
  socket.receive(snapshot({ revision: 3, providers: items, jobs: [parent, planner, { ...worker, status: 'completed' }] }));
  assert.equal(app.get('overview-running').textContent, '0');
  assert.match(app.get('office-summary').textContent, /團隊正在交接/);
  app.stop();
});

test('reviewing the current completed team diff is required before explicit safe apply', async () => {
  const app = await start();
  const changes = artifact('team-1', { changed_files: [{ path: '<img src=x>', status: 'modified', additions: 1, deletions: 0 }] });
  const parent = teamJob({ status: 'completed', artifact: changes });
  load(app, snapshot({ providers: codexCatalog(), jobs: [parent] }));
  app.get('antigravity-select').emit('click');
  assert.equal(app.get('artifact-panel').hidden, false);
  assert.equal(app.get('artifact-apply').disabled, true);
  app.get('artifact-apply').emit('click');
  assert.equal(app.requests.length, 0);
  assert.match(app.get('artifact-files').textContent, /<img src=x>/);
  assert.equal(app.get('artifact-download').href, '/api/monitor/jobs/team-1/patch');
  const diff = '--- a/example.py\n+++ b/example.py\n+<script>literal diff</script>';
  app.respond({ body: { artifact: changes, diff } });
  app.get('artifact-review').emit('click');
  await app.flush();
  assert.equal(app.get('artifact-diff').textContent, diff);
  assert.equal(app.get('artifact-diff').innerHTML, '');
  assert.equal(app.get('artifact-apply').disabled, false);
  app.respond({ body: { type: 'monitor_artifact', artifact: { ...changes, applied: true } } });
  app.get('artifact-apply').emit('click');
  assert.equal(app.get('artifact-apply').disabled, true);
  await app.flush();
  assert.deepEqual(app.requests[1], {
    url: '/api/monitor/jobs/team-1/apply',
    options: { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: '{}' },
  });
  assert.equal(app.get('artifact-status').textContent, '已套用');
  assert.match(app.get('artifact-note').textContent, /已套用/);
  app.get('artifact-apply').emit('click');
  assert.equal(app.requests.length, 2, 'applied changes are never sent twice');
  app.stop();
});

test('stale or mismatched artifact reviews cannot enable applying another team result', async () => {
  const app = await start();
  const first = teamJob({ status: 'completed', artifact: artifact() });
  const socket = load(app, snapshot({ providers: codexCatalog(), jobs: [first] }));
  app.get('antigravity-select').emit('click');
  let finishReview;
  app.respond(() => new Promise(resolve => { finishReview = resolve; }));
  app.get('artifact-review').emit('click');
  const next = teamJob({ id: 'team-2', status: 'completed', created_at: '2026-10-25T11:00:00Z', artifact: artifact('team-2') });
  socket.receive(snapshot({ revision: 2, providers: codexCatalog(), jobs: [first, next] }));
  finishReview({ body: { artifact: first.artifact, diff: 'old team diff' } });
  await app.flush();
  assert.equal(app.get('artifact-apply').disabled, true);
  assert.equal(app.get('artifact-diff').hidden, true);
  app.respond({ body: { artifact: first.artifact, diff: 'wrong artifact identity' } });
  app.get('artifact-review').emit('click');
  await app.flush();
  assert.equal(app.get('artifact-apply').disabled, true);
  assert.match(app.get('artifact-note').textContent, /不一致/);
  app.respond({ body: { artifact: { ...next.artifact, applied: true }, diff: 'already applied on the server' } });
  app.get('artifact-review').emit('click');
  await app.flush();
  assert.equal(app.get('artifact-status').textContent, '已套用', 'review metadata supersedes an older unapplied snapshot');
  assert.equal(app.get('artifact-apply').disabled, true);
  app.stop();
});

test('apply conflicts remain visible and failed or running teams never allow apply', async () => {
  const app = await start();
  const parent = teamJob({ status: 'completed', artifact: artifact() });
  const socket = load(app, snapshot({ providers: codexCatalog(), jobs: [parent] }));
  app.get('antigravity-select').emit('click');
  app.respond({ body: { artifact: parent.artifact, diff: '+ change' } });
  app.get('artifact-review').emit('click');
  await app.flush();
  app.respond({ status: 409, body: { type: 'monitor_error', message: '原檔案已修改，未覆寫任何檔案。' } });
  app.get('artifact-apply').emit('click');
  await app.flush();
  assert.equal(app.get('artifact-status').textContent, '1 個檔案待審查');
  assert.match(app.get('artifact-note').textContent, /未覆寫任何檔案/);
  for (const status of ['running', 'failed']) {
    socket.receive(snapshot({ revision: 3, providers: codexCatalog(), jobs: [{ ...parent, status }] }));
    assert.equal(app.get('artifact-review').disabled, status === 'running');
    assert.equal(app.get('artifact-apply').disabled, true);
    app.get('artifact-apply').emit('click');
  }
  assert.equal(app.requests.length, 2);
  socket.receive(snapshot({ revision: 4, providers: codexCatalog(), jobs: [{ ...parent, artifact: artifact('team-1', { diff_url: 'https://outside.invalid/diff', download_url: '//outside.invalid/patch' }) }] }));
  assert.equal(app.get('artifact-review').disabled, true);
  assert.equal(app.get('artifact-download').hidden, true);
  app.stop();
});

test('office import map shares one pinned Three and OrbitControls release', () => {
  const match = html.match(/<script type="importmap">([\s\S]*?)<\/script>/);
  assert(match);
  const imports = JSON.parse(match[1]).imports;
  assert.equal(imports.three, 'https://cdn.jsdelivr.net/npm/three@0.180.0/build/three.module.js');
  assert.equal(imports['three/addons/'], 'https://cdn.jsdelivr.net/npm/three@0.180.0/examples/jsm/');
});
