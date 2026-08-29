# ai

warden's language-model layer. Off by default, invisible until someone switches
it on. This is the engine-adapter pattern pointed at LLMs instead of databases:
one interface, one class per backend, and nothing above here cares who's actually
answering.

- **`base.py`** the contract. `Provider` (list_models + chat), plus the neutral
  types everything speaks: `ToolCall`, `ChatResult`, and the message shape. Read
  this first.
- **`gemini.py`** the first backend, Google Gemini over its REST API (pooled
  urllib3, no SDK). It's the only file that knows Gemini's `contents`/`parts`
  wire format; everything else stays neutral.
- **`__init__.py`** the `get_provider(name, key, model)` factory.

### The important part
This package only talks to models. It does **not** run database operations, and
it does not import the web server. The orchestration (which read-only tools the
model may call, and dispatching them) lives up in
[`warden_web/assistant.py`](../../../../../web/src/warden_web/assistant.py), so
the safety spine stays in one place: the model proposes, warden disposes.

### Adding a provider (Claude, GPT, Ollama, a bundled model)
1. Write `yourprovider.py` with a `Provider` subclass: `list_models()` and
   `chat(system, messages, tools)`. Translate the neutral message shape into
   whatever wire format it wants, and back.
2. Register it in `__init__.py`. Done, the assistant picks it up for free.

Keep the freeform-SQL door shut: a model fills validated tool args, it never
hands back a statement that runs. That's what keeps a tiny local model safe.
