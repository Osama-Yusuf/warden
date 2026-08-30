"""Unit tests for the access-report normalisation and assembly. Pure functions,
so no live engine needed; the input dicts mirror what each adapter's user_info
actually returns (verified against the running test engines)."""
from warden_web import reports


def norm(info, fam):
    return reports.normalize_user(info, fam)


def test_postgres_normalization():
    admin = {"user": "admin", "can_login": True, "superuser": True, "createdb": True,
             "valid_until": "never", "db_privileges": [{"database": "shop", "privileges": ["CONNECT", "CREATE"]}],
             "grants": [{"db": "postgres", "table": "information_schema.attributes", "privilege": "SELECT"}]}
    a = norm(admin, "postgresql")
    assert a["admin"] and a["write"] and a["summary"] == "full admin" and "superuser" in a["flags"]

    reader = {"user": "alice", "can_login": True, "superuser": False,
              "db_privileges": [{"database": "shop", "privileges": ["CONNECT"]},
                                {"database": "analytics", "privileges": ["CONNECT"]}], "grants": []}
    r = norm(reader, "postgresql")
    assert not r["admin"] and not r["write"]
    assert {s["scope"]: s["level"] for s in r["scopes"]} == {"shop": "read", "analytics": "read"}

    # a real write grant on a normal table lifts a CONNECT-only user to writer
    writer = {"user": "bob", "can_login": True, "superuser": False,
              "db_privileges": [{"database": "shop", "privileges": ["CONNECT"]}],
              "grants": [{"db": "shop", "table": "public.orders", "privilege": "INSERT"}]}
    assert norm(writer, "postgresql")["write"] is True


def test_mysql_grant_parsing():
    root = {"user": "root@%", "can_login": True, "locked": False,
            "grant_statements": ["GRANT ALL PRIVILEGES ON *.* TO `root`@`%` WITH GRANT OPTION"]}
    assert norm(root, "mysql")["admin"] is True

    ro = {"user": "rep@%", "can_login": True, "locked": False,
          "grant_statements": ["GRANT USAGE ON *.* TO `rep`@`%`", "GRANT SELECT ON `shop`.* TO `rep`@`%`"]}
    r = norm(ro, "mysql")
    assert not r["admin"] and not r["write"]
    assert r["scopes"] == [{"scope": "shop", "level": "read"}]   # USAGE dropped

    rw = {"user": "app@%", "can_login": True, "locked": False,
          "grant_statements": ["GRANT SELECT, INSERT, UPDATE ON `shop`.* TO `app`@`%`"]}
    assert norm(rw, "mysql")["write"] is True

    locked = {"user": "old@%", "can_login": False, "locked": True,
              "grant_statements": ["GRANT USAGE ON *.* TO `old`@`%`"]}
    lo = norm(locked, "mysql")
    assert not lo["login"] and "locked" in lo["flags"] and "no_access" in lo["flags"]


def test_mongo_roles():
    assert norm({"user": "r", "roles": [{"role": "read", "db": "shop"}]}, "documentdb") \
        == norm({"user": "r", "roles": [{"role": "read", "db": "shop"}]}, "documentdb")
    reader = norm({"user": "r", "roles": [{"role": "read", "db": "shop"}]}, "documentdb")
    assert not reader["write"] and reader["scopes"] == [{"scope": "shop", "level": "read"}]
    assert norm({"user": "w", "roles": [{"role": "readWrite", "db": "shop"}]}, "documentdb")["write"]
    root = norm({"user": "a", "roles": [{"role": "root", "db": "admin"}]}, "documentdb")
    assert root["admin"] and root["write"]


def test_redis_acl():
    full = norm({"user": "default", "enabled": True, "commands": "+@all", "keys": "~*", "reserved": True}, "redis")
    assert full["admin"] and full["write"] and "full_access" in full["flags"]
    ro = norm({"user": "ro", "enabled": True, "commands": "+@read -@write", "keys": "~*"}, "redis")
    assert not ro["write"] and not ro["admin"]
    wr = norm({"user": "w", "enabled": True, "commands": "+@read +@write", "keys": "~data:*"}, "redis")
    assert wr["write"] and not wr["admin"]


def test_es_roles():
    assert norm({"user": "a", "roles": ["superuser"], "enabled": True}, "elasticsearch")["admin"]
    assert norm({"user": "e", "roles": ["editor"], "enabled": True}, "elasticsearch")["write"]
    v = norm({"user": "v", "roles": ["viewer"], "enabled": True}, "elasticsearch")
    assert not v["write"] and not v["admin"]


def test_build_reports():
    infos = [
        {"user": "admin", "superuser": True, "can_login": True, "db_privileges": [], "grants": []},
        {"user": "alice", "superuser": False, "can_login": True,
         "db_privileges": [{"database": "shop", "privileges": ["CONNECT"]}], "grants": []},
        {"user": "deploy", "superuser": False, "can_login": True,
         "db_privileges": [{"database": "shop", "privileges": ["CONNECT", "CREATE"]}], "grants": []},
    ]
    wa = reports.build_report("write_access", infos, "postgresql", is_prod=True)
    assert wa["kind"] == "write_access" and wa["is_prod"] and "prod" in wa["title"]
    assert {u["user"] for u in wa["users"]} == {"admin", "deploy"}   # alice is read-only
    assert wa["users"][0]["user"] == "admin"                          # admins ranked first

    pos = reports.build_report("posture", infos, "postgresql")
    assert pos["counts"]["high"] == 1 and any(f["user"] == "admin" for f in pos["findings"])

    acc = reports.build_report("access", infos, "postgresql")
    assert acc["kind"] == "access" and acc["total"] == 3 and acc["users"][0]["user"] == "admin"


def test_error_infos_skipped():
    infos = [{"error": "boom"}, {"user": "ok", "superuser": False, "can_login": True,
                                 "db_privileges": [{"database": "d", "privileges": ["CONNECT"]}], "grants": []}]
    acc = reports.build_report("access", infos, "postgresql")
    assert acc["total"] == 1 and acc["users"][0]["user"] == "ok"
