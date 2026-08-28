# warden

Every database has a door. warden is the guy standing at it.

It holds the keys, decides who gets in and what they can touch, kicks people out, and writes everything down. It'll also read a query back to you in plain english before running it, so you know exactly what you're about to do to prod.

Works with DocumentDB/MongoDB, PostgreSQL (Aurora and friends), MySQL/MariaDB, plain SQLite files, Elasticsearch/OpenSearch, and Redis/Valkey/ElastiCache. One core, three ways to use it: CLI, web UI, desktop app.

## What it does

- manage users: create, drop, reset passwords, grant and revoke with confirmation
- smart filters on the users, databases, and collection lists: type `field:value` (`role:kibana_admin`, `size:>100mb`, `status:disabled`) with autocomplete, one-click preset chips per engine, and a live result count
- data browser: rows & columns for any table/collection/index (or a key browser for Redis), with server-side search, sortable size, and safe in-grid CRUD that's locked by default and confirms exactly what it'll run
- query console with a plain-english preview ("Updates ONE document in orders where _id = ...") and a danger badge before anything runs
- security audits: who's admin, who can write where, dead accounts. Exclude the known ones so only real issues show up
- cluster health: connections, slow queries, cache hit, replication lag
- environment guardrails: tag a connection (prod/staging/uat/dev/…, picked in the connection form or auto-inferred from its name) and warden paints the bar red on prod, shows the env badge, and defaults prod to read-only, so you don't fat-finger the wrong environment
- read-only mode for when you just want to look at prod without fear
- quick-jump search (⌘K) across users, databases, and tables; copy any browsed row as JSON or a ready-to-paste INSERT
- every change lands in an append-only audit log

All six engines are on the same footing now: connect, data browsing, cluster health, the query console, in-grid CRUD, and user management. For Elasticsearch that's native-realm users and roles (X-Pack or the OpenSearch security plugin); for Redis it's ACL users (`ACL SETUSER`), so you can create a `cache:*` read-only user in a couple of clicks.

## Running it

You need [uv](https://docs.astral.sh/uv/). DocumentDB, PostgreSQL, Elasticsearch/OpenSearch, and Redis all talk to the server through bundled native drivers (pymongo, psycopg, urllib3 for the ES REST API, redis-py), so mongosh and psql are only needed for the free-form Mongo/PG query console. MySQL/MariaDB and SQLite still use their `mysql` / `sqlite3` clients.

```sh
make setup
make dev        # web UI at http://127.0.0.1:8642
```

No environments ship with it. Open Settings > Environments and add your clusters, or import a backup from another warden.

| command | what |
|---|---|
| `make dev` | web UI, auto reload |
| `make dev-desktop` | desktop app, auto restart |
| `make cli ARGS="docdb list-users"` | the CLI |
| `make build` | wheels for all packages |
| `make build-desktop` | native bundle for this OS |
| `make dmg` | macOS installer |

Or skip make entirely: `uv run warden`, `uv run warden-web`, `uv run warden-desktop`.

Desktop builds for other OSes come from CI. Push a tag like `v1.0.0` and it builds macOS (arm + intel) and Windows bundles and attaches them to a release. PyInstaller can't cross compile so that's the way.

## Tests

```sh
make test              # python unit tests + smart-filter JS tests
make test-integration  # drives live engines, skips any that aren't reachable
```

Unit tests (`tests/`) cover the pure logic with no database needed: engine routing, the validation and quoting guards that sit in front of every query, the native-driver parsers, and the smart-filter engine. The JS tests run the real filter functions pulled straight out of `index.html`, so there's no copy to drift.

Integration tests (`tests/integration/`) drive the actual server handlers against each engine (connect, list databases, list users, browse a page) and skip cleanly when an engine isn't up. Point them at your own hosts with `WARDEN_TEST_<ENGINE>_HOST/PORT/USER/PASS`.

CI (`.github/workflows/test.yml`) runs unit + JS on every push and PR, plus the integration tests against Postgres, MariaDB, MongoDB, and Redis service containers.

## Config

Environments can live in a few places, whatever suits you:

1. server profiles in `~/.warden/profiles.sqlite`, managed from the UI with "save on server". Shared by the CLI, web and desktop on that machine
2. plain JSON shaped `{env: {engine: {host, port, ...}}}` in `environments.local.json`, `~/.warden_environments.json`, or wherever `WARDEN_ENVIRONMENTS_FILE` points
3. browser-local custom envs from the UI
4. backup import, optionally passphrase encrypted. Carries envs, saved credentials, audit results, all of it

Engine keys are matched loosely by name: `mongo`/`document` → MongoDB, `mysql`/`maria` → MySQL, `sqlite` → a file path, `redis`/`valkey`/`elasticache`/`memorydb` → Redis, `elastic`/`opensearch` → Elasticsearch, and everything else → postgres-compatible. So `aurora-mysql`, `aurora`, and `elasticache-redis` all land on the right driver. (The redis check runs before elasticsearch on purpose, since "elasticache" contains "elastic".)

SQLite is a bit special: no server, no credentials. Point an environment at a file path, or just upload a .db file from Settings > Environments and warden stores it under `~/.warden/sqlite/`. You get the query console, tables with sizes, and a health card (integrity check included). No users to manage, so those pages hide themselves.

Elasticsearch/OpenSearch and Redis don't require credentials in the form: ES may be unauthenticated, Redis is often password-only. Leave the fields blank or fill what your cluster needs; the driver handles it. For ES the cluster shows as one "database" whose indices are the browsable units; for Redis the numbered DBs (0..N) are the "databases" and keys are grouped by `prefix:` namespace.

Credentials are saved per env + engine, with optional macOS Keychain storage. Env vars are in `.env.example`. The audit trail sits at `~/.warden/audit.log`.

TLS connections verify the server certificate by default (system trust store + hostname). For self-signed certs or private CAs (including AWS RDS / DocumentDB / ElastiCache), tick **trust invalid cert** on the connection to skip verification. The web server also only answers same-origin requests, so a random page you visit can't drive it while it's running.

## Notes for hacking on it

- the web server reads index.html from disk on every request, so UI edits are just a browser refresh. `make dev` restarts on py changes too
- DocumentDB and PostgreSQL structured operations go through pooled native drivers (`warden_core/mongo_native.py` via pymongo, `warden_core/pg_native.py` via psycopg). Pooling means repeated ops (navigation, background refresh) skip the per-call connection cost, and a real pool makes concurrent requests safe. The mongosh/psql subprocesses are only used for the free-form query console (arbitrary JS/SQL a structured driver shouldn't run). If a driver isn't importable, that engine transparently falls back to its subprocess
- the pg pool pre-flights one direct connect so a wrong password fails in milliseconds instead of the pool retrying for its whole timeout window
- Elasticsearch/OpenSearch go through the REST API over a pooled urllib3 client (`warden_core/es_native.py`) rather than an SDK: ES and OpenSearch share the same `_cat`/`_cluster`/`_search` surface, so one client covers both without the version-incompatibility headaches. Redis/Valkey/ElastiCache use pooled redis-py (`warden_core/redis_native.py`); the key browser pages with SCAN (never KEYS) so a big keyspace is never blocked
- new engines stay fully isolated: a new `engine_family()` branch plus new handler branches, no edits to the existing engine paths. `engine_family` defaults anything unknown to postgres, so any new engine must register its family explicitly or it gets misrouted
- the query console uses a warm mongosh session (PTY based). The first one pays the connection cost, the rest take milliseconds
- mongosh has no `--db` flag and silently ignores it. The target db goes in the connection URI path. Don't "fix" that
- the desktop app needs private_mode off and a fixed port, otherwise localStorage resets every launch
