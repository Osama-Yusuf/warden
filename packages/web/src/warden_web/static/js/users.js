// ── User action modals (the Users page is a hub) ──

function openResetModal(username) {
  showModal(`Reset password: ${esc(username)}`, `<div class="form-field" style="margin-bottom:0"><label>New password (blank = auto-generate)</label>
    <input id="rmPass" type="password" placeholder="auto-generate" style="width:100%"></div>`, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Reset password', cls: 'btn-primary', fn: async () => {
      const password = document.getElementById('rmPass')?.value || '';
      const res = await apiPost('/api/reset-password', { username, password });
      if (res.ok) showModal('Password reset', `<p>New password for <strong>${esc(username)}</strong>. Save it, it won't show again:</p>${passwordHtml(res.password)}`, [{ label: 'Done', cls: 'btn-primary' }]);
      else toast(res.error || 'Failed', 'error');
    }},
  ]);
}

function openTestLoginModal(username, password) {
  if (engineFamily(currentEngine) === 'sqlite') { toast('SQLite has no user accounts', 'error'); return; }
  document.getElementById('modalTitle').textContent = 'Test a login';
  document.getElementById('modalBody').innerHTML = `
    <p>Connects as this user with their own credentials and checks what they can reach. Your admin connection stays put.</p>
    <div style="display:flex; flex-direction:column; gap:10px">
      <div class="form-field"><label>Username</label><input id="tlUser" value="${esc(username || '')}" style="width:100%"></div>
      <div class="form-field"><label>Password</label><input id="tlPass" type="password" value="${esc(password || '')}" placeholder="their password" style="width:100%"></div>
      <div class="form-field"><label>Database to check (optional)</label><input id="tlDb" list="dbList" placeholder="database name" style="width:100%"></div>
    </div>
    <button class="btn btn-primary" style="margin-top:14px" onclick="doTestLogin()">Run test</button>
    <div id="tlResult" style="margin-top:14px"></div>`;
  const actEl = document.getElementById('modalActions');
  actEl.innerHTML = '';
  const b = document.createElement('button');
  b.className = 'btn btn-ghost'; b.textContent = 'Close'; b.onclick = closeModal;
  actEl.appendChild(b);
  document.getElementById('modal').classList.add('open');
  setTimeout(() => document.getElementById(password ? 'tlDb' : 'tlPass')?.focus(), 40);
}

async function doTestLogin() {
  const test_user = document.getElementById('tlUser').value.trim();
  const test_pass = document.getElementById('tlPass').value;
  const test_db = document.getElementById('tlDb').value.trim();
  if (!test_user || !test_pass) { toast('Username and password required', 'error'); return; }
  const box = document.getElementById('tlResult');
  box.innerHTML = loadingInline('Testing login…');
  const base = creds();
  const body = { env: base.env, engine: base.engine, test_user, test_pass };
  if (test_db) body.test_db = test_db;
  if (base.custom_config) body.custom_config = base.custom_config;
  let res;
  try {
    res = await fetch(API + '/api/test-login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(r => r.json());
  } catch (e) {
    box.innerHTML = `<div class="explain-box explain-danger"><div class="explain-text">Request failed: ${esc(e.message)}</div></div>`;
    return;
  }
  if (res.error) { box.innerHTML = `<div class="explain-box explain-danger"><div class="explain-text">${esc(res.error)}</div></div>`; return; }
  if (!res.auth) {
    box.innerHTML = `<div class="explain-box explain-danger"><div class="explain-text"><b>Login failed.</b> ${esc(res.error || 'Those credentials were rejected.')}</div></div>`;
    return;
  }
  let html = `<div class="ctx-line ctx-ok"><b>Login works</b> · authenticated as <span class="mono">${esc(res.identity || test_user)}</span></div>`;
  if (res.roles && res.roles.length) html += `<div style="margin:10px 0 4px; font-size:12.5px"><span style="color:var(--text-muted)">roles: </span>${res.roles.map(r => `<span class="pill pill-ok">${esc(r)}</span>`).join(' ')}</div>`;
  if (res.grants && res.grants.length) html += `<div class="table-wrap" style="margin-top:10px"><table><thead><tr><th>Grants</th></tr></thead><tbody>${res.grants.map(g => `<tr><td class="mono" style="font-size:11px">${esc(g)}</td></tr>`).join('')}</tbody></table></div>`;
  for (const c of (res.checks || [])) {
    html += `<div class="ctx-line ${c.ok ? 'ctx-ok' : 'ctx-bad'}"><b>${esc(c.name)}</b>${c.detail ? ` · <span style="color:var(--text-muted)">${esc(c.detail)}</span>` : ''}</div>`;
  }
  box.innerHTML = html;
}

function openCreateUserModal() {
  if (!requireConnection()) return;
  const fam = engineFamily(currentEngine);
  const isDocdb = fam === 'documentdb';
  let extra = '';
  if (fam === 'documentdb') {
    const roleOpts = (config.docdb_roles || []).map(r => `<option value="${esc(r)}">${esc(r)}</option>`).join('');
    extra = `<div class="form-field"><label>Initial role</label><select id="cmRole">${roleOpts}</select></div>
      <div class="form-field"><label>On database</label><input id="cmRoleDb" list="dbList" placeholder="type to search databases…"></div>`;
  } else if (fam === 'elasticsearch') {
    extra = `<div class="form-field"><label>Roles (comma-separated)</label><input id="cmEsRoles" placeholder="e.g. kibana_admin, monitoring_user"></div>`;
  } else if (fam === 'redis') {
    extra = `<div class="form-field"><label>Key pattern</label><input id="cmRdKeys" value="*" placeholder="* = all keys, or cache:*"></div>
      <div class="form-field"><label>Permissions</label><select id="cmRdLevel"><option value="read">read-only</option><option value="write">read + write</option><option value="all">all commands</option></select></div>`;
  } else if (fam !== 'mysql') {
    extra = `<div class="form-field"><label>Allow login</label><select id="cmLogin"><option value="true">Yes</option><option value="false">No</option></select></div>`;
  }
  const userPh = fam === 'mysql' ? 'name@host (host defaults to %)' : 'username';
  showPanel(`<div class="card"><h2>${ICONS.plus} Create user</h2>
    <div class="form-row">
      <div class="form-field"><label>Username</label><input id="cmUser" placeholder="${esc(userPh)}"></div>
      <div class="form-field"><label>Password (blank = generate)</label><input id="cmPass" type="password" placeholder="auto-generate"></div>
      ${extra}
    </div>
    ${isDocdb ? '<p class="card-meta">More roles can be granted afterwards from the user panel.</p>' : ''}
    <div style="display:flex; gap:8px; margin-top:6px">
      <button class="btn btn-success" onclick="doCreateFromModal()">Create user</button>
      <button class="btn btn-ghost" onclick="closePanel()">Cancel</button>
    </div>
    <div id="cmResult"></div>
  </div>`);
  setTimeout(() => document.getElementById('cmUser')?.focus(), 40);
}

async function doCreateFromModal() {
  const fam = engineFamily(currentEngine);
  const username = document.getElementById('cmUser').value.trim();
  if (!username) { toast('Username required', 'error'); return; }
  const password = document.getElementById('cmPass')?.value || '';
  const extra = {};
  if (fam === 'documentdb') {
    const role = document.getElementById('cmRole').value;
    const db = document.getElementById('cmRoleDb').value.trim();
    if (!db) { toast('Database required for the initial role', 'error'); return; }
    extra.roles = [{ role, db }];
  } else if (fam === 'elasticsearch') {
    extra.roles = document.getElementById('cmEsRoles').value.split(',').map(s => s.trim()).filter(Boolean);
  } else if (fam === 'redis') {
    extra.key_pattern = document.getElementById('cmRdKeys').value.trim() || '*';
    extra.acl_level = document.getElementById('cmRdLevel').value;
  } else if (fam !== 'mysql') {
    extra.can_login = document.getElementById('cmLogin').value === 'true';
  }
  const res = await apiPost('/api/create-user', { username, password, ...extra });
  if (res.ok) {
    refreshCache();
    const box = document.getElementById('cmResult');
    if (box) box.innerHTML = `<div style="margin-top:16px; padding-top:14px; border-top:1px solid var(--border)">
      <p style="color:var(--warning); font-weight:600; margin-bottom:6px">Saved. Copy this password now, it won't show again:</p>
      ${passwordHtml(res.password)}
      <button class="btn btn-primary btn-sm" style="margin-top:10px" data-u="${esc(username)}" data-p="${esc(res.password)}"
        onclick="openTestLoginModal(this.dataset.u, this.dataset.p)">Test this login</button></div>`;
    if (currentView === 'users') viewListUsers();
  } else {
    toast(res.error || 'Failed', 'error');
  }
}

// ── Direct actions from the user info page ──

function revokeRoleDirect(username, role, db) {
  showModal('Confirm Revoke', `<p style="color:var(--danger)">Revoke <strong>${esc(role)} on ${esc(db)}</strong> from <strong>${esc(username)}</strong>?</p>`, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Revoke', cls: 'btn-danger', fn: async () => {
      const res = await apiPost('/api/revoke', { username, roles: [{ role, db }] });
      if (res.ok) { toast(`Revoked ${role} on ${db}`, 'success'); viewUserInfoFor(username); }
      else toast(res.error || 'Failed', 'error');
    }},
  ]);
}

function grantRoleDirect(username) {
  const role = document.getElementById('uiGrantRole').value;
  const db = document.getElementById('uiGrantDb').value.trim();
  if (!db) { toast('Database required', 'error'); return; }
  showModal('Confirm Grant', `<p>Grant <strong>${esc(role)} on ${esc(db)}</strong> to <strong>${esc(username)}</strong>?</p>`, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Grant', cls: 'btn-success', fn: async () => {
      const res = await apiPost('/api/grant', { username, roles: [{ role, db }] });
      if (res.ok) { toast(`Granted ${role} on ${db}`, 'success'); viewUserInfoFor(username); }
      else toast(res.error || 'Failed', 'error');
    }},
  ]);
}

async function esGrantRole(username) {
  const role = document.getElementById('uiEsRole').value.trim();
  if (!role) { toast('Enter a role name', 'error'); return; }
  const res = await apiPost('/api/grant', { username, roles: [role] });
  if (res.ok) { toast(`Granted ${role}`, 'success'); invalidateCache('users'); viewUserInfoFor(username); }
  else toast(res.error || 'Failed', 'error');
}
function esRevokeRole(username, role) {
  showModal('Confirm Revoke', `<p style="color:var(--danger)">Revoke role <strong>${esc(role)}</strong> from <strong>${esc(username)}</strong>?</p>`, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Revoke', cls: 'btn-danger', fn: async () => {
      const res = await apiPost('/api/revoke', { username, roles: [role] });
      if (res.ok) { toast(`Revoked ${role}`, 'success'); invalidateCache('users'); viewUserInfoFor(username); }
      else toast(res.error || 'Failed', 'error');
    }},
  ]);
}
async function redisApplyRule(username) {
  const rule = document.getElementById('uiRedisRule').value.trim();
  if (!rule) { toast('Enter an ACL rule', 'error'); return; }
  const dangerous = /^-|dangerous|\ballkeys\b/i.test(rule) || rule === '~*';
  const run = async () => {
    const res = await apiPost('/api/grant', { username, rule });
    if (res.ok) { toast('ACL updated', 'success'); invalidateCache('users'); viewUserInfoFor(username); }
    else toast(res.error || 'Failed', 'error');
  };
  if (dangerous) {
    showModal('Confirm ACL change', `<p>Apply <strong class="mono">${esc(rule)}</strong> to <strong>${esc(username)}</strong>?</p>`, [
      { label: 'Cancel', cls: 'btn-ghost' }, { label: 'Apply', cls: 'btn-primary', fn: run }]);
  } else { run(); }
}

function grantPgPrivDirect(username) {
  const privilege = document.getElementById('uiGrantPriv').value;
  const database = document.getElementById('uiGrantDb').value.trim();
  // blank schema = all schemas (server resolves it), so grant finds tables
  // wherever they live, not just in public.
  const schema = document.getElementById('uiGrantSchema')?.value.trim() || '';
  if (!database) { toast('Database required', 'error'); return; }
  const scopeTxt = schema ? `${database}.${schema}` : `${database} (all schemas)`;
  showModal('Confirm Grant', `<p>Grant <strong>${esc(privilege)} on ${esc(scopeTxt)}</strong> to <strong>${esc(username)}</strong>?</p>`, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Grant', cls: 'btn-success', fn: async () => {
      const res = await apiPost('/api/grant', { username, privilege, database, schema });
      if (res.ok) {
        if (res.warning) showModal('Grant result', `<p>${esc(res.warning)}</p>`, [{ label: 'OK', cls: 'btn-primary' }]);
        else toast(res.summary || `Granted ${privilege} on ${database}`, 'success');
        viewUserInfoFor(username);
      } else toast(res.error || 'Failed', 'error');
    }},
  ]);
}

function revokePgDbPriv(username, privilege, database) {
  showModal('Confirm Revoke', `<p style="color:var(--danger)">Revoke <strong>${esc(privilege)} on ${esc(database)}</strong> from <strong>${esc(username)}</strong>?</p>`, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Revoke', cls: 'btn-danger', fn: async () => {
      const res = await apiPost('/api/revoke', { username, privilege, database });
      if (res.ok) { toast(`Revoked ${privilege} on ${database}`, 'success'); viewUserInfoFor(username); }
      else toast(res.error || 'Failed', 'error');
    }},
  ]);
}

function toggleLoginDirect(username, enable) {
  const action = enable ? 'Enable' : 'Disable';
  showModal(`${action} Login`, `<p>${action} login for <strong>${esc(username)}</strong>?</p>`, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: action, cls: enable ? 'btn-success' : 'btn-danger', fn: async () => {
      const res = await apiPost('/api/toggle-login', { username, enable });
      if (res.ok) { toast(`${action}d login for ${username}`, 'success'); viewUserInfoFor(username); }
      else toast(res.error || 'Failed', 'error');
    }},
  ]);
}

function confirmHardenConnections() {
  showModal('Lock down connections', `
    <p style="margin-bottom:8px">Postgres lets <strong>every</strong> user connect to <strong>every</strong> database by
    default (CONNECT is granted to PUBLIC). This takes that away on all databases on
    <strong>${esc(creds().env)}</strong>, so a new user only reaches the databases it's explicitly granted.</p>
    <p style="margin-bottom:0; color:var(--text-muted); font-size:12.5px">Users that already hold privileges in a database keep access. Superusers are unaffected.
    To let someone into a database afterward, grant them CONNECT.</p>
  `, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Lock it down', cls: 'btn-danger', fn: async () => {
      const res = await apiPost('/api/harden-connections', {});
      if (res.ok) { toast('Connections locked down: new users are now scoped to what they\'re granted', 'success'); invalidateCache('users'); refreshCache(); }
      else { toast(res.error || 'Failed', 'error'); }
    }},
  ]);
}

function confirmRevokeAll(username) {
  showModal('Revoke all access', `
    <p style="margin-bottom:8px">
      Revoke every privilege from <strong>${esc(username)}</strong> across all databases on
      <strong>${esc(creds().env)}</strong>? The user stays; anything they own is reassigned to the admin.
    </p>
  `, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Revoke all', cls: 'btn-danger', fn: async () => {
      const res = await apiPost('/api/revoke-all', { username });
      if (res.ok) { toast(`Revoked all access from ${username}`, 'success'); invalidateCache('users'); refreshCache(); viewUserInfoFor(username); }
      else { toast(res.error || 'Failed', 'error'); }
    }},
  ]);
}

function confirmDropUser(username) {
  showModal('Confirm Delete', `
    <p style="color:var(--danger); font-weight:600; margin-bottom:8px">
      Permanently delete user <strong>${esc(username)}</strong> from <strong>${esc(creds().env)}</strong>?
    </p>
    <div class="form-field" style="margin-bottom:0">
      <label>Type the username to confirm</label>
      <input id="confirmDrop" placeholder="${esc(username)}">
    </div>
  `, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Delete', cls: 'btn-danger', fn: async () => {
      const typed = document.getElementById('confirmDrop')?.value;
      if (typed !== username) { toast('Username does not match', 'error'); return; }
      const res = await apiPost('/api/drop-user', { username });
      if (res.ok) { toast(`User ${username} dropped`, 'success'); closePanel(); invalidateCache('users'); refreshCache(); navigate('users'); }
      else { toast(res.error || 'Failed', 'error'); }
    }},
  ]);
}

function viewCreateUser() {
  if (!requireConnection()) return;
  const fam = engineFamily(currentEngine);
  const isDocdb = fam === 'documentdb';
  const userPh = fam === 'mysql' ? 'name@host (host defaults to %)' : 'username';
  let extra = '';
  if (fam === 'mysql') {
    extra = '';
  } else if (isDocdb) {
    const roleOpts = (config.docdb_roles || []).map(r => `<option value="${esc(r)}">${esc(r)}</option>`).join('');
    extra = `
      <div class="form-field"><label>Initial role</label><select id="createRole">${roleOpts}</select></div>
      <div class="form-field"><label>On database</label><input id="createRoleDb" list="dbList" placeholder="type to search databases…"></div>`;
  } else {
    extra = `<div class="form-field"><label>Allow login</label><select id="createLogin"><option value="true">Yes</option><option value="false">No</option></select></div>`;
  }
  setContent(`<div class="card"><h2>${ICONS.plus} Create User</h2>
    <div class="form-row">
      <div class="form-field"><label>Username</label><input id="createUser" placeholder="${esc(userPh)}"></div>
      <div class="form-field"><label>Password (blank = generate)</label><input id="createPass" type="password" placeholder="auto-generate"></div>
      ${extra}
    </div>
    ${isDocdb ? '<p class="card-meta">More roles can be granted afterwards from the user’s info page.</p>' : ''}
    <button class="btn btn-success" onclick="doCreateUser()">Create User</button>
  </div>`);
}

async function doCreateUser() {
  const username = document.getElementById('createUser').value;
  if (!username) { toast('Username required', 'error'); return; }
  const password = document.getElementById('createPass')?.value || '';
  const fam = engineFamily(currentEngine);
  const isDocdb = fam === 'documentdb';
  const extra = {};
  if (fam === 'mysql') {
    // nothing extra: grants come afterwards
  } else if (isDocdb) {
    const role = document.getElementById('createRole').value;
    const db = document.getElementById('createRoleDb').value.trim();
    if (!db) { toast('Database required for the initial role', 'error'); return; }
    extra.roles = [{ role, db }];
  } else {
    extra.can_login = document.getElementById('createLogin').value === 'true';
  }
  showModal('Confirm Create User', `<p>Create user <strong>${esc(username)}</strong> on <strong>${esc(creds().env)} / ${esc(currentEngine)}</strong>?</p>`, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Create', cls: 'btn-success', fn: async () => {
      const res = await apiPost('/api/create-user', { username, password, ...extra });
      if (res.ok) {
        toast('User created', 'success');
        refreshCache();
        setContent(`<div class="card"><h2>${ICONS.plus} User Created: <span class="mono" style="font-size:14px">${esc(username)}</span></h2>
          <p style="margin-bottom:8px; color:var(--warning); font-weight:600">Save this password, it won't be shown again:</p>
          ${passwordHtml(res.password)}
          <button class="btn btn-ghost" style="margin-top:12px" onclick="navigate('users')">Back to users</button>
        </div>`);
      } else {
        toast(res.error || 'Failed', 'error');
      }
    }},
  ]);
}

function viewResetPassword(prefill = '') {
  if (!requireConnection()) return;
  setContent(`<div class="card"><h2>${ICONS.key} Reset Password</h2>
    <div class="form-row">
      <div class="form-field"><label>Username</label><input id="resetUser" list="userList" placeholder="type to search users…"></div>
      <div class="form-field"><label>New password (blank = generate)</label><input id="resetPass" type="password" placeholder="auto-generate"></div>
    </div>
    <button class="btn btn-primary" onclick="doResetPassword()">Reset Password</button>
  </div>`);
  if (prefill && typeof prefill === 'string') document.getElementById('resetUser').value = prefill;
}

async function doResetPassword() {
  const username = document.getElementById('resetUser').value;
  if (!username) { toast('Username required', 'error'); return; }
  const password = document.getElementById('resetPass')?.value || '';
  showModal('Confirm Password Reset', `<p>Reset password for <strong>${esc(username)}</strong>?</p>`, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Reset', cls: 'btn-primary', fn: async () => {
      const res = await apiPost('/api/reset-password', { username, password });
      if (res.ok) {
        toast('Password reset', 'success');
        setContent(`<div class="card"><h2>${ICONS.key} Password Reset: <span class="mono" style="font-size:14px">${esc(username)}</span></h2>
          <p style="margin-bottom:8px; color:var(--warning); font-weight:600">New password:</p>
          ${passwordHtml(res.password)}
        </div>`);
      } else {
        toast(res.error || 'Failed', 'error');
      }
    }},
  ]);
}

function viewGrant(prefill = '') {
  if (!requireConnection()) return;
  const fam = engineFamily(currentEngine);
  const isDocdb = fam === 'documentdb';
  let fields = '';
  if (fam === 'mysql') {
    const opts = (config.mysql_privileges || []).map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
    fields = `
      <div class="form-field"><label>Privilege</label><select id="grantPriv">${opts}</select></div>
      <div class="form-field"><label>Database (* = all)</label><input id="grantDb" list="dbList" placeholder="type to search, or *"></div>
    `;
  } else if (isDocdb) {
    const opts = (config.docdb_roles || []).map(r => `<option value="${esc(r)}">${esc(r)}</option>`).join('');
    fields = `
      <div class="form-field"><label>Role</label><select id="grantRole">${opts}</select></div>
      <div class="form-field"><label>Database</label><input id="grantDb" list="dbList" placeholder="type to search databases…"></div>
    `;
  } else {
    const opts = (config.pg_privileges || []).map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
    fields = `
      <div class="form-field"><label>Privilege</label><select id="grantPriv">${opts}</select></div>
      <div class="form-field"><label>Database</label><input id="grantDb" list="dbList" placeholder="type to search databases…"></div>
      <div class="form-field"><label>Schema</label><input id="grantSchema" value="public"></div>
    `;
  }
  setContent(`<div class="card"><h2>${ICONS.shield} Grant ${isDocdb ? 'Roles' : 'Privileges'}</h2>
    <div class="form-row">
      <div class="form-field"><label>Username</label><input id="grantUser" list="userList" placeholder="type to search users…"></div>
      ${fields}
    </div>
    <button class="btn btn-success" onclick="doGrant()">Grant</button>
  </div>`);
  if (prefill && typeof prefill === 'string') document.getElementById('grantUser').value = prefill;
}

async function doGrant() {
  const username = document.getElementById('grantUser').value;
  if (!username) { toast('Username required', 'error'); return; }
  const fam = engineFamily(currentEngine);
  const isDocdb = fam === 'documentdb';
  const extra = {};
  if (fam === 'mysql') {
    extra.privilege = document.getElementById('grantPriv').value;
    extra.database = document.getElementById('grantDb').value.trim() || '*';
  } else if (isDocdb) {
    extra.roles = [{ role: document.getElementById('grantRole').value, db: document.getElementById('grantDb').value }];
  } else {
    extra.privilege = document.getElementById('grantPriv').value;
    extra.database = document.getElementById('grantDb').value;
    extra.schema = document.getElementById('grantSchema')?.value || 'public';
  }
  const desc = isDocdb ? `${esc(extra.roles[0].role)} on ${esc(extra.roles[0].db)}` : `${esc(extra.privilege)} on ${esc(extra.database)}`;
  showModal('Confirm Grant', `<p>Grant <strong>${desc}</strong> to <strong>${esc(username)}</strong>?</p>`, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Grant', cls: 'btn-success', fn: async () => {
      const res = await apiPost('/api/grant', { username, ...extra });
      if (res.ok) { toast('Granted', 'success'); }
      else { toast(res.error || 'Failed', 'error'); }
    }},
  ]);
}

function viewRevoke(prefill = '') {
  if (!requireConnection()) return;
  const fam = engineFamily(currentEngine);
  const isDocdb = fam === 'documentdb';
  let fields = '';
  if (fam === 'mysql') {
    const opts = (config.mysql_privileges || []).map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
    fields = `
      <div class="form-field"><label>Privilege</label><select id="revokePriv">${opts}</select></div>
      <div class="form-field"><label>Database (* = all)</label><input id="revokeDb" list="dbList" placeholder="type to search, or *"></div>
    `;
  } else if (isDocdb) {
    const opts = (config.docdb_roles || []).map(r => `<option value="${esc(r)}">${esc(r)}</option>`).join('');
    fields = `
      <div class="form-field"><label>Role</label><select id="revokeRole">${opts}</select></div>
      <div class="form-field"><label>Database</label><input id="revokeDb" list="dbList" placeholder="type to search databases…"></div>
    `;
  } else {
    const opts = (config.pg_privileges || []).map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
    fields = `
      <div class="form-field"><label>Privilege</label><select id="revokePriv">${opts}</select></div>
      <div class="form-field"><label>Database</label><input id="revokeDb" list="dbList" placeholder="type to search databases…"></div>
      <div class="form-field"><label>Schema</label><input id="revokeSchema" value="public"></div>
    `;
  }
  setContent(`<div class="card"><h2>${ICONS.ban} Revoke ${isDocdb ? 'Roles' : 'Privileges'}</h2>
    <div class="form-row">
      <div class="form-field"><label>Username</label><input id="revokeUser" list="userList" placeholder="type to search users…"></div>
      ${fields}
    </div>
    <button class="btn btn-danger" onclick="doRevoke()">Revoke</button>
  </div>`);
  if (prefill && typeof prefill === 'string') document.getElementById('revokeUser').value = prefill;
}

async function doRevoke() {
  const username = document.getElementById('revokeUser').value;
  if (!username) { toast('Username required', 'error'); return; }
  const fam = engineFamily(currentEngine);
  const isDocdb = fam === 'documentdb';
  const extra = {};
  if (fam === 'mysql') {
    extra.privilege = document.getElementById('revokePriv').value;
    extra.database = document.getElementById('revokeDb').value.trim() || '*';
  } else if (isDocdb) {
    extra.roles = [{ role: document.getElementById('revokeRole').value, db: document.getElementById('revokeDb').value }];
  } else {
    extra.privilege = document.getElementById('revokePriv').value;
    extra.database = document.getElementById('revokeDb').value;
    extra.schema = document.getElementById('revokeSchema')?.value || 'public';
  }
  const desc = isDocdb ? `${esc(extra.roles[0].role)} on ${esc(extra.roles[0].db)}` : `${esc(extra.privilege)} on ${esc(extra.database)}`;
  showModal('Confirm Revoke', `<p style="color:var(--danger)">Revoke <strong>${desc}</strong> from <strong>${esc(username)}</strong>?</p>`, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Revoke', cls: 'btn-danger', fn: async () => {
      const res = await apiPost('/api/revoke', { username, ...extra });
      if (res.ok) { toast('Revoked', 'success'); }
      else { toast(res.error || 'Failed', 'error'); }
    }},
  ]);
}

function viewToggleLogin() {
  if (!requireConnection()) return;
  const my = engineFamily(currentEngine) === 'mysql';
  setContent(`<div class="card"><h2>${ICONS.lock} ${my ? 'Lock / Unlock Account' : 'Enable / Disable User Login'}</h2>
    <div class="form-row">
      <div class="form-field"><label>Username</label><input id="toggleUser" list="userList" placeholder="type to search users…"></div>
    </div>
    <div style="display:flex; gap:8px; margin-top:4px">
      <button class="btn btn-success" onclick="doToggleLogin(true)">${my ? 'Unlock' : 'Enable Login'}</button>
      <button class="btn btn-danger" onclick="doToggleLogin(false)">${my ? 'Lock' : 'Disable Login'}</button>
    </div>
  </div>`);
}

async function doToggleLogin(enable) {
  const username = document.getElementById('toggleUser').value;
  if (!username) { toast('Username required', 'error'); return; }
  toggleLoginDirect(username, enable);
}

function viewDropUser() {
  if (!requireConnection()) return;
  setContent(`<div class="card"><h2 style="color:var(--danger)">${ICONS.trash} Drop User</h2>
    <p class="card-meta">This permanently deletes the user. This cannot be undone.</p>
    <div class="form-row">
      <div class="form-field"><label>Username</label><input id="dropUser" list="userList" placeholder="type to search users…"></div>
    </div>
    <button class="btn btn-danger" onclick="doDropUser()">Drop User</button>
  </div>`);
}

function doDropUser() {
  const username = document.getElementById('dropUser').value;
  if (!username) { toast('Username required', 'error'); return; }
  confirmDropUser(username);
}

let dbHealth = null;
async function viewDatabases() {
  if (!requireConnection()) return;
  const key = cacheKey('databases');
  const cached = viewCache.get(key);
  if (cached) { dbData = cached.dbData; dbHealth = cached.dbHealth; dbSort = { key: 'size', dir: -1 }; renderDatabases(true); }
  else setContent(loading('Listing databases…'));
  const [health, res] = await Promise.all([
    apiPost('/api/health').catch(() => null),
    apiPost('/api/list-databases').catch(() => ({ error: 'Request failed' })),
  ]);
  if (currentView !== 'databases' || cacheKey('databases') !== key) return;
  if (res.error) { if (!cached) setContent(`<div class="card"><h2>Error</h2><p style="color:var(--danger)">${esc(res.error)}</p></div>`); return; }
  dbData = (res.databases || []).map(d => ({
    ...d,
    bytes: d.size_bytes != null ? Number(d.size_bytes) : Number(d.size_mb || 0) * 1048576,
  }));
  dbHealth = (health && !health.error) ? health : null;
  dbSort = { key: 'size', dir: -1 };
  viewCache.set(key, { dbData, dbHealth });
  renderDatabases(false);
}

function healthStripHtml() {
  if (!dbHealth || !dbHealth.health) return '';
  const h = dbHealth.health, eng = dbHealth.engine;
  const num = v => (typeof v === 'number' && isFinite(v)) ? v : (v != null && v !== '' && !isNaN(v) ? Number(v) : null);
  const chip = (k, v) => v == null || v === '' ? '' : `<div class="chip"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`;
  let chips = '';
  if (eng === 'documentdb') {
    const c = h.connections || {};
    chips += chip('version', esc(h.version || ''));
    chips += chip('connections', num(c.current) != null ? String(num(c.current)) : '');
    chips += chip('uptime', num(h.uptime) != null ? fmtUptime(h.uptime) : '');
    chips += chip('active ops', num(h.active_ops) != null ? String(h.active_ops) : '');
  } else if (eng === 'mysql') {
    chips += chip('version', esc(h.version || ''));
    chips += chip('connections', h.threads ? `${esc(h.threads)} / ${esc(h.max_connections || '?')}` : '');
    if (h.total_size) chips += chip('size', fmtSize(Number(h.total_size)));
    chips += chip('slow queries', String((h.slow_queries || []).length));
  } else if (eng === 'sqlite') {
    return '';
  } else if (eng === 'elasticsearch') {
    chips += chip('status', esc(h.status || ''));
    chips += chip('nodes', num(h.nodes) != null ? String(num(h.nodes)) : '');
    chips += chip('docs', num(h.docs) != null ? Number(h.docs).toLocaleString() : '');
    if (h.size_bytes) chips += chip('size', fmtSize(Number(h.size_bytes)));
    if (h.unassigned_shards) chips += chip('unassigned shards', String(h.unassigned_shards));
  } else if (eng === 'redis') {
    chips += chip('version', esc(h.version || ''));
    chips += chip('keys', num(h.total_keys) != null ? Number(h.total_keys).toLocaleString() : '');
    if (h.used_memory) chips += chip('memory', fmtSize(Number(h.used_memory)));
    chips += chip('clients', num(h.connected_clients) != null ? String(num(h.connected_clients)) : '');
    const hits = num(h.keyspace_hits), misses = num(h.keyspace_misses);
    if (hits != null && misses != null && (hits + misses) > 0) chips += chip('hit rate', ((hits / (hits + misses)) * 100).toFixed(1) + '%');
  } else {
    const conns = h.connections || [];
    chips += chip('version', esc(h.version?.[0] || ''));
    chips += chip('connections', conns.length >= 4 ? `${esc(conns[2])} / ${esc(conns[3])}` : '');
    chips += chip('cache hit', h.cache_hit_pct?.[0] ? `${esc(h.cache_hit_pct[0])}%` : '');
    if (h.total_size?.[0]) chips += chip('size', fmtSize(Number(h.total_size[0])));
  }
  if (!chips.trim()) return '';
  return `<div class="card" style="padding:14px 16px; margin-bottom:14px">
    <div style="display:flex; align-items:center; gap:8px; margin-bottom:10px">
      <span style="font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:0.06em; color:var(--text-muted)">Cluster health</span>
      <div style="flex:1"></div>
      <button class="btn btn-ghost btn-sm" onclick="navigate('health')">Details</button>
    </div>
    <div class="stat-strip" style="margin:0">${chips}</div></div>`;
}

function sortDatabases(key) {
  if (dbSort.key === key) dbSort.dir = -dbSort.dir;
  else dbSort = { key, dir: key === 'size' ? -1 : 1 };
  sfApply('dbs');
}

function dbFilterCfg() {
  const fam = engineFamily(currentEngine);
  const cfg = {
    key: 'dbs',
    placeholder: 'filter databases… try size:>100mb',
    rows: () => dbData,
    textFields: ['name'],
    fields: [
      { key: 'name', label: 'name', type: 'text', get: d => d.name },
      { key: 'size', label: 'size, e.g. >100mb', type: 'size', get: d => d.bytes },
    ],
    presets: [],
    render: (list) => renderDbTable(list),
  };
  if (fam === 'documentdb') {
    cfg.fields.push({ key: 'empty', label: 'empty', type: 'bool', get: d => d.empty, values: ['yes', 'no'] });
    cfg.presets = [{ label: 'Non-empty', q: 'empty:no' }, { label: 'Empty', q: 'empty:yes' }, { label: '≥ 100 MB', q: 'size:>100mb' }];
  } else if (fam === 'redis') {
    cfg.fields.push({ key: 'keys', label: 'key count', type: 'number', get: d => d.keys });
    cfg.presets = [{ label: 'Has keys', q: 'keys:>0' }];
  } else {
    cfg.presets = [{ label: '≥ 100 MB', q: 'size:>100mb' }, { label: '≥ 1 GB', q: 'size:>1gb' }];
  }
  return cfg;
}

function renderDatabases(fromCache) {
  const cfg = dbFilterCfg();
  sfRegister(cfg);
  const totalBytes = dbData.reduce((s, d) => s + d.bytes, 0);
  setContent(healthStripHtml() + `<div class="card"><h2>${ICONS.db} Databases</h2>
    <p class="card-meta">${dbData.length} databases · ${fmtSize(totalBytes)} total · click a name to browse it${fromCache ? ' · <span style="color:var(--text-muted)">refreshing…</span>' : ''}</p>
    ${sfBarHtml(cfg)}
    <div id="dbTable"></div></div>`);
  sfApply('dbs');
}

function renderDbTable(list) {
  const target = document.getElementById('dbTable'); if (!target) return;
  const arrow = k => dbSort.key === k ? `<span class="arrow">${dbSort.dir === 1 ? '▲' : '▼'}</span>` : '';
  const sorted = [...list].sort((a, b) => {
    const cmp = dbSort.key === 'size'
      ? a.bytes - b.bytes
      : a.name.toLowerCase().localeCompare(b.name.toLowerCase());
    return cmp * dbSort.dir;
  });
  const maxBytes = Math.max(1, ...list.map(d => d.bytes));
  let rows = '';
  for (const d of sorted) {
    const empty = d.empty ? '<span class="pill pill-muted">empty</span>' : '';
    const pct = Math.max(0.5, (d.bytes / maxBytes) * 100);
    rows += `<tr>
      <td class="mono" style="cursor:pointer" data-db="${esc(d.name)}" onclick="viewCollections(this.dataset.db)" data-tip="List ${currentEngine.startsWith('document') ? 'collections' : 'tables'}">${esc(d.name)}</td>
      <td style="white-space:nowrap">${fmtSize(d.bytes)}<div class="sizebar"><div style="width:${pct.toFixed(1)}%"></div></div></td>
      <td>${empty}</td>
    </tr>`;
  }
  if (!rows) { target.innerHTML = `<div class="empty-state" style="margin-top:14px">No databases match your filter.</div>`; return; }
  target.innerHTML = `<div class="table-wrap"><table>
    <thead><tr>
      <th class="sortable" onclick="sortDatabases('name')">Name${arrow('name')}</th>
      <th class="sortable" onclick="sortDatabases('size')">Size${arrow('size')}</th>
      <th></th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

function viewCollections(prefillDb = '') {
  if (!requireConnection()) return;
  const fam = engineFamily(currentEngine);
  const label = fam === 'documentdb' ? 'collections' : fam === 'elasticsearch' ? 'indices'
    : fam === 'redis' ? 'key namespaces' : 'tables';
  setContent(`<div class="card"><h2>${ICONS.grid} List ${label}</h2>
    <div class="form-row">
      <div class="form-field"><label>Database</label><input id="collDb" list="dbList" placeholder="type to search databases…"></div>
      <button class="btn btn-primary" onclick="doListCollections()">List</button>
    </div><div id="collResults"></div></div>`);
  if (prefillDb && typeof prefillDb === 'string') {
    document.getElementById('collDb').value = prefillDb;
    doListCollections();
  }
}

let collList = null;  // {database, engine, items, sort}

async function doListCollections() {
  const database = document.getElementById('collDb').value;
  if (!database) { toast('Database required', 'error'); return; }
  document.getElementById('collResults').innerHTML = loadingInline();
  const res = await apiPost('/api/list-collections', { database });
  const target = document.getElementById('collResults');
  if (!target) return;
  if (res.error) { target.innerHTML = `<p style="color:var(--danger); margin-top:12px">${esc(res.error)}</p>`; return; }
  let items = [], engine;
  if (res.collections) {
    engine = 'mongo';
    items = res.collections.map(o => typeof o === 'string'
      ? { name: o, size_bytes: null }
      : { name: o.name, size_bytes: o.size_bytes, docs: o.docs, keys: o.keys });
  } else {
    engine = 'sql';
    items = (res.tables || []).map(t => ({ schema: t.schema, name: t.table, size_bytes: t.size_bytes }));
  }
  collList = { database, engine, fam: engineFamily(currentEngine), items, sort: { key: 'size', dir: -1 } };
  renderCollList();
}

function sortCollList(key) {
  if (!collList) return;
  if (collList.sort.key === key) collList.sort.dir = -collList.sort.dir;
  else collList.sort = { key, dir: key === 'size' ? -1 : 1 };
  sfApply('coll');
}

function collFilterCfg() {
  const { engine, fam } = collList;
  const cfg = {
    key: 'coll',
    placeholder: fam === 'redis' ? 'filter namespaces… try keys:>100' : 'filter… try size:>10mb',
    rows: () => collList.items,
    textFields: engine === 'sql' ? ['name', 'schema'] : ['name'],
    fields: [
      { key: 'name', label: 'name', type: 'text', get: o => o.name },
      { key: 'size', label: 'size, e.g. >10mb', type: 'size', get: o => o.size_bytes },
    ],
    presets: [{ label: 'Non-empty', q: 'size:>0' }, { label: '≥ 10 MB', q: 'size:>10mb' }],
    render: (list) => renderCollTable(list),
  };
  if (engine === 'sql') cfg.fields.splice(1, 0, { key: 'schema', label: 'schema', type: 'text', get: o => o.schema });
  if (fam === 'elasticsearch') {
    cfg.fields.push({ key: 'docs', label: 'document count', type: 'number', get: o => o.docs });
    cfg.presets = [{ label: 'Non-empty', q: 'docs:>0' }, { label: '≥ 10 MB', q: 'size:>10mb' }];
  } else if (fam === 'redis') {
    cfg.fields.push({ key: 'keys', label: 'key count', type: 'number', get: o => o.keys });
    cfg.presets = [{ label: 'Has keys', q: 'keys:>0' }];
  }
  return cfg;
}

function renderCollList() {
  const target = document.getElementById('collResults');
  if (!target || !collList) return;
  if (!collList.items.length) {
    target.innerHTML = `<div class="empty-state" style="margin-top:12px">Nothing to show here yet</div>`;
    return;
  }
  const cfg = collFilterCfg();
  sfRegister(cfg);
  target.innerHTML = `<div style="margin-top:12px">${sfBarHtml(cfg)}</div><div id="collTable"></div>`;
  sfApply('coll');
}

function renderCollTable(list) {
  const target = document.getElementById('collTable');
  if (!target || !collList) return;
  const { database, engine, fam, sort } = collList;
  const label = fam === 'elasticsearch' ? 'Index' : fam === 'redis' ? 'Key namespace' : 'Collection';
  const qPrefill = (name) => {
    if (fam === 'elasticsearch') return { db: name, q: '{\n  "query": { "match_all": {} },\n  "size": 5\n}' };
    if (fam === 'redis') return { db: database, q: `SCAN 0 MATCH ${name === '*' || name === '(no prefix)' ? '*' : name + ':*'} COUNT 20` };
    const cr = /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(name) ? `db.${name}` : `db.getCollection(${JSON.stringify(name)})`;
    return { db: database, q: `${cr}.find({}).limit(5)` };
  };
  const act = (icon, cls, db, text, title) => `<button class="ibtn ${cls}" data-tip="${esc(title)}"
    data-db="${esc(db)}" data-q="${esc(text)}" onclick="openQueryWith(this.dataset.db, this.dataset.q)">${icon}</button>`;
  const view = (db, extra) => `<button class="ibtn accent" data-tip="Browse rows &amp; columns"
    data-db="${esc(db)}" ${extra} onclick="openDataGrid(dgSpecFrom(this.dataset))">${ICONS.grid}</button>`;
  const arrow = k => sort.key === k ? `<span class="arrow">${sort.dir === 1 ? '▲' : '▼'}</span>` : '';
  const sizeCell = s => s != null ? fmtSize(s) : '<span style="color:var(--text-muted)">-</span>';
  const sorted = [...list].sort((a, b) => {
    let av, bv;
    if (sort.key === 'size') { av = a.size_bytes ?? -1; bv = b.size_bytes ?? -1; }
    else { av = (a.name || '').toLowerCase(); bv = (b.name || '').toLowerCase(); }
    return av < bv ? -sort.dir : av > bv ? sort.dir : 0;
  });
  if (!sorted.length) {
    target.innerHTML = `<div class="empty-state" style="margin-top:12px">No ${label.toLowerCase()}s match your filter.</div>`;
    return;
  }
  let head, rows;
  if (engine === 'mongo') {
    head = `<tr>
      <th class="sortable" onclick="sortCollList('name')">${label}${arrow('name')}</th>
      <th class="sortable" onclick="sortCollList('size')">Size${arrow('size')}</th><th></th></tr>`;
    rows = sorted.map(o => {
      const qp = qPrefill(o.name);
      return `<tr>
        <td class="mono">${esc(o.name)}</td>
        <td style="white-space:nowrap">${sizeCell(o.size_bytes)}</td>
        <td><div class="row-acts">
          ${view(database, `data-coll="${esc(o.name)}"`)}
          ${act(ICONS.term, 'teal', qp.db, qp.q, 'Open in query console')}
        </div></td></tr>`;
    }).join('');
  } else {
    head = `<tr><th>Schema</th>
      <th class="sortable" onclick="sortCollList('name')">Table${arrow('name')}</th>
      <th class="sortable" onclick="sortCollList('size')">Size${arrow('size')}</th><th></th></tr>`;
    rows = sorted.map(t => {
      const full = `"${t.schema}"."${t.name}"`;
      return `<tr>
        <td class="mono">${esc(t.schema)}</td><td class="mono">${esc(t.name)}</td>
        <td style="white-space:nowrap">${sizeCell(t.size_bytes)}</td>
        <td><div class="row-acts">
          ${view(database, `data-schema="${esc(t.schema)}" data-table="${esc(t.name)}"`)}
          ${act(ICONS.term, 'teal', database, `SELECT * FROM ${full} LIMIT 5;`, 'Open in query console')}
        </div></td></tr>`;
    }).join('');
  }
  target.innerHTML = `<div class="table-wrap" style="margin-top:12px"><table><thead>${head}</thead><tbody>${rows}</tbody></table></div>`;
}

