"""Unit tests for pure helpers inside the native drivers, no network needed."""

import pytest

from warden_core import redis_native as rdn
from warden_core import es_native as esn


# ── redis: numbered-DB name parsing ("db0" → 0) ──

@pytest.mark.parametrize("name,expected", [
    ("db0", 0), ("db5", 5), ("db12", 12),
    ("db", 0),          # no digits after prefix
    ("", 0), (None, 0),
    ("foo", 0),         # not a db name
    ("0", 0),           # missing prefix
])
def test_db_num(name, expected):
    assert rdn._db_num(name) == expected


# ── redis: value serialisation for SET ──

def test_redis_s_passthrough_string():
    assert rdn._s("hello") == "hello"


def test_redis_s_json_encodes_non_strings():
    assert rdn._s({"a": 1}) == '{"a": 1}'
    assert rdn._s([1, 2]) == "[1, 2]"


# ── redis: ACL GETUSER parsing (handles RESP2 flat list AND RESP3 dict) ──

class _FakeRedis:
    def __init__(self, reply):
        self._reply = reply

    def execute_command(self, *args):
        return self._reply


def test_acl_getuser_resp2_flat_list():
    reply = ["flags", ["on"], "passwords", ["deadbeef"],
             "commands", "+@all", "keys", "~*", "channels", "&*"]
    info = rdn._acl_getuser(_FakeRedis(reply), "alice")
    assert info["name"] == "alice"
    assert info["enabled"] is True
    assert info["commands"] == "+@all"
    assert info["keys"] == "~*"
    assert info["has_password"] is True
    assert info["nopass"] is False


def test_acl_getuser_resp3_dict_with_nopass_and_list_keys():
    reply = {"flags": ["on", "nopass"], "passwords": [],
             "commands": "-@all +get", "keys": ["~k1", "~k2"], "channels": []}
    info = rdn._acl_getuser(_FakeRedis(reply), "bob")
    assert info["enabled"] is True
    assert info["nopass"] is True
    assert info["has_password"] is False
    # list-shaped keys/channels get joined into a string
    assert info["keys"] == "~k1 ~k2"
    assert info["channels"] == ""


def test_acl_getuser_disabled_user():
    reply = {"flags": ["off"], "passwords": ["x"], "commands": "-@all",
             "keys": "", "channels": ""}
    info = rdn._acl_getuser(_FakeRedis(reply), "carol")
    assert info["enabled"] is False


def test_acl_getuser_missing_returns_none():
    assert rdn._acl_getuser(_FakeRedis(None), "ghost") is None


# ── elasticsearch: field-name guard used before building painless updates ──

@pytest.mark.parametrize("field", ["price", "user.name", "created_at", "tag-1", "a@b", "F1"])
def test_es_field_regex_accepts(field):
    assert esn._ES_FIELD.match(field)


@pytest.mark.parametrize("field", [
    "drop; ctx._source", "field name", "quote'", 'q"', "semi;colon", "brace}", ""
])
def test_es_field_regex_rejects(field):
    assert not esn._ES_FIELD.match(field)


def test_drivers_report_availability():
    # the deps are installed in the test env, so both should be importable
    assert rdn.available() is True
    assert esn.available() is True
