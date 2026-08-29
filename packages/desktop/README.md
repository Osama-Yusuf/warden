# desktop (`warden_desktop`)

The web app wearing a native coat. There's barely any code here, and that's the point.

- **[`src/warden_desktop/main.py`](src/warden_desktop/main.py)** starts the `warden_web` server on a fixed localhost port in a background thread, then points a pywebview window at it. Roughly 60 lines.
- **[`assets/`](assets/)** the app icons (`.icns`, `.ico`, the 1024px PNG).

### Two things that will bite you if you don't know them
- **The port is fixed (8647), not random.** localStorage is per-origin (host + port), so a random port each launch would silently forget your saved connections, envs, and audit results every time. Don't "helpfully" randomize it.
- **pywebview runs in private mode by default**, which also wipes localStorage. `main.py` forces `private_mode=False` with a real storage path so your stuff survives a restart.

### Building the `.app`
CI does it on a tag push (`.github/workflows/build.yml`). Locally: `uv sync`, `uv pip install pyinstaller`, then the PyInstaller command from that workflow. The whole `static/` tree gets bundled via `--add-data`, so the split CSS/JS ride along automatically.
