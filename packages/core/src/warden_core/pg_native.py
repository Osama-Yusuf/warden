"""Native PostgreSQL access via pooled psycopg connections.

Drop-in replacement for the psql subprocess in pg_query / pg_exec. Each call
would otherwise spawn psql and pay a fresh connection handshake (~100ms-2s to a
remote Aurora over TLS); a pooled connection reuses the socket, so repeated
operations (navigation, background refresh) are fast. Output is formatted to
match `psql -t -A -F '\\t'` byte-for-byte so every caller is unchanged.

A ConnectionPool per (host, port, db, user) gives real concurrency: each query
checks out its own connection, so overlapping requests never share a socket
(the failure mode that made the warm mongosh session desync).

The free-form SQL console stays on psql (pg_csv) — it renders CSV with headers
and runs arbitrary statements a structured path shouldn't.
"""

import datetime
import decimal
import threading
import uuid

try:
    import psycopg
    from psycopg import sql as _sql
    from psycopg_pool import ConnectionPool
    HAVE_PSYCOPG = True
except ImportError:  # pragma: no cover
    HAVE_PSYCOPG = False


_pools = {}
_pools_lock = threading.Lock()


def available():
    return HAVE_PSYCOPG


def _key(config, db, user, pwd):
    return (config["host"], int(config["port"]), db, user, pwd)


def _conninfo(config, db, user, pwd):
    kwargs = dict(
        host=config["host"], port=int(config["port"]), dbname=db,
        user=user, password=pwd, connect_timeout=8,
        application_name="warden",
    )
    # Only pin sslmode if the config asks for it; otherwise let libpq decide
    # (its default "prefer" matches what the psql subprocess used).
    if config.get("sslmode"):
        kwargs["sslmode"] = config["sslmode"]
    elif config.get("tls"):
        kwargs["sslmode"] = "require"
    return psycopg.conninfo.make_conninfo(**kwargs)


def _get_pool(config, db, user, pwd):
    """One small ConnectionPool per connection target, reused across requests.

    A ConnectionPool retries failed connections as if they were transient, so a
    wrong password would hang for the whole acquire timeout. Pre-flight with one
    direct connect: bad credentials raise here immediately (and no dead pool is
    cached), while good ones fall through to the pool that serves every later
    query fast."""
    key = _key(config, db, user, pwd)
    with _pools_lock:
        pool = _pools.get(key)
        if pool is not None:
            return pool
    conninfo = _conninfo(config, db, user, pwd)
    psycopg.connect(conninfo, connect_timeout=8).close()  # raises fast on auth failure
    pool = ConnectionPool(conninfo, min_size=1, max_size=6,
                          timeout=10, max_idle=300, name=f"warden-{db}",
                          open=True)
    with _pools_lock:
        existing = _pools.get(key)
        if existing is not None:
            pool.close()  # lost the race; keep the first pool
            return existing
        _pools[key] = pool
        return pool


def close_all():
    with _pools_lock:
        for p in _pools.values():
            try:
                p.close()
            except Exception:
                pass
        _pools.clear()


def _fmt(v):
    """Render one value the way psql -A -t does: NULL empty, bool as t/f."""
    if v is None:
        return ""
    if v is True:
        return "t"
    if v is False:
        return "f"
    return str(v)


def query(config, admin_user, admin_pass, sql, db=None, timeout=30):
    """SELECT -> (returncode, stdout, stderr). stdout is tab-separated rows,
    matching `psql -t -A -F '\\t'`. Mirrors pg_query's contract exactly."""
    dbname = db or config.get("default_db", "postgres")
    try:
        pool = _get_pool(config, dbname, admin_user, admin_pass)
        with pool.connection(timeout=timeout) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(sql)
                if cur.description is None:
                    return 0, "", ""
                rows = cur.fetchall()
        out = "".join("\t".join(_fmt(c) for c in row) + "\n" for row in rows)
        return 0, out, ""
    except psycopg.Error as e:
        return 1, "", str(e).strip()
    except Exception as e:
        return 1, "", str(e).strip()


def exec_(config, admin_user, admin_pass, sql, db=None, timeout=30):
    """DDL/DML write -> (ok, stdout, stderr). Mirrors pg_exec's contract."""
    dbname = db or config.get("default_db", "postgres")
    try:
        pool = _get_pool(config, dbname, admin_user, admin_pass)
        with pool.connection(timeout=timeout) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(sql)
        return True, "", ""
    except psycopg.Error as e:
        return False, "", str(e).strip()
    except Exception as e:
        return False, "", str(e).strip()


def ping(config, admin_user, admin_pass, db=None):
    """Cheap connectivity/auth check -> (ok, error)."""
    code, _out, err = query(config, admin_user, admin_pass, "SELECT 1", db=db, timeout=8)
    return code == 0, (None if code == 0 else err)


def _cell(v):
    """JSON-safe value for the data grid, keeping type identity where it helps."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (dict, list)):  # jsonb/json come back parsed
        return v
    return str(v)


def select_page(config, admin_user, admin_pass, db, schema, table,
                limit=50, offset=0):
    """A page of rows for the data browser. Returns (result, error) where
    result = {columns, rows, total}. Identifiers are quoted with psycopg.sql so
    they can't inject."""
    dbname = db or config.get("default_db", "postgres")
    rel = _sql.SQL("{}.{}").format(_sql.Identifier(schema), _sql.Identifier(table))
    try:
        pool = _get_pool(config, dbname, admin_user, admin_pass)
        with pool.connection(timeout=30) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    _sql.SQL("SELECT * FROM {} LIMIT %s OFFSET %s").format(rel),
                    (max(1, int(limit)), max(0, int(offset))))
                columns = [d.name for d in cur.description] if cur.description else []
                rows = [[_cell(v) for v in r] for r in cur.fetchall()]
                total = None
                try:
                    cur.execute(_sql.SQL("SELECT count(*) FROM {}").format(rel))
                    total = int(cur.fetchone()[0])
                except psycopg.Error:
                    total = None
        return {"columns": columns, "rows": rows, "total": total}, None
    except psycopg.Error as e:
        return None, str(e).strip()
    except Exception as e:
        return None, str(e).strip()


def login_probe(config, user, pwd, test_db=""):
    """Connect AS the given user with a short-lived, non-pooled connection (test
    creds shouldn't spin up a cached pool), and report auth + read checks.
    Returns a dict shaped like the server's test-login payload."""
    default_db = config.get("default_db", "postgres")
    conn = None
    try:
        conn = psycopg.connect(_conninfo(config, default_db, user, pwd), connect_timeout=8)
        conn.autocommit = True
        checks = []
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            identity = cur.fetchone()[0]
            cur.execute("SELECT datname FROM pg_database WHERE datistemplate=false "
                        "AND has_database_privilege(datname, 'CONNECT') ORDER BY datname")
            dbs = [r[0] for r in cur.fetchall()]
            checks.append({"name": "Databases they can connect to", "ok": True,
                           "detail": ", ".join(dbs) if dbs else "none"})
        if test_db:
            try:
                probe = psycopg.connect(_conninfo(config, test_db, user, pwd), connect_timeout=8)
                probe.close()
                checks.append({"name": f"Connect to '{test_db}'", "ok": True, "detail": "connected"})
            except psycopg.Error as e:
                checks.append({"name": f"Connect to '{test_db}'", "ok": False,
                               "detail": str(e).strip() or "denied"})
        return {"auth": True, "identity": identity, "checks": checks}
    except psycopg.Error as e:
        return {"auth": False, "error": str(e).strip() or "Authentication failed"}
    except Exception as e:
        return {"auth": False, "error": str(e).strip() or "Authentication failed"}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
