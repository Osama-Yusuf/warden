# cli (`warden_cli`)

warden without the pretty face. Same brains as the web app (it's all `core` underneath), just talking to your terminal instead of a browser.

- **[`src/warden_cli/main.py`](src/warden_cli/main.py)** the whole thing: argument parsing, the commands, and the table printing.

Nothing fancy, no click/typer/rich, just stdlib and `core`. If you're adding a command, wire it into the parser here and call the same `core` helpers the web handlers use, so the two faces never disagree about what a "grant" means.

Run it: `uv run warden` (or `uv run warden --help` to see what it can do).
