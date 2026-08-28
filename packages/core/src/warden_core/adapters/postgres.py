"""Postgres, and anything Postgres-compatible (Aurora, Citus, and friends).

Structured reads and row edits go through the pooled native driver (pg_native);
the plain SQL bits (databases, roles, health) go through the psql helper.
"""

from warden_core import pg_native as pn
from warden_core.pg import pg_exec, pg_query
from warden_core.util import pg_ident, pg_literal, validate_ident
from warden_core.validation import clean_columns, validate_pg_privilege

from .base import EngineAdapter, EngineError, Mutation, register


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

    # ── discovery / browse ──────────────────────────────────────────────────
    def browse(self, target, limit, offset, search):
        # Returns the raw driver page (columns/rows/total/estimated/filtered/ids);
        # the handler wraps it into the uniform browser shape.
        database = validate_ident(target.database, "database")
        schema = validate_ident(target.schema or "public", "schema")
        table = validate_ident(target.name, "table")
        return self._unwrap(pn.select_page(self.cfg, self.user, self.pwd,
                                           database, schema, table,
                                           limit, offset, search=search))

    def object_stats(self, target):
        database = validate_ident(target.database, "database")
        schema = validate_ident(target.schema or "public", "schema")
        table = validate_ident(target.name, "table")
        return self._unwrap(pn.object_stats(self.cfg, self.user, self.pwd,
                                            database, schema, table))

    def table_meta(self, target):
        database = validate_ident(target.database, "database")
        schema = validate_ident(target.schema or "public", "schema")
        table = validate_ident(target.name, "table")
        meta = self._unwrap(pn.table_meta(self.cfg, self.user, self.pwd,
                                          database, schema, table))
        reason = None if meta.get("editable") else "table has no primary key"
        return {"reason": reason, **meta}

    def health(self):
        health = {}
        checks = {
            "version": "SELECT current_setting('server_version')",
            "uptime": "SELECT date_trunc('second', now() - pg_postmaster_start_time())::text",
            "connections": ("SELECT count(*) FILTER (WHERE state = 'active'),"
                            " count(*) FILTER (WHERE state = 'idle'), count(*),"
                            " current_setting('max_connections') FROM pg_stat_activity"),
            "cache_hit_pct": ("SELECT round(100.0 * sum(blks_hit) /"
                              " nullif(sum(blks_hit) + sum(blks_read), 0), 1) FROM pg_stat_database"),
            "blocked": "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'",
            "replicas": "SELECT count(*), coalesce(max(replay_lag)::text, '') FROM pg_stat_replication",
            "total_size": "SELECT sum(pg_database_size(datname))::bigint FROM pg_database WHERE datistemplate = false",
        }
        for key, sql in checks.items():
            code, out, _ = pg_query(self.cfg, self.user, self.pwd, sql)
            health[key] = out.strip().split("\t") if code == 0 and out.strip() else None
        code, out, _ = pg_query(self.cfg, self.user, self.pwd,
            "SELECT pid, usename, date_trunc('second', now() - query_start)::text, left(query, 90)"
            " FROM pg_stat_activity WHERE state <> 'idle' AND pid <> pg_backend_pid()"
            " AND now() - query_start > interval '5 seconds' ORDER BY 3 DESC LIMIT 10")
        slow = []
        if code == 0 and out.strip():
            for line in out.strip().split("\n"):
                p = line.split("\t")
                if len(p) >= 4:
                    slow.append({"pid": p[0], "user": p[1], "runtime": p[2], "query": p[3]})
        health["slow_queries"] = slow
        return health

    # ── users / ACL ─────────────────────────────────────────────────────────
    def list_users(self):
        sql = """
            SELECT rolname, rolcanlogin, rolsuper, rolcreatedb,
                   rolcreaterole, COALESCE(rolvaliduntil::text, 'never')
            FROM pg_roles
            WHERE rolname NOT LIKE 'pg_%%'
              AND rolname NOT IN ('rdsadmin','rds_superuser','rds_replication','rds_password','rdsrepladmin')
            ORDER BY rolname
        """
        code, out, err = pg_query(self.cfg, self.user, self.pwd, sql)
        if code != 0:
            raise EngineError(err)
        users = []
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 6:
                users.append({
                    "user": parts[0],
                    "can_login": parts[1] == "t",
                    "superuser": parts[2] == "t",
                    "createdb": parts[3] == "t",
                    "createrole": parts[4] == "t",
                    "valid_until": parts[5],
                })
        return users

    def user_info(self, name):
        sql = f"""
            SELECT rolname, rolcanlogin, rolsuper, rolcreatedb,
                   rolcreaterole, COALESCE(rolvaliduntil::text,'never'),
                   COALESCE(rolconnlimit::text,'unlimited')
            FROM pg_roles WHERE rolname = {pg_literal(name)}
        """
        code, out, err = pg_query(self.cfg, self.user, self.pwd, sql)
        if code != 0 or not out.strip():
            raise EngineError("User not found")
        parts = out.strip().split("\t")
        info = {
            "user": parts[0],
            "can_login": parts[1] == "t",
            "superuser": parts[2] == "t",
            "createdb": parts[3] == "t",
            "createrole": parts[4] == "t",
            "valid_until": parts[5],
            "conn_limit": parts[6] if len(parts) > 6 else "unlimited",
        }

        sql2 = f"""
            SELECT table_catalog, table_schema||'.'||table_name, privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee = {pg_literal(name)} ORDER BY 1,2 LIMIT 50
        """
        code2, out2, _ = pg_query(self.cfg, self.user, self.pwd, sql2)
        grants = []
        if code2 == 0 and out2.strip():
            for line in out2.strip().split("\n"):
                p = line.split("\t")
                if len(p) >= 3:
                    grants.append({"db": p[0], "table": p[1], "privilege": p[2]})

        sql3 = f"""
            SELECT datname,
                   has_database_privilege({pg_literal(name)}, datname, 'CONNECT'),
                   has_database_privilege({pg_literal(name)}, datname, 'CREATE')
            FROM pg_database WHERE datistemplate=false AND datname NOT IN ('rdsadmin')
            ORDER BY datname
        """
        code3, out3, _ = pg_query(self.cfg, self.user, self.pwd, sql3)
        db_privs = []
        if code3 == 0 and out3.strip():
            for line in out3.strip().split("\n"):
                p = line.split("\t")
                if len(p) >= 3:
                    privs = []
                    if p[1] == "t": privs.append("CONNECT")
                    if p[2] == "t": privs.append("CREATE")
                    db_privs.append({"database": p[0], "privileges": privs})

        info["grants"] = grants
        info["db_privileges"] = db_privs
        return info

    def create_user(self, name, password, can_login=True, **opts):
        login = "LOGIN" if can_login else "NOLOGIN"
        sql = f"CREATE USER {pg_ident(name)} WITH {login} PASSWORD {pg_literal(password)}"
        ok, out, err = pg_exec(self.cfg, self.user, self.pwd, sql)
        if not ok:
            raise EngineError(err or out)
        return Mutation("CREATE USER", name, {"password": password})

    def set_password(self, name, password):
        sql = f"ALTER USER {pg_ident(name)} WITH PASSWORD {pg_literal(password)}"
        ok, out, err = pg_exec(self.cfg, self.user, self.pwd, sql)
        if not ok:
            raise EngineError(err or out)
        return Mutation("RESET PASSWORD", name, {"password": password})

    def grant(self, name, privilege=None, database=None, schema=None, **opts):
        priv = validate_pg_privilege(privilege or "SELECT")
        database = validate_ident(database or "postgres", "database")
        schema = validate_ident(schema or "public", "schema")
        # CONNECT/CREATE are database-level and run against the default db;
        # everything else is table-level and runs against the target database.
        if priv in ("CONNECT", "CREATE"):
            sql = f"GRANT {priv} ON DATABASE {pg_ident(database)} TO {pg_ident(name)}"
            run_db = self.cfg.get("default_db", "postgres")
        else:
            sql = f"GRANT {priv} ON ALL TABLES IN SCHEMA {pg_ident(schema)} TO {pg_ident(name)}"
            run_db = database
        ok, out, err = pg_exec(self.cfg, self.user, self.pwd, sql, db=run_db)
        if not ok:
            raise EngineError(err or out)
        return Mutation("GRANT", f"{name} += {priv} on {database}.{schema}")

    def revoke(self, name, privilege=None, database=None, schema=None, **opts):
        priv = validate_pg_privilege(privilege or "SELECT")
        database = validate_ident(database or "postgres", "database")
        schema = validate_ident(schema or "public", "schema")
        if priv in ("CONNECT", "CREATE"):
            sql = f"REVOKE {priv} ON DATABASE {pg_ident(database)} FROM {pg_ident(name)}"
            run_db = self.cfg.get("default_db", "postgres")
        else:
            sql = f"REVOKE {priv} ON ALL TABLES IN SCHEMA {pg_ident(schema)} FROM {pg_ident(name)}"
            run_db = database
        ok, out, err = pg_exec(self.cfg, self.user, self.pwd, sql, db=run_db)
        if not ok:
            raise EngineError(err or out)
        return Mutation("REVOKE", f"{name} -= {priv} on {database}.{schema}")

    def drop_user(self, name):
        ok, out, err = pg_exec(self.cfg, self.user, self.pwd, f"DROP USER {pg_ident(name)}")
        if not ok:
            raise EngineError(err or out)
        return Mutation("DROP USER", name)

    def toggle_login(self, name, enable):
        kw = "LOGIN" if enable else "NOLOGIN"
        ok, out, err = pg_exec(self.cfg, self.user, self.pwd,
                               f"ALTER USER {pg_ident(name)} WITH {kw}")
        if not ok:
            raise EngineError(err or out)
        return Mutation("ENABLE USER" if enable else "DISABLE USER", name)

    # ── row CRUD ────────────────────────────────────────────────────────────
    def insert_row(self, target, body):
        database = validate_ident(target.database, "database")
        schema = validate_ident(target.schema or "public", "schema")
        table = validate_ident(target.name, "table")
        values = clean_columns(body.get("values"), "column")
        if not values:
            raise EngineError("No values to insert")
        res = self._unwrap(pn.insert_row(self.cfg, self.user, self.pwd,
                                        database, schema, table, values))
        return Mutation("INSERT ROW", f"{schema}.{table} ({', '.join(values)})", {"row": res})

    def update_row(self, target, body):
        database = validate_ident(target.database, "database")
        schema = validate_ident(target.schema or "public", "schema")
        table = validate_ident(target.name, "table")
        pk = clean_columns(body.get("pk"), "primary key column")
        changes = clean_columns(body.get("changes"), "column")
        if not pk:
            raise EngineError("Refusing to update without a primary key")
        if not changes:
            raise EngineError("No changes to apply")
        n = self._unwrap(pn.update_row(self.cfg, self.user, self.pwd,
                                      database, schema, table, pk, changes))
        return Mutation("UPDATE ROW",
                        f"{schema}.{table} WHERE {pk} SET {', '.join(changes)}",
                        {"updated": n})

    def delete_row(self, target, body):
        database = validate_ident(target.database, "database")
        schema = validate_ident(target.schema or "public", "schema")
        table = validate_ident(target.name, "table")
        pk = clean_columns(body.get("pk"), "primary key column")
        if not pk:
            raise EngineError("Refusing to delete without a primary key")
        n = self._unwrap(pn.delete_row(self.cfg, self.user, self.pwd,
                                      database, schema, table, pk))
        return Mutation("DELETE ROW", f"{schema}.{table} WHERE {pk}", {"deleted": n})

    # ── test a user's own login ─────────────────────────────────────────────
    def login_probe(self, test_user, test_pass, test_db=""):
        # Native probe: non-pooled, fails fast on a bad password. Falls back to
        # the psql helper when psycopg isn't importable, same as the handler.
        if pn.available():
            r = pn.login_probe(self.cfg, test_user, test_pass, test_db)
            if not r.get("auth"):
                return {"ok": True, "auth": False,
                        "error": (r.get("error") or "").strip() or "Authentication failed"}
            return {"ok": True, "auth": True, "identity": r.get("identity", test_user),
                    "checks": r.get("checks", [])}
        checks = []
        code, out, err = pg_query(self.cfg, test_user, test_pass, "SELECT current_user")
        if code != 0:
            return {"ok": True, "auth": False, "error": (err or "").strip() or "Authentication failed"}
        code2, out2, _ = pg_query(self.cfg, test_user, test_pass,
            "SELECT datname FROM pg_database WHERE datistemplate=false "
            "AND has_database_privilege(datname, 'CONNECT') ORDER BY datname")
        checks.append({"name": "Databases they can connect to", "ok": code2 == 0,
                       "detail": ", ".join(out2.split()) if code2 == 0 and out2.strip() else "none"})
        if test_db:
            code3, out3, err3 = pg_query(self.cfg, test_user, test_pass, "SELECT 1", db=test_db)
            checks.append({"name": f"Connect to '{test_db}'", "ok": code3 == 0,
                           "detail": "connected" if code3 == 0 else (err3 or "").strip() or "denied"})
        return {"ok": True, "auth": True, "identity": out.strip(), "checks": checks}
