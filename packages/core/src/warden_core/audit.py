"""Append-only audit trail of every mutating operation."""

import os
from datetime import datetime
from pathlib import Path

AUDIT_LOG = Path(os.environ.get("WARDEN_AUDIT_LOG", str(Path.home() / ".warden" / "audit.log")))

# One-time migration from the tool's earlier name
_legacy_log = Path.home() / ".dbctl_audit.log"
if not AUDIT_LOG.exists() and "WARDEN_AUDIT_LOG" not in os.environ and _legacy_log.exists():
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_LOG.write_text(_legacy_log.read_text())


def audit(env, engine, action, details=""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}  [{env.upper()}/{engine.upper()}]  {action}"
    if details:
        line += f"  · {details}"
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG, "a") as f:
        f.write(line + "\n")
