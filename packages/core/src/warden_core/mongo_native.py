"""Native DocumentDB/MongoDB access via pymongo.

Replaces the mongosh subprocess for structured operations. MongoClient keeps a
thread-safe connection pool, so this is fast (no per-call spawn) AND safe under
concurrency (no REPL text-scraping to desync). Each function returns
(data, error) with data already shaped like the old mongosh path and made
JSON-safe, so the server handlers and UI are unchanged.

The free-form query console stays on mongosh (it evaluates arbitrary shell JS
that a driver can't run).
"""

import re
import threading
import uuid

try:
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError
    from bson import ObjectId
    HAVE_PYMONGO = True
except ImportError:  # pragma: no cover
    HAVE_PYMONGO = False


_clients = {}
_clients_lock = threading.Lock()


def available():
    return HAVE_PYMONGO


def _key(cfg, user, pwd):
    return (cfg["host"], int(cfg["port"]), cfg.get("auth_db", "admin"),
            bool(cfg.get("tls")), user, pwd)


def get_client(cfg, user, pwd):
    """One pooled MongoClient per connection, reused across all requests."""
    key = _key(cfg, user, pwd)
    with _clients_lock:
        client = _clients.get(key)
        if client is not None:
            return client
        kwargs = dict(
            host=cfg["host"], port=int(cfg["port"]),
            username=user, password=pwd,
            authSource=cfg.get("auth_db", "admin"),
            serverSelectionTimeoutMS=8000, connectTimeoutMS=8000,
            socketTimeoutMS=60000, retryWrites=False,
            appname="warden",
        )
        if cfg.get("tls"):
            kwargs["tls"] = True
            if cfg.get("tls_insecure"):
                kwargs["tlsAllowInvalidCertificates"] = True   # opt out: self-signed / private CA
        client = MongoClient(**kwargs)
        _clients[key] = client
        return client


def close_all():
    with _clients_lock:
        for c in _clients.values():
            try:
                c.close()
            except Exception:
                pass
        _clients.clear()


def _jsonsafe(value):
    """Convert BSON/driver types into plain JSON-serializable Python values."""
    if isinstance(value, dict):
        return {str(k): _jsonsafe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonsafe(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, str)):
        return value
    # UUID / Binary / raw bytes (e.g. a user's userId) -> readable hex.
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, uuid.UUID):
        return str(value)
    # ObjectId, datetime, Decimal128, Int64, etc.
    return str(value)


def _run(fn):
    """Call a pymongo operation, mapping driver errors to (None, message)."""
    try:
        return fn(), None
    except PyMongoError as e:
        return None, str(e)
    except Exception as e:  # keep the server's 500 handler out of the picture
        return None, str(e)


# ---------------------------------------------------------------------------
# Reads (shapes match the old mongosh path exactly)
# ---------------------------------------------------------------------------

def ping(cfg, user, pwd):
    """Returns (whoami, error). whoami is the authenticated username."""
    def go():
        c = get_client(cfg, user, pwd)
        info = c.admin.command("connectionStatus")
        authed = (info.get("authInfo") or {}).get("authenticatedUsers") or []
        return authed[0].get("user") if authed else user
    return _run(go)


def users_info(cfg, user, pwd):
    """All users as the raw usersInfo list (list of dicts)."""
    def go():
        c = get_client(cfg, user, pwd)
        return _jsonsafe(c.admin.command("usersInfo", 1).get("users", []))
    return _run(go)


def user_info(cfg, user, pwd, username):
    """One user's info dict, or (None, 'User not found')."""
    def go():
        c = get_client(cfg, user, pwd)
        users = c.admin.command("usersInfo", {"user": username, "db": "admin"}).get("users", [])
        if not users:
            # usersInfo scoped to admin didn't find it; try the plain name form
            users = c.admin.command("usersInfo", username).get("users", [])
        if not users:
            raise LookupError("User not found")
        return _jsonsafe(users[0])
    data, err = _run(go)
    if isinstance(err, str) and "User not found" in err:
        return None, "User not found"
    return data, err


def list_databases(cfg, user, pwd):
    """[{name, sizeOnDisk, empty}] with sizeOnDisk as a plain int."""
    def go():
        c = get_client(cfg, user, pwd)
        dbs = c.admin.command("listDatabases").get("databases", [])
        return [{"name": d["name"], "sizeOnDisk": int(d.get("sizeOnDisk", 0) or 0),
                 "empty": bool(d.get("empty", False))} for d in dbs]
    return _run(go)


def collection_names(cfg, user, pwd, database):
    def go():
        c = get_client(cfg, user, pwd)
        return sorted(c[database].list_collection_names())
    return _run(go)


def list_collections(cfg, user, pwd, database):
    """Collection names plus on-disk size (data + indexes), like the SQL table
    list. collStats is metadata-cheap per collection; sizes are skipped when a
    database has an unusually large number of collections to keep the list snappy."""
    def go():
        c = get_client(cfg, user, pwd)
        db = c[database]
        names = sorted(db.list_collection_names())
        with_size = len(names) <= 250
        out = []
        for name in names:
            size = None
            if with_size:
                try:
                    st = db.command("collStats", name)
                    size = st.get("totalSize")
                    if size is None:
                        size = int(st.get("storageSize", 0) or 0) + int(st.get("totalIndexSize", 0) or 0)
                    size = int(size)
                except PyMongoError:
                    size = None
            out.append({"name": name, "size_bytes": size})
        return out
    return _run(go)


def id_repr(value):
    """A JSON-encodable, type-preserving handle for an _id so the browser can
    send it back and we target the exact document (ObjectId vs string vs int)."""
    if isinstance(value, ObjectId):
        return {"$oid": str(value)}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {"$oid": str(value)}


def id_from_repr(rep):
    """Rebuild a real _id from the handle id_repr() produced."""
    if isinstance(rep, dict) and "$oid" in rep:
        return ObjectId(rep["$oid"])
    return rep


def _search_filter(coll, term):
    """A best-effort 'any field contains term' filter (case-insensitive regex
    across sampled field names, plus an exact _id match when term is an oid).
    This is a collection scan — bounded by the page limit — like SQL ILIKE."""
    fields, seen = [], set()
    for d in coll.find({}).limit(40):
        for k in d.keys():
            if k not in seen:
                seen.add(k)
                fields.append(k)
    rx = {"$regex": re.escape(term), "$options": "i"}
    ors = [{f: rx} for f in fields]
    try:
        ors.append({"_id": ObjectId(term)})
    except Exception:
        pass
    return {"$or": ors} if ors else {}


def find_page(cfg, user, pwd, database, collection, limit=50, skip=0,
              sort_field=None, sort_dir=1, search=None):
    """A page of documents for the data browser. Returns (result, error) where
    result = {columns, rows, ids, total, filtered}. Columns are the union of
    top-level keys across the page (_id first); `ids` is a parallel array of
    type-preserving _id handles for row-level CRUD. A search never counts (it
    would scan), so total is None then."""
    def go():
        c = get_client(cfg, user, pwd)
        coll = c[database][collection]
        term = (search or "").strip()
        filt = _search_filter(coll, term) if term else {}
        cursor = coll.find(filt)
        if sort_field:
            cursor = cursor.sort(sort_field, -1 if int(sort_dir) < 0 else 1)
        cursor = cursor.skip(max(0, int(skip))).limit(max(1, int(limit)))
        raw = list(cursor)
        ids = [id_repr(d.get("_id")) for d in raw]
        docs = [_jsonsafe(d) for d in raw]
        columns, seen = [], set()
        for d in docs:
            if isinstance(d, dict):
                for k in d.keys():
                    if k not in seen:
                        seen.add(k)
                        columns.append(k)
        if "_id" in seen:
            columns = ["_id"] + [k for k in columns if k != "_id"]
        rows = [[d.get(k) if isinstance(d, dict) else None for k in columns] for d in docs]
        total = None
        if not term:
            try:
                total = int(coll.estimated_document_count())
            except PyMongoError:
                total = None
        return {"columns": columns, "rows": rows, "ids": ids,
                "total": total, "filtered": bool(term)}
    return _run(go)


def collection_stats(cfg, user, pwd, database, collection):
    """Header stats for the data browser: document count, data + storage size,
    index count, average document size. Returns (stats, error)."""
    def go():
        c = get_client(cfg, user, pwd)
        st = c[database].command("collStats", collection)
        return {"rows": int(st.get("count", 0) or 0),
                "size_bytes": int(st.get("size", 0) or 0),
                "storage_bytes": int(st.get("storageSize", 0) or 0),
                "indexes": int(st.get("nindexes", 0) or 0),
                "avg_obj": int(st.get("avgObjSize", 0) or 0)}
    return _run(go)


def collection_meta(cfg, user, pwd, database, collection):
    """What a MongoDB editor needs: the id field is always _id, docs are edited
    as JSON. Returns (meta, error)."""
    def go():
        get_client(cfg, user, pwd)  # validate connectivity/auth
        return {"id_field": "_id", "editable": True, "json_edit": True}
    return _run(go)


def insert_document(cfg, user, pwd, database, collection, doc):
    """insertOne. Returns ({inserted_id}, error)."""
    def go():
        c = get_client(cfg, user, pwd)
        res = c[database][collection].insert_one(dict(doc))
        return {"inserted_id": _jsonsafe(res.inserted_id)}
    return _run(go)


def update_document(cfg, user, pwd, database, collection, rep, set_fields, unset_fields):
    """updateOne targeting exactly one _id, applying only changed fields
    ($set) and removed fields ($unset) so untouched fields keep their original
    BSON types. Returns ({matched, modified}, error)."""
    def go():
        c = get_client(cfg, user, pwd)
        _id = id_from_repr(rep)
        update = {}
        clean_set = {k: v for k, v in (set_fields or {}).items() if k != "_id"}
        if clean_set:
            update["$set"] = clean_set
        if unset_fields:
            update["$unset"] = {k: "" for k in unset_fields if k != "_id"}
        if not update:
            return {"matched": 1, "modified": 0}
        res = c[database][collection].update_one({"_id": _id}, update)
        if res.matched_count != 1:
            raise LookupError(f"Expected to match 1 document, matched {res.matched_count}")
        return {"matched": res.matched_count, "modified": res.modified_count}
    return _run(go)


def delete_document(cfg, user, pwd, database, collection, rep):
    """deleteOne targeting exactly one _id. Returns ({deleted}, error)."""
    def go():
        c = get_client(cfg, user, pwd)
        _id = id_from_repr(rep)
        res = c[database][collection].delete_one({"_id": _id})
        if res.deleted_count != 1:
            raise LookupError(f"Expected to delete 1 document, deleted {res.deleted_count}")
        return {"deleted": res.deleted_count}
    return _run(go)


def server_health(cfg, user, pwd):
    """serverStatus + currentOp, shaped like the mongosh health payload."""
    def go():
        c = get_client(cfg, user, pwd)
        s = c.admin.command("serverStatus")
        conns = s.get("connections") or {}
        mem = s.get("mem") or {}
        out = {
            "version": str(s.get("version", "")),
            "uptime": int(s.get("uptime", 0) or 0),
            "connections": {"current": int(conns.get("current", 0) or 0),
                            "available": int(conns.get("available", 0) or 0)},
            "mem": {"resident": int(mem.get("resident", 0) or 0)} if mem else None,
        }
        try:
            cur = c.admin.command("currentOp", 1, active=True)
            prog = cur.get("inprog") or []
            out["active_ops"] = len(prog)
            slow = []
            for o in prog:
                secs = o.get("secs_running") or 0
                if secs and secs >= 5:
                    slow.append({"opid": str(o.get("opid", "")), "secs": int(secs),
                                 "ns": str(o.get("ns", "")), "op": str(o.get("op", ""))})
            out["slow_ops"] = slow[:10]
        except PyMongoError:
            out["active_ops"] = None
            out["slow_ops"] = []
        return out
    return _run(go)


def login_probe(cfg, user, pwd, test_db=""):
    """Connect AS the given user with a short-lived client (never pooled — test
    creds shouldn't linger), and report auth + read checks. Returns a dict shaped
    exactly like the server's test-login payload, or ({auth: False, error}) on
    an auth failure."""
    kwargs = dict(
        host=cfg["host"], port=int(cfg["port"]),
        username=user, password=pwd,
        authSource=cfg.get("auth_db", "admin"),
        serverSelectionTimeoutMS=6000, connectTimeoutMS=6000,
        socketTimeoutMS=15000, retryWrites=False, appname="warden-test",
    )
    if cfg.get("tls"):
        kwargs["tls"] = True
        kwargs["tlsAllowInvalidCertificates"] = True
    client = None
    try:
        client = MongoClient(**kwargs)
        try:
            info = client.admin.command("connectionStatus")
        except PyMongoError as e:
            return {"auth": False, "error": str(e) or "Authentication failed"}
        roles = (info.get("authInfo") or {}).get("authenticatedUserRoles") or []
        role_strs = [f"{r.get('role')}@{r.get('db', '*')}" for r in roles]
        checks = []
        try:
            dbs = client.admin.command("listDatabases").get("databases", [])
            names = [d.get("name") for d in dbs]
            checks.append({"name": "List all databases", "ok": True,
                           "detail": ", ".join(n for n in names if n) or "none"})
        except PyMongoError as e:
            checks.append({"name": "List all databases", "ok": False,
                           "detail": str(e) or "not permitted"})
        if test_db:
            try:
                cols = client[test_db].list_collection_names()
                checks.append({"name": f"Read '{test_db}'", "ok": True,
                               "detail": f"{len(cols)} collection(s) visible"})
            except PyMongoError as e:
                checks.append({"name": f"Read '{test_db}'", "ok": False,
                               "detail": str(e) or "denied"})
        return {"auth": True, "identity": user, "roles": role_strs, "checks": checks}
    except Exception as e:
        return {"auth": False, "error": str(e) or "Authentication failed"}
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Writes (return (ok, error))
# ---------------------------------------------------------------------------

def create_user(cfg, user, pwd, username, password, roles):
    def go():
        c = get_client(cfg, user, pwd)
        c.admin.command("createUser", username, pwd=password, roles=roles)
        return True
    ok, err = _run(go)
    return bool(ok), err


def update_password(cfg, user, pwd, username, password):
    def go():
        c = get_client(cfg, user, pwd)
        c.admin.command("updateUser", username, pwd=password)
        return True
    ok, err = _run(go)
    return bool(ok), err


def grant_roles(cfg, user, pwd, username, roles):
    def go():
        c = get_client(cfg, user, pwd)
        c.admin.command("grantRolesToUser", username, roles=roles)
        return True
    ok, err = _run(go)
    return bool(ok), err


def revoke_roles(cfg, user, pwd, username, roles):
    def go():
        c = get_client(cfg, user, pwd)
        c.admin.command("revokeRolesFromUser", username, roles=roles)
        return True
    ok, err = _run(go)
    return bool(ok), err


def drop_user(cfg, user, pwd, username):
    def go():
        c = get_client(cfg, user, pwd)
        c.admin.command("dropUser", username)
        return True
    ok, err = _run(go)
    return bool(ok), err
