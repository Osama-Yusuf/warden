"""PostgreSQL primitives with structured returns.

Structured queries/writes go through pooled psycopg (pg_native) when it's
importable, fast and concurrency-safe. They transparently fall back to the
psql subprocess otherwise. The free-form SQL console (pg_csv) always uses psql.
"""

import os
import subprocess

from . import pg_native


def _pg_env(admin_pass):
    env = os.environ.copy()
    env["PGPASSWORD"] = admin_pass
    return env


def _pg_args(config, admin_user, db=None):
    return [
        "psql",
        "-h", config["host"],
        "-p", str(config["port"]),
        "-U", admin_user,
        "-d", db or config.get("default_db", "postgres"),
        "--no-psqlrc",
    ]


def pg_query(config, admin_user, admin_pass, sql, db=None, timeout=30):
    if pg_native.available():
        return pg_native.query(config, admin_user, admin_pass, sql, db=db, timeout=timeout)
    args = _pg_args(config, admin_user, db) + ["-t", "-A", "-F", "\t", "-c", sql]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                env=_pg_env(admin_pass))
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Query timed out"
    except FileNotFoundError:
        return -2, "", "psql not found"


def pg_csv(config, admin_user, admin_pass, sql, db=None, timeout=60):
    """Run arbitrary SQL, result sets as CSV with headers (query console)."""
    args = _pg_args(config, admin_user, db) + ["--csv", "-c", sql]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                env=_pg_env(admin_pass))
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Query timed out"
    except FileNotFoundError:
        return -2, "", "psql not found"


def pg_exec(config, admin_user, admin_pass, sql, db=None, timeout=30):
    if pg_native.available():
        return pg_native.exec_(config, admin_user, admin_pass, sql, db=db, timeout=timeout)
    args = _pg_args(config, admin_user, db) + ["-c", sql]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                env=_pg_env(admin_pass))
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", "Query timed out"
    except FileNotFoundError:
        return False, "", "psql not found"
