# tools

The odd-jobs drawer. Little dev scripts that aren't part of the app itself.

- **`dev.py`** the auto-reload dev runner. Watches `packages/**/*.py` and restarts the server (or the desktop window) when you save. Frontend edits don't even need it, `index.html` is read off disk per request, so just refresh the browser. Run via `make dev` (or `make dev-desktop`).
- **`make-icon.py`** regenerates the app icons (macOS `.icns` + Windows `.ico`) from the blue-cylinder favicon. The outputs are committed, so you only run this if the logo changes: `uv run --with pillow python tools/make-icon.py`.

That's it. If you add a one-off script that helps develop warden but isn't shipped, it belongs here.
