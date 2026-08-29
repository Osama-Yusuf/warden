# js (nine files, one brain)

The app used to be one giant `<script>`. Now it's nine, split by what they do. They're **classic scripts sharing a single global scope**, so any function in one file is callable from another (and from inline `onclick=`). No imports, no exports, no bundler.

**Load order matters** (it's the order in `index.html`), because top-level `const`/`let` in one file aren't hoisted into the ones loaded before it. `init()` is the very last line to run, on purpose.

| file | what's in it |
|---|---|
| `core.js` | the shell: globals, prefs/theme, init, sidebar nav, keychain, connect, tooltips, helpers |
| `filter.js` | the smart-filter engine (`field:value` search) + the user-info panel |
| `users.js` | the Users page: create / grant / revoke / reset / drop, test-login |
| `browser.js` | the data browser: the rows-and-columns grid + in-grid CRUD |
| `audit.js` | the Activity view (reads the audit log) |
| `query.js` | the query console + the recent-queries panel |
| `environments.js` | the Connections page + the add/edit connection form |
| `monitoring.js` | cluster health + the security audits |
| `settings.js` | backup/restore, wipe, keyboard shortcuts |

### Adding a function
Drop it in the file that fits, call it from wherever, done, it's global. Just don't reference another file's top-level `const` *at load time*; at click time everything's loaded and you're fine.

### Heads up
The smart-filter functions (`sfParse`, `sfMatches`, ...) live in `filter.js` and are unit-tested by [`tests/js/smartfilter.test.mjs`](../../../../../../tests/js/), which extracts them by name. Rename one and update the test.
