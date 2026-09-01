"""Integration tests: exercise the real server handlers against live engines.

These call the api_* handlers directly (no HTTP) with a browser-style
custom_config, so they cover routing + driver + query for every engine.

Skipped automatically unless the engine is reachable, and skipped entirely
in a normal run (marked `integration`; opt in with `-m integration`).

Connection details default to the local docker test containers and can be
overridden per engine with env vars, e.g. WARDEN_TEST_PG_HOST / _PORT /
_USER / _PASS.
"""

import os
import pytest

from warden_web import server

pytestmark = pytest.mark.integration


def _cfg(prefix, port, user, pw, extra=None):
    cc = {"host": os.environ.get(f"{prefix}_HOST", "127.0.0.1"),
          "port": int(os.environ.get(f"{prefix}_PORT", port))}
    if extra:
        cc.update(extra)
    return {"custom_config": cc,
            "admin_user": os.environ.get(f"{prefix}_USER", user),
            "admin_pass": os.environ.get(f"{prefix}_PASS", pw)}


ENGINES = {
    "postgresql":    lambda: {"engine": "postgresql", **_cfg("WARDEN_TEST_PG", 5432, "admin", "adminpass")},
    "mysql":         lambda: {"engine": "mysql", **_cfg("WARDEN_TEST_MYSQL", 3306, "root", "adminpass")},
    "documentdb":    lambda: {"engine": "documentdb", **_cfg("WARDEN_TEST_MONGO", 27017, "admin", "adminpass", {"auth_db": "admin"})},
    "elasticsearch": lambda: {"engine": "elasticsearch", **_cfg("WARDEN_TEST_ES", 9200, "elastic", "espass")},
    "redis":         lambda: {"engine": "redis", **_cfg("WARDEN_TEST_REDIS", 6379, "", "testpass")},
}


@pytest.fixture(params=sorted(ENGINES))
def body(request):
    make = ENGINES[request.param]
    b = make()
    res = server.api_connect(dict(b))
    if not res.get("ok"):
        pytest.skip(f"{request.param} not reachable: {res.get('error', 'no connection')}")
    return b


def test_connect(body):
    res = server.api_connect(dict(body))
    assert res.get("ok") is True
    assert res.get("user")   # a human-readable identity string


def test_list_databases(body):
    res = server.api_list_databases(dict(body))
    assert "error" not in res, res.get("error")
    assert isinstance(res.get("databases"), list)
    assert len(res["databases"]) >= 1
    for d in res["databases"]:
        assert "name" in d


def test_list_users(body):
    res = server.api_list_users(dict(body))
    assert "error" not in res, res.get("error")
    assert isinstance(res.get("users"), list)
    # every engine here has at least an admin/root/default account
    assert len(res["users"]) >= 1
    for u in res["users"]:
        assert "user" in u


def test_browse_first_collection(body):
    """connect → list databases → list collections → browse a page."""
    dbs = server.api_list_databases(dict(body))
    assert isinstance(dbs.get("databases"), list) and dbs["databases"]

    # find the first database that actually has something browsable
    database, items = None, []
    for d in dbs["databases"]:
        cols = server.api_list_collections(dict(body, database=d["name"]))
        items = cols.get("collections") or cols.get("tables") or []
        if items:
            database = d["name"]
            break
    if not items:
        pytest.skip("no browsable objects in any database")

    first = items[0]
    browse = dict(body, database=database, limit=5)
    if "table" in first:
        browse["table"] = first["table"]
        if first.get("schema"):
            browse["schema"] = first["schema"]
    else:
        browse["collection"] = first["name"]

    res = server.api_browse_data(browse)
    assert "error" not in res, res.get("error")
    assert isinstance(res.get("columns"), list)
    assert isinstance(res.get("rows"), list)


# ── Postgres: dropping a user who owns objects, connected as a NON-superuser ──
# admin. This is the real-world case (warden usually connects as a privileged
# app role, not a superuser): REASSIGN/DROP OWNED are refused unless warden holds
# the target role's privileges, so the drop used to fail on "N objects in
# database X" even though the cascade "ran". Guards that regression.

def _pg_reachable():
    from warden_core.pg import pg_exec
    cfg = {"host": os.environ.get("WARDEN_TEST_PG_HOST", "127.0.0.1"),
           "port": int(os.environ.get("WARDEN_TEST_PG_PORT", "5432")),
           "default_db": "postgres"}
    ok, _, _ = pg_exec(cfg, os.environ.get("WARDEN_TEST_PG_USER", "admin"),
                       os.environ.get("WARDEN_TEST_PG_PASS", "adminpass"), "SELECT 1")
    return ok, cfg


def test_pg_drop_user_owning_objects_as_nonsuperuser():
    from warden_core.adapters.postgres import PostgresAdapter
    from warden_core.pg import pg_exec, pg_query

    ok, cfg = _pg_reachable()
    if not ok:
        pytest.skip("postgres not reachable")
    su = (os.environ.get("WARDEN_TEST_PG_USER", "admin"),
          os.environ.get("WARDEN_TEST_PG_PASS", "adminpass"))

    def sx(sql, db=None, user=su[0], pw=su[1]):
        return pg_exec(cfg, user, pw, sql, db=db)

    def sq(sql, db=None):
        _, out, _ = pg_query(cfg, su[0], su[1], sql, db=db)
        return out.strip()

    # clean slate
    sx('DROP DATABASE IF EXISTS "wt_app" WITH (FORCE)')
    for r in ("wt_victim", "wt_admin"):
        sx(f'DROP OWNED BY "{r}" CASCADE')
        sx(f'DROP ROLE IF EXISTS "{r}"')
    try:
        # a non-superuser admin that owns its app database, plus a victim that
        # owns a table in it (the object that blocks a naive DROP ROLE).
        sx("CREATE ROLE wt_admin LOGIN PASSWORD 'p' CREATEROLE CREATEDB")
        sx('CREATE DATABASE "wt_app" OWNER wt_admin')
        sx("CREATE ROLE wt_victim LOGIN PASSWORD 'p'", user="wt_admin", pw="p")
        sx('GRANT ALL ON SCHEMA public TO wt_victim', db="wt_app", user="wt_admin", pw="p")
        sx("CREATE TABLE keep_me (id int)", db="wt_app", user="wt_victim", pw="p")

        # a bare DROP ROLE would fail here; warden's cascade must not.
        w = PostgresAdapter(cfg, admin_user="wt_admin", admin_pass="p")
        w.drop_user("wt_victim")

        assert sq("SELECT count(*) FROM pg_roles WHERE rolname='wt_victim'") == "0"
        # the victim's table is reassigned to the admin, never dropped.
        owner = sq("SELECT tableowner FROM pg_tables WHERE tablename='keep_me'", db="wt_app")
        assert owner == "wt_admin", f"table lost or wrong owner: {owner!r}"
    finally:
        sx('DROP DATABASE IF EXISTS "wt_app" WITH (FORCE)')
        for r in ("wt_victim", "wt_admin"):
            sx(f'DROP OWNED BY "{r}" CASCADE')
            sx(f'DROP ROLE IF EXISTS "{r}"')


def test_pg_grant_skips_tables_owned_by_others():
    """Granting SELECT on a schema where one table is owned by ANOTHER role must
    still grant every table the connecting role owns (not abort on the foreign
    one), and report what it skipped. Connect as the db/table owner, like the app
    role does."""
    import psycopg

    from warden_core.adapters.postgres import PostgresAdapter
    from warden_core.pg import pg_exec

    ok, cfg = _pg_reachable()
    if not ok:
        pytest.skip("postgres not reachable")
    su = (os.environ.get("WARDEN_TEST_PG_USER", "admin"),
          os.environ.get("WARDEN_TEST_PG_PASS", "adminpass"))

    def sx(sql, db=None, user=su[0], pw=su[1]):
        return pg_exec(cfg, user, pw, sql, db=db)

    def owner_reads(table):
        try:
            with psycopg.connect(host=cfg["host"], port=cfg["port"], dbname="wt_app",
                                 user="wt_viewer", password="p", connect_timeout=5) as c:
                with c.cursor() as cur:
                    cur.execute(f"SELECT count(*) FROM {table}")
                    cur.fetchone()
            return True
        except Exception:
            return False

    sx('DROP DATABASE IF EXISTS "wt_app" WITH (FORCE)')
    for r in ("wt_viewer", "wt_other", "wt_owner"):
        sx(f'DROP OWNED BY "{r}" CASCADE')
        sx(f'DROP ROLE IF EXISTS "{r}"')
    try:
        sx("CREATE ROLE wt_owner LOGIN PASSWORD 'p' CREATEROLE CREATEDB")
        sx("CREATE ROLE wt_other LOGIN PASSWORD 'p'")
        sx('CREATE DATABASE "wt_app" OWNER wt_owner')
        sx("CREATE TABLE mine_a (id int); CREATE TABLE mine_b (id int)",
           db="wt_app", user="wt_owner", pw="p")
        sx('GRANT CREATE ON SCHEMA public TO wt_other', db="wt_app", user="wt_owner", pw="p")
        sx("CREATE TABLE foreign_t (id int)", db="wt_app", user="wt_other", pw="p")
        sx("CREATE ROLE wt_viewer LOGIN PASSWORD 'p'", user="wt_owner", pw="p")

        w = PostgresAdapter(cfg, admin_user="wt_owner", admin_pass="p")
        w.grant("wt_viewer", database="wt_app", privilege="CONNECT")
        # would have raised "permission denied for table foreign_t" before the fix.
        m = w.grant("wt_viewer", database="wt_app", schema="public", privilege="SELECT")

        assert owner_reads("mine_a"), "viewer can't read owner's table after grant"
        assert owner_reads("mine_b"), "viewer can't read owner's table after grant"
        assert not owner_reads("foreign_t"), "unexpectedly granted the foreign table"
        assert "foreign_t" in (m.response.get("warning") or ""), "skipped table not reported"
    finally:
        sx('DROP DATABASE IF EXISTS "wt_app" WITH (FORCE)')
        for r in ("wt_viewer", "wt_other", "wt_owner"):
            sx(f'DROP OWNED BY "{r}" CASCADE')
            sx(f'DROP ROLE IF EXISTS "{r}"')
