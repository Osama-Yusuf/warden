"""Unit tests for the validation / sanitisation helpers in warden_web.server.
These sit in front of every query, so they're the security-critical layer."""

import pytest

from warden_web import server


# ── mysql_account: name[@host] → quoted 'name'@'host' ──

def test_mysql_account_defaults_host():
    assert server.mysql_account("bob") == "'bob'@'%'"


def test_mysql_account_with_host():
    assert server.mysql_account("bob@10.0.0.1") == "'bob'@'10.0.0.1'"
    assert server.mysql_account("app@localhost") == "'app'@'localhost'"


@pytest.mark.parametrize("bad", ["bob@bad host", "b'; DROP@x", "x@" + "h" * 129, "bad name@h"])
def test_mysql_account_rejects_bad(bad):
    with pytest.raises(ValueError):
        server.mysql_account(bad)


# ── privilege / target validators ──

@pytest.mark.parametrize("priv,expected", [
    ("select", "SELECT"), ("SELECT", "SELECT"), (" insert ", "INSERT"),
    ("all privileges", "ALL PRIVILEGES"),
])
def test_validate_mysql_privilege_ok(priv, expected):
    assert server.validate_mysql_privilege(priv) == expected


def test_validate_mysql_privilege_rejects_unknown():
    with pytest.raises(ValueError):
        server.validate_mysql_privilege("DROP DATABASE")


def test_validate_pg_privilege_ok():
    assert server.validate_pg_privilege("select") == "SELECT"


def test_validate_pg_privilege_rejects_unknown():
    with pytest.raises(ValueError):
        server.validate_pg_privilege("nonsense")


def test_validate_target_mysql_allows_host():
    assert server.validate_target("mysql", "bob@localhost") == "bob@localhost"


def test_validate_target_non_mysql_is_plain_ident():
    assert server.validate_target("postgresql", "bob") == "bob"
    with pytest.raises(ValueError):
        server.validate_target("postgresql", "bad name")   # space is not a valid ident


# ── validate_docdb_roles ──

def test_validate_docdb_roles_ok():
    server.validate_docdb_roles([{"role": "read", "db": "shop"}])   # no raise


@pytest.mark.parametrize("roles", [
    "notalist",
    [{"role": "read"}],                       # missing db
    [{"db": "shop"}],                         # missing role
    [{"role": "superhacker", "db": "shop"}],  # unknown role
    [{"role": "read", "db": "bad db"}],       # bad db ident
])
def test_validate_docdb_roles_rejects(roles):
    with pytest.raises(ValueError):
        server.validate_docdb_roles(roles)


# ── sanitize_custom_config: browser-supplied connection details ──

def test_sanitize_custom_config_host_port():
    cfg = server.sanitize_custom_config({"host": "127.0.0.1", "port": 5432})
    assert cfg == {"host": "127.0.0.1", "port": 5432, "tls": False}


def test_sanitize_custom_config_tls_and_dbs():
    cfg = server.sanitize_custom_config(
        {"host": "db.example.com", "port": "6379", "tls": True, "default_db": "shop"})
    assert cfg["tls"] is True
    assert cfg["default_db"] == "shop"


def test_sanitize_custom_config_tls_insecure_optout():
    # verification is on by default (no flag emitted), and opt-out round-trips
    secure = server.sanitize_custom_config({"host": "h", "port": 9200, "tls": True})
    assert "tls_insecure" not in secure
    insecure = server.sanitize_custom_config(
        {"host": "h", "port": 9200, "tls": True, "tls_insecure": True})
    assert insecure["tls_insecure"] is True


def test_sanitize_custom_config_sqlite_path():
    cfg = server.sanitize_custom_config({"path": "/tmp/my.db"})
    assert cfg["path"].endswith("my.db")


@pytest.mark.parametrize("raw", [
    "notadict",
    {"host": "bad host", "port": 5432},        # space in host
    {"host": "h", "port": 0},                   # port out of range
    {"host": "h", "port": 70000},               # port out of range
    {"host": "h", "port": "notaport"},          # non-numeric port
    {"host": "", "port": 5432},                 # empty host
    {"path": ""},                               # empty path
    {"path": "x" * 501},                        # path too long
])
def test_sanitize_custom_config_rejects(raw):
    with pytest.raises(ValueError):
        server.sanitize_custom_config(raw)


# ── get_config: custom_config vs known env/engine ──

def test_get_config_uses_custom():
    cfg, err = server.get_config(
        {"custom_config": {"host": "h", "port": 5432}, "engine": "postgresql"})
    assert err is None and cfg["host"] == "h"


def test_get_config_unknown_env_errors():
    cfg, err = server.get_config({"env": "nope", "engine": "postgresql"})
    assert cfg is None and "Unknown env/engine" in err


def test_get_config_bad_custom_returns_error():
    cfg, err = server.get_config({"custom_config": {"host": "bad host", "port": 1}})
    assert cfg is None and err


# ── _acl_rule_ok: redis ACL rule guard ──

@pytest.mark.parametrize("rule", ["+@read", "~cache:*", "-@dangerous", ">newpass", "on"])
def test_acl_rule_ok_accepts(rule):
    assert server._acl_rule_ok(rule) is True


@pytest.mark.parametrize("rule", ["", "   ", "has space", "a" * 257, "null\x00", None])
def test_acl_rule_ok_rejects(rule):
    assert server._acl_rule_ok(rule) is False


# ── _sql_like_pattern: escaped LIKE literal for the subprocess engines ──

def test_sql_like_pattern_wraps_and_escapes():
    assert server._sql_like_pattern("bob") == "'%bob%'"


def test_sql_like_pattern_escapes_wildcards_and_quotes():
    out = server._sql_like_pattern("100%_o'x")
    assert out == "'%100\\%\\_o''x%'"
    assert "\x00" not in out


# ── _es_index / _redis_key: mutation-target guards ──

def test_es_index_ok():
    assert server._es_index({"collection": "products"}) == "products"


@pytest.mark.parametrize("body", [
    {"collection": ""},
    {"collection": "_internal"},     # leading underscore
    {"collection": "a,b"},           # multi-index
    {"collection": "null\x00"},
])
def test_es_index_rejects(body):
    with pytest.raises(ValueError):
        server._es_index(body)


def test_redis_key_ok():
    assert server._redis_key({"id": "cache:1"}) == "cache:1"


@pytest.mark.parametrize("body", [
    {"id": ""}, {"id": 123}, {"id": "x" * 513}, {"id": "null\x00"}, {},
])
def test_redis_key_rejects(body):
    with pytest.raises(ValueError):
        server._redis_key(body)


# ── is_mariadb: flavour detection drives the MySQL vs MariaDB query split ──

def test_is_mariadb_detects_and_caches(monkeypatch):
    calls = []

    def fake_query(cfg, user, pwd, sql):
        calls.append(sql)
        return (0, "11.8.9-MariaDB-ubu2404\n", "")

    monkeypatch.setattr(server, "my_query", fake_query)
    server._MARIADB_CACHE.clear()
    cfg = {"host": "h1", "port": 3306}
    assert server.is_mariadb(cfg, "u", "p") is True
    # second call is served from cache — no extra query
    assert server.is_mariadb(cfg, "u", "p") is True
    assert len(calls) == 1


def test_is_mariadb_false_for_mysql(monkeypatch):
    monkeypatch.setattr(server, "my_query", lambda *a: (0, "8.0.36\n", ""))
    server._MARIADB_CACHE.clear()
    assert server.is_mariadb({"host": "h2", "port": 3306}, "u", "p") is False


def test_is_mariadb_false_on_query_error(monkeypatch):
    monkeypatch.setattr(server, "my_query", lambda *a: (1, "", "boom"))
    server._MARIADB_CACHE.clear()
    assert server.is_mariadb({"host": "h3", "port": 3306}, "u", "p") is False
