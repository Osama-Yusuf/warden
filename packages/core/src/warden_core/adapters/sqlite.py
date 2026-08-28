"""SQLite (sqlite3 CLI). A database here is a single file on disk.

Everything runs through the sqlite3 command-line helpers in sqlitedb: structured
reads come back as JSON, scalar counts as tab-separated lines. There are no user
accounts and no in-grid row editing, so the user, CRUD, and query-console methods
stay unimplemented and inherit NotSupported from the base class.
"""

import json
from pathlib import Path

from warden_core.sqlitedb import sq_json, sq_query
from warden_core.validation import sql_like_pattern, validate_ident

from .base import EngineAdapter, EngineError, register


@register
class SqliteAdapter(EngineAdapter):
    family = "sqlite"
    collection_key = "tables"
    has_users = False
    has_login_toggle = False
    editable_rows = False

    # ── discovery / browse ──────────────────────────────────────────────────
    def ping(self):
        db_path = Path(self.cfg["path"])
        if not db_path.is_file():
            raise EngineError(f"No such file: {db_path}")
        code, out, err = sq_query(db_path, "SELECT sqlite_version()")
        if code != 0:
            raise EngineError(err or "Could not open the database file")
        # SQLite has no user identity, so nothing to show in the top bar.
        return ""

    def list_databases(self):
        # One synthetic database: the file itself.
        db_path = Path(self.cfg["path"])
        size = db_path.stat().st_size if db_path.is_file() else 0
        return [{"name": db_path.name, "size_bytes": size,
                 "size_mb": round(size / 1048576, 1)}]

    def list_collections(self, database):
        db_path = Path(self.cfg["path"])
        code, out, err = sq_query(db_path,
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        if code != 0:
            raise EngineError(err)
        names = [l.strip() for l in out.strip().split("\n") if l.strip()]
        sizes = {}
        code2, out2, _ = sq_query(db_path, "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")
        if code2 == 0:
            for line in out2.strip().split("\n"):
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1].isdigit():
                    sizes[parts[0]] = int(parts[1])
        return [{"schema": "main", "table": n,
                 **({"size_bytes": sizes[n]} if n in sizes else {})} for n in names]

    def browse(self, target, limit, offset, search):
        table = validate_ident(target.name, "table")
        db_path = Path(self.cfg["path"])
        rel = '"' + table.replace('"', '""') + '"'
        where = ""
        if search:
            tlit = "'" + table.replace("'", "''") + "'"
            code0, out0, _ = sq_query(db_path, f"SELECT name FROM pragma_table_info({tlit})")
            colnames = [l.strip() for l in out0.strip().split("\n") if l.strip()] if code0 == 0 else []
            pat = sql_like_pattern(search)
            if colnames:
                ors = " OR ".join(f'CAST("{c}" AS TEXT) LIKE {pat} ESCAPE \'\\\'' for c in colnames)
                where = f" WHERE ({ors})"
        code, out, err = sq_json(db_path, f"SELECT * FROM {rel}{where} LIMIT {limit} OFFSET {offset}")
        if code != 0:
            raise EngineError(err or "Query failed")
        try:
            records = json.loads(out) if out.strip() else []
        except (json.JSONDecodeError, ValueError):
            records = []
        columns, seen = [], set()
        for rec in records:
            for k in rec.keys():
                if k not in seen:
                    seen.add(k)
                    columns.append(k)
        rows = [[rec.get(k) for k in columns] for rec in records]
        total = None
        if not search:
            code2, out2, _ = sq_query(db_path, f"SELECT COUNT(*) FROM {rel}")
            if code2 == 0 and out2.strip().isdigit():
                total = int(out2.strip())
        return {"columns": columns, "rows": rows, "total": total,
                "estimated": False, "filtered": bool(search)}

    def object_stats(self, target):
        table = validate_ident(target.name, "table")
        db_path = Path(self.cfg["path"])
        rel = '"' + table.replace('"', '""') + '"'
        tlit = "'" + table.replace("'", "''") + "'"
        st = {}
        code, out, _ = sq_query(db_path, f"SELECT COUNT(*) FROM {rel}")
        if code == 0 and out.strip().isdigit():
            st["rows"] = int(out.strip())
        code2, out2, _ = sq_query(db_path, f"SELECT COUNT(*) FROM pragma_table_info({tlit})")
        if code2 == 0 and out2.strip().isdigit():
            st["columns"] = int(out2.strip())
        code3, out3, _ = sq_query(db_path, f"SELECT COALESCE(SUM(pgsize),0) FROM dbstat WHERE name={tlit}")
        if code3 == 0 and out3.strip().isdigit():
            st["size_bytes"] = int(out3.strip())
        return st

    def table_meta(self, target):
        # SQLite is view and search only for now, never editable.
        return {"engine": "sqlite", "editable": False,
                "reason": "row editing isn't supported for SQLite yet (view & search only)"}

    def health(self):
        db_path = Path(self.cfg["path"])
        health = {"file": str(db_path),
                  "size_bytes": db_path.stat().st_size if db_path.is_file() else 0}
        for key, sql in {
            "version": "SELECT sqlite_version()",
            "page_count": "PRAGMA page_count",
            "page_size": "PRAGMA page_size",
            "journal_mode": "PRAGMA journal_mode",
            "tables": "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'",
            "integrity": "PRAGMA quick_check",
        }.items():
            code, out, _ = sq_query(db_path, sql)
            health[key] = out.strip() if code == 0 else None
        return health
