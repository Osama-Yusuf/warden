"""Input checks and the small per-engine quirks that the API handlers and the
engine adapters both lean on: identifier and privilege validation, MySQL account
quoting, the MariaDB-vs-MySQL flavour probe, escaped LIKE patterns, and a couple
of value coercions. Kept here so the adapters (in warden_core) don't have to
reach up into the web layer for them.
"""

import re

from warden_core.config import DOCDB_ROLES, PG_PRIVILEGES
from warden_core.mysql import my_query
from warden_core.util import engine_family, validate_ident

MYSQL_PRIVILEGES = [
    "SELECT", "INSERT", "UPDATE", "DELETE",
    "ALL PRIVILEGES", "CREATE", "DROP", "ALTER", "INDEX", "EXECUTE",
]

MYSQL_HOST_RE = re.compile(r'^[A-Za-z0-9_.\-%]+$')


def validate_docdb_roles(roles):
    if not isinstance(roles, list):
        raise ValueError("roles must be a list")
    for r in roles:
        if not isinstance(r, dict) or "role" not in r or "db" not in r:
            raise ValueError("Each role must have 'role' and 'db' fields")
        if r["role"] not in DOCDB_ROLES:
            raise ValueError(f"Unknown DocumentDB role: {r['role']}")
        validate_ident(r["db"], "role database")


def mysql_account(target):
    """'name' or 'name@host' quoted as 'name'@'host'. Host defaults to %."""
    name, _, host = str(target).strip().partition("@")
    name = validate_ident(name, "username")
    host = host or "%"
    if len(host) > 128 or not MYSQL_HOST_RE.match(host):
        raise ValueError("Invalid MySQL host part (letters, digits, _ . - %)")
    return f"'{name}'@'{host}'"


# MySQL keeps the account-lock flag in mysql.user.account_locked; MariaDB drops
# that column from the mysql.user view and stashes the flag in mysql.global_priv
# as JSON. Probe the flavour once per host so the right catalog query is used.
_MARIADB_CACHE = {}


def is_mariadb(cfg, user, pwd):
    key = (cfg.get("host"), cfg.get("port"))
    if key not in _MARIADB_CACHE:
        code, out, _ = my_query(cfg, user, pwd, "SELECT VERSION()")
        _MARIADB_CACHE[key] = (code == 0 and "mariadb" in out.lower())
    return _MARIADB_CACHE[key]


def validate_mysql_privilege(priv):
    upper = str(priv).strip().upper()
    if upper not in {p.upper() for p in MYSQL_PRIVILEGES}:
        raise ValueError(f"Unknown privilege: {priv}")
    return upper


def validate_pg_privilege(priv):
    upper = priv.strip().upper()
    if upper not in {p.upper() for p in PG_PRIVILEGES}:
        raise ValueError(f"Unknown privilege: {priv}")
    return upper


def validate_target(engine, username):
    """Validate a username for the engine. MySQL accounts may carry an @host."""
    if engine_family(engine) == "mysql":
        mysql_account(username)  # raises when malformed
        return str(username).strip()
    return validate_ident(username, "username")


def sql_like_pattern(term):
    """A quote-safe, wildcard-escaped LIKE literal for the subprocess engines
    (mysql/sqlite), used with ESCAPE '\\'. Search only, the value is escaped and
    never trusted as SQL."""
    body = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "'%" + body.replace("'", "''") + "%'"


def es_index(body):
    index = str(body.get("collection", "")).strip()
    if not index or "\x00" in index or "," in index or index.startswith("_"):
        raise ValueError("Invalid index name")
    return index


def redis_key(body, field="id"):
    key = body.get(field)
    if not isinstance(key, str) or not key or "\x00" in key or len(key) > 512:
        raise ValueError("Invalid key")
    return key


def clean_columns(mapping, label):
    """Validate the column names in a {column: value} map. Values are passed as
    query parameters (never interpolated), so only the identifiers need checks."""
    out = {}
    for k, v in (mapping or {}).items():
        out[validate_ident(str(k), label)] = v
    return out


def acl_rule_ok(rule):
    r = str(rule or "").strip()
    return bool(r) and len(r) <= 256 and " " not in r and "\x00" not in r


def docdb_num(v):
    """DocumentDB counters come back as BSON Longs that JSON.stringify turns into
    objects ({$numberLong}, {low,high}, {$numberDecimal}). Coerce them to int."""
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
