"""DocumentDB and MongoDB.

Structured reads, writes and row edits go through the pooled native driver
(mongo_native, pymongo under the hood). When pymongo isn't installed we fall
back to the mongosh subprocess (docdb_eval / docdb_exec), exactly as the old API
handlers did. The free-form query console still lives in the handler, so there
is no run_query here.
"""

import json

from warden_core import mongo_native as mn
from warden_core import validation
from warden_core.docdb import docdb_eval, docdb_exec
from warden_core.util import js_string, validate_ident

from .base import EngineAdapter, EngineError, Mutation, register


def _clean_mongosh_noise(text):
    """Drop mongosh's node-module warnings (for example baseline-browser-mapping)
    so they don't leak into a check detail or an error string."""
    return "\n".join(
        line for line in (text or "").splitlines()
        if "[baseline-browser-mapping]" not in line
    ).strip()


@register
class MongoAdapter(EngineAdapter):
    family = "documentdb"
    collection_key = "collections"
    has_login_toggle = False
    editable_rows = True

    def _ns(self, target, require_collection=False):
        """Validate and return (database, collection) for a browse/stats/CRUD call.
        The database goes through the same ident check the handlers used to run;
        the collection is only length-checked where the browser needs it."""
        database = validate_ident(target.database, "database")
        collection = str(target.name).strip()
        if require_collection and (not collection or len(collection) > 128 or "\x00" in collection):
            raise EngineError("Invalid collection name")
        return database, collection

    # ── discovery / browse ──────────────────────────────────────────────────
    def ping(self):
        # Whoami only. Warming the mongosh session is the handler's job.
        if mn.available():
            return self._unwrap(mn.ping(self.cfg, self.user, self.pwd))
        data = self._unwrap(docdb_eval(self.cfg, self.user, self.pwd,
                                       "db.runCommand({connectionStatus:1})"))
        authed = ((data or {}).get("authInfo") or {}).get("authenticatedUsers") or []
        return authed[0].get("user") if authed else self.user

    def list_databases(self):
        if mn.available():
            data = self._unwrap(mn.list_databases(self.cfg, self.user, self.pwd))
        else:
            data = self._unwrap(docdb_eval(self.cfg, self.user, self.pwd,
                                           "db.adminCommand({listDatabases:1}).databases"))
        dbs = []
        for d in (data or []):
            size_bytes = validation.docdb_num(d.get("sizeOnDisk", 0))
            dbs.append({
                "name": d["name"],
                "size_bytes": size_bytes,
                "size_mb": round(size_bytes / (1024 * 1024), 1),
                "empty": d.get("empty", False),
            })
        return dbs

    def list_collections(self, database):
        database = validate_ident(database, "database")
        if mn.available():
            return self._unwrap(mn.list_collections(self.cfg, self.user, self.pwd, database))
        data = self._unwrap(docdb_eval(self.cfg, self.user, self.pwd,
                                       "db.getCollectionNames()", db=database))
        return [{"name": n, "size_bytes": None} for n in sorted(data or [])]

    def create_collection(self, target):
        database, collection = self._ns(target, require_collection=True)
        self._unwrap(mn.create_collection(self.cfg, self.user, self.pwd, database, collection))
        return Mutation("CREATE COLLECTION", f"{database}.{collection}", {"collection": collection})

    def browse(self, target, limit, offset, search):
        database, collection = self._ns(target, require_collection=True)
        if not mn.available():
            raise EngineError("Data browser needs the native MongoDB driver (pymongo)")
        return self._unwrap(mn.find_page(self.cfg, self.user, self.pwd,
                                         database, collection,
                                         limit, offset, search=search))

    def object_stats(self, target):
        if not mn.available():
            return {}
        database, collection = self._ns(target)
        return self._unwrap(mn.collection_stats(self.cfg, self.user, self.pwd,
                                                database, collection))

    def table_meta(self, target):
        if not mn.available():
            return {"editable": False, "reason": "native MongoDB driver unavailable"}
        database, collection = self._ns(target)
        return self._unwrap(mn.collection_meta(self.cfg, self.user, self.pwd,
                                               database, collection))

    def health(self):
        if mn.available():
            return self._unwrap(mn.server_health(self.cfg, self.user, self.pwd))
        # DocumentDB returns these counters as BSON Longs. Coerce to plain
        # numbers or JSON.stringify turns them into {low, high, unsigned} blobs.
        js = ("(() => { const n = v => (v && typeof v.toNumber === 'function') ? v.toNumber()"
              "   : (typeof v === 'number' ? v : Number(v));"
              " const s = db.serverStatus();"
              " const c = s.connections || {};"
              " const out = {version: String(s.version || ''), uptime: n(s.uptime),"
              "   connections: {current: n(c.current), available: n(c.available)},"
              "   mem: s.mem ? {resident: n(s.mem.resident)} : null};"
              " try { const cur = db.adminCommand({currentOp: 1, active: true});"
              "   const prog = cur.inprog || [];"
              "   out.active_ops = prog.length;"
              "   out.slow_ops = prog.filter(o => n(o.secs_running) >= 5).slice(0, 10)"
              "     .map(o => ({opid: String(o.opid), secs: n(o.secs_running), ns: String(o.ns || ''), op: String(o.op || '')}));"
              " } catch (e) { out.active_ops = null; out.slow_ops = []; }"
              " return out })()")
        return self._unwrap(docdb_eval(self.cfg, self.user, self.pwd, js, timeout=45))

    # ── users / ACL ─────────────────────────────────────────────────────────
    def list_users(self):
        if mn.available():
            data = self._unwrap(mn.users_info(self.cfg, self.user, self.pwd))
        else:
            data = self._unwrap(docdb_eval(self.cfg, self.user, self.pwd,
                                           "db.adminCommand({usersInfo:1}).users"))
        users = []
        for u in (data or []):
            roles = u.get("roles", [])
            users.append({
                "user": u.get("user", "?"),
                "db": u.get("db", "?"),
                "roles": [f"{r['role']}@{r.get('db', '*')}" for r in roles],
                "roles_raw": roles,
            })
        return users

    def user_info(self, name):
        if mn.available():
            u = self._unwrap(mn.user_info(self.cfg, self.user, self.pwd, name))
        else:
            data = self._unwrap(docdb_eval(self.cfg, self.user, self.pwd,
                                           f'db.adminCommand({{usersInfo: {js_string(name)}}}).users'))
            if not data:
                raise EngineError("User not found")
            u = data[0]
        return {
            "user": u.get("user"),
            "db": u.get("db", "?"),
            "userId": str(u.get("userId", "n/a")),
            "roles": u.get("roles", []),
        }

    def create_user(self, name, password, roles=None, **opts):
        roles = roles or []
        validation.validate_docdb_roles(roles)
        if mn.available():
            ok, err = mn.create_user(self.cfg, self.user, self.pwd, name, password, roles)
        else:
            role_docs = json.dumps(roles)
            js = (f'db.createUser({{user: {js_string(name)}, pwd: {js_string(password)}, '
                  f'roles: {role_docs}}})')
            ok, out, err = docdb_exec(self.cfg, self.user, self.pwd, js)
            err = err or out
        if not ok:
            raise EngineError(err)
        return Mutation("CREATE USER", f"{name} roles={roles}", {"password": password})

    def set_password(self, name, password):
        if mn.available():
            ok, err = mn.update_password(self.cfg, self.user, self.pwd, name, password)
        else:
            js = f'db.updateUser({js_string(name)}, {{pwd: {js_string(password)}}})'
            ok, out, err = docdb_exec(self.cfg, self.user, self.pwd, js)
            err = err or out
        if not ok:
            raise EngineError(err)
        return Mutation("RESET PASSWORD", name, {"password": password})

    def grant(self, name, roles=None, **opts):
        roles = roles or []
        validation.validate_docdb_roles(roles)
        if mn.available():
            ok, err = mn.grant_roles(self.cfg, self.user, self.pwd, name, roles)
        else:
            role_docs = json.dumps(roles)
            js = f'db.grantRolesToUser({js_string(name)}, {role_docs})'
            ok, out, err = docdb_exec(self.cfg, self.user, self.pwd, js)
            err = err or out
        if not ok:
            raise EngineError(err)
        return Mutation("GRANT", f"{name} += {roles}")

    def revoke(self, name, roles=None, **opts):
        roles = roles or []
        validation.validate_docdb_roles(roles)
        if mn.available():
            ok, err = mn.revoke_roles(self.cfg, self.user, self.pwd, name, roles)
        else:
            role_docs = json.dumps(roles)
            js = f'db.revokeRolesFromUser({js_string(name)}, {role_docs})'
            ok, out, err = docdb_exec(self.cfg, self.user, self.pwd, js)
            err = err or out
        if not ok:
            raise EngineError(err)
        return Mutation("REVOKE", f"{name} -= {roles}")

    def drop_user(self, name):
        if mn.available():
            ok, err = mn.drop_user(self.cfg, self.user, self.pwd, name)
        else:
            ok, out, err = docdb_exec(self.cfg, self.user, self.pwd,
                                      f'db.dropUser({js_string(name)})')
            err = err or out
        if not ok:
            raise EngineError(err)
        return Mutation("DROP USER", name)

    # ── row CRUD (documents) ────────────────────────────────────────────────
    def insert_row(self, target, body):
        database, collection = self._ns(target)
        doc = body.get("document")
        if not isinstance(doc, dict):
            raise EngineError("Document must be a JSON object")
        res = self._unwrap(mn.insert_document(self.cfg, self.user, self.pwd,
                                              database, collection, doc))
        inserted_id = res.get("inserted_id")
        return Mutation("INSERT DOC", f"{database}.{collection} _id={inserted_id}",
                        {"inserted_id": inserted_id})

    def update_row(self, target, body):
        database, collection = self._ns(target)
        rep = body.get("id")
        if rep is None:
            raise EngineError("Missing document _id")
        set_fields = body.get("set") or {}
        unset_fields = body.get("unset") or []
        if not isinstance(set_fields, dict) or not isinstance(unset_fields, list):
            raise EngineError("Invalid update payload")
        res = self._unwrap(mn.update_document(self.cfg, self.user, self.pwd,
                                              database, collection,
                                              rep, set_fields, unset_fields))
        return Mutation("UPDATE DOC", f"{database}.{collection} _id={rep}",
                        {"modified": res.get("modified", 0)})

    def delete_row(self, target, body):
        database, collection = self._ns(target)
        rep = body.get("id")
        if rep is None:
            raise EngineError("Missing document _id")
        res = self._unwrap(mn.delete_document(self.cfg, self.user, self.pwd,
                                              database, collection, rep))
        return Mutation("DELETE DOC", f"{database}.{collection} _id={rep}",
                        {"deleted": res.get("deleted", 0)})

    # ── test a user's own login ─────────────────────────────────────────────
    def login_probe(self, test_user, test_pass, test_db=""):
        if mn.available():
            r = mn.login_probe(self.cfg, test_user, test_pass, test_db)
            if not r.get("auth"):
                return {"auth": False,
                        "error": _clean_mongosh_noise(r.get("error") or "") or "Authentication failed"}
            return {"auth": True, "identity": r.get("identity", test_user),
                    "roles": r.get("roles", []), "checks": r.get("checks", [])}
        data, err = docdb_eval(self.cfg, test_user, test_pass,
                               "db.runCommand({connectionStatus:1})")
        if err or not data:
            return {"auth": False,
                    "error": _clean_mongosh_noise(err or "") or "Authentication failed"}
        roles = ((data or {}).get("authInfo") or {}).get("authenticatedUserRoles") or []
        role_strs = [f"{r.get('role')}@{r.get('db', '*')}" for r in roles]
        checks = []
        dbs, e2 = docdb_eval(self.cfg, test_user, test_pass,
                             "db.adminCommand({listDatabases:1}).databases.map(d => d.name)")
        checks.append({"name": "List all databases", "ok": e2 is None,
                       "detail": (", ".join(dbs) if e2 is None and dbs
                                  else _clean_mongosh_noise(e2 or "") or "not permitted")})
        if test_db:
            names, e3 = docdb_eval(self.cfg, test_user, test_pass,
                                   "db.getCollectionNames()", db=test_db)
            checks.append({"name": f"Read '{test_db}'", "ok": e3 is None,
                           "detail": (f"{len(names or [])} collection(s) visible" if e3 is None
                                      else _clean_mongosh_noise(e3))})
        return {"auth": True, "identity": test_user, "roles": role_strs, "checks": checks}
