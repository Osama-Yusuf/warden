// ── Audit log ──
let auditEntries = [];
let auditFilter = { text: '', action: '' };

async function viewAuditLog() {
  setContent(loading('Reading audit log…'));
  const res = await fetch('/api/audit-log', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' }).then(r => r.json());
  if (!res.entries || !res.entries.length) {
    setContent(`<div class="card"><h2>${ICONS.scroll} Audit Log</h2><div class="empty-state">No audit entries yet. Actions you take here get recorded.</div></div>`);
    return;
  }
  auditEntries = res.entries;
  auditFilter = { text: '', action: '' };
  const chips = ['GRANT', 'REVOKE', 'CREATE', 'DROP', 'QUERY', 'PASSWORD'];
  setContent(`<div class="card"><h2>${ICONS.scroll} Audit Log</h2>
    <p class="card-meta">Last ${res.entries.length} entries (newest first) · stored at ~/.warden/audit.log</p>
    <div style="display:flex; gap:8px; align-items:center; margin-bottom:12px; flex-wrap:wrap">
      <div class="qf" style="width:260px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="6.2"/><path d="M15.8 15.8L21 21"/></svg>
        <input id="auditSearch" placeholder="filter entries…" oninput="auditFilter.text=this.value; renderAuditRows()"></div>
      ${chips.map(c => `<button class="btn btn-ghost btn-sm" id="chip-${c}" onclick="toggleAuditChip('${c}')">${c}</button>`).join('')}
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>Time</th><th>Target</th><th>Action</th><th>Details</th></tr></thead>
      <tbody id="auditRows"></tbody>
    </table></div></div>`);
  renderAuditRows();
}

function toggleAuditChip(action) {
  auditFilter.action = auditFilter.action === action ? '' : action;
  document.querySelectorAll('[id^="chip-"]').forEach(b => b.classList.remove('btn-primary'));
  if (auditFilter.action) document.getElementById('chip-' + auditFilter.action)?.classList.add('btn-primary');
  renderAuditRows();
}

function renderAuditRows() {
  const tbody = document.getElementById('auditRows');
  if (!tbody) return;
  const actionPill = a => {
    const up = a.toUpperCase();
    if (up.includes('DROP') || up.includes('REVOKE') || up.includes('DISABLE')) return `<span class="pill pill-danger">${esc(a)}</span>`;
    if (up.includes('CREATE') || up.includes('GRANT') || up.includes('ENABLE')) return `<span class="pill pill-ok">${esc(a)}</span>`;
    return `<span class="pill pill-warn">${esc(a)}</span>`;
  };
  const text = auditFilter.text.trim().toLowerCase();
  const rows = auditEntries.filter(line => {
    if (text && !line.toLowerCase().includes(text)) return false;
    if (auditFilter.action) {
      const m = line.match(/\]\s+([A-Z ]+?)(?:\s+—|$)/);
      if (!m || !m[1].includes(auditFilter.action)) return false;
    }
    return true;
  }).map(line => {
    const m = line.match(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\[([^\]]+)\]\s+(.*?)(?:\s+—\s+(.*))?$/);
    if (m) {
      return `<tr>
        <td class="mono" style="white-space:nowrap; color:var(--text-muted)">${esc(m[1])}</td>
        <td><span class="pill pill-accent">${esc(m[2])}</span></td>
        <td>${actionPill(m[3])}</td>
        <td class="mono" style="font-size:11.5px">${esc(m[4] || '')}</td>
      </tr>`;
    }
    return `<tr><td colspan="4" class="mono" style="white-space:pre-wrap; font-size:12px">${esc(line)}</td></tr>`;
  }).join('');
  tbody.innerHTML = rows || `<tr><td colspan="4" style="color:var(--text-muted)">No entries match the filter.</td></tr>`;
}

