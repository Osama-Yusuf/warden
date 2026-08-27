"""Environment endpoints, roles/privileges, and .env / override loading."""

import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# .env loading. Searched in cwd, then upward to the git root, then ~/.warden.env.
# Existing environment variables are never overwritten.
# ---------------------------------------------------------------------------

def _env_candidates():
    seen = set()
    cur = Path.cwd()
    for _ in range(10):
        candidate = cur / ".env"
        if candidate not in seen:
            seen.add(candidate)
            yield candidate
        if (cur / ".git").exists() or cur.parent == cur:
            break
        cur = cur.parent
    yield Path.home() / ".warden.env"


def load_dotenv():
    for path in _env_candidates():
        if not path.is_file():
            continue
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            continue
        break  # first .env found wins; ~/.warden.env is the fallback


# ---------------------------------------------------------------------------
# Built-in environments
# ---------------------------------------------------------------------------

# No environments ship with warden, every deployment is different. Add yours:
#   - in the web UI: Settings -> Environments (browser-local or "save on server")
#   - as server profiles shared with the CLI: ~/.warden/profiles.sqlite
#   - as JSON: environments.local.json / ~/.warden_environments.json /
#     WARDEN_ENVIRONMENTS_FILE, shaped {env: {engine: {host, port, ...}}}
#   - or import a backup file from another warden instance
BUILTIN_ENVIRONMENTS = {}


def _override_candidates():
    explicit = os.environ.get("WARDEN_ENVIRONMENTS_FILE")
    if explicit:
        yield Path(explicit).expanduser()
    cur = Path.cwd()
    for _ in range(10):
        yield cur / "environments.local.json"
        if (cur / ".git").exists() or cur.parent == cur:
            break
        cur = cur.parent
    yield Path.home() / ".warden_environments.json"


# ---------------------------------------------------------------------------
# Server-side connection profiles (SQLite), shared across browsers and the
# CLI; passwords never go here (the web UI can use the macOS Keychain).
# ---------------------------------------------------------------------------

def profiles_db_path():
    return Path(os.environ.get("WARDEN_PROFILES_DB",
                               str(Path.home() / ".warden" / "profiles.sqlite")))


def _migrate_legacy_profiles():
    """One-time migration from the tool's earlier name."""
    if "WARDEN_PROFILES_DB" in os.environ:
        return
    new, old = profiles_db_path(), Path.home() / ".dbctl" / "profiles.sqlite"
    if not new.exists() and old.exists():
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old, new)


def _profiles_connect():
    _migrate_legacy_profiles()
    path = profiles_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS profiles (
        name TEXT NOT NULL,
        engine TEXT NOT NULL,
        host TEXT NOT NULL,
        port INTEGER NOT NULL,
        auth_db TEXT,
        default_db TEXT,
        master_user TEXT,
        tls INTEGER DEFAULT 0,
        updated_at TEXT,
        PRIMARY KEY (name, engine)
    )""")
    return con


def load_profile_environments():
    _migrate_legacy_profiles()
    if not profiles_db_path().is_file():
        return {}
    try:
        con = _profiles_connect()
        rows = con.execute("SELECT * FROM profiles").fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    envs = {}
    for r in rows:
        cfg = {"host": r["host"], "port": r["port"], "tls": bool(r["tls"])}
        if r["auth_db"]:
            cfg["auth_db"] = r["auth_db"]
        if r["default_db"]:
            cfg["default_db"] = r["default_db"]
        if r["master_user"]:
            cfg["master_user"] = r["master_user"]
        envs.setdefault(r["name"], {})[r["engine"]] = cfg
    return envs


def save_profile(name, engine, cfg):
    con = _profiles_connect()
    con.execute(
        """INSERT INTO profiles (name, engine, host, port, auth_db, default_db, master_user, tls, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(name, engine) DO UPDATE SET
             host=excluded.host, port=excluded.port, auth_db=excluded.auth_db,
             default_db=excluded.default_db, master_user=excluded.master_user,
             tls=excluded.tls, updated_at=excluded.updated_at""",
        (name, engine, cfg["host"], int(cfg["port"]), cfg.get("auth_db"),
         cfg.get("default_db"), cfg.get("master_user"), int(bool(cfg.get("tls"))),
         datetime.now().isoformat(timespec="seconds")))
    con.commit()
    con.close()


def delete_profile(name, engine):
    con = _profiles_connect()
    con.execute("DELETE FROM profiles WHERE name = ? AND engine = ?", (name, engine))
    con.commit()
    con.close()


def refresh_environments():
    """Recompute ENVIRONMENTS in place so every live reference sees updates."""
    ENVIRONMENTS.clear()
    ENVIRONMENTS.update(load_environments())
    return ENVIRONMENTS


def load_environments():
    """Built-ins, then SQLite profiles, then the first JSON override file.

    Override format mirrors BUILTIN_ENVIRONMENTS: {env: {engine: {host, port, ...}}}.
    Envs/engines are merged per-key so an override can add staging without
    repeating production.
    """
    merged = json.loads(json.dumps(BUILTIN_ENVIRONMENTS))  # deep copy
    for env_name, engines in load_profile_environments().items():
        merged.setdefault(env_name, {}).update(engines)
    for path in _override_candidates():
        if not path.is_file():
            continue
        try:
            overrides = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for env_name, engines in overrides.items():
            merged.setdefault(env_name, {})
            if isinstance(engines, dict):
                merged[env_name].update(engines)
        break
    return merged


DOCDB_ROLES = [
    "read", "readWrite", "dbAdmin", "dbOwner",
    "clusterAdmin", "clusterMonitor",
    "readAnyDatabase", "readWriteAnyDatabase", "dbAdminAnyDatabase",
    "root",
]

PG_PRIVILEGES = [
    "SELECT", "INSERT", "UPDATE", "DELETE",
    "ALL PRIVILEGES", "CONNECT", "CREATE", "USAGE",
]

def _augment_path():
    """Apps launched from Finder/Dock get a minimal PATH without Homebrew,
    so mongosh/psql appear 'missing'. Add the common tool locations."""
    candidates = [
        "/opt/homebrew/bin", "/opt/homebrew/sbin",
        "/usr/local/bin", "/usr/local/sbin",
        "/opt/homebrew/opt/libpq/bin", "/usr/local/opt/libpq/bin",
        str(Path.home() / ".local" / "bin"),
    ]
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for candidate in candidates:
        if candidate not in parts and Path(candidate).is_dir():
            parts.append(candidate)
    os.environ["PATH"] = os.pathsep.join(p for p in parts if p)


load_dotenv()
_augment_path()
ENVIRONMENTS = load_environments()
