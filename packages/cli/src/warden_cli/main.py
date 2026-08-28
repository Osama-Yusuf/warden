#!/usr/bin/env python3
"""
warden: Database User & Permission Manager

Interactive CLI for managing users, passwords, and permissions
on DocumentDB (MongoDB) and PostgreSQL (RDS) across environments.

Usage:
  warden                                    Interactive mode
  warden docdb list-users                   List DocumentDB users
  warden docdb user-info alice              Check user details
  warden docdb create-user alice            Create user (prompts for details)
  warden pg list-users                      List PostgreSQL users
  warden --env staging pg list-databases    Different environment
  warden --dry-run docdb grant alice ...    Preview without executing

Credentials:
  Set WARDEN_ADMIN_USER and WARDEN_ADMIN_PASS env vars (or a .env file), use
  --admin-user / --admin-pass flags, or you'll be prompted.

Requirements:
  Structured commands use bundled native drivers (pymongo / psycopg). mongosh
  and psql are only needed for the free-form query console.
"""

import argparse
import json
import os
import re
import shutil
import sys
import textwrap
from getpass import getpass

from warden_core import (
    ENVIRONMENTS,
    PG_PRIVILEGES,
    audit,
    check_tool,
    docdb_args,
    format_size,
    generate_password,
    js_string,
    pg_exec,
    pg_ident,
    pg_literal,
    pg_query,
    run_cmd,
)
from warden_core import mongo_native as mn

# ---------------------------------------------------------------------------
# Rich (optional). Nice output when installed, plain text fallback otherwise
# ---------------------------------------------------------------------------

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.prompt import Prompt, Confirm
    from rich.text import Text
    from rich.theme import Theme
    from rich import box

    THEME = Theme({
        "info": "cyan",
        "success": "green",
        "warning": "yellow",
        "danger": "bold red",
        "muted": "dim",
        "header": "bold white",
    })
    console = Console(theme=THEME)
    HAS_RICH = True

except ImportError:
    HAS_RICH = False

    class _FakeConsole:
        def print(self, *a, **kw):
            text = " ".join(str(x) for x in a)
            text = re.sub(r"\[/?[a-z_ ]+\]", "", text)
            print(text)

        def status(self, msg):
            class _ctx:
                def __enter__(s): print(f"  {msg}"); return s
                def __exit__(s, *a): pass
            return _ctx()

        def rule(self, title="", **kw):
            w = shutil.get_terminal_size((80, 24)).columns
            if title:
                pad = (w - len(title) - 4) // 2
                print(f"{'─' * pad}  {title}  {'─' * pad}")
            else:
                print("─" * w)

    console = _FakeConsole()

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def print_table(title, columns, rows):
    if HAS_RICH:
        t = Table(title=title, box=box.ROUNDED, show_lines=True)
        for col in columns:
            t.add_column(col, style="cyan" if col == columns[0] else "")
        for row in rows:
            t.add_row(*[str(c) for c in row])
        console.print(t)
    else:
        console.print(f"\n  {title}")
        console.print("  " + "─" * 60)
        header = "  ".join(f"{c:<20}" for c in columns)
        console.print(f"  {header}")
        console.print("  " + "─" * 60)
        for row in rows:
            line = "  ".join(f"{str(c):<20}" for c in row)
            console.print(f"  {line}")
        console.print()


def print_panel(title, body):
    if HAS_RICH:
        console.print(Panel(body, title=title, border_style="cyan"))
    else:
        console.print(f"\n┌─ {title} ─┐")
        for line in body.split("\n"):
            console.print(f"│ {line}")
        console.print(f"└{'─' * (len(title) + 4)}┘\n")


def prompt(msg, default=None, choices=None, password=False):
    if HAS_RICH and not password:
        return Prompt.ask(msg, default=default, choices=choices)
    if password:
        return getpass(f"{msg}: ")
    suffix = f" [{default}]" if default else ""
    if choices:
        suffix += f" ({'/'.join(choices)})"
    val = input(f"{msg}{suffix}: ").strip()
    if not val and default:
        return default
    return val


def confirm(msg, default=False):
    if HAS_RICH:
        return Confirm.ask(msg, default=default)
    answer = input(f"{msg} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def show_header(env, engine, host):
    if HAS_RICH:
        console.print(Panel(
            f"[header]Environment:[/] [info]{env}[/]  │  "
            f"[header]Engine:[/] [info]{engine}[/]\n"
            f"[header]Host:[/] [muted]{host}[/]",
            title="[bold]warden[/] · Database Manager",
            border_style="cyan",
        ))
    else:
        console.print(f"\n  warden · Database Manager")
        console.print(f"  Environment: {env}  |  Engine: {engine}")
        console.print(f"  Host: {host}\n")


# ---------------------------------------------------------------------------
# DocumentDB Backend
# ---------------------------------------------------------------------------

class DocumentDB:
    def __init__(self, env_name, config, admin_user, admin_pass, dry_run=False):
        self.env = env_name
        self.config = config
        self.host = config["host"]
        self.port = config["port"]
        self.auth_db = config.get("auth_db", "admin")
        self.tls = config.get("tls", False)
        self.admin_user = admin_user
        self.admin_pass = admin_pass
        self.dry_run = dry_run
        self.engine = "documentdb"

    def _mongosh_args(self, db=None):
        return docdb_args(self.config, self.admin_user, self.admin_pass, db or self.auth_db)

    def _eval(self, js, db=None, timeout=30):
        args = self._mongosh_args(db) + ["--eval", js]
        return run_cmd(args, timeout=timeout)

    def _eval_json(self, js, db=None):
        wrapped = f"JSON.stringify({js})"
        code, out, err = self._eval(wrapped, db)
        if code != 0:
            return None, err
        try:
            for line in out.strip().split("\n"):
                line = line.strip()
                if line.startswith("{") or line.startswith("["):
                    return json.loads(line), None
            return None, f"No JSON in output: {out[:200]}"
        except json.JSONDecodeError as e:
            return None, f"JSON parse error: {e}\nRaw: {out[:300]}"

    # Native pymongo path: same warden_core module the web/desktop app uses.
    # Skips the ~2s mongosh (node) cold start per command. Falls back to the
    # mongosh subprocess if pymongo isn't importable.
    def _native(self):
        return mn.available()

    def _write(self, native_call, js):
        """Run a write natively (returns (ok, err)) or via mongosh, normalized
        to the (code, out, err) shape the callers already handle."""
        if self._native():
            ok, err = native_call()
            return (0 if ok else 1), "", (err or "")
        return self._eval(js, "admin")

    def test_connection(self):
        with console.status("Testing DocumentDB connection..."):
            if self._native():
                whoami, err = mn.ping(self.config, self.admin_user, self.admin_pass)
                ok = err is None
            else:
                code, out, err2 = self._eval("db.runCommand({ping:1})")
                ok = code == 0 and "ok" in out.lower()
                err = err2 or out
        if ok:
            console.print("[success]  Connected successfully[/]")
            return True
        console.print(f"[danger]  Connection failed:[/] {err}")
        return False

    def list_users(self):
        if self._native():
            data, err = mn.users_info(self.config, self.admin_user, self.admin_pass)
        else:
            data, err = self._eval_json("db.adminCommand({usersInfo:1}).users", "admin")
        if err:
            console.print(f"[danger]Error:[/] {err}")
            return []
        users = []
        for u in (data or []):
            name = u.get("user", "?")
            roles = u.get("roles", [])
            role_strs = [f"{r['role']}@{r.get('db', '*')}" for r in roles]
            users.append({
                "user": name,
                "roles": roles,
                "role_strs": role_strs,
                "db": u.get("db", "?"),
            })
        return users

    def show_users(self):
        users = self.list_users()
        if not users:
            console.print("[warning]  No users found (or error)[/]")
            return
        rows = []
        for u in users:
            rows.append((u["user"], u["db"], "\n".join(u["role_strs"]) or "none"))
        print_table(
            f"DocumentDB Users · {self.env}",
            ["Username", "Auth DB", "Roles"],
            rows,
        )

    def user_info(self, username):
        if self._native():
            user, err = mn.user_info(self.config, self.admin_user, self.admin_pass, username)
            if err == "User not found":
                console.print(f"[warning]  User '{username}' not found[/]")
                return None
            if err:
                console.print(f"[danger]Error:[/] {err}")
                return None
        else:
            data, err = self._eval_json(
                f'db.adminCommand({{usersInfo: {js_string(username)}}}).users', "admin"
            )
            if err:
                console.print(f"[danger]Error:[/] {err}")
                return None
            if not data:
                console.print(f"[warning]  User '{username}' not found[/]")
                return None
            user = data[0]
        roles = user.get("roles", [])
        role_lines = "\n".join(
            f"  {r['role']:20s} on {r.get('db', '*')}" for r in roles
        ) or "  (none)"

        body = (
            f"[header]Username:[/]  {user.get('user')}\n"
            f"[header]Auth DB:[/]   {user.get('db', '?')}\n"
            f"[header]User ID:[/]   {user.get('userId', 'n/a')}\n"
            f"\n[header]Roles:[/]\n{role_lines}"
        )
        print_panel(f"User: {username}", body)
        return user

    def create_user(self, username, password, roles):
        role_docs = json.dumps(roles)
        js = (
            f'db.createUser({{user: {js_string(username)}, '
            f'pwd: {js_string(password)}, '
            f'roles: {role_docs}}})'
        )
        if self.dry_run:
            console.print(f"[warning]DRY RUN: would execute:[/]\n  {js}")
            return True

        console.print(f"\n[header]Creating user:[/] {username}")
        console.print(f"[header]Roles:[/]")
        for r in roles:
            console.print(f"  • {r['role']} on {r['db']}")

        if not confirm("\n  Proceed?"):
            console.print("[muted]  Cancelled[/]")
            return False

        code, out, err = self._write(
            lambda: mn.create_user(self.config, self.admin_user, self.admin_pass, username, password, roles), js)
        if code == 0:
            console.print(f"[success]  User '{username}' created[/]")
            audit(self.env, self.engine, "CREATE USER", f"{username} roles={roles}")
            return True
        console.print(f"[danger]  Failed:[/] {err or out}")
        return False

    def update_password(self, username, new_password):
        js = f'db.updateUser({js_string(username)}, {{pwd: {js_string(new_password)}}})'
        if self.dry_run:
            console.print(f"[warning]DRY RUN: would reset password for {username}[/]")
            return True

        if not confirm(f"  Reset password for '{username}'?"):
            console.print("[muted]  Cancelled[/]")
            return False

        code, out, err = self._write(
            lambda: mn.update_password(self.config, self.admin_user, self.admin_pass, username, new_password), js)
        if code == 0:
            console.print(f"[success]  Password updated for '{username}'[/]")
            audit(self.env, self.engine, "RESET PASSWORD", username)
            return True
        console.print(f"[danger]  Failed:[/] {err or out}")
        return False

    def grant_roles(self, username, roles):
        role_docs = json.dumps(roles)
        js = f'db.grantRolesToUser({js_string(username)}, {role_docs})'
        if self.dry_run:
            console.print(f"[warning]DRY RUN: would grant: {role_docs} to {username}[/]")
            return True

        console.print(f"\n[header]Granting to {username}:[/]")
        for r in roles:
            console.print(f"  [success]+[/] {r['role']} on {r['db']}")

        if not confirm("\n  Proceed?"):
            console.print("[muted]  Cancelled[/]")
            return False

        code, out, err = self._write(
            lambda: mn.grant_roles(self.config, self.admin_user, self.admin_pass, username, roles), js)
        if code == 0:
            console.print(f"[success]  Roles granted[/]")
            audit(self.env, self.engine, "GRANT", f"{username} += {roles}")
            return True
        console.print(f"[danger]  Failed:[/] {err or out}")
        return False

    def revoke_roles(self, username, roles):
        role_docs = json.dumps(roles)
        js = f'db.revokeRolesFromUser({js_string(username)}, {role_docs})'
        if self.dry_run:
            console.print(f"[warning]DRY RUN: would revoke: {role_docs} from {username}[/]")
            return True

        console.print(f"\n[header]Revoking from {username}:[/]")
        for r in roles:
            console.print(f"  [danger]−[/] {r['role']} on {r['db']}")

        if not confirm("\n  Proceed?", default=False):
            console.print("[muted]  Cancelled[/]")
            return False

        code, out, err = self._write(
            lambda: mn.revoke_roles(self.config, self.admin_user, self.admin_pass, username, roles), js)
        if code == 0:
            console.print(f"[success]  Roles revoked[/]")
            audit(self.env, self.engine, "REVOKE", f"{username} -= {roles}")
            return True
        console.print(f"[danger]  Failed:[/] {err or out}")
        return False

    def drop_user(self, username):
        if self.dry_run:
            console.print(f"[warning]DRY RUN: would drop user {username}[/]")
            return True

        console.print(f"\n[danger]  WARNING: This will permanently delete user '{username}'[/]")
        if not confirm(f"  Type 'y' to confirm deletion", default=False):
            console.print("[muted]  Cancelled[/]")
            return False

        code, out, err = self._write(
            lambda: mn.drop_user(self.config, self.admin_user, self.admin_pass, username),
            f'db.dropUser({js_string(username)})')
        if code == 0:
            console.print(f"[success]  User '{username}' dropped[/]")
            audit(self.env, self.engine, "DROP USER", username)
            return True
        console.print(f"[danger]  Failed:[/] {err or out}")
        return False

    def list_databases(self):
        if self._native():
            data, err = mn.list_databases(self.config, self.admin_user, self.admin_pass)
        else:
            data, err = self._eval_json("db.adminCommand({listDatabases:1}).databases", "admin")
        if err:
            console.print(f"[danger]Error:[/] {err}")
            return
        rows = []
        for d in (data or []):
            rows.append((d["name"], format_size(d.get("sizeOnDisk", 0)), "yes" if not d.get("empty") else "empty"))
        print_table(
            f"Databases · {self.env}",
            ["Name", "Size", "Has Data"],
            rows,
        )

    def list_collections(self, database):
        if self._native():
            data, err = mn.collection_names(self.config, self.admin_user, self.admin_pass, database)
        else:
            data, err = self._eval_json("db.getCollectionNames()", database)
        if err:
            console.print(f"[danger]Error:[/] {err}")
            return
        if not data:
            console.print(f"[warning]  No collections in '{database}'[/]")
            return
        rows = [(c,) for c in sorted(data)]
        print_table(f"Collections in {database}", ["Collection"], rows)


# ---------------------------------------------------------------------------
# PostgreSQL Backend
# ---------------------------------------------------------------------------

class PostgreSQL:
    def __init__(self, env_name, config, admin_user, admin_pass, dry_run=False):
        self.env = env_name
        self.config = config
        self.host = config["host"]
        self.port = config["port"]
        self.default_db = config.get("default_db", "postgres")
        self.admin_user = admin_user
        self.admin_pass = admin_pass
        self.dry_run = dry_run
        self.engine = "postgresql"

    def _query(self, sql, db=None, timeout=30):
        return pg_query(self.config, self.admin_user, self.admin_pass, sql, db=db, timeout=timeout)

    def _exec(self, sql, db=None, timeout=30):
        ok, out, err = pg_exec(self.config, self.admin_user, self.admin_pass, sql, db=db, timeout=timeout)
        return (0 if ok else 1), out, err

    def test_connection(self):
        with console.status("Testing PostgreSQL connection..."):
            code, out, err = self._query("SELECT 1")
        if code == 0:
            console.print("[success]  Connected successfully[/]")
            return True
        console.print(f"[danger]  Connection failed:[/] {err or out}")
        return False

    def list_users(self):
        sql = """
            SELECT rolname, rolcanlogin, rolsuper, rolcreatedb,
                   rolcreaterole, COALESCE(rolvaliduntil::text, 'never')
            FROM pg_roles
            WHERE rolname NOT LIKE 'pg_%%'
              AND rolname NOT IN ('rdsadmin', 'rds_superuser', 'rds_replication',
                                  'rds_password', 'rdsrepladmin')
            ORDER BY rolname
        """
        code, out, err = self._query(sql)
        if code != 0:
            console.print(f"[danger]Error:[/] {err}")
            return []
        users = []
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 6:
                users.append({
                    "name": parts[0],
                    "can_login": parts[1] == "t",
                    "superuser": parts[2] == "t",
                    "createdb": parts[3] == "t",
                    "createrole": parts[4] == "t",
                    "valid_until": parts[5],
                })
        return users

    def show_users(self):
        users = self.list_users()
        if not users:
            console.print("[warning]  No users found (or error)[/]")
            return
        rows = []
        for u in users:
            status = "[success]active[/]" if u["can_login"] else "[danger]disabled[/]"
            flags = []
            if u["superuser"]: flags.append("superuser")
            if u["createdb"]: flags.append("createdb")
            if u["createrole"]: flags.append("createrole")
            expires = u["valid_until"] if u["valid_until"] != "never" else "-"
            rows.append((u["name"], status, ", ".join(flags) or "-", expires))
        print_table(
            f"PostgreSQL Users · {self.env}",
            ["Username", "Status", "Flags", "Expires"],
            rows,
        )

    def user_info(self, username):
        sql_role = f"""
            SELECT rolname, rolcanlogin, rolsuper, rolcreatedb,
                   rolcreaterole, COALESCE(rolvaliduntil::text, 'never'),
                   COALESCE(rolconnlimit::text, 'unlimited')
            FROM pg_roles WHERE rolname = {pg_literal(username)}
        """
        code, out, err = self._query(sql_role)
        if code != 0 or not out.strip():
            console.print(f"[warning]  User '{username}' not found[/]")
            return None

        parts = out.strip().split("\t")
        info = {
            "name": parts[0],
            "can_login": parts[1] == "t",
            "superuser": parts[2] == "t",
            "createdb": parts[3] == "t",
            "createrole": parts[4] == "t",
            "valid_until": parts[5],
            "conn_limit": parts[6] if len(parts) > 6 else "unlimited",
        }

        status = "active (can login)" if info["can_login"] else "disabled (no login)"
        flags = []
        if info["superuser"]: flags.append("superuser")
        if info["createdb"]: flags.append("createdb")
        if info["createrole"]: flags.append("createrole")

        body = (
            f"[header]Username:[/]    {info['name']}\n"
            f"[header]Status:[/]      {status}\n"
            f"[header]Flags:[/]       {', '.join(flags) or 'none'}\n"
            f"[header]Expires:[/]     {info['valid_until']}\n"
            f"[header]Conn limit:[/]  {info['conn_limit']}"
        )
        print_panel(f"User: {username}", body)

        sql_grants = f"""
            SELECT table_catalog, table_schema, table_name, privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee = {pg_literal(username)}
            ORDER BY table_catalog, table_schema, table_name
            LIMIT 50
        """
        code2, out2, err2 = self._query(sql_grants)
        if code2 == 0 and out2.strip():
            rows = []
            for line in out2.strip().split("\n"):
                if not line.strip():
                    continue
                p = line.split("\t")
                if len(p) >= 4:
                    rows.append((p[0], f"{p[1]}.{p[2]}", p[3]))
            if rows:
                print_table("Table Grants", ["Database", "Table", "Privilege"], rows)
        else:
            console.print("[muted]  No table-level grants found[/]")

        sql_db_grants = f"""
            SELECT datname, has_database_privilege({pg_literal(username)}, datname, 'CONNECT') as can_connect,
                   has_database_privilege({pg_literal(username)}, datname, 'CREATE') as can_create
            FROM pg_database
            WHERE datistemplate = false
              AND datname NOT IN ('rdsadmin')
            ORDER BY datname
        """
        code3, out3, err3 = self._query(sql_db_grants)
        if code3 == 0 and out3.strip():
            rows = []
            for line in out3.strip().split("\n"):
                if not line.strip():
                    continue
                p = line.split("\t")
                if len(p) >= 3:
                    privs = []
                    if p[1] == "t": privs.append("CONNECT")
                    if p[2] == "t": privs.append("CREATE")
                    rows.append((p[0], ", ".join(privs) or "-"))
            if rows:
                print_table("Database Privileges", ["Database", "Privileges"], rows)

        return info

    def create_user(self, username, password, can_login=True):
        login = "LOGIN" if can_login else "NOLOGIN"
        sql = f"CREATE USER {pg_ident(username)} WITH {login} PASSWORD {pg_literal(password)}"

        if self.dry_run:
            safe = sql.replace(password, "****")
            console.print(f"[warning]DRY RUN: would execute:[/]\n  {safe}")
            return True

        console.print(f"\n[header]Creating PostgreSQL user:[/] {username}")
        console.print(f"[header]Login:[/] {'yes' if can_login else 'no'}")

        if not confirm("\n  Proceed?"):
            console.print("[muted]  Cancelled[/]")
            return False

        code, out, err = self._exec(sql)
        if code == 0:
            console.print(f"[success]  User '{username}' created[/]")
            audit(self.env, self.engine, "CREATE USER", username)
            return True
        console.print(f"[danger]  Failed:[/] {err or out}")
        return False

    def update_password(self, username, new_password):
        sql = f"ALTER USER {pg_ident(username)} WITH PASSWORD {pg_literal(new_password)}"

        if self.dry_run:
            console.print(f"[warning]DRY RUN: would reset password for {username}[/]")
            return True

        if not confirm(f"  Reset password for '{username}'?"):
            console.print("[muted]  Cancelled[/]")
            return False

        code, out, err = self._exec(sql)
        if code == 0:
            console.print(f"[success]  Password updated for '{username}'[/]")
            audit(self.env, self.engine, "RESET PASSWORD", username)
            return True
        console.print(f"[danger]  Failed:[/] {err or out}")
        return False

    def grant(self, username, privilege, target_db, schema="public"):
        if privilege.upper() in ("CONNECT", "CREATE"):
            sql = f"GRANT {privilege.upper()} ON DATABASE {pg_ident(target_db)} TO {pg_ident(username)}"
            run_db = self.default_db
        elif privilege.upper() == "ALL PRIVILEGES":
            sql = f"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA {pg_ident(schema)} TO {pg_ident(username)}"
            run_db = target_db
        else:
            sql = f"GRANT {privilege.upper()} ON ALL TABLES IN SCHEMA {pg_ident(schema)} TO {pg_ident(username)}"
            run_db = target_db

        if self.dry_run:
            console.print(f"[warning]DRY RUN: would execute:[/]\n  {sql}")
            return True

        console.print(f"\n[header]Granting:[/] {privilege} to {username}")
        console.print(f"[header]Target:[/]   {target_db}.{schema}")
        if not confirm("\n  Proceed?"):
            console.print("[muted]  Cancelled[/]")
            return False

        code, out, err = self._exec(sql, db=run_db)
        if code == 0:
            console.print(f"[success]  Granted[/]")
            audit(self.env, self.engine, "GRANT", f"{username} += {privilege} on {target_db}.{schema}")
            return True
        console.print(f"[danger]  Failed:[/] {err or out}")
        return False

    def revoke(self, username, privilege, target_db, schema="public"):
        if privilege.upper() in ("CONNECT", "CREATE"):
            sql = f"REVOKE {privilege.upper()} ON DATABASE {pg_ident(target_db)} FROM {pg_ident(username)}"
            run_db = self.default_db
        elif privilege.upper() == "ALL PRIVILEGES":
            sql = f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {pg_ident(schema)} FROM {pg_ident(username)}"
            run_db = target_db
        else:
            sql = f"REVOKE {privilege.upper()} ON ALL TABLES IN SCHEMA {pg_ident(schema)} FROM {pg_ident(username)}"
            run_db = target_db

        if self.dry_run:
            console.print(f"[warning]DRY RUN: would execute:[/]\n  {sql}")
            return True

        console.print(f"\n[danger]Revoking:[/] {privilege} from {username}")
        console.print(f"[header]Target:[/]   {target_db}.{schema}")
        if not confirm("\n  Proceed?", default=False):
            console.print("[muted]  Cancelled[/]")
            return False

        code, out, err = self._exec(sql, db=run_db)
        if code == 0:
            console.print(f"[success]  Revoked[/]")
            audit(self.env, self.engine, "REVOKE", f"{username} -= {privilege} on {target_db}.{schema}")
            return True
        console.print(f"[danger]  Failed:[/] {err or out}")
        return False

    def drop_user(self, username):
        if self.dry_run:
            console.print(f"[warning]DRY RUN: would drop user {username}[/]")
            return True

        console.print(f"\n[danger]  WARNING: This will permanently delete user '{username}'[/]")
        if not confirm(f"  Type 'y' to confirm deletion", default=False):
            console.print("[muted]  Cancelled[/]")
            return False

        code, out, err = self._exec(f"DROP USER {pg_ident(username)}")
        if code == 0:
            console.print(f"[success]  User '{username}' dropped[/]")
            audit(self.env, self.engine, "DROP USER", username)
            return True
        console.print(f"[danger]  Failed:[/] {err or out}")
        return False

    def enable_user(self, username):
        sql = f"ALTER USER {pg_ident(username)} WITH LOGIN"
        if self.dry_run:
            console.print(f"[warning]DRY RUN: would enable login for {username}[/]")
            return True
        if not confirm(f"  Enable login for '{username}'?"):
            return False
        code, out, err = self._exec(sql)
        if code == 0:
            console.print(f"[success]  Login enabled for '{username}'[/]")
            audit(self.env, self.engine, "ENABLE USER", username)
            return True
        console.print(f"[danger]  Failed:[/] {err or out}")
        return False

    def disable_user(self, username):
        sql = f"ALTER USER {pg_ident(username)} WITH NOLOGIN"
        if self.dry_run:
            console.print(f"[warning]DRY RUN: would disable login for {username}[/]")
            return True
        if not confirm(f"  Disable login for '{username}'?", default=False):
            return False
        code, out, err = self._exec(sql)
        if code == 0:
            console.print(f"[success]  Login disabled for '{username}'[/]")
            audit(self.env, self.engine, "DISABLE USER", username)
            return True
        console.print(f"[danger]  Failed:[/] {err or out}")
        return False

    def list_databases(self):
        sql = """
            SELECT datname, pg_database_size(datname)::bigint as size
            FROM pg_database
            WHERE datistemplate = false AND datname NOT IN ('rdsadmin')
            ORDER BY datname
        """
        code, out, err = self._query(sql)
        if code != 0:
            console.print(f"[danger]Error:[/] {err}")
            return
        rows = []
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                rows.append((parts[0], format_size(int(parts[1]))))
        print_table(f"Databases · {self.env}", ["Name", "Size"], rows)

    def list_tables(self, database):
        sql = """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY table_schema, table_name
        """
        code, out, err = self._query(sql, db=database)
        if code != 0:
            console.print(f"[danger]Error:[/] {err}")
            return
        rows = []
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                rows.append((parts[0], parts[1]))
        if rows:
            print_table(f"Tables in {database}", ["Schema", "Table"], rows)
        else:
            console.print(f"[warning]  No tables in '{database}'[/]")


# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

def parse_docdb_roles_input(input_str):
    """Parse 'readWrite:appdb, dbAdmin:appdb' into [{"role": ..., "db": ...}]."""
    roles = []
    for part in input_str.split(","):
        part = part.strip()
        if ":" in part:
            role, db = part.split(":", 1)
            roles.append({"role": role.strip(), "db": db.strip()})
        else:
            console.print(f"[warning]  Skipping '{part}', expected role:database format[/]")
    return roles


def interactive_docdb_menu(backend):
    while True:
        console.print()
        console.rule("DocumentDB Operations")
        console.print("""
 [info][1][/] List users              [info][5][/] Grant roles
 [info][2][/] User info & permissions  [info][6][/] Revoke roles
 [info][3][/] Create user             [info][7][/] Drop user
 [info][4][/] Reset password
 ──────────────────────────────────────────
 [info][8][/] List databases           [info][9][/] List collections
 ──────────────────────────────────────────
 [muted][b][/] Back   [muted][q][/] Quit
""")
        choice = prompt("Choose").strip().lower()

        if choice == "1":
            backend.show_users()

        elif choice == "2":
            user = prompt("Username")
            backend.user_info(user)

        elif choice == "3":
            user = prompt("Username")
            pwd_choice = prompt("Password", choices=["generate", "manual"], default="generate")
            if pwd_choice == "generate":
                pwd = generate_password()
                console.print(f"\n[success]  Generated password:[/] {pwd}")
            else:
                pwd = prompt("Enter password", password=True)
            roles_input = prompt("Roles (role:database, ...)", default="read:admin")
            roles = parse_docdb_roles_input(roles_input)
            if roles:
                if backend.create_user(user, pwd, roles):
                    if pwd_choice == "generate":
                        console.print(f"\n[warning]  Save this password, it won't be shown again:[/]")
                        console.print(f"  {pwd}\n")

        elif choice == "4":
            user = prompt("Username")
            pwd_choice = prompt("Password", choices=["generate", "manual"], default="generate")
            if pwd_choice == "generate":
                pwd = generate_password()
            else:
                pwd = prompt("Enter new password", password=True)
            if backend.update_password(user, pwd):
                if pwd_choice == "generate":
                    console.print(f"\n[warning]  New password:[/] {pwd}\n")

        elif choice == "5":
            user = prompt("Username")
            roles_input = prompt("Roles to grant (role:database, ...)")
            roles = parse_docdb_roles_input(roles_input)
            if roles:
                backend.grant_roles(user, roles)

        elif choice == "6":
            user = prompt("Username")
            roles_input = prompt("Roles to revoke (role:database, ...)")
            roles = parse_docdb_roles_input(roles_input)
            if roles:
                backend.revoke_roles(user, roles)

        elif choice == "7":
            user = prompt("Username")
            backend.drop_user(user)

        elif choice == "8":
            backend.list_databases()

        elif choice == "9":
            db = prompt("Database name")
            backend.list_collections(db)

        elif choice in ("b", "back"):
            break
        elif choice in ("q", "quit", "exit"):
            sys.exit(0)


def interactive_pg_menu(backend):
    while True:
        console.print()
        console.rule("PostgreSQL Operations")
        console.print("""
 [info][1][/] List users              [info][6][/] Revoke privileges
 [info][2][/] User info & permissions  [info][7][/] Drop user
 [info][3][/] Create user             [info][8][/] Enable user (allow login)
 [info][4][/] Reset password           [info][9][/] Disable user (block login)
 [info][5][/] Grant privileges
 ──────────────────────────────────────────
 [info][d][/] List databases           [info][t][/] List tables
 ──────────────────────────────────────────
 [muted][b][/] Back   [muted][q][/] Quit
""")
        choice = prompt("Choose").strip().lower()

        if choice == "1":
            backend.show_users()

        elif choice == "2":
            user = prompt("Username")
            backend.user_info(user)

        elif choice == "3":
            user = prompt("Username")
            pwd_choice = prompt("Password", choices=["generate", "manual"], default="generate")
            if pwd_choice == "generate":
                pwd = generate_password()
                console.print(f"\n[success]  Generated password:[/] {pwd}")
            else:
                pwd = prompt("Enter password", password=True)
            login = confirm("Allow login?", default=True)
            if backend.create_user(user, pwd, can_login=login):
                if pwd_choice == "generate":
                    console.print(f"\n[warning]  Save this password, it won't be shown again:[/]")
                    console.print(f"  {pwd}\n")

        elif choice == "4":
            user = prompt("Username")
            pwd_choice = prompt("Password", choices=["generate", "manual"], default="generate")
            if pwd_choice == "generate":
                pwd = generate_password()
            else:
                pwd = prompt("Enter new password", password=True)
            if backend.update_password(user, pwd):
                if pwd_choice == "generate":
                    console.print(f"\n[warning]  New password:[/] {pwd}\n")

        elif choice == "5":
            user = prompt("Username")
            priv_list = ", ".join(PG_PRIVILEGES)
            console.print(f"[muted]  Available: {priv_list}[/]")
            priv = prompt("Privilege")
            db = prompt("Database")
            schema = prompt("Schema", default="public")
            backend.grant(user, priv, db, schema)

        elif choice == "6":
            user = prompt("Username")
            priv = prompt("Privilege")
            db = prompt("Database")
            schema = prompt("Schema", default="public")
            backend.revoke(user, priv, db, schema)

        elif choice == "7":
            user = prompt("Username")
            backend.drop_user(user)

        elif choice == "8":
            backend.enable_user(prompt("Username"))

        elif choice == "9":
            backend.disable_user(prompt("Username"))

        elif choice == "d":
            backend.list_databases()

        elif choice == "t":
            db = prompt("Database name")
            backend.list_tables(db)

        elif choice in ("b", "back"):
            break
        elif choice in ("q", "quit", "exit"):
            sys.exit(0)


def interactive_mode(env, admin_user, admin_pass, dry_run=False):
    while True:
        env_config = ENVIRONMENTS[env]

        engine_choices = list(env_config.keys())

        show_header(env, "-", "select engine below")
        console.print(f"\n  Available engines for [info]{env}[/]:")
        for i, e in enumerate(engine_choices, 1):
            console.print(f"    [info][{i}][/] {e}")
        console.print(f"\n  [muted][e][/] Switch environment")
        console.print(f"  [muted][q][/] Quit\n")

        choice = prompt("Choose engine").strip().lower()

        if choice in ("q", "quit", "exit"):
            break
        elif choice in ("e", "env"):
            envs = list(ENVIRONMENTS.keys())
            console.print(f"\n  Available environments:")
            for i, e in enumerate(envs, 1):
                marker = " [success](active)[/]" if e == env else ""
                console.print(f"    [info][{i}][/] {e}{marker}")
            idx = prompt("Choose environment")
            try:
                env = envs[int(idx) - 1]
            except (ValueError, IndexError):
                if idx in envs:
                    env = idx
                else:
                    console.print("[warning]  Invalid choice[/]")
            continue

        try:
            engine_idx = int(choice) - 1
            engine = engine_choices[engine_idx]
        except (ValueError, IndexError):
            if choice in engine_choices:
                engine = choice
            else:
                console.print("[warning]  Invalid choice[/]")
                continue

        config = env_config[engine]
        host = config["host"]

        show_header(env, engine, host)

        is_docdb = engine.startswith("document")
        if is_docdb:
            backend = DocumentDB(env, config, admin_user, admin_pass, dry_run)
        else:
            backend = PostgreSQL(env, config, admin_user, admin_pass, dry_run)

        with console.status(f"Testing {engine} connection..."):
            connected = backend.test_connection()

        if not connected:
            if not confirm("  Connection failed. Continue anyway?", default=False):
                continue

        if is_docdb:
            interactive_docdb_menu(backend)
        else:
            interactive_pg_menu(backend)


# ---------------------------------------------------------------------------
# Non-interactive CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="warden",
        description="Database User & Permission Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              %(prog)s docdb list-users
              %(prog)s docdb user-info alice
              %(prog)s docdb create-user alice --roles readWrite:appdb,dbAdmin:appdb
              %(prog)s docdb grant alice --roles read:analytics
              %(prog)s pg list-users
              %(prog)s pg create-user alice
              %(prog)s pg grant alice --privilege SELECT --database appdb --schema public
              %(prog)s pg --engine-key aurora list-users          # any pg-compatible engine key
              %(prog)s --env staging docdb list-databases
              %(prog)s --dry-run docdb drop-user old_account
        """),
    )
    envs = list(ENVIRONMENTS.keys())
    parser.add_argument("--env", default=(envs[0] if envs else None),
                        help="Target environment" + (f" (default: {envs[0]})" if envs else ""))
    parser.add_argument("--admin-user", help="Admin username (or WARDEN_ADMIN_USER env)")
    parser.add_argument("--admin-pass", help="Admin password (or WARDEN_ADMIN_PASS env)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without executing")
    parser.add_argument("-i", "--interactive", action="store_true", help="Force interactive mode")

    sub = parser.add_subparsers(dest="engine")

    # DocumentDB commands
    docdb = sub.add_parser("docdb", help="DocumentDB operations")
    docdb_sub = docdb.add_subparsers(dest="command")

    docdb_sub.add_parser("list-users", help="List all users")
    cmd = docdb_sub.add_parser("user-info", help="Show user details")
    cmd.add_argument("username")

    cmd = docdb_sub.add_parser("create-user", help="Create a new user")
    cmd.add_argument("username")
    cmd.add_argument("--roles", help="Comma-separated role:db pairs (e.g. readWrite:appdb,dbAdmin:appdb)")
    cmd.add_argument("--password", help="Password (omit to generate)")

    cmd = docdb_sub.add_parser("reset-password", help="Reset user password")
    cmd.add_argument("username")
    cmd.add_argument("--password", help="New password (omit to generate)")

    cmd = docdb_sub.add_parser("grant", help="Grant roles to user")
    cmd.add_argument("username")
    cmd.add_argument("--roles", required=True, help="Comma-separated role:db pairs")

    cmd = docdb_sub.add_parser("revoke", help="Revoke roles from user")
    cmd.add_argument("username")
    cmd.add_argument("--roles", required=True, help="Comma-separated role:db pairs")

    cmd = docdb_sub.add_parser("drop-user", help="Delete a user")
    cmd.add_argument("username")

    docdb_sub.add_parser("list-databases", help="List databases")
    cmd = docdb_sub.add_parser("list-collections", help="List collections in a database")
    cmd.add_argument("database")

    # PostgreSQL-family commands
    def add_pg_subcommands(parent):
        s = parent.add_subparsers(dest="command")
        s.add_parser("list-users", help="List all users/roles")
        cmd = s.add_parser("user-info", help="Show user details and grants")
        cmd.add_argument("username")
        cmd = s.add_parser("create-user", help="Create a new user")
        cmd.add_argument("username")
        cmd.add_argument("--password", help="Password (omit to generate)")
        cmd.add_argument("--no-login", action="store_true", help="Create without login privilege")
        cmd = s.add_parser("reset-password", help="Reset user password")
        cmd.add_argument("username")
        cmd.add_argument("--password", help="New password (omit to generate)")
        cmd = s.add_parser("grant", help="Grant privileges")
        cmd.add_argument("username")
        cmd.add_argument("--privilege", required=True, help="Privilege type (SELECT, INSERT, ALL PRIVILEGES, etc.)")
        cmd.add_argument("--database", required=True, help="Target database")
        cmd.add_argument("--schema", default="public", help="Target schema (default: public)")
        cmd = s.add_parser("revoke", help="Revoke privileges")
        cmd.add_argument("username")
        cmd.add_argument("--privilege", required=True)
        cmd.add_argument("--database", required=True)
        cmd.add_argument("--schema", default="public")
        cmd = s.add_parser("drop-user", help="Delete a user")
        cmd.add_argument("username")
        cmd = s.add_parser("enable-user", help="Allow user to login")
        cmd.add_argument("username")
        cmd = s.add_parser("disable-user", help="Block user from logging in")
        cmd.add_argument("username")
        s.add_parser("list-databases", help="List databases")
        cmd = s.add_parser("list-tables", help="List tables in a database")
        cmd.add_argument("database")

    pg = sub.add_parser("pg", help="PostgreSQL operations")
    pg.add_argument("--engine-key", dest="engine_key", default="postgresql",
                    help="Engine key inside the environment (default: postgresql)")
    add_pg_subcommands(pg)

    return parser


def run_cli(args, admin_user, admin_pass):
    env_config = ENVIRONMENTS[args.env]

    config_key = "documentdb" if args.engine == "docdb" else getattr(args, "engine_key", "postgresql")
    if not config_key or config_key not in env_config:
        console.print(f"[danger]No {args.engine} configured for {args.env}[/]")
        return 1

    if args.engine == "docdb":
        backend = DocumentDB(args.env, env_config[config_key], admin_user, admin_pass, args.dry_run)
    else:
        backend = PostgreSQL(args.env, env_config[config_key], admin_user, admin_pass, args.dry_run)

    cmd = args.command

    if cmd == "list-users":
        backend.show_users()

    elif cmd == "user-info":
        backend.user_info(args.username)

    elif cmd == "create-user":
        pwd = args.password or generate_password()
        if args.engine == "docdb":
            roles_str = args.roles or "read:admin"
            roles = parse_docdb_roles_input(roles_str)
            if backend.create_user(args.username, pwd, roles):
                if not args.password:
                    console.print(f"\n[warning]  Generated password:[/] {pwd}")
        else:
            login = not getattr(args, "no_login", False)
            if backend.create_user(args.username, pwd, can_login=login):
                if not args.password:
                    console.print(f"\n[warning]  Generated password:[/] {pwd}")

    elif cmd == "reset-password":
        pwd = args.password or generate_password()
        if backend.update_password(args.username, pwd):
            if not args.password:
                console.print(f"\n[warning]  New password:[/] {pwd}")

    elif cmd == "grant":
        if args.engine == "docdb":
            roles = parse_docdb_roles_input(args.roles)
            backend.grant_roles(args.username, roles)
        else:
            backend.grant(args.username, args.privilege, args.database, args.schema)

    elif cmd == "revoke":
        if args.engine == "docdb":
            roles = parse_docdb_roles_input(args.roles)
            backend.revoke_roles(args.username, roles)
        else:
            backend.revoke(args.username, args.privilege, args.database, args.schema)

    elif cmd == "drop-user":
        backend.drop_user(args.username)

    elif cmd == "enable-user":
        backend.enable_user(args.username)

    elif cmd == "disable-user":
        backend.disable_user(args.username)

    elif cmd == "list-databases":
        backend.list_databases()

    elif cmd == "list-collections":
        backend.list_collections(args.database)

    elif cmd == "list-tables":
        backend.list_tables(args.database)

    else:
        console.print("[warning]  No command specified. Use -i for interactive mode.[/]")
        return 1

    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def resolve_creds(args):
    admin_user = (
        getattr(args, "admin_user", None)
        or os.environ.get("WARDEN_ADMIN_USER")
    )
    admin_pass = (
        getattr(args, "admin_pass", None)
        or os.environ.get("WARDEN_ADMIN_PASS")
    )
    if not admin_user:
        admin_user = prompt("Admin username")
    if not admin_pass:
        admin_pass = prompt("Admin password", password=True)
    return admin_user, admin_pass


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not ENVIRONMENTS:
        console.print("[danger]  No environments configured.[/]")
        console.print("  Add one in the web UI (Settings > Environments, tick 'save on server'),")
        console.print("  or create ~/.warden_environments.json shaped {env: {engine: {host, port, ...}}}.")
        sys.exit(1)
    if args.env not in ENVIRONMENTS:
        console.print(f"[danger]  Unknown environment: {args.env}[/]")
        console.print(f"  Available: {', '.join(sorted(ENVIRONMENTS))}")
        sys.exit(1)

    # Check for required tools
    if args.engine == "docdb" or (not args.engine and not getattr(args, "interactive", False)):
        if not check_tool("mongosh"):
            if check_tool("mongo"):
                console.print("[warning]  'mongosh' not found but 'mongo' is available.")
                console.print("  Some features may not work. Install mongosh for full support.[/]")
            elif args.engine == "docdb":
                console.print("[danger]  'mongosh' is required for DocumentDB operations.[/]")
                console.print("  Install: brew install mongosh  (or)  npm install -g mongosh")
                sys.exit(1)

    if args.engine == "pg":
        if not check_tool("psql"):
            console.print("[danger]  'psql' is required for PostgreSQL operations.[/]")
            console.print("  Install: brew install libpq  (or)  brew install postgresql")
            sys.exit(1)

    admin_user, admin_pass = resolve_creds(args)

    if not args.engine or args.interactive:
        interactive_mode(args.env, admin_user, admin_pass, args.dry_run)
    else:
        code = run_cli(args, admin_user, admin_pass)
        sys.exit(code)


if __name__ == "__main__":
    main()
