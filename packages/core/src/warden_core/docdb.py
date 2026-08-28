"""DocumentDB (mongosh) primitives with structured returns."""

import json
from urllib.parse import quote

from .util import run_cmd


def docdb_args(config, admin_user, admin_pass, db="admin"):
    # mongosh has no --db flag (it silently ignores unknown options); the
    # target database must go in the connection URI path.
    args = [
        "mongosh",
        f"mongodb://{config['host']}:{config['port']}/{quote(str(db), safe='')}",
        "--retryWrites=false",
        "--username", admin_user,
        "--password", admin_pass,
        "--authenticationDatabase", config.get("auth_db", "admin"),
        "--quiet", "--norc",
    ]
    if config.get("tls"):
        args.append("--tls")
        if config.get("tls_insecure"):
            args.append("--tlsAllowInvalidCertificates")   # opt out: self-signed / private CA
    return args


def docdb_eval(config, admin_user, admin_pass, js, db="admin", timeout=30):
    args = docdb_args(config, admin_user, admin_pass, db) + ["--eval", f"JSON.stringify({js})"]
    code, out, err = run_cmd(args, timeout=timeout)
    if code != 0:
        return None, err or f"Exit code {code}"
    for line in out.strip().split("\n"):
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                return json.loads(line), None
            except json.JSONDecodeError:
                pass
    if "ok" in out.lower():
        return {"ok": True}, None
    return None, f"No JSON found in output: {out[:300]}"


def docdb_exec(config, admin_user, admin_pass, js, db="admin", timeout=30):
    args = docdb_args(config, admin_user, admin_pass, db) + ["--eval", js]
    code, out, err = run_cmd(args, timeout=timeout)
    return code == 0, out, err
