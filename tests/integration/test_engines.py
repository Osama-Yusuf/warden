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
