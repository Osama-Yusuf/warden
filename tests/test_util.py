"""Unit tests for warden_core.util — the routing, quoting, and validation
primitives every engine path leans on."""

import pytest

from warden_core import util


# ── engine_family: the loose router. Misrouting here breaks a whole engine. ──

@pytest.mark.parametrize("engine,family", [
    # documentdb
    ("documentdb", "documentdb"),
    ("mongodb", "documentdb"),
    ("aws-documentdb", "documentdb"),
    ("mongo", "documentdb"),
    # mysql / mariadb
    ("mysql", "mysql"),
    ("mariadb", "mysql"),
    ("aurora-mysql", "mysql"),
    # sqlite
    ("sqlite", "sqlite"),
    # redis family — checked before elasticsearch on purpose
    ("redis", "redis"),
    ("valkey", "redis"),
    ("elasticache-redis", "redis"),   # "elasticache" contains "elastic"
    ("memorydb", "redis"),
    # elasticsearch / opensearch
    ("elasticsearch", "elasticsearch"),
    ("opensearch", "elasticsearch"),
    ("aws-opensearch", "elasticsearch"),
    # default → postgres-compatible
    ("postgresql", "postgresql"),
    ("aurora", "postgresql"),
    ("citus", "postgresql"),
    ("something-unknown", "postgresql"),
    ("", "postgresql"),
    (None, "postgresql"),
])
def test_engine_family(engine, family):
    assert util.engine_family(engine) == family


def test_engine_family_redis_beats_elasticsearch():
    # the ordering bug this guards: "elasticache" also matches "elastic"
    assert util.engine_family("elasticache") == "redis"
    assert util.engine_family("aws-elasticache-valkey") == "redis"


def test_engine_family_is_case_insensitive():
    assert util.engine_family("MySQL") == "mysql"
    assert util.engine_family("OpenSearch") == "elasticsearch"


# ── validate_ident: the identifier gate in front of many queries ──

@pytest.mark.parametrize("value", ["bob", "user_1", "a.b-c", "svc@host", "X" * 128])
def test_validate_ident_accepts_safe(value):
    assert util.validate_ident(value) == value


def test_validate_ident_strips_whitespace():
    assert util.validate_ident("  bob  ") == "bob"


@pytest.mark.parametrize("bad", [
    "", "   ", "bob;drop", "a b", "quote'", 'q"', "semi;", "back`tick",
    "x" * 129, None, 123, "line\nbreak", "null\x00byte",
])
def test_validate_ident_rejects_bad(bad):
    with pytest.raises(ValueError):
        util.validate_ident(bad)


# ── quoting helpers: injection-relevant, so check escaping ──

def test_pg_ident_escapes_double_quotes():
    assert util.pg_ident('weird"name') == '"weird""name"'


def test_pg_literal_escapes_single_quotes():
    assert util.pg_literal("O'Brien") == "'O''Brien'"


def test_js_string_quotes_and_escapes():
    assert util.js_string('a"b') == '"a\\"b"'
    assert util.js_string("plain") == '"plain"'


# ── generate_password: complexity contract ──

def test_generate_password_length_and_complexity():
    for _ in range(50):
        pw = util.generate_password()
        assert len(pw) == 24
        assert any(c.isupper() for c in pw)
        assert any(c.islower() for c in pw)
        assert any(c.isdigit() for c in pw)
        assert any(c in "!@#$%^&*" for c in pw)


def test_generate_password_custom_length():
    assert len(util.generate_password(40)) == 40


# ── format_size: the human-readable byte formatter ──

@pytest.mark.parametrize("num,expected", [
    (0, "0 B"),
    (512, "512 B"),
    (1023, "1023 B"),
    (1024, "1.00 KB"),
    (1536, "1.50 KB"),
    (10 * 1024, "10.0 KB"),
    (100 * 1024, "100 KB"),
    (1048576, "1.00 MB"),
    (1073741824, "1.00 GB"),
    (None, "0 B"),
])
def test_format_size(num, expected):
    assert util.format_size(num) == expected
