"""Postgres, and anything Postgres-compatible (Aurora, Citus, and friends).

Structured reads and row edits go through the pooled native driver (pg_native);
the plain SQL bits (databases, roles, health) go through the psql helper.
"""

from warden_core import pg_native as pn
from warden_core.pg import pg_query
from warden_core.util import validate_ident

from .base import EngineAdapter, EngineError, register


@register
class PostgresAdapter(EngineAdapter):
    family = "postgresql"
    collection_key = "tables"
    has_login_toggle = True
    editable_rows = True

    def ping(self):
        code, out, err = pg_query(self.cfg, self.user, self.pwd, "SELECT current_user")
        if code != 0:
            raise EngineError(err or "Connection failed")
        return out.strip() or self.user

    def list_databases(self):
        sql = """
            SELECT datname, pg_database_size(datname)::bigint
            FROM pg_database WHERE datistemplate=false AND datname NOT IN ('rdsadmin')
            ORDER BY datname
        """
        code, out, err = pg_query(self.cfg, self.user, self.pwd, sql)
        if code != 0:
            raise EngineError(err)
        dbs = []
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                size = int(parts[1])
                dbs.append({"name": parts[0], "size_bytes": size,
                            "size_mb": round(size / (1024 * 1024), 1)})
        return dbs

    def list_collections(self, database):
        database = validate_ident(database, "database")
        sql = """
            SELECT n.nspname, c.relname, pg_total_relation_size(c.oid)::bigint
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r','p','m')
              AND n.nspname NOT IN ('pg_catalog','information_schema')
            ORDER BY n.nspname, c.relname
        """
        code, out, err = pg_query(self.cfg, self.user, self.pwd, sql, db=database)
        if code != 0:
            raise EngineError(err)
        tables = []
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                tables.append({"schema": parts[0], "table": parts[1], "size_bytes": int(parts[2])})
            elif len(parts) >= 2:
                tables.append({"schema": parts[0], "table": parts[1]})
        return tables
