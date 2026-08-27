#!/usr/bin/env python3
"""
Dev runner with auto-reload. Watches packages/**/*.py and restarts the target
module on change.

Targets:
  web (default)  restarts warden_web.server; the UI (static/index.html) is read
                 from disk per request, so frontend edits only need a browser
                 refresh, no restart needed.
  desktop        restarts warden_desktop.main (window reopens); also watches
                 static/index.html since the window can't hot-reload it.

Run via:  make dev          (extra args pass through: make dev ARGS="--port 9000")
          make dev-desktop
"""

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TARGETS = {
    "web": "warden_web.server",
    "desktop": "warden_desktop.main",
}


def snapshot(watch_static):
    state = {}
    for path in (ROOT / "packages").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            state[path] = path.stat().st_mtime
        except OSError:
            continue
    if watch_static:
        for path in (ROOT / "packages").rglob("static/*"):
            try:
                state[path] = path.stat().st_mtime
            except OSError:
                continue
    return state


def spawn(module, args):
    return subprocess.Popen([sys.executable, "-m", module, *args])


def stop(proc, sig=signal.SIGTERM):
    if proc.poll() is None:
        proc.send_signal(sig)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    parser = argparse.ArgumentParser(prog="dev.py")
    parser.add_argument("--target", choices=TARGETS, default="web")
    args, passthrough = parser.parse_known_args()
    module = TARGETS[args.target]
    watch_static = args.target == "desktop"

    watched = "packages/**/*.py" + (" + static/*" if watch_static else " (UI is live-reload by design)")
    print(f"dev: running {module}, watching {watched}", flush=True)
    proc = spawn(module, passthrough)
    state = snapshot(watch_static)
    try:
        while True:
            time.sleep(0.7)
            if proc.poll() is not None:
                print(f"dev: target exited with code {proc.returncode}; waiting for a change to restart…", flush=True)
                while snapshot(watch_static) == state:
                    time.sleep(0.7)
            current = snapshot(watch_static)
            if current != state:
                changed = [
                    str(p.relative_to(ROOT))
                    for p in set(current) | set(state)
                    if state.get(p) != current.get(p)
                ]
                print(f"dev: change in {', '.join(sorted(changed)[:4])}, restarting", flush=True)
                state = current
                stop(proc)
                proc = spawn(module, passthrough)
    except KeyboardInterrupt:
        stop(proc, signal.SIGINT)
        print("\ndev: stopped", flush=True)


if __name__ == "__main__":
    main()
