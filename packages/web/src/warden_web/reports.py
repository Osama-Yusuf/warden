"""Read-only access reports for Ward.

Every engine describes "who can do what" differently: Postgres and MySQL hand out
privileges, MongoDB roles, Redis ACL command categories, Elasticsearch role names.
This turns each user's `user_info` into one shared shape (can they log in, are they
an admin, can they write, and where), then assembles that into the report the user
asked for. Nothing here writes; it only reads and summarises.

The derived signals are deliberately coarse. `admin` and `write` are the ones worth
trusting for "who can touch prod"; the per-scope levels are a best-effort read of a
model that (on SQL especially) is really table-by-table, so they inform rather than
promise.
"""
from __future__ import annotations

# ── per-engine normalisation ────────────────────────────────────────────────

_MONGO_ADMIN = {"root", "useradminanydatabase", "dbowner", "clusteradmin",
                "useradmin", "__system", "readwriteanydatabase", "dbadminanydatabase"}
_MONGO_WRITE = {"readwrite", "dbowner", "root", "readwriteanydatabase"}
_MONGO_ADMIN_LVL = {"root", "dbowner", "dbadmin", "useradmin", "clusteradmin"}

_SQL_WRITE = {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "TRUNCATE",
              "REFERENCES", "INDEX", "ALL", "ALL PRIVILEGES"}


def _sys_table(db, table):
    t = str(table or "").lower()
    return t.startswith("information_schema.") or t.startswith("pg_catalog.")


def _pg_scopes(info):
    scopes = []
    for row in info.get("db_privileges") or []:
        privs = {str(p).upper() for p in (row.get("privileges") or [])}
        if not privs:
            continue
        level = "write" if "CREATE" in privs else "read"
        scopes.append({"scope": row.get("database", "?"), "level": level})
    return scopes


def _normalize_pg(info):
    superuser = bool(info.get("superuser"))
    scopes = _pg_scopes(info)
    # A real write grant on a non-system table lifts the user to writer even if the
    # db-level privilege was only CONNECT.
    table_write = any(str(g.get("privilege", "")).upper() in _SQL_WRITE
                      and not _sys_table(g.get("db"), g.get("table"))
                      for g in (info.get("grants") or []))
    can_write = superuser or bool(info.get("createdb")) or bool(info.get("createrole")) \
        or any(s["level"] == "write" for s in scopes) or table_write
    flags = []
    if superuser:
        flags.append("superuser")
    if str(info.get("valid_until", "")).lower() in ("never", "", "infinity"):
        flags.append("no_expiry")
    return {
        "login": bool(info.get("can_login")),
        "admin": superuser,
        "write": can_write,
        "scopes": scopes,
        "flags": flags,
    }


def _parse_mysql_grant(stmt):
    """One SHOW GRANTS line -> (scope, privileges set, with_grant). Best effort."""
    s = stmt.strip()
    up = s.upper()
    if not up.startswith("GRANT "):
        return None
    on = up.find(" ON ")
    to = up.find(" TO ")
    if on < 0 or to < 0:
        return None
    privs = {p.strip() for p in s[6:on].split(",") if p.strip()}
    privs = {p.upper() for p in privs}
    scope = s[on + 4:to].strip().strip("`").replace("`", "").replace(".*", "").replace("*.*", "*")
    return {"scope": scope or "*", "privs": privs, "grant_option": "WITH GRANT OPTION" in up}


def _normalize_mysql(info):
    scopes, admin, write = [], False, False
    for stmt in info.get("grant_statements") or []:
        g = _parse_mysql_grant(stmt)
        if not g:
            continue
        privs = g["privs"]
        if privs <= {"USAGE"}:
            continue   # USAGE alone is "account exists", not access
        global_all = g["scope"] == "*" and ("ALL PRIVILEGES" in privs or "ALL" in privs)
        if global_all or (g["scope"] == "*" and g["grant_option"]):
            admin = True
        w = bool(privs & _SQL_WRITE)
        write = write or w
        level = "admin" if (global_all or (g["grant_option"] and g["scope"] != "*")) else ("write" if w else "read")
        scopes.append({"scope": g["scope"], "level": level})
    return {
        "login": bool(info.get("can_login")) and not info.get("locked"),
        "admin": admin,
        "write": write or admin,
        "scopes": scopes,
        "flags": (["superuser"] if admin else []) + (["locked"] if info.get("locked") else []),
    }


def _normalize_mongo(info):
    scopes, admin, write = [], False, False
    for r in info.get("roles") or []:
        role = str(r.get("role", "")).lower()
        db = r.get("db", "?")
        if role in _MONGO_ADMIN:
            admin = True
        if role in _MONGO_WRITE:
            write = True
        level = "admin" if role in _MONGO_ADMIN_LVL else ("write" if role in _MONGO_WRITE else "read")
        scopes.append({"scope": db, "level": level})
    return {
        "login": True,   # a Mongo user that exists can authenticate
        "admin": admin,
        "write": write or admin,
        "scopes": scopes,
        "flags": ["superuser"] if admin else [],
    }


def _normalize_redis(info):
    cmds = str(info.get("commands", ""))
    keys = str(info.get("keys", ""))
    full = "+@all" in cmds or "allcommands" in cmds
    admin = full or "+@admin" in cmds or "+@dangerous" in cmds or bool(info.get("reserved"))
    write = full or "+@write" in cmds
    level = "admin" if admin else ("write" if write else "read")
    flags = []
    if full and (keys == "~*" or "allkeys" in keys):
        flags.append("full_access")
    return {
        "login": bool(info.get("enabled", True)),
        "admin": admin,
        "write": write or admin,
        "scopes": [{"scope": keys or "keys", "level": level}],
        "flags": flags,
    }


def _normalize_es(info):
    roles = [str(r).lower() for r in (info.get("roles") or [])]
    admin = "superuser" in roles
    write = admin or "editor" in roles
    level = "admin" if admin else ("write" if write else "read")
    return {
        "login": bool(info.get("enabled", True)),
        "admin": admin,
        "write": write,
        "scopes": [{"scope": r, "level": level} for r in roles] or [{"scope": "cluster", "level": level}],
        "flags": ["superuser"] if admin else [],
    }


_NORMALIZERS = {
    "postgresql": _normalize_pg,
    "mysql": _normalize_mysql,
    "documentdb": _normalize_mongo,
    "redis": _normalize_redis,
    "elasticsearch": _normalize_es,
}


def _summary(n):
    """A short readable line for a normalized user."""
    if n["admin"]:
        return "full admin"
    parts = []
    for s in n["scopes"][:6]:
        parts.append(f"{s['level']} on {s['scope']}")
    if not parts:
        return "no access" if not n["login"] else "login only, no grants"
    extra = len(n["scopes"]) - 6
    line = ", ".join(parts)
    return line + (f", +{extra} more" if extra > 0 else "")


def normalize_user(info, fam):
    """user_info dict + engine family -> the shared shape the reports read."""
    fn = _NORMALIZERS.get(fam)
    base = fn(info) if fn else {"login": True, "admin": False, "write": False, "scopes": [], "flags": []}
    row = {"user": info.get("user", "?"), **base}
    if not row["scopes"] and not row["admin"] and "no_access" not in row["flags"]:
        row["flags"] = row["flags"] + ["no_access"]
    row["summary"] = _summary(row)
    return row


# ── report assembly ─────────────────────────────────────────────────────────

def _rank(rows):
    return sorted(rows, key=lambda r: (not r["admin"], not r["write"], r["user"]))


def build_report(kind, infos, fam, is_prod=False):
    """Assemble one report from a list of user_info dicts. Returns a structure the
    UI renders; `note` narration is added by the caller."""
    rows = [normalize_user(i, fam) for i in infos if isinstance(i, dict) and "error" not in i]

    if kind == "write_access":
        writers = _rank([r for r in rows if r["write"]])
        return {
            "kind": "write_access",
            "title": "Who can change data on prod" if is_prod else "Who can change data",
            "is_prod": is_prod,
            "count": len(writers),
            "total": len(rows),
            "users": writers,
        }

    if kind == "posture":
        findings = []
        for r in rows:
            if r["admin"]:
                findings.append({"severity": "high", "user": r["user"],
                                 "issue": "Full admin", "detail": r["summary"]})
            if "full_access" in r["flags"]:
                findings.append({"severity": "high", "user": r["user"],
                                 "issue": "Unrestricted access", "detail": "all commands on all keys"})
            if not r["login"]:
                findings.append({"severity": "low", "user": r["user"],
                                 "issue": "Cannot log in", "detail": "disabled or locked account"})
            elif "no_access" in r["flags"]:
                findings.append({"severity": "low", "user": r["user"],
                                 "issue": "Can log in but has no grants", "detail": "unused account?"})
        order = {"high": 0, "med": 1, "low": 2}
        findings.sort(key=lambda f: (order.get(f["severity"], 3), f["user"]))
        counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("high", "med", "low")}
        return {
            "kind": "posture",
            "title": "Security posture" + (" (prod)" if is_prod else ""),
            "is_prod": is_prod,
            "counts": counts,
            "total": len(rows),
            "findings": findings,
        }

    # default: the access overview
    return {
        "kind": "access",
        "title": "Access overview" + (" (prod)" if is_prod else ""),
        "is_prod": is_prod,
        "total": len(rows),
        "users": _rank(rows),
    }
