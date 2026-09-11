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

# Every admin write runs with these: a held lock fails in seconds with a clear
# error instead of hanging the request forever (the "drop did nothing for three
# minutes" failure), and no single statement can run unbounded.
_ADMIN_OPTS = "-c lock_timeout=5s -c statement_timeout=60s"


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

        # Explicit database grants only (from each db's ACL). has_database_privilege
        # would also say yes for access inherited from PUBLIC, which made a brand
        # new user look like it had CONNECT everywhere, with revoke buttons that
        # couldn't possibly work. PUBLIC's cluster-wide access is reported apart.
        sql3 = f"""
            SELECT d.datname, a.privilege_type
            FROM pg_database d, aclexplode(d.datacl) a
            WHERE NOT d.datistemplate AND d.datname NOT IN ('rdsadmin')
              AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = {pg_literal(name)})
            ORDER BY 1, 2
        """
        code3, out3, _ = pg_query(self.cfg, self.user, self.pwd, sql3)
        by_db = {}
        if code3 == 0 and out3.strip():
            for line in out3.strip().split("\n"):
                p = line.split("\t")
                if len(p) >= 2 and p[0].strip():
                    by_db.setdefault(p[0].strip(), []).append(p[1].strip())
        db_privs = [{"database": d, "privileges": sorted(set(ps))} for d, ps in sorted(by_db.items())]

        sql3b = """
            SELECT datname FROM pg_database d
            WHERE NOT d.datistemplate AND d.datallowconn AND d.datname NOT IN ('rdsadmin')
              AND (d.datacl IS NULL OR EXISTS (
                    SELECT 1 FROM aclexplode(d.datacl) a
                    WHERE a.grantee = 0 AND a.privilege_type = 'CONNECT'))
            ORDER BY 1
        """
        code3b, out3b, _ = pg_query(self.cfg, self.user, self.pwd, sql3b)
        public_connect = ([l.strip() for l in out3b.strip().split("\n") if l.strip()]
                          if code3b == 0 and out3b.strip() else [])

        code3c, out3c, _ = pg_query(self.cfg, self.user, self.pwd,
            "SELECT datname FROM pg_database WHERE NOT datistemplate "
            f"AND datdba = (SELECT oid FROM pg_roles WHERE rolname = {pg_literal(name)}) ORDER BY 1")
        owned_dbs = ([l.strip() for l in out3c.strip().split("\n") if l.strip()]
                     if code3c == 0 and out3c.strip() else [])

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
        info["public_connect"] = public_connect if info["can_login"] else []
        info["owned_databases"] = owned_dbs
        info["member_of"] = member_of
        return info

    def create_user(self, name, password, can_login=True, lockdown=False, **opts):
        login = "LOGIN" if can_login else "NOLOGIN"
        sql = f"CREATE USER {pg_ident(name)} WITH {login} PASSWORD {pg_literal(password)}"
        ok, out, err = pg_exec(self.cfg, self.user, self.pwd, sql)
        if not ok:
            raise EngineError(err or out)
        m = Mutation("CREATE USER", name, {"password": password})
        if lockdown:
            # Postgres's default lets ANY new role connect to every database via
            # PUBLIC. Zero-access-by-default means taking CONNECT off PUBLIC and
            # keeping it only for the roles already using each database.
            hardened = self.harden_connections()
            m.response["summary"] = (f"{name} created with zero access "
                                     f"({hardened.detail.split(';')[0]}); grant what they need.")
        else:
            code2, out2, _ = pg_query(self.cfg, self.user, self.pwd,
                "SELECT count(*) FROM pg_database d WHERE NOT d.datistemplate AND d.datallowconn "
                "AND (d.datacl IS NULL OR EXISTS (SELECT 1 FROM aclexplode(d.datacl) a "
                "WHERE a.grantee = 0 AND a.privilege_type = 'CONNECT'))")
            open_dbs = int(out2.strip()) if code2 == 0 and out2.strip().isdigit() else 0
            if can_login and open_dbs:
                m.response["warning"] = (
                    f"{name} can already connect to {open_dbs} database(s) - Postgres grants "
                    f"CONNECT to everyone via PUBLIC by default. Use 'Lock down connections' "
                    f"(or recreate with zero access) to make access explicit.")
        return m

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

    def _each_table_priv(self, name, priv, schema, database, grant=True):
        """GRANT or REVOKE a table privilege one table at a time, skipping the
        tables we have no rights on (owned by another role). Returns the skipped
        table names. This is the fallback for when a single ALL TABLES statement
        would abort the whole operation because one table isn't ours."""
        verb, prep = ("GRANT", "TO") if grant else ("REVOKE", "FROM")
        code, out, _ = pg_query(self.cfg, self.user, self.pwd,
            f"SELECT tablename FROM pg_tables WHERE schemaname = {pg_literal(schema)} ORDER BY tablename",
            db=database)
        if code != 0:
            return []
        skipped = []
        for t in (l.strip() for l in out.strip().split("\n") if l.strip()):
            ok, _o, _e = pg_exec(self.cfg, self.user, self.pwd,
                f"{verb} {priv} ON {pg_ident(schema)}.{pg_ident(t)} {prep} {pg_ident(name)}", db=database)
            if not ok:
                skipped.append(t)
        return skipped

    def _user_schemas(self, database):
        """Non-system schemas in the database that actually contain tables. This
        is what makes a grant find the data wherever it lives, instead of assuming
        everything is in public (it usually isn't for a real app)."""
        code, out, _ = pg_query(self.cfg, self.user, self.pwd,
            "SELECT DISTINCT schemaname FROM pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog','information_schema') "
            "AND schemaname NOT LIKE 'pg\\_temp%' AND schemaname NOT LIKE 'pg\\_toast%' "
            "ORDER BY schemaname", db=database)
        return [l.strip() for l in out.strip().split("\n") if l.strip()] if code == 0 else []

    def _target_schemas(self, schema, database):
        """Resolve which schemas a grant applies to. A specific name targets just
        that schema; blank / '*' / 'all' means every user schema that has tables,
        so 'give this user access to the database' works no matter where the
        tables are. Falls back to public for an empty database."""
        want = (schema or "").strip()
        if want and want.lower() not in ("*", "all"):
            return [validate_ident(want, "schema")]
        return self._user_schemas(database) or ["public"]

    def _priv_effect(self, name, priv, schemas, database):
        """Read back what actually landed: (total tables, tables `name` can really
        use, verified, can_connect). Real access needs CONNECT on the database AND
        USAGE on the schema AND the table privilege, so all three are checked - a
        grant that leaves the user unable to even reach the database is not a
        grant. verified is False if the read-back itself failed. 'ALL PRIVILEGES'
        is probed via SELECT."""
        probe = "SELECT" if priv == "ALL PRIVILEGES" else priv
        total = granted = 0
        verified = True
        code, out, _ = pg_query(self.cfg, self.user, self.pwd,
            f"SELECT has_database_privilege({pg_literal(name)}, {pg_literal(database)}, 'CONNECT')")
        can_connect = code == 0 and out.strip() == "t"
        for sch in schemas:
            code, out, _ = pg_query(self.cfg, self.user, self.pwd,
                "SELECT count(*), count(*) FILTER (WHERE "
                f"has_schema_privilege({pg_literal(name)}, schemaname, 'USAGE') AND "
                f"has_table_privilege({pg_literal(name)}, format('%I.%I', schemaname, tablename), {pg_literal(probe)})) "
                f"FROM pg_tables WHERE schemaname = {pg_literal(sch)}", db=database)
            p = out.strip().split("\t")
            if code == 0 and len(p) >= 2:
                total += int(p[0]); granted += int(p[1])
            else:
                verified = False
        return total, granted, verified, can_connect

    def _has_priv(self, name, database, priv):
        code, out, _ = pg_query(self.cfg, self.user, self.pwd,
            f"SELECT has_database_privilege({pg_literal(name)}, {pg_literal(database)}, {pg_literal(priv)})")
        return code == 0 and out.strip() == "t"

    def _db_priv_grantors(self, name, database, priv):
        """Who granted `priv` on `database` to `name` (from the db's ACL). The
        answer decides whether we can revoke it: only the grantor or a superuser
        can remove someone else's grant, and Postgres won't error, it just
        silently revokes nothing."""
        code, out, _ = pg_query(self.cfg, self.user, self.pwd,
            "SELECT DISTINCT pg_get_userbyid(a.grantor) FROM pg_database d, aclexplode(d.datacl) a "
            f"WHERE d.datname = {pg_literal(database)} AND a.privilege_type = {pg_literal(priv)} "
            f"AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = {pg_literal(name)})")
        return [l.strip() for l in out.strip().split("\n") if l.strip()] if code == 0 else []

    def grant(self, name, privilege=None, database=None, schema=None, **opts):
        priv = validate_pg_privilege(privilege or "SELECT")
        database = validate_ident(database or "postgres", "database")
        ddb = self.cfg.get("default_db", "postgres")
        if priv == "CONNECT":
            ok, out, err = pg_exec(self.cfg, self.user, self.pwd,
                f"GRANT CONNECT ON DATABASE {pg_ident(database)} TO {pg_ident(name)}", db=ddb)
            if not ok:
                raise EngineError(f"Couldn't grant CONNECT on {database}: {(err or out).strip()}")
            if not self._has_priv(name, database, "CONNECT"):
                raise EngineError(f"GRANT ran but {name} still can't connect to {database}")
            return Mutation("GRANT", f"{name} += CONNECT on {database}",
                            {"summary": f"{name} can now connect to {database}"})
        if priv == "CREATE":
            # Database-level CREATE only allows CREATE SCHEMA. What people mean
            # by "can create" is tables, and since Postgres 15 the public schema
            # is no longer writable by default, so grant CREATE on the schemas
            # too - otherwise this "works" while the user still can't create
            # anything, which is exactly how it used to fail.
            stmts = [f"GRANT CONNECT ON DATABASE {pg_ident(database)} TO {pg_ident(name)}"]
            ok, out, err = pg_exec(self.cfg, self.user, self.pwd,
                f"GRANT CREATE ON DATABASE {pg_ident(database)} TO {pg_ident(name)}", db=ddb)
            if not ok:
                raise EngineError(f"Couldn't grant CREATE on {database}: {(err or out).strip()}")
            pg_exec(self.cfg, self.user, self.pwd, stmts[0], db=ddb)
            schemas = self._target_schemas(schema, database)
            created_in = []
            for sch in schemas:
                ok, _o, _e = pg_exec(self.cfg, self.user, self.pwd,
                    f"GRANT USAGE, CREATE ON SCHEMA {pg_ident(sch)} TO {pg_ident(name)}", db=database)
                if ok:
                    created_in.append(sch)
            m = Mutation("GRANT", f"{name} += CREATE on {database} ({', '.join(created_in) or 'database only'})")
            if created_in:
                m.response["summary"] = (f"{name} can now create tables in {database} "
                                         f"(schemas: {', '.join(created_in)}) and new schemas")
            else:
                m.response["warning"] = (
                    f"{name} got database-level CREATE (new schemas), but no existing schema "
                    f"accepted a CREATE grant - they're owned by another role, so {name} still "
                    f"can't create tables in them. Grant as the schema owner or a superuser.")
            return m
        if priv == "USAGE":
            # USAGE is a schema privilege, not a table one; grant it on the schemas.
            schemas = self._target_schemas(schema, database)
            for sch in schemas:
                ok, out, err = pg_exec(self.cfg, self.user, self.pwd,
                    f"GRANT USAGE ON SCHEMA {pg_ident(sch)} TO {pg_ident(name)}", db=database)
                if not ok:
                    raise EngineError(f"Couldn't grant USAGE on {database}.{sch}: {(err or out).strip()}")
            return Mutation("GRANT", f"{name} += USAGE on {database} ({', '.join(schemas)})")
        # Table privilege, applied across every target schema. Table access is a
        # three-part key: CONNECT on the database, USAGE on the schema, and the
        # privilege on the table - grant all three, or the user "has SELECT" on
        # tables in a database they can't even log in to (exactly what happens
        # after a connect lock-down). We try one ALL TABLES statement per schema
        # (fast when we own everything) and fall back to table-by-table when a
        # table owned by another role would otherwise abort the whole thing.
        pg_exec(self.cfg, self.user, self.pwd,
                f"GRANT CONNECT ON DATABASE {pg_ident(database)} TO {pg_ident(name)}", db=ddb)
        schemas = self._target_schemas(schema, database)
        for sch in schemas:
            pg_exec(self.cfg, self.user, self.pwd,
                    f"GRANT USAGE ON SCHEMA {pg_ident(sch)} TO {pg_ident(name)}", db=database)
            allok, _, _ = pg_exec(self.cfg, self.user, self.pwd,
                f"GRANT {priv} ON ALL TABLES IN SCHEMA {pg_ident(sch)} TO {pg_ident(name)}", db=database)
            if not allok:
                self._each_table_priv(name, priv, sch, database, grant=True)
            pg_exec(self.cfg, self.user, self.pwd,
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {pg_ident(sch)} GRANT {priv} ON TABLES TO {pg_ident(name)}",
                db=database)
        # Don't take the driver's word for it - read back what the grantee can
        # actually use, and report the honest result (including "nothing happened").
        total, granted, verified, can_connect = self._priv_effect(name, priv, schemas, database)
        where = ", ".join(schemas)
        m = Mutation("GRANT", f"{name} += {priv} on {database} ({where})")
        if verified and not can_connect:
            raise EngineError(
                f"{priv} was granted on tables, but {name} cannot CONNECT to {database}, so they "
                f"can't use any of it. Granting CONNECT failed - warden connects as {self.user}; "
                f"grant CONNECT as the database owner or a superuser, then retry.")
        if verified and total == 0:
            m.response["warning"] = (
                f"No tables found in {database} (schemas checked: {where}), so there was "
                f"nothing to grant {priv} on. If the user just needs to log in, grant CONNECT.")
        elif verified and granted == 0:
            raise EngineError(
                f"Nothing was granted: 0 of {total} tables in {database} now have {priv}. warden "
                f"connects as {self.user}, which can only grant on tables it owns - these are owned "
                f"by another role. Connect as a superuser or the owning role and try again.")
        elif verified and granted < total:
            m.response["warning"] = (
                f"Granted {priv} on {granted} of {total} tables across {where}. The other "
                f"{total - granted} are owned by another role, which {self.user} can't grant on - "
                f"connect as a superuser or the owner to include them.")
        else:
            n = granted if verified else "the"
            m.response["summary"] = f"Granted {priv} on {n} table(s) across {where}; {name} can connect and read them."
        return m

    def _membership_names(self, name, limit=6):
        code, out, _ = pg_query(self.cfg, self.user, self.pwd,
            "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
            f"JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = {pg_literal(name)} LIMIT {limit}")
        return [l.strip() for l in out.strip().split("\n") if l.strip()] if code == 0 else []

    def revoke(self, name, privilege=None, database=None, schema=None, **opts):
        """Every branch here re-reads the catalogs after revoking. Postgres makes
        that mandatory for honesty: REVOKE issued by a role that isn't the grantor
        (and isn't a superuser) succeeds with only a warning while removing
        NOTHING, so without a read-back warden would happily report a revoke that
        never happened."""
        priv = validate_pg_privilege(privilege or "SELECT")
        database = validate_ident(database or "postgres", "database")
        ddb = self.cfg.get("default_db", "postgres")
        if priv == "CONNECT":
            # Revoke it from the user AND take it off PUBLIC (re-granting the roles
            # that use the db), so the user genuinely can't connect. Just revoking
            # from the user is a no-op while PUBLIC still allows everyone in.
            kept = self._lockdown_connect(database, exclude=name)
            m = Mutation("REVOKE", f"{name} -= CONNECT on {database}")
            if self._has_priv(name, database, "CONNECT"):
                grantors = [g for g in self._db_priv_grantors(name, database, "CONNECT") if g != self.user]
                if grantors:
                    raise EngineError(
                        f"{name} can still connect to {database}: their CONNECT was granted by "
                        f"{', '.join(grantors)}, and only the grantor or a superuser can revoke it "
                        f"(warden is connected as {self.user}).")
                m.response["warning"] = (
                    f"{name} can still connect to {database} - they own the database or inherit "
                    f"access through a role ({', '.join(self._membership_names(name)) or 'none listed'}).")
            else:
                m.response["summary"] = (
                    f"{name} can no longer connect to {database}. CONNECT is now explicit "
                    f"({len(kept)} other role(s) kept access).")
            return m
        if priv == "CREATE":
            # Mirror of grant: db-level CREATE plus the per-schema CREATE.
            pg_exec(self.cfg, self.user, self.pwd,
                    f"REVOKE CREATE ON DATABASE {pg_ident(database)} FROM {pg_ident(name)}", db=ddb)
            for sch in self._target_schemas(schema, database):
                pg_exec(self.cfg, self.user, self.pwd,
                        f"REVOKE CREATE ON SCHEMA {pg_ident(sch)} FROM {pg_ident(name)}", db=database)
            m = Mutation("REVOKE", f"{name} -= CREATE on {database}")
            if self._has_priv(name, database, "CREATE"):
                grantors = [g for g in self._db_priv_grantors(name, database, "CREATE") if g != self.user]
                if grantors:
                    raise EngineError(
                        f"{name} still has CREATE on {database}: it was granted by {', '.join(grantors)}, "
                        f"and only the grantor or a superuser can revoke it (warden is connected as "
                        f"{self.user}).")
                m.response["warning"] = (
                    f"{name} still has CREATE on {database} - they own the database or inherit it "
                    f"through a role.")
            else:
                m.response["summary"] = f"{name} can no longer create in {database}."
            return m
        # Table (or USAGE) privilege, across every target schema. Same all-or-nothing
        # trap as grant: one table we don't own would abort REVOKE ON ALL TABLES, so
        # fall back to table-by-table.
        schemas = self._target_schemas(schema, database)
        for sch in schemas:
            if priv == "USAGE":
                pg_exec(self.cfg, self.user, self.pwd,
                    f"REVOKE USAGE ON SCHEMA {pg_ident(sch)} FROM {pg_ident(name)}", db=database)
                continue
            allok, _, _ = pg_exec(self.cfg, self.user, self.pwd,
                f"REVOKE {priv} ON ALL TABLES IN SCHEMA {pg_ident(sch)} FROM {pg_ident(name)}", db=database)
            if not allok:
                self._each_table_priv(name, priv, sch, database, grant=False)
            pg_exec(self.cfg, self.user, self.pwd,
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {pg_ident(sch)} REVOKE {priv} ON TABLES FROM {pg_ident(name)}",
                db=database)
        where = ", ".join(schemas)
        m = Mutation("REVOKE", f"{name} -= {priv} on {database} ({where})")
        if priv == "USAGE":
            return m
        # Read back the ACTUAL table ACLs, not information_schema (a non-superuser
        # can't see grants it isn't party to there, so a silent no-op would read
        # as success). Count direct grants still on this role, and name their
        # grantor, straight from pg_class.relacl via aclexplode.
        pfilter = "" if priv == "ALL PRIVILEGES" else f"AND a.privilege_type = {pg_literal(priv)} "
        in_schemas = ", ".join(pg_literal(s) for s in schemas)
        code, out, _ = pg_query(self.cfg, self.user, self.pwd,
            "SELECT count(*), COALESCE(string_agg(DISTINCT pg_get_userbyid(a.grantor), ', '), '') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a "
            f"WHERE c.relkind IN ('r','p','v','m','f') AND n.nspname IN ({in_schemas}) "
            f"AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = {pg_literal(name)}) {pfilter}",
            db=database)
        p = out.strip().split("\t") if code == 0 and out.strip() else ["0", ""]
        residual, grantors = (int(p[0]) if p[0].isdigit() else 0), (p[1] if len(p) > 1 else "")
        if residual:
            raise EngineError(
                f"{residual} table grant(s) in {database} could not be revoked: they were granted "
                f"by {grantors or 'another role'}, and only the grantor or a superuser can remove "
                f"them (warden is connected as {self.user}).")
        _t, still, verified, _c = self._priv_effect(name, priv, schemas, database)
        if verified and still:
            members = ", ".join(self._membership_names(name)) or "PUBLIC"
            m.response["warning"] = (
                f"No direct grants remain, but {name} can still use {still} table(s) through "
                f"role membership or defaults ({members}). Revoke there to fully remove access.")
        else:
            m.response["summary"] = f"{name} lost {priv} on {database} ({where}); verified."
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

    # ── bounded admin execution ─────────────────────────────────────────────
    # Role cleanup takes locks (REASSIGN/DROP OWNED want exclusive locks on the
    # role's objects). Any other session sitting in an open transaction on one
    # of those tables would block us FOREVER without a lock_timeout, which the
    # UI experiences as minutes of silence. These helpers run every sweep on one
    # short-lived connection with lock_timeout + statement_timeout set.

    def _bounded_statements(self, db, statements):
        """Run statements in order on one bounded connection. Never raises;
        returns [(statement, ok, error)] so callers report per-statement."""
        results = []
        if pn.available():
            try:
                conn = pn.psycopg.connect(pn._conninfo(self.cfg, db, self.user, self.pwd),
                                          options=_ADMIN_OPTS, autocommit=True)
            except Exception as e:
                return [(s, False, str(e).strip()) for s in statements]
            try:
                for s in statements:
                    try:
                        conn.execute(s)
                        results.append((s, True, ""))
                    except Exception as e:
                        results.append((s, False, str(e).strip()))
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            return results
        # psql fallback: the SETs ride in the same -c call, so they apply.
        for s in statements:
            ok, out, err = pg_exec(self.cfg, self.user, self.pwd,
                                   f"SET lock_timeout TO '5s'; SET statement_timeout TO '60s'; {s}",
                                   db=db, timeout=70)
            results.append((s, ok, "" if ok else (err or out).strip()))
        return results

    @staticmethod
    def _friendly_sql_error(err):
        """Turn the two bounded-timeout errors into something a person can act on."""
        low = (err or "").lower()
        if "lock timeout" in low:
            return ("blocked by another session holding a lock (close open "
                    "transactions touching this user's tables and retry)")
        if "statement timeout" in low:
            return "took longer than 60s and was stopped (retry when the database is quieter)"
        return err

    def _dependent_dbs(self, name):
        """Databases that actually contain objects or privileges tied to the
        role, straight from pg_shdepend. Cleanup visits only these instead of
        every database on the cluster, which is the difference between seconds
        and minutes on a big instance. None means the lookup failed."""
        code, out, _ = pg_query(self.cfg, self.user, self.pwd,
            "SELECT DISTINCT COALESCE(d.datname, '') FROM pg_shdepend s "
            "LEFT JOIN pg_database d ON d.oid = s.dbid "
            "WHERE s.refclassid = 'pg_authid'::regclass "
            f"AND s.refobjid = (SELECT oid FROM pg_roles WHERE rolname = {pg_literal(name)})")
        if code != 0:
            return None
        dbs = {l.strip() for l in out.strip().split("\n") if l.strip()}
        # Shared objects (db ownerships, db-level grants) show as dbid 0; they
        # are cleaned from the default db, so it is always on the list.
        dbs.add(self.cfg.get("default_db", "postgres"))
        return sorted(d for d in dbs if d)

    def _describe_blockers(self, name, limit=6):
        """Name the exact objects still pinning a role, per database, with the
        owner where we can get it. Postgres's own error ("N objects in database
        X") hides the names when they live in another db; this does not."""
        items = []
        for db in (self._dependent_dbs(name) or []):
            code, out, _ = pg_query(self.cfg, self.user, self.pwd,
                "SELECT pg_describe_object(s.classid, s.objid, s.objsubid), s.deptype "
                "FROM pg_shdepend s "
                "WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) "
                "AND s.refclassid = 'pg_authid'::regclass "
                f"AND s.refobjid = (SELECT oid FROM pg_roles WHERE rolname = {pg_literal(name)}) "
                "LIMIT 8", db=db)
            if code != 0 or not out.strip():
                continue
            for line in out.strip().split("\n"):
                p = line.split("\t")
                if not p or not p[0].strip():
                    continue
                kind = "privileges on " if len(p) > 1 and p[1].strip() == "a" else "owns "
                items.append(f"{db}: {kind}{p[0].strip()}")
                if len(items) >= limit:
                    return items
        return items

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
        self._bounded_statements(database, stmts)

    def _strip_role(self, name, drop):
        """Hand back everything a role owns or holds so it can be dropped (or
        fully de-privileged) in one shot. Objects are reassigned to the admin,
        never deleted. Visits only the databases pg_shdepend says matter, runs
        everything bounded (a held lock fails in ~5s with a clear message
        instead of hanging), and names the exact blockers when it can't finish."""
        import time as _time
        t0 = _time.perf_counter()
        if name == self.user:
            raise EngineError(f"warden is connected as {name}; it won't strip or drop its own account")
        ident = pg_ident(name)
        admin = pg_ident(self.user)
        ddb = self.cfg.get("default_db", "postgres")
        if drop:
            # Belt and braces before the teardown: no fresh logins can sneak in
            # between the terminate below and the DROP.
            self._bounded_statements(ddb, [f"ALTER ROLE {ident} NOLOGIN",
                                           f"ALTER ROLE {ident} CONNECTION LIMIT 0"])
        # Close its sessions, or DROP hits "role is being used by N sessions"
        # and DROP OWNED can deadlock against its own locks.
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
        # Only the databases that actually hold something of this role's.
        dbs = self._dependent_dbs(name)
        if dbs is None:
            dbs = self._connectable_dbs()
        errors = []
        for db in dbs:
            # Make sure we can reach the db to clean it (a prior lock-down might
            # have taken our own CONNECT away). Superusers connect regardless.
            pg_exec(self.cfg, self.user, self.pwd,
                    f"GRANT CONNECT ON DATABASE {pg_ident(db)} TO {admin}", db=ddb)
            # REASSIGN moves its objects to the admin (kept, not deleted); DROP
            # OWNED then clears its privileges. One bounded connection per db.
            for stmt, ok, err in self._bounded_statements(
                    db, [f"REASSIGN OWNED BY {ident} TO {admin}", f"DROP OWNED BY {ident}"]):
                if not ok and "does not exist" not in (err or "").lower():
                    errors.append(f"{db}: {self._friendly_sql_error(err)[:160]}")
            # Default privileges that grant TO this role but belong to another role
            # aren't covered by DROP OWNED everywhere; revoke them so the drop
            # isn't blocked.
            self._revoke_default_priv_grants(name, db)
        took = _time.perf_counter() - t0
        summary = f"cleaned {len(dbs)} database(s) in {took:.1f}s"
        if drop:
            _, ok, err = self._bounded_statements(ddb, [f"DROP ROLE {ident}"])[0]
            if not ok:
                blockers = self._describe_blockers(name)
                why = ("; ".join(errors[:2]) + "; " if errors else "")
                held = (" Still held: " + "; ".join(blockers) + "."
                        if blockers else "")
                raise EngineError(
                    f"couldn't drop {name}: {self._friendly_sql_error(err)[:200]}. {why}{held} "
                    f"Grants made by another role can only be removed by that role or a "
                    f"superuser - warden is connected as {self.user}.")
            return summary, errors
        if granted_membership:
            # Keeping the role but not dropping it: don't leave the admin sitting
            # as a member of a role it was only cleaning up.
            pg_exec(self.cfg, self.user, self.pwd, f"REVOKE {ident} FROM {admin}")
        return summary, errors

    def drop_user(self, name):
        summary, errors = self._strip_role(name, drop=True)
        # Trust the catalog, not our own success path: the role must be gone.
        code, out, _ = pg_query(self.cfg, self.user, self.pwd,
            f"SELECT count(*) FROM pg_roles WHERE rolname = {pg_literal(name)}")
        if code == 0 and out.strip() and out.strip() != "0":
            raise EngineError(f"{name} still exists after the drop; " + "; ".join(errors[:3]))
        m = Mutation("DROP USER", name, {"summary": f"{name} dropped; {summary}"})
        if errors:
            m.response["warning"] = f"{name} was dropped, but some cleanup steps failed: " + "; ".join(errors[:3])
        return m

    def revoke_all(self, name):
        summary, errors = self._strip_role(name, drop=False)
        # Read back what the user can still touch, and say so instead of a
        # blanket "revoked" that might not be true (grants made by other roles
        # survive DROP OWNED run by a non-superuser).
        code, out, _ = pg_query(self.cfg, self.user, self.pwd,
            "SELECT count(*) FROM information_schema.role_table_grants "
            f"WHERE grantee = {pg_literal(name)}")
        residual = int(out.strip()) if code == 0 and out.strip().isdigit() else 0
        m = Mutation("REVOKE ALL", f"{name}: all privileges revoked",
                     {"summary": f"revoked {name}'s access; {summary}"})
        if residual:
            blockers = self._describe_blockers(name, limit=4)
            m.response["warning"] = (
                f"{residual} grant(s) could not be removed (made by another role, and only "
                f"the grantor or a superuser can revoke them): " + "; ".join(blockers))
        elif errors:
            m.response["warning"] = "some cleanup steps failed: " + "; ".join(errors[:3])
        return m

    def harden_connections(self):
        """Cluster-wide fix for 'new users can reach every database': take CONNECT
        off PUBLIC on every database and keep it only for the roles that use each
        one. After this a new account can't connect anywhere until it's granted,
        and a revoke actually blocks. Existing users with privileges are preserved."""
        dbs = self._connectable_dbs()
        locked, couldnt = [], []
        for db in dbs:
            try:
                self._lockdown_connect(db)
            except EngineError:
                pass
            # Verify, don't assume: a non-superuser admin can't REVOKE on a
            # database it doesn't own, and that REVOKE fails silently.
            if self._public_can_connect(db):
                couldnt.append(db)
            else:
                locked.append(db)
        detail = (f"CONNECT locked down on {len(locked)} of {len(dbs)} database(s); "
                  f"new users now reach only what they're granted")
        m = Mutation("HARDEN", detail)
        if couldnt:
            m.response["warning"] = (
                f"Couldn't lock down {len(couldnt)} database(s) ({', '.join(couldnt[:6])}"
                f"{'...' if len(couldnt) > 6 else ''}): warden connects as {self.user}, which "
                f"doesn't own them, so PUBLIC still grants CONNECT there. Run as the owner or a "
                f"superuser to finish.")
        else:
            m.response["summary"] = f"Locked down CONNECT on all {len(locked)} database(s)."
        return m

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
