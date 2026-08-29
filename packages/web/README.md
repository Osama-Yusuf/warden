# web (`warden_web`)

The door itself: an HTTP server plus the single-page UI. It's `core` with a face.

- **[`src/warden_web/server.py`](src/warden_web/server.py)** the whole backend. A stdlib `http.server` (no Flask, no FastAPI, no framework tax) with a `ROUTES` table of `api_*` handlers. Each handler is thin: build an adapter, call a method, wrap the answer. The engine-specific brains live in `core/adapters`, not here.
- **[`src/warden_web/static/`](src/warden_web/static/)** the front end. index.html + split CSS/JS, served straight off disk.

### Things worth knowing before you touch it
- **Handlers are thin on purpose.** If you're adding engine logic to `server.py`, it probably belongs in an adapter. The three deferred handlers (`api_query`, `api_audit_run`, the query stream) are the exceptions, they're too web-coupled to move.
- **The front end reads the JSON body, not the HTTP status.** So an error is `{"error": "..."}`, and that's what the UI shows.
- **Seatbelts live here:** `read_only` gating (`RO_BLOCKED_ROUTES`), the creds gate (`REQUIRES_CREDS`), and `_same_site()` (refuses cross-origin so a random web page can't drive your server).

Run it: `uv run warden-web`, open `http://localhost:8642`.
