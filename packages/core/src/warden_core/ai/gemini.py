"""Google Gemini, over its REST API. No SDK, just a pooled urllib3 client, the
same way warden talks to Elasticsearch. Gemini's free tier makes it the cheapest
way to kick the tyres, which is why it's the first provider wired up.

The wire format is Gemini's own (contents / parts / functionCall), and this file
is the only place that knows about it. Everything above speaks the neutral
message shape from base.py.
"""
from __future__ import annotations

import json
import threading

from .base import AIError, ChatResult, Provider, ToolCall

try:
    import urllib3
    urllib3.disable_warnings()
    HAVE_URLLIB3 = True
except ImportError:  # pragma: no cover
    HAVE_URLLIB3 = False

_BASE = "https://generativelanguage.googleapis.com/v1beta"

_pool = None
_pool_lock = threading.Lock()


def _http():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = urllib3.PoolManager(
                    retries=urllib3.Retry(2, redirect=False),
                    timeout=urllib3.Timeout(connect=6, read=60), maxsize=8)
    return _pool


def _model_path(model):
    """Gemini wants 'models/gemini-...'; accept it with or without the prefix."""
    m = (model or "").strip()
    return m if m.startswith("models/") else f"models/{m}"


class GeminiProvider(Provider):
    name = "gemini"

    def _request(self, method, url, body=None):
        if not HAVE_URLLIB3:
            raise AIError("The AI assistant needs the native HTTP driver (urllib3)")
        try:
            r = _http().request(
                method, url, headers={"Content-Type": "application/json"},
                body=json.dumps(body).encode() if body is not None else None)
        except Exception as e:  # network blip, DNS, timeout
            raise AIError(f"Could not reach Gemini: {e}")
        text = r.data.decode("utf-8", "replace")
        try:
            data = json.loads(text) if text[:1] in ("{", "[") else {"raw": text}
        except json.JSONDecodeError:
            raise AIError(f"Gemini sent back something unparseable (HTTP {r.status})")
        if r.status >= 400:
            msg = data.get("error", {}).get("message", text[:300]) if isinstance(data, dict) else text[:300]
            # The key is the thing most likely to be wrong, so name it.
            if r.status in (400, 401, 403) and "key" in msg.lower():
                raise AIError(f"Gemini rejected the API key: {msg}")
            raise AIError(f"Gemini error (HTTP {r.status}): {msg}")
        return data

    def list_models(self):
        data = self._request("GET", f"{_BASE}/models?key={self.api_key}")
        out = []
        for m in data.get("models", []):
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" not in methods:
                continue  # embeddings and the like, not for chat
            mid = m.get("name", "").split("/", 1)[-1]
            if not mid:
                continue
            out.append({"id": mid, "label": m.get("displayName") or mid})
        # flash/newest models tend to sort last by name, so surface them first
        out.sort(key=lambda x: x["id"], reverse=True)
        return out

    def chat(self, system, messages, tools=None):
        payload = {"contents": _to_contents(messages)}
        if system:
            payload["system_instruction"] = {"parts": [{"text": system}]}
        if tools:
            payload["tools"] = [{"functionDeclarations": [_to_decl(t) for t in tools]}]
        url = f"{_BASE}/{_model_path(self.model)}:generateContent?key={self.api_key}"
        data = self._request("POST", url, payload)

        cands = data.get("candidates") or []
        if not cands:
            # Usually a safety block or an empty finish; say so rather than a blank bubble.
            fb = data.get("promptFeedback", {}).get("blockReason")
            raise AIError(f"Gemini returned nothing{f' ({fb})' if fb else ''}")
        parts = cands[0].get("content", {}).get("parts", []) or []
        text_bits, calls = [], []
        for i, p in enumerate(parts):
            if "text" in p:
                text_bits.append(p["text"])
            elif "functionCall" in p:
                fc = p["functionCall"]
                calls.append(ToolCall(id=f"call_{i}", name=fc.get("name", ""),
                                      args=fc.get("args", {}) or {}))
        return ChatResult(text="".join(text_bits).strip(), tool_calls=calls)


def _to_contents(messages):
    """Neutral messages -> Gemini contents. Assistant becomes 'model'; a tool
    result becomes a 'user' turn carrying a functionResponse (Gemini's REST
    convention)."""
    contents = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            contents.append({"role": "user", "parts": [{"text": m.get("content", "")}]})
        elif role == "assistant":
            parts = []
            if m.get("content"):
                parts.append({"text": m["content"]})
            for tc in m.get("tool_calls", []):
                parts.append({"functionCall": {"name": tc.name, "args": tc.args}})
            contents.append({"role": "model", "parts": parts or [{"text": ""}]})
        elif role == "tool":
            contents.append({"role": "user", "parts": [{"functionResponse": {
                "name": m.get("name", ""),
                "response": _as_struct(m.get("content", "")),
            }}]})
    return contents


def _as_struct(content):
    """functionResponse.response must be an object. Pass a JSON object through,
    otherwise wrap it."""
    if isinstance(content, dict):
        return content
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    except (json.JSONDecodeError, TypeError):
        return {"result": content}


def _to_decl(tool):
    """Neutral tool declaration -> Gemini functionDeclaration."""
    decl = {"name": tool["name"], "description": tool.get("description", "")}
    params = tool.get("parameters")
    if params and params.get("properties"):
        decl["parameters"] = params  # already JSON Schema shaped
    return decl
