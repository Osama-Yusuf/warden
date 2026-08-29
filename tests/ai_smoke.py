#!/usr/bin/env python3
"""Live Gemini smoke test. Needs a real API key, so keep it in your shell, never
in the repo or a chat window:

    GEMINI_API_KEY=your-key uv run python tests/ai_smoke.py

It checks three things end to end against the real API:
  1. listing the models the key can use
  2. a plain chat turn through the orchestrator
  3. one read-only tool round-trip (with a fake list_users handler), which is
     what proves Gemini's function-call wire format is wired up right

Not a pytest test on purpose (it costs a call and needs a credential).
"""
import os
import sys

from warden_core.ai import get_provider
from warden_web import assistant


def main():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("Set GEMINI_API_KEY in your shell first (it never touches the repo).")

    prov = get_provider("gemini", key)
    models = prov.list_models()
    print(f"[1] models: {len(models)} usable; first few = {[m['id'] for m in models[:5]]}")
    if not models:
        sys.exit("No usable models came back. Is the key valid / the API enabled?")
    model = next((m["id"] for m in models if "flash" in m["id"]), models[0]["id"])
    print(f"    using: {model}")

    base = {"ai_provider": "gemini", "ai_key": key, "ai_model": model, "engine": "documentdb"}

    out = assistant.chat_turn({**base,
        "messages": [{"role": "user", "content": "Reply with exactly: warden online"}]}, routes={})
    print(f"[2] plain chat -> {out.get('reply') or out}")

    def fake_list_users(_sub):
        return {"users": [{"user": "admin"}, {"user": "alice"}, {"user": "bob"}]}

    out2 = assistant.chat_turn({**base, "admin_user": "admin", "admin_pass": "pw",
        "messages": [{"role": "user",
                      "content": "How many user accounts exist? Use your tools, then answer in one line."}]},
        routes={"/api/list-users": fake_list_users})
    print(f"[3] tool round-trip -> {out2.get('reply') or out2}")
    print(f"    tools used: {[s['tool'] for s in out2.get('steps', [])]}")
    print("\nOK. If [3] used list_users and answered '3', function calling is wired up right.")


if __name__ == "__main__":
    main()
