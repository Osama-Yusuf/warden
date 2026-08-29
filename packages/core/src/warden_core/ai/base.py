"""The provider interface. One class per AI backend (Gemini today, Claude and
GPT later, Ollama and a bundled model after that), same shape for all of them,
so the assistant never cares who's actually answering.

Think of it as the engine-adapter pattern, pointed at language models instead of
databases: the orchestrator hands over a system prompt, the conversation so far,
and the tools it's allowed to call, and gets back either some text or a request
to run a tool. That's the whole contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class AIError(Exception):
    """Anything the provider can't recover from: bad key, dead model, a network
    blip, a response we can't parse. The web layer turns it into a plain
    {"error": ...} the chat panel already knows how to show."""


@dataclass
class ToolCall:
    """The model asking to run one tool. `id` ties the eventual result back to
    this call (some providers require it); `name` is the tool, `args` the filled
    form. The model never runs anything itself, it just asks."""
    id: str
    name: str
    args: dict


@dataclass
class ChatResult:
    """What one round-trip to the model gives back: some text to show, and/or a
    list of tools it wants run. Both can be empty (rare), both can be present."""
    text: str = ""
    tool_calls: list = field(default_factory=list)  # list[ToolCall]


# Neutral message shape the orchestrator speaks, and every provider translates
# into its own wire format:
#   {"role": "user",      "content": "..."}
#   {"role": "assistant", "content": "...", "tool_calls": [ToolCall, ...]}
#   {"role": "tool",      "tool_call_id": "...", "name": "...", "content": "..."}
#
# Tool declarations are the neutral form of "here's a tool you may call":
#   {"name": "...", "description": "...", "parameters": {<JSON Schema object>}}


class Provider:
    """Base class. Subclasses fill in list_models() and chat()."""

    name = ""

    def __init__(self, api_key="", model=""):
        self.api_key = api_key
        self.model = model

    def list_models(self):
        """Return the models this key can actually use right now, newest-useful
        first, as [{"id": ..., "label": ...}]. Fetched live so a deprecated name
        never rots in a dropdown."""
        raise NotImplementedError

    def chat(self, system, messages, tools=None):
        """One turn. Given the system prompt, the conversation, and the allowed
        tools, return a ChatResult. Raise AIError on anything unrecoverable."""
        raise NotImplementedError
