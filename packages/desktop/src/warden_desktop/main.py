#!/usr/bin/env python3
"""
warden desktop: the web UI in a native window.

Starts the warden web server on a random localhost port in a background
thread, then opens it in a native window (pywebview / system WebKit).
"""

import os
import socket
import sys
import threading
import traceback
from pathlib import Path

from warden_web.server import create_server

CRASH_LOG = Path.home() / ".warden_desktop.log"


def _guard_stdio():
    # In a PyInstaller --windowed bundle stdout/stderr are detached (None);
    # http.server request logging writes to stderr and must not crash.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# Fixed port: localStorage is per-origin (host + port), so a random port each
# launch would silently reset saved credentials, envs, and audit results.
DESKTOP_PORT = int(os.environ.get("WARDEN_DESKTOP_PORT", "8647"))


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

    try:
        port = DESKTOP_PORT
        server = create_server("127.0.0.1", port)
    except OSError:
        port = _free_port()
        server = create_server("127.0.0.1", port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"warden desktop serving on http://127.0.0.1:{port}", flush=True)

    webview.create_window(
        "warden",
        f"http://127.0.0.1:{port}",
        width=1440,
        height=940,
        min_size=(920, 600),
    )
    # pywebview defaults to private mode, which wipes localStorage (saved
    # credentials, envs, audit results) on every launch. Persist it instead.
    storage = Path.home() / ".warden" / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    try:
        webview.start(private_mode=False, storage_path=str(storage))
    except TypeError:  # older pywebview without storage_path
        webview.start(private_mode=False)
    server.shutdown()


if __name__ == "__main__":
    main()
