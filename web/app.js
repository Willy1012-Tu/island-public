/* The transport and accessible HUD stay independent of the Three.js renderer. */
const $ = (id) => document.getElementById(id);
const controls = [$('pause-button'), $('boredom-button')];
let socket = null;
let scene = null;
let latestState = null;
let reconnectDelay = 700;
let retryTimer = null;
let stopped = false;

const names = { move: '前往下一個目的地', gather: '收集島上材料', chop_tree: '採集木材', mine_rock: '開採石材', rest: '休息，恢復體力' };

function humanStatus(state) {
  if (state.paused) return '時間暫停，讓小島歇一會兒';
  if (state.thinking) return state.brain === 'codex' ? '正在醞釀下一個念頭…' : '正在觀察附近的世界…';
  if (state.current_action) return names[state.current_action.name] || '探索小島中';
  if (state.status?.startsWith('Game completed:')) return '小遊戲完成，心情輕快了一些';
  if (state.status?.startsWith('Thought failed;')) return '思考暫時中斷，稍後會再試一次';
  if (state.status?.startsWith('Goal failed:')) return '換一條路，再想個新點子';
  return '完成這一步，期待下一個發現';
}

function setConnection(status) {
  const connected = status === 'connected';
  $('connection').className = `connection ${status}`;
  $('connection-text').textContent = connected ? '即時連線' : status === 'connecting' ? '連線中' : '重新連線中';
  controls.forEach((button) => { button.disabled = !connected; });
  const banner = $('connection-banner');
  banner.hidden = status !== 'disconnected';
  if (!banner.hidden) banner.textContent = '與小島的連線暫時中斷，正在重新連線。';
}

function setMeter(prefix, value) {
  const amount = Math.min(100, Math.max(0, Number(value) || 0));
  // Values come from Python; model text is never inserted as HTML.
  const output = $(prefix + '-value');
  output.replaceChildren(document.createTextNode(String(amount)));
  const suffix = document.createElement('small');
  suffix.textContent = ' / 100';
  output.append(suffix);
  $(prefix + '-fill').style.width = `${amount}%`;
  $(prefix + '-meter').setAttribute('aria-valuenow', String(amount));
}

function applyState(state) {
  if (!state || state.type !== 'state' || !state.agent || !state.world) return;
  latestState = state;
  $('brain-mode').textContent = state.brain === 'codex' ? 'CODEX · AI' : 'DEMO · 離線';
  $('thought').textContent = state.internal_thought || '看看這座小島，下一步要做什麼呢？';
  $('thought-state').textContent = state.paused ? '暫停中' : state.thinking ? '思考中' : state.mini_game ? '靈感發生中' : '探索中';
  $('activity-dot').classList.toggle('thinking', !state.paused && state.thinking);
  $('action-label').textContent = humanStatus(state);
  const target = state.current_action?.target;
  $('action-target').textContent = target ? `${target.x}, ${target.y}` : '';
  $('action-label').title = state.status || '';
  setMeter('boredom', state.agent.boredom);
  setMeter('energy', state.agent.energy);
  const bored = state.agent.boredom > 70;
  $('boredom-block').classList.toggle('elevated', bored);
  $('boredom-caption').textContent = bored ? '有點無聊了，該發明一個小遊戲！' : '超過 70，讓想像力接手。';
  $('wood-value').textContent = state.agent.inventory?.wood || 0;
  $('stone-value').textContent = state.agent.inventory?.stone || 0;
  $('mini-game').hidden = !state.mini_game;
  $('game-name').textContent = state.mini_game?.name || '';
  $('game-rules').textContent = state.mini_game?.rules || '';
  $('tick-value').textContent = String(state.tick).padStart(4, '0');
  $('plans-value').textContent = state.counters?.plans || 0;
  $('games-value').textContent = state.counters?.games || 0;
  $('pause-button').setAttribute('aria-pressed', String(state.paused));
  $('pause-label').textContent = state.paused ? '繼續探索' : '暫停模擬';
  $('pause-icon').innerHTML = state.paused ? '<path d="m7 4 9 6-9 6V4Z"/>' : '<path d="M7 5v10m6-10v10"/>';
  $('world-label').textContent = `${state.world.size} × ${state.world.size} 的微型宇宙`;
  $('simulation-note').textContent = state.paused ? '世界暫停，心裡的風景還在。' : `每 ${state.tick_seconds} 秒，一個新的小小行動。`;
  $('footer-status').textContent = `SEED ${state.world.seed} · ${state.paused ? 'PAUSED' : 'LIVE'} · ${state.tick_seconds}s / TICK`;
  if (scene) {
    try {
      scene.applyState(state);
      $('scene-loading').hidden = true;
    } catch (error) {
      showSceneError(error);
    }
  }
}

function connect() {
  if (stopped) return;
  setConnection('connecting');
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  socket = new WebSocket(`${protocol}//${location.host}/ws`);
  socket.addEventListener('open', () => {
    reconnectDelay = 700;
    scene?.beginStream();
    setConnection('connected');
  });
  socket.addEventListener('message', (event) => {
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (data.type === 'state') applyState(data);
    else if (data.type === 'error') {
      $('connection-banner').textContent = data.message || '指令暫時無法執行。';
      $('connection-banner').hidden = false;
    }
  });
  socket.addEventListener('close', () => {
    if (stopped) return;
    setConnection('disconnected');
    clearTimeout(retryTimer);
    retryTimer = setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(10000, reconnectDelay * 1.7);
  });
  socket.addEventListener('error', () => { socket.close(); });
}

function sendControl(action) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'control', action }));
}

function showSceneError(error) {
  console.error('Island scene:', error);
  $('scene-loading').hidden = false;
  $('scene-loading').classList.add('error');
  $('loading-title').textContent = '小島畫面暫時無法載入';
  $('loading-message').textContent = '請確認瀏覽器支援 WebGL，並且能連線至 Three.js CDN，再重新載入。';
  $('retry-scene').hidden = false;
}

$('pause-button').addEventListener('click', () => sendControl('toggle_pause'));
$('boredom-button').addEventListener('click', () => sendControl('boredom'));
$('reset-camera').addEventListener('click', () => scene?.resetCamera());
$('retry-scene').addEventListener('click', () => location.reload());
document.addEventListener('keydown', (event) => {
  if (event.repeat || event.altKey || event.ctrlKey || event.metaKey || event.target.closest('input, textarea, select, [contenteditable=true]')) return;
  if (event.code === 'Space' && !event.target.closest('button')) {
    event.preventDefault();
    sendControl('toggle_pause');
  } else if (event.code === 'KeyB') {
    event.preventDefault();
    sendControl('boredom');
  }
});
window.addEventListener('beforeunload', () => {
  stopped = true;
  clearTimeout(retryTimer);
  socket?.close();
  scene?.dispose();
});

connect();
import('./scene.js').then(({ IslandScene }) => {
  scene = new IslandScene($('scene'));
  // Read-only diagnostics are useful when verifying a real rendered scene.
  window.islandView = scene;
  if (latestState) applyState(latestState);
}).catch(showSceneError);
