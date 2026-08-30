"""Read-only access reports for Ward.

Every engine describes "who can do what" differently: Postgres and MySQL hand out
privileges, MongoDB roles, Redis ACL command categories, Elasticsearch role names.
This turns each user's `user_info` into one shared shape (can they log in, are they
an admin, can they write, and where), then assembles that into the report the user
asked for. Nothing here writes; it only reads and summarises.

Honesty rule for a security report: classify a user as read-only ONLY when their
whole access is recognisably read. Anything we cannot fully resolve — a role
membership whose privileges live elsewhere, a custom role, a bare list of
commands — is marked `unresolved` (write "unknown"), never quietly downgraded to
read. Under-reporting who can write is the one failure a "who can touch prod"
report must not make, so when in doubt we say "unresolved", not "safe".

Known coverage gaps (the report is about named user accounts, per connection):
  - Postgres privileges granted to PUBLIC (every role) aren't attributed to each
    user, so a PUBLIC table-write grant would not show up per user. Database-level
    PUBLIC CONNECT/CREATE is caught (has_database_privilege honours PUBLIC).
  - Elasticsearch API keys and run-as aren't users, so a write-capable API key is
    out of scope here.
These are gaps to widen later, not silent downgrades; nothing above turns real
write access into a read-only or "unused" verdict.
"""
from __future__ import annotations

# ── per-engine normalisation ────────────────────────────────────────────────
# Each normaliser returns {login, admin, write, unresolved, scopes, flags}.
# `unresolved` means: this account has access we could not classify, so its
# write/admin standing is unknown and must not be read as read-only.

_MONGO_READ = {"read", "readanydatabase"}
_MONGO_WRITE = {"readwrite", "readwriteanydatabase"}
_MONGO_ADMIN = {"root", "dbowner", "dbadmin", "dbadminanydatabase", "useradmin",
                "useradminanydatabase", "clusteradmin", "clustermanager", "hostmanager",
                "clustermonitor", "backup", "restore", "__system"}

_SQL_WRITE = {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "TRUNCATE",
              "REFERENCES", "INDEX", "ALL", "ALL PRIVILEGES", "CREATE ROUTINE",
              "ALTER ROUTINE", "EVENT", "TRIGGER", "LOCK TABLES", "CREATE VIEW"}
_SQL_READ = {"SELECT", "SHOW VIEW", "USAGE", "CONNECT", "TEMPORARY", "TEMP"}
# Not read, not plainly write: PROXY lets you become another account, EXECUTE can
# run a definer-rights routine that writes. Neither is safe to call read-only.
_SQL_ESCALATE = {"PROXY", "EXECUTE"}

# Redis command categories/commands we treat as confidently read-only.
_REDIS_READ_CATS = {"+@read", "+@keyspace", "+@connection", "+@scripting"}
_REDIS_WRITE_CMDS = {"set", "del", "lpush", "rpush", "sadd", "zadd", "hset", "hdel",
                     "incr", "decr", "append", "setex", "getset", "mset", "expire",
                     "lpop", "rpop", "spop", "rename", "flushdb", "flushall", "copy",
                     "setnx", "msetnx", "persist", "move", "restore", "setrange"}


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
    table_write = any(str(g.get("privilege", "")).upper() in _SQL_WRITE
                      and not _sys_table(g.get("db"), g.get("table"))
                      for g in (info.get("grants") or []))
    direct_write = superuser or bool(info.get("createdb")) or bool(info.get("createrole")) \
        or any(s["level"] == "write" for s in scopes) or table_write
    # Membership in another role carries that role's privileges, which aren't in
    # this user's own grants. We can see the memberships (member_of) but not what
    # they grant, so a member with no direct write is "unresolved", not read-only.
    member_of = [str(r) for r in (info.get("member_of") or []) if r]
    unresolved = bool(member_of) and not direct_write
    flags = []
    if superuser:
        flags.append("superuser")
    if member_of:
        flags.append("roles:" + ",".join(member_of[:4]))
    if str(info.get("valid_until", "")).lower() in ("never", "", "infinity"):
        flags.append("no_expiry")
    return {"login": bool(info.get("can_login")), "admin": superuser,
            "write": direct_write, "unresolved": unresolved, "scopes": scopes, "flags": flags}


def _parse_mysql_grant(stmt):
    """One SHOW GRANTS line -> {scope, privs, grant_option} or {role: name} for a
    role-membership line (GRANT `r` TO `u`), or None."""
    s = stmt.strip()
    up = s.upper()
    if not up.startswith("GRANT "):
        return None
    on = up.find(" ON ")
    to = up.rfind(" TO ")
    if on < 0 or to < 0 or on > to:
        # No "ON <scope>": this is a role-membership grant (GRANT `role` TO `user`).
        if to > 0:
            return {"role": s[6:to].strip()}
        return None
    # Strip any (column, list) qualifiers before splitting privileges on commas.
    priv_str = _strip_parens(s[6:on])
    privs = {p.strip().upper() for p in priv_str.split(",") if p.strip()}
    scope = s[on + 4:to].strip().strip("`").replace("`", "").replace(".*", "").replace("*.*", "*")
    return {"scope": scope or "*", "privs": privs, "grant_option": "WITH GRANT OPTION" in up}


def _strip_parens(s):
    out, depth = [], 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _normalize_mysql(info):
    scopes, admin, write, roles, escalate = [], False, False, [], False
    for stmt in info.get("grant_statements") or []:
        g = _parse_mysql_grant(stmt)
        if not g:
            continue
        if g.get("role"):
            roles.append(g["role"])   # membership; its privileges live in the role
            continue
        privs = g["privs"]
        w = bool(privs & _SQL_WRITE)
        if not w and (privs & _SQL_ESCALATE):
            escalate = True           # PROXY/EXECUTE: can act as / run definer routines
            scopes.append({"scope": g["scope"], "level": "?"})
            continue
        if privs <= _SQL_READ:
            if privs - {"USAGE"}:
                scopes.append({"scope": g["scope"], "level": "read"})
            continue
        global_all = g["scope"] == "*" and ("ALL PRIVILEGES" in privs or "ALL" in privs)
        if global_all or (g["scope"] == "*" and g["grant_option"]):
            admin = True
        write = write or w
        level = "admin" if (global_all or (g["grant_option"] and g["scope"] != "*")) else ("write" if w else "read")
        scopes.append({"scope": g["scope"], "level": level})
    unresolved = (bool(roles) or escalate) and not (write or admin)
    flags = (["superuser"] if admin else []) + (["locked"] if info.get("locked") else [])
    if roles:
        flags.append("roles:" + ",".join(roles[:4]))
    return {"login": bool(info.get("can_login")) and not info.get("locked"),
            "admin": admin, "write": write or admin, "unresolved": unresolved,
            "scopes": scopes, "flags": flags}


def _normalize_mongo(info):
    scopes, admin, write, unknown = [], False, False, []
    for r in info.get("roles") or []:
        role = str(r.get("role", "")).lower()
        db = r.get("db", "?")
        if role in _MONGO_ADMIN:
            admin = True
            level = "admin"
        elif role in _MONGO_WRITE:
            write = True
            level = "write"
        elif role in _MONGO_READ:
            level = "read"
        else:
            unknown.append(role)     # custom role: privileges unknown
            level = "?"
        scopes.append({"scope": db, "level": level})
    unresolved = bool(unknown) and not (write or admin)
    flags = (["superuser"] if admin else [])
    if unknown:
        flags.append("roles:" + ",".join(sorted(set(unknown))[:4]))
    return {"login": True, "admin": admin, "write": write or admin,
            "unresolved": unresolved, "scopes": scopes, "flags": flags}


def _normalize_redis(info):
    cmds = str(info.get("commands", "")).lower()
    keys = str(info.get("keys", ""))
    tokens = cmds.split()
    full = "+@all" in tokens or "allcommands" in cmds
    admin = full or "+@admin" in tokens or "+@dangerous" in tokens or bool(info.get("reserved"))
    write = full or "+@write" in tokens or any(f"+{c}" in tokens for c in _REDIS_WRITE_CMDS)
    # Confidently read-only means: only read-ish categories, nothing else granted.
    grants = [t for t in tokens if t.startswith("+")]
    read_only = bool(grants) and all(t in _REDIS_READ_CATS for t in grants)
    unresolved = not (admin or write or read_only or not grants)
    level = "admin" if admin else ("write" if write else ("read" if read_only else "?"))
    flags = []
    if full and (keys == "~*" or "allkeys" in keys):
        flags.append("full_access")
    return {"login": bool(info.get("enabled", True)), "admin": admin, "write": write or admin,
            "unresolved": unresolved, "scopes": [{"scope": keys or "keys", "level": level}], "flags": flags}


def _normalize_es(info):
    roles = [str(r).lower() for r in (info.get("roles") or [])]
    known_read = {"viewer", "kibana_user", "monitoring_user", "reporting_user"}
    admin = "superuser" in roles
    write = admin or "editor" in roles
    unknown = [r for r in roles if r not in known_read and r not in ("superuser", "editor")]
    unresolved = bool(unknown) and not (write or admin)
    level = "admin" if admin else ("write" if write else ("read" if roles and not unknown else "?"))
    scopes = [{"scope": r, "level": ("admin" if r == "superuser" else "write" if r == "editor"
                                     else "read" if r in known_read else "?")} for r in roles]
    flags = (["superuser"] if admin else [])
    if unknown:
        flags.append("roles:" + ",".join(unknown[:4]))
    return {"login": bool(info.get("enabled", True)), "admin": admin, "write": write,
            "unresolved": unresolved, "scopes": scopes or [{"scope": "cluster", "level": level}], "flags": flags}


_NORMALIZERS = {
    "postgresql": _normalize_pg,
    "mysql": _normalize_mysql,
    "documentdb": _normalize_mongo,
    "redis": _normalize_redis,
    "elasticsearch": _normalize_es,
}


def _roles_note(flags):
    for f in flags:
        if f.startswith("roles:"):
            return "access via " + f[len("roles:"):] + " (not expanded)"
    return "access we couldn't classify"


def _summary(n):
    """A short readable line for a normalized user."""
    if n["admin"]:
        return "full admin"
    parts = [f"{s['level']} on {s['scope']}" for s in n["scopes"][:6] if s["level"] != "?"]
    extra = max(0, len([s for s in n["scopes"] if s["level"] != "?"]) - 6)
    line = ", ".join(parts) + (f", +{extra} more" if extra else "")
    if n.get("unresolved"):
        note = _roles_note(n["flags"])
        return f"{line}; {note}" if line else note
    if not parts:
        return "no access" if not n["login"] else "login only, no grants"
    return line


def normalize_user(info, fam):
    """user_info dict + engine family -> the shared shape the reports read."""
    fn = _NORMALIZERS.get(fam)
    base = fn(info) if fn else {"login": True, "admin": False, "write": False,
                                "unresolved": False, "scopes": [], "flags": []}
    base.setdefault("unresolved", False)
    row = {"user": info.get("user", "?"), **base}
    # "no access" only when we're sure: no scopes, not admin, and nothing unresolved.
    if not row["scopes"] and not row["admin"] and not row["unresolved"] and "no_access" not in row["flags"]:
        row["flags"] = row["flags"] + ["no_access"]
    row["summary"] = _summary(row)
    return row


# ── report assembly ─────────────────────────────────────────────────────────

def _rank(rows):
    return sorted(rows, key=lambda r: (not r["admin"], not r["write"], not r.get("unresolved"), r["user"]))


def build_report(kind, infos, fam, is_prod=False, skipped=0):
    """Assemble one report from a list of user_info dicts. `skipped` is how many
    accounts we couldn't inspect, surfaced so the reader knows the picture is
    partial. Returns a structure the UI renders; `note` narration is the caller's."""
    rows = [normalize_user(i, fam) for i in infos if isinstance(i, dict) and "error" not in i]

    if kind == "write_access":
        # Include the unresolved: they MIGHT write, and a "who can touch prod"
        # report must not hide a maybe-writer. They're flagged, not asserted.
        writers = _rank([r for r in rows if r["write"] or r.get("unresolved")])
        return {"kind": "write_access",
                "title": "Who can change data on prod" if is_prod else "Who can change data",
                "is_prod": is_prod, "count": len(writers), "total": len(rows),
                "skipped": skipped, "users": writers}

    if kind == "posture":
        findings = []
        for r in rows:
            if r["admin"]:
                findings.append({"severity": "high", "user": r["user"], "issue": "Full admin", "detail": r["summary"]})
            if "full_access" in r["flags"]:
                findings.append({"severity": "high", "user": r["user"],
                                 "issue": "Unrestricted access", "detail": "all commands on all keys"})
            if r.get("unresolved"):
                findings.append({"severity": "med", "user": r["user"],
                                 "issue": "Access not fully resolved", "detail": r["summary"]})
            if not r["login"]:
                findings.append({"severity": "low", "user": r["user"],
                                 "issue": "Cannot log in", "detail": "disabled or locked account"})
            elif "no_access" in r["flags"]:
                findings.append({"severity": "low", "user": r["user"],
                                 "issue": "Can log in but has no grants", "detail": "unused account?"})
        order = {"high": 0, "med": 1, "low": 2}
        findings.sort(key=lambda f: (order.get(f["severity"], 3), f["user"]))
        counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("high", "med", "low")}
        return {"kind": "posture", "title": "Security posture" + (" (prod)" if is_prod else ""),
                "is_prod": is_prod, "counts": counts, "total": len(rows), "skipped": skipped,
                "findings": findings}

    return {"kind": "access", "title": "Access overview" + (" (prod)" if is_prod else ""),
            "is_prod": is_prod, "total": len(rows), "skipped": skipped, "users": _rank(rows)}
