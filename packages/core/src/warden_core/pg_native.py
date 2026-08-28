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
        # verify the server cert + hostname by default; tls_insecure downgrades to
        # encrypt-only "require" (for self-signed certs or private CAs like AWS RDS)
        kwargs["sslmode"] = "require" if config.get("tls_insecure") else "verify-full"
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


# Below this many rows we count exactly; above it we trust the planner's
# estimate so opening a huge table never triggers a full-table count scan.
_EXACT_COUNT_CEILING = 50000


def _pg_total(cur, conn, rel, schema, table):
    """(total, is_estimate). Fast planner estimate for big tables, exact for
    small ones. Never scans a million-row table just to show a count."""
    est = -1
    try:
        cur.execute("SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass(%s)",
                    (_sql.Identifier(schema, table).as_string(conn),))
        row = cur.fetchone()
        if row and row[0] is not None:
            est = int(row[0])
    except psycopg.Error:
        est = -1
    if est < 0 or est <= _EXACT_COUNT_CEILING:
        try:
            cur.execute(_sql.SQL("SELECT count(*) FROM {}").format(rel))
            return int(cur.fetchone()[0]), False
        except psycopg.Error:
            return (est if est >= 0 else None), est >= 0
    return est, True


def _search_where(columns, term):
    """A parameterized 'any text column contains term' filter. Returns
    (sql_fragment, params) or (None, []) if nothing to search."""
    if not term or not columns:
        return None, []
    like = f"%{term}%"
    clauses = [_sql.SQL("CAST({} AS text) ILIKE %s").format(_sql.Identifier(c)) for c in columns]
    frag = _sql.SQL("(") + _sql.SQL(" OR ").join(clauses) + _sql.SQL(")")
    return frag, [like] * len(columns)


def select_page(config, admin_user, admin_pass, db, schema, table,
                limit=50, offset=0, search=None):
    """A page of rows for the data browser. Returns (result, error) where
    result = {columns, rows, total, estimated, filtered}. Identifiers are quoted
    with psycopg.sql and the search term is parameterized, so nothing injects.
    A search never runs a filtered count (that would scan); total is None then."""
    dbname = db or config.get("default_db", "postgres")
    rel = _sql.SQL("{}.{}").format(_sql.Identifier(schema), _sql.Identifier(table))
    term = (search or "").strip()
    try:
        pool = _get_pool(config, dbname, admin_user, admin_pass)
        with pool.connection(timeout=30) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                # column names first (a zero-row select is planned, not scanned)
                cur.execute(_sql.SQL("SELECT * FROM {} LIMIT 0").format(rel))
                columns = [d.name for d in cur.description] if cur.description else []
                where, wparams = _search_where(columns, term) if term else (None, [])
                q = _sql.SQL("SELECT * FROM {}").format(rel)
                if where is not None:
                    q = q + _sql.SQL(" WHERE ") + where
                q = q + _sql.SQL(" LIMIT %s OFFSET %s")
                cur.execute(q, wparams + [max(1, int(limit)), max(0, int(offset))])
                rows = [[_cell(v) for v in r] for r in cur.fetchall()]
                if term:
                    total, estimated = None, False   # no filtered count on purpose
                else:
                    total, estimated = _pg_total(cur, conn, rel, schema, table)
        return {"columns": columns, "rows": rows, "total": total,
                "estimated": estimated, "filtered": bool(term)}, None
    except psycopg.Error as e:
        return None, str(e).strip()
    except Exception as e:
        return None, str(e).strip()


def table_meta(config, admin_user, admin_pass, db, schema, table):
    """Metadata a safe editor needs: primary-key columns and per-column
    {name, type, nullable, default, is_pk, generated}. Returns (meta, error)."""
    dbname = db or config.get("default_db", "postgres")
    try:
        pool = _get_pool(config, dbname, admin_user, admin_pass)
        with pool.connection(timeout=15) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT a.attname
                    FROM pg_index i
                    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                    WHERE i.indrelid = to_regclass(%s) AND i.indisprimary
                    ORDER BY array_position(i.indkey, a.attnum)
                """, (_sql.Identifier(schema, table).as_string(conn),))
                pk = [r[0] for r in cur.fetchall()]
                cur.execute("""
                    SELECT column_name, data_type, is_nullable, column_default,
                           is_identity, is_generated
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                """, (schema, table))
                cols = []
                for name, dtype, nullable, default, identity, generated in cur.fetchall():
                    cols.append({
                        "name": name, "type": dtype,
                        "nullable": nullable == "YES",
                        "has_default": default is not None or identity == "YES",
                        "generated": identity == "YES" or generated == "ALWAYS",
                        "is_pk": name in pk,
                    })
        return {"primary_key": pk, "columns": cols, "editable": bool(pk)}, None
    except psycopg.Error as e:
        return None, str(e).strip()
    except Exception as e:
        return None, str(e).strip()


def object_stats(config, admin_user, admin_pass, db, schema, table):
    """Header stats for the data browser: rows (estimate on big tables), on-disk
    size, column count, index count. Returns (stats, error)."""
    dbname = db or config.get("default_db", "postgres")
    rel = _sql.SQL("{}.{}").format(_sql.Identifier(schema), _sql.Identifier(table))
    try:
        pool = _get_pool(config, dbname, admin_user, admin_pass)
        with pool.connection(timeout=15) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                regname = _sql.Identifier(schema, table).as_string(conn)
                total, estimated = _pg_total(cur, conn, rel, schema, table)
                cur.execute("SELECT pg_total_relation_size(to_regclass(%s))", (regname,))
                size = int((cur.fetchone() or [0])[0] or 0)
                cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
                            (schema, table))
                ncols = int(cur.fetchone()[0])
                cur.execute("SELECT count(*) FROM pg_index WHERE indrelid = to_regclass(%s)", (regname,))
                nidx = int(cur.fetchone()[0])
        return {"rows": total, "estimated": estimated, "size_bytes": size,
                "columns": ncols, "indexes": nidx}, None
    except psycopg.Error as e:
        return None, str(e).strip()
    except Exception as e:
        return None, str(e).strip()


def insert_row(config, admin_user, admin_pass, db, schema, table, values):
    """Parameterized INSERT of one row. `values` maps column -> value. Returns
    (inserted_row_dict_or_None, error). RETURNING * gives back the stored row."""
    dbname = db or config.get("default_db", "postgres")
    rel = _sql.SQL("{}.{}").format(_sql.Identifier(schema), _sql.Identifier(table))
    cols = list(values.keys())
    if not cols:
        return None, "No values to insert"
    collist = _sql.SQL(", ").join(_sql.Identifier(c) for c in cols)
    placeholders = _sql.SQL(", ").join(_sql.Placeholder() * len(cols))
    q = _sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(rel, collist, placeholders)
    try:
        pool = _get_pool(config, dbname, admin_user, admin_pass)
        with pool.connection(timeout=30) as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(q, [values[c] for c in cols])
                out_cols = [d.name for d in cur.description] if cur.description else []
                row = cur.fetchone()
            conn.commit()
        return (dict(zip(out_cols, [_cell(v) for v in row])) if row else {}), None
    except psycopg.Error as e:
        return None, str(e).strip()
    except Exception as e:
        return None, str(e).strip()


def update_row(config, admin_user, admin_pass, db, schema, table, pk, changes):
    """Parameterized UPDATE targeting exactly one row. `pk` maps the primary-key
    column(s) -> value(s); `changes` maps column -> new value. Runs in a
    transaction and refuses to commit unless exactly one row matched.
    Returns (updated_count, error)."""
    dbname = db or config.get("default_db", "postgres")
    rel = _sql.SQL("{}.{}").format(_sql.Identifier(schema), _sql.Identifier(table))
    if not pk:
        return 0, "Refusing to update without a primary key"
    if not changes:
        return 0, "No changes to apply"
    set_cols = list(changes.keys())
    set_frag = _sql.SQL(", ").join(
        _sql.SQL("{} = %s").format(_sql.Identifier(c)) for c in set_cols)
    pk_cols = list(pk.keys())
    where_frag = _sql.SQL(" AND ").join(
        _sql.SQL("{} = %s").format(_sql.Identifier(c)) for c in pk_cols)
    q = _sql.SQL("UPDATE {} SET {} WHERE {}").format(rel, set_frag, where_frag)
    params = [changes[c] for c in set_cols] + [pk[c] for c in pk_cols]
    try:
        pool = _get_pool(config, dbname, admin_user, admin_pass)
        with pool.connection(timeout=30) as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(q, params)
                n = cur.rowcount
                if n != 1:
                    conn.rollback()
                    return n, (f"Expected to update 1 row but matched {n}; rolled back")
            conn.commit()
        return 1, None
    except psycopg.Error as e:
        return 0, str(e).strip()
    except Exception as e:
        return 0, str(e).strip()


def delete_row(config, admin_user, admin_pass, db, schema, table, pk):
    """Parameterized DELETE targeting exactly one row by primary key. Runs in a
    transaction and refuses to commit unless exactly one row matched.
    Returns (deleted_count, error)."""
    dbname = db or config.get("default_db", "postgres")
    rel = _sql.SQL("{}.{}").format(_sql.Identifier(schema), _sql.Identifier(table))
    if not pk:
        return 0, "Refusing to delete without a primary key"
    pk_cols = list(pk.keys())
    where_frag = _sql.SQL(" AND ").join(
        _sql.SQL("{} = %s").format(_sql.Identifier(c)) for c in pk_cols)
    q = _sql.SQL("DELETE FROM {} WHERE {}").format(rel, where_frag)
    try:
        pool = _get_pool(config, dbname, admin_user, admin_pass)
        with pool.connection(timeout=30) as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(q, [pk[c] for c in pk_cols])
                n = cur.rowcount
                if n != 1:
                    conn.rollback()
                    return n, (f"Expected to delete 1 row but matched {n}; rolled back")
            conn.commit()
        return 1, None
    except psycopg.Error as e:
        return 0, str(e).strip()
    except Exception as e:
        return 0, str(e).strip()


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
