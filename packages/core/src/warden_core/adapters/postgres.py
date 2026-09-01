"""Postgres, and anything Postgres-compatible (Aurora, Citus, and friends).

Structured reads and row edits go through the pooled native driver (pg_native);
the plain SQL bits (databases, roles, health) go through the psql helper.
"""

from warden_core import pg_native as pn
from warden_core.pg import pg_exec, pg_query
from warden_core.util import pg_ident, pg_literal, validate_ident
from warden_core.validation import clean_columns, validate_pg_privilege

from .base import EngineAdapter, EngineError, Mutation, register

# pg_default_acl.defaclobjtype code -> the ALTER DEFAULT PRIVILEGES object word.
_DEFACL_OBJ = {"r": "TABLES", "S": "SEQUENCES", "f": "FUNCTIONS", "T": "TYPES", "n": "SCHEMAS"}


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
        if not pn.available():
            raise EngineError("Data browser needs the native PostgreSQL driver (psycopg)")
        return self._unwrap(pn.select_page(self.cfg, self.user, self.pwd,
                                           database, schema, table,
                                           limit, offset, search=search))

    def object_stats(self, target):
        if not pn.available():
            return {}
        database = validate_ident(target.database, "database")
        schema = validate_ident(target.schema or "public", "schema")
        table = validate_ident(target.name, "table")
        return self._unwrap(pn.object_stats(self.cfg, self.user, self.pwd,
                                            database, schema, table))

    def table_meta(self, target):
        if not pn.available():
            return {"editable": False, "reason": "native PostgreSQL driver unavailable"}
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

        # Roles this user is a member of. Their privileges live on the role, not on
        # this user, so an access report needs to know the memberships exist rather
        # than reading the user's own grants as the whole story.
        sql4 = f"""
            SELECT r.rolname
            FROM pg_auth_members m
            JOIN pg_roles r ON r.oid = m.roleid
            JOIN pg_roles u ON u.oid = m.member
            WHERE u.rolname = {pg_literal(name)} ORDER BY 1 LIMIT 50
        """
        code4, out4, _ = pg_query(self.cfg, self.user, self.pwd, sql4)
        member_of = [l.strip() for l in out4.strip().split("\n") if l.strip()] if code4 == 0 and out4.strip() else []

        info["grants"] = grants
        info["db_privileges"] = db_privs
        info["member_of"] = member_of
        return info

    def create_user(self, name, password, can_login=True, **opts):
        login = "LOGIN" if can_login else "NOLOGIN"
        sql = f"CREATE USER {pg_ident(name)} WITH {login} PASSWORD {pg_literal(password)}"
        ok, out, err = pg_exec(self.cfg, self.user, self.pwd, sql)
        if not ok:
            raise EngineError(err or out)
        return Mutation("CREATE USER", name, {"password": password})

    def create_database(self, name):
        name = validate_ident(name, "database")
        # CREATE DATABASE can't run in a transaction; psql autocommits each stmt.
        ok, out, err = pg_exec(self.cfg, self.user, self.pwd, f"CREATE DATABASE {pg_ident(name)}",
                               db=self.cfg.get("default_db", "postgres"))
        if not ok:
            raise EngineError(err or out)
        return Mutation("CREATE DATABASE", name, {})

    def set_password(self, name, password):
        sql = f"ALTER USER {pg_ident(name)} WITH PASSWORD {pg_literal(password)}"
        ok, out, err = pg_exec(self.cfg, self.user, self.pwd, sql)
        if not ok:
            raise EngineError(err or out)
        return Mutation("RESET PASSWORD", name, {"password": password})

    def _run_each(self, statements, db=None, strict=True):
        """Run each statement as its own call. Postgres's driver here (psycopg3)
        does not run a semicolon-joined string as separate statements reliably, so
        a REASSIGN;DROP OWNED batch could drop what the REASSIGN just moved. One
        call per statement keeps the order (and each autocommit) honest."""
        for s in statements:
            ok, out, err = pg_exec(self.cfg, self.user, self.pwd, s, db=db)
            if not ok and strict:
                raise EngineError(err or out)

    def _db_connect_keepers(self, database):
        """Login, non-superuser roles that use `database` and should keep access
        when we take CONNECT off PUBLIC: those with table privileges there, plus
        those already holding an explicit CONNECT grant on the db (so a user given
        CONNECT to an empty db isn't cut off just because it has no tables yet)."""
        keepers = set()
        code, out, _ = pg_query(self.cfg, self.user, self.pwd,
            "SELECT DISTINCT g.grantee FROM information_schema.role_table_grants g "
            "JOIN pg_roles r ON r.rolname = g.grantee "
            "WHERE g.grantee <> 'PUBLIC' AND r.rolcanlogin AND NOT r.rolsuper",
            db=database)
        if code == 0:
            keepers |= {l.strip() for l in out.strip().split("\n") if l.strip()}
        code2, out2, _ = pg_query(self.cfg, self.user, self.pwd,
            "SELECT DISTINCT r.rolname FROM pg_database d, aclexplode(d.datacl) a "
            "JOIN pg_roles r ON r.oid = a.grantee "
            f"WHERE d.datname = {pg_literal(database)} AND a.privilege_type = 'CONNECT' "
            "AND r.rolcanlogin AND NOT r.rolsuper", db=self.cfg.get("default_db", "postgres"))
        if code2 == 0:
            keepers |= {l.strip() for l in out2.strip().split("\n") if l.strip()}
        return sorted(keepers)

    def _lockdown_connect(self, database, keep_extra=(), exclude=None):
        """Make connecting to `database` explicit: revoke CONNECT from PUBLIC, then
        grant it back to the roles that use the db (plus keep_extra), and revoke it
        from `exclude`. After this, only granted accounts can connect, so a new
        user can't reach the db unless given access and a revoke actually bites.
        Returns the roles left able to connect."""
        # Always keep the admin warden connects as: locking ourselves out of a
        # database means we can never manage (or clean up before dropping) in it.
        keep = {k for k in (list(self._db_connect_keepers(database)) + list(keep_extra) + [self.user]) if k}
        if exclude:
            keep.discard(exclude)
        ddb = self.cfg.get("default_db", "postgres")
        stmts = [f"REVOKE CONNECT ON DATABASE {pg_ident(database)} FROM PUBLIC"]
        stmts += [f"GRANT CONNECT ON DATABASE {pg_ident(database)} TO {pg_ident(k)}" for k in sorted(keep)]
        if exclude:
            stmts.append(f"REVOKE CONNECT ON DATABASE {pg_ident(database)} FROM {pg_ident(exclude)}")
        self._run_each(stmts, db=ddb, strict=False)
        return sorted(keep)

    def grant(self, name, privilege=None, database=None, schema=None, **opts):
        priv = validate_pg_privilege(privilege or "SELECT")
        database = validate_ident(database or "postgres", "database")
        schema = validate_ident(schema or "public", "schema")
        if priv in ("CONNECT", "CREATE"):
            self._run_each([f"GRANT {priv} ON DATABASE {pg_ident(database)} TO {pg_ident(name)}"],
                           db=self.cfg.get("default_db", "postgres"))
        else:
            # Table-level: schema USAGE + the privilege on existing tables + the
            # same as a default for tables created later, so "read" keeps working
            # as the schema grows instead of covering only today's tables.
            self._run_each([
                f"GRANT USAGE ON SCHEMA {pg_ident(schema)} TO {pg_ident(name)}",
                f"GRANT {priv} ON ALL TABLES IN SCHEMA {pg_ident(schema)} TO {pg_ident(name)}",
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {pg_ident(schema)} GRANT {priv} ON TABLES TO {pg_ident(name)}",
            ], db=database)
        return Mutation("GRANT", f"{name} += {priv} on {database}.{schema}")

    def revoke(self, name, privilege=None, database=None, schema=None, **opts):
        priv = validate_pg_privilege(privilege or "SELECT")
        database = validate_ident(database or "postgres", "database")
        schema = validate_ident(schema or "public", "schema")
        note = None
        if priv == "CONNECT":
            # Revoke it from the user AND take it off PUBLIC (re-granting the roles
            # that use the db), so the user genuinely can't connect. Just revoking
            # from the user is a no-op while PUBLIC still allows everyone in.
            kept = self._lockdown_connect(database, exclude=name)
            note = (f"{name} can no longer connect to {database}. CONNECT is now explicit "
                    f"({len(kept)} other role(s) kept access); accounts not granted it can't connect.")
        elif priv == "CREATE":
            self._run_each([f"REVOKE {priv} ON DATABASE {pg_ident(database)} FROM {pg_ident(name)}"],
                           db=self.cfg.get("default_db", "postgres"))
        else:
            self._run_each([
                f"REVOKE {priv} ON ALL TABLES IN SCHEMA {pg_ident(schema)} FROM {pg_ident(name)}",
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {pg_ident(schema)} REVOKE {priv} ON TABLES FROM {pg_ident(name)}",
            ], db=database)
        m = Mutation("REVOKE", f"{name} -= {priv} on {database}.{schema}")
        if note:
            m.response["warning"] = note
        return m

    def _public_can_connect(self, database):
        code, out, _ = pg_query(self.cfg, self.user, self.pwd,
            f"SELECT has_database_privilege('public', {pg_literal(database)}, 'CONNECT')",
            db=self.cfg.get("default_db", "postgres"))
        return code == 0 and out.strip().lower() in ("t", "true")

    def _connectable_dbs(self):
        """Databases the admin can connect to, for cluster-wide role cleanup: a
        role can't be dropped while it owns or holds anything in any database."""
        code, out, _ = pg_query(self.cfg, self.user, self.pwd,
            "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate")
        return [l.strip() for l in out.strip().split("\n") if l.strip()] if code == 0 else []

    def _revoke_default_priv_grants(self, name, database):
        """Revoke default-privilege grants where `name` is the GRANTEE but another
        role owns the entry (someone ran ALTER DEFAULT PRIVILEGES FOR ROLE other
        ... GRANT ... TO name). DROP OWNED BY name doesn't clear those, so they
        block DROP ROLE ('privileges for default privileges belonging to role X')."""
        code, out, _ = pg_query(self.cfg, self.user, self.pwd,
            "SELECT pg_get_userbyid(d.defaclrole), COALESCE(n.nspname,''), d.defaclobjtype "
            "FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace "
            "WHERE EXISTS (SELECT 1 FROM aclexplode(d.defaclacl) a "
            f"WHERE a.grantee = (SELECT oid FROM pg_roles WHERE rolname = {pg_literal(name)}))",
            db=database)
        if code != 0 or not out.strip():
            return
        stmts = []
        for line in out.strip().split("\n"):
            p = line.split("\t")
            if len(p) < 3:
                continue
            owner, schema, objname = p[0].strip(), p[1].strip(), _DEFACL_OBJ.get(p[2].strip())
            if not owner or not objname:
                continue
            inschema = f"IN SCHEMA {pg_ident(schema)} " if schema else ""
            stmts.append(f"ALTER DEFAULT PRIVILEGES FOR ROLE {pg_ident(owner)} {inschema}"
                         f"REVOKE ALL ON {objname} FROM {pg_ident(name)}")
        self._run_each(stmts, db=database, strict=False)

    def _strip_role(self, name, drop):
        """Hand back everything a role owns or holds, across every database, so it
        can be dropped (or fully de-privileged) in one shot instead of failing on
        the first dependency. Objects are reassigned to the admin, never deleted."""
        ident = pg_ident(name)
        admin = pg_ident(self.user)
        ddb = self.cfg.get("default_db", "postgres")
        # Close its sessions first, or DROP hits "role is being used by N sessions".
        pg_exec(self.cfg, self.user, self.pwd,
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = {pg_literal(name)}")
        # REASSIGN/DROP OWNED need the *privileges of* the role, not just admin
        # rights over it: a non-superuser admin (the common case) is otherwise
        # told "only roles with privileges of role X may reassign objects". Grant
        # ourselves inheriting membership so the cascade is allowed. WITH INHERIT
        # is Postgres 16+; fall back to a plain grant on older servers.
        ok, _, _ = pg_exec(self.cfg, self.user, self.pwd,
                           f"GRANT {ident} TO {admin} WITH INHERIT TRUE")
        if not ok:
            ok, _, _ = pg_exec(self.cfg, self.user, self.pwd, f"GRANT {ident} TO {admin}")
        granted_membership = ok
        errors = []
        for db in self._connectable_dbs():
            # Make sure we can reach the db to clean it (a prior lock-down might
            # have taken our own CONNECT away). Superusers connect regardless.
            pg_exec(self.cfg, self.user, self.pwd,
                    f"GRANT CONNECT ON DATABASE {pg_ident(db)} TO {admin}", db=ddb)
            # REASSIGN moves its objects to the admin (kept, not deleted); DROP
            # OWNED then clears its privileges. Separate calls so the reassign
            # commits first and a table can't be lost.
            for stmt in (f"REASSIGN OWNED BY {ident} TO {admin}", f"DROP OWNED BY {ident}"):
                ok, out, err = pg_exec(self.cfg, self.user, self.pwd, stmt, db=db)
                if not ok and "does not exist" not in (err or "").lower():
                    errors.append(f"{db}: {(err or out).strip()[:120]}")
            # Default privileges that grant TO this role but belong to another role
            # aren't covered by DROP OWNED; revoke them so the drop isn't blocked.
            self._revoke_default_priv_grants(name, db)
        if drop:
            ok, out, err = pg_exec(self.cfg, self.user, self.pwd, f"DROP ROLE {ident}")
            if not ok:
                detail = f" (couldn't fully clean: {'; '.join(errors[:3])})" if errors else ""
                raise EngineError((err or out).strip() + detail)
        elif granted_membership:
            # Keeping the role but not dropping it: don't leave the admin sitting
            # as a member of a role it was only cleaning up.
            pg_exec(self.cfg, self.user, self.pwd, f"REVOKE {ident} FROM {admin}")

    def drop_user(self, name):
        self._strip_role(name, drop=True)
        return Mutation("DROP USER", name)

    def revoke_all(self, name):
        self._strip_role(name, drop=False)
        return Mutation("REVOKE ALL", f"{name}: all privileges revoked")

    def harden_connections(self):
        """Cluster-wide fix for 'new users can reach every database': take CONNECT
        off PUBLIC on every database and keep it only for the roles that use each
        one. After this a new account can't connect anywhere until it's granted,
        and a revoke actually blocks. Existing users with privileges are preserved."""
        dbs = self._connectable_dbs()
        for db in dbs:
            try:
                self._lockdown_connect(db)
            except EngineError:
                pass
        return Mutation("HARDEN", f"CONNECT locked down on {len(dbs)} database(s); "
                                  f"new users now reach only what they're granted")

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
