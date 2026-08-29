# tests

Proof warden isn't lying to you. Three flavors:

- **unit** (`test_util.py`, `test_native_helpers.py`, `test_server_validation.py`) fast, no network, no databases. The security-critical validators and pure helpers. These are the ones that must never go red.
- **integration** ([`integration/test_engines.py`](integration/)) drives the real `api_*` handlers against live engines: connect, list, browse, the works. Marked `integration` and **skipped by default**, opt in with `-m integration`. Each engine is skipped gracefully if it isn't reachable.
- **js** ([`js/smartfilter.test.mjs`](js/)) runs the real smart-filter functions straight out of `filter.js` (no copy-paste) using node's built-in test runner. No npm.

### Running them
```bash
uv run pytest                    # unit only (integration is deselected by default)
uv run pytest -m integration     # the live-engine ones (needs databases up)
node --test tests/js/            # the front-end filter tests
```

### The live engines
Integration + the smoke want the engines reachable on localhost. There's a compose file that spins them all up with the exact creds the tests expect:

```bash
cd tests/engines && ./engines.sh up      # or: docker compose up -d
```

See [`engines/`](engines/) for the details (subsets, ports, creds). Point the tests somewhere else with `WARDEN_TEST_<ENGINE>_HOST/PORT/USER/PASS`.

**Gotcha:** MySQL's client CLI needs to be on `PATH`, e.g. `PATH="/opt/homebrew/opt/mysql-client/bin:$PATH" uv run pytest -m integration`.
