"""The assistant orchestrator: warden's side of the AI conversation.

Phase 1 is read-only. The model can look at anything through the tools below,
but it cannot change data. When the user asks for a change, the model doesn't
run it, it calls draft_operation and warden shows the human a preview to run
themselves. That's the safety spine: the model proposes, warden disposes.

This module knows nothing about which provider is answering (that's warden_core.
ai) and doesn't import the server (that would be circular). It's handed the
route table so it can dispatch a tool call to the same read-only handler the
buttons use.
"""
from __future__ import annotations

import json

from warden_core.ai import AIError, get_provider
from warden_core.ai import local

MAX_HOPS = 6            # tool round-trips before we stop and answer with what we have
MAX_TOOL_CHARS = 6000   # trim a tool result before feeding it back, to keep tokens sane
MAX_MSGS = 40           # ignore anything older than this in the thread


# The read-only tools the model may call, and the handler each maps to. The
# route is always one that only reads, so a tool call can never mutate. Args are
# merged straight onto the connection context and passed to the handler.
TOOLS = [
    {
        "name": "list_users", "route": "/api/list-users",
        "description": "List the database's user accounts and their access. Use this to answer who can do what.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "user_info", "route": "/api/user-info",
        "description": "Everything about one user: their roles, grants, and what they can touch.",
        "parameters": {"type": "object", "properties": {
            "username": {"type": "string", "description": "The exact account name, from list_users."}},
            "required": ["username"]},
    },
    {
        "name": "list_databases", "route": "/api/list-databases",
        "description": "List the databases (or for Redis, the numbered DBs) on this connection.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "list_collections", "route": "/api/list-collections",
        "description": "List the tables (SQL) or collections/indices for one database.",
        "parameters": {"type": "object", "properties": {
            "database": {"type": "string", "description": "Which database, from list_databases."}},
            "required": ["database"]},
    },
    {
        "name": "browse", "route": "/api/browse-data",
        "description": "Read one page of rows from a table or collection. For looking, not counting.",
        "parameters": {"type": "object", "properties": {
            "database": {"type": "string"},
            "name": {"type": "string", "description": "The table or collection name."},
            "search": {"type": "string", "description": "Optional server-side filter."},
            "limit": {"type": "integer", "description": "Rows to fetch, default 20, max 50."}},
            "required": ["database", "name"]},
    },
    {
        "name": "health", "route": "/api/health",
        "description": "Cluster health: connections, slow queries, cache hit, replication lag, and the like.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "run_audit", "route": "/api/audit-run",
        "description": "Run one security audit and get its findings. Only use an id listed in the system prompt.",
        "parameters": {"type": "object", "properties": {
            "audit": {"type": "string", "description": "The audit id, e.g. 'admin-access'."}},
            "required": ["audit"]},
    },
]

# Belt and braces: even though every tool above points at a reading handler,
# the dispatcher refuses to call anything outside this set.
READONLY_ROUTES = {t["route"] for t in TOOLS}
_BY_NAME = {t["name"]: t for t in TOOLS}

# Tools the model can call that don't run anything. They end the turn with a
# payload the chat panel renders: a question to the user, or a proposed
# operation for the human to run. Declared to the model alongside the real ones.
VIRTUAL_TOOLS = [
    {
        "name": "ask_user",
        "description": ("Ask the user for something you genuinely cannot look up or default, like the "
                        "name of a thing to create. Do not ask about passwords or privileges, those have "
                        "safe defaults."),
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string"},
            "kind": {"type": "string", "enum": ["text", "select", "multiselect", "boolean"]},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": "Choices, for select or multiselect."},
            "hint": {"type": "string", "description": "A short clarifying note under the question."}},
            "required": ["question", "kind"]},
    },
    {
        "name": "draft_operation",
        "description": ("Propose a change instead of making it. warden shows the user a preview they can run "
                        "themselves. Use this for anything that creates, grants, revokes, changes, or deletes."),
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "One plain-english line of what this does."},
            "statements": {"type": "array", "items": {"type": "string"},
                           "description": "The concrete steps, one per line."},
            "writes": {"type": "boolean", "description": "True if it changes data (almost always true here)."}},
            "required": ["summary", "statements"]},
    },
]

_VIRTUAL = {t["name"] for t in VIRTUAL_TOOLS}
_BY_VNAME = {t["name"]: t for t in VIRTUAL_TOOLS}


# ---------------------------------------------------------------------------

def _system_prompt(body):
    ctx = body.get("context", {}) or {}
    engine = body.get("engine") or ctx.get("engine") or "the database"
    env = ctx.get("env") or body.get("env") or "unknown"
    page = ctx.get("page") or "unknown"
    selection = ctx.get("selection") or "nothing"
    ro = "on" if body.get("read_only") else "off"
    audits = body.get("_audits", [])
    audit_line = (", ".join(f"{a['id']} ({a['title']})" for a in audits)
                  if audits else "none for this engine")
    persona = (body.get("persona") or "").strip()

    lines = [
        "You are warden's assistant, standing next to the doorman who guards the user's databases.",
        "You help them inspect and understand their data. You are careful, brief, and plain-spoken.",
        "",
        "You are in READ-ONLY mode. You may look at anything with your tools, but you cannot change data.",
        "Decide how to respond, in this order:",
        "1. A greeting or a question about you: just answer in a sentence. No tools.",
        "2. A question about the data: call the read tools you need, then ANSWER in plain words using what"
        " came back. Never invent a user, database, table, or collection name; look it up first. Do not"
        " call the same tool twice, and never call ask_user just to repeat the user's own question.",
        "3. A request to create, grant, revoke, change, or delete, when you have the essentials (a name,"
        " and roughly what access): call draft_operation with the concrete steps. warden shows the user a"
        " preview to run; you never run it. Passwords and privileges have safe defaults, so you do NOT"
        " need them and must not ask about them.",
        "4. Only call ask_user when YOU are missing an essential you cannot look up or default, like a name"
        " the user never gave. Keep it to one short question.",
        "Keep every reply short. This is production; be precise, not chatty.",
        "",
        "Worked examples:",
        "- User: 'who can write?' -> call list_users, then reply in words like 'Two can: alice on shop,"
        " and admin everywhere.' Do NOT ask a question back.",
        "- User: 'create user mamo, read on book, write on learn' -> call draft_operation with those two"
        " grants. You already have the name and the access, so do NOT ask anything.",
        "- User: 'create a user in book' -> call ask_user 'What should I name them?'. Only the name is"
        " missing; do not ask about the password or privileges.",
        "",
        f"Context: engine={engine}, environment={env}, page={page}, selected={selection}, read-only={ro}.",
        f"Security audits available here: {audit_line}.",
    ]
    if persona:
        lines += ["", f"The user also asked you to: {persona}"]
    return "\n".join(lines)


def _connection(body):
    """The connection context handlers need, without the AI or control fields."""
    drop = {"messages", "ai_provider", "ai_key", "ai_model", "persona",
            "context", "_audits", "read_only"}
    conn = {k: v for k, v in body.items() if k not in drop}
    conn["read_only"] = bool(body.get("read_only"))  # keep it, harmless for reads
    return conn


def _neutral_messages(raw):
    """Trust but bound: take the recent thread, keep only the shapes we expect."""
    out = []
    for m in raw[-MAX_MSGS:]:
        role = m.get("role")
        if role in ("user", "assistant") and isinstance(m.get("content"), str):
            out.append({"role": role, "content": m["content"][:8000]})
    return out


def _run_tool(tc, conn, routes):
    """Dispatch one read-only tool call to its handler and return the result."""
    spec = _BY_NAME.get(tc.name)
    if not spec or spec["route"] not in READONLY_ROUTES:
        return {"error": f"Unknown or non-readonly tool: {tc.name}"}
    args = dict(tc.args or {})
    if tc.name == "browse":  # map the friendly arg onto what _target expects
        name = args.pop("name", "")
        args["table"] = name
        args["collection"] = name
        try:
            args["limit"] = max(1, min(int(args.get("limit", 20)), 50))
        except (TypeError, ValueError):
            args["limit"] = 20
    handler = routes.get(spec["route"])
    if handler is None:
        return {"error": "handler unavailable"}
    sub = {**conn, **args}
    try:
        return handler(sub)
    except Exception as e:  # a tool blowing up shouldn't kill the whole turn
        return {"error": str(e)}


def chat_turn(body, routes):
    """Run one assistant turn: talk to the model, run any read-only tools it asks
    for, loop until it answers or proposes something. Returns one of:
      {"reply": text, "steps": [...]}              a plain answer
      {"question": {...}, "steps": [...]}          it needs input from the user
      {"draft": {...}, "steps": [...]}             a proposed operation to preview
      {"error": "..."}                             something went wrong
    """
    provider_name = body.get("ai_provider", "")
    key = body.get("ai_key", "")
    model = body.get("ai_model", "")
    try:
        provider = get_provider(provider_name, key, model)
    except AIError as e:
        return {"error": str(e)}
    if provider.needs_key and not key:
        return {"error": "No API key. Add one in Settings to switch the assistant on."}
    if not model:
        return {"error": "No model selected. Pick one in Settings."}

    system = _system_prompt(body)
    conn = _connection(body)
    messages = _neutral_messages(body.get("messages", []))
    if not messages:
        return {"error": "Nothing to answer."}
    steps = []
    seen = set()   # tool-call signatures we've already run, to stop small models looping
    # Strong models get the full kit; weak on-device ones skip ask_user (they
    # tend to misuse it, echoing the user's own question back).
    decls = TOOLS + VIRTUAL_TOOLS if provider.strong else TOOLS + [_BY_VNAME["draft_operation"]]

    for _ in range(MAX_HOPS):
        try:
            result = provider.chat(system, messages, decls)
        except AIError as e:
            return {"error": str(e), "steps": steps}

        if not result.tool_calls:
            reply = _clean_reply(result.text)
            if reply:
                return {"reply": reply, "steps": steps}
            break  # empty or malformed: synthesize a plain answer below

        # A virtual tool ends the turn with a payload for the panel.
        for tc in result.tool_calls:
            if tc.name in _VIRTUAL:
                key_out = "question" if tc.name == "ask_user" else "draft"
                return {key_out: tc.args or {}, "steps": steps, "note": _clean_reply(result.text)}

        # Otherwise run the read-only tools and feed the results back.
        messages.append({"role": "assistant", "content": result.text, "tool_calls": result.tool_calls})
        for tc in result.tool_calls:
            sig = (tc.name, json.dumps(tc.args, sort_keys=True, default=str))
            if sig in seen:
                out = {"note": "you already fetched this above; use it to answer now"}
            else:
                seen.add(sig)
                out = _run_tool(tc, conn, routes)
                steps.append({"tool": tc.name, "args": tc.args})
            messages.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name,
                             "content": json.dumps(out, default=str)[:MAX_TOOL_CHARS]})

    # Ran out of hops or got a blank answer: force a plain-words reply (no tools).
    try:
        final = provider.chat(system + "\n\nNow answer the user in plain words using what you already"
                              " learned. Do not call any tool.", messages, None)
        reply = _clean_reply(final.text)
        if reply:
            return {"reply": reply, "steps": steps}
    except AIError:
        pass
    return {"reply": "I had a look but couldn't put together a clean answer. Mind rephrasing?", "steps": steps}


def _clean_reply(text):
    """Small models sometimes leak a raw 'functions.name:' token instead of an
    answer. Treat that (and empties) as no answer."""
    t = (text or "").strip()
    if not t or t.startswith("functions.") or t.startswith("functions ") or t == "()":
        return ""
    return t


def list_models(body):
    """Handler for /api/ai/models: what can this provider actually use right now.
    For remote providers that's what the key unlocks; for on-device it's the
    catalog with each size marked downloaded / in-progress / not yet."""
    try:
        provider = get_provider(body.get("ai_provider", ""), body.get("ai_key", ""))
        return {"models": provider.list_models()}
    except AIError as e:
        return {"error": str(e)}


def download_model(body):
    """Handler for /api/ai/download: start fetching one on-device model size (or
    report where an in-flight one is). The download runs in the background; the
    UI polls /api/ai/models to watch the bar."""
    try:
        local.start_download(body.get("tier", ""))
    except AIError as e:
        return {"error": str(e)}
    return {"ok": True, "state": local.download_state().get(body.get("tier", ""), {})}


def check_machine(_body=None):
    """Handler for /api/ai/check: look at this machine and say which size fits."""
    return local.machine_report()
