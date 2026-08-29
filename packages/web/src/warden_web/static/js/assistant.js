// ── AI assistant (Phase 1: talk & explain, read-only) ───────────────────────
// The dock lives bottom-right and only exists when AI is switched on. It opens a
// side panel that chats with the model through /api/ai/chat. Nothing here writes
// to a database: the model can look, explain, and draft, but the human runs any
// change themselves. See packages/web/src/warden_web/assistant.py for the spine.

const AI_STORE = 'warden.ai';
const AI_KEYHOLE = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="9" r="4"/><path d="M9.6 12.2 8.2 19h7.6l-1.4-6.8z"/></svg>';

function aiSettings() { try { return JSON.parse(localStorage.getItem(AI_STORE)) || {}; } catch { return {}; } }
function saveAiSettings(patch) {
  const s = { ...aiSettings(), ...patch };
  try { localStorage.setItem(AI_STORE, JSON.stringify(s)); } catch {}
  return s;
}
function aiReady() { const s = aiSettings(); return !!(s.enabled && s.key && s.model); }

let aiThread = [];       // [{role:'user'|'assistant', content}]
let aiBusy = false;

// ── dock + panel plumbing ───────────────────────────────────────────────────
function mountAssistantDock() {
  const on = !!aiSettings().enabled;
  const dock = document.getElementById('aiDock');
  const panel = document.getElementById('aiPanel');
  if (!on) { if (dock) dock.remove(); if (panel) panel.remove(); return; }
  if (!dock) {
    const b = document.createElement('button');
    b.id = 'aiDock'; b.className = 'ai-dock'; b.type = 'button';
    b.title = 'Assistant'; b.setAttribute('aria-label', 'Open the assistant');
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
      <span class="ai-who">assistant</span>
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
  aiBubble('ai', "Hey. I can explain who can touch what, summarize a table, run the security audits as a report, or draft an operation for you to run. I only ever look, never change, so poke around freely.");
}

function aiNeedsSetup() {
  aiBubble('ai', 'Almost there. Add an API key and pick a model on the Assistant page, then we can talk.');
  const t = document.getElementById('aiThread');
  const cta = document.createElement('button');
  cta.className = 'ai-inline-btn'; cta.type = 'button'; cta.textContent = 'Open Assistant settings';
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
  let controls = '';
  if (kind === 'boolean') {
    controls = `<div class="ai-ask-row">
      <button type="button" class="ai-chip-btn" onclick="aiAnswer('yes')">Yes</button>
      <button type="button" class="ai-chip-btn" onclick="aiAnswer('no')">No</button></div>`;
  } else if (kind === 'select' && Array.isArray(q.options)) {
    controls = `<div class="ai-ask-row">` +
      q.options.map(o => `<button type="button" class="ai-chip-btn" onclick="aiAnswer(${JSON.stringify(esc(o)).replace(/"/g, '&quot;')})">${esc(o)}</button>`).join('') +
      `</div>`;
  } else if (kind === 'multiselect' && Array.isArray(q.options)) {
    controls = `<div class="ai-ask-multi">` +
      q.options.map((o, i) => `<label class="ai-check"><input type="checkbox" value="${esc(o)}" id="aiMs${i}"> ${esc(o)}</label>`).join('') +
      `</div><button type="button" class="ai-chip-btn primary" onclick="aiAnswerMulti(this)">Confirm</button>`;
  } else {
    controls = `<div class="ai-ask-row"><input type="text" class="ai-ask-input" id="aiAskInput"
      placeholder="type your answer…" onkeydown="if(event.key==='Enter'){aiAnswer(this.value)}">
      <button type="button" class="ai-chip-btn primary" onclick="aiAnswer(document.getElementById('aiAskInput').value)">Send</button></div>`;
  }
  wrap.innerHTML = `<div class="ai-ask-q">${esc(q.question || '')}</div>${controls}${q.hint ? `<div class="ai-ask-hint">${esc(q.hint)}</div>` : ''}`;
  t.appendChild(wrap); aiScroll();
  setTimeout(() => document.getElementById('aiAskInput')?.focus(), 30);
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
  const writes = d.writes !== false;
  const lines = Array.isArray(d.statements) ? d.statements : [];
  const el = document.createElement('div');
  el.className = 'ai-op' + (writes ? '' : ' readonly');
  el.innerHTML = `
    <div class="ai-op-h">proposed${writes ? ' · write' : ' · read'}</div>
    <div class="ai-op-sum">${esc(d.summary || '')}</div>
    <div class="ai-op-lines">${lines.map(l => `<div>${esc(l)}</div>`).join('')}</div>
    <div class="ai-op-foot">
      <span class="ai-op-note">Phase 1: run this yourself in the UI. Executing from chat lands in the next pass.</span>
      <button type="button" class="ai-chip-btn" onclick='aiCopyDraft(${JSON.stringify(lines).replace(/'/g, "&#39;")})'>Copy</button>
    </div>`;
  t.appendChild(el); aiScroll();
}
function aiCopyDraft(lines) { copyText((lines || []).join('\n')); }

// ── the Assistant page (settings + what it can do) ───────────────────────────
function viewAssistant() {
  markActive('assistant');
  const s = aiSettings();
  const on = !!s.enabled;
  setContent(`
    <div class="view-head"><h2>Assistant</h2>
      <label class="ai-toggle">
        <input type="checkbox" id="aiEnable" ${on ? 'checked' : ''} onchange="aiToggleEnabled(this.checked)">
        <span>${on ? 'On' : 'Off'}</span>
      </label>
    </div>
    <p class="muted" style="max-width:60ch">An assistant that knows what you're looking at. It can explain access, summarize data, run the security audits as a report, and draft operations for you. Off by default, and it only ever reads, never changes anything on its own.</p>
    <div id="aiConfig" style="${on ? '' : 'display:none'}">
      <div class="card ai-card">
        <h3>Provider</h3>
        <div class="ai-field">
          <label>Provider</label>
          <select id="aiProvider" onchange="0">
            <option value="gemini" selected>Google Gemini</option>
          </select>
        </div>
        <div class="ai-field">
          <label>API key</label>
          <input type="password" id="aiApiKey" value="${esc(s.key || '')}" placeholder="paste your Gemini API key" autocomplete="off">
        </div>
        <div class="ai-field">
          <label>Model</label>
          <div class="ai-model-row">
            <select id="aiModel">${s.model ? `<option value="${esc(s.model)}" selected>${esc(s.model)}</option>` : '<option value="">fetch models first</option>'}</select>
            <button type="button" class="btn" onclick="aiFetchModels()">Fetch models</button>
          </div>
        </div>
        <div class="ai-field">
          <label>Custom behavior <span class="muted">(optional)</span></label>
          <textarea id="aiPersona" rows="2" placeholder="e.g. always answer in a single sentence, prefer table names over ids…">${esc(s.persona || '')}</textarea>
        </div>
        <div class="ai-privacy">${ICONS.shield || ''} Your prompt and your schema names (table, user, database names) are sent to Google. Your row data and credentials never are. Turn AI off and nothing leaves your machine.</div>
        <div class="ai-actions"><button type="button" class="btn primary" onclick="aiSaveConfig()">Save</button></div>
      </div>
    </div>`);
}

function aiToggleEnabled(on) {
  saveAiSettings({ enabled: !!on });
  document.getElementById('aiConfig').style.display = on ? '' : 'none';
  document.querySelector('.ai-toggle span').textContent = on ? 'On' : 'Off';
  mountAssistantDock();
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

function aiSaveConfig() {
  const key = document.getElementById('aiApiKey').value.trim();
  const model = document.getElementById('aiModel').value;
  const persona = document.getElementById('aiPersona').value.trim();
  saveAiSettings({ provider: 'gemini', key, model, persona, enabled: true });
  mountAssistantDock();
  toast(aiReady() ? 'Assistant is ready. The dock is bottom-right.' : 'Saved. Pick a model to finish.', aiReady() ? 'success' : 'info');
}
