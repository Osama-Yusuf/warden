"""Validation, quoting, passwords, sizes, and subprocess helpers."""

import json
import re
import secrets
import shutil
import string
import subprocess

SAFE_IDENT_RE = re.compile(r'^[a-zA-Z0-9_.\-@]+$')


def validate_ident(value, label="value"):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    value = value.strip()
    if len(value) > 128:
        raise ValueError(f"{label} too long (max 128 chars)")
    if not SAFE_IDENT_RE.match(value):
        raise ValueError(f"{label} contains invalid characters (allowed: letters, digits, _ . - @)")
    return value


def js_string(value):
    """Safely quote a value for embedding in a JavaScript expression."""
    return json.dumps(str(value))


def pg_ident(value):
    """Double-quote a PostgreSQL identifier, escaping internal double-quotes."""
    return '"' + str(value).replace('"', '""') + '"'


def pg_literal(value):
    """Single-quote a PostgreSQL string literal, escaping internal single-quotes."""
    return "'" + str(value).replace("'", "''") + "'"


def generate_password(length=24):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        has_upper = any(c.isupper() for c in pwd)
        has_lower = any(c.islower() for c in pwd)
        has_digit = any(c.isdigit() for c in pwd)
        has_special = any(c in "!@#$%^&*" for c in pwd)
        if has_upper and has_lower and has_digit and has_special:
            return pwd


def format_size(num_bytes):
    """Human-readable size: picks KB/MB/GB/TB automatically."""
    size = float(num_bytes or 0)
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        size /= 1024
        if size < 1024 or unit == "PB":
            if size >= 100:
                return f"{size:.0f} {unit}"
            if size >= 10:
                return f"{size:.1f} {unit}"
            return f"{size:.2f} {unit}"


def run_cmd(args, timeout=30):
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except FileNotFoundError:
        return -2, "", f"Command not found: {args[0]}"


def check_tool(name):
    return shutil.which(name) is not None


def engine_family(engine):
    """Which client family an engine key belongs to. Engine keys are free-form
    (mysql, mariadb, aurora-mysql, sqlite, aurora, citus...), so match loosely."""
    e = (engine or "").lower()
    if e.startswith("document") or "mongo" in e:
        return "documentdb"
    if "mysql" in e or "maria" in e:
        return "mysql"
    if "sqlite" in e:
        return "sqlite"
    return "postgresql"
