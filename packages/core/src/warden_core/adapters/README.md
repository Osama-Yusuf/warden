# adapters

One class per engine, so the rest of warden never has to write `if engine == "mysql"` ever again. The web/CLI layer asks an adapter to "list users" or "browse a page" and doesn't care what's behind the door.

### The cast
- **`base.py`** the contract. `EngineAdapter` (methods every engine *might* implement), `Target` (what a browse/CRUD points at), `Mutation` (what a write hands back), and the `get_adapter(engine, ...)` factory. Also `EngineError` / `NotSupported`, because errors are **raised**, not returned.
- **`postgres.py` `mysql.py` `mongo.py` `elasticsearch.py` `redis.py` `sqlite.py`** the actual engines. Each wraps the drivers over in `warden_core`, it does not reinvent the SQL.

### How it works, in one breath
`get_adapter()` uses `engine_family()` to pick the class, you call a method, it either hands back the data or raises. Write ops (create user, grant, insert row, ...) return a `Mutation(action, detail, response)` so the caller writes the audit line and the `{"ok": true, ...}` envelope without knowing a single engine detail.

### Adding an engine (the fun part)
1. Write `yourengine.py`: `@register class YourAdapter(EngineAdapter)` with `family = "..."`.
2. Only implement what it can do. Leave the rest, `base` returns a polite `NotSupported`.
3. Import it in `__init__.py` (that's what fires `@register`). Skip this and it stays a ghost.
4. Return the **inner value** (a list, a dict). The handler adds the envelope + audit.

### House rules
- Validate your inputs (mongo/redis do it inline, the SQL ones lean on `validate_ident`). The handlers trust you.
- If a native driver might be missing, guard it and degrade with a friendly message instead of a 500. Ask postgres' `browse` how it learned that lesson.
