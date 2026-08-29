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

_ALL_DECLS = TOOLS + VIRTUAL_TOOLS
_VIRTUAL = {t["name"] for t in VIRTUAL_TOOLS}


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
        "Rules:",
        "- To answer a question, call the read tools. Never invent a user, database, table, or collection"
        " name; look it up first with list_users / list_databases / list_collections.",
        "- If the user asks to create, grant, revoke, change, or delete something, do NOT do it. Call"
        " draft_operation to lay out exactly what should happen, and warden lets the user run it. warden's"
        " defaults are fine: generate a password when none is given, and a user may have no privileges.",
        "- If you are missing something you truly cannot guess (like the name for a new user), call ask_user."
        " Do not ask about passwords or privileges.",
        "- Keep replies short. This is production; be precise, not chatty.",
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
    if not key:
        return {"error": "No API key. Add one in Settings to switch the assistant on."}
    if not model:
        return {"error": "No model selected. Pick one in Settings."}

    try:
        provider = get_provider(provider_name, key, model)
    except AIError as e:
        return {"error": str(e)}

    system = _system_prompt(body)
    conn = _connection(body)
    messages = _neutral_messages(body.get("messages", []))
    if not messages:
        return {"error": "Nothing to answer."}
    steps = []

    for _ in range(MAX_HOPS):
        try:
            result = provider.chat(system, messages, _ALL_DECLS)
        except AIError as e:
            return {"error": str(e), "steps": steps}

        if not result.tool_calls:
            return {"reply": result.text or "(no answer)", "steps": steps}

        # A virtual tool ends the turn with a payload for the panel.
        for tc in result.tool_calls:
            if tc.name in _VIRTUAL:
                key_out = "question" if tc.name == "ask_user" else "draft"
                return {key_out: tc.args or {}, "steps": steps,
                        "note": result.text or ""}

        # Otherwise run the read-only tools and feed the results back.
        messages.append({"role": "assistant", "content": result.text,
                         "tool_calls": result.tool_calls})
        for tc in result.tool_calls:
            out = _run_tool(tc, conn, routes)
            steps.append({"tool": tc.name, "args": tc.args})
            messages.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name,
                             "content": json.dumps(out, default=str)[:MAX_TOOL_CHARS]})

    return {"reply": "I kept reaching for tools without landing on an answer. Mind rephrasing?",
            "steps": steps}


def list_models(body):
    """Handler for /api/ai/models: what can this key actually use right now."""
    try:
        provider = get_provider(body.get("ai_provider", ""), body.get("ai_key", ""))
        return {"models": provider.list_models()}
    except AIError as e:
        return {"error": str(e)}
