"""Redis, and the Redis-compatible stores (ElastiCache, Valkey).

Redis has no tables or rows: data is typed keys (string/hash/list/set/zset/
stream) living in numbered logical DBs. So a numbered DB is a "database", a key
prefix (before ':') is a "collection", and each key is an editable "row". All of
the real work happens in the pooled native driver (redis_native); this class
just gives those calls the one shape the handlers expect.
"""

from warden_core import redis_native as rdn
from warden_core.util import validate_ident
from warden_core.validation import acl_rule_ok, redis_key

from .base import EngineAdapter, EngineError, Mutation, NotSupported, register


@register
class RedisAdapter(EngineAdapter):
    family = "redis"
    collection_key = "collections"
    has_login_toggle = False
    editable_rows = True

    # ── discovery / browse ──────────────────────────────────────────────────
    def ping(self):
        if not rdn.available():
            raise EngineError("Redis needs the native driver (redis-py)")
        return self._unwrap(rdn.ping(self.cfg, self.user, self.pwd))

    def list_databases(self):
        data = self._unwrap(rdn.list_databases(self.cfg, self.user, self.pwd))
        return [{"name": d["name"], "size_bytes": d.get("size_bytes"),
                 "keys": d.get("keys")} for d in (data or [])]

    def list_collections(self, database):
        database = validate_ident(database or "db0", "database")
        data = self._unwrap(rdn.list_namespaces(self.cfg, self.user, self.pwd, database))
        return [{"name": d["name"], "size_bytes": d.get("size_bytes"),
                 "keys": d.get("keys")} for d in (data or [])]

    def browse(self, target, limit, offset, search):
        database = validate_ident(target.database or "db0", "database")
        namespace = str(target.name or "*").strip() or "*"
        if len(namespace) > 256 or "\x00" in namespace:
            raise EngineError("Invalid key namespace")
        return self._unwrap(rdn.scan_keys(self.cfg, self.user, self.pwd, database,
                                          namespace, limit, offset, search=search))

    def object_stats(self, target):
        database = validate_ident(target.database or "db0", "database")
        return self._unwrap(rdn.db_stats(self.cfg, self.user, self.pwd, database))

    def table_meta(self, target):
        # Keys are edited with a key/type/ttl/value form, not a column grid.
        native = rdn.available()
        return {"engine": "redis", "editable": bool(native), "id_field": "key",
                "redis_edit": True,
                "reason": None if native else "native driver unavailable"}

    def health(self):
        return self._unwrap(rdn.info_health(self.cfg, self.user, self.pwd))

    # ── users / ACL ─────────────────────────────────────────────────────────
    def list_users(self):
        data = self._unwrap(rdn.list_acl_users(self.cfg, self.user, self.pwd))
        return [{"user": u["name"], "enabled": u.get("enabled", True),
                 "commands": u.get("commands", ""), "keys": u.get("keys", ""),
                 "reserved": u["name"] == "default"} for u in (data or [])]

    def user_info(self, name):
        u = self._unwrap(rdn.acl_getuser(self.cfg, self.user, self.pwd, name))
        return {"user": u["name"], "enabled": u.get("enabled", True),
                "commands": u.get("commands", ""), "keys": u.get("keys", ""),
                "channels": u.get("channels", ""), "engine_family": "redis"}

    def create_user(self, name, password, key_pattern=None, acl_level=None, **opts):
        # An ACL rule list: on, the password, one key pattern, then a command set
        # picked from the requested level (read / write / all).
        level = acl_level or "read"
        keypat = str(key_pattern or "*")
        keypat = keypat if keypat.startswith("~") else "~" + keypat
        cmds = {"read": "+@read", "write": "+@read +@write", "all": "+@all"}.get(level, "+@read")
        rules = ["on", f">{password}", keypat] + cmds.split()
        self._unwrap(rdn.acl_setuser(self.cfg, self.user, self.pwd, name, rules))
        return Mutation("CREATE ACL USER", f"{name} {level} {keypat}", {"password": password})

    def set_password(self, name, password):
        self._unwrap(rdn.acl_setuser(self.cfg, self.user, self.pwd, name,
                                     ["resetpass", f">{password}"]))
        return Mutation("RESET PASSWORD", name, {"password": password})

    def grant(self, name, rule=None, **opts):
        rule = str(rule or "").strip()
        if not acl_rule_ok(rule):
            raise EngineError("Invalid ACL rule")
        self._unwrap(rdn.acl_setuser(self.cfg, self.user, self.pwd, name, [rule]))
        return Mutation("GRANT", f"{name} {rule}")

    def revoke(self, name, rule=None, **opts):
        rule = str(rule or "").strip()
        if not acl_rule_ok(rule):
            raise EngineError("Invalid ACL rule")
        self._unwrap(rdn.acl_setuser(self.cfg, self.user, self.pwd, name, [rule]))
        return Mutation("REVOKE", f"{name} {rule}")

    def drop_user(self, name):
        res = self._unwrap(rdn.acl_deluser(self.cfg, self.user, self.pwd, name))
        return Mutation("DELETE ACL USER", name, {"deleted": res.get("deleted", 0)})

    # ── row CRUD (keys) ─────────────────────────────────────────────────────
    def insert_row(self, target, body):
        database = validate_ident(target.database or "db0", "database")
        key = redis_key(body, "key")
        self._unwrap(rdn.set_key(self.cfg, self.user, self.pwd, database, key,
                                 body.get("ktype", "string"), body.get("value"),
                                 body.get("ttl")))
        return Mutation("SET KEY", f"{database} {key} ({body.get('ktype')})", {"key": key})

    def update_row(self, target, body):
        database = validate_ident(target.database or "db0", "database")
        key = redis_key(body, "id")
        self._unwrap(rdn.set_key(self.cfg, self.user, self.pwd, database, key,
                                 body.get("ktype", "string"), body.get("value"),
                                 body.get("ttl")))
        return Mutation("SET KEY", f"{database} {key} ({body.get('ktype')})", {"modified": 1})

    def delete_row(self, target, body):
        database = validate_ident(target.database or "db0", "database")
        key = redis_key(body, "id")
        res = self._unwrap(rdn.delete_key(self.cfg, self.user, self.pwd, database, key))
        return Mutation("DELETE KEY", f"{database} {key}", {"deleted": res.get("deleted", 0)})

    # ── test a user's own login ─────────────────────────────────────────────
    def login_probe(self, test_user, test_pass, test_db=""):
        raise NotSupported("Login testing for this engine is coming in the next pass.")
