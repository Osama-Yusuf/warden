"""On-device models: download one, run it, done. No key, no Ollama, no terminal.

warden downloads a small GGUF from Hugging Face into ~/.warden/models and runs it
in-process with llama.cpp (Metal on a Mac, CPU elsewhere). Three sizes, labelled
by what they can actually do rather than by a number, so nobody picks the tiny
one and expects a research report.

llama-cpp-python is an optional extra (install with `warden[local]`). If it isn't
there, this provider says so plainly instead of blowing up.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from pathlib import Path

from .base import AIError, ChatResult, Provider, ToolCall

try:
    from llama_cpp import Llama
    HAVE_LLAMA = True
except ImportError:  # pragma: no cover
    HAVE_LLAMA = False

MODELS_DIR = Path.home() / ".warden" / "models"

# The download-and-go catalog. Same family (Qwen2.5 instruct) at three sizes, so
# behaviour is consistent and only the capability changes.
# Sizes labelled by what they can actually do, with an honest one-liner (shown
# in the UI) so nobody's surprised. Qwen2.5 instruct at three sizes; behaviour is
# consistent, only the capability grows.
CATALOG = {
    "nano": {
        "label": "Nano", "size": "~0.4 GB",
        "note": "Tiny and instant. Handles a simple ask or draft, but gets muddled easily. A quick taste of Ward.",
        "repo": "bartowski/Qwen2.5-0.5B-Instruct-GGUF",
        "file": "Qwen2.5-0.5B-Instruct-Q4_K_M.gguf",
    },
    "small": {
        "label": "Small", "size": "~1 GB",
        "note": "Chats and drafts changes nicely. Reads are usually right, just a touch over-eager sometimes.",
        "repo": "bartowski/Qwen2.5-1.5B-Instruct-GGUF",
        "file": "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf",
    },
    "medium": {
        "label": "Medium", "size": "~2 GB", "recommended": True,
        "note": "The dependable one. Reads results, writes reports, drafts changes. Best pick for a laptop.",
        "repo": "bartowski/Qwen2.5-3B-Instruct-GGUF",
        "file": "Qwen2.5-3B-Instruct-Q4_K_M.gguf",
    },
}


def model_path(tier):
    return MODELS_DIR / CATALOG[tier]["file"]


def is_downloaded(tier):
    return tier in CATALOG and model_path(tier).exists()


def _download_url(tier):
    c = CATALOG[tier]
    return f"https://huggingface.co/{c['repo']}/resolve/main/{c['file']}"


# ── download manager ─────────────────────────────────────────────────────────
# One download at a time, progress tracked so the UI can show a bar. Downloads
# to a .part file and renames on success, so a half-finished file never looks
# "downloaded".
_dl = {}                       # tier -> {status, pct, done, total, error}
_dl_lock = threading.Lock()


def download_state():
    with _dl_lock:
        return {t: dict(s) for t, s in _dl.items()}


def start_download(tier):
    if tier not in CATALOG:
        raise AIError(f"Unknown model size: {tier}")
    with _dl_lock:
        cur = _dl.get(tier)
        if cur and cur.get("status") == "downloading":
            return  # already going
        _dl[tier] = {"status": "downloading", "pct": 0, "done": 0, "total": 0, "error": ""}
    threading.Thread(target=_run_download, args=(tier,), daemon=True).start()


def _set(tier, **patch):
    with _dl_lock:
        _dl.setdefault(tier, {}).update(patch)


def _run_download(tier):
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    dst = model_path(tier)
    tmp = dst.with_suffix(dst.suffix + ".part")
    try:
        def hook(block, block_size, total):
            done = block * block_size
            _set(tier, done=done, total=total,
                 pct=round(done / total * 100, 1) if total > 0 else 0)
        urllib.request.urlretrieve(_download_url(tier), tmp, hook)
        os.replace(tmp, dst)
        _set(tier, status="done", pct=100)
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        _set(tier, status="error", error=str(e))


# ── inference ────────────────────────────────────────────────────────────────
# Loading a model costs a few seconds and a chunk of RAM, so keep exactly one
# loaded and swap when the tier changes.
_loaded = {"path": None, "llm": None}
_load_lock = threading.Lock()


def _llm_for(path):
    with _load_lock:
        if _loaded["path"] != path:
            # Plain ChatML (Qwen's native format). We don't use llama.cpp's
            # function-calling handler; tool use is driven by a JSON-schema
            # grammar instead, which small models follow far more reliably.
            _loaded["llm"] = Llama(model_path=path, n_ctx=8192, n_gpu_layers=-1,
                                   chat_format="chatml", verbose=False)
            _loaded["path"] = path
        return _loaded["llm"]


class LocalProvider(Provider):
    name = "local"
    needs_key = False
    strong = False   # small on-device models: give them fewer, simpler tools

    def list_models(self):
        """The catalog, each marked with whether it's downloaded and any progress."""
        prog = download_state()
        out = []
        for tier, c in CATALOG.items():
            row = {"id": tier, "label": c["label"], "size": c["size"],
                   "note": c.get("note", ""), "recommended": c.get("recommended", False),
                   "downloaded": is_downloaded(tier)}
            if tier in prog:
                row["download"] = prog[tier]
            out.append(row)
        return out

    def chat(self, system, messages, tools=None):
        if not HAVE_LLAMA:
            raise AIError("On-device models need the llama-cpp-python engine (install warden's 'local' extra).")
        tier = self.model or "small"
        if not is_downloaded(tier):
            raise AIError(f"The {tier} model isn't downloaded yet.")
        llm = _llm_for(str(model_path(tier)))

        # We don't hand a small model open-ended function-calling. Instead we ask
        # for ONE decision as JSON, constrained by a grammar so it's always valid
        # and always one of a fixed set of actions. The model just fills a form.
        read = [t for t in (tools or []) if t["name"] not in ("draft_operation", "ask_user")]
        names = [t["name"] for t in read]
        convo = [{"role": "system", "content": _routing_prompt(system, read)}] + _render(messages)
        try:
            resp = llm.create_chat_completion(
                messages=convo, temperature=0.2, max_tokens=768,
                response_format={"type": "json_object", "schema": _decision_schema(names)})
        except Exception as e:
            raise AIError(f"The local model errored: {e}")

        raw = (resp["choices"][0]["message"].get("content") or "").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return ChatResult(text=raw)  # grammar should prevent this, but be safe
        return _decision_to_result(data, names)


def _routing_prompt(system, read_tools):
    lines = [system, "", "Tools you can call to look things up (read-only):"]
    for t in read_tools:
        props = (t.get("parameters") or {}).get("properties") or {}
        args = ", ".join(props) if props else "no arguments"
        lines.append(f"- {t['name']}({args}): {t.get('description', '')}")
    lines += [
        "",
        "Reply with exactly ONE JSON object and nothing else. Choose one action:",
        '  {"action": "reply", "reply": "<your answer, in plain words>"}',
        '  {"action": "tool", "tool": "<a tool name above>", "args": { ... }}',
        '  {"action": "draft", "draft": {"summary": "<one line>", "statements": ["<step>", "..."]}}',
        '  {"action": "ask", "ask": {"question": "<one question>", "kind": "text"}}',
        "",
        "reply: to chat or to answer once you have what you need.",
        "tool: only when you still need data you don't have. After a tool result appears, switch to reply.",
        "draft: for any create / grant / revoke / change / delete. You never run it; warden shows a preview.",
        "ask: only when a required detail like a name is missing. Never ask about passwords or privileges.",
        "",
        "Examples:",
        '- "make user bob read-only on shop" -> {"action": "draft", "draft": {"summary": "Create bob with read on shop", "statements": ["create user bob", "grant read on shop to bob"]}}',
        '- "add a new collection gg to learn" -> {"action": "draft", "draft": {"summary": "Create collection gg in learn", "statements": ["create collection gg in learn"]}}',
        '- "add a user to shop" (no name given) -> {"action": "ask", "ask": {"question": "What should I name them?", "kind": "text"}}',
        '- "who are the admins?" -> {"action": "tool", "tool": "list_users", "args": {}}',
        '- (after a tool result is shown) -> {"action": "reply", "reply": "Two can write: alice on shop and the admin."}',
    ]
    return "\n".join(lines)


def _render(messages):
    """Neutral messages -> a plain chat transcript the routing model can read.
    Tool calls and their results become readable lines, not a wire protocol."""
    out = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m.get("content", "")})
        elif role == "assistant":
            if m.get("tool_calls"):
                looked = ", ".join(tc.name for tc in m["tool_calls"])
                out.append({"role": "assistant", "content": f"(looked up: {looked})"})
            elif m.get("content"):
                out.append({"role": "assistant", "content": m["content"]})
        elif role == "tool":
            out.append({"role": "user",
                        "content": f"Result of {m.get('name', 'tool')}: {m.get('content', '')}"})
    return out


def _decision_schema(tool_names):
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["reply", "tool", "draft", "ask"]},
            "reply": {"type": "string"},
            "tool": {"type": "string", "enum": tool_names or ["none"]},
            "args": {"type": "object"},
            "draft": {"type": "object", "properties": {
                "summary": {"type": "string"},
                "statements": {"type": "array", "items": {"type": "string"}}}},
            "ask": {"type": "object", "properties": {
                "question": {"type": "string"},
                "kind": {"type": "string", "enum": ["text", "select", "multiselect", "boolean"]},
                "options": {"type": "array", "items": {"type": "string"}},
                "hint": {"type": "string"}}},
        },
        "required": ["action"],
    }


def _decision_to_result(data, tool_names):
    action = data.get("action")
    if action == "tool" and data.get("tool") in tool_names:
        return ChatResult(tool_calls=[ToolCall(id="call_0", name=data["tool"], args=data.get("args") or {})])
    if action == "draft" and isinstance(data.get("draft"), dict):
        draft = dict(data["draft"])
        draft.setdefault("writes", True)
        return ChatResult(tool_calls=[ToolCall(id="draft_0", name="draft_operation", args=draft)])
    if action == "ask" and isinstance(data.get("ask"), dict):
        return ChatResult(tool_calls=[ToolCall(id="ask_0", name="ask_user", args=data["ask"])])
    return ChatResult(text=(data.get("reply") or "").strip())
