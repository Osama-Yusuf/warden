// ── Backup & transfer (everything under warden.* keys) ─────────────────────

const BACKUP_VERSION = 1;
const b64buf = buf => btoa(String.fromCharCode(...new Uint8Array(buf)));
const unb64buf = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));

function collectExportData() {
  const local = {}, session = {};
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (k.startsWith('warden.')) local[k] = localStorage.getItem(k);
  }
  for (let i = 0; i < sessionStorage.length; i++) {
    const k = sessionStorage.key(i);
    if (k.startsWith('warden.')) session[k] = sessionStorage.getItem(k);
  }
  return { app: 'warden', version: BACKUP_VERSION, exported_at: new Date().toISOString(), local, session };
}

async function backupKey(pass, salt) {
  const material = await crypto.subtle.importKey('raw', new TextEncoder().encode(pass), 'PBKDF2', false, ['deriveKey']);
  return crypto.subtle.deriveKey({ name: 'PBKDF2', salt, iterations: 150000, hash: 'SHA-256' },
    material, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
}

async function encryptBackup(payload, pass) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const key = await backupKey(pass, salt);
  const ct = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, key,
    new TextEncoder().encode(JSON.stringify(payload)));
  return { app: 'warden', version: BACKUP_VERSION, encrypted: true,
           salt: b64buf(salt), iv: b64buf(iv), data: b64buf(ct) };
}

async function decryptBackup(obj, pass) {
  const key = await backupKey(pass, unb64buf(obj.salt));
  const pt = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: unb64buf(obj.iv) }, key, unb64buf(obj.data));
  return JSON.parse(new TextDecoder().decode(pt));
}

async function doExportAll() {
  const pass = document.getElementById('beoPass')?.value || '';
  const payload = collectExportData();
  const out = pass ? await encryptBackup(payload, pass) : payload;
  downloadFile(`warden-backup-${new Date().toISOString().slice(0, 10)}.json`,
    JSON.stringify(out, null, 2), 'application/json');
  toast(pass ? 'Encrypted backup downloaded' : 'Backup downloaded. It contains saved passwords, keep it safe', pass ? 'success' : 'info');
}

function importBackupPicked(input) {
  const file = input.files?.[0];
  input.value = '';
  if (!file) return;
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      let obj = JSON.parse(reader.result);
      if (obj.app !== 'warden' && obj.app !== 'dbctl') throw new Error('not a warden backup file');
      if (obj.encrypted) {
        const pass = document.getElementById('beoPass')?.value || '';
        if (!pass) { toast('This backup is encrypted, enter the passphrase in the field first', 'error'); return; }
        try { obj = await decryptBackup(obj, pass); }
        catch { toast('Wrong passphrase', 'error'); return; }
      }
      confirmImport(obj);
    } catch (e) {
      toast('Import failed: ' + e.message, 'error');
    }
  };
  reader.readAsText(file);
}

function confirmImport(obj) {
  // Normalize keys from pre-rename (dbctl) backups
  const norm = o => Object.fromEntries(Object.entries(o || {}).map(([k, v]) => [k.replace(/^dbctl\./, 'warden.'), v]));
  obj = { ...obj, local: norm(obj.local), session: norm(obj.session) };
  const local = obj.local || {};
  const parse = (k, fb) => { try { return JSON.parse(local[k]) || fb; } catch { return fb; } };
  const nCreds = Object.keys(parse('warden.creds', {})).length;
  const nPass = Object.keys(local).filter(k => k.startsWith('warden.k')).length;
  const nEnvs = Object.values(parse('warden.customEnvs', {})).reduce((s, e) => s + Object.keys(e).length, 0);
  const nRuns = Object.keys(parse('warden.auditRuns', {})).length;
  const nExcl = Object.values(parse('warden.auditExcl', {})).reduce((s, l) => s + l.length, 0);
  const line = (n, label) => n ? `<div>• ${plural(n, label)}</div>` : '';
  showModal('Import Backup', `
    <p>Backup from <strong>${esc((obj.exported_at || 'unknown date').slice(0, 19).replace('T', ' '))}</strong> containing:</p>
    <div style="font-size:13px; line-height:1.8; margin-bottom:12px">
      ${line(nCreds, 'saved login')}${line(nPass, 'stored password')}${line(nEnvs, 'custom environment / override')}
      ${line(nRuns, 'audit result')}${line(nExcl, 'audit exclusion')}
      ${!(nCreds || nPass || nEnvs || nRuns || nExcl) ? '<div>preferences and history only</div>' : ''}
    </div>
    <p>This replaces the matching settings here, then reloads the app.</p>`, [
    { label: 'Cancel', cls: 'btn-ghost' },
    { label: 'Import', cls: 'btn-primary', fn: () => {
      for (const [k, v] of Object.entries(obj.local || {})) { try { localStorage.setItem(k, v); } catch {} }
      for (const [k, v] of Object.entries(obj.session || {})) { try { sessionStorage.setItem(k, v); } catch {} }
      toast('Imported, reloading…', 'success');
      setTimeout(() => location.reload(), 700);
    }},
  ]);
}

// ── Keyboard: Esc closes modal, ⌘K / Ctrl+K focuses quick-jump ──
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeModal(); closePanel(); }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    document.getElementById('qfInput').focus();
  }
});

// ── keyboard shortcuts: "/" focus filter, "g"+key nav, "?" cheatsheet ──
let _gPending = false, _gTimer = null;
document.addEventListener('keydown', e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target, tag = (t.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select' || t.isContentEditable) return;
  if (e.key === '/') {
    e.preventDefault();
    (document.querySelector('.dg-search input, [id^="sf-in-"]') || document.getElementById('qfInput'))?.focus();
    return;
  }
  if (e.key === '?') { e.preventDefault(); showShortcutHelp(); return; }
  if (_gPending) {
    _gPending = false; clearTimeout(_gTimer);
    const dest = { u: 'users', d: 'databases', q: 'query', a: 'audits', c: 'envs', v: 'audit' }[e.key.toLowerCase()];
    if (dest) { e.preventDefault(); navigate(dest); }
    return;
  }
  if (e.key.toLowerCase() === 'g') { _gPending = true; _gTimer = setTimeout(() => { _gPending = false; }, 900); }
});

function showShortcutHelp() {
  const rows = [
    ['/', 'Focus the filter / search on this page'],
    ['⌘ / Ctrl + K', 'Jump to a user, database, or table'],
    ['g then u', 'Go to Users'],
    ['g then d', 'Go to Databases'],
    ['g then q', 'Go to Query'],
    ['g then a', 'Go to Audits'],
    ['g then c', 'Go to Connections'],
    ['Esc', 'Close a dialog or panel'],
    ['?', 'Show this help'],
  ].map(([k, d]) => `<kbd>${esc(k)}</kbd><span>${esc(d)}</span>`).join('');
  showModal('Keyboard shortcuts', `<div class="kbd-grid">${rows}</div>`, [{ label: 'Close', cls: 'btn-primary' }]);
}

init();
