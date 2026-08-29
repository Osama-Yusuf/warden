#!/usr/bin/env python3
"""
warden web server: browser UI for database user management.
Run:  warden-web  (or: python -m warden_web.server)
Open: http://localhost:8642
"""

import argparse
import queue
import shutil
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

import json
import os

import re

from warden_core import (
    AUDIT_LOG,
    DOCDB_ROLES,
    ENVIRONMENTS,
    PG_PRIVILEGES,
    audit,
    engine_family,
    my_csv,
    my_query,
    sq_csv,
    sq_query,
    delete_profile,
    load_profile_environments,
    refresh_environments,
    save_profile,
    docdb_args,
    docdb_eval,
    generate_password,
    js_string,
    pg_csv,
    pg_query,
    run_cmd,
    validate_ident,
)
from warden_core import mongo_native as mn
from warden_core import pg_native as pn
from warden_core import es_native as esn
from warden_core import redis_native as rdn
# The query console (api_query) still lives here, so it keeps the few validators
# it needs directly. Everything else moved into the per-engine adapters.
from warden_core.validation import MYSQL_PRIVILEGES, is_mariadb, validate_target
from warden_core.adapters import EngineError, Target, get_adapter
from warden_web import assistant

USE_NATIVE_MONGO = mn.available()
USE_NATIVE_PG = pn.available()
USE_NATIVE_ES = esn.available()
USE_NATIVE_REDIS = rdn.available()

DEFAULT_HOST = os.environ.get("WARDEN_WEB_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("WARDEN_WEB_PORT", "8642"))
MAX_BODY_SIZE = 65536


def static_dir():
    # PyInstaller bundles unpack to sys._MEIPASS
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "warden_web" / "static"
    return Path(__file__).parent / "static"


INDEX_HTML = static_dir() / "index.html"

# The only sub-asset types the UI serves (CSS + the split-out JS modules).
ASSET_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}


def require_fields(body, *fields):
    missing = [f for f in fields if not body.get(f)]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")


def require_creds(body):
    """Admin credentials, except where auth is absent or optional: SQLite has no
    accounts, and Elasticsearch/Redis may be unauthenticated or password-only, so
    whatever creds are supplied are passed straight to the driver."""
    if engine_family(body.get("engine", "")) in ("sqlite", "elasticsearch", "redis"):
        return
    require_fields(body, "admin_user", "admin_pass")


# ---------------------------------------------------------------------------
# API handlers
# ---------------------------------------------------------------------------

def api_config(body):
    envs = {}
    for env_name, env_cfg in ENVIRONMENTS.items():
        envs[env_name] = list(env_cfg.keys())
    return {
        "environments": envs,
        "environment_details": ENVIRONMENTS,
        "docdb_roles": DOCDB_ROLES,
        "pg_privileges": PG_PRIVILEGES,
        "tools": {
            "mongosh": shutil.which("mongosh") is not None,
            "psql": shutil.which("psql") is not None,
            "mysql": shutil.which("mysql") is not None,
            "sqlite3": shutil.which("sqlite3") is not None,
            "keychain": _keychain_available(),
        },
        "mysql_privileges": MYSQL_PRIVILEGES,
        "profile_envs": {name: sorted(engines.keys())
                         for name, engines in load_profile_environments().items()},
        "audits": AUDIT_DEFS,
        "defaults": {
            "env": os.environ.get("WARDEN_ENV", ""),
            "admin_user": os.environ.get("WARDEN_ADMIN_USER", ""),
        },
    }


HOST_RE = re.compile(r'^[a-zA-Z0-9_.\-]+$')


def sanitize_custom_config(raw):
    """Validate a browser-supplied environment config (custom envs live in
    the client's localStorage, so the server sees them per-request)."""
    if not isinstance(raw, dict):
        raise ValueError("custom_config must be an object")
    if raw.get("path"):
        p = str(raw["path"]).strip()
        if not p or len(p) > 500 or "\n" in p or "\x00" in p:
            raise ValueError("Invalid sqlite path")
        return {"path": str(Path(p).expanduser())}
    host = str(raw.get("host", "")).strip()
    if not host or len(host) > 255 or not HOST_RE.match(host):
        raise ValueError("Invalid custom host")
    try:
        port = int(raw.get("port", 0))
    except (TypeError, ValueError):
        raise ValueError("Invalid custom port")
    if not 1 <= port <= 65535:
        raise ValueError("Invalid custom port")
    cfg = {"host": host, "port": port, "tls": bool(raw.get("tls"))}
    if raw.get("tls_insecure"):
        cfg["tls_insecure"] = True   # opt out of certificate verification (self-signed / private CA)
    if raw.get("auth_db"):
        cfg["auth_db"] = validate_ident(str(raw["auth_db"]), "auth_db")
    if raw.get("default_db"):
        cfg["default_db"] = validate_ident(str(raw["default_db"]), "default_db")
    return cfg


def get_config(body):
    custom = body.get("custom_config")
    if custom is not None:
        try:
            return sanitize_custom_config(custom), None
        except ValueError as e:
            return None, str(e)
    env = body.get("env", "production")
    engine = body.get("engine", "documentdb")
    if env not in ENVIRONMENTS or engine not in ENVIRONMENTS[env]:
        return None, f"Unknown env/engine: {env}/{engine}"
    return ENVIRONMENTS[env][engine], None


# ---------------------------------------------------------------------------
# Adapter dispatch. Every engine-specific handler is the same three steps: build
# the adapter for this request, ask it to do the thing, wrap the answer. The
# adapter (one class per engine, over in warden_core) raises EngineError or
# ValueError when something's off, and we turn that into a plain {"error": ...}
# the browser already knows how to show. Write ops also hand back a Mutation, so
# we can write the audit line without the handler knowing a single engine detail.
# ---------------------------------------------------------------------------

def _adapter_for(body):
    """Resolve the engine adapter for this request, or return (None, error)."""
    cfg, err = get_config(body)
    if err:
        return None, err
    return get_adapter(body.get("engine", "documentdb"), cfg,
                       body.get("admin_user", ""), body.get("admin_pass", "")), None


def _target(body):
    """The browse/CRUD Target. `name` is a table (SQL) or a collection / index /
    key-namespace (everything else); each adapter validates the parts it uses."""
    return Target(database=body.get("database", ""),
                  name=body.get("table") or body.get("collection", ""),
                  schema=body.get("schema", "public"))


def _named_user(body):
    """Validate the target username for this engine (MySQL accounts may carry an
    @host), raising ValueError when it's missing or malformed."""
    require_fields(body, "username")
    return validate_target(body.get("engine", ""), body["username"])


def _mutate(body, method, *args, env_default="production", **kwargs):
    """Run one write op through its adapter and turn the Mutation into the reply:
    do the work, write the audit line, return {"ok": True, ...extra}."""
    adapter, err = _adapter_for(body)
    if err:
        return {"error": err}
    try:
        m = getattr(adapter, method)(*args, **kwargs)
    except (EngineError, ValueError) as e:
        return {"error": str(e)}
    detail = f"{m.detail} · via Ward" if body.get("_source") == "ward" else m.detail
    audit(body.get("env", env_default), adapter.family, m.action, detail)
    return {"ok": True, **m.response}


def api_connect(body):
    adapter, err = _adapter_for(body)
    if err:
        return {"ok": False, "error": err}
    try:
        whoami = adapter.ping()
    except (EngineError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    if adapter.family == "documentdb":
        # Reads and writes go native, but the query console still uses mongosh,
        # so warm its session now for a fast first query.
        prewarm_docdb(adapter.cfg, adapter.user, adapter.pwd)
    cfg = adapter.cfg
    host = str(Path(cfg["path"])) if "path" in cfg else cfg.get("host", "")
    return {"ok": True, "host": host, "user": whoami}


def api_test_login(body):
    """Connect as a given user (their own credentials) and probe what they can
    do. Stateless: never touches the admin session, read-only checks only. The
    adapter runs the probe and resolves auth-failure messages; we just wrap it."""
    adapter, err = _adapter_for(body)
    if err:
        return {"error": err}
    require_fields(body, "test_user", "test_pass")
    test_db = validate_ident(body["test_db"], "database") if body.get("test_db") else ""
    try:
        probe = adapter.login_probe(body["test_user"], body["test_pass"], test_db)
    except (EngineError, ValueError) as e:
        return {"error": str(e)}
    return {"ok": True, **probe}


def api_list_users(body):
    adapter, err = _adapter_for(body)
    if err:
        return {"error": err}
    if not adapter.has_users:
        return {"users": [], "note": "SQLite has no user accounts"}
    try:
        return {"users": adapter.list_users()}
    except (EngineError, ValueError) as e:
        return {"error": str(e)}


def api_user_info(body):
    adapter, err = _adapter_for(body)
    if err:
        return {"error": err}
    require_fields(body, "username")
    if not adapter.has_users:
        return {"error": "SQLite has no user accounts"}
    # Elasticsearch/Redis ACL names aren't SQL identifiers, so (like the original)
    # they skip validate_target; every other engine validates the username the
    # same way the rest of the user operations do.
    name = (str(body["username"]).strip() if adapter.family in ("elasticsearch", "redis")
            else validate_target(body.get("engine", ""), body["username"]))
    try:
        return adapter.user_info(name)
    except (EngineError, ValueError) as e:
        return {"error": str(e)}


def api_create_user(body):
    name = _named_user(body)
    password = body.get("password") or generate_password()
    return _mutate(body, "create_user", name, password,
                   can_login=body.get("can_login", True),
                   roles=body.get("roles"),
                   key_pattern=body.get("key_pattern"),
                   acl_level=body.get("acl_level", "read"))


def api_reset_password(body):
    name = _named_user(body)
    password = body.get("password") or generate_password()
    return _mutate(body, "set_password", name, password)


def api_grant(body):
    name = _named_user(body)
    return _mutate(body, "grant", name,
                   privilege=body.get("privilege"), database=body.get("database"),
                   schema=body.get("schema"), roles=body.get("roles"),
                   rule=body.get("rule"))


def api_revoke(body):
    name = _named_user(body)
    return _mutate(body, "revoke", name,
                   privilege=body.get("privilege"), database=body.get("database"),
                   schema=body.get("schema"), roles=body.get("roles"),
                   rule=body.get("rule"))


def api_drop_user(body):
    name = _named_user(body)
    return _mutate(body, "drop_user", name)


def api_toggle_login(body):
    name = _named_user(body)
    return _mutate(body, "toggle_login", name, body.get("enable", True))


def api_list_databases(body):
    adapter, err = _adapter_for(body)
    if err:
        return {"error": err}
    try:
        return {"databases": adapter.list_databases()}
    except (EngineError, ValueError) as e:
        return {"error": str(e)}


def api_list_collections(body):
    adapter, err = _adapter_for(body)
    if err:
        return {"error": err}
    try:
        # SQL engines answer under "tables", the document/key stores under
        # "collections"; that's exactly what collection_key tells us.
        return {adapter.collection_key: adapter.list_collections(body.get("database", ""))}
    except (EngineError, ValueError) as e:
        return {"error": str(e)}


BROWSE_MAX_LIMIT = 200
BROWSE_SEARCH_MAX = 200


def api_browse_data(body):
    """A page of rows from one collection/table for the data browser. Uniform
    shape across engines: {engine, columns, rows, ids?, total, estimated,
    filtered, offset, limit}. Optional `search` filters server-side (bounded by
    the page limit); it never runs a filtered count. The adapter fetches the
    page, we put the uniform wrapper around it."""
    adapter, err = _adapter_for(body)
    if err:
        return {"error": err}
    try:
        limit = max(1, min(BROWSE_MAX_LIMIT, int(body.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(body.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    search = str(body.get("search", "") or "").strip()[:BROWSE_SEARCH_MAX]
    try:
        res = adapter.browse(_target(body), limit, offset, search)
    except (EngineError, ValueError) as e:
        return {"error": str(e)}
    out = {"engine": adapter.family, "columns": res.get("columns", []),
           "rows": res.get("rows", []), "total": res.get("total"),
           "estimated": res.get("estimated", False),
           "filtered": res.get("filtered", bool(search)),
           "offset": offset, "limit": limit}
    if "ids" in res:
        out["ids"] = res["ids"]
    return out


def api_table_meta(body):
    """Metadata a safe editor needs: primary key + columns, plus whether the
    object is editable. Each adapter knows its own answer (Postgres/Mongo edit
    rows, Elasticsearch/Redis edit documents/keys, MySQL/SQLite are read-only)."""
    adapter, err = _adapter_for(body)
    if err:
        return {"error": err}
    try:
        return {"engine": adapter.family, **adapter.table_meta(_target(body))}
    except (EngineError, ValueError) as e:
        return {"error": str(e)}


def api_object_stats(body):
    """Header stats for the data browser (rows, size, columns, indexes), shaped
    per engine. Best-effort: returns whatever is cheap to compute."""
    adapter, err = _adapter_for(body)
    if err:
        return {"error": err}
    try:
        return {"engine": adapter.family, "stats": adapter.object_stats(_target(body))}
    except (EngineError, ValueError) as e:
        return {"error": str(e)}


def _crud_supported(fam):
    """Only the native engines may mutate rows."""
    if fam == "documentdb" and USE_NATIVE_MONGO:
        return None
    if fam == "postgresql" and USE_NATIVE_PG:
        return None
    if fam == "elasticsearch" and USE_NATIVE_ES:
        return None
    if fam == "redis" and USE_NATIVE_REDIS:
        return None
    return {"error": "Row editing isn't available for this engine"}


def api_row_insert(body):
    blocked = _crud_supported(engine_family(body.get("engine", "")))
    if blocked:
        return blocked
    return _mutate(body, "insert_row", _target(body), body, env_default="custom")


def api_row_update(body):
    blocked = _crud_supported(engine_family(body.get("engine", "")))
    if blocked:
        return blocked
    return _mutate(body, "update_row", _target(body), body, env_default="custom")


def api_row_delete(body):
    blocked = _crud_supported(engine_family(body.get("engine", "")))
    if blocked:
        return blocked
    return _mutate(body, "delete_row", _target(body), body, env_default="custom")


MAX_QUERY_LEN = 20000
MAX_OUTPUT_LEN = 300000

# Redis commands that only read, used to gate the console in read-only mode.
REDIS_READ_CMDS = {
    "GET", "MGET", "STRLEN", "GETRANGE", "SUBSTR", "GETBIT", "BITCOUNT",
    "EXISTS", "TYPE", "TTL", "PTTL", "EXPIRETIME", "PEXPIRETIME", "OBJECT",
    "KEYS", "SCAN", "HSCAN", "SSCAN", "ZSCAN", "RANDOMKEY", "DBSIZE",
    "HGET", "HMGET", "HGETALL", "HKEYS", "HVALS", "HLEN", "HEXISTS", "HSTRLEN",
    "LRANGE", "LLEN", "LINDEX", "LPOS",
    "SMEMBERS", "SCARD", "SISMEMBER", "SMISMEMBER", "SRANDMEMBER", "SINTER", "SUNION", "SDIFF",
    "ZRANGE", "ZREVRANGE", "ZRANGEBYSCORE", "ZRANGEBYLEX", "ZCARD", "ZSCORE",
    "ZRANK", "ZREVRANK", "ZCOUNT", "ZMSCORE",
    "XLEN", "XRANGE", "XREVRANGE", "XINFO",
    "INFO", "MEMORY", "PING", "ECHO", "TIME", "LOLWUT", "COMMAND",
}

CURSOR_CALL_RE = re.compile(r'\.(find|aggregate)\s*\(')
CURSOR_CONSUMED_RE = re.compile(r'\.(toArray|forEach|itcount|explain|next|size|map)\s*\(')


# ---------------------------------------------------------------------------
# Read-only mode is a seatbelt, not a security boundary. The client sends
# read_only: true and the server refuses anything that mutates.
# ---------------------------------------------------------------------------

RO_BLOCKED_ROUTES = {
    "/api/create-user", "/api/reset-password", "/api/grant",
    "/api/revoke", "/api/drop-user", "/api/toggle-login",
    "/api/row-insert", "/api/row-update", "/api/row-delete",
    "/api/ai/execute",   # Ward running a confirmed change is still a change
}

RO_DOCDB_BLOCK = re.compile(
    r'\.(insert\w*|update\w*|replace\w*|delete\w*|remove|drop\w*|rename\w*|create\w*|'
    r'bulkWrite|findOneAndUpdate|findOneAndReplace|findOneAndDelete|'
    r'grantRoles\w*|revokeRoles\w*)\s*\(|\$out\b|\$merge\b|dropDatabase',
    re.IGNORECASE)

RO_PG_ALLOWED = {"SELECT", "WITH", "SHOW", "VALUES", "TABLE"}


def read_only_violation(engine, query):
    """Returns a reason string when the query would write, else None."""
    q = query.strip()
    if engine.startswith("document"):
        m = RO_DOCDB_BLOCK.search(q)
        return f"blocked in read-only mode: {m.group(0)}" if m else None
    for stmt in q.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        if re.match(r'^explain\b', stmt, re.IGNORECASE):
            if re.match(r'^explain\s*(\([^)]*analyze|analyze)', stmt, re.IGNORECASE):
                return "blocked in read-only mode: EXPLAIN ANALYZE executes the statement"
            stmt = re.sub(r'^explain\s*(\([^)]*\))?\s*', '', stmt, flags=re.IGNORECASE)
        m = re.match(r'^[A-Za-z]+', stmt)
        kw = m.group(0).upper() if m else ""
        if kw not in RO_PG_ALLOWED:
            return f"blocked in read-only mode: {kw or stmt[:20]}"
    return None


def _js_balanced(q):
    """True when brackets and strings are closed. An unbalanced query would
    put the REPL into multi-line continuation mode and stall the session."""
    depth = 0
    in_str = None
    escaped = False
    for c in q:
        if in_str:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == in_str:
                in_str = None
            continue
        if c in "\"'`":
            in_str = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and in_str is None


def _clean_mongosh_noise(text):
    """Drop mongosh's node-module warnings (e.g. baseline-browser-mapping)."""
    return "\n".join(
        line for line in (text or "").splitlines()
        if "[baseline-browser-mapping]" not in line
    ).strip()


# ---------------------------------------------------------------------------
# Persistent mongosh sessions. A cold mongosh start (node boot + TLS/auth
# handshake) costs seconds per query; a warm REPL answers in milliseconds.
# ---------------------------------------------------------------------------

class SessionError(Exception):
    pass


class SessionTimeout(SessionError):
    pass


class MongoSession:
    """mongosh under a pseudo-terminal. Piped stdin puts mongosh in batch mode
    (it buffers everything until EOF), so a PTY is required for a live REPL.
    Markers are masked per call so echoed input can never fake a marker."""

    ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>]|\r')

    def __init__(self, cfg, admin_user, admin_pass):
        import pty
        import termios
        args = docdb_args(cfg, admin_user, admin_pass, cfg.get("auth_db", "admin"))
        master, slave = pty.openpty()
        try:
            attrs = termios.tcgetattr(slave)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
        except termios.error:
            pass
        env = os.environ.copy()
        env["TERM"] = "dumb"
        env["NO_COLOR"] = "1"
        self.proc = subprocess.Popen(args, stdin=slave, stdout=slave,
                                     stderr=slave, env=env, close_fds=True)
        os.close(slave)
        self.master = master
        self.lines = queue.Queue()
        self.lock = threading.Lock()
        self.last_used = time.time()
        threading.Thread(target=self._pump, daemon=True).start()
        # Round-trip once so a broken connection fails here, not mid-query.
        if self._roundtrip("1", timeout=15) is None:
            self.close()
            raise SessionError("mongosh session failed to start")

    def _pump(self):
        buf = b""
        try:
            while True:
                chunk = os.read(self.master, 4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self.lines.put(self.ANSI_RE.sub("", line.decode("utf-8", "replace")))
        except OSError:
            pass
        self.lines.put(None)

    def alive(self):
        return self.proc.poll() is None

    def close(self):
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            os.close(self.master)
        except OSError:
            pass

    def _drain(self):
        try:
            while True:
                self.lines.get_nowait()
        except queue.Empty:
            pass

    def _write(self, text):
        os.write(self.master, (text + "\n").encode())

    def _roundtrip(self, js_line, timeout):
        """Send one REPL line followed by a masked end marker; collect output
        lines until the marker appears. Returns None on timeout."""
        import uuid
        tag = uuid.uuid4().hex[:10]
        eoq = f"__WARDEN_EOQ_{tag}__"
        eoq_expr = f'"__WARDEN_" + "EOQ_{tag}" + "__"'
        self._drain()
        try:
            self._write(js_line)
            self._write(f'print({eoq_expr})')
        except OSError:
            raise SessionError("mongosh session closed")
        deadline = time.time() + timeout
        collected = []
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                line = self.lines.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:
                raise SessionError("mongosh session ended")
            if eoq in line:
                return collected
            collected.append(line)

    def run_structured(self, query, db, timeout=60):
        """Run a single-expression query, returning EJSON text between markers."""
        import uuid
        with self.lock:
            self.last_used = time.time()
            if not self.alive():
                raise SessionError("session dead")
            tag = uuid.uuid4().hex[:10]
            ok_m = f"__WARDEN_OK_{tag}__"
            err_m = f"__WARDEN_ERR_{tag}__"
            ok_expr = f'"__WARDEN_" + "OK_{tag}" + "__"'
            err_expr = f'"__WARDEN_" + "ERR_{tag}" + "__"'
            one_line = " ".join(query.splitlines()).strip()
            payload = (
                f'db = db.getSiblingDB({js_string(db)}); '
                f'try {{ print({ok_expr} + EJSON.stringify(( {one_line} ), null, 2, {{relaxed: true}})) }} '
                f'catch (e) {{ print({err_expr} + (e && e.message || String(e))) }}'
            )
            lines = self._roundtrip(payload, timeout)
            if lines is None:
                self.close()
                raise SessionTimeout(f"Query timed out after {timeout}s")
            for idx, line in enumerate(lines):
                if ok_m in line:
                    raw = "\n".join([line.split(ok_m, 1)[1]] + lines[idx + 1:])
                    return {"ok": True, "raw": self._trim_to_json(raw)}
                if err_m in line:
                    msg = "\n".join([line.split(err_m, 1)[1]] + lines[idx + 1:])
                    return {"ok": False, "error": msg.strip()}
            # No marker printed, so the payload itself failed to parse; whatever
            # the REPL emitted is the error text (minus echoed payload lines).
            msg = "\n".join(l for l in lines if l.strip() and "__WARDEN_" not in l).strip()
            return {"ok": False, "error": msg or "Query failed with no output"}

    @staticmethod
    def _trim_to_json(raw):
        """Drop trailing REPL noise lines (e.g. a stray prompt) after the JSON."""
        text = raw.rstrip()
        for _ in range(4):
            try:
                json.loads(text.strip())
                return text
            except (json.JSONDecodeError, ValueError):
                if "\n" not in text:
                    break
                text = text.rsplit("\n", 1)[0].rstrip()
        return raw


MONGO_SESSIONS = {}
MONGO_SESSIONS_LOCK = threading.Lock()
SESSION_IDLE_SECONDS = 600
SESSION_MAX = 4
SESSION_COOLDOWN_SECONDS = 300
_session_cooldown_until = 0.0


def get_mongo_session(cfg, admin_user, admin_pass):
    global _session_cooldown_until
    key = (cfg["host"], cfg["port"], cfg.get("auth_db", "admin"),
           bool(cfg.get("tls")), admin_user, admin_pass)
    with MONGO_SESSIONS_LOCK:
        now = time.time()
        for k in list(MONGO_SESSIONS):
            s = MONGO_SESSIONS[k]
            if not s.alive() or now - s.last_used > SESSION_IDLE_SECONDS:
                s.close()
                del MONGO_SESSIONS[k]
        sess = MONGO_SESSIONS.get(key)
        if sess and sess.alive():
            return sess
        if now < _session_cooldown_until:
            raise SessionError("session recently failed; using one-shot mode")
        if len(MONGO_SESSIONS) >= SESSION_MAX:
            oldest = min(MONGO_SESSIONS, key=lambda k: MONGO_SESSIONS[k].last_used)
            MONGO_SESSIONS[oldest].close()
            del MONGO_SESSIONS[oldest]
        try:
            sess = MongoSession(cfg, admin_user, admin_pass)
        except SessionError:
            _session_cooldown_until = time.time() + SESSION_COOLDOWN_SECONDS
            raise
        MONGO_SESSIONS[key] = sess
        return sess


def prewarm_docdb(cfg, user, pwd):
    """Open the warm session in the background so the first query-console run is
    instant. Reads/writes use pymongo; only the console still needs mongosh."""
    def _go():
        try:
            get_mongo_session(cfg, user, pwd)
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()


def api_query(body):
    cfg, err = get_config(body)
    if err:
        return {"error": err}
    require_creds(body)
    require_fields(body, "query")
    adm_user = body.get("admin_user", "")
    adm_pass = body.get("admin_pass", "")
    engine = body.get("engine", "documentdb")
    env = body.get("env", "custom")
    query = body["query"]
    if not isinstance(query, str) or not query.strip():
        return {"error": "Query is required"}
    query = query.strip()
    if len(query) > MAX_QUERY_LEN:
        return {"error": f"Query too long (max {MAX_QUERY_LEN} chars)"}
    database = body.get("database") or ""
    if database:
        database = validate_ident(database, "database")

    if body.get("read_only"):
        violation = read_only_violation(engine, query)
        if violation:
            return {"ok": False, "database": database, "data": None, "output": "",
                    "error": violation, "truncated": False, "mode": "read-only"}

    fam = engine_family(engine)

    if fam == "elasticsearch":
        # The console runs a Query DSL body via _search against the given index
        # (the console's "database" field). _search never mutates, so it's safe.
        index = database or "_all"
        try:
            dsl = json.loads(query) if query.strip().startswith("{") else {"query": {"query_string": {"query": query}}}
        except (json.JSONDecodeError, ValueError) as e:
            return {"ok": False, "database": index, "data": None, "output": "",
                    "error": f"Invalid JSON query: {e}", "truncated": False, "mode": "syntax"}
        audit(env, "elasticsearch", "SEARCH", f"{index} :: {query[:300]}")
        data, err = esn.raw_search(cfg, adm_user, adm_pass, index, dsl)
        if err:
            return {"ok": False, "database": index, "data": None, "output": "",
                    "error": err, "truncated": False, "mode": "native"}
        out = json.dumps(data, indent=2)[:MAX_OUTPUT_LEN]
        return {"ok": True, "database": index, "data": data, "output": out,
                "error": "", "truncated": len(out) >= MAX_OUTPUT_LEN, "mode": "native"}

    if fam == "redis":
        db = database or "db0"
        if body.get("read_only") and query.split()[0].upper() not in REDIS_READ_CMDS:
            return {"ok": False, "database": db, "data": None, "output": "",
                    "error": f"Read-only mode: '{query.split()[0]}' can modify data and is blocked.",
                    "truncated": False, "mode": "read-only"}
        audit(env, "redis", "COMMAND", f"{db} :: {query[:300]}")
        data, err = rdn.run_command(cfg, adm_user, adm_pass, db, query)
        if err:
            return {"ok": False, "database": db, "data": None, "output": "",
                    "error": err, "truncated": False, "mode": "native"}
        out = data if isinstance(data, str) else json.dumps(data, indent=2, default=str)
        out = (out or "")[:MAX_OUTPUT_LEN]
        return {"ok": True, "database": db, "data": data, "output": out,
                "error": "", "truncated": False, "mode": "native"}

    if engine.startswith("document"):
        db = database or "admin"
        if not _js_balanced(query):
            return {"ok": False, "database": db, "data": None, "output": "",
                    "error": "Query has unbalanced brackets or an unclosed string. Check the syntax.",
                    "truncated": False, "mode": "syntax"}
        q_stripped = query.rstrip().rstrip(";")
        single_expr = ";" not in q_stripped and "//" not in q_stripped
        q_exec = q_stripped
        # A bare cursor (find/aggregate) can't be EJSON-serialized; iterate it.
        if (CURSOR_CALL_RE.search(q_exec) and single_expr
                and not CURSOR_CONSUMED_RE.search(q_exec)):
            q_exec = q_exec + ".toArray()"
        audit(env, "documentdb", "QUERY", f"db={db} :: {query[:300]}")

        # Fast path: warm persistent session (skips node boot + TLS/auth).
        if single_expr:
            try:
                sess = get_mongo_session(cfg, adm_user, adm_pass)
                r = sess.run_structured(q_exec, db, timeout=60)
                if r["ok"] and r.get("raw") is not None:
                    raw = r["raw"]
                    truncated = len(raw) > MAX_OUTPUT_LEN
                    raw = raw[:MAX_OUTPUT_LEN]
                    try:
                        data = json.loads(raw.strip())
                    except (json.JSONDecodeError, ValueError):
                        data = None
                    return {"ok": True, "database": db, "data": data, "output": raw,
                            "error": "", "truncated": truncated, "mode": "warm"}
                if r["ok"]:
                    out = (r.get("output") or "")[:MAX_OUTPUT_LEN]
                    return {"ok": True, "database": db, "data": None, "output": out,
                            "error": "", "truncated": False, "mode": "warm"}
                return {"ok": False, "database": db, "data": None, "output": "",
                        "error": r["error"], "truncated": False, "mode": "warm"}
            except SessionTimeout as e:
                return {"ok": False, "database": db, "data": None, "output": "",
                        "error": str(e), "truncated": False, "mode": "warm"}
            except SessionError:
                pass  # fall through to a one-shot mongosh run

        # One-shot fallback (multi-statement queries or broken session).
        args = docdb_args(cfg, adm_user, adm_pass, db) + ["--json=relaxed", "--eval", q_exec]
        code, out, err_out = run_cmd(args, timeout=60)
        if code != 0 and "Converting circular structure" in (out + err_out):
            args = docdb_args(cfg, adm_user, adm_pass, db) + ["--eval", query]
            code, out, err_out = run_cmd(args, timeout=60)
        err_out = _clean_mongosh_noise(err_out)
        truncated = len(out) > MAX_OUTPUT_LEN
        out = out[:MAX_OUTPUT_LEN]
        data = None
        error = ""
        if code == 0:
            try:
                data = json.loads(out.strip())
            except (json.JSONDecodeError, ValueError):
                data = None
        else:
            # In --json mode mongosh prints the error as JSON on stdout.
            try:
                error = json.loads(out.strip()).get("message", "")
            except (json.JSONDecodeError, ValueError, AttributeError):
                error = ""
            error = error or err_out or f"Exit code {code}"
        return {
            "ok": code == 0,
            "database": db,
            "data": data,
            "output": out,
            "error": error,
            "truncated": truncated,
            "mode": "cold",
        }
    else:
        fam = engine_family(engine)
        if fam == "mysql":
            db = database or cfg.get("default_db") or ""
            code, out, err_out = my_csv(cfg, adm_user, adm_pass, query, db=db or None, timeout=60)
            audit(env, "mysql", "QUERY", f"db={db or '*'} :: {query[:300]}")
            truncated = len(out) > MAX_OUTPUT_LEN
            return {"ok": code == 0, "database": db or "*", "csv": out[:MAX_OUTPUT_LEN],
                    "output": out[:MAX_OUTPUT_LEN],
                    "error": err_out if code != 0 else "",
                    "notices": err_out if code == 0 else "", "truncated": truncated}
        if fam == "sqlite":
            db_path = Path(cfg["path"])
            code, out, err_out = sq_csv(db_path, query, timeout=60)
            audit(env, "sqlite", "QUERY", f"db={db_path.name} :: {query[:300]}")
            truncated = len(out) > MAX_OUTPUT_LEN
            return {"ok": code == 0, "database": db_path.name, "csv": out[:MAX_OUTPUT_LEN],
                    "output": out[:MAX_OUTPUT_LEN],
                    "error": err_out if code != 0 else "",
                    "notices": err_out if code == 0 else "", "truncated": truncated}
        db = database or cfg.get("default_db", "postgres")
        code, out, err_out = pg_csv(cfg, adm_user, adm_pass, query, db=db, timeout=60)
        audit(env, "postgresql", "QUERY", f"db={db} :: {query[:300]}")
        truncated = len(out) > MAX_OUTPUT_LEN
        return {
            "ok": code == 0,
            "database": db,
            "csv": out[:MAX_OUTPUT_LEN],
            "output": out[:MAX_OUTPUT_LEN],
            "error": err_out if code != 0 else "",
            "notices": err_out if code == 0 else "",
            "truncated": truncated,
        }


# ---------------------------------------------------------------------------
# Security audits: predefined access-review checks with structured findings
# ---------------------------------------------------------------------------

AUDIT_DEFS = [
    {"id": "admin-access", "engine": "documentdb", "severity": "critical",
     "title": "Full admin access",
     "description": "Accounts that control the whole cluster. They can manage users, permissions and every database."},
    {"id": "anydb-write", "engine": "documentdb", "severity": "critical",
     "title": "Can change every database",
     "description": "Accounts able to modify data in all databases at once (readWriteAnyDatabase)."},
    {"id": "anydb-read", "engine": "documentdb", "severity": "warning",
     "title": "Can read every database",
     "description": "Accounts able to read all databases at once (readAnyDatabase)."},
    {"id": "write-access", "engine": "documentdb", "severity": "info",
     "title": "Who can change data",
     "description": "Which accounts can modify data, and in which databases."},
    {"id": "no-roles", "engine": "documentdb", "severity": "info",
     "title": "Accounts with no access",
     "description": "Accounts that can't do anything. Usually leftovers worth removing."},
    {"id": "mysql-global-admin", "engine": "mysql", "severity": "critical",
     "title": "Full admin access",
     "description": "Accounts holding SUPER, GRANT OPTION or CREATE USER globally. They control the whole server."},
    {"id": "mysql-global-write", "engine": "mysql", "severity": "critical",
     "title": "Can change every database",
     "description": "Accounts with global insert, update, delete or drop rights across all databases."},
    {"id": "mysql-write-access", "engine": "mysql", "severity": "info",
     "title": "Who can change data",
     "description": "Which accounts can add, edit or delete data, and in which databases."},
    {"id": "mysql-locked-privileged", "engine": "mysql", "severity": "info",
     "title": "Locked accounts that still have access",
     "description": "Accounts that are locked but still hold permissions. Worth cleaning up."},
    {"id": "superusers", "engine": "postgresql", "severity": "critical",
     "title": "Superusers",
     "description": "Accounts with total control. Every permission check is skipped for them."},
    {"id": "role-managers", "engine": "postgresql", "severity": "warning",
     "title": "Can create users or databases",
     "description": "Accounts able to create new users or databases, a quiet path to more access."},
    {"id": "pg-write-access", "engine": "postgresql", "severity": "info",
     "title": "Who can change data",
     "description": "Which accounts can add, edit or delete data, and where. Checked across every database."},
    {"id": "disabled-privileged", "engine": "postgresql", "severity": "info",
     "title": "Disabled accounts that still have access",
     "description": "Accounts that can no longer log in but still hold permissions. Worth cleaning up."},
]

DOCDB_ADMIN_ROLES = {"root", "userAdminAnyDatabase", "dbAdminAnyDatabase", "clusterAdmin"}
DOCDB_WRITE_ROLES = {"readWrite", "dbOwner", "dbAdmin"}


def api_audits(body):
    return {"audits": AUDIT_DEFS}


def _plural(n, word, plural_form=None):
    return f"{n} {word if n == 1 else (plural_form or word + 's')}"


def _docdb_users(cfg, user, pwd):
    if USE_NATIVE_MONGO:
        return mn.users_info(cfg, user, pwd)
    data, err = docdb_eval(cfg, user, pwd, "db.adminCommand({usersInfo:1}).users")
    if err:
        return None, err
    return (data or []), None


def _pg_grant_sweep(cfg, user, pwd):
    """INSERT/UPDATE/DELETE table grants per grantee, across all databases."""
    code, out, err = pg_query(cfg, user, pwd,
        "SELECT datname FROM pg_database WHERE datistemplate=false AND datname NOT IN ('rdsadmin')")
    if code != 0:
        return None, err
    grants = {}
    for db in [l.strip() for l in out.strip().split("\n") if l.strip()]:
        sql = """
            SELECT grantee, count(DISTINCT table_schema||'.'||table_name),
                   string_agg(DISTINCT privilege_type, ',')
            FROM information_schema.role_table_grants
            WHERE privilege_type IN ('INSERT','UPDATE','DELETE')
              AND grantee NOT IN ('PUBLIC')
            GROUP BY grantee
        """
        code, out2, _ = pg_query(cfg, user, pwd, sql, db=db)
        if code != 0:
            continue
        for line in out2.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) >= 3:
                g = grants.setdefault(parts[0], {"dbs": {}, "privs": set()})
                g["dbs"][db] = int(parts[1])
                g["privs"].update(parts[2].split(","))
    return grants, None


def api_audit_run(body):
    cfg, err = get_config(body)
    if err:
        return {"error": err}
    require_creds(body)
    require_fields(body, "audit")
    adm_user = body.get("admin_user", "")
    adm_pass = body.get("admin_pass", "")
    engine = body.get("engine", "documentdb")
    audit_id = body["audit"]
    definition = next((a for a in AUDIT_DEFS if a["id"] == audit_id), None)
    if not definition:
        return {"error": f"Unknown audit: {audit_id}"}
    if engine_family(engine) == "sqlite":
        return {"error": "SQLite has no user accounts, so there is nothing to audit"}
    findings = []
    scanned = 0

    fam = engine_family(engine)
    if fam in ("elasticsearch", "redis"):
        return {"error": "This isn't available for Elasticsearch or Redis yet. Coming in the next pass."}
    if fam == "mysql":
        excl = ("'mysql.sys','mysql.session','mysql.infoschema','mariadb.sys',"
                "'rdsadmin','rdsrepladmin'")
        if is_mariadb(cfg, adm_user, adm_pass):
            sql = ("SELECT u.user, u.host, IF(JSON_VALUE(g.Priv,'$.account_locked')=1,'Y','N'), "
                   "u.Super_priv, u.Grant_priv, u.Create_user_priv, u.Insert_priv, u.Update_priv, "
                   "u.Delete_priv, u.Drop_priv "
                   "FROM mysql.user u JOIN mysql.global_priv g ON u.user=g.User AND u.host=g.Host "
                   f"WHERE u.user NOT IN ({excl})")
        else:
            sql = ("SELECT user, host, account_locked, Super_priv, Grant_priv, Create_user_priv, "
                   "Insert_priv, Update_priv, Delete_priv, Drop_priv FROM mysql.user "
                   f"WHERE user NOT IN ({excl})")
        code, out, err = my_query(cfg, adm_user, adm_pass, sql)
        if code != 0:
            return {"error": err}
        accounts = []
        for line in out.strip().split("\n"):
            p = line.split("\t")
            if len(p) >= 10:
                accounts.append({"acct": f"{p[0]}@{p[1]}", "locked": p[2] == "Y",
                                 "super": p[3] == "Y", "grant": p[4] == "Y", "createuser": p[5] == "Y",
                                 "gwrite": any(v == "Y" for v in p[6:10])})
        scanned = len(accounts)
        if audit_id == "mysql-global-admin":
            for a in accounts:
                whats = [w for w, on in (("SUPER", a["super"]), ("GRANT OPTION", a["grant"]),
                                         ("CREATE USER", a["createuser"])) if on]
                if whats:
                    findings.append({"user": a["acct"], "severity": "critical",
                                     "summary": "has server-wide admin power",
                                     "detail": "via " + ", ".join(whats)})
        elif audit_id == "mysql-global-write":
            for a in accounts:
                if a["gwrite"]:
                    findings.append({"user": a["acct"], "severity": "critical",
                                     "summary": "can change data in every database",
                                     "detail": "global insert/update/delete/drop privileges"})
        elif audit_id in ("mysql-write-access", "mysql-locked-privileged"):
            sql = ("SELECT grantee, table_schema, privilege_type FROM information_schema.schema_privileges "
                   "WHERE privilege_type IN ('INSERT','UPDATE','DELETE','DROP') "
                   "UNION ALL "
                   "SELECT grantee, table_schema, privilege_type FROM information_schema.table_privileges "
                   "WHERE privilege_type IN ('INSERT','UPDATE','DELETE','DROP')")
            code, out, err = my_query(cfg, adm_user, adm_pass, sql)
            if code != 0:
                return {"error": err}
            grants = {}
            for line in out.strip().split("\n"):
                p = line.split("\t")
                if len(p) >= 3:
                    acct = p[0].replace("'", "").replace("`", "")
                    g = grants.setdefault(acct, {"dbs": set(), "privs": set()})
                    g["dbs"].add(p[1])
                    g["privs"].add(p[2])
            locked_map = {a["acct"]: a["locked"] for a in accounts}
            verb_map = {"INSERT": "add", "UPDATE": "edit", "DELETE": "delete", "DROP": "drop"}
            for acct, g in sorted(grants.items()):
                verbs = [verb_map[p] for p in ("INSERT", "UPDATE", "DELETE", "DROP") if p in g["privs"]]
                doing = ", ".join(verbs[:-1]) + " and " + verbs[-1] if len(verbs) > 1 else verbs[0]
                where = _plural(len(g["dbs"]), "database")
                detail = ", ".join(sorted(g["privs"])) + " on " + ", ".join(sorted(g["dbs"]))
                if audit_id == "mysql-write-access":
                    sev = "warning" if len(g["dbs"]) > 2 else "info"
                    findings.append({"user": acct, "severity": sev,
                                     "summary": f"can {doing} data in {where}", "detail": detail})
                elif audit_id == "mysql-locked-privileged" and locked_map.get(acct):
                    findings.append({"user": acct, "severity": "info",
                                     "summary": f"locked, but still holds access to {where}",
                                     "detail": detail})
    elif fam == "documentdb":
        users, err = _docdb_users(cfg, adm_user, adm_pass)
        if err:
            return {"error": err}
        scanned = len(users)
        for u in users:
            name = u.get("user", "?")
            roles = u.get("roles", [])
            role_strs = [f"{r['role']}@{r.get('db', '*')}" for r in roles]
            if audit_id == "admin-access":
                hits = [s for r, s in zip(roles, role_strs)
                        if r["role"] in DOCDB_ADMIN_ROLES
                        or (r["role"] == "dbOwner" and r.get("db") == "admin")]
                if hits:
                    findings.append({"user": name, "severity": "critical",
                                     "summary": "has full admin control",
                                     "detail": "via " + ", ".join(hits)})
            elif audit_id == "anydb-write":
                if any(r["role"] == "readWriteAnyDatabase" for r in roles):
                    findings.append({"user": name, "severity": "critical",
                                     "summary": "can change data in every database",
                                     "detail": "via readWriteAnyDatabase"})
            elif audit_id == "anydb-read":
                if any(r["role"] == "readAnyDatabase" for r in roles):
                    findings.append({"user": name, "severity": "warning",
                                     "summary": "can read every database",
                                     "detail": "via readAnyDatabase"})
            elif audit_id == "write-access":
                dbs = sorted({r.get("db", "*") for r in roles if r["role"] in DOCDB_WRITE_ROLES})
                if dbs:
                    sev = "warning" if len(dbs) > 3 or "*" in dbs else "info"
                    findings.append({"user": name, "severity": sev,
                                     "summary": f"can change data in {_plural(len(dbs), 'database')}",
                                     "detail": ", ".join(dbs)})
            elif audit_id == "no-roles":
                if not roles:
                    findings.append({"user": name, "severity": "info",
                                     "summary": "can't do anything",
                                     "detail": "the account exists but has no permissions, probably a leftover"})
    else:
        sql = """
            SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin
            FROM pg_roles
            WHERE rolname NOT LIKE 'pg\\_%'
              AND rolname NOT IN ('rdsadmin','rds_superuser','rds_replication','rds_password','rdsrepladmin')
        """
        code, out, err = pg_query(cfg, adm_user, adm_pass, sql)
        if code != 0:
            return {"error": err}
        roles = []
        for line in out.strip().split("\n"):
            p = line.split("\t")
            if len(p) >= 5:
                roles.append({"name": p[0], "super": p[1] == "t", "createrole": p[2] == "t",
                              "createdb": p[3] == "t", "login": p[4] == "t"})
        scanned = len(roles)
        if audit_id == "superusers":
            for r in roles:
                if r["super"]:
                    findings.append({"user": r["name"], "severity": "critical",
                                     "summary": "has total control (superuser)",
                                     "detail": "every permission check is skipped for this account"
                                               + ("" if r["login"] else " (login is disabled)")})
        elif audit_id == "role-managers":
            for r in roles:
                if (r["createrole"] or r["createdb"]) and not r["super"]:
                    what = ("new users and databases" if r["createrole"] and r["createdb"]
                            else "new users" if r["createrole"] else "new databases")
                    flags = [f for f, on in (("CREATEROLE", r["createrole"]), ("CREATEDB", r["createdb"])) if on]
                    findings.append({"user": r["name"], "severity": "warning",
                                     "summary": f"can create {what}",
                                     "detail": f"could use this to gain more access ({', '.join(flags)})"})
        elif audit_id in ("pg-write-access", "disabled-privileged"):
            grants, err = _pg_grant_sweep(cfg, adm_user, adm_pass)
            if err:
                return {"error": err}
            login_map = {r["name"]: r["login"] for r in roles}
            verb_map = {"INSERT": "add", "UPDATE": "edit", "DELETE": "delete"}
            for grantee, g in sorted(grants.items(), key=lambda kv: -sum(kv[1]["dbs"].values())):
                total = sum(g["dbs"].values())
                privs = ", ".join(p for p in ("INSERT", "UPDATE", "DELETE") if p in g["privs"])
                detail = (privs + " · "
                          + ", ".join(f"{db} ({_plural(n, 'table')})" for db, n in sorted(g["dbs"].items())))
                where = (f"{_plural(total, 'table')} across {_plural(len(g['dbs']), 'database')}"
                         if len(g["dbs"]) > 1 else f"{_plural(total, 'table')} in one database")
                if audit_id == "pg-write-access":
                    verbs = [verb_map[p] for p in ("INSERT", "UPDATE", "DELETE") if p in g["privs"]]
                    doing = ", ".join(verbs[:-1]) + " and " + verbs[-1] if len(verbs) > 1 else verbs[0]
                    sev = "warning" if len(g["dbs"]) > 2 else "info"
                    findings.append({"user": grantee, "severity": sev,
                                     "summary": f"can {doing} data in {where}",
                                     "detail": detail})
                elif audit_id == "disabled-privileged" and login_map.get(grantee) is False:
                    findings.append({"user": grantee, "severity": "info",
                                     "summary": f"login disabled, but can still change {where}",
                                     "detail": detail})

    order = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 3), f["user"]))
    return {"audit": audit_id, "engine": engine, "findings": findings,
            "scanned": scanned, "ran_at": time.strftime("%Y-%m-%d %H:%M:%S")}


# ---------------------------------------------------------------------------
# Cluster health
# ---------------------------------------------------------------------------

def api_health(body):
    adapter, err = _adapter_for(body)
    if err:
        return {"error": err}
    try:
        return {"engine": adapter.family, "health": adapter.health()}
    except (EngineError, ValueError) as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Server-side profiles + macOS Keychain
# ---------------------------------------------------------------------------

def api_profile_save(body):
    require_fields(body, "name", "profile_engine", "profile")
    name = validate_ident(body["name"], "profile name")
    engine = validate_ident(body["profile_engine"], "engine")
    cfg = sanitize_custom_config(body["profile"])
    if body["profile"].get("master_user"):
        cfg["master_user"] = validate_ident(str(body["profile"]["master_user"]), "master_user")
    save_profile(name, engine, cfg)
    refresh_environments()
    audit("profiles", engine, "PROFILE SAVE", f"{name} -> {cfg.get('host') or cfg.get('path')}")
    return {"ok": True}


def api_profile_delete(body):
    require_fields(body, "name", "profile_engine")
    name = validate_ident(body["name"], "profile name")
    engine = validate_ident(body["profile_engine"], "engine")
    delete_profile(name, engine)
    refresh_environments()
    audit("profiles", engine, "PROFILE DELETE", name)
    return {"ok": True}


def api_wipe(body):
    """Delete server-side warden data for a clean slate. Browser storage is
    cleared client-side; this removes what lives on the host."""
    import shutil as _sh
    base = Path.home() / ".warden"
    removed = []
    # Connection profiles shared across clients + the CLI
    prof = base / "profiles.sqlite"
    if prof.exists():
        prof.unlink()
        removed.append("connections")
    # Uploaded SQLite files
    up = base / "sqlite"
    if up.is_dir():
        _sh.rmtree(up, ignore_errors=True)
        removed.append("uploaded databases")
    # Audit trail, only if explicitly asked
    if body.get("wipe_audit"):
        for name in ("audit.log", ".dbctl_audit.log"):
            f = base / name if name == "audit.log" else Path.home() / name
            if f.exists():
                f.unlink()
                removed.append("activity log")
    refresh_environments()
    return {"ok": True, "removed": removed}


KEYCHAIN_SERVICE = "warden"
LEGACY_KEYCHAIN_SERVICE = "dbctl"  # pre-rename entries


def _keychain_available():
    return sys.platform == "darwin" and shutil.which("security") is not None


def _keychain_account(body):
    host = str(body.get("host", "")).strip()
    user = str(body.get("user", "")).strip()
    if not host or not user or not HOST_RE.match(host):
        raise ValueError("host and user are required")
    return f"{user}@{host}"


def api_keychain_save(body):
    if not _keychain_available():
        return {"error": "Keychain is only available on macOS"}
    account = _keychain_account(body)
    password = body.get("password", "")
    if not password:
        return {"error": "password is required"}
    code, out, err = run_cmd(["security", "add-generic-password", "-a", account,
                              "-s", KEYCHAIN_SERVICE, "-w", password, "-U"], timeout=10)
    if code != 0:
        return {"error": err or "Keychain write failed"}
    return {"ok": True, "account": account}


def api_keychain_get(body):
    if not _keychain_available():
        return {"error": "Keychain is only available on macOS"}
    account = _keychain_account(body)
    code, out, err = run_cmd(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                              "-a", account, "-w"], timeout=10)
    if code != 0:
        code, out, err = run_cmd(["security", "find-generic-password", "-s", LEGACY_KEYCHAIN_SERVICE,
                                  "-a", account, "-w"], timeout=10)
    if code != 0:
        return {"ok": False}
    return {"ok": True, "password": out.strip()}


def api_keychain_delete(body):
    if not _keychain_available():
        return {"error": "Keychain is only available on macOS"}
    account = _keychain_account(body)
    run_cmd(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", account], timeout=10)
    return {"ok": True}


def api_audit_log(body):
    if not AUDIT_LOG.exists():
        return {"entries": []}
    lines = AUDIT_LOG.read_text().strip().split("\n")
    lines.reverse()
    return {"entries": lines[:100]}


# ---------------------------------------------------------------------------
# AI assistant (off by default; none of this runs unless the user turns it on
# and supplies a key). The heavy lifting lives in assistant.py; these two are
# thin wrappers so the assistant can reuse the same read-only handlers and the
# same route table the rest of the app uses.
# ---------------------------------------------------------------------------

def api_ai_models(body):
    return assistant.list_models(body)


def api_ai_download(body):
    return assistant.download_model(body)


def api_ai_check(body):
    return assistant.check_machine(body)


def api_ai_execute(body):
    return assistant.run_operations(body, ROUTES)


def api_ai_chat(body):
    fam = engine_family(body.get("engine", ""))
    body["_audits"] = [{"id": a["id"], "title": a["title"]}
                       for a in AUDIT_DEFS if a["engine"] == fam]
    return assistant.chat_turn(body, ROUTES)


REQUIRES_CREDS = {
    "/api/connect", "/api/list-users", "/api/user-info",
    "/api/create-user", "/api/reset-password", "/api/grant",
    "/api/revoke", "/api/drop-user", "/api/toggle-login",
    "/api/list-databases", "/api/list-collections", "/api/browse-data",
    "/api/table-meta", "/api/object-stats", "/api/row-insert", "/api/row-update", "/api/row-delete",
    "/api/query", "/api/query-stream",
    "/api/audit-run", "/api/health",
    "/api/ai/execute",
}

ROUTES = {
    "/api/config": api_config,
    "/api/connect": api_connect,
    "/api/test-login": api_test_login,
    "/api/list-users": api_list_users,
    "/api/user-info": api_user_info,
    "/api/create-user": api_create_user,
    "/api/reset-password": api_reset_password,
    "/api/grant": api_grant,
    "/api/revoke": api_revoke,
    "/api/drop-user": api_drop_user,
    "/api/toggle-login": api_toggle_login,
    "/api/list-databases": api_list_databases,
    "/api/list-collections": api_list_collections,
    "/api/browse-data": api_browse_data,
    "/api/table-meta": api_table_meta,
    "/api/object-stats": api_object_stats,
    "/api/row-insert": api_row_insert,
    "/api/row-update": api_row_update,
    "/api/row-delete": api_row_delete,
    "/api/query": api_query,
    "/api/audit-run": api_audit_run,
    "/api/health": api_health,
    "/api/profile-save": api_profile_save,
    "/api/profile-delete": api_profile_delete,
    "/api/wipe": api_wipe,
    "/api/keychain-save": api_keychain_save,
    "/api/keychain-get": api_keychain_get,
    "/api/keychain-delete": api_keychain_delete,
    "/api/audit-log": api_audit_log,
    "/api/ai/models": api_ai_models,
    "/api/ai/download": api_ai_download,
    "/api/ai/check": api_ai_check,
    "/api/ai/chat": api_ai_chat,
    "/api/ai/execute": api_ai_execute,
}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        ts = self.log_date_time_string()
        sys.stderr.write(f"[{ts}] {fmt % args}\n")

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _same_site(self):
        """Reject cross-origin API calls (CSRF) and DNS-rebound requests: the
        Host must be one we actually serve, and any Origin must match. When the
        operator deliberately binds a wildcard address we can't allowlist the
        hostname, so we don't enforce (they've opted into network exposure)."""
        bind = getattr(self.server, "warden_bind_host", "127.0.0.1")
        port = getattr(self.server, "warden_port", None)
        if bind in ("0.0.0.0", "::", ""):
            return True
        allowed = {"127.0.0.1", "localhost", "::1", bind}
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host and host not in allowed:
            return False
        origin = self.headers.get("Origin")
        if origin:
            try:
                o = urlparse(origin)
                if o.hostname not in allowed or (port is not None and o.port != port):
                    return False
            except ValueError:
                return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            if not INDEX_HTML.exists():
                self.send_error(404, "index.html not found")
                return
            content = INDEX_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif path.startswith("/css/") or path.startswith("/js/"):
            self._serve_asset(path)
        elif path == "/api/config":
            if not self._same_site():
                self._json_response({"error": "Cross-origin request refused"}, 403)
                return
            self._json_response(api_config({}))
        else:
            self.send_error(404)

    def _serve_asset(self, path):
        """Serve a split-out CSS/JS file from the static dir. Traversal-safe: the
        resolved target has to stay inside static_dir(), and we only hand back the
        asset types we actually ship."""
        base = static_dir().resolve()
        target = (base / path.lstrip("/")).resolve()
        if target != base and base not in target.parents:
            self.send_error(403)
            return
        ctype = ASSET_TYPES.get(target.suffix.lower())
        if ctype is None or not target.is_file():
            self.send_error(404)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._same_site():
            self._json_response({"error": "Cross-origin request refused"}, 403)
            return
        if path == "/api/sqlite-upload":
            try:
                self._sqlite_upload()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        handler = ROUTES.get(path)
        if not handler and path != "/api/query-stream":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BODY_SIZE:
                self._json_response({"error": "Request too large"}, 413)
                return
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self._json_response({"error": "Invalid JSON"}, 400)
            return
        # SQLite has no accounts; Elasticsearch/Redis may be unauthenticated or
        # password-only, so their drivers handle whatever creds are supplied.
        if path in REQUIRES_CREDS and engine_family(body.get("engine", "")) not in ("sqlite", "elasticsearch", "redis"):
            if not body.get("admin_user") or not body.get("admin_pass"):
                self._json_response({"error": "Admin credentials required"}, 401)
                return
        if body.get("read_only") and path in RO_BLOCKED_ROUTES:
            self._json_response({"error": "Read-only mode is on. Turn it off in the top bar to make changes."}, 403)
            return
        if path == "/api/query-stream":
            try:
                self._query_stream(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        try:
            result = handler(body)
            self._json_response(result)
        except ValueError as e:
            self._json_response({"error": str(e)}, 400)
        except KeyError as e:
            self._json_response({"error": f"Missing field: {e}"}, 400)
        except Exception:
            import traceback
            sys.stderr.write(traceback.format_exc())
            self._json_response({"error": "Internal server error"}, 500)

    MAX_UPLOAD = 512 * 1024 * 1024

    def _sqlite_upload(self):
        """Raw-body upload of a .sqlite/.db file into ~/.warden/sqlite/."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0:
            self._json_response({"error": "Empty upload"}, 400)
            return
        if length > self.MAX_UPLOAD:
            self._json_response({"error": "File too large (max 512 MB)"}, 413)
            return
        raw_name = os.path.basename(self.headers.get("X-Filename", "database.db"))
        stem, ext = os.path.splitext(raw_name)
        stem = re.sub(r"[^A-Za-z0-9_.\-]", "_", stem)[:80] or "database"
        if ext.lower() not in (".db", ".sqlite", ".sqlite3"):
            ext = ".db"
        dest_dir = Path.home() / ".warden" / "sqlite"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{stem}{ext}"
        n = 1
        while dest.exists():
            dest = dest_dir / f"{stem}-{n}{ext}"
            n += 1
        remaining = length
        with open(dest, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        code, out, err = sq_query(dest, "SELECT COUNT(*) FROM sqlite_master")
        if code != 0:
            dest.unlink(missing_ok=True)
            self._json_response({"error": "That file is not a readable SQLite database"}, 400)
            return
        audit("local", "sqlite", "UPLOAD", str(dest))
        self._json_response({"ok": True, "path": str(dest), "name": dest.name})

    def _query_stream(self, body):
        """Live mode: raw engine output streamed to the browser as it arrives."""
        cfg, err = get_config(body)
        if err:
            self._json_response({"error": err}, 400)
            return
        query = body.get("query")
        if not isinstance(query, str) or not query.strip():
            self._json_response({"error": "Query is required"}, 400)
            return
        query = query.strip()[:MAX_QUERY_LEN]
        engine = body.get("engine", "documentdb")
        env = body.get("env", "custom")
        if body.get("read_only"):
            violation = read_only_violation(engine, query)
            if violation:
                self._json_response({"error": violation}, 403)
                return
        database = body.get("database") or ""
        if database:
            try:
                database = validate_ident(database, "database")
            except ValueError as e:
                self._json_response({"error": str(e)}, 400)
                return

        proc_env = None
        fam = engine_family(engine)
        if fam == "documentdb":
            db = database or "admin"
            args = docdb_args(cfg, body["admin_user"], body["admin_pass"], db) + ["--eval", query]
            engine_name = "documentdb"
        elif fam == "mysql":
            db = database or cfg.get("default_db") or ""
            args = ["mysql", "-h", cfg["host"], "-P", str(cfg["port"]), "-u", body["admin_user"],
                    "--protocol=TCP", "-t", "-e", query] + (["-D", db] if db else [])
            proc_env = os.environ.copy()
            proc_env["MYSQL_PWD"] = body["admin_pass"]
            engine_name = "mysql"
        elif fam == "sqlite":
            db = Path(cfg["path"]).name
            args = ["sqlite3", "-batch", "-column", "-header", str(cfg["path"]), query]
            engine_name = "sqlite"
        else:
            db = database or cfg.get("default_db", "postgres")
            args = ["psql", "-h", cfg["host"], "-p", str(cfg["port"]),
                    "-U", body["admin_user"], "-d", db, "--no-psqlrc", "-c", query]
            proc_env = os.environ.copy()
            proc_env["PGPASSWORD"] = body["admin_pass"]
            engine_name = "postgresql"
        audit(env, engine_name, "QUERY", f"db={db} [live] :: {query[:300]}")

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=proc_env, bufsize=0)
        deadline = time.time() + 300
        try:
            while True:
                chunk = proc.stdout.read(1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                if time.time() > deadline:
                    proc.kill()
                    self.wfile.write(b"\n[warden] stream timed out after 300s\n")
                    break
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()


def create_server(host=DEFAULT_HOST, port=DEFAULT_PORT):
    if not INDEX_HTML.exists():
        print(f"Warning: {INDEX_HTML} not found, the web UI won't load.")
    # Threaded so a long-running or streamed query doesn't block the UI.
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.warden_bind_host = host   # used to reject cross-origin / DNS-rebound requests
    server.warden_port = port
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(prog="warden-web", description="warden web UI server")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Bind address (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (default: {DEFAULT_PORT})")
    args = parser.parse_args(argv)

    server = create_server(args.host, args.port)
    print(f"\n  warden web server running at http://{args.host}:{args.port}")
    print("  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
