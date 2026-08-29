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
    assert set(CATALOG) == {"nano", "small", "medium"}
    models = p.list_models()
    assert [m["id"] for m in models] == ["nano", "small", "medium"]
    assert all("downloaded" in m and "size" in m for m in models)
    assert isinstance(download_state(), dict)


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
