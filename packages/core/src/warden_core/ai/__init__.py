"""warden's AI layer. Off by default, invisible until switched on.

One provider interface, one class per backend. Gemini is wired up first; Claude,
GPT, Ollama, and a bundled local model slot in behind the same interface without
anything above this package noticing.
"""
from __future__ import annotations

from .base import AIError, ChatResult, Provider, ToolCall
from .gemini import GeminiProvider
from .local import LocalProvider

_PROVIDERS = {
    "gemini": GeminiProvider,
    "local": LocalProvider,
}


def get_provider(name, api_key="", model=""):
    """Build the provider for this name, or raise AIError if we don't have one."""
    cls = _PROVIDERS.get((name or "").strip().lower())
    if cls is None:
        raise AIError(f"Unknown AI provider: {name!r}")
    return cls(api_key=api_key, model=model)


def providers():
    """The provider names warden currently knows how to drive."""
    return sorted(_PROVIDERS)


__all__ = ["AIError", "ChatResult", "Provider", "ToolCall", "get_provider", "providers"]
