"""MySQL and MariaDB (the mysql CLI).

Everything here shells out to the mysql client through the primitives in
warden_core.mysql: my_query (tab-separated rows), my_csv (CSV with a header
row, used by the data browser), and my_exec (a write that reports ok/fail).
A couple of catalog queries differ between MariaDB and MySQL because MariaDB
moved the account-lock flag out of mysql.user, so is_mariadb() picks the query.

Row editing is deliberately absent: mysql is view-and-search only, so the CRUD
methods fall through to the base class NotSupported.
"""

import csv
import io

from warden_core.mysql import my_csv, my_exec, my_query
from warden_core.util import validate_ident
from warden_core.validation import (
    is_mariadb,
    mysql_account,
    sql_like_pattern,
    validate_mysql_privilege,
)

from .base import EngineAdapter, EngineError, Mutation, register


@register
class MysqlAdapter(EngineAdapter):
    family = "mysql"
    collection_key = "tables"
    has_login_toggle = True
    editable_rows = False

    # ── discovery / browse ──────────────────────────────────────────────────
    def ping(self):
        code, out, err = my_query(self.cfg, self.user, self.pwd, "SELECT CURRENT_USER()")
        if code != 0:
            raise EngineError(err or "Connection failed")
        return out.strip() or self.user

    def list_databases(self):
        sql = ("SELECT s.schema_name, COALESCE(SUM(t.data_length + t.index_length), 0) "
               "FROM information_schema.schemata s "
               "LEFT JOIN information_schema.tables t ON t.table_schema = s.schema_name "
               "WHERE s.schema_name NOT IN ('information_schema','performance_schema','sys') "
               "GROUP BY s.schema_name ORDER BY s.schema_name")
        code, out, err = my_query(self.cfg, self.user, self.pwd, sql)
        if code != 0:
            raise EngineError(err)
        dbs = []
        for line in out.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) >= 2:
                dbs.append({"name": parts[0], "size_bytes": int(float(parts[1])),
                            "size_mb": round(float(parts[1]) / 1048576, 1)})
        return dbs

    def list_collections(self, database):
        database = validate_ident(database, "database")
        sql = ("SELECT table_name, COALESCE(data_length + index_length, 0) "
               "FROM information_schema.tables WHERE table_schema = '%s' "
               "ORDER BY table_name" % database.replace("'", "''"))
        code, out, err = my_query(self.cfg, self.user, self.pwd, sql)
        if code != 0:
            raise EngineError(err)
        tables = []
        for line in out.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) >= 2:
                tables.append({"schema": database, "table": parts[0],
                               "size_bytes": int(float(parts[1]))})
        return tables

    def browse(self, target, limit, offset, search):
        database = validate_ident(target.database, "database")
        table = validate_ident(target.name, "table")
        rel = f"`{database}`.`{table}`"
        where = ""
        if search:
            code0, out0, _ = my_query(self.cfg, self.user, self.pwd,
                f"SELECT column_name FROM information_schema.columns "
                f"WHERE table_schema = '{database}' AND table_name = '{table}'", db=database)
            colnames = [l.strip() for l in out0.strip().split("\n") if l.strip()] if code0 == 0 else []
            pat = sql_like_pattern(search)
            if colnames:
                ors = " OR ".join(f"CAST(`{c}` AS CHAR) LIKE {pat} ESCAPE '\\\\'" for c in colnames)
                where = f" WHERE ({ors})"
        code, out, err = my_csv(self.cfg, self.user, self.pwd,
                                f"SELECT * FROM {rel}{where} LIMIT {limit} OFFSET {offset}", db=database)
        if code != 0:
            raise EngineError(err or "Query failed")
        reader = list(csv.reader(io.StringIO(out)))
        columns = reader[0] if reader else []
        rows = [r for r in reader[1:]] if len(reader) > 1 else []
        total = None
        if not search:
            code2, out2, _ = my_query(self.cfg, self.user, self.pwd,
                                      f"SELECT COUNT(*) FROM {rel}", db=database)
            if code2 == 0 and out2.strip().isdigit():
                total = int(out2.strip())
        return {"columns": columns, "rows": rows, "total": total,
                "estimated": False, "filtered": bool(search)}

    def object_stats(self, target):
        database = validate_ident(target.database, "database")
        table = validate_ident(target.name, "table")
        st = {}
        code, out, _ = my_query(self.cfg, self.user, self.pwd,
            f"SELECT table_rows, COALESCE(data_length+index_length,0) FROM information_schema.tables "
            f"WHERE table_schema='{database}' AND table_name='{table}'", db=database)
        if code == 0 and out.strip():
            p = out.strip().split("\t")
            if len(p) >= 2:
                st["rows"] = int(p[0]) if p[0].isdigit() else None
                st["estimated"] = True
                st["size_bytes"] = int(p[1]) if p[1].isdigit() else None
        code2, out2, _ = my_query(self.cfg, self.user, self.pwd,
            f"SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='{database}' AND table_name='{table}'", db=database)
        if code2 == 0 and out2.strip().isdigit():
            st["columns"] = int(out2.strip())
        return st

    def table_meta(self, target):
        # mysql is view-and-search only, so the editor is always locked out.
        return {"engine": "mysql", "editable": False,
                "reason": "row editing isn't supported for MySQL yet (view & search only)"}

    def health(self):
        health = {}
        checks = {
            "version": "SELECT VERSION()",
            "uptime": "SELECT variable_value FROM performance_schema.global_status WHERE variable_name = 'Uptime'",
            "threads": "SELECT variable_value FROM performance_schema.global_status WHERE variable_name = 'Threads_connected'",
            "max_connections": "SELECT @@max_connections",
            "total_size": ("SELECT COALESCE(SUM(data_length + index_length), 0) FROM information_schema.tables "
                           "WHERE table_schema NOT IN ('information_schema','performance_schema','sys')"),
        }
        for key, sql in checks.items():
            code, out, _ = my_query(self.cfg, self.user, self.pwd, sql)
            health[key] = out.strip() if code == 0 and out.strip() else None
        code, out, _ = my_query(self.cfg, self.user, self.pwd,
            "SELECT id, user, time, LEFT(COALESCE(info, ''), 90) FROM information_schema.processlist "
            "WHERE command <> 'Sleep' AND time >= 5 AND info IS NOT NULL ORDER BY time DESC LIMIT 10")
        slow = []
        if code == 0 and out.strip():
            for line in out.strip().split("\n"):
                p = line.split("\t")
                if len(p) >= 4:
                    slow.append({"pid": p[0], "user": p[1], "runtime": p[2] + "s", "query": p[3]})
        health["slow_queries"] = slow
        return health

    # ── users / ACL ─────────────────────────────────────────────────────────
    def list_users(self):
        excl = ("'mysql.sys','mysql.session','mysql.infoschema','mariadb.sys',"
                "'rdsadmin','rdsrepladmin'")
        if is_mariadb(self.cfg, self.user, self.pwd):
            sql = ("SELECT User, Host, IF(JSON_VALUE(Priv,'$.account_locked')=1,'Y','N') "
                   "FROM mysql.global_priv "
                   f"WHERE User NOT IN ({excl}) AND JSON_VALUE(Priv,'$.is_role') IS NULL "
                   "ORDER BY User, Host")
        else:
            sql = (f"SELECT user, host, account_locked FROM mysql.user "
                   f"WHERE user NOT IN ({excl}) ORDER BY user, host")
        code, out, err = my_query(self.cfg, self.user, self.pwd, sql)
        if code != 0:
            raise EngineError(err)
        users = []
        for line in out.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) >= 3:
                users.append({"user": f"{parts[0]}@{parts[1]}",
                              "can_login": parts[2] != "Y",
                              "superuser": False, "createdb": False,
                              "createrole": False, "valid_until": "never"})
        return users

    def user_info(self, name):
        acct = mysql_account(name)
        code, out, err = my_query(self.cfg, self.user, self.pwd, f"SHOW GRANTS FOR {acct}")
        if code != 0:
            raise EngineError(err.strip() or "User not found")
        grants = [l for l in out.strip().split("\n") if l.strip()]
        uname, _, host = name.partition("@")
        nm, ht = uname.replace("'", "''"), (host or '%').replace("'", "''")
        if is_mariadb(self.cfg, self.user, self.pwd):
            lock_sql = ("SELECT IF(JSON_VALUE(Priv,'$.account_locked')=1,'Y','N') "
                        f"FROM mysql.global_priv WHERE User = '{nm}' AND Host = '{ht}'")
        else:
            lock_sql = f"SELECT account_locked FROM mysql.user WHERE user = '{nm}' AND host = '{ht}'"
        code2, out2, _ = my_query(self.cfg, self.user, self.pwd, lock_sql)
        locked = out2.strip() == "Y"
        return {"user": name, "engine_family": "mysql", "locked": locked,
                "can_login": not locked, "grant_statements": grants}

    def create_user(self, name, password, **opts):
        acct = mysql_account(name)
        pwd_lit = password.replace("\\", "\\\\").replace("'", "\\'")
        ok, out, err = my_exec(self.cfg, self.user, self.pwd,
                               f"CREATE USER {acct} IDENTIFIED BY '{pwd_lit}'")
        if not ok:
            raise EngineError(err or out)
        return Mutation("CREATE USER", name, {"password": password})

    def create_database(self, name):
        db = validate_ident(name, "database")
        ok, out, err = my_exec(self.cfg, self.user, self.pwd, f"CREATE DATABASE `{db}`")
        if not ok:
            raise EngineError(err or out)
        return Mutation("CREATE DATABASE", db, {})

    def set_password(self, name, password):
        acct = mysql_account(name)
        pwd_lit = password.replace("\\", "\\\\").replace("'", "\\'")
        ok, out, err = my_exec(self.cfg, self.user, self.pwd,
                               f"ALTER USER {acct} IDENTIFIED BY '{pwd_lit}'")
        if not ok:
            raise EngineError(err or out)
        return Mutation("RESET PASSWORD", name, {"password": password})

    def grant(self, name, privilege=None, database=None, **opts):
        acct = mysql_account(name)
        priv = validate_mysql_privilege(privilege or "SELECT")
        db_raw = str(database or "*").strip() or "*"
        obj = "*.*" if db_raw == "*" else f"`{validate_ident(db_raw, 'database')}`.*"
        ok, out, err = my_exec(self.cfg, self.user, self.pwd, f"GRANT {priv} ON {obj} TO {acct}")
        if not ok:
            raise EngineError(err or out)
        return Mutation("GRANT", f"{name} += {priv} on {obj}")

    def revoke(self, name, privilege=None, database=None, **opts):
        acct = mysql_account(name)
        priv = validate_mysql_privilege(privilege or "SELECT")
        db_raw = str(database or "*").strip() or "*"
        obj = "*.*" if db_raw == "*" else f"`{validate_ident(db_raw, 'database')}`.*"
        ok, out, err = my_exec(self.cfg, self.user, self.pwd, f"REVOKE {priv} ON {obj} FROM {acct}")
        if not ok:
            raise EngineError(err or out)
        return Mutation("REVOKE", f"{name} -= {priv} on {obj}")

    def drop_user(self, name):
        acct = mysql_account(name)
        ok, out, err = my_exec(self.cfg, self.user, self.pwd, f"DROP USER {acct}")
        if not ok:
            raise EngineError(err or out)
        return Mutation("DROP USER", name)

    def toggle_login(self, name, enable):
        acct = mysql_account(name)
        kw = "UNLOCK" if enable else "LOCK"
        ok, out, err = my_exec(self.cfg, self.user, self.pwd, f"ALTER USER {acct} ACCOUNT {kw}")
        if not ok:
            raise EngineError(err or out)
        return Mutation("ENABLE USER" if enable else "DISABLE USER", name)

    # ── test a user's own login ─────────────────────────────────────────────
    def login_probe(self, test_user, test_pass, test_db=""):
        checks = []
        code, out, err = my_query(self.cfg, test_user, test_pass, "SELECT CURRENT_USER()")
        if code != 0:
            return {"auth": False, "error": (err or "").strip() or "Authentication failed"}
        code2, out2, _ = my_query(self.cfg, test_user, test_pass, "SHOW DATABASES")
        checks.append({"name": "Databases visible", "ok": code2 == 0,
                       "detail": ", ".join(out2.split()) if code2 == 0 and out2.strip() else "none"})
        grants = []
        code3, out3, _ = my_query(self.cfg, test_user, test_pass, "SHOW GRANTS")
        if code3 == 0:
            grants = [l for l in out3.split("\n") if l.strip()]
        if test_db:
            code4, out4, err4 = my_query(self.cfg, test_user, test_pass, "SHOW TABLES", db=test_db)
            checks.append({"name": f"Access '{test_db}'", "ok": code4 == 0,
                           "detail": (f"{len(out4.split())} table(s) visible" if code4 == 0 else (err4 or "").strip() or "denied")})
        return {"auth": True, "identity": out.strip(), "grants": grants, "checks": checks}
