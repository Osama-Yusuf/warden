# warden

Every database has a door. warden is the guy standing at it.

It holds the keys, decides who gets in and what they can touch, kicks people out, and writes everything down. It'll also read a query back to you in plain english before running it, so you know exactly what you're about to do to prod.

Works with DocumentDB/MongoDB, PostgreSQL (Aurora and friends), MySQL/MariaDB, and plain SQLite files. One core, three ways to use it: CLI, web UI, desktop app.

## What it does

- manage users: create, drop, reset passwords, grant and revoke with confirmation
- query console with a plain-english preview ("Updates ONE document in orders where _id = ...") and a danger badge before anything runs
- security audits: who's admin, who can write where, dead accounts. Exclude the known ones so only real issues show up
- cluster health: connections, slow queries, cache hit, replication lag
- read-only mode for when you just want to look at prod without fear
- every change lands in an append-only audit log

## Running it

You need [uv](https://docs.astral.sh/uv/), plus the clients for whatever you connect to: mongosh, psql, mysql, sqlite3.

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

## Config

Environments can live in a few places, whatever suits you:

1. server profiles in `~/.warden/profiles.sqlite`, managed from the UI with "save on server". Shared by the CLI, web and desktop on that machine
2. plain JSON shaped `{env: {engine: {host, port, ...}}}` in `environments.local.json`, `~/.warden_environments.json`, or wherever `WARDEN_ENVIRONMENTS_FILE` points
3. browser-local custom envs from the UI
4. backup import, optionally passphrase encrypted. Carries envs, saved credentials, audit results, all of it

Engine keys are matched loosely: anything with mongo/document in the name uses mongosh, mysql/maria uses the mysql client, sqlite is a file path, and everything else is treated as postgres-compatible. So `aurora-mysql` and `aurora` land on the right client, and extra engines work via `warden pg --engine-key aurora ...`.

SQLite is a bit special: no server, no credentials. Point an environment at a file path, or just upload a .db file from Settings > Environments and warden stores it under `~/.warden/sqlite/`. You get the query console, tables with sizes, and a health card (integrity check included). No users to manage, so those pages hide themselves.

Credentials are saved per env + engine, with optional macOS Keychain storage. Env vars are in `.env.example`. The audit trail sits at `~/.warden/audit.log`.

## Notes for hacking on it

- the web server reads index.html from disk on every request, so UI edits are just a browser refresh. `make dev` restarts on py changes too
- queries go through a warm mongosh session (PTY based). The first one pays the connection cost, the rest take milliseconds
- mongosh has no `--db` flag and silently ignores it. The target db goes in the connection URI path. Don't "fix" that
- the desktop app needs private_mode off and a fixed port, otherwise localStorage resets every launch
