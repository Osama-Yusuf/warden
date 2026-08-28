// ── Query console ─────────────────────────────────────────────────────────

const DANGER_BADGE = {
  read: '<span class="pill pill-ok">READ-ONLY</span>',
  write: '<span class="pill pill-warn">MODIFIES DATA</span>',
  danger: '<span class="pill pill-danger">DESTRUCTIVE</span>',
  none: '',
};

function viewQuery() {
  if (!requireConnection()) return;
  const fam = engineFamily(currentEngine);
  const isDocdb = fam === 'documentdb';
  const ph = isDocdb
    ? 'db.<collection>.find({ <field>: <value> }).limit(5)'
    : 'SELECT <columns> FROM <table> WHERE <condition> LIMIT 10;';
  setContent(`<div class="card"><h2>${ICONS.term} Run Query</h2>
    <p class="card-meta">Runs as your admin user on <strong>${esc(creds().env)} / ${esc(engineLabel(currentEngine))}</strong>.
      The plain-english preview updates as you type. Read it before running. ⌘⏎ to run.</p>
    <div class="form-row" style="margin-bottom:10px">
      <div class="form-field"${fam === 'sqlite' ? ' style="display:none"' : ''}><label>Database</label><input id="qDb" list="dbList" placeholder="${isDocdb ? 'admin (default)' : 'default database'}"></div>
      <div class="form-field"><label>Snippets</label><select id="qSnippets" onchange="applySnippet()">
        <option value="">insert a ready-made query…</option>
        ${querySnippets().map((s, i) => `<option value="${i}">${esc(s.label)}</option>`).join('')}
      </select></div>
    </div>
    <div class="form-field" style="margin-bottom:10px"><label>${isDocdb ? 'mongosh query' : 'SQL'}</label>
      <textarea id="qText" class="query-input" spellcheck="false" placeholder="${esc(ph)}"></textarea></div>
    <div id="qExplain" class="explain-box"></div>
    <div id="qContext"></div>
    <div style="display:flex; gap:10px; align-items:center; margin-top:12px; flex-wrap:wrap">
      <button class="btn btn-primary" onclick="runQueryConfirm()">Run</button>
      <button class="btn btn-ghost" onclick="runExplain()" data-tip="Show the execution plan without modifying anything">Explain</button>
      <label class="chk" data-tip="Power mode: stream the engine's raw output live as it arrives, instead of the formatted table."><input type="checkbox" id="qLive"> live output</label>
      <span id="qTiming" style="font-family:var(--mono-font); font-size:11px; color:var(--text-muted)"></span>
    </div>
    <div id="qResults" style="margin-top:14px"></div>
    <div id="qHistory"></div>
  </div>`);
  const t = document.getElementById('qText');
  t.addEventListener('input', qExplainUpdate);
  t.addEventListener('keydown', e => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); runQueryConfirm(); } });
  document.getElementById('qDb').addEventListener('input', qExplainUpdate);
  const live = document.getElementById('qLive');
  live.checked = !!prefs.qLive;
  live.addEventListener('change', () => savePrefs({ qLive: live.checked }));
  qExplainUpdate();
  _qHistFilter = '';
  renderQueryHistory();
}

function querySnippets() {
  const fam = engineFamily(currentEngine);
  if (fam === 'mysql') {
    return [
      { label: 'Latest 10 rows', q: 'SELECT * FROM <table> ORDER BY <id_column> DESC LIMIT 10;' },
      { label: 'Row count (fast estimate)', q: "SELECT table_rows FROM information_schema.tables WHERE table_name = '<table>';" },
      { label: 'Running queries (longest first)', q: "SELECT id, user, time, state, LEFT(info, 100) AS query FROM information_schema.processlist WHERE command <> 'Sleep' ORDER BY time DESC;" },
      { label: 'Biggest tables', q: "SELECT table_schema, table_name, ROUND((data_length + index_length) / 1048576, 1) AS size_mb FROM information_schema.tables WHERE table_schema NOT IN ('information_schema','performance_schema','sys') ORDER BY (data_length + index_length) DESC LIMIT 20;" },
      { label: 'Kill a query by id', q: 'KILL <id>;' },
    ];
  }
  if (fam === 'sqlite') {
    return [
      { label: 'Latest 10 rows', q: 'SELECT * FROM <table> ORDER BY rowid DESC LIMIT 10;' },
      { label: 'Row count', q: 'SELECT COUNT(*) FROM <table>;' },
      { label: 'Table columns', q: "PRAGMA table_info(<table>);" },
      { label: 'All indexes', q: "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index';" },
      { label: 'Integrity check', q: 'PRAGMA integrity_check;' },
    ];
  }
  if (fam === 'elasticsearch') {
    return [
      { label: 'Match all (first 10)', q: '{\n  "query": { "match_all": {} },\n  "size": 10\n}' },
      { label: 'Full-text search', q: '{\n  "query": { "query_string": { "query": "<term>" } },\n  "size": 10\n}' },
      { label: 'Term filter', q: '{\n  "query": { "term": { "<field>": "<value>" } }\n}' },
      { label: 'Sort by a field', q: '{\n  "query": { "match_all": {} },\n  "sort": [{ "<field>": "desc" }],\n  "size": 10\n}' },
      { label: 'Aggregate (terms)', q: '{\n  "size": 0,\n  "aggs": { "by_field": { "terms": { "field": "<field>" } } }\n}' },
    ];
  }
  if (fam === 'redis') {
    return [
      { label: 'Read a key', q: 'GET <key>' },
      { label: 'Scan keys by pattern', q: 'SCAN 0 MATCH <prefix>:* COUNT 50' },
      { label: 'Inspect a hash', q: 'HGETALL <key>' },
      { label: 'Key type & TTL', q: 'TYPE <key>' },
      { label: 'Server info', q: 'INFO' },
      { label: 'Keys in this DB', q: 'DBSIZE' },
    ];
  }
  if (fam === 'documentdb') {
    return [
      { label: 'Find by _id', q: 'db.<collection>.find({ _id: ObjectId("<id>") })' },
      { label: 'Latest 10 documents', q: 'db.<collection>.find({}).sort({ _id: -1 }).limit(10)' },
      { label: 'Count documents (fast estimate)', q: 'db.<collection>.estimatedDocumentCount()' },
      { label: 'List indexes of a collection', q: 'db.<collection>.getIndexes()' },
      { label: 'Currently running operations', q: 'db.adminCommand({ currentOp: 1, active: true })' },
      { label: 'Connection count', q: 'db.adminCommand({ serverStatus: 1 }).connections' },
      { label: 'Database storage stats', q: 'db.runCommand({ dbStats: 1 })' },
    ];
  }
  return [
    { label: 'Latest 10 rows', q: 'SELECT * FROM <table> ORDER BY <id_column> DESC LIMIT 10;' },
    { label: 'Row count (fast estimate)', q: "SELECT reltuples::bigint AS estimated_rows FROM pg_class WHERE relname = '<table>';" },
    { label: 'Active queries (longest first)', q: "SELECT pid, usename, state, now() - query_start AS runtime, left(query, 100) AS query\nFROM pg_stat_activity WHERE state <> 'idle' AND pid <> pg_backend_pid()\nORDER BY runtime DESC NULLS LAST;" },
    { label: 'Biggest tables', q: "SELECT relname AS table, pg_size_pretty(pg_total_relation_size(c.oid)) AS size\nFROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace\nWHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog','information_schema')\nORDER BY pg_total_relation_size(c.oid) DESC LIMIT 20;" },
    { label: 'Connections per database', q: 'SELECT datname, count(*) FROM pg_stat_activity GROUP BY datname ORDER BY count(*) DESC;' },
    { label: 'Blocking locks', q: "SELECT blocked.pid AS blocked_pid, left(blocked.query, 60) AS blocked_query,\n       blocking.pid AS blocking_pid, left(blocking.query, 60) AS blocking_query\nFROM pg_stat_activity blocked\nJOIN pg_locks bl ON bl.pid = blocked.pid AND NOT bl.granted\nJOIN pg_locks gl ON gl.locktype = bl.locktype AND gl.database IS NOT DISTINCT FROM bl.database\n  AND gl.relation IS NOT DISTINCT FROM bl.relation AND gl.granted\nJOIN pg_stat_activity blocking ON blocking.pid = gl.pid;" },
    { label: 'Terminate a query by pid', q: 'SELECT pg_terminate_backend(<pid>);' },
  ];
}

function applySnippet() {
  const sel = document.getElementById('qSnippets');
  const s = querySnippets()[Number(sel.value)];
  sel.value = '';
  if (!s) return;
  const t = document.getElementById('qText');
  t.value = s.q;
  qExplainUpdate();
  t.focus();
}

function runExplain() {
  const t = document.getElementById('qText');
  let q = (t?.value || '').trim().replace(/;+\s*$/, '');
  if (!q) { toast('Type a query first', 'error'); return; }
  const database = document.getElementById('qDb').value.trim();
  if (currentEngine.startsWith('document')) {
    if (!/\.(find|aggregate)\s*\(/.test(q) || /\.explain\s*\(/.test(q)) {
      toast('Explain works on find() / aggregate() queries', 'error');
      return;
    }
    executeQuery(q + '.explain()', database);
  } else {
    executeQuery(/^\s*explain/i.test(q) ? q : 'EXPLAIN ' + q, database);
  }
}

function openQueryWith(db, text) {
  markActive('query');
  viewQuery();
  if (!document.getElementById('qText')) return;
  document.getElementById('qDb').value = db || '';
  document.getElementById('qText').value = text;
  qExplainUpdate();
  executeQuery(text, db || '');
}

let qTimer;
function qExplainUpdate() {
  clearTimeout(qTimer);
  qTimer = setTimeout(() => {
    const box = document.getElementById('qExplain');
    if (!box) return;
    const txt = document.getElementById('qText')?.value || '';
    const ex = explainQuery(engineFamily(currentEngine), txt);
    box.className = 'explain-box explain-' + ex.danger;
    box.innerHTML = (DANGER_BADGE[ex.danger] || '') + `<div class="explain-text">${ex.html}</div>`;
    qContextCheck();
  }, 200);
}

// Live target checks: does the db exist, does the collection/table exist

let collCache = {};      // db -> [names] | null (fetch failed)
let collFetching = {};

function extractMongoTarget(text) {
  let rest = (text || '').trim(), db = null;
  const m = rest.match(/^db\.getSiblingDB\(\s*['"]([^'"]+)['"]\s*\)\.([\s\S]*)$/);
  if (m) { db = m[1]; rest = 'db.' + m[2]; }
  const call = parseMongoCall(rest);
  return { db, coll: call ? call.coll : null, method: call ? call.method : null };
}

function extractSqlTable(text) {
  const first = splitTopLevel((text || '').trim(), ';')[0] || '';
  const m = first.match(/\b(?:from|into|update|truncate\s+(?:table\s+)?|join)\s+([a-zA-Z0-9_"]+)/i);
  return m ? m[1].replace(/"/g, '') : null;
}

const ctxLine = (cls, html) => `<div class="ctx-line ctx-${cls}">${html}</div>`;
const ctxSuggest = names => names.slice(0, 3).map(d =>
  `<button class="btn btn-ghost btn-sm" data-db="${esc(d)}" onclick="qSetDb(this.dataset.db)">use ${esc(d)}</button>`).join(' ');

function qSetDb(db) {
  const input = document.getElementById('qDb');
  if (!input) return;
  input.value = db;
  qExplainUpdate();
}

async function fetchColls(db) {
  if (collFetching[db]) return;
  collFetching[db] = true;
  try {
    const res = await apiPost('/api/list-collections', { database: db });
    if (res.collections) collCache[db] = res.collections.map(c => typeof c === 'string' ? c : c.name);
    else if (res.tables) collCache[db] = res.tables.map(t => t.table);
    else collCache[db] = null;
  } catch { collCache[db] = null; }
  collFetching[db] = false;
  qContextCheck();
}

function qContextCheck() {
  const box = document.getElementById('qContext');
  if (!box || !connected) { if (box) box.innerHTML = ''; return; }
  const isDocdb = currentEngine.startsWith('document');
  const txt = document.getElementById('qText')?.value.trim() || '';
  const dbInput = document.getElementById('qDb');
  if (!txt) { box.innerHTML = ''; return; }
  const lines = [];

  let effDb = dbInput.value.trim();
  let target = { coll: null, method: null };
  if (isDocdb) {
    target = extractMongoTarget(txt);
    if (target.db && target.db !== effDb) {
      dbInput.value = target.db;
      effDb = target.db;
      lines.push(ctxLine('ok', `database <b>${esc(target.db)}</b> picked up from the query (getSiblingDB)`));
    }
    if (!effDb) effDb = 'admin';
  } else {
    target.coll = extractSqlTable(txt);
    if (engineFamily(currentEngine) === 'sqlite' && !effDb) effDb = cachedDbs[0] || '';
  }

  // does the database exist on this cluster?
  let dbOk = true;
  if (effDb && cachedDbs.length) {
    if (!cachedDbs.includes(effDb)) {
      dbOk = false;
      const q = effDb.toLowerCase();
      const near = cachedDbs.filter(d => d.toLowerCase().includes(q) || q.includes(d.toLowerCase()));
      lines.push(ctxLine('bad', `database <b>${esc(effDb)}</b> does not exist on this cluster ${near.length ? '· ' + ctxSuggest(near) : ''}`));
    }
  }

  // does the collection/table exist in that database?
  if (target.coll && effDb && dbOk) {
    const colls = collCache[effDb];
    const isInsert = (target.method || '').startsWith('insert');
    if (colls === undefined) {
      fetchColls(effDb);
      lines.push(ctxLine('muted', `checking <b>${esc(target.coll)}</b> in ${esc(effDb)}…`));
    } else if (Array.isArray(colls)) {
      if (colls.includes(target.coll)) {
        lines.push(ctxLine('ok', `<b>${esc(target.coll)}</b> exists in database <b>${esc(effDb)}</b>, the query runs there`));
      } else if (isInsert && isDocdb) {
        lines.push(ctxLine('warn', `<b>${esc(target.coll)}</b> doesn't exist in <b>${esc(effDb)}</b> yet, the insert would CREATE it there`));
      } else {
        const elsewhere = Object.entries(collCache)
          .filter(([d, c]) => Array.isArray(c) && c.includes(target.coll)).map(([d]) => d);
        const stem = target.coll.toLowerCase().replace(/s$/, '');
        const guesses = cachedDbs.filter(d => {
          const dl = d.toLowerCase();
          return dl.includes(stem) || stem.includes(dl);
        }).filter(d => d !== effDb);
        const suggestions = [...new Set([...elsewhere, ...guesses])];
        lines.push(ctxLine('bad',
          `<b>${esc(target.coll)}</b> not found in database <b>${esc(effDb)}</b>, the query would match nothing there ${suggestions.length ? '· ' + ctxSuggest(suggestions) : ''}`));
        for (const g of suggestions.slice(0, 2)) if (collCache[g] === undefined) fetchColls(g);
      }
    }
  }
  box.innerHTML = lines.join('');
}

// Explainer: turns a query into plain english plus a danger level

function preview(v, max = 48) {
  v = String(v == null ? '' : v).trim().replace(/\s+/g, ' ');
  if (v.length > max) v = v.slice(0, max) + '…';
  return `<code>${esc(v)}</code>`;
}

function splitTopLevel(s, sep = ',') {
  const parts = [];
  let cur = '', depth = 0, inStr = null;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (inStr) { cur += c; if (c === inStr && s[i - 1] !== '\\') inStr = null; continue; }
    if (c === '"' || c === "'" || c === '`') { inStr = c; cur += c; continue; }
    if ('([{'.includes(c)) depth++;
    if (')]}'.includes(c)) depth--;
    if (c === sep && depth === 0) { parts.push(cur); cur = ''; continue; }
    cur += c;
  }
  if (cur.trim()) parts.push(cur);
  return parts.map(p => p.trim());
}

function shallowKeys(objText) {
  objText = (objText || '').trim();
  if (!objText.startsWith('{')) return [];
  const inner = objText.slice(1, objText.lastIndexOf('}'));
  return splitTopLevel(inner).map(pair => {
    const idx = (() => {
      let depth = 0, inStr = null;
      for (let i = 0; i < pair.length; i++) {
        const c = pair[i];
        if (inStr) { if (c === inStr && pair[i - 1] !== '\\') inStr = null; continue; }
        if (c === '"' || c === "'" || c === '`') { inStr = c; continue; }
        if ('([{'.includes(c)) depth++;
        if (')]}'.includes(c)) depth--;
        if (c === ':' && depth === 0) return i;
      }
      return -1;
    })();
    if (idx < 0) return { key: pair.trim().replace(/['"]/g, ''), value: '' };
    return {
      key: pair.slice(0, idx).trim().replace(/['"]/g, ''),
      value: pair.slice(idx + 1).trim(),
    };
  }).filter(kv => kv.key);
}

function kvSummary(objText, max = 4) {
  const kvs = shallowKeys(objText);
  if (!kvs.length) return '';
  const parts = kvs.slice(0, max).map(kv => `<b>${esc(kv.key)}</b> = ${preview(kv.value)}`);
  if (kvs.length > max) parts.push(`+${kvs.length - max} more`);
  return parts.join(', ');
}

function filterDesc(argText, emptyMeansAll) {
  argText = (argText || '').trim();
  if (!argText || /^\{\s*\}$/.test(argText)) {
    return emptyMeansAll ? { html: '<b style="color:var(--danger)">matching EVERY document</b>', all: true } : { html: '(all documents)', all: true };
  }
  const summary = kvSummary(argText);
  return { html: summary ? `where ${summary}` : `matching ${preview(argText, 80)}`, all: false };
}

function updateDesc(argText) {
  const ops = shallowKeys(argText);
  if (!ops.length) return `with ${preview(argText, 80)}`;
  const known = {
    '$set': v => `sets ${kvSummary(v, 5) || preview(v)}`,
    '$unset': v => `removes ${shallowKeys(v).length === 1 ? 'field' : 'fields'} ${shallowKeys(v).map(k => `<b>${esc(k.key)}</b>`).join(', ') || preview(v)}`,
    '$push': v => `appends to ${kvSummary(v)}`,
    '$addToSet': v => `adds to set ${kvSummary(v)}`,
    '$pull': v => `removes matching items from ${kvSummary(v)}`,
    '$inc': v => `increments ${kvSummary(v)}`,
    '$rename': v => `renames ${kvSummary(v)}`,
  };
  const hasOperator = ops.some(o => o.key.startsWith('$'));
  if (!hasOperator) return `<b style="color:var(--danger)">REPLACES the entire document</b> with ${kvSummary(argText) || preview(argText)}`;
  return ops.map(o => known[o.key] ? known[o.key](o.value) : `applies <code>${esc(o.key)}</code> to ${kvSummary(o.value) || preview(o.value)}`).join('; ');
}

function parseMongoCall(rest) {
  const m = rest.match(/^db\.([A-Za-z0-9_$\-]+(?:\.[A-Za-z0-9_$\-]+)*)\.([A-Za-z]+)\s*\(/);
  if (!m) return null;
  let i = m[0].length, depth = 1, inStr = null, args = '';
  for (; i < rest.length && depth > 0; i++) {
    const c = rest[i];
    if (inStr) { if (c === inStr && rest[i - 1] !== '\\') inStr = null; }
    else if (c === '"' || c === "'" || c === '`') inStr = c;
    else if ('([{'.includes(c)) depth++;
    else if (')]}'.includes(c)) { depth--; if (!depth) break; }
    args += c;
  }
  return { coll: m[1], method: m[2], args, tail: rest.slice(i + 1).trim() };
}

function explainMongo(text) {
  let dbNote = '';
  let rest = text;
  const sib = text.match(/^db\.getSiblingDB\(\s*['"]([^'"]+)['"]\s*\)\.([\s\S]*)$/);
  if (sib) { dbNote = ` in database <b>${esc(sib[1])}</b>`; rest = 'db.' + sib[2]; }

  // db-level methods (no collection)
  const dbLevel = rest.match(/^db\.([A-Za-z]+)\s*\(/);
  const call = parseMongoCall(rest);
  if (!call && dbLevel) {
    const dm = dbLevel[1];
    if (dm === 'dropDatabase') return { danger: 'danger', html: `<b style="color:var(--danger)">Deletes the ENTIRE database</b>${dbNote}: every collection and document in it.` };
    if (dm === 'getCollectionNames' || dm === 'stats' || dm === 'getCollectionInfos') return { danger: 'read', html: `Reads database metadata${dbNote} (no data is changed).` };
    if (/^(createUser|updateUser|grantRolesToUser|revokeRolesFromUser|dropUser)$/.test(dm)) return { danger: 'write', html: `Runs the user-management command <code>${esc(dm)}</code>${dbNote}. The Users pages do this with confirmation built in.` };
    if (dm === 'adminCommand' || dm === 'runCommand') return { danger: 'write', html: `Runs a raw database command${dbNote}. Effects depend on the command, review carefully.` };
    return { danger: 'write', html: `Runs <code>db.${esc(dm)}(…)</code>${dbNote}. Review carefully before running.` };
  }
  if (!call) return { danger: 'write', html: `Runs custom JavaScript${dbNote} that could read or modify anything. Review carefully before running.` };

  const { coll, method, args, tail } = call;
  const argList = splitTopLevel(args);
  const collB = `<b>${esc(coll)}</b>`;
  const mods = [];
  const lim = tail.match(/\.limit\(\s*(\d+)\s*\)/); if (lim) mods.push(`at most ${lim[1]} results`);
  if (/\.sort\(/.test(tail)) mods.push('sorted');
  const modTxt = mods.length ? ` (${mods.join(', ')})` : '';

  switch (method) {
    case 'find': case 'findOne': {
      const f = filterDesc(argList[0], false);
      const one = method === 'findOne' ? 'one document' : 'documents';
      return { danger: 'read', html: `<b>Reads</b> ${one} from ${collB}${dbNote} ${f.html}${modTxt}. Nothing is changed.` };
    }
    case 'countDocuments': case 'count': case 'estimatedDocumentCount':
      return { danger: 'read', html: `<b>Counts</b> documents in ${collB}${dbNote} ${filterDesc(argList[0], false).html}. Nothing is changed.` };
    case 'distinct':
      return { danger: 'read', html: `<b>Lists distinct values</b> of ${preview(argList[0])} in ${collB}${dbNote}. Nothing is changed.` };
    case 'aggregate': {
      const writes = /\$out|\$merge/.test(args);
      return writes
        ? { danger: 'danger', html: `Runs an aggregation on ${collB}${dbNote} that <b style="color:var(--danger)">WRITES its results to another collection</b> ($out/$merge).` }
        : { danger: 'read', html: `Runs an <b>aggregation pipeline</b> on ${collB}${dbNote} (${splitTopLevel(argList[0]?.replace(/^\[|\]$/g, '') || '').length || '?'} stages). Nothing is changed.` };
    }
    case 'insertOne':
      return { danger: 'write', html: `<b>Adds one new document</b> to ${collB}${dbNote}: ${kvSummary(argList[0]) || preview(argList[0])}.` };
    case 'insertMany':
      return { danger: 'write', html: `<b>Adds multiple new documents</b> to ${collB}${dbNote}.` };
    case 'updateOne': case 'findOneAndUpdate': {
      const f = filterDesc(argList[0], true);
      return { danger: f.all ? 'danger' : 'write', html: `<b>Updates ONE document</b> in ${collB}${dbNote} ${f.html}: ${updateDesc(argList[1] || '')}.` };
    }
    case 'updateMany': {
      const f = filterDesc(argList[0], true);
      return { danger: f.all ? 'danger' : 'write', html: `<b>Updates ALL documents</b> in ${collB}${dbNote} ${f.html}: ${updateDesc(argList[1] || '')}.` };
    }
    case 'replaceOne': {
      const f = filterDesc(argList[0], true);
      return { danger: 'write', html: `<b style="color:var(--danger)">REPLACES one entire document</b> in ${collB}${dbNote} ${f.html} with ${kvSummary(argList[1]) || preview(argList[1])}.` };
    }
    case 'deleteOne': {
      const f = filterDesc(argList[0], true);
      return { danger: 'danger', html: `<b style="color:var(--danger)">DELETES one document</b> from ${collB}${dbNote} ${f.html}. This cannot be undone.` };
    }
    case 'deleteMany': case 'remove': {
      const f = filterDesc(argList[0], true);
      return { danger: 'danger', html: `<b style="color:var(--danger)">DELETES ${f.all ? 'EVERY document' : 'all matching documents'}</b> from ${collB}${dbNote} ${f.all ? '' : f.html}. This cannot be undone.` };
    }
    case 'drop':
      return { danger: 'danger', html: `<b style="color:var(--danger)">DELETES the entire collection</b> ${collB}${dbNote} and all its documents. This cannot be undone.` };
    case 'createIndex':
      return { danger: 'write', html: `Creates an index on ${collB}${dbNote}: ${preview(argList[0], 80)}.` };
    case 'dropIndex': case 'dropIndexes':
      return { danger: 'danger', html: `<b style="color:var(--danger)">Drops index(es)</b> on ${collB}${dbNote}, which can slow queries down badly.` };
    case 'getIndexes': case 'stats':
      return { danger: 'read', html: `Reads metadata of ${collB}${dbNote}. Nothing is changed.` };
    default:
      return { danger: 'write', html: `Runs <code>${esc(method)}</code> on ${collB}${dbNote}. Not a recognized read operation, review carefully.` };
  }
}

function explainSql(text) {
  const stmts = splitTopLevel(text, ';').map(s => s.trim()).filter(Boolean);
  if (!stmts.length) return { danger: 'none', html: '' };
  const first = stmts[0];
  const kw = (first.match(/^[A-Za-z]+/) || [''])[0].toUpperCase();
  const table = re => { const m = first.match(re); return m ? `<b>${esc(m[1].replace(/"/g, ''))}</b>` : 'a table'; };
  const whereM = first.match(/\bwhere\b([\s\S]+?)(\border\s+by\b|\blimit\b|\bgroup\s+by\b|\breturning\b|$)/i);
  const where = whereM ? ` where ${preview(whereM[1], 90)}` : '';
  const more = stmts.length > 1 ? ` <b>+ ${plural(stmts.length - 1, 'more statement')}</b>, only the first is explained here.` : '';
  const bump = d => (stmts.length > 1 && d === 'read') ? 'write' : d;

  let r;
  switch (kw) {
    case 'SELECT': case 'WITH': case 'EXPLAIN': case 'SHOW':
      r = { danger: 'read', html: `<b>Reads</b> rows from ${table(/\bfrom\s+([a-zA-Z0-9_."]+)/i)}${where || ' (no filter)'}${/\blimit\b/i.test(first) ? '' : where ? '' : ', <b>all rows</b>'}. Nothing is changed.` };
      break;
    case 'INSERT':
      r = { danger: 'write', html: `<b>Adds new rows</b> to ${table(/\binto\s+([a-zA-Z0-9_."]+)/i)}.` };
      break;
    case 'UPDATE': {
      const cols = (first.match(/\bset\b([\s\S]+?)(\bwhere\b|$)/i) || [])[1] || '';
      const colList = splitTopLevel(cols).slice(0, 4).map(c => preview(c, 40)).join(', ');
      r = whereM
        ? { danger: 'write', html: `<b>Updates rows</b> in ${table(/\bupdate\s+([a-zA-Z0-9_."]+)/i)}${where}, setting ${colList}.` }
        : { danger: 'danger', html: `<b style="color:var(--danger)">Updates EVERY row</b> in ${table(/\bupdate\s+([a-zA-Z0-9_."]+)/i)} (no WHERE clause!), setting ${colList}.` };
      break;
    }
    case 'DELETE':
      r = whereM
        ? { danger: 'danger', html: `<b style="color:var(--danger)">DELETES rows</b> from ${table(/\bfrom\s+([a-zA-Z0-9_."]+)/i)}${where}. This cannot be undone.` }
        : { danger: 'danger', html: `<b style="color:var(--danger)">DELETES EVERY row</b> from ${table(/\bfrom\s+([a-zA-Z0-9_."]+)/i)} (no WHERE clause!). This cannot be undone.` };
      break;
    case 'TRUNCATE':
      r = { danger: 'danger', html: `<b style="color:var(--danger)">Removes ALL rows</b> from ${table(/\btruncate\s+(?:table\s+)?([a-zA-Z0-9_."]+)/i)} instantly. This cannot be undone.` };
      break;
    case 'DROP':
      r = { danger: 'danger', html: `<b style="color:var(--danger)">Permanently deletes</b> ${preview(first.replace(/^drop\s+/i, ''), 70)}. This cannot be undone.` };
      break;
    case 'ALTER':
      r = { danger: 'danger', html: `<b>Changes the structure</b> of ${preview(first.replace(/^alter\s+/i, ''), 70)}, which can break applications using it.` };
      break;
    case 'CREATE':
      r = { danger: 'write', html: `<b>Creates</b> ${preview(first.replace(/^create\s+/i, ''), 70)}.` };
      break;
    case 'GRANT': case 'REVOKE':
      r = { danger: 'write', html: `<b>Changes permissions</b>: ${preview(first, 90)}. The Permissions pages do this with confirmation built in.` };
      break;
    case 'VACUUM': case 'ANALYZE': case 'REINDEX':
      r = { danger: 'write', html: `Runs maintenance (<code>${esc(kw)}</code>). Safe for data but can load the server.` };
      break;
    default:
      r = { danger: 'write', html: `Runs <code>${esc(kw || first.slice(0, 20))}</code>. Not a recognized read statement, review carefully.` };
  }
  return { danger: bump(r.danger), html: r.html + more };
}

const REDIS_READ_JS = new Set(['GET','MGET','STRLEN','GETRANGE','SUBSTR','GETBIT','BITCOUNT','EXISTS','TYPE','TTL','PTTL','OBJECT','KEYS','SCAN','HSCAN','SSCAN','ZSCAN','RANDOMKEY','DBSIZE','HGET','HMGET','HGETALL','HKEYS','HVALS','HLEN','HEXISTS','LRANGE','LLEN','LINDEX','SMEMBERS','SCARD','SISMEMBER','SRANDMEMBER','ZRANGE','ZREVRANGE','ZRANGEBYSCORE','ZCARD','ZSCORE','ZRANK','ZCOUNT','XLEN','XRANGE','XINFO','INFO','MEMORY','PING','ECHO','TIME','COMMAND']);
const REDIS_DESTRUCTIVE = new Set(['FLUSHDB','FLUSHALL','DEL','UNLINK','RENAME','RENAMENX','MOVE','SWAPDB','SHUTDOWN','MIGRATE']);

function explainRedis(text) {
  const cmd = (text.trim().split(/\s+/)[0] || '').toUpperCase();
  if (!cmd) return { danger: 'none', html: '' };
  if (REDIS_READ_JS.has(cmd)) return { danger: 'read', html: `Reads with <code>${esc(cmd)}</code>. Nothing is changed.` };
  if (REDIS_DESTRUCTIVE.has(cmd)) return { danger: 'danger', html: `<code>${esc(cmd)}</code> deletes or destroys data — this cannot be undone.` };
  return { danger: 'write', html: `<code>${esc(cmd)}</code> can modify data. Review before running.` };
}

function explainEs() {
  // The console only issues _search, which never mutates.
  return { danger: 'read', html: 'Runs a read-only <code>_search</code> against the index.' };
}

function explainQuery(fam, text) {
  text = (text || '').trim();
  if (!text) return { danger: 'none', html: '<span style="color:var(--text-muted)">Type a query and a plain-english explanation of what it does will appear here.</span>' };
  if (fam === 'documentdb') return explainMongo(text);
  if (fam === 'redis') return explainRedis(text);
  if (fam === 'elasticsearch') return explainEs(text);
  return explainSql(text);
}

// Execution

function runQueryConfirm() {
  const query = document.getElementById('qText').value.trim();
  if (!query) { toast('Query is empty', 'error'); return; }
  const fam = engineFamily(currentEngine);
  const isDocdb = fam === 'documentdb';
  const database = document.getElementById('qDb').value.trim();
  const ex = explainQuery(fam, query);
  if (ex.danger === 'read') { executeQuery(query, database); return; }
  const target = `${esc(creds().env)} / ${esc(engineLabel(currentEngine))} / ${esc(database || (isDocdb ? 'admin' : 'default db'))}`;
  showModal(ex.danger === 'danger' ? '⚠ Destructive Query' : 'Confirm Query', `
    <div class="explain-box explain-${ex.danger}" style="margin-bottom:12px">${DANGER_BADGE[ex.danger]}<div class="explain-text">${ex.html}</div></div>
    <p style="margin-bottom:0">Target: <strong>${target}</strong></p>
  `, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Run it', cls: ex.danger === 'danger' ? 'btn-danger' : 'btn-primary', fn: () => executeQuery(query, database) },
  ]);
}

async function executeQuery(query, database) {
  if (document.getElementById('qLive')?.checked) return streamQuery(query, database);
  const box = document.getElementById('qResults');
  if (box) box.innerHTML = loadingInline('Running…');
  const t0 = performance.now();
  const res = await apiPost('/api/query', { query, database });
  const timing = document.getElementById('qTiming');
  if (timing) {
    const mode = res.mode === 'warm' ? ' · ⚡ warm session' : res.mode === 'cold' ? ' · cold start' : '';
    timing.textContent = `${Math.round(performance.now() - t0)} ms · db: ${res.database || ''}${mode}`;
  }
  pushQueryHistory(query);
  renderQueryHistory();
  renderQueryResults(res);
}

async function streamQuery(query, database) {
  const box = document.getElementById('qResults');
  if (!box) return;
  box.innerHTML = `<p class="card-meta" style="margin-bottom:6px">Live output, raw engine stream</p><pre class="result-pre" id="qLivePre"></pre>`;
  const pre = document.getElementById('qLivePre');
  const t0 = performance.now();
  pushQueryHistory(query);
  renderQueryHistory();
  try {
    const resp = await fetch(API + '/api/query-stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...creds(), query, database }),
    });
    if ((resp.headers.get('content-type') || '').includes('json')) {
      const err = await resp.json();
      box.innerHTML = `<div class="explain-box explain-danger"><div class="explain-text"><b>Failed:</b> ${esc(err.error || 'Unknown error')}</div></div>`;
      return;
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      pre.textContent += dec.decode(value, { stream: true });
      pre.scrollTop = pre.scrollHeight;
    }
    if (!pre.textContent.trim()) pre.textContent = '(no output, statement executed)';
  } catch (e) {
    pre.textContent += '\n[stream interrupted: ' + e.message + ']';
  }
  const timing = document.getElementById('qTiming');
  if (timing) timing.textContent = `${Math.round(performance.now() - t0)} ms · live stream`;
}

function jsonCell(v) {
  if (v == null) return '<span style="color:var(--text-muted)">null</span>';
  if (typeof v === 'object') {
    const s = JSON.stringify(v);
    return `<span data-tip="${esc(s.slice(0, 500))}">${esc(s.length > 60 ? s.slice(0, 60) + '…' : s)}</span>`;
  }
  return esc(String(v).length > 120 ? String(v).slice(0, 120) + '…' : String(v));
}

function downloadFile(name, text, type) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

function csvFromDocs(docs) {
  const cols = [];
  for (const d of docs) for (const k of Object.keys(d)) if (!cols.includes(k)) cols.push(k);
  const cell = v => {
    let s = v == null ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  return [cols.join(','), ...docs.map(d => cols.map(c => cell(d[c])).join(','))].join('\n');
}

function exportResults(kind) {
  const res = window._qLast;
  if (!res || !res.ok) return;
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
  if (res.data !== undefined && res.data !== null) {
    const json = JSON.stringify(res.data, null, 2);
    if (kind === 'copy') { copyText(json); return; }
    if (kind === 'json') { downloadFile(`warden-result-${stamp}.json`, json, 'application/json'); return; }
    if (kind === 'csv') {
      const docs = Array.isArray(res.data) ? res.data : [res.data];
      downloadFile(`warden-result-${stamp}.csv`, csvFromDocs(docs.filter(d => d && typeof d === 'object')), 'text/csv');
      return;
    }
  }
  if (res.csv !== undefined) {
    if (kind === 'copy') { copyText(res.csv); return; }
    if (kind === 'csv') { downloadFile(`warden-result-${stamp}.csv`, res.csv, 'text/csv'); return; }
    if (kind === 'json') {
      const rows = parseCSV(res.csv.trim());
      const docs = rows.slice(1).map(r => Object.fromEntries(rows[0].map((c, i) => [c, r[i]])));
      downloadFile(`warden-result-${stamp}.json`, JSON.stringify(docs, null, 2), 'application/json');
      return;
    }
  }
  copyText(res.output || '');
}

function renderQueryResults(res) {
  const box = document.getElementById('qResults');
  if (!box) return;
  window._qLast = res;
  if (!res.ok) {
    box.innerHTML = `<div class="explain-box explain-danger"><div class="explain-text"><b>Failed:</b> <pre class="result-pre" style="margin-top:8px">${esc(res.error || res.output || 'Unknown error')}</pre></div></div>`;
    return;
  }
  let html = '';
  const hasTabular = (Array.isArray(res.data) && res.data.length) || (res.csv && res.csv.trim());
  if (hasTabular || (res.data !== undefined && res.data !== null)) {
    html += `<div style="display:flex; gap:8px; margin-bottom:8px">
      <button class="btn btn-ghost btn-sm" onclick="exportResults('copy')">Copy</button>
      <button class="btn btn-ghost btn-sm" onclick="exportResults('json')">↓ JSON</button>
      ${hasTabular ? `<button class="btn btn-ghost btn-sm" onclick="exportResults('csv')">↓ CSV</button>` : ''}
    </div>`;
  }
  const trunc = res.truncated ? '<p class="card-meta">Output truncated. Narrow the query for full results.</p>' : '';

  if (res.data !== undefined && res.data !== null) {
    const data = res.data;
    if (Array.isArray(data) && data.length && data.every(d => d && typeof d === 'object' && !Array.isArray(d))) {
      const cols = [];
      for (const d of data.slice(0, 50)) for (const k of Object.keys(d)) if (!cols.includes(k)) cols.push(k);
      const shown = cols.slice(0, 10);
      const rows = data.slice(0, 200).map(d =>
        `<tr>${shown.map(c => `<td class="mono" style="font-size:11.5px">${jsonCell(d[c])}</td>`).join('')}</tr>`).join('');
      html += `<p class="card-meta">${plural(data.length, 'document')}${data.length > 200 ? ', showing first 200' : ''}${cols.length > 10 ? ` · ${plural(cols.length - 10, 'column')} hidden` : ''}</p>
        <div class="table-wrap"><table><thead><tr>${shown.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`;
    } else if (Array.isArray(data) && !data.length) {
      html += `<div class="empty-state">No documents matched.</div>`;
    } else {
      html += `<pre class="result-pre">${esc(JSON.stringify(data, null, 2))}</pre>`;
    }
  } else if (res.csv !== undefined) {
    const rows = parseCSV(res.csv.trim());
    if (rows.length > 1 && rows[0].length > 0) {
      const head = rows[0], body = rows.slice(1, 201);
      html += `<p class="card-meta">${plural(rows.length - 1, 'row')}${rows.length - 1 > 200 ? ', showing first 200' : ''}</p>
        <div class="table-wrap"><table><thead><tr>${head.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead>
        <tbody>${body.map(r => `<tr>${r.map(c => `<td class="mono" style="font-size:11.5px">${esc(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    } else {
      html += `<pre class="result-pre">${esc(res.output.trim() || '(no output, statement executed)')}</pre>`;
    }
    if (res.notices) html += `<pre class="result-pre" style="margin-top:8px; color:var(--text-muted)">${esc(res.notices.trim())}</pre>`;
  } else {
    html += `<pre class="result-pre">${esc((res.output || '').trim() || '(no output)')}</pre>`;
  }
  box.innerHTML = html + trunc;
}

function parseCSV(text) {
  const rows = [];
  let row = [], cur = '', inQ = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQ) {
      if (c === '"') { if (text[i + 1] === '"') { cur += '"'; i++; } else inQ = false; }
      else cur += c;
    }
    else if (c === '"') inQ = true;
    else if (c === ',') { row.push(cur); cur = ''; }
    else if (c === '\n') { row.push(cur); rows.push(row); row = []; cur = ''; }
    else if (c !== '\r') cur += c;
  }
  if (cur !== '' || row.length) { row.push(cur); rows.push(row); }
  return rows;
}

// History (localStorage, per engine)

function historyKey() { return 'warden.queryHistory.' + (currentEngine.startsWith('document') ? 'docdb' : 'pg'); }
function pinnedKey() { return 'warden.queryPinned.' + (currentEngine.startsWith('document') ? 'docdb' : 'pg'); }
function loadList(key) { try { return JSON.parse(localStorage.getItem(key)) || []; } catch { return []; } }
function saveList(key, list) { try { localStorage.setItem(key, JSON.stringify(list)); } catch {} }

function pushQueryHistory(q) {
  const h = [q, ...loadList(historyKey()).filter(x => x !== q)].slice(0, 15);
  saveList(historyKey(), h);
}

function togglePin(q) {
  let p = loadList(pinnedKey());
  p = p.includes(q) ? p.filter(x => x !== q) : [q, ...p].slice(0, 10);
  saveList(pinnedKey(), p);
  renderQueryHistoryList();   // list only, so the filter box keeps focus + text
}

// The recent/pinned list lives below the results, out of the run→result flow.
// The shell (label + filter box) is drawn once; typing only refreshes the list
// inside it, so the search input never loses focus mid-keystroke.
let _qHistFilter = '';

function renderQueryHistory() {
  const box = document.getElementById('qHistory');
  if (!box) return;
  if (!(loadList(pinnedKey()).length || loadList(historyKey()).length)) { box.innerHTML = ''; return; }
  box.innerHTML = `<div class="qhist">
    <div class="qhist-head">
      <span class="qhist-title">Recent queries</span>
      <input id="qHistSearch" class="qhist-search" type="search" placeholder="filter…"
             value="${esc(_qHistFilter)}" oninput="_qHistFilter = this.value; renderQueryHistoryList()">
    </div>
    <div id="qHistList" class="qhist-list"></div>
  </div>`;
  renderQueryHistoryList();
}

function renderQueryHistoryList() {
  const list = document.getElementById('qHistList');
  if (!list) return;
  const pinned = loadList(pinnedKey());
  const recent = loadList(historyKey()).filter(q => !pinned.includes(q));
  window._qHist = [...pinned, ...recent];
  const f = _qHistFilter.trim().toLowerCase();
  const hit = q => !f || q.toLowerCase().includes(f);
  const chip = (q, i, isPinned) => `<div class="qhist-row">
    <button class="copy-btn" onclick="togglePin(window._qHist[${i}])" data-tip="${isPinned ? 'Unpin' : 'Pin this query'}" style="font-size:12px">${isPinned ? '★' : '☆'}</button>
    <button class="hist-chip" onclick="recallQuery(${i})"><code>${esc(q.length > 105 ? q.slice(0, 105) + '…' : q)}</code></button>
  </div>`;
  const pinnedRows = pinned.map((q, i) => hit(q) ? chip(q, i, true) : '').join('');
  const recentRows = recent.map((q, i) => hit(q) ? chip(q, pinned.length + i, false) : '').join('');
  let html = '';
  if (pinnedRows) html += `<div class="qhist-group">Pinned</div>` + pinnedRows;
  if (recentRows) html += `<div class="qhist-group">Recent</div>` + recentRows;
  if (!html) html = `<div class="qhist-empty">Nothing matches “${esc(_qHistFilter.trim())}”.</div>`;
  list.innerHTML = html;
}

function recallQuery(i) {
  const q = (window._qHist || [])[i];
  if (!q) return;
  document.getElementById('qText').value = q;
  qExplainUpdate();
  document.getElementById('qText').focus();
}

