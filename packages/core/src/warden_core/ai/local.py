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
CATALOG = {
    "nano": {
        "label": "Nano", "does": "chat & explain", "size": "~0.5 GB",
        "repo": "bartowski/Qwen2.5-0.5B-Instruct-GGUF",
        "file": "Qwen2.5-0.5B-Instruct-Q4_K_M.gguf",
    },
    "small": {
        "label": "Small", "does": "look things up & draft", "size": "~1 GB",
        "repo": "bartowski/Qwen2.5-1.5B-Instruct-GGUF",
        "file": "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf",
    },
    "medium": {
        "label": "Medium", "does": "reports & reasoning", "size": "~2 GB",
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
            _loaded["llm"] = Llama(model_path=path, n_ctx=8192, n_gpu_layers=-1,
                                   chat_format="chatml-function-calling", verbose=False)
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
            row = {"id": tier, "label": f"{c['label']} ({c['does']})",
                   "size": c["size"], "downloaded": is_downloaded(tier)}
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

        chat_messages = _to_openai(system, messages)
        kwargs = {"messages": chat_messages, "temperature": 0.3, "max_tokens": 1024}
        if tools:
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
            kwargs["tool_choice"] = "auto"
        try:
            resp = llm.create_chat_completion(**kwargs)
        except Exception as e:
            raise AIError(f"The local model errored: {e}")

        msg = resp["choices"][0]["message"]
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), args=args))
        return ChatResult(text=(msg.get("content") or "").strip(), tool_calls=calls)


def _to_openai(system, messages):
    """Neutral messages -> the OpenAI-style shape llama.cpp's chat handler wants."""
    out = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m.get("content", "")})
        elif role == "assistant":
            entry = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                entry["tool_calls"] = [{
                    "id": tc.id, "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
                } for tc in m["tool_calls"]]
                entry["content"] = None
            out.append(entry)
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": m.get("tool_call_id", ""),
                        "content": m.get("content", "")})
    return out
