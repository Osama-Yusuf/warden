"""Unit tests for the AI layer. No network, no key: the provider's HTTP call and
the orchestrator's model are both faked, so we exercise the request/response
translation and the read-only tool loop without talking to Gemini.

The live Gemini round-trip is verified separately with a real key (see
tests/ai_smoke.py), since that needs a credential we don't put in the repo.
"""
import json

import pytest

from warden_core.ai import ChatResult, ToolCall, get_provider
from warden_core.ai.base import Provider
from warden_core.ai.gemini import GeminiProvider, _to_contents, _to_decl
from warden_web import assistant


# --------------------------------------------------------------------------
# Gemini wire-format translation (pure, no HTTP)
# --------------------------------------------------------------------------

def test_to_contents_roles():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [ToolCall("c0", "list_users", {})]},
        {"role": "tool", "name": "list_users", "content": '{"users": []}'},
    ]
    c = _to_contents(msgs)
    assert c[0] == {"role": "user", "parts": [{"text": "hi"}]}
    assert c[1]["role"] == "model"
    assert c[1]["parts"][0]["functionCall"]["name"] == "list_users"
    assert c[2]["role"] == "user"
    fr = c[2]["parts"][0]["functionResponse"]
    assert fr["name"] == "list_users" and fr["response"] == {"users": []}


def test_to_decl_drops_empty_params():
    decl = _to_decl({"name": "health", "description": "d", "parameters": {"type": "object", "properties": {}}})
    assert "parameters" not in decl  # Gemini dislikes an empty properties object
    decl2 = _to_decl({"name": "user_info", "description": "d",
                      "parameters": {"type": "object", "properties": {"username": {"type": "string"}}}})
    assert decl2["parameters"]["properties"]["username"]["type"] == "string"


def test_gemini_chat_parses_text(monkeypatch):
    p = GeminiProvider("key", "gemini-x")
    monkeypatch.setattr(p, "_request", lambda *a, **k: {
        "candidates": [{"content": {"role": "model", "parts": [{"text": "the answer"}]}}]})
    r = p.chat("sys", [{"role": "user", "content": "q"}], None)
    assert r.text == "the answer" and r.tool_calls == []


def test_gemini_chat_parses_function_call(monkeypatch):
    p = GeminiProvider("key", "gemini-x")
    monkeypatch.setattr(p, "_request", lambda *a, **k: {
        "candidates": [{"content": {"role": "model", "parts": [
            {"functionCall": {"name": "user_info", "args": {"username": "alice"}}}]}}]})
    r = p.chat("sys", [{"role": "user", "content": "q"}], assistant.TOOLS)
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0].name == "user_info"
    assert r.tool_calls[0].args == {"username": "alice"}


def test_gemini_list_models_filters(monkeypatch):
    p = GeminiProvider("key")
    monkeypatch.setattr(p, "_request", lambda *a, **k: {"models": [
        {"name": "models/gemini-2.0-flash", "displayName": "Gemini 2.0 Flash",
         "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004", "displayName": "Embeddings",
         "supportedGenerationMethods": ["embedContent"]},
    ]})
    ids = [m["id"] for m in p.list_models()]
    assert ids == ["gemini-2.0-flash"]  # the embedding-only model is filtered out


def test_get_provider_unknown():
    with pytest.raises(Exception):
        get_provider("hal9000", "key")


# --------------------------------------------------------------------------
# Orchestrator loop, with a scripted fake provider
# --------------------------------------------------------------------------

class FakeProvider(Provider):
    """Returns pre-scripted ChatResults, one per chat() call."""
    def __init__(self, script):
        super().__init__("k", "m")
        self.script = list(script)
        self.calls = []

    def chat(self, system, messages, tools=None):
        self.calls.append((system, messages, tools))
        return self.script.pop(0)


def _use(monkeypatch, script):
    fake = FakeProvider(script)
    monkeypatch.setattr(assistant, "get_provider", lambda *a, **k: fake)
    return fake


def _body(**extra):
    b = {"ai_provider": "gemini", "ai_key": "k", "ai_model": "m",
         "engine": "documentdb", "admin_user": "admin", "admin_pass": "pw",
         "messages": [{"role": "user", "content": "who are the users?"}]}
    b.update(extra)
    return b


def test_plain_reply(monkeypatch):
    _use(monkeypatch, [ChatResult(text="hello there")])
    out = assistant.chat_turn(_body(), routes={})
    assert out == {"reply": "hello there", "steps": []}


def test_missing_key():
    out = assistant.chat_turn({"ai_provider": "gemini", "ai_model": "m",
                               "messages": [{"role": "user", "content": "hi"}]}, routes={})
    assert "error" in out and "key" in out["error"].lower()


def test_tool_call_dispatches_then_answers(monkeypatch):
    _use(monkeypatch, [
        ChatResult(tool_calls=[ToolCall("c0", "list_users", {})]),
        ChatResult(text="You have one admin."),
    ])
    seen = {}
    def fake_list_users(sub):
        seen.update(sub)
        return {"users": [{"user": "admin"}]}
    out = assistant.chat_turn(_body(), routes={"/api/list-users": fake_list_users})
    assert out["reply"] == "You have one admin."
    assert out["steps"] == [{"tool": "list_users", "args": {}}]
    # the read handler got the connection context, not the AI fields
    assert seen["admin_user"] == "admin" and "ai_key" not in seen


def test_browse_maps_name_and_caps_limit(monkeypatch):
    _use(monkeypatch, [
        ChatResult(tool_calls=[ToolCall("c0", "browse", {"database": "shop", "name": "orders", "limit": 999})]),
        ChatResult(text="done"),
    ])
    got = {}
    out = assistant.chat_turn(_body(), routes={"/api/browse-data": lambda sub: got.update(sub) or {"rows": []}})
    assert got["table"] == "orders" and got["collection"] == "orders"
    assert got["limit"] == 50 and got["database"] == "shop"
    assert out["reply"] == "done"


def test_ask_user_ends_turn(monkeypatch):
    _use(monkeypatch, [ChatResult(tool_calls=[
        ToolCall("c0", "ask_user", {"question": "Name them?", "kind": "text"})])])
    out = assistant.chat_turn(_body(), routes={})
    assert out["question"]["question"] == "Name them?"
    assert out["question"]["kind"] == "text"


def test_draft_operation_ends_turn(monkeypatch):
    _use(monkeypatch, [ChatResult(tool_calls=[
        ToolCall("c0", "draft_operation",
                 {"summary": "create user mamo", "statements": ["CREATE USER mamo"], "writes": True})])])
    out = assistant.chat_turn(_body(), routes={})
    assert out["draft"]["summary"] == "create user mamo"
    assert out["draft"]["writes"] is True


def test_non_readonly_tool_refused(monkeypatch):
    # If the model somehow asks for a tool we don't map, the dispatcher says no
    # rather than reaching for anything mutating.
    _use(monkeypatch, [
        ChatResult(tool_calls=[ToolCall("c0", "drop_user", {"username": "x"})]),
        ChatResult(text="ok"),
    ])
    called = []
    out = assistant.chat_turn(_body(), routes={"/api/drop-user": lambda sub: called.append(sub)})
    assert called == []            # never dispatched
    assert out["reply"] == "ok"


def test_audits_land_in_system_prompt(monkeypatch):
    fake = _use(monkeypatch, [ChatResult(text="hi")])
    body = _body()
    body["_audits"] = [{"id": "admin-access", "title": "Full admin access"}]
    assistant.chat_turn(body, routes={})
    system = fake.calls[0][0]
    assert "admin-access" in system and "READ-ONLY" in system


# --------------------------------------------------------------------------
# On-device (local) provider: catalog + capability flags. No model file or
# llama-cpp needed here; list_models only checks the filesystem.
# --------------------------------------------------------------------------

def test_local_provider_flags_and_catalog():
    from warden_core.ai.local import CATALOG, LocalProvider, download_state
    p = get_provider("local", "", "small")
    assert isinstance(p, LocalProvider)
    assert p.needs_key is False and p.strong is False
    assert set(CATALOG) == {"nano", "small", "medium", "large"}
    models = p.list_models()
    assert [m["id"] for m in models] == ["nano", "small", "medium", "large"]
    assert all("downloaded" in m and "size" in m and "note" in m for m in models)
    assert sum(m["recommended"] for m in models) == 1  # exactly one recommended
    assert isinstance(download_state(), dict)


def test_data_op_translation():
    from warden_web.assistant import _data_op_body
    conn = {"admin_user": "a"}
    # Mongo: document on insert; a bare ObjectId hex gets the {"$oid"} wrapper on update
    ins = _data_op_body(conn, {"kind": "insert_data", "database": "shop", "collection": "users",
                               "document": {"n": 1}}, "documentdb")
    assert ins["collection"] == "users" and ins["document"] == {"n": 1} and ins["_source"] == "ward"
    upd = _data_op_body(conn, {"kind": "update_data", "collection": "users", "id": "a" * 24,
                               "changes": {"n": 2}}, "documentdb")
    assert upd["id"] == {"$oid": "a" * 24} and upd["set"] == {"n": 2}
    # ES id is a plain string, left alone
    assert _data_op_body(conn, {"kind": "delete_data", "collection": "logs", "id": "abc"}, "elasticsearch")["id"] == "abc"
    # Postgres insert -> column values; Redis insert -> key + value
    assert _data_op_body(conn, {"kind": "insert_data", "name": "t", "document": {"a": 1}}, "postgresql")["values"] == {"a": 1}
    rk = _data_op_body(conn, {"kind": "insert_data", "name": "k", "value": "v"}, "redis")
    assert rk["key"] == "k" and rk["value"] == "v"


def test_mongo_id_guard():
    from warden_web.assistant import _data_op_body
    c = {"admin_user": "a"}
    assert _data_op_body(c, {"kind": "delete_data", "collection": "x", "id": "a" * 24}, "documentdb")["id"] == {"$oid": "a" * 24}
    # a query operator can never become the _id filter
    assert _data_op_body(c, {"kind": "delete_data", "collection": "x", "id": {"$gt": ""}}, "documentdb")["id"] == ""
    assert _data_op_body(c, {"kind": "delete_data", "collection": "x", "id": {"$oid": "b" * 24}}, "documentdb")["id"] == {"$oid": "b" * 24}


def test_match_resolution():
    from warden_web import assistant
    # exactly one match -> auto-resolve its id, no question
    d = {"operations": [{"kind": "delete_data", "collection": "u", "match": {"name": "bob"}}]}
    assert assistant._resolve_draft_matches(d, {}, "documentdb",
        {"/api/resolve-match": lambda s: {"ids": [{"$oid": "1" * 24}], "truncated": False}}) is None
    assert d["operations"][0]["id"] == {"$oid": "1" * 24}
    # several matches -> a select question, nothing resolved
    d2 = {"operations": [{"kind": "delete_data", "collection": "u", "match": {"name": "dup"}}]}
    q2 = assistant._resolve_draft_matches(d2, {}, "documentdb",
        {"/api/resolve-match": lambda s: {"ids": [1, 2], "truncated": False}})
    assert q2 and q2["kind"] == "select" and len(q2["options"]) == 2 and "id" not in d2["operations"][0]
    # no match -> ask for the id, don't draft a blind delete
    q0 = assistant._resolve_draft_matches({"operations": [{"kind": "delete_data", "collection": "u", "match": {"x": 1}}]},
        {}, "documentdb", {"/api/resolve-match": lambda s: {"ids": [], "truncated": False}})
    assert q0 and q0["kind"] == "text"


def test_match_resolution_truncated_and_stray_match():
    from warden_web import assistant
    # too many matches to be sure -> ask (select), never auto-resolve
    d = {"operations": [{"kind": "delete_data", "collection": "big", "match": {"name": "bob"}}]}
    q = assistant._resolve_draft_matches(d, {}, "documentdb",
        {"/api/resolve-match": lambda s: {"ids": [1, 2, 3], "truncated": True}})
    assert q and q["kind"] == "select" and "id" not in d["operations"][0]
    # truncated with nothing surfaced (index too big to read) -> ask for the id
    q2 = assistant._resolve_draft_matches({"operations": [{"kind": "delete_data", "collection": "big", "match": {"name": "z"}}]},
        {}, "documentdb", {"/api/resolve-match": lambda s: {"ids": [], "truncated": True}})
    assert q2 and q2["kind"] == "text"
    # a lookup error asks rather than guessing
    q3 = assistant._resolve_draft_matches({"operations": [{"kind": "delete_data", "collection": "u", "match": {"name": "b"}}]},
        {}, "documentdb", {"/api/resolve-match": lambda s: {"error": "boom"}})
    assert q3 and q3["kind"] == "text"
    # a stray match on a SQL op is ignored so its pk path still runs
    assert assistant._resolve_draft_matches(
        {"operations": [{"kind": "delete_data", "table": "t", "pk": {"id": 5}, "match": {"name": "z"}}]},
        {}, "postgresql", {"/api/resolve-match": lambda s: {"ids": [], "truncated": False}}) is None


def test_resolve_match_rejects_operator_values():
    from warden_web import server
    # a dict/list match value would be a Mongo query operator; refuse before connecting
    r = server.api_resolve_match({"engine": "documentdb", "database": "d",
                                  "collection": "c", "match": {"name": {"$ne": None}}})
    assert r.get("error") and "operators" in r["error"]
    r2 = server.api_resolve_match({"engine": "documentdb", "database": "d",
                                   "collection": "c", "match": {"tags": [1, 2]}})
    assert r2.get("error") and "operators" in r2["error"]
    # a "$"-key (scalar value) must also be refused: it would run as an operator
    r3 = server.api_resolve_match({"engine": "documentdb", "database": "d",
                                   "collection": "c", "match": {"$where": "true"}})
    assert r3.get("error") and "operators" in r3["error"]
    r4 = server.api_resolve_match({"engine": "documentdb", "collection": "c", "match": {}})
    assert r4.get("error") and "non-empty" in r4["error"]


def test_es_grounding_and_collection_fallback():
    from warden_web import assistant
    # ES has one cluster and no databases, so don't block a draft whose "database"
    # is really an index name; other engines still ground against the real list.
    es_draft = {"operations": [{"kind": "delete_data", "database": "wardidx", "match": {"name": "x"}}]}
    assert assistant._validate_draft_db(es_draft, ["docker-cluster"], "elasticsearch") is None
    q = assistant._validate_draft_db({"operations": [{"kind": "delete_data", "database": "nope"}]},
                                     ["shop"], "documentdb")
    assert q and q["kind"] in ("select", "text")
    # on ES an index given in 'database' becomes the collection target; elsewhere never
    assert assistant._op_collection({"database": "wardidx"}, "elasticsearch") == "wardidx"
    assert assistant._op_collection({"collection": "c", "database": "wardidx"}, "elasticsearch") == "c"
    assert assistant._op_collection({"database": "shop"}, "documentdb") == ""


def test_es_redis_grant_translation():
    from warden_web.assistant import _grant_bodies
    conn = {"admin_user": "a"}
    g = {"username": "u", "database": "logs"}
    # Elasticsearch: built-in role; admin must NEVER be superuser (no cluster root)
    assert _grant_bodies(conn, {**g, "kind": "grant", "access": "write"}, "elasticsearch")[0]["roles"] == ["editor"]
    assert _grant_bodies(conn, {**g, "kind": "grant", "access": "admin"}, "elasticsearch")[0]["roles"] == ["editor"]
    # Redis GRANT: positive tokens, space-free
    gr = [b["rule"] for b in _grant_bodies(conn, {**g, "kind": "grant", "access": "write"}, "redis")]
    assert gr == ["~*", "+@read", "+@write"] and all(" " not in t for t in gr)
    # Redis REVOKE: negative tokens and NO '~*' (must not re-grant key access)
    rv = [b["rule"] for b in _grant_bodies(conn, {**g, "kind": "revoke", "access": "read"}, "redis")]
    assert rv == ["-@read"] and "~*" not in rv


def test_is_prod_no_shortcircuit():
    from warden_web.assistant import _is_prod
    # a prod-named env sent with a wrong tag still trips the gate
    assert _is_prod({"env_kind": "staging", "env": "production-main"}) is True
    assert _is_prod({"env_kind": "dev", "env": "dev-1"}) is False


def test_prod_execute_governance():
    from warden_web.assistant import _is_prod, run_operations
    assert _is_prod({"env_kind": "prod"}) is True
    assert _is_prod({"env": "production-db"}) is True
    assert _is_prod({"env": "staging"}) is False
    assert _is_prod({"env_kind": "dev"}) is False
    # prod without opt-in: refused before any op runs
    out = run_operations({"env_kind": "prod", "operations": [{"kind": "create_user", "username": "x"}]}, routes={})
    assert "error" in out and "prod" in out["error"].lower()
    # opting in gets past the prod gate (empty op list then trips the normal guard)
    out2 = run_operations({"env_kind": "prod", "allow_prod": True, "operations": []}, routes={})
    assert out2.get("error") == "Nothing to run."


def test_create_database_op_and_grounding_exempt():
    from warden_web.assistant import OP_ROUTE, _op_body, _validate_draft_db
    assert OP_ROUTE["create_database"] == "/api/create-database"
    assert _op_body({"admin_user": "a"}, {"kind": "create_database", "database": "sales"})["database"] == "sales"
    # a new database must NOT trip the "does this db exist?" guardrail
    draft = {"operations": [{"kind": "create_database", "database": "sales"}]}
    assert _validate_draft_db(draft, ["shop", "analytics"]) is None


def test_tidy_error():
    from warden_web.assistant import _tidy_error
    assert _tidy_error('User "alice@admin" already exists, full error: {...}', "alice") == "alice already exists"
    assert _tidy_error(None, "x") is None


def test_create_collection_op():
    from warden_web.assistant import OP_ROUTE, _op_body
    assert OP_ROUTE["create_collection"] == "/api/create-collection"
    b = _op_body({"admin_user": "a"}, {"kind": "create_collection", "database": "learn", "collection": "gg"})
    assert b["database"] == "learn" and b["collection"] == "gg" and b["_source"] == "ward"


def test_grant_access_translation():
    from warden_web.assistant import _grant_bodies
    conn = {"admin_user": "a"}
    g = {"username": "u", "database": "shop"}
    # Mongo: one role grant, db attached to the role
    b = _grant_bodies(conn, {**g, "access": "write"}, "documentdb")
    assert len(b) == 1 and b[0]["roles"] == [{"role": "readWrite", "db": "shop"}]
    # MySQL write is the four DML privileges, not a blanket ALL
    privs = [x["privilege"] for x in _grant_bodies(conn, {**g, "access": "write"}, "mysql")]
    assert privs == ["SELECT", "INSERT", "UPDATE", "DELETE"]
    # Postgres read must include CONNECT or the user can't even reach the db
    pg = [x["privilege"] for x in _grant_bodies(conn, {**g, "access": "read"}, "postgresql")]
    assert "CONNECT" in pg and "SELECT" in pg
    # admin uses the valid name, never the invalid bare 'ALL'
    adm = [x["privilege"] for x in _grant_bodies(conn, {**g, "access": "admin"}, "mysql")]
    assert "ALL PRIVILEGES" in adm and "ALL" not in adm


def test_draft_db_validation():
    from warden_web.assistant import _validate_draft_db
    dbs = ["koala-editor", "staging-editor", "fox-editor", "shop"]
    # a database that doesn't exist but matches several -> select the close ones
    q = _validate_draft_db({"database": "editor"}, dbs)
    assert q and q["kind"] == "select"
    assert set(q["options"]) == {"koala-editor", "staging-editor", "fox-editor"}
    # an exact match -> no question, the draft stands
    assert _validate_draft_db({"database": "fox-editor"}, dbs) is None
    # no database on the draft -> nothing to check
    assert _validate_draft_db({"summary": "make a user"}, dbs) is None
    # a name that matches nothing -> ask them to type it
    q2 = _validate_draft_db({"database": "zzzzz"}, dbs)
    assert q2 and q2["kind"] == "text"
    # can't list databases -> never blocks
    assert _validate_draft_db({"database": "editor"}, []) is None


def test_machine_report_shape():
    from warden_core.ai.local import CATALOG, machine_report
    r = machine_report()
    assert r["recommended"] in CATALOG
    assert set(r["fits"]) == set(CATALOG)
    assert isinstance(r["cores"], int) and r["cores"] >= 1
    assert r.get("summary")
    # a machine that can't run the 7B should never recommend it
    if not r["fits"]["large"]:
        assert r["recommended"] != "large"


def test_weak_provider_is_not_offered_ask_user(monkeypatch):
    fake = FakeProvider([ChatResult(text="hi")])
    fake.needs_key = False
    fake.strong = False
    monkeypatch.setattr(assistant, "get_provider", lambda *a, **k: fake)
    assistant.chat_turn(_body(ai_key=""), routes={})
    names = [t["name"] for t in fake.calls[0][2]]
    assert "draft_operation" in names and "ask_user" not in names


def test_strong_provider_gets_the_full_kit(monkeypatch):
    fake = _use(monkeypatch, [ChatResult(text="hi")])   # default strong=True
    assistant.chat_turn(_body(), routes={})
    names = [t["name"] for t in fake.calls[0][2]]
    assert "ask_user" in names and "draft_operation" in names
