// ── Smart filter ──────────────────────────────────────────────────────────
// Config-driven, client-side. Views load their full list, so filtering runs
// in memory (instant, no round-trips). A config declares fields (how to read a
// value off a row + its type) and preset chips. The engine parses `field:value`
// tokens with operators (size:>100mb), bare-text substring, an autocomplete
// dropdown, active-filter chips, and a live "X of Y" count.
const sfReg = {};                 // key -> config
const sfAc = {};                  // key -> {sel, opts}

function sfRegister(cfg) { sfReg[cfg.key] = cfg; sfAc[cfg.key] = { sel: -1, opts: [] }; }
function sfInputVal(key) { return document.getElementById('sf-in-' + key)?.value || ''; }
function sfTokens(str) { return str.match(/(?:[^\s"]+|"[^"]*")+/g) || []; }

function sfParseSize(s) {
  const m = String(s).trim().toLowerCase().match(/^([\d.]+)\s*(b|kb|k|mb|m|gb|g|tb|t)?$/);
  if (!m) return null;
  const mult = { b: 1, k: 1024, kb: 1024, m: 1048576, mb: 1048576, g: 1073741824, gb: 1073741824, t: 1099511627776, tb: 1099511627776 };
  return parseFloat(m[1]) * (mult[m[2] || 'b']);
}

function sfParse(q, fields) {
  const known = new Set(fields.map(f => f.key));
  const tokens = [], textParts = [];
  for (const p of sfTokens(q)) {
    const m = p.match(/^(-?)([A-Za-z_]+):(.*)$/);
    if (m && known.has(m[2].toLowerCase())) {
      let val = m[3], op = '';
      const om = val.match(/^(>=|<=|>|<|=)(.*)$/);
      if (om) { op = om[1]; val = om[2]; }
      tokens.push({ field: m[2].toLowerCase(), op, val: val.replace(/^"|"$/g, ''), neg: m[1] === '-', raw: p });
    } else {
      textParts.push(p.replace(/^"|"$/g, ''));
    }
  }
  return { tokens, text: textParts.join(' ').trim().toLowerCase() };
}

function sfVals(f, row) {
  const v = f.get(row);
  if (v == null || v === '') return [];
  return Array.isArray(v) ? v.map(String) : [String(v)];
}

function sfTokenMatch(f, tok, row) {
  const raw = f.get(row);
  if (f.type === 'bool') {
    const truthy = ['on', 'true', 'yes', '1', 'active', 'enabled'].includes(tok.val.toLowerCase());
    return Boolean(raw) === truthy;
  }
  if (f.type === 'size' || f.type === 'number') {
    const n = Number(raw);
    const target = f.type === 'size' ? sfParseSize(tok.val) : Number(tok.val);
    if (raw == null || isNaN(n) || target == null || isNaN(target)) return false;
    switch (tok.op) {
      case '>': return n > target;  case '<': return n < target;
      case '>=': return n >= target; case '<=': return n <= target;
      default: return n === target;
    }
  }
  const needle = tok.val.toLowerCase();
  return sfVals(f, row).some(s => s.toLowerCase().includes(needle));
}

function sfMatches(row, parsed, cfg) {
  if (parsed.text) {
    const hit = cfg.textFields.some(k => {
      const f = cfg.fields.find(x => x.key === k);
      return f && sfVals(f, row).some(s => s.toLowerCase().includes(parsed.text));
    });
    if (!hit) return false;
  }
  for (const tok of parsed.tokens) {
    const f = cfg.fields.find(x => x.key === tok.field);
    if (!f) continue;
    const m = sfTokenMatch(f, tok, row);
    if (tok.neg ? m : !m) return false;
  }
  return true;
}

function sfBarHtml(cfg) {
  const presets = (cfg.presets || []).map((p, i) =>
    `<button class="sf-preset" data-i="${i}" onclick="sfPreset('${cfg.key}', ${i})">${esc(p.label)}<span class="sf-preset-n"></span></button>`).join('');
  return `<div class="sf" id="sf-${cfg.key}">
    <div class="sf-bar">
      <div class="sf-box">
        <div class="sf-input">
          <svg class="sf-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="6.2"/><path d="M15.8 15.8L21 21"/></svg>
          <input id="sf-in-${cfg.key}" placeholder="${esc(cfg.placeholder || 'filter…')}" autocomplete="off" spellcheck="false"
            oninput="sfApply('${cfg.key}')" onkeydown="sfKey(event,'${cfg.key}')" onfocus="sfSuggest('${cfg.key}')" onblur="sfBlur('${cfg.key}')">
          <button class="sf-clear" id="sf-x-${cfg.key}" data-tip="Clear filter" onclick="sfClearAll('${cfg.key}')">&times;</button>
        </div>
        <div class="sf-ac" id="sf-ac-${cfg.key}"></div>
      </div>
      <span class="sf-count" id="sf-cnt-${cfg.key}"></span>
    </div>
    ${presets ? `<div class="sf-presets">${presets}</div>` : ''}
    <div class="sf-chips" id="sf-chips-${cfg.key}" style="display:none"></div>
  </div>`;
}

function sfApply(key) {
  const cfg = sfReg[key]; if (!cfg) return;
  const val = sfInputVal(key);
  const parsed = sfParse(val, cfg.fields);
  const all = cfg.rows();
  const list = all.filter(r => sfMatches(r, parsed, cfg));
  cfg.render(list, parsed);
  const active = parsed.tokens.length || parsed.text;
  const cnt = document.getElementById('sf-cnt-' + key);
  if (cnt) cnt.textContent = cfg.countText
    ? cfg.countText(list, parsed, all.length)
    : (active ? `${list.length} of ${all.length}` : `${all.length} total`);
  const x = document.getElementById('sf-x-' + key); if (x) x.style.display = val ? 'block' : 'none';
  document.querySelectorAll(`#sf-${key} .sf-preset`).forEach((el, i) => {
    const p = cfg.presets[i]; if (!p) return;
    el.classList.toggle('on', val.includes(p.q));
    // live facet count: how many rows this preset alone would match
    const n = all.filter(r => sfMatches(r, sfParse(p.q, cfg.fields), cfg)).length;
    el.classList.toggle('sf-zero', n === 0);
    const ns = el.querySelector('.sf-preset-n');
    if (ns) ns.textContent = ' · ' + n;
  });
  sfRenderChips(key, parsed);
  sfSuggest(key);
}

function sfRenderChips(key, parsed) {
  const box = document.getElementById('sf-chips-' + key); if (!box) return;
  const chips = parsed.tokens.map(t =>
    `<span class="sf-chip"><b>${esc(t.field)}${esc(t.op || ':')}</b>${esc(t.val) || '·'}<button class="sf-chip-x" data-raw="${esc(t.raw)}" data-tip="Remove" onclick="sfRemove('${key}', this.dataset.raw)">&times;</button></span>`);
  if (chips.length) chips.push(`<button class="sf-clearall" onclick="sfClearAll('${key}')">clear all</button>`);
  box.innerHTML = chips.join('');
  box.style.display = chips.length ? 'flex' : 'none';
}

function sfPreset(key, i) {
  const cfg = sfReg[key], p = cfg.presets[i], inp = document.getElementById('sf-in-' + key);
  let val = inp.value.trim();
  val = val.includes(p.q) ? val.replace(p.q, '').replace(/\s+/g, ' ').trim() : (val + ' ' + p.q).trim();
  inp.value = val; sfApply(key); inp.focus();
}

function sfRemove(key, raw) {
  const inp = document.getElementById('sf-in-' + key);
  inp.value = sfTokens(inp.value).filter(p => p !== raw).join(' ');
  sfApply(key); inp.focus();
}

function sfClearAll(key) {
  const inp = document.getElementById('sf-in-' + key);
  inp.value = ''; sfApply(key); inp.focus();
}

// click a value in a table → add it as a filter token (used by tokenable pills)
function sfCellToken(ev, key, field, value) {
  ev.stopPropagation();   // don't also trigger the row's open-detail click
  const inp = document.getElementById('sf-in-' + key);
  if (!inp) return;
  const tok = field + ':' + (/\s/.test(value) ? `"${value}"` : value);
  if (!sfTokens(inp.value).includes(tok)) inp.value = (inp.value.trim() + ' ' + tok).trim();
  sfApply(key);
}

function sfDistinct(cfg, f) {
  const set = new Set();
  for (const r of cfg.rows()) for (const v of sfVals(f, r)) set.add(v);
  return [...set].sort().slice(0, 60);
}

function sfSuggest(key) {
  const cfg = sfReg[key], inp = document.getElementById('sf-in-' + key), ac = document.getElementById('sf-ac-' + key);
  if (!cfg || !inp || !ac || document.activeElement !== inp) { if (ac) ac.classList.remove('open'); return; }
  const caret = inp.selectionStart ?? inp.value.length;
  const cur = (inp.value.slice(0, caret).match(/(\S+)$/) || ['', ''])[1];
  const colon = cur.indexOf(':');
  let opts = [];
  if (colon === -1) {
    const pre = cur.toLowerCase();
    opts = cfg.fields.filter(f => f.suggest !== false && f.key !== 'name' && f.key.startsWith(pre))
      .map(f => ({ text: f.key + ':', hint: f.label }));
  } else {
    const f = cfg.fields.find(x => x.key === cur.slice(0, colon).toLowerCase());
    if (f && f.type !== 'size' && f.type !== 'number') {
      const typed = cur.slice(colon + 1).toLowerCase();
      const vals = f.values || sfDistinct(cfg, f);
      opts = vals.filter(v => String(v).toLowerCase().includes(typed)).slice(0, 12)
        .map(v => ({ text: f.key + ':' + v, hint: f.label }));
    }
  }
  const st = sfAc[key]; st.opts = opts; st.sel = opts.length ? 0 : -1;
  if (!opts.length) { ac.classList.remove('open'); return; }
  ac.innerHTML = opts.map((o, i) =>
    `<button class="sf-opt${i === st.sel ? ' sel' : ''}" data-i="${i}" onmousedown="event.preventDefault()" onclick="sfPick('${key}', ${i})">
      <span class="k">${esc(o.text)}</span>${o.hint ? `<span class="sf-desc">${esc(o.hint)}</span>` : ''}</button>`).join('');
  ac.classList.add('open');
}

function sfPick(key, i) {
  const o = sfAc[key].opts[i]; if (!o) return;
  const inp = document.getElementById('sf-in-' + key);
  const caret = inp.selectionStart ?? inp.value.length;
  const before = inp.value.slice(0, caret).replace(/(\S+)$/, '');
  const after = inp.value.slice(caret);
  const insert = o.text + (o.text.endsWith(':') ? '' : ' ');
  inp.value = before + insert + after;
  const pos = (before + insert).length;
  inp.focus(); inp.setSelectionRange(pos, pos);
  sfApply(key);
  if (o.text.endsWith(':')) sfSuggest(key);
}

function sfAcHi(key) {
  document.querySelectorAll('#sf-ac-' + key + ' .sf-opt').forEach((el, i) => el.classList.toggle('sel', i === sfAc[key].sel));
}

function sfKey(e, key) {
  const st = sfAc[key], ac = document.getElementById('sf-ac-' + key);
  const open = ac && ac.classList.contains('open') && st.opts.length;
  if (e.key === 'ArrowDown' && open) { e.preventDefault(); st.sel = (st.sel + 1) % st.opts.length; sfAcHi(key); }
  else if (e.key === 'ArrowUp' && open) { e.preventDefault(); st.sel = (st.sel - 1 + st.opts.length) % st.opts.length; sfAcHi(key); }
  else if ((e.key === 'Enter' || e.key === 'Tab') && open && st.sel >= 0) { e.preventDefault(); sfPick(key, st.sel); }
  else if (e.key === 'Escape' && open) { e.preventDefault(); ac.classList.remove('open'); }
}

function sfBlur(key) { setTimeout(() => { const ac = document.getElementById('sf-ac-' + key); if (ac) ac.classList.remove('open'); }, 120); }

async function viewListUsers() {
  if (!requireConnection()) return;
  const key = cacheKey('users');
  const cached = viewCache.get(key);
  allUsers = cached || [];
  usersLoading = !cached;  // first load has no data yet, show a skeleton, not "no users"
  renderUsersShell(!!cached);
  const res = await apiPost('/api/list-users').catch(() => ({ error: 'Request failed' }));
  if (currentView !== 'users' || cacheKey('users') !== key) return;  // navigated away
  usersLoading = false;
  if (res.error) {
    if (!cached) setContent(`<div class="card"><h2>Error</h2><p style="color:var(--danger)">${esc(res.error)}</p></div>`);
    else { const h = document.getElementById('usersRefreshing'); if (h) h.textContent = 'refresh failed'; }
    return;
  }
  allUsers = res.users || [];
  usersNote = res.note || '';
  viewCache.set(key, allUsers);
  const h = document.getElementById('usersRefreshing'); if (h) h.style.display = 'none';
  renderUserRows();
}

function renderUsersShell(fromCache) {
  const fam = engineFamily(currentEngine);
  const titles = { mysql: 'MySQL Accounts', documentdb: 'MongoDB Users', elasticsearch: 'Elasticsearch Users', redis: 'Redis ACL Users' };
  const title = titles[fam] || 'PostgreSQL Users';
  const isDocdb = fam === 'documentdb';
  const colsByFam = {
    documentdb: '<th>Username</th><th>Auth DB</th><th>Roles</th>',
    elasticsearch: '<th>Username</th><th>Status</th><th>Roles</th>',
    redis: '<th>Username</th><th>Status</th><th>Commands</th><th>Keys</th>',
  };
  const cols = colsByFam[fam] || '<th>Username</th><th>Status</th><th>Flags</th><th>Expires</th>';
  const cfg = usersFilterCfg(fam);
  sfRegister(cfg);
  setContent(`<div class="card"><h2>${ICONS.users} ${title}</h2>
    <div style="display:flex; gap:14px; align-items:flex-start; margin-bottom:6px; flex-wrap:wrap">
      <div style="flex:1; min-width:240px">${sfBarHtml(cfg)}</div>
      <div style="display:flex; align-items:center; gap:10px; padding-top:1px">
        <span id="usersRefreshing" style="font-size:11px; color:var(--text-muted); display:${fromCache ? 'inline' : 'none'}">refreshing…</span>
        <button class="btn btn-success" onclick="openCreateUserModal()">${ICONS.plus} Create user</button>
      </div>
    </div>
    <div class="table-wrap"><table>
      <thead><tr>${cols}</tr></thead>
      <tbody id="userRows"></tbody>
    </table></div></div>`);
  renderUserRows();
}

function userRowsHtml(list, fam) {
  const isDocdb = fam === 'documentdb';
  const tr = u => `<tr class="urow" data-user="${esc(u.user)}" onclick="viewUserInfoFor(this.dataset.user)">`;
  // a pill you can click to add "field:value" to the filter
  const tok = (cls, field, value, label) =>
    `<span class="pill ${cls} tok" data-f="${field}" data-v="${esc(value)}" data-tip="Filter by ${field}:${esc(value)}"
      onclick="sfCellToken(event,'users',this.dataset.f,this.dataset.v)">${esc(label ?? value)}</span>`;
  let rows = '';
  for (const u of list) {
    if (fam === 'elasticsearch') {
      const status = tok(u.enabled ? 'pill-ok' : 'pill-danger', 'status', u.enabled ? 'enabled' : 'disabled');
      const roles = (u.roles || []).map(r => tok('pill-ok', 'role', r)).join(' ');
      rows += `${tr(u)}<td class="mono">${esc(u.user)}${u.reserved ? ' <span class="pill pill-muted">built-in</span>' : ''}</td><td>${status}</td>
        <td>${roles || '<span class="pill pill-muted">none</span>'}</td></tr>`;
    } else if (fam === 'redis') {
      const status = tok(u.enabled ? 'pill-ok' : 'pill-danger', 'status', u.enabled ? 'on' : 'off');
      rows += `${tr(u)}<td class="mono">${esc(u.user)}${u.reserved ? ' <span class="pill pill-muted">default</span>' : ''}</td><td>${status}</td>
        <td class="mono" style="font-size:11px">${esc(u.commands || '-')}</td>
        <td class="mono" style="font-size:11px">${esc(u.keys || '-')}</td></tr>`;
    } else if (isDocdb) {
      const roles = u.roles.map(r => tok('pill-ok', 'role', r)).join(' ');
      rows += `${tr(u)}<td class="mono">${esc(u.user)}</td><td>${esc(u.db)}</td>
        <td>${roles || '<span class="pill pill-muted">none</span>'}</td></tr>`;
    } else {
      const status = tok(u.can_login ? 'pill-ok' : 'pill-danger', 'status', u.can_login ? 'active' : 'disabled');
      const flags = [];
      if (u.superuser) flags.push(tok('pill-warn', 'superuser', 'yes', 'superuser'));
      if (u.createdb) flags.push(tok('pill-accent', 'createdb', 'yes', 'createdb'));
      if (u.createrole) flags.push(tok('pill-accent', 'createrole', 'yes', 'createrole'));
      rows += `${tr(u)}<td class="mono">${esc(u.user)}</td><td>${status}</td>
        <td>${flags.join(' ') || '-'}</td>
        <td style="color:var(--text-muted)">${u.valid_until === 'never' ? '-' : esc(u.valid_until)}</td></tr>`;
    }
  }
  return rows;
}

function userSkelRows(fam) {
  const ncols = (fam === 'documentdb' || fam === 'elasticsearch') ? 3 : 4;
  const cell = `<td><div class="skel" style="width:${60 + Math.floor(Math.random() * 30)}%"></div></td>`;
  return Array.from({ length: 4 }, () => `<tr>${cell.repeat(ncols)}</tr>`).join('');
}

// thin wrapper so post-CRUD refreshers keep working; re-runs the smart filter
function renderUserRows() { if (sfReg.users) sfApply('users'); }

function usersFilterCfg(fam) {
  const cfg = {
    key: 'users',
    placeholder: 'filter users… try status:disabled',
    rows: () => allUsers,
    textFields: ['name'],
    fields: [{ key: 'name', label: 'username', type: 'text', get: u => u.user }],
    presets: [],
    countText: (list, parsed, total) => usersLoading ? 'loading…'
      : ((parsed.tokens.length || parsed.text) ? `${list.length} of ${total}` : `${total} total`),
    render: (list, parsed) => {
      const tb = document.getElementById('userRows'); if (!tb) return;
      if (!allUsers.length && usersLoading) { tb.innerHTML = userSkelRows(fam); return; }
      if (!list.length) {
        const active = parsed.tokens.length || parsed.text;
        const msg = active ? 'No users match your filter.' : (usersNote || 'No users on this connection yet.');
        tb.innerHTML = `<tr><td colspan="6" style="color:var(--text-muted)">${esc(msg)}</td></tr>`;
        return;
      }
      tb.innerHTML = userRowsHtml(list, fam);
    },
  };
  if (fam === 'elasticsearch') {
    cfg.placeholder = 'filter users… try role:kibana_admin or status:disabled';
    cfg.fields.push(
      { key: 'status', label: 'enabled / disabled', type: 'bool', get: u => u.enabled, values: ['enabled', 'disabled'] },
      { key: 'role', label: 'role', type: 'enum', get: u => u.roles || [] },
      { key: 'builtin', label: 'built-in', type: 'bool', get: u => u.reserved, values: ['yes', 'no'] },
    );
    cfg.presets = [{ label: 'Disabled', q: 'status:disabled' }, { label: 'Built-in', q: 'builtin:yes' }, { label: 'Custom', q: 'builtin:no' }];
  } else if (fam === 'redis') {
    cfg.placeholder = 'filter users… try command:+@write or keys:cache';
    cfg.fields.push(
      { key: 'status', label: 'on / off', type: 'bool', get: u => u.enabled, values: ['on', 'off'] },
      { key: 'command', label: 'command rule', type: 'text', get: u => u.commands },
      { key: 'keys', label: 'key pattern', type: 'text', get: u => u.keys },
      { key: 'default', label: 'default user', type: 'bool', get: u => u.reserved, values: ['yes', 'no'] },
    );
    // command rules carry +/- signs (+@all grants, -@all revokes), so match the grant prefix so a revoked rule doesn't count
    cfg.presets = [{ label: 'Disabled', q: 'status:off' }, { label: 'Write grant', q: 'command:+@write' }, { label: 'Full access', q: 'command:+@all' }, { label: 'All keys', q: 'keys:~*' }];
  } else if (fam === 'documentdb') {
    cfg.placeholder = 'filter users… try role:readWrite or db:admin';
    cfg.fields.push(
      { key: 'role', label: 'role', type: 'enum', get: u => u.roles || [] },
      { key: 'db', label: 'auth db', type: 'text', get: u => u.db },
      { key: 'hasrole', label: 'has roles', type: 'bool', get: u => (u.roles || []).length > 0, values: ['yes', 'no'] },
    );
    cfg.presets = [{ label: 'No roles', q: 'hasrole:no' }];
  } else if (fam === 'mysql') {
    cfg.placeholder = 'filter accounts… try status:disabled';
    cfg.fields.push({ key: 'status', label: 'active / disabled', type: 'bool', get: u => u.can_login, values: ['active', 'disabled'] });
    cfg.presets = [{ label: 'Active', q: 'status:active' }, { label: 'Locked', q: 'status:disabled' }];
  } else {
    cfg.placeholder = 'filter users… try superuser:yes or status:disabled';
    cfg.fields.push(
      { key: 'status', label: 'active / disabled', type: 'bool', get: u => u.can_login, values: ['active', 'disabled'] },
      { key: 'superuser', label: 'superuser', type: 'bool', get: u => u.superuser, values: ['yes', 'no'] },
      { key: 'createdb', label: 'can create db', type: 'bool', get: u => u.createdb, values: ['yes', 'no'] },
      { key: 'createrole', label: 'can create roles', type: 'bool', get: u => u.createrole, values: ['yes', 'no'] },
      { key: 'expires', label: 'has expiry', type: 'bool', get: u => u.valid_until && u.valid_until !== 'never', values: ['yes', 'no'] },
    );
    cfg.presets = [{ label: 'Superusers', q: 'superuser:yes' }, { label: 'Can log in', q: 'status:active' }, { label: "Can't log in", q: 'status:disabled' }, { label: 'Can create DB', q: 'createdb:yes' }, { label: 'Expiring', q: 'expires:yes' }];
  }
  return cfg;
}

async function viewUserInfoFor(username) {
  showPanel(loading('Looking up user…'));
  const res = await apiPost('/api/user-info', { username });
  if (res.error) { showPanel(`<div class="card"><h2>Error</h2><p style="color:var(--danger)">${esc(res.error)}</p></div>`); return; }
  const uAttr = esc(res.user);
  if (res.engine_family === 'elasticsearch') { showPanel(userPanelEs(res, uAttr)); return; }
  if (res.engine_family === 'redis') { showPanel(userPanelRedis(res, uAttr)); return; }
  if (res.engine_family === 'mysql') { showPanel(userPanelMysql(res, uAttr)); return; }
  // Mongo and Postgres share a header + a management toolbar; only the middle differs.
  showPanel(userPanelSql(res, uAttr, currentEngine.startsWith('document')));
}

// The card open + "User: <name>" heading every engine's panel starts with.
function userPanelHead(uAttr) {
  return `<div class="card"><h2>${ICONS.users} User: <span class="mono" style="font-size:14px">${uAttr}</span></h2>`;
}

function userPanelEs(res, uAttr) {
  const status = res.enabled ? '<span class="pill pill-ok">enabled</span>' : '<span class="pill pill-danger">disabled</span>';
  let html = userPanelHead(uAttr) + `<p style="margin-bottom:12px">${status}${res.reserved ? ' &nbsp;<span class="pill pill-muted">built-in</span>' : ''}</p>`;
  const rRows = (res.roles || []).map(r => `<tr><td class="mono">${esc(r)}</td>
      <td style="text-align:right"><button class="btn btn-ghost btn-sm" data-user="${uAttr}" data-role="${esc(r)}" onclick="esRevokeRole(this.dataset.user, this.dataset.role)">Revoke</button></td></tr>`).join('');
  html += `<h2 style="margin-top:4px">Roles</h2><div class="table-wrap"><table><thead><tr><th>Role</th><th></th></tr></thead><tbody>${rRows || '<tr><td colspan="2" style="color:var(--text-muted)">No roles</td></tr>'}</tbody></table></div>
      <h2 style="margin-top:18px">${ICONS.shield} Grant a role</h2>
      <div class="form-row" style="margin-bottom:0">
        <div class="form-field"><label>Role name</label><input id="uiEsRole" placeholder="e.g. monitoring_user"></div>
        <button class="btn btn-success" data-user="${uAttr}" onclick="esGrantRole(this.dataset.user)">Grant</button>
      </div>`;
  if (!res.reserved) html += `<div class="user-toolbar">
      <button class="btn btn-primary" data-user="${uAttr}" onclick="openResetModal(this.dataset.user)">Reset password</button>
      <button class="btn btn-danger" data-user="${uAttr}" onclick="confirmDropUser(this.dataset.user)">Delete user</button></div>`;
  return html + '</div>';
}

function userPanelRedis(res, uAttr) {
  const status = res.enabled ? '<span class="pill pill-ok">on</span>' : '<span class="pill pill-danger">off</span>';
  let html = userPanelHead(uAttr) + `<p style="margin-bottom:12px">${status}${res.user === 'default' ? ' &nbsp;<span class="pill pill-muted">default</span>' : ''}</p>
      <div class="table-wrap"><table><tbody>
        <tr><td style="width:110px;color:var(--text-muted)">Commands</td><td class="mono" style="font-size:12px">${esc(res.commands || '-')}</td></tr>
        <tr><td style="color:var(--text-muted)">Keys</td><td class="mono" style="font-size:12px">${esc(res.keys || '-')}</td></tr>
        <tr><td style="color:var(--text-muted)">Channels</td><td class="mono" style="font-size:12px">${esc(res.channels || '-')}</td></tr>
      </tbody></table></div>
      <h2 style="margin-top:18px">${ICONS.shield} Change permissions</h2>
      <p class="card-meta">Add an ACL rule: a category (<code>+@write</code>, <code>-@dangerous</code>), a command (<code>+get</code>), or a key pattern (<code>~cache:*</code>).</p>
      <div class="form-row" style="margin-bottom:0">
        <div class="form-field"><label>ACL rule</label><input id="uiRedisRule" placeholder="+@write"></div>
        <button class="btn btn-success" data-user="${uAttr}" onclick="redisApplyRule(this.dataset.user)">Apply</button>
      </div>`;
  if (res.user !== 'default') html += `<div class="user-toolbar">
      <button class="btn btn-primary" data-user="${uAttr}" onclick="openResetModal(this.dataset.user)">Reset password</button>
      <button class="btn btn-danger" data-user="${uAttr}" onclick="confirmDropUser(this.dataset.user)">Delete user</button></div>`;
  return html + '</div>';
}

function userPanelMysql(res, uAttr) {
  const status = res.can_login ? '<span class="pill pill-ok">active</span>' : '<span class="pill pill-danger">locked</span>';
  let html = userPanelHead(uAttr) + `<p style="margin-bottom:12px">${status}</p>`;
  html += `<div class="table-wrap"><table><thead><tr><th>Grant</th></tr></thead><tbody>${
    (res.grant_statements || []).map(g => `<tr><td class="mono" style="font-size:11.5px">${esc(g)}</td></tr>`).join('')
    || '<tr><td style="color:var(--text-muted)">No grants</td></tr>'}</tbody></table></div>`;
  const privOpts = (config.mysql_privileges || []).map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
  html += `<h2 style="margin-top:20px">${ICONS.shield} Grant a Privilege</h2>
      <div class="form-row" style="margin-bottom:0">
        <div class="form-field"><label>Privilege</label><select id="uiGrantPriv">${privOpts}</select></div>
        <div class="form-field"><label>Database (* = all)</label><input id="uiGrantDb" list="dbList" placeholder="type to search, or *"></div>
        <button class="btn btn-success" data-user="${uAttr}" onclick="grantPgPrivDirect(this.dataset.user)">Grant</button>
      </div>`;
  const lockBtn = res.can_login
    ? `<button class="btn btn-ghost" data-user="${uAttr}" onclick="toggleLoginDirect(this.dataset.user, false)">Lock account</button>`
    : `<button class="btn btn-ghost" data-user="${uAttr}" onclick="toggleLoginDirect(this.dataset.user, true)">Unlock account</button>`;
  html += `<div class="user-toolbar">
      <button class="btn btn-primary" data-user="${uAttr}" onclick="openResetModal(this.dataset.user)">Reset password</button>
      <button class="btn btn-ghost" data-user="${uAttr}" onclick="openTestLoginModal(this.dataset.user, '')">Test access</button>
      ${lockBtn}
      <button class="btn btn-danger" data-user="${uAttr}" onclick="confirmDropUser(this.dataset.user)">Drop user</button>
    </div></div>`;
  return html;
}

// Mongo (isDocdb) and Postgres: shared head + toolbar, engine-specific middle.
function userPanelSql(res, uAttr, isDocdb) {
  let html = userPanelHead(uAttr);
  if (isDocdb) {
    html += `<p class="card-meta"><strong>Auth DB:</strong> ${esc(res.db)} &nbsp;·&nbsp; <strong>ID:</strong> <span class="mono">${esc(res.userId)}</span></p>`;
    let rRows = '';
    for (const r of res.roles) {
      rRows += `<tr>
        <td>${esc(r.role)}</td><td class="mono">${esc(r.db || '*')}</td>
        <td style="text-align:right"><button class="btn btn-ghost btn-sm" data-user="${uAttr}" data-role="${esc(r.role)}" data-db="${esc(r.db || '')}"
          onclick="revokeRoleDirect(this.dataset.user, this.dataset.role, this.dataset.db)">Revoke</button></td>
      </tr>`;
    }
    html += `<div class="table-wrap"><table><thead><tr><th>Role</th><th>Database</th><th></th></tr></thead><tbody>${rRows || '<tr><td colspan="3" style="color:var(--text-muted)">No roles</td></tr>'}</tbody></table></div>`;
    const roleOpts = (config.docdb_roles || []).map(r => `<option value="${esc(r)}">${esc(r)}</option>`).join('');
    html += `<h2 style="margin-top:20px">${ICONS.shield} Grant a Role</h2>
      <div class="form-row" style="margin-bottom:0">
        <div class="form-field"><label>Role</label><select id="uiGrantRole">${roleOpts}</select></div>
        <div class="form-field"><label>Database</label><input id="uiGrantDb" list="dbList" placeholder="type to search databases…"></div>
        <button class="btn btn-success" data-user="${uAttr}" onclick="grantRoleDirect(this.dataset.user)">Grant</button>
      </div>`;
  } else {
    const status = res.can_login ? '<span class="pill pill-ok">active</span>' : '<span class="pill pill-danger">disabled</span>';
    const flags = [];
    if (res.superuser) flags.push('superuser');
    if (res.createdb) flags.push('createdb');
    if (res.createrole) flags.push('createrole');
    html += `<p style="margin-bottom:8px">${status} &nbsp; Flags: ${flags.join(', ') || 'none'} &nbsp; Expires: ${esc(res.valid_until)} &nbsp; Conn limit: ${esc(res.conn_limit)}</p>`;
    const granted = (res.db_privileges || []).filter(d => d.privileges && d.privileges.length);
    if (granted.length) {
      let dbRows = '';
      for (const d of granted) {
        const btns = d.privileges.map(p => `<button class="btn btn-ghost btn-sm" data-user="${uAttr}" data-priv="${esc(p)}" data-db="${esc(d.database)}"
          onclick="revokePgDbPriv(this.dataset.user, this.dataset.priv, this.dataset.db)">Revoke ${esc(p)}</button>`).join(' ');
        dbRows += `<tr><td class="mono">${esc(d.database)}</td><td>${d.privileges.map(esc).join(', ')}</td><td style="text-align:right">${btns}</td></tr>`;
      }
      html += `<h2 style="margin-top:16px">Database Privileges</h2>
        <div class="table-wrap"><table><thead><tr><th>Database</th><th>Privileges</th><th></th></tr></thead><tbody>${dbRows}</tbody></table></div>`;
    }
    if (res.grants && res.grants.length) {
      let gRows = '';
      for (const g of res.grants) {
        gRows += `<tr><td class="mono">${esc(g.db)}</td><td class="mono">${esc(g.table)}</td><td>${esc(g.privilege)}</td></tr>`;
      }
      html += `<h2 style="margin-top:16px">Table Grants</h2>
        <div class="table-wrap"><table><thead><tr><th>Database</th><th>Table</th><th>Privilege</th></tr></thead><tbody>${gRows}</tbody></table></div>`;
    }
    const privOpts = (config.pg_privileges || []).map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
    html += `<h2 style="margin-top:20px">${ICONS.shield} Grant a Privilege</h2>
      <div class="form-row" style="margin-bottom:0">
        <div class="form-field"><label>Privilege</label><select id="uiGrantPriv">${privOpts}</select></div>
        <div class="form-field"><label>Database</label><input id="uiGrantDb" list="dbList" placeholder="type to search databases…"></div>
        <div class="form-field"><label>Schema</label><input id="uiGrantSchema" value="public"></div>
        <button class="btn btn-success" data-user="${uAttr}" onclick="grantPgPrivDirect(this.dataset.user)">Grant</button>
      </div>`;
  }
  const pgButtons = isDocdb ? '' : (res.can_login
    ? `<button class="btn btn-ghost" data-user="${uAttr}" onclick="toggleLoginDirect(this.dataset.user, false)">🔒 Disable login</button>`
    : `<button class="btn btn-ghost" data-user="${uAttr}" onclick="toggleLoginDirect(this.dataset.user, true)">🔓 Enable login</button>`);
  const revokeAllBtn = isDocdb ? '' :
    `<button class="btn btn-ghost" data-user="${uAttr}" onclick="confirmRevokeAll(this.dataset.user)">⊘ Revoke all access</button>`;
  html += `<div class="user-toolbar">
    <button class="btn btn-primary" data-user="${uAttr}" onclick="openResetModal(this.dataset.user)">🔑 Reset password</button>
    <button class="btn btn-ghost" data-user="${uAttr}" onclick="openTestLoginModal(this.dataset.user, '')">✓ Test access</button>
    ${pgButtons}
    ${revokeAllBtn}
    <button class="btn btn-danger" data-user="${uAttr}" onclick="confirmDropUser(this.dataset.user)">🗑️ Drop user</button>
  </div>`;
  html += '</div>';
  return html;
}

