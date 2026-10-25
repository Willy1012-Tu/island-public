/* Real CLI activity and explicitly scripted idle personalities share one desk. */
const $ = (id) => document.getElementById(id);
const PROVIDERS = ['codex', 'claude', 'antigravity'];
const NAMES = { codex: 'Codex', claude: 'Claude 4.6', antigravity: 'Antigravity' };
const TRANSPORT_NAMES = { codex: 'Codex CLI', antigravity: 'Antigravity CLI' };
const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'timed_out']);
const ACTIVE = new Set(['queued', 'running']);
const EVENT_NAMES = { status: '狀態', message: '公開訊息', tool: '工具活動', result: '任務結果', error: '錯誤', usage: '用量' };
const STATUS_NAMES = { queued: '排隊中', running: '執行中', completed: '已完成', failed: '執行失敗', cancelled: '已停止', timed_out: '執行逾時' };
const AUTH_NAMES = { verified: '登入已驗證', unknown: '登入未驗證', required: '需要登入' };
const MODE_NAMES = { read_only: '唯讀工作區', isolated: '隔離副本', workspace_write: '可修改工作區' };
const EFFORT_NAMES = { none: '不使用', minimal: '最少', low: '低', medium: '中', high: '高', xhigh: '極高', max: '最大', ultra: '超高' };
const TEAM_PHASES = { planning: '主導者規劃', delegating: '交接任務', working: '夥伴執行', synthesizing: '主導者統整', completed: '協作完成', failed: '協作失敗', cancelled: '協作已停止' };
const ROLE_NAMES = { planning: '規劃', worker: '受託執行', synthesis: '統整' };
let socket = null;
let state = null;
let selectedProvider = 'codex';
let selectedJobId = null;
let providerChosen = false;
let connected = false;
let synchronized = false;
let stopped = false;
let reconnectDelay = 700;
let retryTimer = null;
let snapshotAt = 0;
let frozenAt = 0;
let pendingStart = null;
let pendingCancel = null;
let feedSignature = '';
let lastRenderedJob = null;
let officeScene = null;
let reviewedArtifact = null;
let pendingReview = null;
let pendingApply = null;
let artifactMessage = null;
const selectedModels = new Map();
const selectedReasoningEfforts = new Map();
const selectedTeamMembers = new Map();

function string(value) { return typeof value === 'string' ? value : ''; }
function text(id, value) { $(id).textContent = value == null ? '' : String(value); }
function timestamp(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString('zh-TW', { timeZone: 'Asia/Taipei', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
function duration(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor(value % 3600 / 60);
  const rest = String(value % 60).padStart(2, '0');
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${rest}` : `${String(minutes).padStart(2, '0')}:${rest}`;
}
function elapsed(job) {
  if (!job) return '—';
  const base = Number(job.elapsed_seconds) || 0;
  const now = connected && synchronized ? performance.now() : frozenAt;
  const extra = job.status === 'running' ? Math.max(0, now - snapshotAt) / 1000 : 0;
  return duration(base + extra);
}
function provider(id) { return state?.providers.find(item => item.id === id); }
function providerName(item, id) { return id === 'claude' ? NAMES.claude : string(item?.name) || NAMES[id]; }
function transportName(item, id) {
  return TRANSPORT_NAMES[item?.transport] || TRANSPORT_NAMES[id === 'claude' ? 'antigravity' : id] || 'CLI';
}
function modelOptions(item) {
  const seen = new Set();
  return (Array.isArray(item?.model_options) ? item.model_options : []).filter(option => {
    if (!option || typeof option.id !== 'string' || !option.id || seen.has(option.id)) return false;
    seen.add(option.id);
    return true;
  });
}
function modelName(item, id) { return string(modelOptions(item).find(option => option.id === id)?.name) || string(id) || '由 CLI 選擇'; }
function reasoningOptions(item, modelId = selectedModels.get(selectedProvider) || item?.configured_model) {
  if (item?.id !== 'codex') return [];
  const options = modelOptions(item).find(option => option.id === modelId)?.reasoning_efforts;
  return [...new Set((Array.isArray(options) ? options : []).filter(value => typeof value === 'string' && value))];
}
function effortName(value) { return EFFORT_NAMES[value] ? `${value} · ${EFFORT_NAMES[value]}` : string(value); }
function selectedModelFor(id) {
  const item = provider(id);
  const models = modelOptions(item);
  const previous = selectedModels.get(id);
  return models.some(option => option.id === previous) ? previous
    : models.some(option => option.id === item?.configured_model) ? item.configured_model : '';
}
function selectedEffortFor(id, modelId) {
  const item = provider(id);
  const model = modelOptions(item).find(option => option.id === modelId);
  const efforts = reasoningOptions(item, modelId);
  const previous = selectedReasoningEfforts.get(modelId);
  return efforts.includes(previous) ? previous
    : efforts.includes(item?.configured_reasoning_effort) ? item.configured_reasoning_effort
    : efforts.includes(model?.default_reasoning_effort) ? model.default_reasoning_effort : efforts[0] || '';
}
function jobsFor(id) {
  return (state?.jobs || []).filter(job => job.provider === id).sort((a, b) => {
    const difference = Date.parse(b.created_at) - Date.parse(a.created_at);
    return Number.isNaN(difference) ? 0 : difference;
  });
}
function activeJob(item) {
  if (!item) return null;
  return state.jobs.find(job => job.id === item.active_job_id && ACTIVE.has(job.status))
    || jobsFor(item.id).find(job => ACTIVE.has(job.status)) || null;
}
function workingJob(item) {
  if (!item) return null;
  return state.jobs.find(job => job.id === item.active_job_id && ACTIVE.has(job.status) && job.kind !== 'collaboration')
    || jobsFor(item.id).find(job => ACTIVE.has(job.status) && job.kind !== 'collaboration') || null;
}
function reserved(item) { return Boolean(item?.team_run_id) || activeJob(item)?.kind === 'collaboration'; }
function busyProvider(item) { return Boolean(activeJob(item)) || reserved(item); }
function teamMode() { return $('task-mode').value === 'team'; }
function teamParent(job) {
  return job?.kind === 'collaboration' ? job
    : job?.parent_job_id ? state?.jobs.find(item => item.id === job.parent_job_id && item.kind === 'collaboration') || null : null;
}
function cancellationTarget(job) { return teamParent(job) || job; }
function chosenJob() {
  const jobs = jobsFor(selectedProvider);
  return jobs.find(job => job.id === selectedJobId) || activeJob(provider(selectedProvider)) || jobs[0] || null;
}
function toolName(value) {
  return typeof value === 'string' ? value : string(value?.name) || string(value?.tool);
}
function showError(message) {
  text('error-message', message);
  $('error-banner').hidden = false;
}
function hideError() { $('error-banner').hidden = true; }

function setConnection(next) {
  connected = next === 'connected';
  if (!connected) {
    if (synchronized) frozenAt = performance.now();
    synchronized = false;
  }
  $('monitor-connection').className = `connection ${next}`;
  text('connection-text', connected ? '即時連線' : next === 'connecting' ? '連線中' : '重新連線中');
  if (state) renderCards();
  syncOffice();
  updateControls();
  if (!connected && state) text('feed-note', '連線中斷，畫面保留最後狀態；重新連線後會同步實際任務。');
}

function phaseLabel(item, job) {
  if (!item) return '等待狀態';
  if (!item.available && !job && !reserved(item)) return '未偵測到 CLI';
  const label = job ? job.cancel_requested ? '停止中' : STATUS_NAMES[job.status] || '未知狀態' : reserved(item) ? '協作待交接' : item.phase === 'error' ? '狀態異常' : '待命中';
  return connected && synchronized ? label : `最後：${label}`;
}

function renderCards() {
  PROVIDERS.forEach((id, index) => {
    const item = provider(id);
    const running = workingJob(item);
    const latest = running || jobsFor(id)[0];
    const phase = !item ? 'loading' : running ? running.status : reserved(item) ? 'reserved' : !item.available ? 'missing' : item.phase === 'error' ? 'error' : 'idle';
    const live = connected && synchronized;
    $(id + '-card').setAttribute('data-phase', phase);
    $(id + '-card').setAttribute('data-connected', String(live));
    $(id + '-card').classList.toggle('selected', selectedProvider === id);
    text(id + '-name', providerName(item, id));
    text(id + '-status', phaseLabel(item, running));
    text(id + '-persona', string(item?.persona?.name) || (item ? '準備好再一起出發' : '等待夥伴狀態'));
    $(id + '-persona').title = string(item?.persona?.description);
    const idle = phase === 'idle' && live;
    text(id + '-idle-label', idle ? '閒置演出 · 非 LLM' : phase === 'missing' ? '尚未接入' : !live ? '等待同步' : running ? '真實 CLI 任務' : phase === 'reserved' ? '協作待交接 · 未呼叫 CLI' : phase === 'error' ? '狀態異常' : '等待同步');
    const lines = item?.persona?.idle_lines?.filter(line => typeof line === 'string') || [];
    const idleLine = lines.length ? lines[(Math.floor(Date.now() / 8000) + index) % lines.length] : '整理好小工具，等下一件值得動手的事。';
    text(id + '-idle-line', !item ? '正在確認 CLI 是否可用。' : !item.available && !running && !reserved(item) ? '這位夥伴尚未接入，完成 CLI 安裝與登入後就能加入。' : !live ? '等待重新同步，暫停角色演出。' : idle ? idleLine : phase === 'reserved' ? '已加入團隊，正在等待交接；目前沒有執行 CLI。' : phase === 'error' ? 'CLI 狀態異常，請查看安裝與登入資訊。' : running?.cancel_requested ? '已請求停止，等待 CLI 結束。' : running?.status === 'queued' ? '任務已排入佇列，等待 CLI 啟動。' : '演出暫停，現在專心處理你交辦的事。');
    text(id + '-auth', AUTH_NAMES[item?.auth] || '登入未驗證');
    $(id + '-auth').setAttribute('data-verified', String(item?.auth === 'verified'));
    text(id + '-mode', MODE_NAMES[item?.execution_mode] || '未提供執行模式');
    $(id + '-mode').title = string(item?.execution_note);
    text(id + '-model', modelName(item, running?.model || latest?.model || item?.model || item?.configured_model));
    text(id + '-transport', transportName(item, id));
    const currentTool = toolName(running?.active_tool);
    text(id + '-tool', running?.cancel_requested ? '已要求停止 CLI' : currentTool ? `工具 · ${currentTool}` : running ? STATUS_NAMES[running.status] || '任務處理中' : latest ? `上次任務 · ${STATUS_NAMES[latest.status] || '未知狀態'}` : '尚無任務');
    text(id + '-elapsed', elapsed(latest));
    text(id + '-message', string(latest?.error) || string(latest?.latest_message) || (running ? '等待 CLI 的第一則公開訊息。' : '任務的公開訊息將顯示在這裡。'));
    text(id + '-reason', string(item?.reason) || (!item ? '等待偵測結果。' : item.available ? '已偵測到 CLI；登入狀態以實際驗證結果為準。' : '伺服器目前找不到可用的 CLI 執行檔。'));
    $(id + '-select').setAttribute('aria-pressed', String(selectedProvider === id));
  });
  text('overview-running', state.providers.filter(item => workingJob(item)).length);
  text('overview-available', state.providers.filter(item => item.available).length);
  text('overview-finished', state.jobs.filter(job => job.status === 'completed').length);
}

function renderProviderSelect() {
  const select = $('provider-select');
  const options = PROVIDERS.map(id => {
    const item = provider(id);
    const option = document.createElement('option');
    option.value = id;
    option.textContent = `${providerName(item, id)} · ${!item?.available ? '未偵測到 CLI' : busyProvider(item) ? '任務進行中' : '可交辦'}`;
    // Missing providers stay inspectable via their card; they cannot be started.
    option.disabled = !item?.available;
    return option;
  });
  select.replaceChildren(...options);
  select.value = selectedProvider;
}

function renderModelSelect() {
  const item = provider(selectedProvider);
  const models = modelOptions(item);
  const previous = selectedModels.get(selectedProvider);
  const selected = models.some(option => option.id === previous) ? previous
    : models.some(option => option.id === item?.configured_model) ? item.configured_model : '';
  const options = models.map(model => {
    const option = document.createElement('option');
    option.value = model.id;
    option.textContent = string(model.name) || model.id;
    return option;
  });
  if (!selected) {
    const fallback = document.createElement('option');
    fallback.value = '';
    fallback.textContent = '沿用已設定模型';
    options.unshift(fallback);
  }
  $('model-select').replaceChildren(...options);
  $('model-select').value = selected;
  selectedModels.set(selectedProvider, selected);
  $('model-field').hidden = !models.length;
  text('model-note', `${transportName(item, selectedProvider)} · 實際模型以任務回報為準。`);
  renderReasoningSelect();
  renderOfficeSelection();
}

function renderReasoningSelect() {
  const item = provider(selectedProvider);
  const modelId = selectedModels.get(selectedProvider) || item?.configured_model;
  const efforts = reasoningOptions(item, modelId);
  const selected = selectedEffortFor(selectedProvider, modelId);
  $('reasoning-effort-select').replaceChildren(...efforts.map(value => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = effortName(value);
    return option;
  }));
  $('reasoning-effort-select').value = selected;
  $('reasoning-effort-field').hidden = selectedProvider !== 'codex' || !efforts.length;
  if (modelId && selected) selectedReasoningEfforts.set(modelId, selected);
}

function renderTeamMembers() {
  const ready = connected && synchronized && !pendingStart;
  $('team-members').hidden = !teamMode();
  text('team-lead-note', `主導者：${providerName(provider(selectedProvider), selectedProvider)}。至少選一位夥伴，最多四輪、六件委派工作。`);
  PROVIDERS.forEach(id => {
    const item = provider(id);
    const available = Boolean(item?.available) && !busyProvider(item);
    if (!selectedTeamMembers.has(id) && item?.available) selectedTeamMembers.set(id, true);
    $('team-row-' + id).hidden = id === selectedProvider;
    const checkbox = $('team-member-' + id);
    checkbox.checked = id !== selectedProvider && available && selectedTeamMembers.get(id) === true;
    checkbox.disabled = !ready || id === selectedProvider || !available;
    const modelId = selectedModelFor(id);
    const effort = selectedEffortFor(id, modelId || item?.configured_model);
    text('team-model-' + id, `${modelName(item, modelId || item?.configured_model)}${effort ? ` · 深度 ${effortName(effort)}` : ''}`);
    text('team-status-' + id, !item?.available ? '未接入' : !available ? '忙碌中' : '可加入');
  });
  const count = PROVIDERS.filter(id => id !== selectedProvider && $('team-member-' + id).checked).length;
  text('team-validation', count ? `已選 ${count} 位夥伴；沿用各房間選定的模型設定。` : '請至少選擇一位已接入且空閒的夥伴。');
  return count;
}

function renderOfficeSelection() {
  const item = provider(selectedProvider);
  text('office-selected-label', `${providerName(item, selectedProvider)} 辦公室`);
  const selected = modelName(item, selectedModels.get(selectedProvider) || item?.configured_model);
  const effort = selectedProvider === 'codex' ? $('reasoning-effort-select').value : '';
  text('office-selected-model', `${selected}${effort ? ` · 深度 ${effortName(effort)}` : ''}`);
}

function showOfficeError(error) {
  console.error('Model offices:', error);
  if (officeScene) {
    try { officeScene.dispose(); } catch { /* Keep the task controls usable. */ }
    officeScene = null;
  }
  $('office-loading').hidden = false;
  $('office-loading').classList.add('error');
  text('office-loading-title', '3D 辦公室暫時無法顯示');
  text('office-loading-message', '請確認 WebGL 與 Three.js 連線。你仍可使用下方卡片與表單交辦任務。');
  $('office-retry').hidden = false;
  $('office-reset-camera').disabled = true;
}

function syncOffice() {
  const live = connected && synchronized;
  const running = state?.providers.filter(item => workingJob(item)).length || 0;
  const teamWaiting = state?.jobs.some(job => job.kind === 'collaboration' && ACTIVE.has(job.status));
  text('office-summary', !state ? '三個獨立空間，等待世界同步。' : !live ? '連線暫停，保留最後的工作狀態。' : running ? `${running} 間辦公室正在處理真實任務。` : teamWaiting ? '團隊正在交接或整理結果，等待下一個實際 CLI 工作。' : '每一張桌子，都在等下一個好問題。');
  renderOfficeSelection();
  if (!officeScene || !state) return;
  try {
    officeScene.applyState({ ...state, view_connected: live });
    officeScene.selectProvider(selectedProvider);
    $('office-loading').hidden = true;
  } catch (error) { showOfficeError(error); }
}

function renderJobSelect(job) {
  const jobs = jobsFor(selectedProvider);
  const options = jobs.map(item => {
    const option = document.createElement('option');
    option.value = item.id;
    const prompt = string(item.prompt).replace(/\s+/g, ' ');
    option.textContent = `${timestamp(item.created_at)} · ${STATUS_NAMES[item.status] || '未知狀態'} · ${prompt.slice(0, 38)}${prompt.length > 38 ? '…' : ''}`;
    return option;
  });
  if (!options.length) {
    const empty = document.createElement('option');
    empty.value = '';
    empty.textContent = '尚無任務紀錄';
    options.push(empty);
  }
  $('job-select').replaceChildren(...options);
  $('job-select').disabled = !jobs.length;
  $('job-select').value = job?.id || '';
}

function publicEvents(events) {
  const grouped = [];
  for (const event of Array.isArray(events) ? events : []) {
    if (!EVENT_NAMES[event.kind] || typeof event.text !== 'string') continue;
    const previous = grouped[grouped.length - 1];
    if (event.kind === 'message' && event.delta === true && previous?.kind === 'message' && previous.delta === true) {
      previous.text += event.text;
      previous.lastId = event.id;
    } else grouped.push({ ...event, lastId: event.id });
  }
  return grouped;
}

function renderFeed(job) {
  const events = publicEvents(job?.events);
  const signature = JSON.stringify([job?.id, events.map(event => [event.id, event.lastId, event.kind, event.text, event.status, event.tool])]);
  if (signature === feedSignature) return;
  feedSignature = signature;
  const feed = $('event-feed');
  const follow = lastRenderedJob !== job?.id || feed.scrollHeight - feed.scrollTop - feed.clientHeight < 65;
  lastRenderedJob = job?.id;
  const nodes = events.map(event => {
    const row = document.createElement('article');
    row.className = 'event';
    row.setAttribute('data-kind', event.kind);
    const heading = document.createElement('div');
    heading.className = 'event-heading';
    const label = document.createElement('span');
    label.textContent = EVENT_NAMES[event.kind];
    heading.append(label);
    const tool = toolName(event.tool);
    if (tool || event.status) {
      const tag = document.createElement('span');
      tag.className = 'event-tag';
      tag.textContent = [tool, string(event.status)].filter(Boolean).join(' · ');
      heading.append(tag);
    }
    const at = document.createElement('time');
    at.dateTime = string(event.at);
    at.textContent = timestamp(event.at);
    heading.append(at);
    const body = document.createElement('p');
    body.className = 'event-text';
    body.textContent = event.text;
    row.append(heading, body);
    return row;
  });
  feed.replaceChildren(...nodes);
  if (follow) feed.scrollTop = feed.scrollHeight;
}

function inspectJob(id) {
  const job = state?.jobs.find(item => item.id === id);
  if (!job) return;
  chooseProvider(job.provider);
  selectedJobId = job.id;
  renderJob();
}

function renderTeamProgress(job) {
  const parent = teamParent(job);
  $('team-progress').hidden = !parent;
  if (!parent) return;
  const collaboration = parent.collaboration || {};
  text('team-progress-title', `${providerName(provider(parent.provider), parent.provider)} 主導的團隊`);
  text('team-progress-phase', TEAM_PHASES[collaboration.phase] || STATUS_NAMES[parent.status] || '等待交接狀態');
  text('team-progress-budget', `第 ${Number.isInteger(collaboration.round) ? collaboration.round : 0} / ${Number.isInteger(collaboration.max_rounds) ? collaboration.max_rounds : 4} 輪 · 最多六件委派工作 · 修改先審查後套用`);
  const members = Array.isArray(collaboration.members) ? collaboration.members.filter(member => member && PROVIDERS.includes(member.provider)) : [];
  text('team-progress-members', members.map(member => {
    const item = provider(member.provider);
    const effort = member.provider === 'codex' ? string(member.reasoning_effort) : '';
    return `${providerName(item, member.provider)}（${modelName(item, member.model)}${effort ? ` · ${effortName(effort)}` : ''}）`;
  }).join(' · '));
  const children = state.jobs.filter(item => item.parent_job_id === parent.id).sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
  const links = [parent, ...children].map(item => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'team-job-link';
    button.setAttribute('aria-pressed', String(item.id === job.id));
    const role = item.id === parent.id ? '團隊總任務' : item.write_strategy === 'proposals' ? '實作提案' : ROLE_NAMES[item.role] || '子任務';
    const mode = item.task_mode === 'write' ? item.write_strategy === 'proposals' ? ' · 變更套用至副本' : ' · 隔離副本修改' : item.task_mode === 'read' ? ' · 分析' : '';
    button.textContent = `${role} · ${providerName(provider(item.provider), item.provider)} · ${STATUS_NAMES[item.status] || '未知狀態'}${mode}`;
    button.addEventListener('click', () => inspectJob(item.id));
    return button;
  });
  $('team-job-links').replaceChildren(...links);
}

function artifactContext(job = chosenJob()) {
  const parent = teamParent(job);
  const artifact = parent?.artifact;
  return parent && artifact && typeof artifact.id === 'string' && artifact.id === parent.id
    ? { parent, artifact } : null;
}

function artifactUrl(parent, suffix) { return `/api/monitor/jobs/${encodeURIComponent(parent.id)}/${suffix}`; }

function renderArtifact(job) {
  const context = artifactContext(job);
  $('artifact-panel').hidden = !context;
  if (!context) return;
  const { parent, artifact } = context;
  const changed = Array.isArray(artifact.changed_files) ? artifact.changed_files.filter(item => item && typeof item.path === 'string') : [];
  const reviewed = reviewedArtifact?.jobId === parent.id && reviewedArtifact.artifactId === artifact.id;
  const ready = connected && synchronized;
  const loading = pendingReview === parent.id;
  const applying = pendingApply === parent.id;
  const diffUrl = artifactUrl(parent, 'changes');
  const downloadUrl = artifactUrl(parent, 'patch');
  text('artifact-status', artifact.applied ? '已套用' : applying ? '套用中…' : parent.status !== 'completed' ? ACTIVE.has(parent.status) ? '等待協作完成' : '僅供檢視，無法套用' : `${changed.length} 個檔案待審查`);
  $('artifact-files').replaceChildren(...changed.map(item => {
    const row = document.createElement('li');
    const path = document.createElement('span');
    path.textContent = `${string(item.status)} · ${item.path}`;
    const count = document.createElement('span');
    count.textContent = `+${Number.isFinite(item.additions) ? item.additions : 0} −${Number.isFinite(item.deletions) ? item.deletions : 0}`;
    row.append(path, count);
    return row;
  }));
  $('artifact-review').disabled = !ready || loading || applying || !TERMINAL.has(parent.status) || artifact.diff_url !== diffUrl;
  text('artifact-review', loading ? '讀取差異中…' : reviewed ? '重新檢視差異' : '檢視差異');
  $('artifact-download').hidden = artifact.download_url !== downloadUrl;
  $('artifact-download').href = artifact.download_url === downloadUrl ? downloadUrl : '';
  $('artifact-apply').disabled = !ready || !reviewed || loading || applying || artifact.applied === true || parent.status !== 'completed' || !changed.length || artifact.diff_url !== diffUrl;
  text('artifact-apply', artifact.applied ? '已套用修改' : applying ? '套用中…' : '套用修改');
  $('artifact-diff').hidden = !reviewed;
  text('artifact-diff', reviewed ? reviewedArtifact.diff : '');
  const message = artifactMessage?.jobId === parent.id ? artifactMessage.text : '';
  text('artifact-note', message || string(artifact.error) || string(artifact.conflict) || (artifact.applied ? '修改已套用到原工作目錄。' : TERMINAL.has(parent.status) && parent.status !== 'completed' ? '此任務未成功完成，差異僅供檢視與下載，不能套用。' : reviewed ? '差異已顯示。確認後按「套用修改」，才會變更原工作目錄。' : '先檢視差異，再明確套用到原工作目錄；發生衝突時不覆寫檔案。'));
}

async function reviewChanges() {
  const context = artifactContext();
  if (!context || $('artifact-review').disabled) return;
  const { parent, artifact } = context;
  pendingReview = parent.id;
  reviewedArtifact = null;
  artifactMessage = { jobId: parent.id, text: '正在取得隔離副本的差異。' };
  renderArtifact(chosenJob());
  try {
    const response = await fetch(artifactUrl(parent, 'changes'), { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } });
    const data = await response.json();
    if (!response.ok) throw new Error(string(data?.message) || '無法讀取差異。');
    if (data?.artifact?.id !== artifact.id || typeof data.diff !== 'string') throw new Error('差異與目前的修改版本不一致，請重新取得。');
    const current = artifactContext();
    if (stopped || current?.parent.id !== parent.id || current.artifact.id !== artifact.id || !TERMINAL.has(current.parent.status)) return;
    current.parent.artifact = data.artifact;
    reviewedArtifact = { jobId: parent.id, artifactId: artifact.id, diff: data.diff };
    artifactMessage = null;
  } catch (error) {
    if (!stopped) artifactMessage = { jobId: parent.id, text: string(error.message) || '無法讀取差異。' };
  } finally {
    if (pendingReview === parent.id) pendingReview = null;
    if (!stopped) renderArtifact(chosenJob());
  }
}

async function applyChanges() {
  const context = artifactContext();
  if (!context || $('artifact-apply').disabled) return;
  const { parent, artifact } = context;
  // A matching displayed review is required even if a caller dispatches a click manually.
  if (parent.status !== 'completed' || artifact.applied || reviewedArtifact?.jobId !== parent.id || reviewedArtifact.artifactId !== artifact.id) return;
  pendingApply = parent.id;
  artifactMessage = { jobId: parent.id, text: '正在檢查原工作目錄是否衝突並套用修改。' };
  renderArtifact(chosenJob());
  try {
    const response = await fetch(artifactUrl(parent, 'apply'), {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: '{}',
    });
    const data = await response.json();
    if (!response.ok) throw new Error(string(data?.message) || '無法套用修改，請檢查工作目錄與最新狀態。');
    if (data?.type !== 'monitor_artifact' || data.artifact?.id !== artifact.id || data.artifact.applied !== true) throw new Error('伺服器尚未確認套用結果，請同步最新狀態。');
    const current = state?.jobs.find(item => item.id === parent.id);
    if (current) current.artifact = data.artifact;
    artifactMessage = { jobId: parent.id, text: '修改已套用到原工作目錄。' };
  } catch (error) {
    if (!stopped) artifactMessage = { jobId: parent.id, text: string(error.message) || '套用失敗，請檢查工作目錄。' };
  } finally {
    if (pendingApply === parent.id) pendingApply = null;
    if (!stopped) renderArtifact(chosenJob());
  }
}

function renderJob() {
  const job = chosenJob();
  const name = providerName(provider(selectedProvider), selectedProvider);
  text('selected-provider-label', name);
  text('feed-title', '任務紀錄');
  renderJobSelect(job);
  $('job-summary').hidden = !job;
  $('job-empty').hidden = Boolean(job);
  $('event-feed').hidden = !job;
  if (job) {
    text('job-status', job.cancel_requested && ACTIVE.has(job.status) ? '停止中' : STATUS_NAMES[job.status] || '未知狀態');
    $('job-status').setAttribute('data-status', string(job.status));
    text('job-duration', elapsed(job));
    text('job-prompt', string(job.prompt));
    const role = job.kind === 'collaboration' ? '團隊總任務' : job.parent_job_id ? job.write_strategy === 'proposals' ? '實作提案' : ROLE_NAMES[job.role] || '協作子任務' : '';
    text('job-role', role);
    $('job-role').hidden = !role;
    text('job-model', job.model || job.configured_model ? `模型 ${job.model || job.configured_model}` : '模型：由 CLI 選擇');
    const effort = selectedProvider === 'codex' ? string(job.reasoning_effort) || string(job.configured_reasoning_effort) : '';
    text('job-reasoning-effort', effort ? `思考深度 ${effortName(effort)}` : '');
    $('job-reasoning-effort').hidden = !effort;
    text('job-transport', `通道 ${transportName(job, selectedProvider)}`);
    text('job-session', job.session_id ? `Session ${job.session_id}` : '');
    text('job-error', string(job.error));
    $('job-error').hidden = !job.error;
    const usage = job.usage;
    const parts = [];
    if (usage && typeof usage === 'object') {
      if (Number.isFinite(usage.input_tokens)) parts.push(`輸入 ${usage.input_tokens} tokens`);
      if (Number.isFinite(usage.output_tokens)) parts.push(`輸出 ${usage.output_tokens} tokens`);
      const cost = usage.cost_usd ?? usage.total_cost_usd;
      if (Number.isFinite(cost)) parts.push(`CLI 回報 US$${cost.toFixed(4)}`);
    }
    text('usage-summary', parts.join(' · '));
    $('usage-summary').hidden = !parts.length;
  }
  renderTeamProgress(job);
  renderArtifact(job);
  renderFeed(job);
  if (connected && synchronized) text('feed-note', job && !publicEvents(job.events).length ? 'CLI 尚未提供公開事件；這裡不會生成模擬進度。' : '僅顯示 CLI 提供的公開事件，不顯示私有推理。');
  updateControls();
}

function updateControls() {
  const ready = connected && synchronized;
  const item = provider(selectedProvider);
  const busy = busyProvider(item);
  const teamCount = renderTeamMembers();
  const prompt = $('task-prompt').value?.trim() || '';
  $('provider-select').disabled = !ready;
  $('task-mode').disabled = !ready || Boolean(pendingStart);
  $('model-select').disabled = !ready || !item?.available || Boolean(busy) || Boolean(pendingStart) || !modelOptions(item).length;
  $('reasoning-effort-select').disabled = !ready || !item?.available || Boolean(busy) || Boolean(pendingStart) || !reasoningOptions(item).length;
  $('refresh-providers').disabled = !ready;
  $('start-task').disabled = !ready || !item?.available || Boolean(busy) || !prompt || Boolean(pendingStart) || (teamMode() && !teamCount);
  text('start-label', pendingStart ? '送出中…' : busy ? '這位夥伴正在忙' : teamMode() ? '啟動團隊協作' : '啟動任務');
  const job = chosenJob();
  const target = cancellationTarget(job);
  const cancelling = target && ACTIVE.has(target.status) && (pendingCancel === target.id || target.cancel_requested);
  $('cancel-task').disabled = !ready || !target || !ACTIVE.has(target.status) || Boolean(cancelling);
  text('cancel-task', cancelling ? '停止中…' : teamParent(job) ? '停止整個團隊' : '停止任務');
  let note = '等待 CLI 狀態後即可交辦。';
  if (item) {
    if (!ready) note = '等待連線與最新狀態，暫時無法交辦。';
    else if (!item.available) note = '此 CLI 尚未接入，請先完成卡片中的安裝與登入步驟。';
    else if (busy) note = '每位夥伴同時處理一件事；可先查看或停止目前任務。';
    else {
      const mode = MODE_NAMES[item.execution_mode] || '執行模式由伺服器設定';
      const account = item.transport === 'antigravity' || item.id === 'claude' ? '（Antigravity 帳號）' : '';
      const authNote = item.auth === 'verified' ? '' : item.auth === 'required' ? ` 需要先在終端機完成登入${account}。` : ` 登入尚未驗證；若無法啟動，請先在終端機登入${account}。`;
      note = `${mode}。${string(item.execution_note) || '預設先交辦分析與檢查任務。'}${authNote}`;
    }
  }
  text('form-note', note);
  text('prompt-count', `${$('task-prompt').value?.length || 0} / 8000`);
  renderArtifact(job);
}

function receiveState(next) {
  if (next.version !== 1 || !Array.isArray(next.providers) || !Array.isArray(next.jobs)) return;
  const known = next.providers.filter(item => item && PROVIDERS.includes(item.id));
  state = { ...next, providers: known, jobs: next.jobs.filter(job => job && PROVIDERS.includes(job.provider) && typeof job.id === 'string') };
  snapshotAt = performance.now();
  frozenAt = snapshotAt;
  synchronized = true;
  if (!providerChosen) {
    selectedProvider = state.providers.find(item => item.available)?.id || 'codex';
    providerChosen = true;
  }
  if (pendingStart) {
    // Public prompts may be redacted by the server, so never compare them to
    // the original text to acknowledge a request. A provider runs one job.
    const created = state.jobs.find(job => job.provider === pendingStart.provider && !job.parent_job_id && !pendingStart.priorIds.has(job.id));
    if (created) {
      selectedProvider = created.provider;
      selectedJobId = created.id;
      pendingStart = null;
      text('form-status', '任務已建立，公開活動會顯示在右側。');
    }
  }
  if (pendingCancel && state.jobs.some(job => job.id === pendingCancel && TERMINAL.has(job.status))) pendingCancel = null;
  text('workspace-path', string(state.workspace) || '伺服器未提供工作目錄');
  $('workspace-path').title = string(state.workspace);
  text('last-sync', `最後同步 ${timestamp(new Date().toISOString())} · REV ${state.revision ?? '—'}`);
  renderProviderSelect();
  renderModelSelect();
  renderCards();
  renderJob();
  syncOffice();
}

function sendControl(payload) {
  if (!connected || !synchronized || socket?.readyState !== WebSocket.OPEN) return false;
  socket.send(JSON.stringify({ type: 'monitor_control', ...payload }));
  return true;
}

function chooseProvider(id) {
  if (!PROVIDERS.includes(id)) return;
  selectedProvider = id;
  selectedJobId = null;
  providerChosen = true;
  $('provider-select').value = id;
  if (state) { renderModelSelect(); renderCards(); renderJob(); }
  renderOfficeSelection();
  if (officeScene) {
    try { officeScene.selectProvider(id); } catch (error) { showOfficeError(error); }
  }
  updateControls();
}

function connect() {
  if (stopped) return;
  setConnection('connecting');
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const current = new WebSocket(`${protocol}//${location.host}/ws/monitor`);
  socket = current;
  current.addEventListener('open', () => {
    if (current !== socket || stopped) return;
    reconnectDelay = 700;
    setConnection('connected');
  });
  current.addEventListener('message', event => {
    if (current !== socket || stopped) return;
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (!data || typeof data !== 'object') return;
    if (data.type === 'monitor_state') receiveState(data);
    else if (data.type === 'monitor_error') {
      pendingStart = null;
      pendingCancel = null;
      showError(string(data.message) || '指令暫時無法執行。');
      updateControls();
    }
  });
  current.addEventListener('close', () => {
    if (current !== socket || stopped) return;
    pendingStart = null;
    pendingCancel = null;
    setConnection('disconnected');
    clearTimeout(retryTimer);
    retryTimer = setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(10000, reconnectDelay * 1.7);
  });
  current.addEventListener('error', () => { current.close(); });
}

PROVIDERS.forEach(id => $(id + '-select').addEventListener('click', () => chooseProvider(id)));
PROVIDERS.forEach(id => $('team-member-' + id).addEventListener('change', () => {
  selectedTeamMembers.set(id, $('team-member-' + id).checked === true);
  updateControls();
}));
$('task-mode').addEventListener('change', updateControls);
$('provider-select').addEventListener('change', () => chooseProvider($('provider-select').value));
$('model-select').addEventListener('change', () => {
  const value = $('model-select').value;
  if (value && !modelOptions(provider(selectedProvider)).some(option => option.id === value)) {
    renderModelSelect();
    return;
  }
  selectedModels.set(selectedProvider, value);
  renderReasoningSelect();
  renderOfficeSelection();
  updateControls();
});
$('reasoning-effort-select').addEventListener('change', () => {
  const item = provider(selectedProvider);
  const value = $('reasoning-effort-select').value;
  if (selectedProvider !== 'codex' || !reasoningOptions(item).includes(value)) {
    renderReasoningSelect();
    return;
  }
  selectedReasoningEfforts.set(selectedModels.get(selectedProvider) || item?.configured_model, value);
  renderOfficeSelection();
  updateControls();
});
$('job-select').addEventListener('change', () => { selectedJobId = $('job-select').value || null; renderJob(); });
$('task-prompt').addEventListener('input', updateControls);
$('task-form').addEventListener('submit', event => {
  event.preventDefault();
  const prompt = $('task-prompt').value.trim();
  if ($('start-task').disabled || !prompt || prompt.length > 8000) return;
  const request = { provider: selectedProvider, priorIds: new Set((state?.jobs || []).map(job => job.id)) };
  const selectedModel = $('model-select').value;
  if (selectedModel && !modelOptions(provider(selectedProvider)).some(option => option.id === selectedModel)) {
    showError('這個模型已不在可用選項中，請重新選擇。');
    renderModelSelect();
    return;
  }
  const efforts = reasoningOptions(provider(selectedProvider), selectedModel || provider(selectedProvider)?.configured_model);
  const selectedEffort = selectedProvider === 'codex' ? $('reasoning-effort-select').value : '';
  if ((efforts.length || selectedEffort) && !efforts.includes(selectedEffort)) {
    showError('這個思考深度已不在模型支援的選項中，請重新選擇。');
    renderReasoningSelect();
    renderOfficeSelection();
    return;
  }
  let collaboration = null;
  if (teamMode()) {
    const members = PROVIDERS.filter(id => id !== selectedProvider && $('team-member-' + id).checked).map(id => {
      const item = provider(id);
      const modelId = selectedModelFor(id);
      const effort = selectedEffortFor(id, modelId || item?.configured_model);
      return { provider: id, ...(modelId ? { model: modelId } : {}), ...(effort ? { reasoning_effort: effort } : {}) };
    });
    if (!members.length || members.some(member => !provider(member.provider)?.available || busyProvider(provider(member.provider)))) {
      showError('請選擇至少一位已接入且空閒的夥伴。');
      updateControls();
      return;
    }
    collaboration = { members };
  }
  if (sendControl({ action: 'start', provider: selectedProvider, prompt, ...(selectedModel ? { model: selectedModel } : {}), ...(selectedEffort ? { reasoning_effort: selectedEffort } : {}), ...(collaboration ? { collaboration } : {}) })) {
    pendingStart = request;
    selectedJobId = null;
    hideError();
    text('form-status', '任務已送出，等待伺服器確認。');
    updateControls();
  }
});
$('cancel-task').addEventListener('click', () => {
  const job = cancellationTarget(chosenJob());
  if ($('cancel-task').disabled || !job || !ACTIVE.has(job.status)) return;
  if (sendControl({ action: 'cancel', job_id: job.id })) {
    pendingCancel = job.id;
    text('form-status', '已請求停止這個任務。');
    updateControls();
  }
});
$('artifact-review').addEventListener('click', reviewChanges);
$('artifact-apply').addEventListener('click', applyChanges);
$('refresh-providers').addEventListener('click', () => {
  if (sendControl({ action: 'refresh' })) text('form-status', '已請求重新偵測 CLI。');
});
$('dismiss-error').addEventListener('click', hideError);
$('office-reset-camera').addEventListener('click', () => {
  if (officeScene) {
    try { officeScene.resetCamera(); } catch (error) { showOfficeError(error); }
  }
});
$('office-retry').addEventListener('click', () => location.reload());
const clock = setInterval(() => {
  if (!state || stopped) return;
  renderCards();
  const job = chosenJob();
  if (job) text('job-duration', elapsed(job));
}, 1000);
window.addEventListener('beforeunload', () => {
  stopped = true;
  clearTimeout(retryTimer);
  clearInterval(clock);
  socket?.close();
  officeScene?.dispose();
});
connect();
import('./office-scene.js').then(({ OfficeScene }) => {
  if (stopped) return;
  officeScene = new OfficeScene($('office-scene'), { onSelect: id => chooseProvider(id) });
  window.officeView = officeScene;
  $('office-reset-camera').disabled = false;
  syncOffice();
}).catch(showOfficeError);
