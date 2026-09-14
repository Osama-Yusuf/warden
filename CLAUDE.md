# warden

A multi-engine database management tool: browse data, run queries, and manage users/roles across Postgres, MySQL/MariaDB, MongoDB, Elasticsearch, Redis, and SQLite. Ships as a CLI, a local web app, and a desktop app (same core, three frontends).

Public repo: `Osama-Yusuf/warden`. MIT licensed. Early development. There is nothing company-specific in here (no easyGenerator references, no secrets, `BUILTIN_ENVIRONMENTS = {}`) and it must stay that way.

## Working rules (read these first)

- **Never commit, push, open a PR, or merge without explicit consent.** Ask every time. Approval for one push does not carry to the next.
- **No AI attribution anywhere** — not in commits, PR bodies, branch names, or code comments. No `Co-Authored-By`. Conventional Commits, title-only or 1-2 short lines. See the git-etiquette skill.
- **No em dashes in any prose** (comments, docs, commit messages, UI copy). Write like a human. See the human-writing skill.
- **Test against real engines, not mocks.** Every fix to a user-management path gets verified live against an actual database before it's called done. The user has been burned by "looks right" fixes that didn't work in their Aurora setup.
- Comments explain *why*, humanized, matching the surrounding density. Don't narrate the obvious.

## Layout

Monorepo, uv-managed Python. Four packages under `packages/`:

- `core/` — `warden_core`, the engine. Adapters, validation, native drivers. Frontends never talk to a DB directly, they go through this.
- `web/` — `warden_web`, a stdlib HTTP server (`server.py`) plus a static single-page UI under `static/`.
- `cli/` — `warden_cli`, terminal frontend.
- `desktop/` — `warden_desktop`, a pywebview window wrapping the web UI. Builds to `dist/warden.app` / `warden.dmg`.

Inside `core/src/warden_core/`:
- `adapters/` — one file per engine (`postgres.py`, `mysql.py`, `mongo.py`, `elasticsearch.py`, `redis.py`, `sqlite.py`), all subclassing `base.py`. This is where engine-specific behavior lives.
- `pg.py`, `pg_native.py` — Postgres query helpers. `pg_native.py` uses psycopg3 with a `ConnectionPool`. `pg.py` wraps it (`pg_query`, `pg_exec`, `pg_csv`).
- `mysql.py`, `mongo_native.py`, `es_native.py`, `redis_native.py`, `docdb.py` — same idea per engine.
- `validation.py` — identifier/privilege validation (`validate_ident`, `pg_ident`, `pg_literal`, `validate_pg_privilege`, `validate_target`). Use these, never hand-build SQL with raw user input.
- `config.py`, `audit.py`, `util.py`, `ai/`.

Web UI JS (`web/src/warden_web/static/js/`): `core.js` (shared helpers, creds, modals, icons), `filter.js` (user-detail panel + DB-access table), `users.js`, `query.js`, `browser.js`, `audit.js`, `monitoring.js`, `settings.js`, `environments.js`, `assistant.js`.

## Running it

```
make dev              # web dev server on localhost:8642 with auto-reload (tools/dev.py)
make dev-desktop      # desktop window, auto-reload
make test             # pytest -q + node --test tests/js/*.test.mjs
make test-integration # pytest -m integration (live engines, needs the containers up)
make build            # uv build all packages to dist/
make build-desktop    # dist/warden.app
make dmg              # dist/warden.dmg
make install-desktop  # rebuilds if needed, installs to /Applications/warden.app
```

`tools/dev.py` watches `packages/**/*.py` and restarts the target on change. **Python edits reload automatically. Static JS/CSS is served fresh on browser refresh, no restart.** So after editing an adapter or `server.py`, just wait a beat; after editing JS, just refresh the page.

The dev server on the host connects to the user's **real Aurora Postgres**. The host cannot reach container-internal IPs inside OrbStack, so for browser-based UI testing the target is Aurora, and for adapter integration tests the target is the local dev containers.

## Testing against live engines

Integration tests live in `tests/integration/test_engines.py`, marked `@pytest.mark.integration`, gated by reachability probes (e.g. `_pg_reachable()` which calls `pn.close_all()`). The engines come from `tests/engines/docker-compose.yml`:

- postgres:16, mariadb:11 (deliberately MariaDB to exercise the MySQL/MariaDB split), mongo:6, elasticsearch:8.13.4, redis, and SQLite is fileless.

Bring them up / reset:
```
docker compose -f tests/engines/docker-compose.yml up -d
docker compose -f tests/engines/docker-compose.yml down -v && up -d   # full reset
```

**OrbStack containers are flaky.** They corrupt their WAL and change IPs between restarts (`exit 1` on the PG container, IP drifting). When integration tests suddenly can't connect, the fix is almost always `down -v` then `up -d`, not a code change. See the `warden-ci-startup-failure` memory: hosted CI minutes are out, CI runs on a self-hosted Docker runner.

## Postgres user-management: the load-bearing facts

Most of the hard work in this repo is making Postgres user management honest and correct, especially for a **non-superuser admin** (the Aurora `postgres` master role is NOT a real superuser). These are the truths the code is built around. Breaking any of them silently regresses:

- **PUBLIC is a pseudo-role.** Every account gets `CONNECT` + `TEMP` on every database and `USAGE` on the `public` schema by default, granted to `PUBLIC`, not to the user. You cannot deny it per-user. The only way to remove it is to revoke from PUBLIC cluster-wide (a "lockdown"), then re-grant to the roles that should keep it. This is why a freshly created user appears to "have access to everything."

- **REVOKE is a silent no-op when you're not the grantor.** Postgres returns success and revokes nothing if the invoker didn't grant the privilege (and isn't a superuser). Never trust the command status. Read the ACL back via `aclexplode` and verify. `revoke()` in `postgres.py` does this and raises with the grantor's name when it couldn't actually revoke.

- **DROP USER / REASSIGN OWNED / DROP OWNED need INHERIT membership** when the admin isn't a superuser. The admin must be a member of the target role *with inherit* first: `GRANT role TO admin WITH INHERIT TRUE` (PG16 syntax). The error text Postgres gives is misleading, and a superuser masks the whole problem, which is why it wasn't caught early. `_strip_role` handles this.

- **These ops take AccessExclusiveLock**, so they need a `lock_timeout` or they hang forever behind an active connection. `_ADMIN_OPTS = "-c lock_timeout=5s -c statement_timeout=60s"`, applied via `_bounded_statements`.

- **`GRANT ON ALL TABLES` aborts the whole statement if one table is owned by another role.** One foreign-owned table and nothing gets granted. Fall back to table-by-table so the rest still applies.

- **Schemas are not just `public`.** Grants must walk every target schema dynamically (`_target_schemas`, `_user_schemas`), not hardcode `public`, or grants silently miss tables.

- **`information_schema` views are permission-filtered.** A non-superuser can't see grants it isn't party to, and `information_schema.columns` hides columns on tables it can't read (shows COLUMNS as 0). Use `pg_class` / `pg_database` / `pg_namespace` with `aclexplode`, and `pg_attribute` for column counts.

- **`has_*_privilege` counts PUBLIC and inherited access.** Great for "can this user effectively do X", wrong for "what was granted directly to this user". Both notions are needed, see below.

- **`pg_shdepend` is a cluster-shared catalog.** With `refclassid = 'pg_authid'::regclass, deptype = 'a', refobjid = <role oid>` it lists exactly which databases hold object grants for a role. This lets `access_map` avoid a per-database round trip for the common connect-only case.

- **PG16 `CREATEROLE` no longer implies superuser-like power** over roles it didn't create. Relevant when the admin creates and manages users.

- **Query console runs natively via psycopg, not system psql.** `console_csv()` in `pg_native.py`. psycopg3 in autocommit runs multi-statement scripts and steps result sets with `cur.nextset()`. `pg_csv` is native-first and only shells to psql for backslash meta-commands.

- **MariaDB stores account lock in a `global_priv` JSON blob**, not in `mysql.user` columns. Branch on `is_mariadb()`. (MySQL-specific, but same "engines lie differently" theme.)

### effective vs explicit privileges (important distinction)

Two adapter methods answer two different questions, and mixing them up produces the confusing-and-wrong UI we already fixed once:

- `effective_db_privileges(name, database, schema)` — what the user **can actually do**, PUBLIC and role membership included (via `has_*_privilege`). A table privilege counts only if held on every table in the target schemas. Used by the **grant** form so it doesn't offer privileges the user already effectively has.

- `explicit_db_privileges(name, database)` — what was granted **directly to this role** (via `aclexplode` where grantee is the role oid). This is the only set that's actually revocable from one user. Used by the **revoke** form. PUBLIC defaults (CONNECT, USAGE-on-public) deliberately do not appear, because revoking them from a single user is a silent no-op.

`access_map(name)` returns `{database: [privs]}` for the whole cluster in as few round trips as possible: one cluster query for db-level CONNECT/CREATE, one `pg_shdepend` query to find which databases hold object grants, then one per-database query only for those. This is what the DB-access table's Privileges column uses. It's fast (sub-second for dozens of DBs) precisely because it doesn't visit every database.

The endpoint `/api/user-db-privileges` returns both `held` (effective) and `explicit`. The grant modal offers `!held`, the revoke modal offers `explicit`, and when `explicit` is empty on a connect-only row it explains that access is via PUBLIC and points the user at the `×` (remove all) action, which does the lockdown.

## The user-detail UI (web)

Rendered in `filter.js`, `viewUserInfoFor` → `userPanelSql`. The **Database Access** table has columns DATABASE | Privileges | Access | Add/Remove:
- Privileges cell shows what `access_map` returns, loaded in one batch call (`loadAccessPrivs` → `/api/user-access-map`, cached per user in `_apCache`). "connect only" means the user only has the PUBLIC defaults, no real data access.
- Access cell shows "granted" or "via PUBLIC".
- Each non-owner row has three `.ibtn` actions: `+` (grant modal), `−` (revoke modal), `×` (remove all access on that DB, which triggers the connection lockdown for PUBLIC-only access).
- `openDbAccessModal(username, db, mode)` drives grant/revoke with a searchable multi-select checkbox list (`.msel-opt`). Grant uses `held`, revoke uses `explicit`.
- Toolbar: Reset password, Test access, Disable login, Revoke all access, Drop user.

`revoke_all` in `postgres.py` also does `ALTER ROLE ... NOLOGIN` + `CONNECTION LIMIT 0`, so "revoke all access" genuinely cuts the user off even when their reach was via PUBLIC. Without that, a PUBLIC-only user still reaches everything and the button looks broken.

Icons are inline SVG in `core.js` (`ICONS`). `.ibtn` is the 30px row-action button (variants: success/danger/warn/accent). `apiPost` has a 90s timeout and never throws, it returns `{error}`. `runAction` guards against double-submits. `showModal` hides the modal before running its action so the DOM stays available.

### Connection lifecycle

The web UI is a single-page app and the server is **stateless per request** (no session/cookie/token, creds ride in each request body, stored client-side in local/session storage). Refreshing the browser resets the client `connected` flag so you reconnect, but it does not leak connections: the real DB connections live in server-side pools keyed by target (`ConnectionPool(min_size=1, max_size=6, timeout=10, max_idle=300)`), bounded and idle-closed after 5 minutes, reused on reconnect. A long query you abort by refreshing may keep running server-side until it finishes or hits `statement_timeout`.

## Memory

Distilled Postgres facts and gotchas are also in the auto-memory files (`warden-pg-role-mgmt-truths`, `warden-pg-drop-nonsuperuser`, `warden-pg-grant-mixed-owners`, `warden-pg-console-native`, `warden-mariadb-account-locked`, `warden-ci-startup-failure`). When you learn a new load-bearing fact, add it there and mirror the essentials here.
