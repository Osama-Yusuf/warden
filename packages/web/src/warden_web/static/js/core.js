const API = '';
let config = {};
let connected = false;
let currentEngine = '';
let currentView = '';
let cachedUsers = [];
let cachedDbs = [];
let dbData = [];
let dbSort = { key: 'name', dir: 1 };

// ── Custom tooltip (instant, themed — native title is slow and unstyled) ──
const tipEl = document.createElement('div');
tipEl.className = 'tooltip';
document.body.appendChild(tipEl);
function showTip(el) {
  const txt = el.getAttribute('data-tip');
  if (!txt) return;
  tipEl.textContent = txt;
  const r = el.getBoundingClientRect();
  const below = r.top < 46;
  tipEl.classList.toggle('below', below);
  const half = tipEl.offsetWidth / 2;
  const cx = Math.max(half + 6, Math.min(window.innerWidth - half - 6, r.left + r.width / 2));
  tipEl.style.left = cx + 'px';
  tipEl.style.top = (below ? r.bottom : r.top) + 'px';
  tipEl.classList.add('show');
}
function hideTip() { tipEl.classList.remove('show'); }
document.addEventListener('mouseover', e => {
  const el = e.target.closest && e.target.closest('[data-tip]');
  if (el) showTip(el); else hideTip();   // moving onto anything without a tip clears it
});
document.addEventListener('mousedown', hideTip);
document.addEventListener('scroll', hideTip, true);

// ── Icons (inline SVG, stroke = currentColor) ──
const I = p => `<svg class="ni" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
const ICONS = {
  users: I('<circle cx="9" cy="8" r="3.2"/><path d="M3 19.5c0-3.3 2.7-6 6-6s6 2.7 6 6"/><circle cx="17.5" cy="9.5" r="2.3"/><path d="M21 19.5c0-2.6-1.7-4.8-4-5.6"/>'),
  search: I('<circle cx="11" cy="11" r="6.2"/><path d="M15.8 15.8L21 21"/>'),
  plus: I('<path d="M12 5v14M5 12h14"/>'),
  key: I('<circle cx="8.5" cy="14.5" r="4"/><path d="M11.5 11.5L20 3M16.5 6.5l3 3"/>'),
  shield: I('<path d="M12 3l7.5 2.8v5.4c0 4.6-3.2 7.7-7.5 9.8-4.3-2.1-7.5-5.2-7.5-9.8V5.8z"/><path d="M8.7 12l2.4 2.4 4.2-4.8"/>'),
  ban: I('<circle cx="12" cy="12" r="8.2"/><path d="M6.3 6.3l11.4 11.4"/>'),
  lock: I('<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8.5 11V7.8a3.5 3.5 0 017 0V11"/>'),
  trash: I('<path d="M4.5 7h15M10 7V4.5h4V7M6.5 7l.9 12.5h9.2L17.5 7M10 11v5M14 11v5"/>'),
  db: I('<ellipse cx="12" cy="5.5" rx="7" ry="2.8"/><path d="M5 5.5v13c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8v-13"/><path d="M5 12c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8"/>'),
  grid: I('<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M4 9.5h16M9.5 9.5V20"/>'),
  scroll: I('<path d="M6.5 3.5h11V20l-2.75-1.8L12 20l-2.75-1.8L6.5 20z"/><path d="M9.5 8h5M9.5 11.5h5"/>'),
  bolt: I('<path d="M13 2L4.5 13.5H11L9.5 22l8.5-11.5H13z"/>'),
  pulse: I('<path d="M3 12h4l2.5-7 4 14 2.5-7h5"/>'),
  term: I('<path d="M4 17l6-5-6-5M12 19h8"/>'),
  sliders: I('<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="7" cy="18" r="2"/>'),
  unlock: I('<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8.5 11V7.5a3.5 3.5 0 016.7-1.4"/>'),
  close: I('<path d="M6 6l12 12M18 6L6 18"/>'),
  refresh: I('<path d="M20 11.5a8 8 0 10-2.3 5.4"/><path d="M20 5.5v5h-5"/>'),
  rows: I('<rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="M3.5 9.7h17M3.5 14.3h17"/>'),
  copy: I('<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 012-2h8"/>'),
  pencil: I('<path d="M4 20l1-4L16 5a2 2 0 012.8 2.8L8 18.7z"/><path d="M14.5 6.5l3 3"/>'),
  chevron: I('<path d="M9 6l6 6-6 6"/>'),
};

const TB_SVG = {
  lockOpen: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.8-1.3"/></svg>',
  lockClosed: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
  sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>',
  auto: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3v18a9 9 0 0 0 0-18z" fill="currentColor" stroke="none"/></svg>',
};

function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function plural(n, word, pluralForm) {
  return `${n} ${n === 1 ? word : (pluralForm || word + 's')}`;
}

function engineFamily(e) {
  e = (e || '').toLowerCase();
  if (e.startsWith('document') || e.includes('mongo')) return 'documentdb';
  if (e.includes('mysql') || e.includes('maria')) return 'mysql';
  if (e.includes('sqlite')) return 'sqlite';
  // redis before elasticsearch: "elasticache" contains "elastic"
  if (e.includes('redis') || e.includes('valkey') || e.includes('elasticache') || e.includes('memorydb')) return 'redis';
  if (e.includes('elastic') || e.includes('opensearch')) return 'elasticsearch';
  return 'postgresql';
}

// Pretty display name for an engine key. Only the mongo family gets renamed
// (DocumentDB speaks the Mongo protocol); custom keys like "aurora-mysql" keep
// their own name so the user's own labels survive.
function engineLabel(e) {
  const k = String(e || '');
  const l = k.toLowerCase();
  if (l.startsWith('document') || l.includes('mongo')) return 'MongoDB';
  if (l.includes('redis') || l.includes('valkey') || l.includes('elasticache') || l.includes('memorydb')) return 'Redis';
  if (l.includes('opensearch')) return 'OpenSearch';
  if (l.includes('elastic')) return 'Elasticsearch';
  return k.replace(/_/g, ' ');
}

function fmtSize(bytes) {
  if (bytes == null || isNaN(bytes)) return '—';
  let v = Number(bytes);
  if (v < 1024) return `${Math.round(v)} B`;
  for (const unit of ['KB', 'MB', 'GB', 'TB', 'PB']) {
    v /= 1024;
    if (v < 1024 || unit === 'PB') {
      const digits = v >= 100 ? 0 : v >= 10 ? 1 : 2;
      return `${v.toFixed(digits)} ${unit}`;
    }
  }
}

// ── Preferences & session persistence ──
// One-time storage migration from the tool's earlier name (dbctl.* keys)
(() => {
  for (const store of [localStorage, sessionStorage]) {
    for (const k of Object.keys(store)) {
      if (k.startsWith('dbctl.')) {
        const nk = 'warden.' + k.slice(6);
        if (store.getItem(nk) == null) store.setItem(nk, store.getItem(k));
        store.removeItem(k);
      }
    }
  }
})();

const PREFS_KEY = 'warden.prefs';
const PASS_KEY = 'warden.k';
let prefs = (() => { try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || {}; } catch { return {}; } })();
function savePrefs(patch) {
  prefs = { ...prefs, ...patch };
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch {}
}
// Credentials are stored PER TARGET (env + engine): username and remember flag
// in warden.creds, the password under a per-target key. Legacy single-slot
// passwords are kept as a read fallback.
function targetKey() {
  return `${document.getElementById('envSelect')?.value || ''}:${currentEngine}`;
}
function credsStore() { try { return JSON.parse(localStorage.getItem('warden.creds')) || {}; } catch { return {}; } }
function saveTargetCreds(user, remember) {
  const s = credsStore();
  s[targetKey()] = { user, remember: !!remember };
  try { localStorage.setItem('warden.creds', JSON.stringify(s)); } catch {}
}
function targetCreds() { return credsStore()[targetKey()] || null; }

function savePassword(pwd) {
  try {
    const enc = btoa(unescape(encodeURIComponent(pwd)));
    const key = PASS_KEY + '.' + targetKey();
    if (document.getElementById('rememberChk').checked) {
      localStorage.setItem(key, enc); sessionStorage.removeItem(key);
    } else {
      sessionStorage.setItem(key, enc); localStorage.removeItem(key);
    }
  } catch {}
}
function loadPassword() {
  try {
    const key = PASS_KEY + '.' + targetKey();
    const v = sessionStorage.getItem(key) || localStorage.getItem(key)
      || sessionStorage.getItem(PASS_KEY) || localStorage.getItem(PASS_KEY); // legacy slot
    return v ? decodeURIComponent(escape(atob(v))) : '';
  } catch { return ''; }
}
function clearPassword() {
  const key = PASS_KEY + '.' + targetKey();
  sessionStorage.removeItem(key); localStorage.removeItem(key);
  sessionStorage.removeItem(PASS_KEY); localStorage.removeItem(PASS_KEY);
}

async function loadCredsForTarget() {
  // Fill the credential fields for the selected env/engine. Never connects
  // on its own; connecting is always an explicit click.
  const userInput = document.getElementById('adminUser');
  const passInput = document.getElementById('adminPass');
  if (!userInput || !passInput) return;
  renderConnState();
  const sqliteFam = engineFamily(currentEngine) === 'sqlite';
  userInput.disabled = sqliteFam;
  passInput.disabled = sqliteFam;
  document.getElementById('rememberChk').disabled = sqliteFam;
  const kb = document.getElementById('keychainBtn');
  if (kb) kb.style.display = (config.tools?.keychain && !sqliteFam) ? '' : 'none';
  if (sqliteFam) { userInput.value = ''; passInput.value = ''; return; }
  const saved = targetCreds();
  userInput.value = saved?.user || prefs.adminUser || config.defaults?.admin_user || '';
  document.getElementById('rememberChk').checked = saved ? !!saved.remember : !!prefs.remember;
  passInput.value = loadPassword();
  if (!passInput.value && userInput.value.trim()) await keychainAutofill();
}

// ── Theme (system → dark → light) ──
function applyTheme(t) {
  const root = document.documentElement;
  if (t === 'dark' || t === 'light') root.dataset.theme = t;
  else delete root.dataset.theme;
  const btn = document.getElementById('themeBtn');
  if (btn) {
    btn.innerHTML = t === 'dark' ? TB_SVG.moon : t === 'light' ? TB_SVG.sun : TB_SVG.auto;
    btn.title = 'Theme: ' + (t === 'system' ? 'follows system' : t) + ' (click to change)';
  }
}
function cycleTheme() {
  const order = ['system', 'dark', 'light'];
  const next = order[(order.indexOf(prefs.theme || 'system') + 1) % order.length];
  savePrefs({ theme: next });
  applyTheme(next);
}

// ── Environment overlay (browser-local): custom envs, overrides of
//    built-ins, and hidden built-in engines. Sent to the server per-request. ──
const CUSTOM_ENVS_KEY = 'warden.customEnvs';
const HIDDEN_ENVS_KEY = 'warden.hiddenEnvs';
let customEnvs = (() => { try { return JSON.parse(localStorage.getItem(CUSTOM_ENVS_KEY)) || {}; } catch { return {}; } })();
let hiddenEnvs = (() => { try { return JSON.parse(localStorage.getItem(HIDDEN_ENVS_KEY)) || []; } catch { return []; } })();
function saveCustomEnvs() { try { localStorage.setItem(CUSTOM_ENVS_KEY, JSON.stringify(customEnvs)); } catch {} }
function saveHiddenEnvs() { try { localStorage.setItem(HIDDEN_ENVS_KEY, JSON.stringify(hiddenEnvs)); } catch {} }
function isHidden(env, eng) { return hiddenEnvs.includes(env + '/' + eng); }

function envEngines(env) {
  const builtin = (config.environments?.[env] || []).filter(e => !isHidden(env, e));
  const custom = Object.keys(customEnvs[env] || {}).filter(e => !builtin.includes(e));
  return [...builtin, ...custom];
}
function envConfigFor(env, engine) {
  // An entry in customEnvs wins. It's either a custom env or an override of a built-in.
  return customEnvs[env]?.[engine] || null;
}
function envVisible(env) { return envEngines(env).length > 0; }
function renderEnvOptions(selected) {
  const envSel = document.getElementById('envSelect');
  const builtins = Object.keys(config.environments || {});
  const names = [...builtins];
  for (const env of Object.keys(customEnvs)) if (!builtins.includes(env)) names.push(env);
  let html = '';
  for (const env of names) {
    if (!envVisible(env)) continue;
    html += `<option value="${esc(env)}">${esc(env)}</option>`;
  }
  envSel.innerHTML = html;
  if (selected && envVisible(selected)) envSel.value = selected;
}

// ── Autocomplete cache: feeds search dropdowns, quick-jump & badges ──
async function refreshCache() {
  if (!connected) return;
  try {
    const [u, d] = await Promise.all([
      apiPost('/api/list-users'),
      apiPost('/api/list-databases'),
    ]);
    cachedUsers = (u.users || []).map(x => x.user).filter(Boolean).sort();
    cachedDbs = (d.databases || []).map(x => x.name).filter(Boolean).sort();
    document.getElementById('userList').innerHTML = cachedUsers.map(n => `<option value="${esc(n)}">`).join('');
    document.getElementById('dbList').innerHTML = cachedDbs.map(n => `<option value="${esc(n)}">`).join('');
    renderBadges();
  } catch (e) { /* autocomplete is best-effort */ }
}

function renderBadges() {
  const bu = document.getElementById('badge-users');
  const bd = document.getElementById('badge-databases');
  if (bu) bu.textContent = cachedUsers.length || '';
  if (bd) bd.textContent = cachedDbs.length || '';
  if (bu) bu.style.display = cachedUsers.length ? '' : 'none';
  if (bd) bd.style.display = cachedDbs.length ? '' : 'none';
  const ba = document.getElementById('badge-audits');
  if (ba) {
    const n = connected ? auditIssueCount() : 0;
    ba.textContent = n || '';
    ba.style.display = n ? '' : 'none';
    ba.style.color = n ? 'var(--danger)' : '';
  }
}

// ── Init ──
async function init() {
  applyTheme(prefs.theme || 'system');
  const r = await fetch(API + '/api/config');
  config = await r.json();
  const envSel = document.getElementById('envSelect');
  const defaults = config.defaults || {};
  const startEnv =
    (prefs.env && envVisible(prefs.env)) ? prefs.env
    : (defaults.env && envVisible(defaults.env)) ? defaults.env : '';
  renderEnvOptions(startEnv);
  envSel.onchange = () => { savePrefs({ env: envSel.value }); disconnected(); updateEngines(); loadCredsForTarget(); };
  updateEngines(true);

  document.getElementById('adminPass').addEventListener('keydown', e => { if (e.key === 'Enter') testConnection(); });
  document.getElementById('rememberChk').addEventListener('change', e => savePrefs({ remember: e.target.checked }));
  if (config.tools?.keychain) document.getElementById('keychainBtn').style.display = '';
  renderRoBtn();

  // Prefill the saved credentials for this target. Connecting stays manual
  await loadCredsForTarget();
  renderWelcome();
}

function updateEngines(restoring = false) {
  const env = document.getElementById('envSelect').value;
  const engSel = document.getElementById('engineSelect');
  const engines = envEngines(env) || [];
  engSel.innerHTML = '';
  for (const e of engines) {
    engSel.innerHTML += `<option value="${esc(e)}">${esc(engineLabel(e))}</option>`;
  }
  // Keep the currently selected engine when the new env also has it, since
  // switching env shouldn't silently flip postgresql back to documentdb.
  const previous = currentEngine;
  currentEngine = engines.includes(previous) ? previous : (engines[0] || '');
  if (restoring && prefs.engine && engines.includes(prefs.engine)) {
    currentEngine = prefs.engine;
  }
  engSel.value = currentEngine;
  savePrefs({ engine: currentEngine });
  engSel.onchange = () => {
    currentEngine = engSel.value;
    savePrefs({ engine: currentEngine });
    disconnected(); renderNav(); renderWelcome();
    loadCredsForTarget();
  };
  renderNav();
}

// ── Sidebar: data-driven nav ──
function navModel() {
  const fam = engineFamily(currentEngine);
  if (fam === 'sqlite') {
    return [
      { id: 'databases', icon: ICONS.db, label: 'Databases', badge: 'databases' },
      { id: 'query', icon: ICONS.term, label: 'Query' },
      { id: 'audit', icon: ICONS.scroll, label: 'Activity', always: true },
      { id: 'envs', icon: ICONS.sliders, label: 'Connections', always: true },
    ];
  }
  return [
    { id: 'users', icon: ICONS.users, label: 'Users', badge: 'users' },
    { id: 'databases', icon: ICONS.db, label: 'Databases', badge: 'databases' },
    { id: 'query', icon: ICONS.term, label: 'Query' },
    { id: 'audits', icon: ICONS.shield, label: 'Audits', badge: 'audits' },
    { id: 'audit', icon: ICONS.scroll, label: 'Activity', always: true },
    { id: 'envs', icon: ICONS.sliders, label: 'Connections', always: true },
  ];
}

const VIEWS = {
  users: () => viewListUsers(),
  databases: () => viewDatabases(),
  collections: () => viewCollections(),
  health: () => viewHealth(),
  query: () => viewQuery(),
  audits: () => viewAudits(),
  audit: () => viewAuditLog(),
  envs: () => viewEnvs(),
};

function renderNav() {
  const box = document.getElementById('navSections');
  let html = '';
  for (const item of navModel()) {
    const dis = (connected || item.always) ? '' : 'disabled';
    const badge = item.badge ? `<span class="nav-badge" id="badge-${item.badge}" style="display:none"></span>` : '';
    html += `<button id="nav-${item.id}" class="nav-item" ${dis}
      onclick="navigate('${item.id}')">${item.icon}<span>${esc(item.label)}</span>${badge}</button>`;
  }
  box.innerHTML = html;
  markActive(currentView);
  renderBadges();
  renderSidebarFoot();
  const qf = document.getElementById('qfInput');
  qf.disabled = !connected;
  qf.placeholder = connected ? 'Jump to a user, db, or table…' : 'Connect to search…';
}

function renderSidebarFoot() {
  const foot = document.getElementById('sidebarFoot');
  if (connected) {
    foot.innerHTML = `${ICONS.bolt}<span>${esc(document.getElementById('envSelect').value)} · ${esc(engineLabel(currentEngine))}</span>`;
  } else {
    foot.innerHTML = `${ICONS.lock}<span>connect to unlock operations</span>`;
  }
}

function navigate(id) {
  const fn = VIEWS[id];
  if (!fn) return;
  currentView = id;
  savePrefs({ lastView: id });
  renderNav();
  fn();
}

function markActive(id) {
  document.querySelectorAll('.nav-item.active').forEach(el => el.classList.remove('active'));
  const el = document.getElementById('nav-' + id);
  if (el) el.classList.add('active');
  currentView = id || currentView;
}

// ── Quick-jump search ──
// tables/collections already fetched this session (collCache), matching q
function qfCollHits(q) {
  const hits = [];
  for (const [db, names] of Object.entries(collCache)) {
    if (!Array.isArray(names)) continue;
    for (const c of names) {
      if (c.toLowerCase().includes(q)) hits.push({ db, coll: c });
      if (hits.length >= 8) return hits;
    }
  }
  return hits;
}

function qfRender() {
  const input = document.getElementById('qfInput');
  const box = document.getElementById('qfResults');
  const q = input.value.trim().toLowerCase();
  if (!q || !connected) { box.classList.remove('open'); box.innerHTML = ''; return; }
  // warm the collection cache (bounded) so tables become searchable, re-render as they land
  let started = 0;
  for (const db of cachedDbs) {
    if (collCache[db] === undefined && !collFetching[db] && started < 30) {
      started++;
      fetchColls(db).then(() => { if (box.classList.contains('open')) qfRender(); });
    }
  }
  const us = cachedUsers.filter(n => n.toLowerCase().includes(q)).slice(0, 6);
  const ds = cachedDbs.filter(n => n.toLowerCase().includes(q)).slice(0, 6);
  const cs = qfCollHits(q);
  if (!us.length && !ds.length && !cs.length) {
    box.innerHTML = `<div class="qf-hit" style="cursor:default;color:var(--text-muted)">no matches</div>`;
    box.classList.add('open');
    return;
  }
  const objLabel = engineFamily(currentEngine) === 'documentdb' ? 'collection'
    : engineFamily(currentEngine) === 'elasticsearch' ? 'index'
    : engineFamily(currentEngine) === 'redis' ? 'keys' : 'table';
  let html = '';
  for (const n of us) html += `<button class="qf-hit" data-name="${esc(n)}" onclick="qfGo('user', this.dataset.name)">${ICONS.users}<span>${esc(n)}</span><span class="tag">user</span></button>`;
  for (const n of ds) html += `<button class="qf-hit" data-name="${esc(n)}" onclick="qfGo('db', this.dataset.name)">${ICONS.db}<span>${esc(n)}</span><span class="tag">db</span></button>`;
  for (const h of cs) html += `<button class="qf-hit" data-db="${esc(h.db)}" data-coll="${esc(h.coll)}" onclick="qfGoColl(this.dataset.db, this.dataset.coll)">${ICONS.grid}<span>${esc(h.coll)}</span><span class="tag">${esc(objLabel)} · ${esc(h.db)}</span></button>`;
  box.innerHTML = html;
  box.classList.add('open');
}
function qfGo(type, name) {
  const input = document.getElementById('qfInput');
  input.value = '';
  qfRender();
  if (type === 'user') { markActive('users'); viewUserInfoFor(name); }
  else { markActive('databases'); viewCollections(name); }
}
// jump to a table/collection: open its database's list, filtered to that object
function qfGoColl(db, coll) {
  document.getElementById('qfInput').value = '';
  qfRender();
  markActive('databases');
  viewCollections(db);
  setTimeout(() => { const inp = document.getElementById('sf-in-coll'); if (inp) { inp.value = coll; sfApply('coll'); } }, 750);
}
document.addEventListener('DOMContentLoaded', () => {
  const qf = document.getElementById('qfInput');
  qf.addEventListener('input', qfRender);
  qf.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      const first = document.querySelector('#qfResults .qf-hit[data-name]');
      if (first) first.click();
    } else if (e.key === 'Escape') { qf.value = ''; qfRender(); qf.blur(); }
  });
  document.addEventListener('click', e => {
    if (!e.target.closest('.qf-wrap')) document.getElementById('qfResults').classList.remove('open');
  });
});

// ── Helpers ──
function creds() {
  const env = document.getElementById('envSelect').value;
  const body = {
    env,
    engine: currentEngine,
    admin_user: document.getElementById('adminUser').value,
    admin_pass: document.getElementById('adminPass').value,
  };
  const override = envConfigFor(env, currentEngine);
  if (override) body.custom_config = override;
  if (prefs.readOnly) body.read_only = true;
  return body;
}

// ── Read-only mode ──
function toggleReadOnly() {
  // an explicit choice by the user overrides (and clears) the prod auto-seatbelt
  savePrefs({ readOnly: !prefs.readOnly, roAuto: false });
  renderRoBtn();
  renderNav();
  toast(prefs.readOnly ? 'Read-only mode ON, the server now refuses all writes' : 'Read-only mode off', prefs.readOnly ? 'info' : 'success');
}
function renderRoBtn() {
  const btn = document.getElementById('roBtn');
  if (!btn) return;
  btn.classList.toggle('on', !!prefs.readOnly);
  btn.innerHTML = prefs.readOnly ? TB_SVG.lockClosed : TB_SVG.lockOpen;
  btn.title = prefs.readOnly
    ? 'Read-only mode is ON. Click to allow writes again.'
    : 'Read-only mode: click to refuse every write (safe for poking around prod).';
}

// ── Environment awareness (prod / staging / dev) ──
const ENVKINDS_KEY = 'warden.envKinds';
let envKinds = (() => { try { return JSON.parse(localStorage.getItem(ENVKINDS_KEY)) || {}; } catch { return {}; } })();
// prefs.roAuto tracks whether read-only was auto-enabled for prod (persisted, so a
// page reload doesn't strand the seatbelt on when you next connect to a non-prod env)
function saveEnvKinds() { try { localStorage.setItem(ENVKINDS_KEY, JSON.stringify(envKinds)); } catch {} }

// a connection's env tag: explicit (set in the connection form) or inferred from name
function envKind(name) {
  if (envKinds[name]) return envKinds[name];
  const n = (name || '').toLowerCase();
  if (/\buat\b/.test(n)) return 'uat';
  if (/\bqa\b/.test(n)) return 'qa';
  if (/stag|preprod|pre-prod/.test(n)) return 'staging';
  if (/prod/.test(n)) return 'prod';
  if (/local/.test(n)) return 'local';
  if (/dev|test|sandbox/.test(n)) return 'dev';
  return null;
}

// which tier a tag belongs to — drives the colour and the prod read-only guardrail
function envTier(tag) {
  if (tag === 'prod') return 'prod';
  if (['staging', 'uat', 'qa', 'preprod'].includes(tag)) return 'staging';
  if (['dev', 'local', 'sandbox', 'test'].includes(tag)) return 'dev';
  return null;
}

// paint the top bar for the connected environment + apply the prod read-only seatbelt
function applyEnvGuard(name) {
  const tag = envKind(name);
  const tier = envTier(tag);
  const badge = document.getElementById('envBadge');
  document.body.classList.remove('env-prod', 'env-staging', 'env-dev');
  if (tier && badge) {
    document.body.classList.add('env-' + tier);
    badge.className = 'env-badge ' + tier;
    badge.textContent = tag;
    badge.style.display = '';
  } else if (badge) {
    badge.style.display = 'none';
  }
  if (tier === 'prod' && !prefs.readOnly) {
    savePrefs({ readOnly: true, roAuto: true }); renderRoBtn(); renderNav();
    toast('Connected to PROD — read-only is on as a safety net. Click the lock to allow writes.', 'info');
  } else if (tier !== 'prod' && prefs.roAuto && prefs.readOnly) {
    savePrefs({ readOnly: false, roAuto: false }); renderRoBtn(); renderNav();
  }
}

function clearEnvGuard() {
  document.body.classList.remove('env-prod', 'env-staging', 'env-dev');
  const badge = document.getElementById('envBadge'); if (badge) badge.style.display = 'none';
  if (prefs.roAuto && prefs.readOnly) { savePrefs({ readOnly: false, roAuto: false }); renderRoBtn(); }
}

// Show credential inputs only while disconnected; the row stays calm when live.
function renderConnState() {
  const sqliteFam = engineFamily(currentEngine) === 'sqlite';
  const creds = document.getElementById('credsCluster');
  const connectBtn = document.getElementById('connectBtn');
  const disconnBtn = document.getElementById('disconnBtn');
  const pill = document.getElementById('connPill');
  if (creds) creds.style.display = (connected || sqliteFam) ? 'none' : '';
  if (connectBtn) connectBtn.style.display = connected ? 'none' : '';
  if (disconnBtn) disconnBtn.style.display = connected ? '' : 'none';
  if (pill) pill.style.display = '';  // always show status
}

// ── macOS Keychain (server-mediated) ──
function currentHost() {
  const env = document.getElementById('envSelect').value;
  return envConfigFor(env, currentEngine)?.host
    || config.environment_details?.[env]?.[currentEngine]?.host || '';
}
async function saveToKeychain() {
  const host = currentHost();
  const user = document.getElementById('adminUser').value.trim();
  const password = document.getElementById('adminPass').value;
  if (!host || !user || !password) { toast('Need env, username and password first', 'error'); return; }
  const res = await apiPost('/api/keychain-save', { host, user, password });
  if (res.ok) toast(`Password saved to Keychain for ${user}@${host.split('.')[0]}`, 'success');
  else toast(res.error || 'Keychain save failed', 'error');
}
async function keychainAutofill() {
  if (!config.tools?.keychain) return false;
  const host = currentHost();
  const user = document.getElementById('adminUser').value.trim();
  if (!host || !user || document.getElementById('adminPass').value) return false;
  try {
    const res = await apiPost('/api/keychain-get', { host, user });
    if (res.ok && res.password) {
      document.getElementById('adminPass').value = res.password;
      return true;
    }
  } catch {}
  return false;
}

function requireConnection() {
  if (!connected) {
    toast('Connect first: enter credentials and click Connect', 'error');
    return false;
  }
  return true;
}

async function apiPost(path, extra = {}) {
  const body = { ...creds(), ...extra };
  const r = await fetch(API + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json();
}

function toast(msg, type = 'info') {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function setContent(html) {
  if (window._viewTimer) { clearInterval(window._viewTimer); window._viewTimer = null; }
  if (typeof hideTip === 'function') hideTip();   // clear any tooltip whose anchor is being replaced
  document.getElementById('content').innerHTML = html;
}

function loading(msg = 'Loading…') {
  return `<div class="card">
    <div class="loading-msg"><div class="spinner"></div> ${esc(msg)}</div>
    <div class="skel" style="width:55%"></div>
    <div class="skel" style="width:82%"></div>
    <div class="skel" style="width:68%"></div>
    <div class="skel" style="width:74%; margin-bottom:0"></div>
  </div>`;
}

function loadingInline(msg = 'Loading…') {
  return `<div class="loading-msg"><div class="spinner"></div> ${esc(msg)}</div>`;
}

function copyText(text) {
  navigator.clipboard.writeText(text);
  toast('Copied to clipboard', 'success');
}

function passwordHtml(pwd) {
  return `<div class="password-box">
    <code>${esc(pwd)}</code>
    <button class="copy-btn" data-clip="${esc(pwd)}" onclick="copyText(this.dataset.clip)" data-tip="Copy">📋</button>
  </div>`;
}

function showModal(title, bodyHtml, actions) {
  document.getElementById('modalTitle').textContent = title;
  document.getElementById('modalBody').innerHTML = bodyHtml;
  const actEl = document.getElementById('modalActions');
  actEl.innerHTML = '';
  for (const a of actions) {
    const btn = document.createElement('button');
    btn.className = `btn ${a.cls || 'btn-ghost'}`;
    btn.textContent = a.label;
    btn.onclick = () => { closeModal(); if (a.fn) a.fn(); };
    actEl.appendChild(btn);
  }
  document.getElementById('modal').classList.add('open');
}
function closeModal() { document.getElementById('modal').classList.remove('open'); }

function showPanel(bodyHtml) {
  document.getElementById('panelBody').innerHTML = bodyHtml;
  document.getElementById('panelModal').classList.add('open');
}
function closePanel() { document.getElementById('panelModal').classList.remove('open'); }

function disconnected() {
  connected = false;
  cachedUsers = [];
  cachedDbs = [];
  collCache = {};
  collFetching = {};
  if (typeof viewCache !== 'undefined') viewCache.clear();
  document.getElementById('userList').innerHTML = '';
  document.getElementById('dbList').innerHTML = '';
  document.getElementById('connDot').className = 'conn-dot';
  document.getElementById('connText').textContent = 'offline';
  document.getElementById('qfResults').classList.remove('open');
  clearEnvGuard();
  renderConnState();
}
function doDisconnect() {
  clearPassword();
  document.getElementById('adminPass').value = '';
  disconnected();
  renderNav();
  renderWelcome();
  toast('Disconnected, saved password cleared', 'success');
}

// ── Connection ──
async function testConnection() {
  const u = document.getElementById('adminUser').value.trim();
  const p = document.getElementById('adminPass').value.trim();
  // SQLite has no auth; Elasticsearch/Redis may be unauthenticated or password-only.
  const optionalAuth = ['sqlite', 'elasticsearch', 'redis'].includes(engineFamily(currentEngine));
  if (!optionalAuth && (!u || !p)) { toast('Enter admin username and password', 'error'); return; }
  const dot = document.getElementById('connDot');
  const txt = document.getElementById('connText');
  dot.className = 'conn-dot'; txt.textContent = 'connecting…';
  const requestTarget = targetKey();
  try {
    const t0 = performance.now();
    const res = await apiPost('/api/connect');
    const rtt = Math.round(performance.now() - t0);
    if (targetKey() !== requestTarget) return; // user switched away mid-connect
    if (res.ok) {
      dot.className = 'conn-dot ok';
      txt.textContent = `${res.user ? res.user + ' @ ' : ''}${res.host || 'connected'} · ${rtt}ms`;
      txt.title = `${res.user || ''} @ ${res.host || ''}, connect round-trip ${rtt}ms`;
      connected = true;
      renderConnState();
      savePrefs({ adminUser: u, env: document.getElementById('envSelect').value, engine: currentEngine });
      saveTargetCreds(u, document.getElementById('rememberChk').checked);
      savePassword(p);
      renderNav();
      toast(`Connected · ${document.getElementById('envSelect').value} / ${engineLabel(currentEngine)}`, 'success');
      applyEnvGuard(document.getElementById('envSelect').value);
      refreshCache();
      const ids = navModel().map(i => i.id);
      const landing = (prefs.lastView && ids.includes(prefs.lastView)) ? prefs.lastView : ids[0];
      navigate(landing);
    } else {
      dot.className = 'conn-dot fail';
      txt.textContent = 'auth failed';
      connected = false;
      renderNav();
      toast(res.error || 'Connection failed', 'error');
    }
  } catch (e) {
    dot.className = 'conn-dot fail';
    txt.textContent = 'error';
    connected = false;
    renderNav();
    toast('Server unreachable', 'error');
  }
}

// ── Landing: the Connections page (never a blank screen) ──
function renderWelcome() {
  currentView = 'envs';
  renderNav();
  viewEnvs();
}

// ── Views ──

let allUsers = [];
let usersLoading = false;
let usersNote = '';
// Stale-while-revalidate cache: show the last result instantly, refresh behind it.
const viewCache = new Map();
function cacheKey(kind) { return `${document.getElementById('envSelect')?.value}:${currentEngine}:${kind}`; }
function invalidateCache(kind) { viewCache.delete(cacheKey(kind)); }

