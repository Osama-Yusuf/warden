#!/usr/bin/env python3
"""
warden desktop: the web UI in a native window.

Starts the warden web server on localhost:8647 in a background
thread, then opens it in a native window (pywebview / system WebKit).
"""

import faulthandler
import json
import os
import sys
import threading
import traceback
import urllib.request
from pathlib import Path

from warden_web.server import create_server

CRASH_LOG = Path.home() / ".warden_desktop.log"
APP_LOG = Path.home() / ".warden" / "desktop.log"


def _guard_stdio():
    # In a PyInstaller --windowed bundle stdout/stderr are detached (None).
    # Send them to a log file (http.server writes request logs to stderr), and
    # dump native crashes there too, so a dying app leaves a trace.
    if sys.stdout is not None and sys.stderr is not None:
        return
    APP_LOG.parent.mkdir(parents=True, exist_ok=True)
    if APP_LOG.exists() and APP_LOG.stat().st_size > 5_000_000:
        APP_LOG.replace(APP_LOG.with_suffix(".log.old"))
    log = open(APP_LOG, "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log
    faulthandler.enable(log)


def _warden_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=2) as r:
            return "environments" in json.load(r)
    except Exception:
        return False


# Fixed port: localStorage is per-origin (host + port), so a random port each
# launch would silently reset saved credentials, envs, and audit results.
DESKTOP_PORT = int(os.environ.get("WARDEN_DESKTOP_PORT", "8647"))


class _SaveApi:
    """Exposed to JS as window.pywebview.api; WKWebView can't download blobs."""

    def save_file(self, name, text):
        import webview

        picked = webview.windows[0].create_file_dialog(webview.FileDialog.SAVE, save_filename=name)
        if not picked:
            return False
        path = picked if isinstance(picked, str) else picked[0]
        Path(path).write_text(text, encoding="utf-8")
        return True


def main():
    _guard_stdio()
    try:
        _run()
    except Exception:
        CRASH_LOG.write_text(traceback.format_exc())
        raise


def _run():
    try:
        import webview
    except ImportError:
        print("pywebview is not installed. Run:  make setup  (or: uv sync)")
        sys.exit(1)

    port = DESKTOP_PORT
    server = None
    try:
        server = create_server("127.0.0.1", port)
    except OSError:
        # A random port would be a new origin with empty storage (no saved
        # connections). Reuse a warden already serving there, else say so.
        if not _warden_running(port):
            webview.create_window("warden", html=(
                f"<body style='font:15px -apple-system;padding:40px'><h3>Port {port} is in use</h3>"
                f"<p>Another program holds port {port}, which warden needs to find its saved data. "
                f"Close it (<code>lsof -i :{port}</code> shows which), then reopen warden.</p></body>"),
                width=560, height=260)
            webview.start()
            return
    if server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"warden desktop on http://127.0.0.1:{port}", flush=True)

    webview.create_window(
        "warden",
        f"http://127.0.0.1:{port}",
        width=1440,
        height=940,
        min_size=(920, 600),
        js_api=_SaveApi(),
    )
    # pywebview defaults to private mode, which wipes localStorage (saved
    # credentials, envs, audit results) on every launch. Persist it instead.
    storage = Path.home() / ".warden" / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    try:
        webview.start(private_mode=False, storage_path=str(storage))
    except TypeError:  # older pywebview without storage_path
        webview.start(private_mode=False)
    if server:
        server.shutdown()


if __name__ == "__main__":
    main()
