"""SQLite (sqlite3 CLI) primitives. A database here is just a file path."""

import subprocess


def sq_query(path, sql, timeout=30):
    """Rows as tab-separated lines, no header."""
    args = ["sqlite3", "-batch", "-noheader", "-separator", "\t", str(path), sql]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Query timed out"
    except FileNotFoundError:
        return -2, "", "sqlite3 not found"


def sq_exec(path, sql, timeout=30):
    code, out, err = sq_query(path, sql, timeout=timeout)
    return code == 0, out, err


def sq_csv(path, sql, timeout=60):
    """Result sets as CSV with headers (query console)."""
    args = ["sqlite3", "-batch", "-csv", "-header", str(path), sql]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Query timed out"
    except FileNotFoundError:
        return -2, "", "sqlite3 not found"
