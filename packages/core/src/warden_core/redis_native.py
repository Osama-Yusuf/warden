"""Native Redis / ElastiCache / Valkey access via redis-py (pooled).

Redis has no tables/rows: data is keys with typed values (string/hash/list/set/
zset/stream) in numbered logical DBs (0..N). So a numbered DB maps to a
"database", a key namespace (prefix before ':') to a "collection", and each key
to a "row" of {key, type, ttl, memory, value}. SCAN is used everywhere so a big
keyspace is never blocked. The free-form console runs raw Redis commands.
"""

import json as _json
import shlex
import threading

try:
    import redis as _redis
    from redis.exceptions import RedisError
    HAVE_REDIS = True
except ImportError:  # pragma: no cover
    HAVE_REDIS = False


_clients = {}
_clients_lock = threading.Lock()
_VALUE_CAP = 2000
_ITEM_CAP = 200


def available():
    return HAVE_REDIS


def _client(cfg, user, pwd, db=0):
    key = (cfg["host"], int(cfg["port"]), bool(cfg.get("tls")), user, pwd, int(db))
    with _clients_lock:
        c = _clients.get(key)
        if c is not None:
            return c
        kwargs = dict(host=cfg["host"], port=int(cfg["port"]), db=int(db),
                      socket_connect_timeout=6, socket_timeout=30,
                      decode_responses=True, max_connections=8)
        if pwd:
            kwargs["password"] = pwd
        if user:
            kwargs["username"] = user
        if cfg.get("tls"):
            kwargs["ssl"] = True
            kwargs["ssl_cert_reqs"] = None
        c = _redis.Redis(**kwargs)
        _clients[key] = c
        return c


def _run(fn):
    try:
        return fn(), None
    except RedisError as e:
        return None, str(e)
    except Exception as e:
        return None, str(e)


def ping(cfg, user, pwd):
    def go():
        c = _client(cfg, user, pwd, 0)
        c.ping()
        info = c.info("server")
        return f"redis {info.get('redis_version', '')} @ {cfg['host']}"
    return _run(go)


def list_databases(cfg, user, pwd):
    """Numbered logical DBs with key counts. Stops early on cluster/restricted
    setups (ElastiCache cluster mode) where only db0 exists."""
    def go():
        c0 = _client(cfg, user, pwd, 0)
        try:
            n = int((c0.config_get("databases") or {}).get("databases", 16))
        except Exception:
            n = 16
        out = [{"name": "db0", "size_bytes": None, "keys": int(c0.dbsize())}]
        for i in range(1, max(1, n)):
            try:
                cnt = int(_client(cfg, user, pwd, i).dbsize())
            except Exception:
                break
            if cnt:
                out.append({"name": f"db{i}", "size_bytes": None, "keys": cnt})
        return out
    return _run(go)


def _db_num(name):
    s = str(name or "db0")
    return int(s[2:]) if s.startswith("db") and s[2:].isdigit() else 0


def list_namespaces(cfg, user, pwd, database):
    """Group keys by prefix before ':' (a common Redis namespacing convention),
    sampled via SCAN so it never blocks. Always offers '*' (all keys)."""
    def go():
        c = _client(cfg, user, pwd, _db_num(database))
        counts, seen, cursor = {}, 0, 0
        while True:
            cursor, keys = c.scan(cursor=cursor, count=500)
            for k in keys:
                ns = k.split(":", 1)[0] if ":" in k else "(no prefix)"
                counts[ns] = counts.get(ns, 0) + 1
            seen += len(keys)
            if cursor == 0 or seen >= 20000:
                break
        out = [{"name": "*", "size_bytes": None, "keys": int(c.dbsize())}]
        for ns, cnt in sorted(counts.items(), key=lambda x: -x[1]):
            out.append({"name": ns, "size_bytes": None, "keys": cnt})
        return out
    return _run(go)


def _read_value(c, key, ktype):
    try:
        if ktype == "string":
            v = c.get(key)
            return v if v is None or len(v) <= _VALUE_CAP else v[:_VALUE_CAP] + "…"
        if ktype == "hash":
            return c.hgetall(key)
        if ktype == "list":
            return c.lrange(key, 0, _ITEM_CAP)
        if ktype == "set":
            return sorted(c.sscan_iter(key, count=_ITEM_CAP))[:_ITEM_CAP]
        if ktype == "zset":
            return [[m, s] for m, s in c.zrange(key, 0, _ITEM_CAP, withscores=True)]
        if ktype == "stream":
            return {"length": c.xlen(key)}
        return None
    except RedisError:
        return None


def scan_keys(cfg, user, pwd, database, namespace="*", limit=50, offset=0, search=None):
    """A page of keys with type / ttl / memory / value. SCAN collects up to
    offset+limit matching keys (bounded) then slices; value/meta come from one
    pipeline so a page is a single round trip."""
    def go():
        c = _client(cfg, user, pwd, _db_num(database))
        ns = namespace or "*"
        term = (search or "").strip()
        if term:
            match = f"*{term}*"
        elif ns and ns != "*":
            match = f"{ns}:*" if ns != "(no prefix)" else "*"
        else:
            match = "*"
        need, collected, cursor, guard = int(offset) + int(limit), [], 0, 0
        while len(collected) < need and guard < 500:
            cursor, keys = c.scan(cursor=cursor, match=match, count=max(int(limit) * 2, 200))
            collected.extend(keys)
            guard += 1
            if cursor == 0:
                break
        page = collected[int(offset):int(offset) + int(limit)]
        pipe = c.pipeline(transaction=False)
        for k in page:
            pipe.type(k)
            pipe.ttl(k)
            pipe.memory_usage(k)
        meta = pipe.execute() if page else []
        rows, ids = [], []
        for i, k in enumerate(page):
            ktype = meta[i * 3]
            ttl = meta[i * 3 + 1]
            mem = meta[i * 3 + 2]
            rows.append([k, ktype, (ttl if isinstance(ttl, int) and ttl >= 0 else None),
                         mem, _read_value(c, k, ktype)])
            ids.append(k)
        return {"columns": ["key", "type", "ttl", "memory", "value"],
                "rows": rows, "ids": ids, "total": None, "filtered": bool(term)}
    return _run(go)


def db_stats(cfg, user, pwd, database):
    """Header stats for a Redis DB: total keys + memory footprint estimate."""
    def go():
        c = _client(cfg, user, pwd, _db_num(database))
        keys = int(c.dbsize())
        mem = None
        try:
            mem = int((c.info("memory") or {}).get("used_memory", 0)) or None
        except RedisError:
            mem = None
        return {"rows": keys, "size_bytes": mem, "estimated": mem is not None}
    return _run(go)


def info_health(cfg, user, pwd):
    def go():
        c = _client(cfg, user, pwd, 0)
        info = c.info()
        total_keys = sum(v.get("keys", 0) for k, v in info.items()
                         if isinstance(v, dict) and k.startswith("db"))
        return {
            "version": info.get("redis_version"),
            "uptime": info.get("uptime_in_seconds"),
            "role": info.get("role"),
            "connected_clients": info.get("connected_clients"),
            "maxclients": info.get("maxclients"),
            "used_memory": info.get("used_memory"),
            "used_memory_peak": info.get("used_memory_peak"),
            "maxmemory": info.get("maxmemory"),
            "keyspace_hits": info.get("keyspace_hits"),
            "keyspace_misses": info.get("keyspace_misses"),
            "ops_per_sec": info.get("instantaneous_ops_per_sec"),
            "evicted_keys": info.get("evicted_keys"),
            "total_keys": total_keys,
        }
    return _run(go)


def run_command(cfg, user, pwd, database, command_str):
    """Query console: run one raw Redis command line."""
    def go():
        parts = shlex.split(command_str)
        if not parts:
            raise ValueError("empty command")
        c = _client(cfg, user, pwd, _db_num(database))
        return c.execute_command(*parts)
    return _run(go)


# ---------------------------------------------------------------------------
# Key CRUD
# ---------------------------------------------------------------------------

def _s(v):
    return v if isinstance(v, str) else _json.dumps(v)


def set_key(cfg, user, pwd, database, key, ktype, value, ttl=None):
    """Create or replace a key with a typed value. Structured types are rebuilt
    atomically (DEL + write) in a pipeline. ttl in seconds, 0/None = no expiry."""
    def go():
        c = _client(cfg, user, pwd, _db_num(database))
        t = (ktype or "string").lower()
        if t == "string":
            c.set(key, _s(value))
        else:
            pipe = c.pipeline(transaction=True)
            pipe.delete(key)
            if t == "hash" and isinstance(value, dict) and value:
                pipe.hset(key, mapping={str(k): _s(v) for k, v in value.items()})
            elif t == "list" and isinstance(value, list) and value:
                pipe.rpush(key, *[_s(v) for v in value])
            elif t == "set" and isinstance(value, list) and value:
                pipe.sadd(key, *[_s(v) for v in value])
            elif t == "zset" and isinstance(value, list) and value:
                mapping = {str(it[0]): float(it[1]) for it in value
                           if isinstance(it, (list, tuple)) and len(it) == 2}
                if mapping:
                    pipe.zadd(key, mapping)
            elif t not in ("hash", "list", "set", "zset"):
                raise ValueError(f"Editing '{t}' values isn't supported")
            pipe.execute()
        if ttl and int(ttl) > 0:
            c.expire(key, int(ttl))
        return {"ok": True, "key": key}
    return _run(go)


def delete_key(cfg, user, pwd, database, key):
    def go():
        c = _client(cfg, user, pwd, _db_num(database))
        return {"deleted": int(c.delete(key))}
    return _run(go)


def set_ttl(cfg, user, pwd, database, key, ttl):
    """Set expiry in seconds; ttl <= 0 makes the key persistent."""
    def go():
        c = _client(cfg, user, pwd, _db_num(database))
        if ttl and int(ttl) > 0:
            ok = c.expire(key, int(ttl))
        else:
            ok = c.persist(key)
        return {"ok": bool(ok)}
    return _run(go)


# ---------------------------------------------------------------------------
# ACL user management (Redis 6+, Valkey; ElastiCache RBAC may restrict ACL)
# ---------------------------------------------------------------------------

def _acl_getuser(c, name):
    raw = c.execute_command("ACL", "GETUSER", name)
    if raw is None:
        return None
    if isinstance(raw, dict):
        d = raw
    else:  # RESP2 flat list [k, v, k, v, ...]
        d, it = {}, iter(raw)
        for k in it:
            d[k] = next(it, None)
    flags = d.get("flags") or []
    if isinstance(flags, str):
        flags = [flags]
    keys = d.get("keys", "")
    if isinstance(keys, list):
        keys = " ".join(keys)
    channels = d.get("channels", "")
    if isinstance(channels, list):
        channels = " ".join(channels)
    return {
        "name": name,
        "enabled": "on" in flags,
        "commands": d.get("commands", ""),
        "keys": keys,
        "channels": channels,
        "has_password": bool(d.get("passwords")) or "nopass" not in flags,
        "nopass": "nopass" in flags,
    }


def list_acl_users(cfg, user, pwd):
    def go():
        c = _client(cfg, user, pwd, 0)
        names = c.execute_command("ACL", "USERS") or []
        out = []
        for n in names:
            info = _acl_getuser(c, n)
            if info:
                out.append(info)
        return out
    return _run(go)


def acl_getuser(cfg, user, pwd, name):
    def go():
        c = _client(cfg, user, pwd, 0)
        info = _acl_getuser(c, name)
        if info is None:
            raise LookupError("User not found")
        return info
    data, err = _run(go)
    if isinstance(err, str) and "User not found" in err:
        return None, "User not found"
    return data, err


def acl_setuser(cfg, user, pwd, name, rules):
    """ACL SETUSER with a list of rule tokens (e.g. ['on', '>pw', '~cache:*', '+@read'])."""
    def go():
        c = _client(cfg, user, pwd, 0)
        c.execute_command("ACL", "SETUSER", name, *rules)
        return {"ok": True}
    return _run(go)


def acl_deluser(cfg, user, pwd, name):
    def go():
        c = _client(cfg, user, pwd, 0)
        return {"deleted": int(c.execute_command("ACL", "DELUSER", name))}
    return _run(go)
