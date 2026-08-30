"""Unit tests for the access-report normalisation and assembly. Pure functions,
so no live engine needed; the input dicts mirror what each adapter's user_info
returns. The guiding rule under test: never classify a user read-only unless we
are sure; unclassifiable access is `unresolved`, never silently downgraded."""
from warden_web import reports


def norm(info, fam):
    return reports.normalize_user(info, fam)


def test_postgres_direct_and_role_membership():
    admin = {"user": "admin", "can_login": True, "superuser": True, "createdb": True,
             "valid_until": "never", "db_privileges": [{"database": "shop", "privileges": ["CONNECT", "CREATE"]}],
             "grants": [], "member_of": []}
    a = norm(admin, "postgresql")
    assert a["admin"] and a["write"] and not a["unresolved"] and a["summary"] == "full admin"

    reader = {"user": "alice", "can_login": True, "superuser": False, "member_of": [],
              "db_privileges": [{"database": "shop", "privileges": ["CONNECT"]}], "grants": []}
    r = norm(reader, "postgresql")
    assert not r["write"] and not r["unresolved"]

    # member of a role -> privileges live on the role; must be unresolved, NOT read-only/no-access
    via_role = {"user": "bob", "can_login": True, "superuser": False,
                "db_privileges": [{"database": "shop", "privileges": ["CONNECT"]}],
                "grants": [], "member_of": ["app_rw"]}
    vr = norm(via_role, "postgresql")
    assert vr["unresolved"] and not vr["write"] and "no_access" not in vr["flags"]
    assert "app_rw" in vr["summary"]

    # a real write grant on a normal table is a confident writer
    writer = {"user": "w", "can_login": True, "superuser": False, "member_of": [],
              "db_privileges": [{"database": "shop", "privileges": ["CONNECT"]}],
              "grants": [{"db": "shop", "table": "public.orders", "privilege": "INSERT"}]}
    assert norm(writer, "postgresql")["write"] and not norm(writer, "postgresql")["unresolved"]


def test_mysql_grants_roles_and_columns():
    root = {"user": "root@%", "can_login": True, "locked": False,
            "grant_statements": ["GRANT ALL PRIVILEGES ON *.* TO `root`@`%` WITH GRANT OPTION"]}
    assert norm(root, "mysql")["admin"]

    # role-membership line must not be dropped to "no access"; it's unresolved
    via_role = {"user": "bob@%", "can_login": True, "locked": False,
                "grant_statements": ["GRANT USAGE ON *.* TO `bob`@`%`", "GRANT `app_write`@`%` TO `bob`@`%`"]}
    vr = norm(via_role, "mysql")
    assert vr["unresolved"] and not vr["write"] and "no_access" not in vr["flags"] and "app_write" in vr["summary"]

    # column-scoped write must read as write, not be mis-tokenized to read
    col = {"user": "c@%", "can_login": True, "locked": False,
           "grant_statements": ["GRANT SELECT (a,b), INSERT (c) ON `shop`.`orders` TO `c`@`%`"]}
    assert norm(col, "mysql")["write"] is True

    ro = {"user": "rep@%", "can_login": True, "locked": False,
          "grant_statements": ["GRANT SELECT ON `shop`.* TO `rep`@`%`"]}
    assert not norm(ro, "mysql")["write"] and not norm(ro, "mysql")["unresolved"]


def test_mongo_builtin_custom_and_admin():
    assert not norm({"user": "r", "roles": [{"role": "read", "db": "shop"}]}, "documentdb")["write"]
    assert norm({"user": "w", "roles": [{"role": "readWrite", "db": "shop"}]}, "documentdb")["write"]
    assert norm({"user": "a", "roles": [{"role": "root", "db": "admin"}]}, "documentdb")["admin"]
    # dbAdmin can drop collections -> admin, not silently read
    assert norm({"user": "d", "roles": [{"role": "dbAdmin", "db": "shop"}]}, "documentdb")["admin"]
    # custom role -> unresolved, never read-only
    cu = norm({"user": "c", "roles": [{"role": "app_custom", "db": "shop"}]}, "documentdb")
    assert cu["unresolved"] and not cu["write"] and "app_custom" in cu["summary"]


def test_redis_categories_commands_custom():
    full = norm({"user": "default", "enabled": True, "commands": "+@all", "keys": "~*", "reserved": True}, "redis")
    assert full["admin"] and "full_access" in full["flags"]
    ro = norm({"user": "ro", "enabled": True, "commands": "+@read", "keys": "~*"}, "redis")
    assert not ro["write"] and not ro["unresolved"]
    # specific write command without the @write category is still write
    wr = norm({"user": "w", "enabled": True, "commands": "+get +set +del", "keys": "~data:*"}, "redis")
    assert wr["write"] and not wr["admin"]
    # specific non-category commands we can't classify -> unresolved, not read
    un = norm({"user": "x", "enabled": True, "commands": "+get +cluster", "keys": "~*"}, "redis")
    assert un["unresolved"] and not un["write"]


def test_es_builtin_and_custom():
    assert norm({"user": "a", "roles": ["superuser"], "enabled": True}, "elasticsearch")["admin"]
    assert norm({"user": "e", "roles": ["editor"], "enabled": True}, "elasticsearch")["write"]
    assert not norm({"user": "v", "roles": ["viewer"], "enabled": True}, "elasticsearch")["write"]
    cu = norm({"user": "c", "roles": ["team_writer"], "enabled": True}, "elasticsearch")
    assert cu["unresolved"] and not cu["write"] and "team_writer" in cu["summary"]


def test_posture_never_calls_unresolved_unused():
    infos = [
        {"user": "admin", "superuser": True, "can_login": True, "db_privileges": [], "grants": [], "member_of": []},
        {"user": "viarole", "superuser": False, "can_login": True, "db_privileges": [], "grants": [], "member_of": ["rw"]},
        {"user": "dead", "superuser": False, "can_login": True, "db_privileges": [], "grants": [], "member_of": []},
    ]
    pos = reports.build_report("posture", infos, "postgresql")
    issues = {f["user"]: f["issue"] for f in pos["findings"]}
    assert pos["counts"]["high"] == 1 and issues["admin"] == "Full admin"
    assert issues["viarole"] == "Access not fully resolved"          # med, NOT "unused"
    assert issues["dead"] == "Can log in but has no grants"          # only the truly empty one


def test_write_access_includes_unresolved():
    infos = [
        {"user": "admin", "superuser": True, "can_login": True, "db_privileges": [], "grants": [], "member_of": []},
        {"user": "alice", "superuser": False, "can_login": True, "member_of": [],
         "db_privileges": [{"database": "shop", "privileges": ["CONNECT"]}], "grants": []},
        {"user": "viarole", "superuser": False, "can_login": True, "db_privileges": [], "grants": [], "member_of": ["rw"]},
    ]
    wa = reports.build_report("write_access", infos, "postgresql", is_prod=True)
    assert wa["is_prod"] and {u["user"] for u in wa["users"]} == {"admin", "viarole"}  # alice read-only excluded
    assert wa["users"][0]["user"] == "admin"


def test_skipped_surfaced():
    acc = reports.build_report("access", [{"user": "ok", "superuser": False, "can_login": True,
                                           "db_privileges": [{"database": "d", "privileges": ["CONNECT"]}],
                                           "grants": [], "member_of": []}], "postgresql", skipped=3)
    assert acc["total"] == 1 and acc["skipped"] == 3
