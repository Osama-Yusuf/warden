"""MySQL/MariaDB (mysql client) primitives with structured returns."""

import csv
import io
import os
import subprocess


def _my_env(admin_pass):
    env = os.environ.copy()
    env["MYSQL_PWD"] = admin_pass
    return env


def _my_args(config, admin_user, db=None):
    args = ["mysql", "-h", config["host"], "-P", str(config["port"]),
            "-u", admin_user, "--protocol=TCP", "--connect-timeout=10"]
    target = db or config.get("default_db")
    if target:
        args += ["-D", target]
    return args


def my_query(config, admin_user, admin_pass, sql, db=None, timeout=30):
    """Rows as tab-separated lines, no header."""
    args = _my_args(config, admin_user, db) + ["-N", "-B", "--raw", "-e", sql]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           env=_my_env(admin_pass))
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Query timed out"
    except FileNotFoundError:
        return -2, "", "mysql client not found"


def my_exec(config, admin_user, admin_pass, sql, db=None, timeout=30):
    code, out, err = my_query(config, admin_user, admin_pass, sql, db=db, timeout=timeout)
    return code == 0, out, err


def my_csv(config, admin_user, admin_pass, sql, db=None, timeout=60):
    """Result sets as CSV with headers (query console)."""
    args = _my_args(config, admin_user, db) + ["-B", "--raw", "-e", sql]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           env=_my_env(admin_pass))
    except subprocess.TimeoutExpired:
        return -1, "", "Query timed out"
    except FileNotFoundError:
        return -2, "", "mysql client not found"
    if r.returncode != 0:
        return r.returncode, r.stdout, r.stderr
    buf = io.StringIO()
    writer = csv.writer(buf)
    for line in r.stdout.splitlines():
        writer.writerow(line.split("\t"))
    return 0, buf.getvalue(), r.stderr
