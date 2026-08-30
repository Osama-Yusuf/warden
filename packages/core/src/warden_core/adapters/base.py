"""One class per database engine.

The web and CLI layers ask an adapter to do a thing (list databases, browse a
page, drop a user) and don't care which engine is behind it. Each adapter wraps
that engine's existing driver module, so the drivers keep doing the real work
and the adapters just give them one shared shape. Adding a new engine means
writing one class here, not editing a branch in twenty handlers.

Errors are raised, not returned: a method either gives you the data or raises
EngineError (something went wrong) / NotSupported (this engine can't do it).
"""

from dataclasses import dataclass, field

from warden_core.util import engine_family


class EngineError(Exception):
    """The engine refused or failed the operation: bad creds, missing table, etc."""


class NotSupported(EngineError):
    """This engine doesn't offer this operation, e.g. SQLite has no user accounts."""


@dataclass
class Target:
    """What a browse/stats/CRUD call points at. `name` is the collection, table,
    index, or key-namespace depending on the engine; `schema` is SQL-only."""
    database: str = ""
    name: str = ""
    schema: str = ""


@dataclass
class Mutation:
    """What a write op hands back. `action` and `detail` are the two strings the
    handler writes to the audit log (e.g. "GRANT", "bob += SELECT on shop.public");
    `response` is any extra fields to merge into the {"ok": True, ...} reply, like
    a fresh password or a row count. The handler owns the audit call and the
    envelope, so the adapter never has to know either exists."""
    action: str
    detail: str = ""
    response: dict = field(default_factory=dict)


class EngineAdapter:
    # ── what this engine can do (handlers read these instead of guessing) ──
    family = ""
    collection_key = "tables"   # list_collections returns under "tables" or "collections"
    has_users = True            # SQLite turns this off
    has_login_toggle = False    # only Postgres/MySQL can lock or unlock a login
    editable_rows = False       # in-grid row CRUD

    def __init__(self, cfg, admin_user="", admin_pass="", env="production", engine=None):
        self.cfg = cfg
        self.user = admin_user
        self.pwd = admin_pass
        self.env = env
        self.engine = engine or self.family

    # A lot of drivers hand back (data, error). This turns that into "give me the
    # data or raise", so every adapter method reads the same way.
    @staticmethod
    def _unwrap(result):
        data, err = result
        if err:
            raise EngineError(err)
        return data

    # ── discovery / browse ──────────────────────────────────────────────────
    def ping(self):
        """Connect and return a short human identity string for the top bar."""
        raise NotSupported("connect is not implemented for this engine")

    def list_databases(self):
        raise NotSupported("listing databases is not supported here")

    def list_collections(self, database):
        raise NotSupported("listing collections is not supported here")

    def create_collection(self, target):
        raise NotSupported("creating a collection isn't supported for this engine")

    def create_database(self, name):
        raise NotSupported("creating a database isn't supported for this engine")

    def browse(self, target, limit, offset, search):
        raise NotSupported("browsing rows is not supported here")

    def object_stats(self, target):
        return {}

    def table_meta(self, target):
        return {"editable": False,
                "reason": f"row editing isn't supported for {self.family} yet"}

    def health(self):
        raise NotSupported("health is not supported here")

    # ── users / ACL ─────────────────────────────────────────────────────────
    def list_users(self):
        raise NotSupported("this engine has no user accounts")

    def user_info(self, name):
        raise NotSupported("this engine has no user accounts")

    def create_user(self, name, password, **opts):
        raise NotSupported("creating users is not supported here")

    def set_password(self, name, password):
        raise NotSupported("resetting passwords is not supported here")

    def grant(self, name, **opts):
        raise NotSupported("granting is not supported here")

    def revoke(self, name, **opts):
        raise NotSupported("revoking is not supported here")

    def drop_user(self, name):
        raise NotSupported("dropping users is not supported here")

    def toggle_login(self, name, enable):
        raise NotSupported("enabling or disabling a login isn't applicable here")

    # ── row CRUD ────────────────────────────────────────────────────────────
    # `body` is the raw request body: each engine pulls what it needs (a document,
    # a key, a {column: value} map) out of it, so one shape covers all of them.
    def insert_row(self, target, body):
        raise NotSupported("row editing isn't available for this engine")

    def update_row(self, target, body):
        raise NotSupported("row editing isn't available for this engine")

    def delete_row(self, target, body):
        raise NotSupported("row editing isn't available for this engine")

    # ── query console ───────────────────────────────────────────────────────
    def run_query(self, database, query):
        raise NotSupported("the query console isn't wired up for this engine")

    # ── test a user's own login ─────────────────────────────────────────────
    def login_probe(self, test_user, test_pass, test_db=""):
        raise NotSupported("there's nothing to test-login here")


_ADAPTERS = {}


def register(cls):
    _ADAPTERS[cls.family] = cls
    return cls


def get_adapter(engine, cfg, admin_user="", admin_pass="", env="production"):
    """Pick the adapter for an engine key (loose matching, same as everywhere)."""
    cls = _ADAPTERS.get(engine_family(engine))
    if cls is None:
        raise EngineError(f"no adapter for engine {engine!r}")
    return cls(cfg, admin_user, admin_pass, env, engine)
