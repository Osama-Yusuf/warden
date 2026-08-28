// ── Environments manager (custom envs live in localStorage) ───────────────

function viewEnvs() {
  const rows = envConnRows();
  const banner = connected ? '' : envOnboardBanner(rows.trim().length > 0);
  const connCount = (rows.match(/<tr/g) || []).length;
  setContent(banner + `
    <details class="sect" open>
      <summary>
        <span class="sect-ic">${ICONS.sliders}</span>
        <span class="sect-title">Connections</span>
        ${connCount ? `<span class="sect-badge">${connCount}</span>` : ''}
        <span class="sect-spacer"></span>
        <button class="ibtn accent sect-add" data-tip="Add a connection"
          onclick="event.preventDefault();event.stopPropagation();openConnForm()">${ICONS.plus}</button>
        <span class="sect-chev">${ICONS.chevron}</span>
      </summary>
      <div class="sect-body">
        <p class="card-meta" style="margin-top:0">Everything here lives in this browser only (localStorage). The server is never touched, so any change is reversible.</p>
        ${connCount ? `<div class="table-wrap"><table>
          <thead><tr><th>Name</th><th>Engine</th><th>Endpoint</th><th>Status</th><th></th></tr></thead>
          <tbody>${rows}</tbody>
        </table></div>` : `<div class="conn-empty">
          <div class="conn-empty-ic">${ICONS.db}</div>
          <p>No connections yet.</p>
          <button class="btn btn-primary btn-sm" onclick="openConnForm()">${ICONS.plus} Add a connection</button>
        </div>`}
      </div>
    </details>

    <details class="sect">
      <summary>
        <span class="sect-ic">${ICONS.db}</span>
        <span class="sect-title">Data</span>
        <span class="sect-spacer"></span>
        <span class="sect-chev">${ICONS.chevron}</span>
      </summary>
      <div class="sect-body">
        <div class="subsect">
          <h3 class="subsect-title">${ICONS.scroll} Backup &amp; Transfer</h3>
          <p class="card-meta">Move everything to the desktop app or another browser: saved credentials per environment, custom environments and overrides, audit results and exclusions, pinned queries and history, preferences. Open this same page there and import the file.</p>
          <div class="form-row" style="margin-bottom:0">
            <div class="form-field"><label>Passphrase (optional, encrypts the file)</label>
              <input id="beoPass" type="password" placeholder="recommended, the file contains passwords" style="min-width:280px"></div>
            <button class="btn btn-primary btn-sm" onclick="doExportAll()">Export everything</button>
            <button class="btn btn-ghost btn-sm" onclick="document.getElementById('beoFile').click()">Import…</button>
            <input type="file" id="beoFile" accept=".json,application/json" style="display:none" onchange="importBackupPicked(this)">
          </div>
        </div>
        <div class="subsect">
          <h3 class="subsect-title danger">${ICONS.trash} Reset</h3>
          <p class="card-meta">Wipes everything and drops you back to a fresh start: connections, saved passwords, audit results and exclusions, pinned queries, history, preferences. Export a backup first if you might want it back.</p>
          <div class="form-row" style="margin-bottom:0; align-items:center">
            <label class="chk" data-tip="Also delete ~/.warden/audit.log. Off keeps the change history."><input type="checkbox" id="wipeAudit"> also clear the activity log</label>
            <button class="btn btn-danger btn-sm" onclick="wipeAllData()">Wipe all data</button>
          </div>
        </div>
      </div>
    </details>`);
}

// One <tr> per known connection: built-in envs, engines added onto a built-in
// env, and fully custom envs. Each row carries its status pill + action buttons.
function envConnRows() {
  const details = config.environment_details || {};
  const builtinNames = Object.keys(config.environments || {});
  let rows = '';
  const actionBtn = (icon, title, fn, env, eng, cls = '') =>
    `<button class="ibtn ${cls}" data-tip="${esc(title)}" data-env="${esc(env)}" data-eng="${esc(eng)}"
      onclick="${fn}(this.dataset.env, this.dataset.eng)">${icon}</button>`;

  const isServerProfile = (env, eng) => (config.profile_envs?.[env] || []).includes(eng);
  for (const env of builtinNames) {
    for (const eng of (config.environments[env] || [])) {
      const override = customEnvs[env]?.[eng];
      const cfg = override || details[env]?.[eng] || {};
      const hidden = isHidden(env, eng);
      const server = isServerProfile(env, eng);
      const status = hidden ? '<span class="pill pill-muted">hidden</span>'
        : override ? '<span class="pill pill-warn">override</span>'
        : server ? '<span class="pill pill-accent">server profile</span>'
        : '<span class="pill pill-muted">built-in</span>';
      const actions = hidden
        ? actionBtn(ICONS.refresh, 'Restore', 'restoreEnv', env, eng, 'teal')
        : server && !override
          ? actionBtn(ICONS.pencil, 'Edit', 'editEnv', env, eng, 'accent') + actionBtn(ICONS.trash, 'Delete server profile', 'deleteServerProfile', env, eng, 'danger')
          : actionBtn(ICONS.pencil, 'Edit', 'editEnv', env, eng, 'accent')
            + (override ? actionBtn(ICONS.refresh, 'Restore built-in', 'deleteCustomEnv', env, eng, 'teal')
                        : actionBtn(ICONS.ban, 'Hide', 'hideEnv', env, eng, 'warn'));
      rows += `<tr${hidden ? ' style="opacity:0.45"' : ''}>
        <td class="mono">${esc(env)}</td>
        <td><span class="pill pill-accent">${esc(engineLabel(eng))}</span></td>
        <td class="mono" style="font-size:11px">${esc(cfg.host || '')}:${esc(cfg.port || '')}${cfg.tls ? ' · tls' : ''}</td>
        <td>${status}</td>
        <td><div class="row-acts">${actions}</div></td></tr>`;
    }
    // engines added onto a built-in env
    for (const [eng, cfg] of Object.entries(customEnvs[env] || {})) {
      if ((config.environments[env] || []).includes(eng)) continue;
      rows += `<tr><td class="mono">${esc(env)}</td>
        <td><span class="pill pill-accent">${esc(engineLabel(eng))}</span></td>
        <td class="mono" style="font-size:11px">${esc(cfg.host)}:${esc(cfg.port)}${cfg.tls ? ' · tls' : ''}</td>
        <td><span class="pill pill-warn">added</span></td>
        <td><div class="row-acts">${actionBtn(ICONS.pencil, 'Edit', 'editEnv', env, eng, 'accent')}${actionBtn(ICONS.trash, 'Delete', 'deleteCustomEnv', env, eng, 'danger')}</div></td></tr>`;
    }
  }
  for (const [env, engines] of Object.entries(customEnvs)) {
    if (builtinNames.includes(env)) continue;
    for (const [eng, cfg] of Object.entries(engines)) {
      rows += `<tr><td class="mono">${esc(env)}</td>
        <td><span class="pill pill-accent">${esc(engineLabel(eng))}</span></td>
        <td class="mono" style="font-size:11px">${esc(cfg.host)}:${esc(cfg.port)}${cfg.tls ? ' · tls' : ''}</td>
        <td><span class="pill pill-ok">custom</span></td>
        <td><div class="row-acts">${actionBtn(ICONS.pencil, 'Edit', 'editEnv', env, eng, 'accent')}${actionBtn(ICONS.trash, 'Delete', 'deleteCustomEnv', env, eng, 'danger')}</div></td></tr>`;
    }
  }
  return rows;
}

// The pre-connect welcome: a slim nudge once some connections exist, the full
// hero (with onboarding steps) when there are none yet.
function envOnboardBanner(hasAny) {
  return hasAny
    ? `<div class="card onboard"><h2>${ICONS.bolt} Ready when you are</h2>
        <p style="color:var(--text-muted); line-height:1.6; margin:0">Pick a connection and engine in the top bar, drop in your admin login, and hit <strong>Connect</strong>. warden takes it from there.</p></div>`
    : `<div class="card onboard onboard-hero">
        <div class="onboard-logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><ellipse cx="12" cy="5.5" rx="7" ry="2.8"/><path d="M5 5.5v13c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8v-13"/><path d="M5 12c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8"/></svg></div>
        <h1 class="onboard-title">Welcome to warden</h1>
        <p class="onboard-lead">It stands at the door of your databases. Who gets in, what they can touch, and a note in the logbook for every change. Works with MongoDB, PostgreSQL, MySQL and SQLite.</p>
        <div class="onboard-steps">
          <div class="ob-step"><span class="ob-num">1</span><div><b>Add a connection</b><span>Host and admin login, or just a SQLite file</span></div></div>
          <div class="ob-step"><span class="ob-num">2</span><div><b>Connect</b><span>Pick it in the top bar and hit Connect</span></div></div>
          <div class="ob-step"><span class="ob-num">3</span><div><b>Take the wheel</b><span>Manage users, run queries, audit access</span></div></div>
        </div>
        <div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:18px">
          <button class="btn btn-primary" onclick="openConnForm()">Add my first connection</button>
          <button class="btn btn-ghost" onclick="document.getElementById('beoFile').click()">Import a backup</button>
        </div></div>`;
}

function openConnForm() {
  document.getElementById('connFormTitle').innerHTML = `${ICONS.plus} Add a connection`;
  document.getElementById('ceName').value = '';
  document.getElementById('ceEngine').value = 'documentdb';
  document.getElementById('ceHost').value = '';
  document.getElementById('cePort').value = '';
  document.getElementById('ceDb').value = '';
  document.getElementById('ceTls').checked = false;
  document.getElementById('ceInsecure').checked = false;
  document.getElementById('ceEnvKind').value = '';
  const srv = document.getElementById('ceServer'); if (srv) srv.checked = false;
  ceEngineChanged();
  document.getElementById('connModal').classList.add('open');
  setTimeout(() => document.getElementById('ceName').focus(), 40);
}
function closeConnModal() { document.getElementById('connModal').classList.remove('open'); }
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('connModal').classList.contains('open')) closeConnModal();
});

async function wipeAllData() {
  const wipeAudit = document.getElementById('wipeAudit')?.checked;
  showModal('Wipe all data?', `
    <p style="color:var(--danger); font-weight:600; margin-bottom:8px">This erases everything and cannot be undone.</p>
    <p style="margin-bottom:0">Connections, saved passwords, audit results${wipeAudit ? ', the activity log' : ''}, pinned queries, history and preferences all go. warden reopens like a fresh install.</p>`, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Wipe everything', cls: 'btn-danger', fn: async () => {
      try { await apiPost('/api/wipe', { wipe_audit: !!wipeAudit }); } catch {}
      // Browser-local state
      for (const store of [localStorage, sessionStorage]) {
        for (const k of Object.keys(store)) {
          if (k.startsWith('warden.') || k.startsWith('dbctl.')) store.removeItem(k);
        }
      }
      toast('Wiped. Restarting fresh…', 'success');
      setTimeout(() => location.reload(), 700);
    }},
  ]);
}

async function reloadConfig() {
  try { config = await (await fetch(API + '/api/config')).json(); } catch {}
}

function deleteServerProfile(env, eng) {
  showModal('Delete Server Profile', `<p>Delete profile <strong>${esc(env)} / ${esc(eng)}</strong> from the server? Every browser and the CLI will lose it.</p>`, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Delete', cls: 'btn-danger', fn: async () => {
      const res = await apiPost('/api/profile-delete', { name: env, profile_engine: eng });
      if (res.error) { toast(res.error, 'error'); return; }
      await reloadConfig();
      afterEnvChange(env);
      toast('Server profile deleted', 'success');
      viewEnvs();
    }},
  ]);
}

function editEnv(env, eng) {
  const cfg = customEnvs[env]?.[eng] || config.environment_details?.[env]?.[eng] || {};
  document.getElementById('connFormTitle').innerHTML = `${ICONS.pencil} Edit ${esc(env)} / ${esc(engineLabel(eng))}`;
  document.getElementById('ceName').value = env;
  document.getElementById('ceEngine').value = eng;
  ceEngineChanged();
  document.getElementById('ceHost').value = cfg.host || cfg.path || '';
  document.getElementById('cePort').value = cfg.port || '';
  document.getElementById('ceDb').value = cfg.auth_db || cfg.default_db || '';
  document.getElementById('ceTls').checked = !!cfg.tls;
  document.getElementById('ceInsecure').checked = !!cfg.tls_insecure;
  document.getElementById('ceEnvKind').value = envKinds[env] || '';
  ceTlsChanged();
  const serverChk = document.getElementById('ceServer');
  if (serverChk) serverChk.checked = (config.profile_envs?.[env] || []).includes(eng);
  document.getElementById('connModal').classList.add('open');
  setTimeout(() => document.getElementById('ceHost').focus(), 40);
}

function hideEnv(env, eng) {
  hiddenEnvs.push(env + '/' + eng);
  saveHiddenEnvs();
  afterEnvChange(env);
  toast(`Hidden ${env} / ${eng}`, 'success');
  viewEnvs();
}

function restoreEnv(env, eng) {
  hiddenEnvs = hiddenEnvs.filter(h => h !== env + '/' + eng);
  saveHiddenEnvs();
  renderEnvOptions(document.getElementById('envSelect').value);
  toast(`Restored ${env} / ${eng}`, 'success');
  viewEnvs();
}

function afterEnvChange(env) {
  const sel = document.getElementById('envSelect');
  const cur = sel.value;
  renderEnvOptions(cur);
  // The visible selection can change here (e.g. the first connection you save
  // becomes the only option), so resync the engine dropdown to whatever is now
  // selected. Without this the engine list stays empty until you re-pick the
  // connection, and an immediate Connect fails. updateEngines() keeps the
  // current engine and never disconnects, so this is safe on edits too.
  updateEngines();
  if (cur === env) disconnected();
}

function ceEngineChanged() {
  const fam = engineFamily(document.getElementById('ceEngine').value);
  const sqlite = fam === 'sqlite';
  const noDb = sqlite || fam === 'elasticsearch' || fam === 'redis';
  document.getElementById('ceHostLabel').textContent = sqlite ? 'File path' : 'Host';
  document.getElementById('ceHost').placeholder = sqlite ? '/path/to/data.db (or upload)' : 'hostname or IP';
  document.getElementById('cePortWrap').style.display = sqlite ? 'none' : '';
  document.getElementById('ceDbWrap').style.display = noDb ? 'none' : '';
  document.getElementById('ceDbLabel').textContent = fam === 'documentdb' ? 'Auth DB' : 'Default DB';
  document.getElementById('ceDb').placeholder = fam === 'documentdb' ? 'admin' : fam === 'mysql' ? '(optional)' : 'postgres';
  const ports = { documentdb: '27017', mysql: '3306', elasticsearch: '9200', redis: '6379', postgresql: '5432' };
  document.getElementById('cePort').placeholder = ports[fam] || '5432';
  document.getElementById('ceTlsWrap').style.display = ['documentdb', 'elasticsearch', 'redis'].includes(fam) ? '' : 'none';
  const up = document.getElementById('ceUploadBtn');
  if (up) up.style.display = sqlite ? '' : 'none';
  const compat = {
    documentdb: 'Also AWS DocumentDB',
    postgresql: 'Also Aurora, Citus & other Postgres-compatible',
    mysql: 'Also MariaDB & Aurora MySQL',
    sqlite: '',
    elasticsearch: 'Also OpenSearch',
    redis: 'Also Valkey, ElastiCache & MemoryDB',
  };
  const c = document.getElementById('ceCompat');
  if (c) c.textContent = compat[fam] || '';
  ceTlsChanged();
}

// the "trust invalid cert" opt-out only makes sense when TLS is on
function ceTlsChanged() {
  const tlsWrap = document.getElementById('ceTlsWrap');
  const tls = document.getElementById('ceTls');
  const insecure = document.getElementById('ceInsecureWrap');
  const on = tlsWrap && tlsWrap.style.display !== 'none' && tls && tls.checked;
  if (insecure) insecure.style.display = on ? '' : 'none';
}

async function uploadSqlite(input) {
  const file = input.files?.[0];
  input.value = '';
  if (!file) return;
  toast(`Uploading ${file.name}…`, 'info');
  try {
    const resp = await fetch(API + '/api/sqlite-upload', {
      method: 'POST', headers: { 'X-Filename': file.name }, body: file,
    });
    const res = await resp.json();
    if (res.error) { toast(res.error, 'error'); return; }
    document.getElementById('ceHost').value = res.path;
    const nameInput = document.getElementById('ceName');
    if (nameInput && !nameInput.value.trim()) nameInput.value = res.name.replace(/\.(db|sqlite3?)$/i, '');
    toast(`Uploaded. Now hit Save to register it as an environment.`, 'success');
  } catch (e) {
    toast('Upload failed: ' + e.message, 'error');
  }
}

function addCustomEnv() {
  const name = document.getElementById('ceName').value.trim();
  const eng = document.getElementById('ceEngine').value;
  const fam = engineFamily(eng);
  const host = document.getElementById('ceHost').value.trim();
  const dbv = document.getElementById('ceDb').value.trim();
  if (!name || !/^[a-zA-Z0-9_.\-]+$/.test(name)) { toast('Name: letters, digits, _ . - only', 'error'); return; }
  let cfg;
  if (fam === 'sqlite') {
    if (!host) { toast('File path required (or upload a file)', 'error'); return; }
    cfg = { path: host };
  } else {
    const defPort = { documentdb: '27017', mysql: '3306', elasticsearch: '9200', redis: '6379' }[fam] || '5432';
    const port = parseInt(document.getElementById('cePort').value || defPort, 10);
    if (!host || !/^[a-zA-Z0-9_.\-]+$/.test(host)) { toast('Valid host required', 'error'); return; }
    if (!(port >= 1 && port <= 65535)) { toast('Valid port required', 'error'); return; }
    cfg = { host, port, tls: document.getElementById('ceTls').checked };
    if (cfg.tls && document.getElementById('ceInsecure')?.checked) cfg.tls_insecure = true;
    if (fam === 'documentdb') cfg.auth_db = dbv || 'admin';
    else if (fam === 'mysql') { if (dbv) cfg.default_db = dbv; }
    else if (fam === 'elasticsearch' || fam === 'redis') { /* host/port/tls only */ }
    else cfg.default_db = dbv || 'postgres';
  }
  // the env tag lives in browser-local state, keyed by connection name
  const envTag = document.getElementById('ceEnvKind')?.value || '';
  if (envTag) envKinds[name] = envTag; else delete envKinds[name];
  saveEnvKinds();
  if (document.getElementById('ceServer')?.checked) {
    apiPost('/api/profile-save', { name, profile_engine: eng, profile: cfg }).then(async res => {
      if (res.error) { toast(res.error, 'error'); return; }
      await reloadConfig();
      afterEnvChange(name);
      closeConnModal();
      toast(`Server profile saved: ${name} / ${eng}`, 'success');
      viewEnvs();
    });
    return;
  }
  const isOverride = !!config.environments?.[name];
  customEnvs[name] = customEnvs[name] || {};
  customEnvs[name][eng] = cfg;
  saveCustomEnvs();
  afterEnvChange(name);
  closeConnModal();
  toast(isOverride ? `Override saved for ${name} / ${eng}` : `Saved ${name} / ${eng}`, 'success');
  viewEnvs();
}

function deleteCustomEnv(env, eng) {
  const isOverride = !!config.environments?.[env];
  showModal(isOverride ? 'Restore Built-in' : 'Remove Environment',
    `<p>${isOverride ? 'Discard your override and restore the built-in settings for' : 'Remove'} <strong>${esc(env)} / ${esc(eng)}</strong>?</p>`, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: isOverride ? 'Restore' : 'Remove', cls: 'btn-danger', fn: () => {
      if (customEnvs[env]) {
        delete customEnvs[env][eng];
        if (!Object.keys(customEnvs[env]).length) delete customEnvs[env];
      }
      saveCustomEnvs();
      afterEnvChange(env);
      toast(isOverride ? 'Built-in restored' : 'Removed', 'success');
      viewEnvs();
    }},
  ]);
}

