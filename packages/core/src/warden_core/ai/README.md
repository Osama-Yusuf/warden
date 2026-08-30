# ai

The brain behind **Ward** (warden's assistant). Off by default, invisible until
someone switches it on. This is the engine-adapter pattern pointed at language
models instead of databases: one interface, one class per backend, and nothing
above here cares who's actually answering.

- **`base.py`** the contract. `Provider` (list_models + chat), plus the neutral
  types everything speaks: `ToolCall`, `ChatResult`, and the message shape. Also
  two capability flags: `needs_key` (remote vs on-device) and `strong` (whether
  the model can be trusted with nuanced tools). Read this first.
- **`gemini.py`** a remote backend, Google Gemini over its REST API (pooled
  urllib3, no SDK). The only file that knows Gemini's `contents`/`parts` wire
  format; everything else stays neutral.
- **`local.py`** the download-and-go backend. Pulls a small GGUF from Hugging
  Face into `~/.warden/models` and runs it in-process with llama.cpp. `strong`
  is False here, so small on-device models get a simpler toolset. Needs the
  optional `local` extra (`llama-cpp-python`); degrades to a plain message if
  it's missing.
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
