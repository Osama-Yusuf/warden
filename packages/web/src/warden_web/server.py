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
    my_exec,
    my_query,
    sq_csv,
    sq_exec,
    sq_query,
    delete_profile,
    load_profile_environments,
    refresh_environments,
    save_profile,
    docdb_args,
    docdb_eval,
    docdb_exec,
    generate_password,
    js_string,
    pg_csv,
    pg_exec,
    pg_ident,
    pg_literal,
    pg_query,
    run_cmd,
    validate_ident,
)

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


def require_fields(body, *fields):
    missing = [f for f in fields if not body.get(f)]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")


def require_creds(body):
    """Admin credentials, except for SQLite where there are none."""
    if engine_family(body.get("engine", "")) == "sqlite":
        return
    require_fields(body, "admin_user", "admin_pass")


def validate_docdb_roles(roles):
    if not isinstance(roles, list):
        raise ValueError("roles must be a list")
    for r in roles:
        if not isinstance(r, dict) or "role" not in r or "db" not in r:
            raise ValueError("Each role must have 'role' and 'db' fields")
        if r["role"] not in DOCDB_ROLES:
            raise ValueError(f"Unknown DocumentDB role: {r['role']}")
        validate_ident(r["db"], "role database")


MYSQL_PRIVILEGES = [
    "SELECT", "INSERT", "UPDATE", "DELETE",
    "ALL PRIVILEGES", "CREATE", "DROP", "ALTER", "INDEX", "EXECUTE",
]

MYSQL_HOST_RE = re.compile(r'^[A-Za-z0-9_.\-%]+$')


def mysql_account(target):
    """'name' or 'name@host' quoted as 'name'@'host'. Host defaults to %."""
    name, _, host = str(target).strip().partition("@")
    name = validate_ident(name, "username")
    host = host or "%"
    if len(host) > 128 or not MYSQL_HOST_RE.match(host):
        raise ValueError("Invalid MySQL host part (letters, digits, _ . - %)")
    return f"'{name}'@'{host}'"


def validate_mysql_privilege(priv):
    upper = str(priv).strip().upper()
    if upper not in {p.upper() for p in MYSQL_PRIVILEGES}:
        raise ValueError(f"Unknown privilege: {priv}")
    return upper


def validate_target(engine, username):
    """Validate a username for the engine. MySQL accounts may carry @host."""
    if engine_family(engine) == "mysql":
        mysql_account(username)  # raises when malformed
        return str(username).strip()
    return validate_ident(username, "username")


def validate_pg_privilege(priv):
    upper = priv.strip().upper()
    valid = {p.upper() for p in PG_PRIVILEGES}
    if upper not in valid:
        raise ValueError(f"Unknown privilege: {priv}")
    return upper


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


def api_connect(body):
    cfg, err = get_config(body)
    if err:
        return {"ok": False, "error": err}
    user = body.get("admin_user", "")
    pwd = body.get("admin_pass", "")
    engine = body.get("engine", "documentdb")

    fam = engine_family(engine)
    if fam == "documentdb":
        data, err = docdb_eval(cfg, user, pwd, "db.runCommand({connectionStatus:1})")
        if err:
            return {"ok": False, "error": err}
        authed = ((data or {}).get("authInfo") or {}).get("authenticatedUsers") or []
        whoami = authed[0].get("user") if authed else user
        prewarm_docdb(cfg, user, pwd)
        return {"ok": True, "host": cfg["host"], "user": whoami}
    if fam == "mysql":
        code, out, err = my_query(cfg, user, pwd, "SELECT CURRENT_USER()")
        if code != 0:
            return {"ok": False, "error": err or "Connection failed"}
        return {"ok": True, "host": cfg["host"], "user": out.strip() or user}
    if fam == "sqlite":
        db_path = Path(cfg["path"])
        if not db_path.is_file():
            return {"ok": False, "error": f"No such file: {db_path}"}
        code, out, err = sq_query(db_path, "SELECT sqlite_version()")
        if code != 0:
            return {"ok": False, "error": err or "Could not open the database file"}
        return {"ok": True, "host": str(db_path), "user": ""}
    code, out, err = pg_query(cfg, user, pwd, "SELECT current_user")
    if code != 0:
        return {"ok": False, "error": err or "Connection failed"}
    return {"ok": True, "host": cfg["host"], "user": out.strip() or user}


def api_test_login(body):
    """Connect AS a given user (their own credentials) and probe what they can
    do. Stateless: never touches the admin session. Read-only checks only."""
    cfg, err = get_config(body)
    if err:
        return {"error": err}
    engine = body.get("engine", "documentdb")
    fam = engine_family(engine)
    if fam == "sqlite":
        return {"error": "SQLite has no user accounts to test"}
    require_fields(body, "test_user", "test_pass")
    tu = body["test_user"]
    tp = body["test_pass"]
    test_db = body.get("test_db") or ""
    if test_db:
        test_db = validate_ident(test_db, "database")
    checks = []

    if fam == "documentdb":
        data, err = docdb_eval(cfg, tu, tp, "db.runCommand({connectionStatus:1})")
        if err or not data:
            return {"ok": True, "auth": False, "error": _clean_mongosh_noise(err or "") or "Authentication failed"}
        roles = ((data or {}).get("authInfo") or {}).get("authenticatedUserRoles") or []
        role_strs = [f"{r.get('role')}@{r.get('db', '*')}" for r in roles]
        dbs, e2 = docdb_eval(cfg, tu, tp, "db.adminCommand({listDatabases:1}).databases.map(d => d.name)")
        checks.append({"name": "List all databases", "ok": e2 is None,
                       "detail": (", ".join(dbs) if e2 is None and dbs else _clean_mongosh_noise(e2 or "") or "not permitted")})
        if test_db:
            names, e3 = docdb_eval(cfg, tu, tp, "db.getCollectionNames()", db=test_db)
            checks.append({"name": f"Read '{test_db}'", "ok": e3 is None,
                           "detail": (f"{len(names or [])} collection(s) visible" if e3 is None
                                      else _clean_mongosh_noise(e3))})
        return {"ok": True, "auth": True, "identity": tu, "roles": role_strs, "checks": checks}

    if fam == "mysql":
        code, out, err = my_query(cfg, tu, tp, "SELECT CURRENT_USER()")
        if code != 0:
            return {"ok": True, "auth": False, "error": (err or "").strip() or "Authentication failed"}
        code2, out2, _ = my_query(cfg, tu, tp, "SHOW DATABASES")
        checks.append({"name": "Databases visible", "ok": code2 == 0,
                       "detail": ", ".join(out2.split()) if code2 == 0 and out2.strip() else "none"})
        grants = []
        code3, out3, _ = my_query(cfg, tu, tp, "SHOW GRANTS")
        if code3 == 0:
            grants = [l for l in out3.split("\n") if l.strip()]
        if test_db:
            code4, out4, err4 = my_query(cfg, tu, tp, "SHOW TABLES", db=test_db)
            checks.append({"name": f"Access '{test_db}'", "ok": code4 == 0,
                           "detail": (f"{len(out4.split())} table(s) visible" if code4 == 0 else (err4 or "").strip() or "denied")})
        return {"ok": True, "auth": True, "identity": out.strip(), "grants": grants, "checks": checks}

    # postgresql family
    code, out, err = pg_query(cfg, tu, tp, "SELECT current_user")
    if code != 0:
        return {"ok": True, "auth": False, "error": (err or "").strip() or "Authentication failed"}
    code2, out2, _ = pg_query(cfg, tu, tp,
        "SELECT datname FROM pg_database WHERE datistemplate=false "
        "AND has_database_privilege(datname, 'CONNECT') ORDER BY datname")
    checks.append({"name": "Databases they can connect to", "ok": code2 == 0,
                   "detail": ", ".join(out2.split()) if code2 == 0 and out2.strip() else "none"})
    if test_db:
        code3, out3, err3 = pg_query(cfg, tu, tp, "SELECT 1", db=test_db)
        checks.append({"name": f"Connect to '{test_db}'", "ok": code3 == 0,
                       "detail": "connected" if code3 == 0 else (err3 or "").strip() or "denied"})
    return {"ok": True, "auth": True, "identity": out.strip(), "checks": checks}


def api_list_users(body):
    cfg, err = get_config(body)
    if err:
        return {"error": err}
    require_creds(body)
    user = body.get("admin_user", "")
    pwd = body.get("admin_pass", "")
    engine = body.get("engine", "documentdb")
    fam = engine_family(engine)

    if fam == "mysql":
        sql = ("SELECT user, host, account_locked FROM mysql.user "
               "WHERE user NOT IN ('mysql.sys','mysql.session','mysql.infoschema',"
               "'rdsadmin','rdsrepladmin') ORDER BY user, host")
        code, out, err = my_query(cfg, user, pwd, sql)
        if code != 0:
            return {"error": err}
        users = []
        for line in out.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) >= 3:
                users.append({"user": f"{parts[0]}@{parts[1]}",
                              "can_login": parts[2] != "Y",
                              "superuser": False, "createdb": False,
                              "createrole": False, "valid_until": "never"})
        return {"users": users}
    if fam == "sqlite":
        return {"users": [], "note": "SQLite has no user accounts"}

    if fam == "documentdb":
        data, err = docdb_read(cfg, user, pwd, "db.adminCommand({usersInfo:1}).users")
        if err:
            return {"error": err}
        users = []
        for u in (data or []):
            roles = u.get("roles", [])
            users.append({
                "user": u.get("user", "?"),
                "db": u.get("db", "?"),
                "roles": [f"{r['role']}@{r.get('db', '*')}" for r in roles],
                "roles_raw": roles,
            })
        return {"users": users}
    else:
        sql = """
            SELECT rolname, rolcanlogin, rolsuper, rolcreatedb,
                   rolcreaterole, COALESCE(rolvaliduntil::text, 'never')
            FROM pg_roles
            WHERE rolname NOT LIKE 'pg_%%'
              AND rolname NOT IN ('rdsadmin','rds_superuser','rds_replication','rds_password','rdsrepladmin')
            ORDER BY rolname
        """
        code, out, err = pg_query(cfg, user, pwd, sql)
        if code != 0:
            return {"error": err}
        users = []
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 6:
                users.append({
                    "user": parts[0],
                    "can_login": parts[1] == "t",
                    "superuser": parts[2] == "t",
                    "createdb": parts[3] == "t",
                    "createrole": parts[4] == "t",
                    "valid_until": parts[5],
                })
        return {"users": users}


def api_user_info(body):
    cfg, err = get_config(body)
    if err:
        return {"error": err}
    require_fields(body, "admin_user", "admin_pass", "username")
    user = body.get("admin_user", "")
    pwd = body.get("admin_pass", "")
    engine = body.get("engine", "documentdb")
    fam = engine_family(engine)
    target = validate_target(engine, body["username"])

    if fam == "mysql":
        acct = mysql_account(target)
        code, out, err = my_query(cfg, user, pwd, f"SHOW GRANTS FOR {acct}")
        if code != 0:
            return {"error": err.strip() or "User not found"}
        grants = [l for l in out.strip().split("\n") if l.strip()]
        name, _, host = target.partition("@")
        code2, out2, _ = my_query(
            cfg, user, pwd,
            "SELECT account_locked FROM mysql.user WHERE user = '%s' AND host = '%s'"
            % (name.replace("'", "''"), (host or '%').replace("'", "''")))
        locked = out2.strip() == "Y"
        return {"user": target, "engine_family": "mysql", "locked": locked,
                "can_login": not locked, "grant_statements": grants}
    if fam == "sqlite":
        return {"error": "SQLite has no user accounts"}

    if fam == "documentdb":
        data, err = docdb_read(cfg, user, pwd, f'db.adminCommand({{usersInfo: {js_string(target)}}}).users')
        if err:
            return {"error": err}
        if not data:
            return {"error": f"User not found"}
        u = data[0]
        return {
            "user": u.get("user"),
            "db": u.get("db", "?"),
            "userId": str(u.get("userId", "n/a")),
            "roles": u.get("roles", []),
        }
    else:
        sql = f"""
            SELECT rolname, rolcanlogin, rolsuper, rolcreatedb,
                   rolcreaterole, COALESCE(rolvaliduntil::text,'never'),
                   COALESCE(rolconnlimit::text,'unlimited')
            FROM pg_roles WHERE rolname = {pg_literal(target)}
        """
        code, out, err = pg_query(cfg, user, pwd, sql)
        if code != 0 or not out.strip():
            return {"error": "User not found"}
        parts = out.strip().split("\t")
        info = {
            "user": parts[0],
            "can_login": parts[1] == "t",
            "superuser": parts[2] == "t",
            "createdb": parts[3] == "t",
            "createrole": parts[4] == "t",
            "valid_until": parts[5],
            "conn_limit": parts[6] if len(parts) > 6 else "unlimited",
        }

        sql2 = f"""
            SELECT table_catalog, table_schema||'.'||table_name, privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee = {pg_literal(target)} ORDER BY 1,2 LIMIT 50
        """
        code2, out2, _ = pg_query(cfg, user, pwd, sql2)
        grants = []
        if code2 == 0 and out2.strip():
            for line in out2.strip().split("\n"):
                p = line.split("\t")
                if len(p) >= 3:
                    grants.append({"db": p[0], "table": p[1], "privilege": p[2]})

        sql3 = f"""
            SELECT datname,
                   has_database_privilege({pg_literal(target)}, datname, 'CONNECT'),
                   has_database_privilege({pg_literal(target)}, datname, 'CREATE')
            FROM pg_database WHERE datistemplate=false AND datname NOT IN ('rdsadmin')
            ORDER BY datname
        """
        code3, out3, _ = pg_query(cfg, user, pwd, sql3)
        db_privs = []
        if code3 == 0 and out3.strip():
            for line in out3.strip().split("\n"):
                p = line.split("\t")
                if len(p) >= 3:
                    privs = []
                    if p[1] == "t": privs.append("CONNECT")
                    if p[2] == "t": privs.append("CREATE")
                    db_privs.append({"database": p[0], "privileges": privs})

        info["grants"] = grants
        info["db_privileges"] = db_privs
        return info


def api_create_user(body):
    cfg, err = get_config(body)
    if err:
        return {"error": err}
    require_fields(body, "admin_user", "admin_pass", "username")
    adm_user = body.get("admin_user", "")
    adm_pass = body.get("admin_pass", "")
    engine = body.get("engine", "documentdb")
    env = body.get("env", "production")
    fam = engine_family(engine)
    target = validate_target(engine, body["username"])
    password = body.get("password") or generate_password()

    if fam == "sqlite":
        return {"error": "SQLite has no user accounts"}
    if fam == "mysql":
        acct = mysql_account(target)
        pwd_lit = password.replace("\\", "\\\\").replace("'", "\\'")
        ok, out, err = my_exec(cfg, adm_user, adm_pass,
                               f"CREATE USER {acct} IDENTIFIED BY '{pwd_lit}'")
        if ok:
            audit(env, "mysql", "CREATE USER", target)
            return {"ok": True, "password": password}
        return {"error": err or out}
    if fam == "documentdb":
        roles = body.get("roles", [])
        validate_docdb_roles(roles)
        role_docs = json.dumps(roles)
        js = f'db.createUser({{user: {js_string(target)}, pwd: {js_string(password)}, roles: {role_docs}}})'
        ok, out, err = docdb_exec(cfg, adm_user, adm_pass, js)
        if ok:
            audit(env, "documentdb", "CREATE USER", f"{target} roles={roles}")
            return {"ok": True, "password": password}
        return {"error": err or out}
    else:
        login = "LOGIN" if body.get("can_login", True) else "NOLOGIN"
        sql = f"CREATE USER {pg_ident(target)} WITH {login} PASSWORD {pg_literal(password)}"
        ok, out, err = pg_exec(cfg, adm_user, adm_pass, sql)
        if ok:
            audit(env, "postgresql", "CREATE USER", target)
            return {"ok": True, "password": password}
        return {"error": err or out}


def api_reset_password(body):
    cfg, err = get_config(body)
    if err:
        return {"error": err}
    require_fields(body, "admin_user", "admin_pass", "username")
    adm_user = body.get("admin_user", "")
    adm_pass = body.get("admin_pass", "")
    engine = body.get("engine", "documentdb")
    env = body.get("env", "production")
    fam = engine_family(engine)
    target = validate_target(engine, body["username"])
    password = body.get("password") or generate_password()

    if fam == "sqlite":
        return {"error": "SQLite has no user accounts"}
    if fam == "mysql":
        acct = mysql_account(target)
        pwd_lit = password.replace("\\", "\\\\").replace("'", "\\'")
        ok, out, err = my_exec(cfg, adm_user, adm_pass,
                               f"ALTER USER {acct} IDENTIFIED BY '{pwd_lit}'")
        if ok:
            audit(env, "mysql", "RESET PASSWORD", target)
            return {"ok": True, "password": password}
        return {"error": err or out}
    if fam == "documentdb":
        js = f'db.updateUser({js_string(target)}, {{pwd: {js_string(password)}}})'
        ok, out, err = docdb_exec(cfg, adm_user, adm_pass, js)
        if ok:
            audit(env, "documentdb", "RESET PASSWORD", target)
            return {"ok": True, "password": password}
        return {"error": err or out}
    else:
        sql = f"ALTER USER {pg_ident(target)} WITH PASSWORD {pg_literal(password)}"
        ok, out, err = pg_exec(cfg, adm_user, adm_pass, sql)
        if ok:
            audit(env, "postgresql", "RESET PASSWORD", target)
            return {"ok": True, "password": password}
        return {"error": err or out}


def api_grant(body):
    cfg, err = get_config(body)
    if err:
        return {"error": err}
    require_fields(body, "admin_user", "admin_pass", "username")
    adm_user = body.get("admin_user", "")
    adm_pass = body.get("admin_pass", "")
    engine = body.get("engine", "documentdb")
    env = body.get("env", "production")
    fam = engine_family(engine)
    target = validate_target(engine, body["username"])

    if fam == "sqlite":
        return {"error": "SQLite has no user accounts"}
    if fam == "mysql":
        acct = mysql_account(target)
        priv = validate_mysql_privilege(body.get("privilege", "SELECT"))
        db_raw = str(body.get("database", "*")).strip() or "*"
        obj = "*.*" if db_raw == "*" else f"`{validate_ident(db_raw, 'database')}`.*"
        ok, out, err = my_exec(cfg, adm_user, adm_pass, f"GRANT {priv} ON {obj} TO {acct}")
        if ok:
            audit(env, "mysql", "GRANT", f"{target} += {priv} on {obj}")
            return {"ok": True}
        return {"error": err or out}
    if fam == "documentdb":
        roles = body.get("roles", [])
        validate_docdb_roles(roles)
        role_docs = json.dumps(roles)
        js = f'db.grantRolesToUser({js_string(target)}, {role_docs})'
        ok, out, err = docdb_exec(cfg, adm_user, adm_pass, js)
        if ok:
            audit(env, "documentdb", "GRANT", f"{target} += {roles}")
            return {"ok": True}
        return {"error": err or out}
    else:
        priv = validate_pg_privilege(body.get("privilege", "SELECT"))
        database = validate_ident(body.get("database", "postgres"), "database")
        schema = validate_ident(body.get("schema", "public"), "schema")
        if priv in ("CONNECT", "CREATE"):
            sql = f"GRANT {priv} ON DATABASE {pg_ident(database)} TO {pg_ident(target)}"
            run_db = cfg.get("default_db", "postgres")
        else:
            sql = f"GRANT {priv} ON ALL TABLES IN SCHEMA {pg_ident(schema)} TO {pg_ident(target)}"
            run_db = database
        ok, out, err = pg_exec(cfg, adm_user, adm_pass, sql, db=run_db)
        if ok:
            audit(env, "postgresql", "GRANT", f"{target} += {priv} on {database}.{schema}")
            return {"ok": True}
        return {"error": err or out}


def api_revoke(body):
    cfg, err = get_config(body)
    if err:
        return {"error": err}
    require_fields(body, "admin_user", "admin_pass", "username")
    adm_user = body.get("admin_user", "")
    adm_pass = body.get("admin_pass", "")
    engine = body.get("engine", "documentdb")
    env = body.get("env", "production")
    fam = engine_family(engine)
    target = validate_target(engine, body["username"])

    if fam == "sqlite":
        return {"error": "SQLite has no user accounts"}
    if fam == "mysql":
        acct = mysql_account(target)
        priv = validate_mysql_privilege(body.get("privilege", "SELECT"))
        db_raw = str(body.get("database", "*")).strip() or "*"
        obj = "*.*" if db_raw == "*" else f"`{validate_ident(db_raw, 'database')}`.*"
        ok, out, err = my_exec(cfg, adm_user, adm_pass, f"REVOKE {priv} ON {obj} FROM {acct}")
        if ok:
            audit(env, "mysql", "REVOKE", f"{target} -= {priv} on {obj}")
            return {"ok": True}
        return {"error": err or out}
    if fam == "documentdb":
        roles = body.get("roles", [])
        validate_docdb_roles(roles)
        role_docs = json.dumps(roles)
        js = f'db.revokeRolesFromUser({js_string(target)}, {role_docs})'
        ok, out, err = docdb_exec(cfg, adm_user, adm_pass, js)
        if ok:
            audit(env, "documentdb", "REVOKE", f"{target} -= {roles}")
            return {"ok": True}
        return {"error": err or out}
    else:
        priv = validate_pg_privilege(body.get("privilege", "SELECT"))
        database = validate_ident(body.get("database", "postgres"), "database")
        schema = validate_ident(body.get("schema", "public"), "schema")
        if priv in ("CONNECT", "CREATE"):
            sql = f"REVOKE {priv} ON DATABASE {pg_ident(database)} FROM {pg_ident(target)}"
            run_db = cfg.get("default_db", "postgres")
        else:
            sql = f"REVOKE {priv} ON ALL TABLES IN SCHEMA {pg_ident(schema)} FROM {pg_ident(target)}"
            run_db = database
        ok, out, err = pg_exec(cfg, adm_user, adm_pass, sql, db=run_db)
        if ok:
            audit(env, "postgresql", "REVOKE", f"{target} -= {priv} on {database}.{schema}")
            return {"ok": True}
        return {"error": err or out}


def api_drop_user(body):
    cfg, err = get_config(body)
    if err:
        return {"error": err}
    require_fields(body, "admin_user", "admin_pass", "username")
    adm_user = body.get("admin_user", "")
    adm_pass = body.get("admin_pass", "")
    engine = body.get("engine", "documentdb")
    env = body.get("env", "production")
    fam = engine_family(engine)
    target = validate_target(engine, body["username"])

    if fam == "sqlite":
        return {"error": "SQLite has no user accounts"}
    if fam == "mysql":
        acct = mysql_account(target)
        ok, out, err = my_exec(cfg, adm_user, adm_pass, f"DROP USER {acct}")
        if ok:
            audit(env, "mysql", "DROP USER", target)
            return {"ok": True}
        return {"error": err or out}
    if fam == "documentdb":
        ok, out, err = docdb_exec(cfg, adm_user, adm_pass, f'db.dropUser({js_string(target)})')
        if ok:
            audit(env, "documentdb", "DROP USER", target)
            return {"ok": True}
        return {"error": err or out}
    else:
        ok, out, err = pg_exec(cfg, adm_user, adm_pass, f"DROP USER {pg_ident(target)}")
        if ok:
            audit(env, "postgresql", "DROP USER", target)
            return {"ok": True}
        return {"error": err or out}


def api_toggle_login(body):
    cfg, err = get_config(body)
    if err:
        return {"error": err}
    engine = body.get("engine", "")
    fam = engine_family(engine)
    if fam == "documentdb":
        return {"error": "Not applicable to DocumentDB"}
    if fam == "sqlite":
        return {"error": "SQLite has no user accounts"}
    require_fields(body, "admin_user", "admin_pass", "username")
    adm_user = body.get("admin_user", "")
    adm_pass = body.get("admin_pass", "")
    env = body.get("env", "production")
    target = validate_target(engine, body["username"])
    enable = body.get("enable", True)
    if fam == "mysql":
        acct = mysql_account(target)
        kw = "UNLOCK" if enable else "LOCK"
        ok, out, err = my_exec(cfg, adm_user, adm_pass, f"ALTER USER {acct} ACCOUNT {kw}")
        if ok:
            action = "ENABLE" if enable else "DISABLE"
            audit(env, "mysql", f"{action} USER", target)
            return {"ok": True}
        return {"error": err or out}
    kw = "LOGIN" if enable else "NOLOGIN"
    ok, out, err = pg_exec(cfg, adm_user, adm_pass, f"ALTER USER {pg_ident(target)} WITH {kw}")
    if ok:
        action = "ENABLE" if enable else "DISABLE"
        audit(env, "postgresql", f"{action} USER", target)
        return {"ok": True}
    return {"error": err or out}


def api_list_databases(body):
    cfg, err = get_config(body)
    if err:
        return {"error": err}
    require_creds(body)
    adm_user = body.get("admin_user", "")
    adm_pass = body.get("admin_pass", "")
    engine = body.get("engine", "documentdb")
    fam = engine_family(engine)

    if fam == "mysql":
        sql = ("SELECT s.schema_name, COALESCE(SUM(t.data_length + t.index_length), 0) "
               "FROM information_schema.schemata s "
               "LEFT JOIN information_schema.tables t ON t.table_schema = s.schema_name "
               "WHERE s.schema_name NOT IN ('information_schema','performance_schema','sys') "
               "GROUP BY s.schema_name ORDER BY s.schema_name")
        code, out, err = my_query(cfg, adm_user, adm_pass, sql)
        if code != 0:
            return {"error": err}
        dbs = []
        for line in out.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) >= 2:
                dbs.append({"name": parts[0], "size_bytes": int(float(parts[1])),
                            "size_mb": round(float(parts[1]) / 1048576, 1)})
        return {"databases": dbs}
    if fam == "sqlite":
        db_path = Path(cfg["path"])
        size = db_path.stat().st_size if db_path.is_file() else 0
        return {"databases": [{"name": db_path.name, "size_bytes": size,
                               "size_mb": round(size / 1048576, 1)}]}

    if fam == "documentdb":
        data, err = docdb_read(cfg, adm_user, adm_pass, "db.adminCommand({listDatabases:1}).databases")
        if err:
            return {"error": err}
        dbs = []
        for d in (data or []):
            size_bytes = _docdb_num(d.get("sizeOnDisk", 0))
            dbs.append({
                "name": d["name"],
                "size_bytes": size_bytes,
                "size_mb": round(size_bytes / (1024 * 1024), 1),
                "empty": d.get("empty", False),
            })
        return {"databases": dbs}
    else:
        sql = """
            SELECT datname, pg_database_size(datname)::bigint
            FROM pg_database WHERE datistemplate=false AND datname NOT IN ('rdsadmin')
            ORDER BY datname
        """
        code, out, err = pg_query(cfg, adm_user, adm_pass, sql)
        if code != 0:
            return {"error": err}
        dbs = []
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                size_bytes = int(parts[1])
                dbs.append({
                    "name": parts[0],
                    "size_bytes": size_bytes,
                    "size_mb": round(size_bytes / (1024 * 1024), 1),
                })
        return {"databases": dbs}


def api_list_collections(body):
    cfg, err = get_config(body)
    if err:
        return {"error": err}
    require_creds(body)
    adm_user = body.get("admin_user", "")
    adm_pass = body.get("admin_pass", "")
    engine = body.get("engine", "documentdb")
    fam = engine_family(engine)
    if fam == "sqlite":
        db_path = Path(cfg["path"])
        code, out, err = sq_query(db_path,
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        if code != 0:
            return {"error": err}
        names = [l.strip() for l in out.strip().split("\n") if l.strip()]
        sizes = {}
        code2, out2, _ = sq_query(db_path, "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")
        if code2 == 0:
            for line in out2.strip().split("\n"):
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1].isdigit():
                    sizes[parts[0]] = int(parts[1])
        return {"tables": [{"schema": "main", "table": n,
                            **({"size_bytes": sizes[n]} if n in sizes else {})} for n in names]}
    database = validate_ident(body.get("database", ""), "database")
    if fam == "mysql":
        sql = ("SELECT table_name, COALESCE(data_length + index_length, 0) "
               "FROM information_schema.tables WHERE table_schema = '%s' "
               "ORDER BY table_name" % database.replace("'", "''"))
        code, out, err = my_query(cfg, adm_user, adm_pass, sql)
        if code != 0:
            return {"error": err}
        tables = []
        for line in out.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) >= 2:
                tables.append({"schema": database, "table": parts[0],
                               "size_bytes": int(float(parts[1]))})
        return {"tables": tables}

    if fam == "documentdb":
        data, err = docdb_read(cfg, adm_user, adm_pass, "db.getCollectionNames()", db=database)
        if err:
            return {"error": err}
        return {"collections": sorted(data or [])}
    else:
        sql = """
            SELECT n.nspname, c.relname, pg_total_relation_size(c.oid)::bigint
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r','p','m')
              AND n.nspname NOT IN ('pg_catalog','information_schema')
            ORDER BY n.nspname, c.relname
        """
        code, out, err = pg_query(cfg, adm_user, adm_pass, sql, db=database)
        if code != 0:
            return {"error": err}
        tables = []
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                tables.append({"schema": parts[0], "table": parts[1],
                               "size_bytes": int(parts[2])})
            elif len(parts) >= 2:
                tables.append({"schema": parts[0], "table": parts[1]})
        return {"tables": tables}


MAX_QUERY_LEN = 20000
MAX_OUTPUT_LEN = 300000

CURSOR_CALL_RE = re.compile(r'\.(find|aggregate)\s*\(')
CURSOR_CONSUMED_RE = re.compile(r'\.(toArray|forEach|itcount|explain|next|size|map)\s*\(')


# ---------------------------------------------------------------------------
# Read-only mode is a seatbelt, not a security boundary. The client sends
# read_only: true and the server refuses anything that mutates.
# ---------------------------------------------------------------------------

RO_BLOCKED_ROUTES = {
    "/api/create-user", "/api/reset-password", "/api/grant",
    "/api/revoke", "/api/drop-user", "/api/toggle-login",
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


def _docdb_num(v):
    """DocumentDB counters come back as BSON Longs that JSON.stringify turns
    into objects ({$numberLong}, {low,high}, {$numberDecimal}). Coerce to int."""
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, dict):
        if "$numberLong" in v:
            return int(v["$numberLong"])
        if "$numberDecimal" in v:
            return int(float(v["$numberDecimal"]))
        if "$numberInt" in v:
            return int(v["$numberInt"])
        if "$numberDouble" in v:
            try:
                return int(float(v["$numberDouble"]))
            except (ValueError, TypeError):
                return 0
        if "low" in v and "high" in v:
            return (int(v["high"]) << 32) + (int(v["low"]) & 0xFFFFFFFF)
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0


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

    def run_json(self, query, db, timeout=45):
        """Run a single-expression read, returning parsed JSON. Uses plain
        JSON.stringify so the output matches the one-shot docdb_eval path
        exactly (drop-in replacement, same downstream parsing)."""
        import uuid
        with self.lock:
            self.last_used = time.time()
            if not self.alive():
                raise SessionError("session dead")
            tag = uuid.uuid4().hex[:10]
            ok_m = f"__WARDEN_JOK_{tag}__"
            err_m = f"__WARDEN_JERR_{tag}__"
            ok_expr = f'"__WARDEN_" + "JOK_{tag}" + "__"'
            err_expr = f'"__WARDEN_" + "JERR_{tag}" + "__"'
            one_line = " ".join(query.splitlines()).strip()
            payload = (
                f'db = db.getSiblingDB({js_string(db)}); '
                f'try {{ print({ok_expr} + JSON.stringify(( {one_line} ))) }} '
                f'catch (e) {{ print({err_expr} + (e && e.message || String(e))) }}'
            )
            lines = self._roundtrip(payload, timeout)
            if lines is None:
                self.close()
                raise SessionTimeout(f"Query timed out after {timeout}s")
            for idx, line in enumerate(lines):
                if ok_m in line:
                    raw = self._trim_to_json("\n".join([line.split(ok_m, 1)[1]] + lines[idx + 1:]))
                    try:
                        return {"ok": True, "data": json.loads(raw.strip())}
                    except (json.JSONDecodeError, ValueError):
                        return {"ok": False, "error": "Could not parse result"}
                if err_m in line:
                    msg = "\n".join([line.split(err_m, 1)[1]] + lines[idx + 1:])
                    return {"ok": False, "error": msg.strip()}
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


def docdb_read(cfg, user, pwd, js, db="admin", timeout=30):
    """Fast DocumentDB read through the warm session pool, with a transparent
    one-shot fallback. Returns (data, error) exactly like docdb_eval."""
    try:
        sess = get_mongo_session(cfg, user, pwd)
        r = sess.run_json(js, db, timeout=timeout)
        if r["ok"]:
            return r["data"], None
        return None, _clean_mongosh_noise(r.get("error") or "") or "Query failed"
    except (SessionError, SessionTimeout):
        return docdb_eval(cfg, user, pwd, js, db=db, timeout=timeout)


def prewarm_docdb(cfg, user, pwd):
    """Open the warm session in the background so the first read is instant."""
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
    data, err = docdb_read(cfg, user, pwd, "db.adminCommand({usersInfo:1}).users")
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
    if fam == "mysql":
        sql = ("SELECT user, host, account_locked, Super_priv, Grant_priv, Create_user_priv, "
               "Insert_priv, Update_priv, Delete_priv, Drop_priv FROM mysql.user "
               "WHERE user NOT IN ('mysql.sys','mysql.session','mysql.infoschema','rdsadmin','rdsrepladmin')")
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
    cfg, err = get_config(body)
    if err:
        return {"error": err}
    require_creds(body)
    adm_user = body.get("admin_user", "")
    adm_pass = body.get("admin_pass", "")
    engine = body.get("engine", "documentdb")

    fam = engine_family(engine)
    if fam == "mysql":
        health = {}
        checks = {
            "version": "SELECT VERSION()",
            "uptime": "SELECT variable_value FROM performance_schema.global_status WHERE variable_name = 'Uptime'",
            "threads": "SELECT variable_value FROM performance_schema.global_status WHERE variable_name = 'Threads_connected'",
            "max_connections": "SELECT @@max_connections",
            "total_size": ("SELECT COALESCE(SUM(data_length + index_length), 0) FROM information_schema.tables "
                           "WHERE table_schema NOT IN ('information_schema','performance_schema','sys')"),
        }
        for key, sql in checks.items():
            code, out, _ = my_query(cfg, adm_user, adm_pass, sql)
            health[key] = out.strip() if code == 0 and out.strip() else None
        code, out, _ = my_query(cfg, adm_user, adm_pass,
            "SELECT id, user, time, LEFT(COALESCE(info, ''), 90) FROM information_schema.processlist "
            "WHERE command <> 'Sleep' AND time >= 5 AND info IS NOT NULL ORDER BY time DESC LIMIT 10")
        slow = []
        if code == 0 and out.strip():
            for line in out.strip().split("\n"):
                p = line.split("\t")
                if len(p) >= 4:
                    slow.append({"pid": p[0], "user": p[1], "runtime": p[2] + "s", "query": p[3]})
        health["slow_queries"] = slow
        return {"engine": "mysql", "health": health}
    if fam == "sqlite":
        db_path = Path(cfg["path"])
        health = {"file": str(db_path),
                  "size_bytes": db_path.stat().st_size if db_path.is_file() else 0}
        for key, sql in {
            "version": "SELECT sqlite_version()",
            "page_count": "PRAGMA page_count",
            "page_size": "PRAGMA page_size",
            "journal_mode": "PRAGMA journal_mode",
            "tables": "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'",
            "integrity": "PRAGMA quick_check",
        }.items():
            code, out, _ = sq_query(db_path, sql)
            health[key] = out.strip() if code == 0 else None
        return {"engine": "sqlite", "health": health}
    if fam == "documentdb":
        # DocumentDB returns these counters as BSON Longs. Coerce to plain
        # numbers or JSON.stringify turns them into {low, high, unsigned} blobs.
        js = ("(() => { const n = v => (v && typeof v.toNumber === 'function') ? v.toNumber()"
              "   : (typeof v === 'number' ? v : Number(v));"
              " const s = db.serverStatus();"
              " const c = s.connections || {};"
              " const out = {version: String(s.version || ''), uptime: n(s.uptime),"
              "   connections: {current: n(c.current), available: n(c.available)},"
              "   mem: s.mem ? {resident: n(s.mem.resident)} : null};"
              " try { const cur = db.adminCommand({currentOp: 1, active: true});"
              "   const prog = cur.inprog || [];"
              "   out.active_ops = prog.length;"
              "   out.slow_ops = prog.filter(o => n(o.secs_running) >= 5).slice(0, 10)"
              "     .map(o => ({opid: String(o.opid), secs: n(o.secs_running), ns: String(o.ns || ''), op: String(o.op || '')}));"
              " } catch (e) { out.active_ops = null; out.slow_ops = []; }"
              " return out })()")
        data, err = docdb_eval(cfg, adm_user, adm_pass, js, timeout=45)
        if err:
            return {"error": err}
        return {"engine": "documentdb", "health": data}
    else:
        health = {}
        checks = {
            "version": "SELECT current_setting('server_version')",
            "uptime": "SELECT date_trunc('second', now() - pg_postmaster_start_time())::text",
            "connections": ("SELECT count(*) FILTER (WHERE state = 'active'),"
                            " count(*) FILTER (WHERE state = 'idle'), count(*),"
                            " current_setting('max_connections') FROM pg_stat_activity"),
            "cache_hit_pct": ("SELECT round(100.0 * sum(blks_hit) /"
                              " nullif(sum(blks_hit) + sum(blks_read), 0), 1) FROM pg_stat_database"),
            "blocked": "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'",
            "replicas": "SELECT count(*), coalesce(max(replay_lag)::text, '') FROM pg_stat_replication",
            "total_size": "SELECT sum(pg_database_size(datname))::bigint FROM pg_database WHERE datistemplate = false",
        }
        for key, sql in checks.items():
            code, out, _ = pg_query(cfg, adm_user, adm_pass, sql)
            health[key] = out.strip().split("\t") if code == 0 and out.strip() else None
        code, out, _ = pg_query(cfg, adm_user, adm_pass,
            "SELECT pid, usename, date_trunc('second', now() - query_start)::text, left(query, 90)"
            " FROM pg_stat_activity WHERE state <> 'idle' AND pid <> pg_backend_pid()"
            " AND now() - query_start > interval '5 seconds' ORDER BY 3 DESC LIMIT 10")
        slow = []
        if code == 0 and out.strip():
            for line in out.strip().split("\n"):
                p = line.split("\t")
                if len(p) >= 4:
                    slow.append({"pid": p[0], "user": p[1], "runtime": p[2], "query": p[3]})
        health["slow_queries"] = slow
        return {"engine": "postgresql", "health": health}


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


REQUIRES_CREDS = {
    "/api/connect", "/api/list-users", "/api/user-info",
    "/api/create-user", "/api/reset-password", "/api/grant",
    "/api/revoke", "/api/drop-user", "/api/toggle-login",
    "/api/list-databases", "/api/list-collections",
    "/api/query", "/api/query-stream",
    "/api/audit-run", "/api/health",
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
        elif path == "/api/config":
            self._json_response(api_config({}))
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
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
        if path in REQUIRES_CREDS and engine_family(body.get("engine", "")) != "sqlite":
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
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(prog="warden-web", description="warden web UI server")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Bind address (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (default: {DEFAULT_PORT})")
    args = parser.parse_args(argv)

    server = create_server(args.host, args.port)
    print(f"\n  warden web server running at http://{args.host}:{args.port}")
    print(f"  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
