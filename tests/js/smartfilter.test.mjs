// Unit tests for the smart-filter engine, run against the REAL functions in
// index.html (no duplication). Uses Node's built-in test runner + vm — no npm
// deps. Run with:  node --test tests/js/
//
// The extractor pulls named `function` declarations out of the inline <script>
// with string/comment-aware brace counting. (Regex literals containing literal
// { } are not supported by the extractor, but none of the filter functions use
// them.)

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
// non-strict assert: structural (prototype-agnostic) so cross-realm arrays
// returned from the vm context compare correctly.
import assert from 'node:assert';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, '../../packages/web/src/warden_web/static/index.html'), 'utf8');

function extractFn(src, name) {
  const start = src.indexOf(`function ${name}(`);
  if (start === -1) throw new Error(`function ${name} not found in index.html`);
  let i = src.indexOf('{', start);
  const bodyStart = i;
  let depth = 0, str = null, line = false, block = false;
  for (; i < src.length; i++) {
    const c = src[i], n = src[i + 1];
    if (line) { if (c === '\n') line = false; continue; }
    if (block) { if (c === '*' && n === '/') { block = false; i++; } continue; }
    if (str) { if (c === '\\') { i++; } else if (c === str) { str = null; } continue; }
    if (c === '/' && n === '/') { line = true; i++; continue; }
    if (c === '/' && n === '*') { block = true; i++; continue; }
    if (c === '"' || c === "'" || c === '`') { str = c; continue; }
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error(`unbalanced braces extracting ${name}`);
}

const NAMES = ['sfParseSize', 'sfTokens', 'sfParse', 'sfVals', 'sfTokenMatch', 'sfMatches'];
const code = NAMES.map((n) => extractFn(html, n)).join('\n\n');
const ctx = vm.createContext({});
const sf = vm.runInContext(code + `\n({ ${NAMES.join(', ')} })`, ctx);

// ── sfParseSize ──────────────────────────────────────────────────────────
test('sfParseSize parses units', () => {
  assert.equal(sf.sfParseSize('512'), 512);
  assert.equal(sf.sfParseSize('1kb'), 1024);
  assert.equal(sf.sfParseSize('1.5kb'), 1536);
  assert.equal(sf.sfParseSize('100mb'), 104857600);
  assert.equal(sf.sfParseSize('1gb'), 1073741824);
  assert.equal(sf.sfParseSize('2 mb'), 2097152);
});
test('sfParseSize rejects junk', () => {
  assert.equal(sf.sfParseSize('abc'), null);
  assert.equal(sf.sfParseSize(''), null);
});

// ── sfTokens ─────────────────────────────────────────────────────────────
test('sfTokens splits on spaces but keeps quoted groups', () => {
  assert.deepEqual(sf.sfTokens('a b c'), ['a', 'b', 'c']);
  assert.deepEqual(sf.sfTokens('name:"a b" x'), ['name:"a b"', 'x']);
  assert.deepEqual(sf.sfTokens(''), []);
});

// ── sfParse ──────────────────────────────────────────────────────────────
const FIELDS = [
  { key: 'name', type: 'text' }, { key: 'role', type: 'enum' },
  { key: 'status', type: 'bool' }, { key: 'size', type: 'size' },
];
test('sfParse separates field tokens from free text', () => {
  const p = sf.sfParse('role:admin foo bar', FIELDS);
  assert.equal(p.tokens.length, 1);
  assert.equal(p.tokens[0].field, 'role');
  assert.equal(p.tokens[0].val, 'admin');
  assert.equal(p.text, 'foo bar');
});
test('sfParse reads operators on size/number', () => {
  const p = sf.sfParse('size:>100mb', FIELDS);
  assert.equal(p.tokens[0].op, '>');
  assert.equal(p.tokens[0].val, '100mb');
});
test('sfParse handles negation', () => {
  const p = sf.sfParse('-status:on', FIELDS);
  assert.equal(p.tokens[0].neg, true);
  assert.equal(p.tokens[0].field, 'status');
});
test('sfParse treats unknown field as free text', () => {
  const p = sf.sfParse('bogus:x', FIELDS);
  assert.equal(p.tokens.length, 0);
  assert.equal(p.text, 'bogus:x');
});

// ── sfMatches (the whole filter) ─────────────────────────────────────────
const CFG = {
  textFields: ['name'],
  fields: [
    { key: 'name', type: 'text', get: (u) => u.user },
    { key: 'role', type: 'enum', get: (u) => u.roles },
    { key: 'status', type: 'bool', get: (u) => u.active },
    { key: 'size', type: 'size', get: (u) => u.bytes },
  ],
};
const ROWS = [
  { user: 'admin', roles: ['superuser', 'read'], active: true, bytes: 5000 },
  { user: 'alice', roles: ['read'], active: true, bytes: 100 },
  { user: 'bob', roles: [], active: false, bytes: 0 },
];
const filter = (q) => ROWS.filter((r) => sf.sfMatches(r, sf.sfParse(q, CFG.fields), CFG));

test('empty query matches everything', () => {
  assert.equal(filter('').length, 3);
});
test('free text matches the name field', () => {
  assert.deepEqual(filter('ali').map((r) => r.user), ['alice']);
});
test('enum token matches array membership (substring)', () => {
  assert.deepEqual(filter('role:super').map((r) => r.user), ['admin']);
  assert.deepEqual(filter('role:read').map((r) => r.user), ['admin', 'alice']);
});
test('bool token respects truthiness', () => {
  assert.deepEqual(filter('status:on').map((r) => r.user), ['admin', 'alice']);
  assert.deepEqual(filter('status:off').map((r) => r.user), ['bob']);
});
test('size operators compare numerically', () => {
  assert.deepEqual(filter('size:>1000').map((r) => r.user), ['admin']);
  assert.deepEqual(filter('size:>0').map((r) => r.user), ['admin', 'alice']);
});
test('multiple tokens AND together', () => {
  assert.deepEqual(filter('role:read status:on').map((r) => r.user), ['admin', 'alice']);
});
test('negation excludes matches', () => {
  assert.deepEqual(filter('-role:super').map((r) => r.user), ['alice', 'bob']);
});
test('text + token combine to narrow', () => {
  // 'li' hits only alice; role:read hits admin+alice → intersection is alice
  assert.deepEqual(filter('li role:read').map((r) => r.user), ['alice']);
});
