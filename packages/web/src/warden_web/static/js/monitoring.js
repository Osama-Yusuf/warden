// ── Cluster health ─────────────────────────────────────────────────────────

function fmtUptime(sec) {
  sec = Number(sec) || 0;
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

function viewHealth() {
  if (!requireConnection()) return;
  setContent(`<div class="card"><h2>${ICONS.pulse} Health</h2>
    <div style="display:flex; gap:12px; align-items:center; margin-bottom:12px">
      <button class="btn btn-ghost btn-sm" onclick="refreshHealth()">Refresh</button>
      <label class="chk"><input type="checkbox" id="healthAuto" onchange="healthAutoChanged()"> auto-refresh (10s)</label>
      <span id="healthMeta" style="font-family:var(--mono-font); font-size:11px; color:var(--text-muted)"></span>
    </div>
    <div class="chips" id="healthChips">${loadingInline('Checking cluster…')}</div>
    <div id="healthOps" style="margin-top:16px"></div>
  </div>`);
  const auto = document.getElementById('healthAuto');
  auto.checked = !!prefs.healthAuto;
  healthAutoChanged();
  refreshHealth();
}

function healthAutoChanged() {
  const on = document.getElementById('healthAuto')?.checked;
  savePrefs({ healthAuto: !!on });
  if (window._viewTimer) { clearInterval(window._viewTimer); window._viewTimer = null; }
  if (on) window._viewTimer = setInterval(refreshHealth, 10000);
}

async function refreshHealth() {
  const chips = document.getElementById('healthChips');
  if (!chips) return;
  const res = await apiPost('/api/health');
  const meta = document.getElementById('healthMeta');
  if (!document.getElementById('healthChips')) return;
  if (res.error) {
    document.getElementById('healthChips').innerHTML = `<div class="explain-box explain-danger"><div class="explain-text">${esc(res.error)}</div></div>`;
    return;
  }
  if (meta) meta.textContent = `updated ${new Date().toLocaleTimeString()}`;
  const { html, ops } = healthChips(res);
  document.getElementById('healthChips').innerHTML = html;
  document.getElementById('healthOps').innerHTML = ops;
}

// Build the chip row + optional slow-ops/queries table for whichever engine
// answered. Returns { html, ops } as strings; the caller drops them into place.
function healthChips(res) {
  const chip = (k, v, warnIf) => `<div class="chip"><span class="k">${esc(k)}</span><span class="v"${warnIf ? ' style="color:var(--danger)"' : ''}>${v}</span></div>`;
  const num = v => (typeof v === 'number' && isFinite(v)) ? v : null;
  let html = '';
  let ops = '';
  if (res.engine === 'documentdb') {
    const h = res.health || {};
    const c = h.connections || {};
    const cur = num(c.current), avail = num(c.available);
    html += chip('version', esc(h.version || '-'));
    html += chip('uptime', num(h.uptime) != null ? fmtUptime(h.uptime) : '-');
    html += chip('connections', cur != null ? `${cur}${avail != null ? ' / ' + (cur + avail) : ''}` : '-');
    html += chip('active ops', esc(num(h.active_ops) ?? '-'));
    if (num(h.mem?.resident)) html += chip('memory', `${h.mem.resident} MB`);
    const slow = h.slow_ops || [];
    html += chip('slow ops (≥5s)', slow.length, slow.length > 0);
    if (slow.length) {
      ops = `<h2>Slow operations</h2><div class="table-wrap"><table>
        <thead><tr><th>OpId</th><th>Running</th><th>Namespace</th><th>Op</th></tr></thead>
        <tbody>${slow.map(o => `<tr><td class="mono">${esc(o.opid)}</td><td>${esc(o.secs)}s</td><td class="mono">${esc(o.ns || '')}</td><td>${esc(o.op || '')}</td></tr>`).join('')}</tbody>
      </table></div>`;
    }
  } else if (res.engine === 'mysql') {
    const h = res.health || {};
    html += chip('version', esc(h.version || '-'));
    html += chip('uptime', h.uptime ? fmtUptime(Number(h.uptime)) : '-');
    html += chip('connections', h.threads ? `${esc(h.threads)} / ${esc(h.max_connections || '?')}` : '-');
    if (h.total_size) html += chip('total size', fmtSize(Number(h.total_size)));
    const slow = h.slow_queries || [];
    html += chip('slow queries (>=5s)', slow.length, slow.length > 0);
    if (slow.length) {
      ops = `<h2>Slow queries</h2><div class="table-wrap"><table>
        <thead><tr><th>Id</th><th>User</th><th>Runtime</th><th>Query</th></tr></thead>
        <tbody>${slow.map(q => `<tr><td class="mono">${esc(q.pid)}</td><td>${esc(q.user)}</td><td>${esc(q.runtime)}</td><td class="mono" style="font-size:11px">${esc(q.query)}</td></tr>`).join('')}</tbody>
      </table></div>`;
    }
  } else if (res.engine === 'sqlite') {
    const h = res.health || {};
    html += chip('file', esc((h.file || '').split('/').pop() || '-'));
    html += chip('size', fmtSize(Number(h.size_bytes) || 0));
    html += chip('version', esc(h.version || '-'));
    if (h.page_count && h.page_size) html += chip('pages', `${esc(h.page_count)} x ${esc(h.page_size)}B`);
    html += chip('journal', esc(h.journal_mode || '-'));
    html += chip('tables', esc(h.tables ?? '-'));
    const ok = (h.integrity || '').trim() === 'ok';
    html += chip('integrity', ok ? 'ok' : esc(h.integrity || '-'), !ok);
  } else if (res.engine === 'elasticsearch') {
    const h = res.health || {};
    html += chip('cluster', esc(h.cluster_name || '-'));
    html += chip('status', esc(h.status || '-'), h.status === 'red');
    html += chip('nodes', `${esc(h.nodes ?? '-')}${h.data_nodes != null ? ' (' + h.data_nodes + ' data)' : ''}`);
    html += chip('indices', esc(h.indices ?? '-'));
    html += chip('documents', num(h.docs) != null ? Number(h.docs).toLocaleString() : '-');
    if (h.size_bytes) html += chip('store size', fmtSize(Number(h.size_bytes)));
    html += chip('active shards', esc(h.active_shards ?? '-'));
    html += chip('unassigned shards', esc(h.unassigned_shards ?? '-'), Number(h.unassigned_shards) > 0);
    if (h.heap_used_bytes) html += chip('heap used', fmtSize(Number(h.heap_used_bytes)));
  } else if (res.engine === 'redis') {
    const h = res.health || {};
    html += chip('version', esc(h.version || '-'));
    html += chip('role', esc(h.role || '-'));
    html += chip('uptime', num(h.uptime) != null ? fmtUptime(h.uptime) : '-');
    html += chip('keys', num(h.total_keys) != null ? Number(h.total_keys).toLocaleString() : '-');
    html += chip('clients', `${esc(h.connected_clients ?? '-')}${h.maxclients ? ' / ' + h.maxclients : ''}`);
    if (h.used_memory) html += chip('memory', `${fmtSize(Number(h.used_memory))}${Number(h.maxmemory) > 0 ? ' / ' + fmtSize(Number(h.maxmemory)) : ''}`);
    if (h.used_memory_peak) html += chip('peak memory', fmtSize(Number(h.used_memory_peak)));
    const hits = num(h.keyspace_hits), misses = num(h.keyspace_misses);
    if (hits != null && misses != null && (hits + misses) > 0) html += chip('hit rate', ((hits / (hits + misses)) * 100).toFixed(1) + '%');
    html += chip('ops/sec', esc(h.ops_per_sec ?? '-'));
    html += chip('evicted keys', esc(h.evicted_keys ?? '-'), Number(h.evicted_keys) > 0);
  } else {
    const h = res.health || {};
    const conns = h.connections || [];
    html += chip('version', esc(h.version?.[0] || '-'));
    html += chip('uptime', esc(h.uptime?.[0] || '-'));
    html += chip('connections', conns.length >= 4 ? `${esc(conns[2])} / ${esc(conns[3])} (${esc(conns[0])} active)` : '-');
    html += chip('cache hit', h.cache_hit_pct?.[0] ? `${esc(h.cache_hit_pct[0])}%` : '-');
    html += chip('blocked queries', esc(h.blocked?.[0] ?? '-'), Number(h.blocked?.[0]) > 0);
    const reps = h.replicas || [];
    html += chip('replicas', reps[0] ? `${esc(reps[0])}${reps[1] ? ' · lag ' + esc(reps[1]) : ''}` : '0');
    if (h.total_size?.[0]) html += chip('total size', fmtSize(Number(h.total_size[0])));
    const slow = h.slow_queries || [];
    html += chip('slow queries (≥5s)', slow.length, slow.length > 0);
    if (slow.length) {
      ops = `<h2>Slow queries</h2><div class="table-wrap"><table>
        <thead><tr><th>PID</th><th>User</th><th>Runtime</th><th>Query</th></tr></thead>
        <tbody>${slow.map(q => `<tr><td class="mono">${esc(q.pid)}</td><td>${esc(q.user)}</td><td>${esc(q.runtime)}</td><td class="mono" style="font-size:11px">${esc(q.query)}</td></tr>`).join('')}</tbody>
      </table></div>`;
    }
  }
  return { html, ops };
}

// ── Security audits ────────────────────────────────────────────────────────

function auditKind() { return engineFamily(currentEngine); }
function auditCatalog() { return (config.audits || []).filter(a => a.engine === auditKind()); }
function auditRunKey(id) { return `${document.getElementById('envSelect').value}:${auditKind()}:${id}`; }
function auditExclKey(id) { return `${document.getElementById('envSelect').value}:${id}`; }
function loadAuditRuns() { try { return JSON.parse(localStorage.getItem('warden.auditRuns')) || {}; } catch { return {}; } }
function loadExclusions() { try { return JSON.parse(localStorage.getItem('warden.auditExcl')) || {}; } catch { return {}; } }
function saveExclusions(map) { try { localStorage.setItem('warden.auditExcl', JSON.stringify(map)); } catch {} }
function exclFor(id) { return loadExclusions()[auditExclKey(id)] || []; }

function sevPill(s) {
  return s === 'critical' ? '<span class="pill pill-danger">critical</span>'
    : s === 'warning' ? '<span class="pill pill-warn">warning</span>'
    : '<span class="pill pill-muted">info</span>';
}

function auditIssueCount() {
  const runs = loadAuditRuns();
  let n = 0;
  for (const a of auditCatalog()) {
    const run = runs[auditRunKey(a.id)];
    if (!run) continue;
    const excluded = new Set(exclFor(a.id).map(e => e.user));
    n += (run.findings || []).filter(f => !excluded.has(f.user) && f.severity !== 'info').length;
  }
  return n;
}

async function runAudit(id, silent = false) {
  const res = await apiPost('/api/audit-run', { audit: id });
  if (res.error) { if (!silent) toast(res.error, 'error'); return null; }
  const runs = loadAuditRuns();
  runs[auditRunKey(id)] = res;
  try { localStorage.setItem('warden.auditRuns', JSON.stringify(runs)); } catch {}
  renderBadges();
  return res;
}

function viewAudits() {
  if (!requireConnection()) return;
  const runs = loadAuditRuns();
  const totals = { critical: 0, warning: 0, info: 0, excluded: 0 };
  const rows = auditCatalog().map(a => {
    const run = runs[auditRunKey(a.id)];
    const excluded = new Set(exclFor(a.id).map(e => e.user));
    let counts = '';
    if (run) {
      const live = (run.findings || []).filter(f => !excluded.has(f.user));
      const exCount = (run.findings || []).length - live.length;
      totals.excluded += exCount;
      for (const f of live) totals[f.severity] = (totals[f.severity] || 0) + 1;
      counts = `${plural(live.length, 'finding')}${exCount ? ` · ${exCount} excluded` : ''} · ${esc(run.ran_at)}`;
    } else {
      counts = 'never run';
    }
    return `<tr>
      <td>${sevPill(a.severity)}</td>
      <td><strong>${esc(a.title)}</strong><br><span style="color:var(--text-muted); font-size:12px">${esc(a.description)}</span></td>
      <td style="color:var(--text-muted); font-size:12px; white-space:nowrap">${counts}</td>
      <td style="text-align:right; white-space:nowrap">
        <button class="btn btn-ghost btn-sm" data-id="${esc(a.id)}" onclick="viewAuditDetail(this.dataset.id)">Open</button>
        <button class="btn btn-primary btn-sm" data-id="${esc(a.id)}" onclick="runAudit(this.dataset.id).then(r => r && viewAuditDetail(this.dataset.id))">Run</button>
      </td></tr>`;
  }).join('');
  setContent(`<div class="card"><h2>${ICONS.shield} Security Audits</h2>
    <p class="card-meta">Access reviews for <strong>${esc(document.getElementById('envSelect').value)} / ${esc(engineLabel(currentEngine))}</strong>.
      Exclude known accounts (with a reason) so the findings show only real issues.</p>
    <div class="chips" style="margin-bottom:14px">
      <div class="chip"><span class="k">critical</span><span class="v" style="color:${totals.critical ? 'var(--danger)' : 'inherit'}">${totals.critical}</span></div>
      <div class="chip"><span class="k">warning</span><span class="v" style="color:${totals.warning ? 'var(--warning)' : 'inherit'}">${totals.warning}</span></div>
      <div class="chip"><span class="k">info</span><span class="v">${totals.info}</span></div>
      <div class="chip"><span class="k">excluded</span><span class="v">${totals.excluded}</span></div>
      <button class="btn btn-success" style="align-self:center" onclick="runAllAudits()">Run all</button>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>Severity</th><th>Audit</th><th>Last run</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div></div>`);
}

async function runAllAudits() {
  const catalog = auditCatalog();
  toast(`Running ${catalog.length} audits…`, 'info');
  for (const a of catalog) await runAudit(a.id, true);
  toast('All audits finished', 'success');
  viewAudits();
}

async function viewAuditDetail(id) {
  markActive('audits');
  const def = (config.audits || []).find(a => a.id === id);
  if (!def) return;
  let run = loadAuditRuns()[auditRunKey(id)];
  if (!run) {
    setContent(loading(`Running audit: ${def.title}…`));
    run = await runAudit(id);
    if (!run) { viewAudits(); return; }
  }
  const exclusions = exclFor(id);
  const excludedSet = new Set(exclusions.map(e => e.user));
  const live = (run.findings || []).filter(f => !excludedSet.has(f.user));
  const excludedFindings = (run.findings || []).filter(f => excludedSet.has(f.user));

  const findingRow = f => `<tr>
    <td><input type="checkbox" class="audit-sel" data-user="${esc(f.user)}" onchange="auditSelChanged()"></td>
    <td>${sevPill(f.severity)}</td>
    <td class="mono" style="cursor:pointer" data-user="${esc(f.user)}" onclick="viewUserInfoFor(this.dataset.user)" data-tip="Open user page">${esc(f.user)}</td>
    <td>${esc(f.summary)}</td>
    <td class="mono" style="font-size:11px; color:var(--text-muted)">${esc(f.detail)}</td>
    <td style="text-align:right"><button class="btn btn-ghost btn-sm" data-id="${esc(id)}" data-user="${esc(f.user)}"
      onclick="excludeFromAudit(this.dataset.id, this.dataset.user)">Exclude</button></td></tr>`;

  let html = `<div class="card">
    <h2>${ICONS.shield} ${esc(def.title)}</h2>
    <p class="card-meta">${esc(def.description)} · ${plural(run.scanned, 'account')} checked · ${esc(run.ran_at)}</p>
    <div style="display:flex; gap:8px; margin-bottom:14px; flex-wrap:wrap">
      <button class="btn btn-ghost btn-sm" onclick="viewAudits()">← All audits</button>
      <button class="btn btn-primary btn-sm" data-id="${esc(id)}" onclick="runAudit(this.dataset.id).then(r => r && viewAuditDetail(this.dataset.id))">Re-run</button>
      <button class="btn btn-ghost btn-sm" data-id="${esc(id)}" onclick="exportAudit(this.dataset.id, 'json')">↓ JSON</button>
      <button class="btn btn-ghost btn-sm" data-id="${esc(id)}" onclick="exportAudit(this.dataset.id, 'csv')">↓ CSV</button>
      <button class="btn btn-primary btn-sm" id="exclSelBtn" style="display:none" data-id="${esc(id)}"
        onclick="excludeSelected(this.dataset.id)">Exclude selected</button>
    </div>`;
  html += live.length
    ? `<div class="table-wrap"><table>
        <thead><tr><th><input type="checkbox" id="auditSelAll" onchange="auditSelAll(this.checked)" data-tip="Select all"></th><th>Severity</th><th>User</th><th>Finding</th><th>Detail</th><th></th></tr></thead>
        <tbody>${live.map(findingRow).join('')}</tbody></table></div>`
    : `<div class="empty-state">No findings${excludedFindings.length ? ' (everything is excluded)' : ', all clear'} ✓</div>`;
  if (exclusions.length) {
    html += `<h2 style="margin-top:20px">Excluded (${exclusions.length})</h2>
      <div class="table-wrap"><table>
        <thead><tr><th>User</th><th>Reason</th><th>Since</th><th></th></tr></thead>
        <tbody>${exclusions.map(e => `<tr style="opacity:0.6">
          <td class="mono">${esc(e.user)}</td><td>${esc(e.reason || '-')}</td>
          <td style="color:var(--text-muted)">${esc(e.at || '')}</td>
          <td style="text-align:right"><button class="btn btn-ghost btn-sm" data-id="${esc(id)}" data-user="${esc(e.user)}"
            onclick="restoreToAudit(this.dataset.id, this.dataset.user)">Restore</button></td></tr>`).join('')}</tbody>
      </table></div>`;
  }
  html += '</div>';
  setContent(html);
}

function auditSelected() {
  return [...document.querySelectorAll('.audit-sel:checked')].map(c => c.dataset.user);
}

function auditSelChanged() {
  const btn = document.getElementById('exclSelBtn');
  if (!btn) return;
  const n = auditSelected().length;
  btn.style.display = n ? '' : 'none';
  btn.textContent = `Exclude selected (${n})`;
  const all = document.getElementById('auditSelAll');
  const boxes = document.querySelectorAll('.audit-sel');
  if (all) all.checked = n > 0 && n === boxes.length;
}

function auditSelAll(checked) {
  document.querySelectorAll('.audit-sel').forEach(c => { c.checked = checked; });
  auditSelChanged();
}

function excludeSelected(id) {
  const users = auditSelected();
  if (!users.length) return;
  showModal(`Exclude ${plural(users.length, 'user')}`, `
    <p>Exclude <strong>${users.slice(0, 6).map(esc).join(', ')}${users.length > 6 ? ` +${users.length - 6} more` : ''}</strong> from this audit's findings?</p>
    <div class="form-field" style="margin-bottom:0">
      <label>Reason, applied to all (recommended)</label>
      <input id="exclReason" placeholder="e.g. service accounts, approved admins">
    </div>`, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: `Exclude ${users.length}`, cls: 'btn-primary', fn: () => {
      const reason = document.getElementById('exclReason')?.value.trim() || '';
      const at = new Date().toISOString().slice(0, 10);
      const map = loadExclusions();
      const key = auditExclKey(id);
      const kept = (map[key] || []).filter(e => !users.includes(e.user));
      map[key] = [...kept, ...users.map(user => ({ user, reason, at }))];
      saveExclusions(map);
      renderBadges();
      toast(`Excluded ${plural(users.length, 'user')}`, 'success');
      viewAuditDetail(id);
    }},
  ]);
}

function excludeFromAudit(id, user) {
  showModal(`Exclude ${user}`, `
    <p>Exclude <strong>${esc(user)}</strong> from this audit's findings so you can focus on real issues?</p>
    <div class="form-field" style="margin-bottom:0">
      <label>Reason (recommended, future you will ask)</label>
      <input id="exclReason" placeholder="e.g. service account, approved admin">
    </div>`, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Exclude', cls: 'btn-primary', fn: () => {
      const map = loadExclusions();
      const key = auditExclKey(id);
      map[key] = [...(map[key] || []).filter(e => e.user !== user),
        { user, reason: document.getElementById('exclReason')?.value.trim() || '', at: new Date().toISOString().slice(0, 10) }];
      saveExclusions(map);
      renderBadges();
      viewAuditDetail(id);
    }},
  ]);
}

function restoreToAudit(id, user) {
  const map = loadExclusions();
  const key = auditExclKey(id);
  map[key] = (map[key] || []).filter(e => e.user !== user);
  saveExclusions(map);
  renderBadges();
  viewAuditDetail(id);
}

function exportAudit(id, kind) {
  const run = loadAuditRuns()[auditRunKey(id)];
  if (!run) return;
  const excluded = new Set(exclFor(id).map(e => e.user));
  const rows = (run.findings || []).map(f => ({ ...f, excluded: excluded.has(f.user) }));
  const stamp = run.ran_at.replace(/[: ]/g, '-');
  if (kind === 'json') {
    downloadFile(`audit-${id}-${stamp}.json`, JSON.stringify({ ...run, findings: rows }, null, 2), 'application/json');
  } else {
    downloadFile(`audit-${id}-${stamp}.csv`, csvFromDocs(rows), 'text/csv');
  }
}

