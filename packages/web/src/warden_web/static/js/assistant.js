// ── Ward, warden's assistant (Phase 1: talk & explain, read-only) ───────────
// Ward is warden's nephew: knows the house, still learning, allowed to look but
// not touch. The dock lives bottom-right and only exists when AI is switched on.
// It opens a side panel that chats through /api/ai/chat. Nothing here writes to
// a database: Ward can look, explain, and draft, but you run any change.
// See packages/web/src/warden_web/assistant.py for the safety spine.

const AI_STORE = 'warden.ai';
// Ward's mark: a keyhole (warden's lineage) with a spark (the bright young helper).
const WARD_MARK = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="9.5" r="3.7"/><path d="M8.4 12.4 7 18.6h7l-1.4-6.2z"/><path d="M18.4 4.6l.62 1.78 1.78.62-1.78.62-.62 1.78-.62-1.78-1.78-.62 1.78-.62z"/></svg>';
const AI_KEYHOLE = WARD_MARK;   // legacy alias used across the panel markup

function aiSettings() { try { return JSON.parse(localStorage.getItem(AI_STORE)) || {}; } catch { return {}; } }
function saveAiSettings(patch) {
  const s = { ...aiSettings(), ...patch };
  try { localStorage.setItem(AI_STORE, JSON.stringify(s)); } catch {}
  return s;
}
function aiReady() {
  const s = aiSettings();
  if (!s.enabled || !s.model) return false;
  return s.provider === 'local' ? true : !!s.key;   // local needs no key
}

let aiThread = [];       // [{role:'user'|'assistant', content}]
let aiBusy = false;
let _aiMachine = null;   // last "check my machine" result, if run

// ── dock + panel plumbing ───────────────────────────────────────────────────
function mountAssistantDock() {
  const on = !!aiSettings().enabled;
  const dock = document.getElementById('aiDock');
  const panel = document.getElementById('aiPanel');
  if (!on) { if (dock) dock.remove(); if (panel) panel.remove(); return; }
  if (!dock) {
    const b = document.createElement('button');
    b.id = 'aiDock'; b.className = 'ai-dock'; b.type = 'button';
    b.title = 'Ward'; b.setAttribute('aria-label', 'Open Ward');
    b.onclick = toggleAiPanel;
    b.innerHTML = AI_KEYHOLE;
    document.body.appendChild(b);
  }
  ensureAiPanel();
}

function ensureAiPanel() {
  if (document.getElementById('aiPanel')) return;
  const p = document.createElement('div');
  p.id = 'aiPanel'; p.className = 'ai-panel'; p.setAttribute('aria-hidden', 'true');
  p.innerHTML = `
    <div class="ai-head">
      <span class="ai-avatar">${AI_KEYHOLE}</span>
      <span class="ai-who">Ward</span>
      <span class="ai-ctx" id="aiCtx"></span>
      <button class="ai-x" type="button" title="Close" aria-label="Close" onclick="toggleAiPanel()">&times;</button>
    </div>
    <div class="ai-thread" id="aiThread"></div>
    <form class="ai-compose" id="aiCompose" onsubmit="return aiSubmit(event)">
      <textarea id="aiInput" rows="1" placeholder="Ask about your data, or describe a change to draft…"
        aria-label="Message the assistant"></textarea>
      <button class="ai-send" type="submit" title="Send" aria-label="Send">${AI_KEYHOLE}</button>
    </form>`;
  document.body.appendChild(p);
  const ta = p.querySelector('#aiInput');
  ta.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); aiSubmit(e); }
  });
  ta.addEventListener('input', () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 140) + 'px'; });
  if (!aiThread.length) aiGreeting();
}

function toggleAiPanel() {
  ensureAiPanel();
  const p = document.getElementById('aiPanel');
  const open = p.classList.toggle('open');
  p.setAttribute('aria-hidden', open ? 'false' : 'true');
  document.getElementById('aiDock')?.classList.toggle('active', open);
  if (open) {
    aiRenderContext();
    if (!aiReady()) aiNeedsSetup();
    setTimeout(() => document.getElementById('aiInput')?.focus(), 30);
  }
}

function aiRenderContext() {
  const el = document.getElementById('aiCtx'); if (!el) return;
  const env = aiEnvValue();
  const eng = currentEngine ? engineLabel(currentEngine) : 'no engine';
  const kind = env ? envKind(env) : null;
  el.innerHTML = `${esc(eng)}${env ? ' · ' + `<b class="k-${esc(kind || '')}">${esc(env)}</b>` : ''}${prefs.readOnly ? ' · read-only' : ''}`;
}

function aiEnvValue() { return document.getElementById('envSelect')?.value || ''; }

// ── chat ────────────────────────────────────────────────────────────────────
function aiGreeting() {
  aiBubble('ai', "Hey, I'm Ward. I can explain who can reach what, summarize a table, run the security audits as a report, or draft a change for you to run. I only ever look, never touch, so poke around freely.");
}

function aiNeedsSetup() {
  aiBubble('ai', "Almost there. Pick a model on the Ward page (download one, or paste a key), then we can talk.");
  const t = document.getElementById('aiThread');
  const cta = document.createElement('button');
  cta.className = 'ai-inline-btn'; cta.type = 'button'; cta.textContent = 'Set up Ward';
  cta.onclick = () => { toggleAiPanel(); navigate('assistant'); };
  t.appendChild(cta); aiScroll();
}

function aiSubmit(e) {
  if (e && e.preventDefault) e.preventDefault();
  const ta = document.getElementById('aiInput');
  const text = (ta.value || '').trim();
  if (!text || aiBusy) return false;
  ta.value = ''; ta.style.height = 'auto';
  aiAsk(text);
  return false;
}

async function aiAsk(text) {
  if (!aiReady()) { aiNeedsSetup(); return; }
  aiThread.push({ role: 'user', content: text });
  aiBubble('me', text);
  aiBusy = true;
  const work = aiWorking();
  const s = aiSettings();
  let res;
  try {
    res = await apiPost('/api/ai/chat', {
      messages: aiThread,
      ai_provider: s.provider || 'gemini', ai_key: s.key, ai_model: s.model,
      persona: s.persona || '',
      context: { engine: currentEngine, env: aiEnvValue(), page: currentView,
                 connected: !!connected },
    });
  } catch (err) {
    res = { error: 'Could not reach the server.' };
  }
  work.remove();
  aiBusy = false;
  aiHandle(res);
}

function aiHandle(res) {
  if (!res || res.error) { aiBubble('err', res?.error || 'Something went wrong.'); return; }
  if (res.steps && res.steps.length) aiSteps(res.steps);
  if (res.question) { aiThread.push({ role: 'assistant', content: res.question.question || 'A question' }); aiQuestion(res.question); return; }
  if (res.draft) { aiThread.push({ role: 'assistant', content: 'Proposed: ' + (res.draft.summary || 'an operation') }); aiDraft(res.draft); return; }
  const reply = res.reply || '(no answer)';
  aiThread.push({ role: 'assistant', content: reply });
  aiBubble('ai', reply);
}

// ── rendering bits ───────────────────────────────────────────────────────────
function aiThreadEl() { return document.getElementById('aiThread'); }
function aiScroll() { const t = aiThreadEl(); if (t) t.scrollTop = t.scrollHeight; }

function aiBubble(kind, text) {
  const t = aiThreadEl(); if (!t) return;
  const d = document.createElement('div');
  d.className = 'ai-msg ai-' + kind;
  d.textContent = text;
  t.appendChild(d); aiScroll();
}

function aiWorking() {
  const t = aiThreadEl();
  const d = document.createElement('div');
  d.className = 'ai-work';
  d.innerHTML = `<span class="ai-bars"><i></i><i></i><i></i></span> thinking`;
  t.appendChild(d); aiScroll();
  return d;
}

function aiSteps(steps) {
  const names = steps.map(s => s.tool).join(', ');
  const t = aiThreadEl();
  const d = document.createElement('div');
  d.className = 'ai-steps';
  d.textContent = 'looked at: ' + names;
  t.appendChild(d); aiScroll();
}

function aiQuestion(q) {
  const t = aiThreadEl();
  const wrap = document.createElement('div');
  wrap.className = 'ai-ask';
  const kind = q.kind || 'text';
  const opts = Array.isArray(q.options) ? q.options : [];
  let controls = '';
  if (kind === 'boolean') {
    controls = `<div class="ai-ask-row">
      <button type="button" class="ai-chip-btn" onclick="aiAnswer('yes')">Yes</button>
      <button type="button" class="ai-chip-btn" onclick="aiAnswer('no')">No</button></div>`;
  } else if (kind === 'select' && opts.length) {
    controls = `<div class="ai-ask-row">` +
      opts.map((o, i) => `<button type="button" class="ai-chip-btn ai-opt" data-i="${i}">${esc(o)}</button>`).join('') +
      `</div>` + aiTypeOut();
  } else if (kind === 'multiselect' && opts.length) {
    controls = `<div class="ai-ask-multi">` +
      opts.map(o => `<label class="ai-check"><input type="checkbox" value="${esc(o)}"> ${esc(o)}</label>`).join('') +
      `</div><button type="button" class="ai-chip-btn primary" onclick="aiAnswerMulti(this)">Confirm</button>` + aiTypeOut();
  } else {
    controls = `<div class="ai-ask-row"><input type="text" class="ai-ask-input" id="aiAskInput"
      placeholder="type your answer…" onkeydown="if(event.key==='Enter'){aiAnswer(this.value)}">
      <button type="button" class="ai-chip-btn primary" onclick="aiAnswer(document.getElementById('aiAskInput').value)">Send</button></div>`;
  }
  wrap.innerHTML = `<div class="ai-ask-q">${esc(q.question || '')}</div>${controls}${q.hint ? `<div class="ai-ask-hint">${esc(q.hint)}</div>` : ''}`;
  // Wire option buttons by index so we never have to escape a value into inline JS.
  wrap.querySelectorAll('.ai-opt').forEach(b => { b.onclick = () => aiAnswer(opts[+b.dataset.i]); });
  const typeBtn = wrap.querySelector('.ai-typeout-btn');
  if (typeBtn) typeBtn.onclick = () => {
    const row = wrap.querySelector('.ai-typeout-row');
    row.style.display = 'flex'; typeBtn.style.display = 'none';
    row.querySelector('input').focus();
  };
  t.appendChild(wrap); aiScroll();
  setTimeout(() => document.getElementById('aiAskInput')?.focus(), 30);
}

// A "none of these, let me type it" escape for pick lists.
function aiTypeOut() {
  return `<button type="button" class="ai-typeout-btn">none of these, type it out</button>
    <div class="ai-typeout-row" style="display:none">
      <input type="text" class="ai-ask-input" placeholder="type the exact name…" onkeydown="if(event.key==='Enter')aiAnswer(this.value)">
      <button type="button" class="ai-chip-btn primary" onclick="aiAnswer(this.previousElementSibling.value)">Use</button>
    </div>`;
}

function aiAnswer(val) {
  val = (val || '').toString().trim();
  if (!val) return;
  // disable the widget that asked
  aiThreadEl().querySelectorAll('.ai-ask').forEach(el => el.classList.add('answered'));
  aiAsk(val);
}
function aiAnswerMulti(btn) {
  const picks = [...btn.parentElement.querySelectorAll('input:checked')].map(i => i.value);
  if (!picks.length) return;
  aiAnswer(picks.join(', '));
}

function aiDraft(d) {
  const t = aiThreadEl();
  const ops = Array.isArray(d.operations) ? d.operations : [];
  const lines = ops.length ? ops.map(aiOpLine) : (Array.isArray(d.statements) ? d.statements : []);
  const el = document.createElement('div');
  el.className = 'ai-op';
  el._ops = ops;
  el.innerHTML = `
    <div class="ai-op-h">proposed · write</div>
    <div class="ai-op-sum">${esc(d.summary || '')}</div>
    <div class="ai-op-lines">${lines.map(l => `<div>${esc(l)}</div>`).join('')}</div>
    <div class="ai-op-foot"></div>`;
  t.appendChild(el);
  aiDraftFoot(el, 'idle');
  aiScroll();
}

// A structured operation as one plain-english line.
function aiOpLine(op) {
  const u = op.username || '';
  const a = op.access || 'read';
  const db = op.database || '?';
  switch (op.kind) {
    case 'create_user': return `create user ${u}`;
    case 'drop_user': return `drop user ${u}`;
    case 'reset_password': return `reset password for ${u}`;
    case 'toggle_login': return `${op.enable === false ? 'disable' : 'enable'} login for ${u}`;
    case 'grant': return `grant ${a} on ${db} to ${u}`;
    case 'revoke': return `revoke ${a} on ${db} from ${u}`;
    case 'create_collection': return `create collection ${op.collection || '?'} in ${db}`;
    case 'create_database': return `create database ${db}`;
    default: return op.kind || 'operation';
  }
}

// The card's footer changes with state: idle -> a restricted confirm -> results.
function aiDraftFoot(el, state) {
  const foot = el.querySelector('.ai-op-foot');
  const ops = el._ops || [];
  if (!ops.length) {
    foot.innerHTML = `<span class="ai-op-note">Run this yourself in the UI.</span>
      <button type="button" class="ai-chip-btn">Copy</button>`;
    foot.querySelector('button').onclick = () => copyText(ops.map(aiOpLine).join('\n'));
    return;
  }
  if (prefs.readOnly) {
    foot.innerHTML = `<span class="ai-op-note ai-warn">Read-only mode is on. Turn it off in the top bar to let Ward run this.</span>`;
    return;
  }
  if (envKind(aiEnvValue()) === 'prod' && !aiSettings().allowProd) {
    foot.innerHTML = `<span class="ai-op-note ai-warn">This looks like production. Ward drafts only here, run it yourself. (You can allow prod runs on the Ward page.)</span>`;
    return;
  }
  if (state === 'confirm') {
    foot.innerHTML = `<span class="ai-op-note ai-warn">This changes data. Sure?</span>
      <button type="button" class="ai-chip-btn">Cancel</button>
      <button type="button" class="ai-chip-btn danger">Yes, run it</button>`;
    const [cancel, go] = foot.querySelectorAll('button');
    cancel.onclick = () => aiDraftFoot(el, 'idle');
    go.onclick = () => aiRunOps(el);
  } else {
    foot.innerHTML = `<span class="ai-op-note">Ward will run this for you.</span>
      <button type="button" class="ai-chip-btn primary">Confirm &amp; run &#9656;</button>`;
    foot.querySelector('button').onclick = () => aiDraftFoot(el, 'confirm');
  }
}

async function aiRunOps(el) {
  el.querySelector('.ai-op-foot').innerHTML =
    `<span class="ai-op-note"><span class="ai-bars"><i></i><i></i><i></i></span> running</span>`;
  const res = await apiPost('/api/ai/execute', { operations: el._ops,
    env_kind: envKind(aiEnvValue()), allow_prod: !!aiSettings().allowProd });
  const foot = el.querySelector('.ai-op-foot');
  el.classList.add('done');
  if (!res || res.error) { foot.innerHTML = `<span class="ai-op-note ai-warn">${esc(res && res.error || 'Failed to run.')}</span>`; return; }
  const results = res.results || [];
  el.querySelector('.ai-op-lines').innerHTML = results.map(r =>
    `<div class="ai-res ${r.ok ? 'ok' : 'bad'}">${r.ok ? '&#10003;' : '&#10007;'} ${esc(r.kind)}${r.target ? ' ' + esc(r.target) : ''}` +
    `${r.error ? ' &mdash; ' + esc(r.error) : ''}` +
    `${r.password ? ` &middot; password <code class="ai-pw" title="click to copy">${esc(r.password)}</code>` : ''}</div>`).join('');
  const ok = results.filter(r => r.ok).length;
  foot.innerHTML = `<span class="ai-op-note">${ok}/${results.length} done${results.some(r => r.password) ? ' &middot; copy the password now' : ''}.</span>`;
  el.querySelectorAll('.ai-pw').forEach(c => { c.onclick = () => copyText(c.textContent); });
  if (typeof viewListUsers === 'function' && currentView === 'users' && connected) viewListUsers();
  aiScroll();
}

// ── the Ward page (meet him, then pick a brain) ──────────────────────────────
function viewAssistant() {
  markActive('assistant');
  const s = aiSettings();
  const on = !!s.enabled;
  setContent(`
    <div class="view-head"><h2>Ward</h2>
      <label class="ai-toggle">
        <input type="checkbox" id="aiEnable" ${on ? 'checked' : ''} onchange="aiToggleEnabled(this.checked)">
        <span>${on ? 'On' : 'Off'}</span>
      </label>
    </div>
    <p class="muted ai-lore">warden's nephew. Knows the house, still learning the ropes, and allowed to look but never touch. Ward explains who can reach what, reads you a report, or drafts a change for you to run. Off by default.</p>
    <div id="aiConfig" style="${on ? '' : 'display:none'}"></div>`);
  if (on) aiRenderConfig();
}

function aiToggleEnabled(on) {
  saveAiSettings({ enabled: !!on });
  document.querySelector('.ai-toggle span').textContent = on ? 'On' : 'Off';
  document.getElementById('aiConfig').style.display = on ? '' : 'none';
  if (on) aiRenderConfig();
  mountAssistantDock();
}

function aiRenderConfig() {
  const s = aiSettings();
  const provider = s.provider || 'local';
  const card = (id, title, desc) => `<button type="button" class="ai-prov ${provider === id ? 'sel' : ''}" onclick="aiPickProvider('${id}')">
      <span class="ai-prov-t">${esc(title)}</span><span class="ai-prov-d">${esc(desc)}</span></button>`;
  document.getElementById('aiConfig').innerHTML = `
    <div class="ai-providers">
      ${card('local', 'On this machine', 'Private, no key. Downloads a small model. Best for chatting and drafting.')}
      ${card('gemini', 'Google Gemini', 'Needs a free API key. Sharper for questions and reports.')}
    </div>
    <div id="aiProviderBody"></div>
    <div class="card ai-card">
      <label class="ai-prod-toggle"><input type="checkbox" id="aiAllowProd" ${s.allowProd ? 'checked' : ''} onchange="aiSaveAllowProd(this.checked)">
        <span>Let Ward run changes on <b>production</b> environments. Off by default, Ward only drafts there and you run it yourself.</span></label>
      <div class="ai-field" style="margin-top:16px"><label>Custom behavior <span class="muted">(optional)</span></label>
        <textarea id="aiPersona" rows="2" placeholder="e.g. keep answers to one line, prefer names over ids…">${esc(s.persona || '')}</textarea></div>
      <div class="ai-actions"><button type="button" class="btn" onclick="aiSavePersona()">Save behavior</button></div>
    </div>`;
  aiRenderProviderBody(provider);
}

function aiPickProvider(id) {
  saveAiSettings({ provider: id });
  aiRenderConfig();
  mountAssistantDock();
}

function aiRenderProviderBody(provider) {
  const body = document.getElementById('aiProviderBody');
  if (!body) return;
  if (provider === 'gemini') { body.innerHTML = aiGeminiHtml(); return; }
  body.innerHTML = `<div class="card ai-card"><div class="loading-msg"><div class="spinner"></div> Loading model sizes…</div></div>`;
  aiRenderLocal();
}

// ── local (download & go) ────────────────────────────────────────────────────
async function aiRenderLocal() {
  const res = await apiPost('/api/ai/models', { ai_provider: 'local' });
  const body = document.getElementById('aiProviderBody');
  if (!body) return;
  if (res.error) { body.innerHTML = `<div class="card ai-card"><div class="ai-err-inline">${esc(res.error)}</div></div>`; return; }
  body.innerHTML = aiLocalHtml(res.models || []);
  if ((res.models || []).some(t => t.download && t.download.status === 'downloading')) aiPollLocal();
}

function aiLocalHtml(tiers) {
  const cur = aiSettings().model;
  const m = _aiMachine;
  const check = (m && !m.error)
    ? `<div class="ai-machine">${ICONS.pulse || ''}<span><b>Your machine:</b> ${esc(m.summary)}</span>
         <button type="button" class="ai-machine-re" onclick="aiCheckMachine()" title="Check again">recheck</button></div>`
    : `<button type="button" id="aiCheckBtn" class="btn ai-check-btn" onclick="aiCheckMachine()">${ICONS.pulse || ''} Which size fits my machine?</button>`;
  return `${check}
    <div class="ai-tiers">${tiers.map(t => aiTierCard(t, cur)).join('')}</div>
    <div class="ai-privacy">${ICONS.shield || ''} Nothing leaves your machine. The model runs right here.</div>`;
}

async function aiCheckMachine() {
  const btn = document.getElementById('aiCheckBtn');
  if (btn) { btn.disabled = true; btn.innerHTML = 'Checking…'; }
  const res = await apiPost('/api/ai/check', {});
  _aiMachine = res.error ? null : res;
  if (res.error) { toast(res.error, 'error'); return; }
  aiRenderLocal();
}

function aiTierCard(t, cur) {
  const dl = t.download;
  let ctrl;
  if (dl && dl.status === 'downloading') {
    const pct = dl.pct || 0;
    const mb = dl.total ? ` ${Math.round((dl.done || 0) / 1e6)}/${Math.round(dl.total / 1e6)} MB` : '';
    ctrl = `<div class="ai-prog"><div class="ai-prog-bar" style="width:${pct}%"></div></div><span class="ai-prog-pct">${pct}%${mb}</span>`;
  } else if (t.downloaded) {
    ctrl = (cur === t.id)
      ? `<span class="ai-tier-inuse">In use ✓</span>`
      : `<button type="button" class="btn primary" onclick="aiUseTier('${t.id}')">Use this</button>`;
  } else if (dl && dl.status === 'error') {
    ctrl = `<button type="button" class="btn" onclick="aiDownloadTier('${t.id}')">Retry</button>`;
  } else {
    ctrl = `<button type="button" class="btn" onclick="aiDownloadTier('${t.id}')">Download ${esc(t.size)}</button>`;
  }
  const m = _aiMachine;
  const best = (m && m.recommended === t.id) ? `<span class="ai-tier-best">best for you</span>` : '';
  const heavy = (m && m.fits && m.fits[t.id] === false) ? `<span class="ai-tier-heavy">heavy here</span>` : '';
  const rec = t.recommended ? `<span class="ai-tier-rec">recommended</span>` : '';
  return `<div class="ai-tier ${cur === t.id ? 'sel' : ''} ${heavy ? 'dim' : ''}">
    <div class="ai-tier-info">
      <span class="ai-tier-head"><span class="ai-tier-name">${esc(t.label)}</span>${rec}${best}${heavy}<span class="ai-tier-size">${esc(t.size)}</span></span>
      ${t.note ? `<span class="ai-tier-note">${esc(t.note)}</span>` : ''}
    </div>
    <div class="ai-tier-ctrl">${ctrl}</div></div>`;
}

async function aiDownloadTier(tier) {
  const res = await apiPost('/api/ai/download', { tier });
  if (res.error) { toast(res.error, 'error'); return; }
  aiRenderLocal();
}

let _aiPoll = null;
function aiPollLocal() {
  if (_aiPoll) return;
  _aiPoll = setInterval(async () => {
    if (currentView !== 'assistant') { clearInterval(_aiPoll); _aiPoll = null; return; }
    const res = await apiPost('/api/ai/models', { ai_provider: 'local' });
    const body = document.getElementById('aiProviderBody');
    if (body && !res.error) body.innerHTML = aiLocalHtml(res.models || []);
    if (!(res.models || []).some(t => t.download && t.download.status === 'downloading')) {
      clearInterval(_aiPoll); _aiPoll = null;
    }
  }, 1200);
}

function aiUseTier(tier) {
  saveAiSettings({ provider: 'local', model: tier, enabled: true });
  aiRenderLocal();
  mountAssistantDock();
  toast('Ward is using the ' + tier + ' model. Dock is bottom-right.', 'success');
}

// ── gemini (bring your own key) ──────────────────────────────────────────────
function aiGeminiHtml() {
  const s = aiSettings();
  const model = s.provider === 'gemini' ? s.model : '';
  return `<div class="card ai-card">
    <div class="ai-field"><label>API key</label>
      <input type="password" id="aiApiKey" value="${esc(s.key || '')}" placeholder="paste your Gemini API key" autocomplete="off"></div>
    <div class="ai-field"><label>Model</label>
      <div class="ai-model-row">
        <select id="aiModel">${model ? `<option value="${esc(model)}" selected>${esc(model)}</option>` : '<option value="">fetch models first</option>'}</select>
        <button type="button" class="btn" onclick="aiFetchModels()">Fetch models</button></div></div>
    <div class="ai-privacy">${ICONS.shield || ''} Your prompt and schema names (table, user, database names) go to Google. Your rows and credentials never do.</div>
    <div class="ai-actions"><button type="button" class="btn primary" onclick="aiSaveGemini()">Save</button></div>
  </div>`;
}

async function aiFetchModels() {
  const key = document.getElementById('aiApiKey').value.trim();
  if (!key) { toast('Add your API key first', 'info'); return; }
  const sel = document.getElementById('aiModel');
  sel.innerHTML = '<option>fetching…</option>';
  const res = await apiPost('/api/ai/models', { ai_provider: 'gemini', ai_key: key });
  if (res.error) { sel.innerHTML = '<option value="">failed</option>'; toast(res.error, 'error'); return; }
  const models = res.models || [];
  if (!models.length) { sel.innerHTML = '<option value="">none found</option>'; return; }
  const cur = aiSettings().model;
  sel.innerHTML = models.map(m => `<option value="${esc(m.id)}" ${m.id === cur ? 'selected' : ''}>${esc(m.label)}</option>`).join('');
  toast(`${models.length} models available`, 'success');
}

function aiSaveGemini() {
  const key = document.getElementById('aiApiKey').value.trim();
  const model = document.getElementById('aiModel').value;
  saveAiSettings({ provider: 'gemini', key, model, enabled: true });
  mountAssistantDock();
  toast(aiReady() ? 'Ward is ready with Gemini.' : 'Saved. Pick a model to finish.', aiReady() ? 'success' : 'info');
}

function aiSavePersona() {
  saveAiSettings({ persona: document.getElementById('aiPersona').value.trim() });
  toast('Saved', 'success');
}

function aiSaveAllowProd(on) {
  saveAiSettings({ allowProd: !!on });
  toast(on ? 'Ward can now run changes on prod. Tread carefully.' : 'Ward is draft-only on prod again.',
        on ? 'info' : 'success');
}
