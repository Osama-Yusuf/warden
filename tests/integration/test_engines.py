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
    import warden_core.pg_native as pn
    from warden_core.pg import pg_exec

    # These tests drop and recreate the same db name; clear the shared pool first
    # so a pooled connection to a db a prior test dropped can't leak in here.
    pn.close_all()
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
        # honest partial: 2 of 3 granted, the foreign one reported as skipped.
        warning = m.response.get("warning") or ""
        assert "2 of 3" in warning and "another role" in warning, f"partial not reported: {warning!r}"
    finally:
        sx('DROP DATABASE IF EXISTS "wt_app" WITH (FORCE)')
        for r in ("wt_viewer", "wt_other", "wt_owner"):
            sx(f'DROP OWNED BY "{r}" CASCADE')
            sx(f'DROP ROLE IF EXISTS "{r}"')


def test_pg_grant_finds_tables_in_any_schema():
    """A grant with no schema must reach tables wherever they live (not just
    public), actually take effect, and report the honest result. Guards the
    'reported granted but the user had no access' bug (tables in a non-public
    schema)."""
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

    def viewer_reads(qualified):
        try:
            with psycopg.connect(host=cfg["host"], port=cfg["port"], dbname="wt_app",
                                 user="wt_viewer", password="p", connect_timeout=5) as c:
                with c.cursor() as cur:
                    cur.execute(f"SELECT count(*) FROM {qualified}")
                    cur.fetchone()
            return True
        except Exception:
            return False

    sx('DROP DATABASE IF EXISTS "wt_app" WITH (FORCE)')
    for r in ("wt_viewer", "wt_owner"):
        sx(f'DROP OWNED BY "{r}" CASCADE')
        sx(f'DROP ROLE IF EXISTS "{r}"')
    try:
        sx("CREATE ROLE wt_owner LOGIN PASSWORD 'p' CREATEROLE CREATEDB")
        sx('CREATE DATABASE "wt_app" OWNER wt_owner')
        sx("CREATE SCHEMA app; CREATE TABLE app.courses (id int)",
           db="wt_app", user="wt_owner", pw="p")
        sx("CREATE ROLE wt_viewer LOGIN PASSWORD 'p'", user="wt_owner", pw="p")

        w = PostgresAdapter(cfg, admin_user="wt_owner", admin_pass="p")
        w.grant("wt_viewer", database="wt_app", privilege="CONNECT")
        # no schema -> should find and grant the non-public 'app' schema
        m = w.grant("wt_viewer", database="wt_app", schema="", privilege="SELECT")

        assert viewer_reads("app.courses"), "viewer can't read a non-public table after grant"
        assert "app" in m.detail, f"grant didn't target the app schema: {m.detail!r}"
        assert "warning" not in m.response, f"unexpected warning: {m.response.get('warning')!r}"
    finally:
        sx('DROP DATABASE IF EXISTS "wt_app" WITH (FORCE)')
        for r in ("wt_viewer", "wt_owner"):
            sx(f'DROP OWNED BY "{r}" CASCADE')
            sx(f'DROP ROLE IF EXISTS "{r}"')


def _pg_adapter(cfg):
    from warden_core.adapters.postgres import PostgresAdapter
    su = (os.environ.get("WARDEN_TEST_PG_USER", "admin"),
          os.environ.get("WARDEN_TEST_PG_PASS", "adminpass"))
    return PostgresAdapter(cfg, admin_user=su[0], admin_pass=su[1]), su


def test_pg_grant_create_lets_user_create_tables():
    """Granting CREATE must actually let the user create a table (db-level CREATE
    alone can't since PG15), and only report success when it worked."""
    ok, cfg = _pg_reachable()
    if not ok:
        pytest.skip("postgres not reachable")
    from warden_core.pg import pg_exec
    import psycopg
    w, su = _pg_adapter(cfg)

    def sx(sql, db=None):
        return pg_exec(cfg, su[0], su[1], sql, db=db)

    sx('DROP DATABASE IF EXISTS "wt_cr" WITH (FORCE)')
    sx("DROP ROLE IF EXISTS wt_maker")
    try:
        sx('CREATE DATABASE "wt_cr"')
        sx("CREATE ROLE wt_maker LOGIN PASSWORD 'p'")
        m = w.grant("wt_maker", database="wt_cr", privilege="CREATE")
        assert "warning" not in m.response, m.response.get("warning")
        with psycopg.connect(host=cfg["host"], port=cfg["port"], dbname="wt_cr",
                             user="wt_maker", password="p", connect_timeout=5,
                             autocommit=True) as c:
            c.execute("CREATE TABLE made_it (id int)")  # raised before the fix
    finally:
        sx('DROP DATABASE IF EXISTS "wt_cr" WITH (FORCE)')
        sx("DROP OWNED BY wt_maker CASCADE")
        sx("DROP ROLE IF EXISTS wt_maker")


def test_pg_revoke_reports_when_it_cannot_actually_revoke():
    """A non-superuser admin revoking a grant made by another role must NOT report
    success: Postgres revokes nothing (silently), so warden has to read the ACL
    back and raise with the grantor named."""
    ok, cfg = _pg_reachable()
    if not ok:
        pytest.skip("postgres not reachable")
    from warden_core.adapters.postgres import PostgresAdapter
    from warden_core.adapters.base import EngineError
    from warden_core.pg import pg_exec, pg_query
    su = (os.environ.get("WARDEN_TEST_PG_USER", "admin"),
          os.environ.get("WARDEN_TEST_PG_PASS", "adminpass"))

    def sx(sql, db=None, user=su[0], pw=su[1]):
        return pg_exec(cfg, user, pw, sql, db=db)

    sx('DROP DATABASE IF EXISTS "wt_rv" WITH (FORCE)')
    for r in ("wt_admin", "wt_u"):
        sx(f"DROP OWNED BY {r} CASCADE")
        sx(f"DROP ROLE IF EXISTS {r}")
    try:
        sx("CREATE ROLE wt_admin LOGIN PASSWORD 'p' CREATEROLE CREATEDB")
        sx("CREATE ROLE wt_u LOGIN PASSWORD 'p'")
        sx('CREATE DATABASE "wt_rv" OWNER wt_admin')
        # a table owned by the SUPERUSER, granted to wt_u by the superuser
        sx("CREATE TABLE su_t (id int)", db="wt_rv")
        sx('GRANT CONNECT ON DATABASE "wt_rv" TO wt_u')
        sx("GRANT SELECT ON su_t TO wt_u", db="wt_rv")
        # wt_admin (non-superuser, not the grantor) cannot revoke it
        w = PostgresAdapter(cfg, admin_user="wt_admin", admin_pass="p")
        raised = False
        try:
            w.revoke("wt_u", privilege="SELECT", database="wt_rv", schema="public")
        except EngineError as e:
            raised = True
            assert "grantor" in str(e).lower() or "superuser" in str(e).lower(), str(e)
        assert raised, "revoke falsely reported success on a silent no-op"
        # and the grant genuinely survived
        _, out, _ = pg_query(cfg, su[0], su[1],
                             "SELECT has_table_privilege('wt_u','su_t','SELECT')", db="wt_rv")
        assert out.strip() == "t"
    finally:
        sx('DROP DATABASE IF EXISTS "wt_rv" WITH (FORCE)')
        for r in ("wt_admin", "wt_u"):
            sx(f"DROP OWNED BY {r} CASCADE")
            sx(f"DROP ROLE IF EXISTS {r}")


def test_pg_drop_under_held_lock_fails_fast():
    """A drop blocked by another session's lock must fail in seconds with a clear
    message, not hang (the reported 3-minute silence). Bounded by lock_timeout."""
    import threading
    import time as _time
    ok, cfg = _pg_reachable()
    if not ok:
        pytest.skip("postgres not reachable")
    from warden_core.adapters.postgres import PostgresAdapter
    from warden_core.adapters.base import EngineError
    from warden_core.pg import pg_exec
    import psycopg
    su = (os.environ.get("WARDEN_TEST_PG_USER", "admin"),
          os.environ.get("WARDEN_TEST_PG_PASS", "adminpass"))

    def sx(sql, db=None):
        return pg_exec(cfg, su[0], su[1], sql, db=db)

    sx('DROP DATABASE IF EXISTS "wt_lock" WITH (FORCE)')
    sx("DROP ROLE IF EXISTS wt_locked")
    locker = None
    try:
        sx("CREATE ROLE wt_locked LOGIN PASSWORD 'p'")
        sx('CREATE DATABASE "wt_lock"')
        sx("GRANT ALL ON SCHEMA public TO wt_locked", db="wt_lock")
        sx('GRANT CONNECT ON DATABASE "wt_lock" TO wt_locked')
        with psycopg.connect(host=cfg["host"], port=cfg["port"], dbname="wt_lock",
                             user="wt_locked", password="p", connect_timeout=5,
                             autocommit=True) as c:
            c.execute("CREATE TABLE locked_t (id int)")
        # hold an AccessShareLock on locked_t in an open transaction
        locker = psycopg.connect(host=cfg["host"], port=cfg["port"], dbname="wt_lock",
                                 user=su[0], password=su[1], connect_timeout=5)
        locker.cursor().execute("SELECT * FROM locked_t")

        w = PostgresAdapter(cfg, admin_user=su[0], admin_pass=su[1])
        result = {}

        def do_drop():
            t0 = _time.perf_counter()
            try:
                w.drop_user("wt_locked")
                result["ok"] = True
            except EngineError as e:
                result["err"] = str(e)
            result["dt"] = _time.perf_counter() - t0

        th = threading.Thread(target=do_drop, daemon=True)
        th.start()
        th.join(timeout=40)
        assert not th.is_alive(), "drop hung > 40s behind a held lock (should fail fast)"
        assert result.get("dt", 999) < 40, f"drop took {result.get('dt')}s"
        assert "err" in result, "drop should have failed under the held lock"
    finally:
        if locker is not None:
            locker.close()
        sx('DROP DATABASE IF EXISTS "wt_lock" WITH (FORCE)')
        sx("DROP OWNED BY wt_locked CASCADE")
        sx("DROP ROLE IF EXISTS wt_locked")


def test_pg_create_user_lockdown_gives_zero_access():
    """A user created with lockdown can't connect to app databases; the admin
    still can."""
    ok, cfg = _pg_reachable()
    if not ok:
        pytest.skip("postgres not reachable")
    from warden_core.pg import pg_exec
    import psycopg
    w, su = _pg_adapter(cfg)

    def sx(sql, db=None):
        return pg_exec(cfg, su[0], su[1], sql, db=db)

    def connects(user, pw, db):
        try:
            psycopg.connect(host=cfg["host"], port=cfg["port"], dbname=db, user=user,
                            password=pw, connect_timeout=4).close()
            return True
        except Exception:
            return False

    sx('DROP DATABASE IF EXISTS "wt_ld" WITH (FORCE)')
    sx("DROP ROLE IF EXISTS wt_zero")
    try:
        sx('CREATE DATABASE "wt_ld"')
        sx("CREATE TABLE t (id int)", db="wt_ld")  # a real object so keepers exist
        w.create_user("wt_zero", "zpw", lockdown=True)
        assert not connects("wt_zero", "zpw", "wt_ld"), "lockdown user still reached the db"
        assert connects(su[0], su[1], "wt_ld"), "admin locked itself out"
    finally:
        sx('DROP DATABASE IF EXISTS "wt_ld" WITH (FORCE)')
        sx('GRANT CONNECT ON DATABASE postgres TO PUBLIC')
        sx("DROP ROLE IF EXISTS wt_zero")


def test_pg_user_info_separates_explicit_from_public():
    """A brand-new user must NOT show explicit CONNECT everywhere; PUBLIC access is
    reported separately so the UI doesn't render dead revoke buttons."""
    ok, cfg = _pg_reachable()
    if not ok:
        pytest.skip("postgres not reachable")
    from warden_core.pg import pg_exec
    w, su = _pg_adapter(cfg)

    def sx(sql, db=None):
        return pg_exec(cfg, su[0], su[1], sql, db=db)

    sx('DROP DATABASE IF EXISTS "wt_pub" WITH (FORCE)')
    sx("DROP ROLE IF EXISTS wt_fresh")
    try:
        sx('CREATE DATABASE "wt_pub"')
        sx("CREATE ROLE wt_fresh LOGIN PASSWORD 'p'")
        info = w.user_info("wt_fresh")
        explicit_dbs = [d["database"] for d in info.get("db_privileges", [])]
        assert "wt_pub" not in explicit_dbs, "PUBLIC access shown as an explicit grant"
        assert "wt_pub" in info.get("public_connect", []), "PUBLIC connect not reported"
    finally:
        sx('DROP DATABASE IF EXISTS "wt_pub" WITH (FORCE)')
        sx("DROP ROLE IF EXISTS wt_fresh")
