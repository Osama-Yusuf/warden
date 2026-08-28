// ── Data browser (rows & columns grid) ──
let dg = null;

function dgSpecFrom(ds) {
  const spec = { database: ds.db };
  if (ds.coll) spec.collection = ds.coll;
  else { spec.table = ds.table; if (ds.schema) spec.schema = ds.schema; }
  return spec;
}

function openDataGrid(spec) {
  // spec: {database, collection} (mongo) or {database, schema?, table} (sql)
  if (!requireConnection()) return;
  const name = spec.collection || (spec.schema ? `${spec.schema}.${spec.table}` : spec.table);
  dg = { ...spec, name, fam: engineFamily(currentEngine), limit: 50, offset: 0, total: null,
         columns: [], rows: [], ids: [], loading: true, error: null, search: '', locked: true,
         meta: null, editable: false, filtered: false, estimated: false };
  document.getElementById('dataModal').classList.add('open');
  document.getElementById('dgStats').innerHTML = '';
  renderDgChrome();
  dgFetch();
  dgLoadStats();
}

function dgRefresh() { dgFetch(); dgLoadStats(); }

async function dgLoadStats() {
  if (!dg) return;
  const p = { database: dg.database };
  if (dg.collection) p.collection = dg.collection;
  if (dg.table) { p.table = dg.table; if (dg.schema) p.schema = dg.schema; }
  const res = await apiPost('/api/object-stats', p).catch(() => null);
  const el = document.getElementById('dgStats');
  if (!dg || !el) return;
  if (!res || res.error || !res.stats) { el.innerHTML = ''; return; }
  dg.stats = res.stats;
  el.innerHTML = dgStatCards(res.engine || (dg.collection ? 'documentdb' : 'postgresql'), res.stats);
}

function dgStatCards(engine, s) {
  const card = (k, v) => `<div class="dg-stat"><span class="k">${k}</span><span class="v">${v}</span></div>`;
  const rows = s.rows == null ? '-' : (s.estimated ? '~' : '') + Number(s.rows).toLocaleString();
  const out = [];
  if (engine === 'documentdb') {
    out.push(card('Documents', rows));
    if (s.size_bytes != null) out.push(card('Data size', fmtSize(s.size_bytes)));
    if (s.storage_bytes != null) out.push(card('Storage', fmtSize(s.storage_bytes)));
    if (s.indexes != null) out.push(card('Indexes', s.indexes));
    if (s.avg_obj) out.push(card('Avg document', fmtSize(s.avg_obj)));
  } else if (engine === 'elasticsearch') {
    out.push(card('Documents', rows));
    if (s.size_bytes != null) out.push(card('Size', fmtSize(s.size_bytes)));
    if (s.segments != null) out.push(card('Segments', s.segments));
  } else if (engine === 'redis') {
    out.push(card('Keys in DB', rows));
    if (s.size_bytes != null) out.push(card('Server memory', fmtSize(s.size_bytes)));
  } else {
    out.push(card('Rows', rows));
    if (s.columns != null) out.push(card('Columns', s.columns));
    if (s.size_bytes != null) out.push(card('Size', fmtSize(s.size_bytes)));
    if (s.indexes != null) out.push(card('Indexes', s.indexes));
  }
  return out.join('');
}

function closeDataModal() {
  document.getElementById('dataModal').classList.remove('open');
  ['cellModal', 'dgFormModal', 'dgConfirmModal'].forEach(id => document.getElementById(id).classList.remove('open'));
  dg = null; dgPendingOp = null;
}

async function dgFetch() {
  if (!dg) return;
  dg.loading = true;
  renderDgBody(); renderDgFoot();
  const payload = { database: dg.database, limit: dg.limit, offset: dg.offset };
  if (dg.search) payload.search = dg.search;
  if (dg.collection) payload.collection = dg.collection;
  if (dg.table) { payload.table = dg.table; if (dg.schema) payload.schema = dg.schema; }
  const res = await apiPost('/api/browse-data', payload).catch(() => ({ error: 'Request failed' }));
  if (!dg) return;  // closed mid-flight
  dg.loading = false;
  if (res.error) { dg.error = res.error; }
  else {
    dg.error = null; dg.columns = res.columns || []; dg.rows = res.rows || [];
    dg.ids = res.ids || []; dg.total = res.total;
    dg.filtered = !!res.filtered; dg.estimated = !!res.estimated;
  }
  renderDgBody(); renderDgFoot();
}

async function dgToggleLock() {
  if (!dg) return;
  if (!dg.locked) { dg.locked = true; renderDgChrome(); renderDgBody(); return; }
  if (prefs.readOnly) { toast('Read-only mode is on. Turn it off in the top bar to edit', 'error'); return; }
  // Unlocking: fetch metadata so we know the primary key + columns before editing.
  const p = { database: dg.database };
  if (dg.collection) p.collection = dg.collection;
  if (dg.table) { p.table = dg.table; if (dg.schema) p.schema = dg.schema; }
  const meta = await apiPost('/api/table-meta', p).catch(() => ({ error: 'Request failed' }));
  if (!dg) return;
  if (meta.error) { toast(meta.error, 'error'); return; }
  if (!meta.editable) { toast(meta.reason || 'This object cannot be edited', 'error'); return; }
  dg.meta = meta; dg.editable = true; dg.locked = false;
  renderDgChrome(); renderDgBody();
  toast('Editing unlocked. Changes are confirmed before they run', 'info');
}

function renderDgChrome() {
  const kind = dg.collection ? 'collection' : 'table';
  const full = `${dg.database}.${dg.name}`;
  document.getElementById('dgTitle').innerHTML =
    `<span class="badge">${kind}</span><span class="name" data-tip="${esc(full)}">${esc(full)}</span>`;
  const lockBtn = dg.locked
    ? `<button class="ibtn" onclick="dgToggleLock()" data-tip="Read-only, click to enable editing">${ICONS.lock}</button>`
    : `<button class="ibtn on" onclick="dgToggleLock()" data-tip="Editing enabled, click to lock (read-only)">${ICONS.unlock}</button>`;
  const addBtn = (!dg.locked && dg.editable)
    ? `<button class="ibtn success" onclick="dgAddRow()" data-tip="Add a row">${ICONS.plus}</button>` : '';
  document.getElementById('dgTools').innerHTML = `
    <div class="dg-search">
      ${ICONS.search}
      <input id="dgSearchInput" placeholder="search…" value="${esc(dg.search)}" autocomplete="off"
        onkeydown="if(event.key==='Enter')dgSearchSubmit();else if(event.key==='Escape'){this.value='';dgClearSearch();}">
      ${dg.search ? `<button class="dg-search-x" onclick="dgClearSearch()" data-tip="Clear search">&times;</button>` : ''}
    </div>
    ${addBtn}
    ${lockBtn}
    <select class="tb-select" onchange="dgSetLimit(this.value)" data-tip="Rows per page">
      ${[25,50,100,200].map(n => `<option value="${n}" ${n===dg.limit?'selected':''}>${n} rows</option>`).join('')}
    </select>
    <button class="ibtn teal" onclick="dgRefresh()" data-tip="Refresh">${ICONS.refresh}</button>`;
}

function dgSearchSubmit() {
  if (!dg) return;
  const v = (document.getElementById('dgSearchInput')?.value || '').trim();
  if (v === dg.search) return;
  dg.search = v; dg.offset = 0;
  renderDgChrome(); dgFetch();
  setTimeout(() => document.getElementById('dgSearchInput')?.focus(), 0);
}
function dgClearSearch() {
  if (!dg || !dg.search) { const i = document.getElementById('dgSearchInput'); if (i) i.value=''; return; }
  dg.search = ''; dg.offset = 0; renderDgChrome(); dgFetch();
}

function dgCellHtml(v) {
  if (v === null || v === undefined) return '<span class="dg-null">NULL</span>';
  const t = typeof v;
  if (t === 'boolean') return `<span class="dg-bool ${v?'t':'f'}">${v}</span>`;
  if (t === 'number') return `<span class="dg-num">${esc(String(v))}</span>`;
  if (t === 'object') return `<span class="dg-json">${esc(JSON.stringify(v))}</span>`;
  return esc(String(v));
}

function renderDgBody() {
  const body = document.getElementById('dgBody');
  if (!body || !dg) return;
  if (dg.error) { body.innerHTML = `<div style="padding:24px; color:var(--danger)">${esc(dg.error)}</div>`; return; }
  const cols = dg.columns;
  const edit = !dg.locked && dg.editable;
  const actH = edit ? '<th class="dg-actcol"></th>' : '';
  if (dg.loading) {
    const nc = cols.length || 5;
    const head = `<thead><tr><th class="dg-corner"></th>${actH}${(cols.length ? cols.map(c=>`<th>${esc(c)}</th>`) : Array.from({length:nc},()=>'<th><div class="skel" style="width:80px"></div></th>')).join('')}</tr></thead>`;
    const rows = Array.from({length:12}, () => `<tr class="dg-skelrow"><td class="dg-rownum"></td>${edit?'<td class="dg-actions"></td>':''}${'<td><div class="skel" style="width:70%"></div></td>'.repeat(nc)}</tr>`).join('');
    body.innerHTML = `<table class="dgrid">${head}<tbody>${rows}</tbody></table>`;
    return;
  }
  if (!dg.rows.length) {
    const what = dg.collection ? 'collection' : 'table';
    body.innerHTML = `<div class="dg-empty" style="padding:24px">${dg.search ? `No rows match “${esc(dg.search)}”.` : `No rows in this ${what}.`}</div>`;
    return;
  }
  const head = `<thead><tr><th class="dg-corner"></th>${actH}${cols.map(c => `<th data-tip="${esc(c)}">${esc(c)}</th>`).join('')}</tr></thead>`;
  const tbody = dg.rows.map((row, r) => {
    const actC = edit ? `<td class="dg-actions"><div class="dg-actwrap">
      <button class="dg-act" data-tip="Edit row" onclick="dgEditRow(${r})">${ICONS.pencil}</button>
      <span class="dg-actsep"></span>
      <button class="dg-act danger" data-tip="Delete row" onclick="dgDeleteRow(${r})">${ICONS.trash}</button>
    </div></td>` : '';
    return `<tr>
      <td class="dg-rownum" style="cursor:pointer" data-tip="Copy this row" onclick="dgShowRow(${r})">${dg.offset + r + 1}</td>${actC}
      ${cols.map((c, ci) => {
        const v = row[ci];
        const big = v !== null && v !== undefined && (typeof v === 'object' || String(v).length > 40);
        return `<td class="${big ? 'dg-cell' : ''}"${big ? ` onclick="dgShowCell(${r},${ci})"` : ''}>${dgCellHtml(v)}</td>`;
      }).join('')}
    </tr>`;
  }).join('');
  body.innerHTML = `<table class="dgrid">${head}<tbody>${tbody}</tbody></table>`;
}

function renderDgFoot() {
  const foot = document.getElementById('dgFoot');
  if (!foot || !dg) return;
  const n = dg.rows.length;
  const from = n ? dg.offset + 1 : 0;
  const to = dg.offset + n;
  let range;
  if (dg.filtered) {
    range = `${from.toLocaleString()}-${to.toLocaleString()} <span class="dg-tag">filtered</span>`;
  } else {
    const totalStr = dg.total != null ? (dg.estimated ? '~' : '') + dg.total.toLocaleString() : '?';
    range = `${from.toLocaleString()}-${to.toLocaleString()} of ${totalStr}`;
  }
  const atEnd = (n < dg.limit) || (!dg.filtered && !dg.estimated && dg.total != null && to >= dg.total);
  foot.innerHTML = `
    <span class="count">${dg.loading ? 'loading…' : `${dg.columns.length} column${dg.columns.length===1?'':'s'}`}${!dg.locked && dg.editable ? ' · <span class="dg-tag warn">editing</span>' : ''}</span>
    <div class="dg-pager">
      <span class="range">${range}</span>
      <button class="btn btn-ghost btn-sm" onclick="dgPage(-1)" ${dg.offset<=0?'disabled':''}>‹ Prev</button>
      <button class="btn btn-ghost btn-sm" onclick="dgPage(1)" ${atEnd?'disabled':''}>Next ›</button>
    </div>`;
}

function dgPage(dir) {
  if (!dg) return;
  const next = dg.offset + dir * dg.limit;
  if (next < 0) return;
  dg.offset = next;
  dgFetch();
}
function dgSetLimit(v) { if (!dg) return; dg.limit = parseInt(v, 10) || 50; dg.offset = 0; renderDgChrome(); dgFetch(); }

let dgCellValue = '';
function dgShowCell(r, c) {
  if (!dg) return;
  const v = dg.rows[r][c];
  dgCellValue = (v !== null && typeof v === 'object') ? JSON.stringify(v, null, 2) : String(v);
  document.getElementById('cellTitle').textContent = `${dg.columns[c]} · row ${dg.offset + r + 1}`;
  document.getElementById('cellBody').textContent = dgCellValue;
  document.getElementById('cellExtra').innerHTML = '';
  document.getElementById('cellModal').classList.add('open');
}
function dgCopyCell() { navigator.clipboard?.writeText(dgCellValue).then(() => toast('Copied', 'success')).catch(() => {}); }

// ── Copy a whole row as JSON or a ready-to-paste INSERT ──
function dgRowObject(r) {
  const o = {};
  dg.columns.forEach((c, i) => { o[c] = dg.rows[r][i]; });
  return o;
}
function dgSqlLit(v) {
  if (v === null || v === undefined) return 'NULL';
  if (typeof v === 'number') return String(v);
  if (typeof v === 'boolean') return v ? 'TRUE' : 'FALSE';
  if (typeof v === 'object') return `'${JSON.stringify(v).replace(/'/g, "''")}'`;
  return `'${String(v).replace(/'/g, "''")}'`;
}
function dgRowInsert(r) {
  // MySQL quotes identifiers with backticks; Postgres/SQLite use double quotes
  const bt = engineFamily(currentEngine) === 'mysql';
  const qi = s => bt ? '`' + String(s).replace(/`/g, '``') + '`' : `"${String(s).replace(/"/g, '""')}"`;
  const tbl = dg.schema ? `${qi(dg.schema)}.${qi(dg.table)}` : dg.table ? qi(dg.table) : (dg.collection || 'table');
  const cols = dg.columns.map(qi).join(', ');
  const vals = dg.columns.map((c, i) => dgSqlLit(dg.rows[r][i])).join(', ');
  return `INSERT INTO ${tbl} (${cols}) VALUES (${vals});`;
}
function dgShowRow(r) {
  if (!dg) return;
  const json = JSON.stringify(dgRowObject(r), null, 2);
  dgCellValue = json;
  document.getElementById('cellTitle').textContent = `Row ${dg.offset + r + 1}`;
  document.getElementById('cellBody').textContent = json;
  const fam = engineFamily(currentEngine);
  const sql = fam === 'postgresql' || fam === 'mysql' || fam === 'sqlite';
  let extra = `<button class="btn btn-ghost btn-sm" onclick="dgCopyRow(${r},'json')">Copy as JSON</button>`;
  if (sql) extra += `<button class="btn btn-ghost btn-sm" onclick="dgCopyRow(${r},'insert')">Copy as INSERT</button>`;
  document.getElementById('cellExtra').innerHTML = extra;
  document.getElementById('cellModal').classList.add('open');
}
function dgCopyRow(r, kind) {
  const text = kind === 'insert' ? dgRowInsert(r) : JSON.stringify(dgRowObject(r), null, 2);
  navigator.clipboard?.writeText(text).then(() => toast(`Copied row as ${kind === 'insert' ? 'INSERT' : 'JSON'}`, 'success')).catch(() => {});
}

// ── CRUD: type coercion + previews ──────────────────────────────────────────
function dgPgCat(type) {
  const t = (type || '').toLowerCase();
  if (/\bserial\b|int|bigint|smallint/.test(t) && !/interval/.test(t)) return 'int';
  if (/numeric|decimal|real|double|float|money/.test(t)) return 'float';
  if (/bool/.test(t)) return 'bool';
  if (/json/.test(t)) return 'json';
  return 'text';
}
function dgCoerce(cat, raw) {
  if (cat === 'int') { if (!/^-?\d+$/.test(raw.trim())) throw new Error('must be a whole number'); return parseInt(raw, 10); }
  if (cat === 'float') { const n = Number(raw); if (!isFinite(n)) throw new Error('must be a number'); return n; }
  if (cat === 'bool') { return raw === 'true'; }
  if (cat === 'json') { try { return JSON.parse(raw); } catch { throw new Error('must be valid JSON'); } }
  return raw;
}
function dgVal(v) {  // human-readable value for a preview statement
  if (v === null || v === undefined) return 'NULL';
  if (typeof v === 'number') return String(v);
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'object') return JSON.stringify(v);
  return `'${String(v).replace(/'/g, "''")}'`;
}
function dgMongoIdJs(rep) {
  return (rep && typeof rep === 'object' && '$oid' in rep) ? `ObjectId('${rep.$oid}')` : JSON.stringify(rep);
}
function dgReconstructDoc(r) {
  const o = {};
  dg.columns.forEach((c, i) => { o[c] = dg.rows[r][i]; });
  return o;
}
function dgPgPk(r) {
  const pk = {};
  for (const c of dg.meta.primary_key) pk[c] = dg.rows[r][dg.columns.indexOf(c)];
  return pk;
}

// ── CRUD: forms ─────────────────────────────────────────────────────────────
let dgForm = null;
function dgAddRow() { dgOpenForm('insert', null); }
function dgEditRow(r) { dgOpenForm('edit', r); }

function dgOpenForm(mode, r) {
  if (!dg || dg.locked || !dg.editable) return;
  dgForm = { mode, r };
  let bodyHtml, title;
  if (dg.fam === 'redis') {
    title = `${mode === 'insert' ? 'Add a key to' : 'Edit key in'} ${esc(dg.database)}`;
    bodyHtml = dgRedisFormFields(mode, r);
  } else if (dg.collection) {  // Mongo & Elasticsearch: edit the document as JSON
    const eng = engineLabel(currentEngine);
    title = `${mode === 'insert' ? 'Add document to' : 'Edit document in'} ${esc(dg.database)}.${esc(dg.name)}`;
    const initial = mode === 'edit' ? JSON.stringify(dgReconstructDoc(r), null, 2) : '{\n  \n}';
    bodyHtml = `<p class="dgf-hint">Edit the document as JSON. ${mode === 'edit' ? 'Only changed fields are written; <code>_id</code> can\'t change.' : `Leave out <code>_id</code> to let ${esc(eng)} assign one.`}</p>
      <textarea id="dgJsonInput" class="dgf-json" spellcheck="false">${esc(initial)}</textarea>`;
  } else {
    title = `${mode === 'insert' ? 'Add row to' : 'Edit row in'} ${esc(dg.database)}.${esc(dg.name)}`;
    bodyHtml = `<div class="dgf-grid">${dgPgFormFields(mode, r)}</div>`;
  }
  document.getElementById('dgFormTitle').textContent = title;
  document.getElementById('dgFormBody').innerHTML = bodyHtml;
  document.getElementById('dgFormSave').textContent = mode === 'insert' ? 'Review insert' : 'Review changes';
  document.getElementById('dgFormModal').classList.add('open');
}

const RD_VAL_HINT = { string: 'Plain text value.', hash: 'JSON object: {"field": "value", …}', list: 'JSON array: ["a", "b", …]', set: 'JSON array of unique members.', zset: 'JSON array of [member, score] pairs.' };

function dgRedisFormFields(mode, r) {
  const isEdit = mode === 'edit';
  const key = isEdit ? dg.rows[r][0] : '';
  const ktype = isEdit ? dg.rows[r][1] : 'string';
  const ttl = isEdit ? (dg.rows[r][2] ?? '') : '';
  const val = isEdit ? dg.rows[r][4] : '';
  const valStr = ktype === 'string' ? (val == null ? '' : String(val)) : JSON.stringify(val ?? (ktype === 'hash' ? {} : []), null, 2);
  const typeField = isEdit
    ? `<input class="dgf-input" value="${esc(ktype)}" readonly>`
    : `<select class="dgf-input" id="rdType" onchange="dgRedisTypeChanged()">${['string','hash','list','set','zset'].map(t => `<option value="${t}" ${t===ktype?'selected':''}>${t}</option>`).join('')}</select>`;
  return `<div class="dgf-grid">
    <div class="dgf-row"><label><span class="dgf-name">Key</span></label>
      <div class="dgf-inputwrap"><input id="rdKey" class="dgf-input" value="${esc(key)}" ${isEdit?'readonly':''} placeholder="namespace:key" autocomplete="off"></div></div>
    <div class="dgf-row"><label><span class="dgf-name">Type</span></label><div class="dgf-inputwrap">${typeField}</div></div>
    <div class="dgf-row"><label><span class="dgf-name">TTL</span><span class="dgf-type">seconds · blank = no expiry</span></label>
      <div class="dgf-inputwrap"><input id="rdTtl" class="dgf-input" type="number" min="1" value="${esc(String(ttl))}" placeholder="(no expiry)"></div></div>
    <div class="dgf-row"><label><span class="dgf-name">Value</span><span class="dgf-type" id="rdValHint">${RD_VAL_HINT[ktype]}</span></label>
      <div class="dgf-inputwrap"><textarea id="rdValue" class="dgf-json" spellcheck="false">${esc(valStr)}</textarea></div></div>
  </div>`;
}
function dgRedisTypeChanged() {
  const t = document.getElementById('rdType')?.value || 'string';
  const h = document.getElementById('rdValHint'); if (h) h.textContent = RD_VAL_HINT[t];
}

function dgPgFormFields(mode, r) {
  const isEdit = mode === 'edit';
  return dg.meta.columns.map(col => {
    if (!isEdit && col.generated) return '';
    const cat = dgPgCat(col.type);
    const idx = dg.columns.indexOf(col.name);
    const cur = (isEdit && idx >= 0) ? dg.rows[r][idx] : undefined;
    const curNull = isEdit && (cur === null || cur === undefined);   // insert never starts NULL
    const readonly = isEdit && col.is_pk;
    const initStr = (!isEdit || cur === null || cur === undefined) ? '' : (typeof cur === 'object' ? JSON.stringify(cur) : String(cur));
    let input;
    if (cat === 'bool') {
      const opts = [];
      if (!isEdit && col.has_default) opts.push({ v: '', t: '(default)' });
      opts.push({ v: 'true', t: 'true' }, { v: 'false', t: 'false' });
      if (col.nullable) opts.push({ v: '__null__', t: '(null)' });
      const sel = isEdit ? (curNull ? '__null__' : String(cur)) : (col.has_default ? '' : 'false');
      input = `<select class="dgf-input" data-name="${esc(col.name)}" data-cat="bool" data-init="${esc(sel)}" ${readonly?'disabled':''}>
        ${opts.map(o => `<option value="${o.v}" ${o.v===sel?'selected':''}>${o.t}</option>`).join('')}</select>`;
    } else {
      input = `<input class="dgf-input" data-name="${esc(col.name)}" data-cat="${cat}" data-init="${esc(initStr)}"
        value="${esc(initStr)}" ${readonly?'readonly':''}
        placeholder="${!isEdit&&col.has_default?'(default)':(col.nullable?'(empty = NULL)':'')}" autocomplete="off">`;
    }
    const nullToggle = (col.nullable && !readonly && cat !== 'bool')
      ? `<button type="button" class="dgf-nullbtn ${curNull?'on':''}" onclick="dgToggleNull(this)" data-tip="Set NULL">∅</button>` : '';
    const tags = `${col.is_pk?'<span class="dgf-tag pk">PK</span>':''}${(!col.nullable&&!col.has_default)?'<span class="dgf-tag req">required</span>':''}`;
    return `<div class="dgf-row" data-field="${esc(col.name)}" data-null="${curNull?'1':'0'}" data-initnull="${curNull?'1':'0'}">
      <label><span class="dgf-name">${esc(col.name)}</span>${tags}<span class="dgf-type">${esc(col.type)}</span></label>
      <div class="dgf-inputwrap ${curNull?'is-null':''}">${input}${nullToggle}</div>
    </div>`;
  }).join('');
}

function dgToggleNull(btn) {
  const row = btn.closest('.dgf-row');
  const on = row.dataset.null === '1';
  row.dataset.null = on ? '0' : '1';
  btn.classList.toggle('on', !on);
  row.querySelector('.dgf-inputwrap').classList.toggle('is-null', !on);
  const inp = row.querySelector('.dgf-input');
  if (inp && !inp.readOnly) inp.disabled = !on;
}

function dgFormSubmit() {
  if (!dg || !dgForm) return;
  if (dg.fam === 'redis') return dgFormSubmitRedis();
  if (dg.collection) return dgFormSubmitMongo();
  return dgFormSubmitPg();
}

function dgFormSubmitRedis() {
  const isEdit = dgForm.mode === 'edit';
  const key = document.getElementById('rdKey').value.trim();
  if (!key) { toast('Key is required', 'error'); return; }
  const ktype = isEdit ? dg.rows[dgForm.r][1] : document.getElementById('rdType').value;
  const ttlRaw = document.getElementById('rdTtl').value.trim();
  const ttl = ttlRaw ? parseInt(ttlRaw, 10) : null;
  if (ttlRaw && (!Number.isFinite(ttl) || ttl <= 0)) { toast('TTL must be a positive number of seconds', 'error'); return; }
  const raw = document.getElementById('rdValue').value;
  let value;
  if (ktype === 'string') value = raw;
  else { try { value = JSON.parse(raw); } catch (e) { toast('Value must be valid JSON: ' + e.message, 'error'); return; } }
  const q = s => JSON.stringify(String(s));
  let preview;
  if (ktype === 'string') preview = `SET ${key} ${q(value)}${ttl ? ` EX ${ttl}` : ''}`;
  else if (ktype === 'hash') preview = `DEL ${key}\nHSET ${key} ${Object.entries(value || {}).map(([k, v]) => `${k} ${q(v)}`).join(' ')}`;
  else if (ktype === 'list') preview = `DEL ${key}\nRPUSH ${key} ${(value || []).map(q).join(' ')}`;
  else if (ktype === 'set') preview = `DEL ${key}\nSADD ${key} ${(value || []).map(q).join(' ')}`;
  else preview = `DEL ${key}\nZADD ${key} ${(value || []).map(p => `${p[1]} ${q(p[0])}`).join(' ')}`;
  if (ttl && ktype !== 'string') preview += `\nEXPIRE ${key} ${ttl}`;
  dgConfirm({
    action: isEdit ? 'update' : 'insert', danger: false,
    endpoint: isEdit ? '/api/row-update' : '/api/row-insert',
    payload: isEdit ? { database: dg.database, id: key, ktype, value, ttl }
                    : { database: dg.database, key, ktype, value, ttl },
    preview,
    summary: isEdit ? `Replaces the value of ${key}${ttl ? ` and sets a ${ttl}s TTL` : ''}.`
                    : `Creates ${key} as a ${ktype}${ttl ? ` with a ${ttl}s TTL` : ''}.`,
    changes: null, confirmLabel: isEdit ? 'Save key' : 'Create key',
    successMsg: isEdit ? 'Key updated' : 'Key created',
  });
}

function dgFormSubmitPg() {
  const isEdit = dgForm.mode === 'edit';
  const rowEls = [...document.querySelectorAll('#dgFormBody .dgf-row')];
  const out = {};      // insert: all provided; edit: only changed
  const errors = [];
  for (const el of rowEls) {
    const name = el.dataset.field;
    const col = dg.meta.columns.find(c => c.name === name);
    const input = el.querySelector('.dgf-input');
    if (!input) continue;
    if (isEdit && col.is_pk) continue;  // pk is the identifier, never changed here
    const isNull = el.dataset.null === '1';
    const initNull = el.dataset.initnull === '1';
    const cat = input.dataset.cat;
    const raw = input.value;
    const init = input.dataset.init ?? '';
    if (isEdit && isNull === initNull && raw === init) continue;  // unchanged
    let value, omit = false;
    if (cat === 'bool') {
      if (raw === '') omit = true;                       // (default)
      else if (raw === '__null__') value = null;
      else value = raw === 'true';
    } else if (isNull) {
      value = null;
    } else if (raw === '') {
      if (isEdit) {
        if (cat === 'text') value = '';
        else if (col.nullable) value = null;
        else { errors.push(`${name} can't be empty`); continue; }
      } else if (col.has_default || col.nullable) {
        omit = true;                                     // let the DB default / NULL apply
      } else { errors.push(`${name} is required`); continue; }
    } else {
      try { value = dgCoerce(cat, raw); }
      catch (e) { errors.push(`${name}: ${e.message}`); continue; }
    }
    if (!omit) out[name] = value;
  }
  if (errors.length) { toast(errors[0], 'error'); return; }
  if (!Object.keys(out).length) { toast(isEdit ? 'Nothing changed' : 'Nothing to insert', 'error'); return; }
  const mode = dgForm.mode;

  const rel = `${dg.schema || 'public'}.${dg.table}`;
  if (mode === 'insert') {
    const cols = Object.keys(out);
    dgConfirm({
      action: 'insert', danger: false,
      endpoint: '/api/row-insert',
      payload: { database: dg.database, schema: dg.schema, table: dg.table, values: out },
      preview: `INSERT INTO ${rel}\n  (${cols.join(', ')})\nVALUES\n  (${cols.map(c => dgVal(out[c])).join(', ')})`,
      summary: 'Inserts 1 new row.', changes: null, confirmLabel: 'Insert row', successMsg: 'Row inserted',
    });
  } else {
    const pk = dgPgPk(dgForm.r);
    const cols = Object.keys(out);
    const setStr = cols.map(c => `${c} = ${dgVal(out[c])}`).join(',\n      ');
    const whereStr = Object.entries(pk).map(([k, v]) => `${k} = ${dgVal(v)}`).join(' AND ');
    const changeRows = cols.map(c => {
      const idx = dg.columns.indexOf(c);
      return { col: c, from: idx >= 0 ? dg.rows[dgForm.r][idx] : undefined, to: out[c] };
    });
    dgConfirm({
      action: 'update', danger: false,
      endpoint: '/api/row-update',
      payload: { database: dg.database, schema: dg.schema, table: dg.table, pk, changes: out },
      preview: `UPDATE ${rel}\nSET   ${setStr}\nWHERE ${whereStr}`,
      summary: 'Updates exactly 1 row. Previous values are overwritten.',
      changes: changeRows, confirmLabel: 'Save changes', successMsg: 'Row updated',
    });
  }
}

function dgFormSubmitMongo() {
  const mode = dgForm.mode;
  let edited;
  try { edited = JSON.parse(document.getElementById('dgJsonInput').value); }
  catch (e) { toast('Invalid JSON: ' + e.message, 'error'); return; }
  if (typeof edited !== 'object' || Array.isArray(edited) || edited === null) { toast('Document must be a JSON object', 'error'); return; }
  const coll = dg.collection;
  const isEs = dg.fam === 'elasticsearch';
  const noun = isEs ? 'document' : 'document';
  if (mode === 'insert') {
    dgConfirm({
      action: 'insert', danger: false,
      endpoint: '/api/row-insert',
      payload: { database: dg.database, collection: coll, document: edited },
      preview: isEs
        ? `POST /${coll}/_doc\n${JSON.stringify(edited, null, 2)}`
        : `db.getCollection('${coll}').insertOne(\n${JSON.stringify(edited, null, 2)}\n)`,
      summary: `Inserts 1 new ${noun}.`, changes: null, confirmLabel: 'Insert document', successMsg: 'Document inserted',
    });
  } else {
    const orig = dgReconstructDoc(dgForm.r);
    const set = {}, unset = [];
    for (const k of Object.keys(edited)) {
      if (k === '_id') continue;
      if (JSON.stringify(edited[k]) !== JSON.stringify(orig[k])) set[k] = edited[k];
    }
    for (const k of Object.keys(orig)) { if (k !== '_id' && !(k in edited)) unset.push(k); }
    if (!Object.keys(set).length && !unset.length) { toast('Nothing changed', 'error'); return; }
    const rep = dg.ids[dgForm.r];
    const parts = [];
    if (Object.keys(set).length) parts.push(`$set: ${JSON.stringify(set)}`);
    if (unset.length) parts.push(`$unset: ${JSON.stringify(Object.fromEntries(unset.map(k => [k, ''])))}`);
    dgConfirm({
      action: 'update', danger: false,
      endpoint: '/api/row-update',
      payload: { database: dg.database, collection: coll, id: rep, set, unset },
      preview: isEs
        ? `POST /${coll}/_update/${rep}\n${JSON.stringify({ doc: set, ...(unset.length ? { unset } : {}) }, null, 2)}`
        : `db.getCollection('${coll}').updateOne(\n  { _id: ${dgMongoIdJs(rep)} },\n  { ${parts.join(', ')} }\n)`,
      summary: `Updates exactly 1 ${noun}.`,
      changes: [...Object.keys(set).map(k => ({ col: k, from: orig[k], to: set[k] })),
                ...unset.map(k => ({ col: k, from: orig[k], to: undefined, removed: true }))],
      confirmLabel: 'Save changes', successMsg: 'Document updated',
    });
  }
}

function dgDeleteRow(r) {
  if (!dg || dg.locked || !dg.editable) return;
  if (dg.fam === 'redis') {
    const key = dg.rows[r][0];
    dgConfirm({
      action: 'delete', danger: true,
      endpoint: '/api/row-delete',
      payload: { database: dg.database, id: key },
      preview: `DEL ${key}`,
      summary: `Permanently deletes key ${key}. This cannot be undone.`,
      changes: null, rowPreview: { key, type: dg.rows[r][1], ttl: dg.rows[r][2], value: dg.rows[r][4] },
      confirmLabel: 'Delete key', successMsg: 'Key deleted',
    });
    return;
  }
  if (dg.collection) {
    const rep = dg.ids[r];
    dgConfirm({
      action: 'delete', danger: true,
      endpoint: '/api/row-delete',
      payload: { database: dg.database, collection: dg.collection, id: rep },
      preview: dg.fam === 'elasticsearch'
        ? `DELETE /${dg.collection}/_doc/${rep}`
        : `db.getCollection('${dg.collection}').deleteOne(\n  { _id: ${dgMongoIdJs(rep)} }\n)`,
      summary: 'Permanently deletes 1 document. This cannot be undone.',
      changes: null, rowPreview: dgReconstructDoc(r), confirmLabel: 'Delete document', successMsg: 'Document deleted',
    });
  } else {
    const pk = dgPgPk(r);
    const rel = `${dg.schema || 'public'}.${dg.table}`;
    const whereStr = Object.entries(pk).map(([k, v]) => `${k} = ${dgVal(v)}`).join(' AND ');
    dgConfirm({
      action: 'delete', danger: true,
      endpoint: '/api/row-delete',
      payload: { database: dg.database, schema: dg.schema, table: dg.table, pk },
      preview: `DELETE FROM ${rel}\nWHERE ${whereStr}`,
      summary: 'Permanently deletes exactly 1 row. This cannot be undone.',
      changes: null, rowPreview: dgReconstructDoc(r), confirmLabel: 'Delete row', successMsg: 'Row deleted',
    });
  }
}

// ── CRUD: confirmation ──────────────────────────────────────────────────────
let dgPendingOp = null;
function dgConfirm(op) {
  dgPendingOp = op;
  const badge = op.danger
    ? '<span class="dgc-badge danger">DELETE</span>'
    : `<span class="dgc-badge ${op.action==='insert'?'ok':'warn'}">${op.action.toUpperCase()}</span>`;
  let extra = '';
  if (op.changes && op.changes.length) {
    extra = `<table class="dgc-changes"><thead><tr><th>column</th><th>from</th><th>to</th></tr></thead><tbody>${
      op.changes.map(c => `<tr><td class="mono">${esc(c.col)}</td>
        <td>${dgCellHtml(c.from)}</td>
        <td>${c.removed ? '<span class="dg-null">removed</span>' : dgCellHtml(c.to)}</td></tr>`).join('')}</tbody></table>`;
  } else if (op.rowPreview) {
    extra = `<table class="dgc-changes"><tbody>${
      Object.entries(op.rowPreview).map(([k, v]) => `<tr><td class="mono">${esc(k)}</td><td>${dgCellHtml(v)}</td></tr>`).join('')}</tbody></table>`;
  }
  document.getElementById('dgConfirmBody').innerHTML = `
    <div class="dgc-head">${badge}<span class="dgc-target">${esc(dg.database)}.${esc(dg.name)}</span></div>
    <pre class="dgc-sql">${esc(op.preview)}</pre>
    ${extra}
    <div class="dgc-note ${op.danger?'danger':(op.action==='insert'?'ok':'warn')}">${esc(op.summary)}</div>`;
  const btn = document.getElementById('dgConfirmBtn');
  btn.textContent = op.confirmLabel;
  btn.className = 'btn btn-sm ' + (op.danger ? 'btn-danger' : 'btn-success');
  btn.disabled = false;
  document.getElementById('dgConfirmModal').classList.add('open');
}

async function dgConfirmExecute() {
  const op = dgPendingOp;
  if (!op || !dg) return;
  const btn = document.getElementById('dgConfirmBtn');
  btn.disabled = true; btn.textContent = 'Working…';
  const res = await apiPost(op.endpoint, op.payload).catch(() => ({ error: 'Request failed' }));
  if (!res || res.error) {
    toast((res && res.error) || 'Failed', 'error');
    btn.disabled = false; btn.textContent = op.confirmLabel;
    return;
  }
  dgPendingOp = null;
  document.getElementById('dgConfirmModal').classList.remove('open');
  document.getElementById('dgFormModal').classList.remove('open');
  toast(op.successMsg, 'success');
  dgRefresh();  // reflect true DB state (rows + stats)
}
function dgConfirmCancel() { document.getElementById('dgConfirmModal').classList.remove('open'); dgPendingOp = null; }

document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  const layers = ['dgConfirmModal', 'cellModal', 'dgFormModal'];
  for (const id of layers) {
    const el = document.getElementById(id);
    if (el && el.classList.contains('open')) { el.classList.remove('open'); if (id==='dgConfirmModal') dgPendingOp=null; return; }
  }
  if (document.getElementById('dataModal').classList.contains('open')) closeDataModal();
});

