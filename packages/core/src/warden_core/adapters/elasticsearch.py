"""Elasticsearch, and OpenSearch alongside it.

Both speak the same REST surface, so one native driver (es_native, aliased esn)
covers them. An index is a "collection", a document is a "row": browsing pages
documents, and the row CRUD edits documents as JSON keyed by _id. User and role
management routes to X-Pack or the OpenSearch security plugin inside the driver.
"""

from warden_core import es_native as esn
from warden_core.util import validate_ident
from warden_core.validation import es_index

from .base import EngineAdapter, EngineError, Mutation, NotSupported, register


@register
class ElasticsearchAdapter(EngineAdapter):
    family = "elasticsearch"
    collection_key = "collections"
    has_login_toggle = False
    editable_rows = True

    # ── discovery / browse ──────────────────────────────────────────────────
    def ping(self):
        return self._unwrap(esn.ping(self.cfg, self.user, self.pwd))

    def list_databases(self):
        # ES has no databases; the cluster is one logical "database" whose
        # "collections" are the indices. A failed health read is non-fatal:
        # we just report a zero size, exactly like the handler did.
        name = self._unwrap(esn.cluster_name(self.cfg, self.user, self.pwd))
        health, _herr = esn.cluster_health(self.cfg, self.user, self.pwd)
        size = (health or {}).get("size_bytes") or 0
        return [{"name": name, "size_bytes": int(size),
                 "size_mb": round(int(size) / 1048576, 1)}]

    def list_collections(self, database):
        data = self._unwrap(esn.list_indices(self.cfg, self.user, self.pwd))
        return [{"name": d["name"], "size_bytes": d.get("size_bytes"),
                 "docs": d.get("docs")} for d in (data or [])]

    def browse(self, target, limit, offset, search):
        # Same index-name check the handler ran inline on the collection field.
        index = str(target.name).strip()
        if not index or "\x00" in index or "," in index or index.startswith("_"):
            raise EngineError("Invalid index name")
        return self._unwrap(esn.search_docs(self.cfg, self.user, self.pwd,
                                             index, limit, offset, search=search))

    def object_stats(self, target):
        index = str(target.name).strip()
        return self._unwrap(esn.index_stats(self.cfg, self.user, self.pwd, index))

    def table_meta(self, target):
        # Documents are edited as JSON, keyed by _id (same shape as MongoDB).
        native = esn.available()
        return {"engine": "elasticsearch", "editable": bool(native),
                "id_field": "_id", "json_edit": True,
                "reason": None if native else "native driver unavailable"}

    def health(self):
        return self._unwrap(esn.cluster_health(self.cfg, self.user, self.pwd))

    # ── users / ACL ─────────────────────────────────────────────────────────
    def list_users(self):
        data = self._unwrap(esn.list_users(self.cfg, self.user, self.pwd))
        return [{"user": u["user"], "roles": u.get("roles", []),
                 "enabled": u.get("enabled", True), "reserved": u.get("reserved", False)}
                for u in (data or [])]

    def user_info(self, name):
        nm = str(name).strip()
        u = self._unwrap(esn.user_info(self.cfg, self.user, self.pwd, nm))
        return {"user": u["user"], "roles": u.get("roles", []),
                "enabled": u.get("enabled", True), "reserved": u.get("reserved", False),
                "engine_family": "elasticsearch"}

    def create_user(self, name, password, roles=None, **opts):
        name = validate_ident(str(name), "username")
        roles = roles or []
        ok, err = esn.create_user(self.cfg, self.user, self.pwd, name, password, roles)
        if not ok:
            raise EngineError(err)
        return Mutation("CREATE USER", f"{name} roles={roles}", {"password": password})

    def set_password(self, name, password):
        name = validate_ident(str(name), "username")
        ok, err = esn.set_password(self.cfg, self.user, self.pwd, name, password)
        if not ok:
            raise EngineError(err)
        return Mutation("RESET PASSWORD", name, {"password": password})

    def grant(self, name, roles=None, **opts):
        # X-Pack has no partial role update, so read the current set, union the
        # new roles in, and write the whole set back.
        name = validate_ident(str(name), "username")
        add = [str(x) for x in (roles or [])]
        cur = self._unwrap(esn.user_info(self.cfg, self.user, self.pwd, name))
        new_roles = sorted(set(cur.get("roles", [])) | set(add))
        ok, err = esn.set_roles(self.cfg, self.user, self.pwd, name, new_roles)
        if not ok:
            raise EngineError(err)
        return Mutation("GRANT", f"{name} += {add}")

    def revoke(self, name, roles=None, **opts):
        # Mirror of grant: subtract the named roles from the current set.
        name = validate_ident(str(name), "username")
        rem = {str(x) for x in (roles or [])}
        cur = self._unwrap(esn.user_info(self.cfg, self.user, self.pwd, name))
        new_roles = sorted(set(cur.get("roles", [])) - rem)
        ok, err = esn.set_roles(self.cfg, self.user, self.pwd, name, new_roles)
        if not ok:
            raise EngineError(err)
        return Mutation("REVOKE", f"{name} -= {sorted(rem)}")

    def drop_user(self, name):
        name = validate_ident(str(name), "username")
        ok, err = esn.delete_user(self.cfg, self.user, self.pwd, name)
        if not ok:
            raise EngineError(err)
        return Mutation("DELETE USER", name)

    # ── row CRUD (documents keyed by _id; the index comes from the body) ─────
    def insert_row(self, target, body):
        index = es_index(body)
        doc = body.get("document")
        if not isinstance(doc, dict):
            raise EngineError("Document must be a JSON object")
        doc_id = body.get("id") if isinstance(body.get("id"), str) and body.get("id") else None
        res = self._unwrap(esn.insert_document(self.cfg, self.user, self.pwd, index, doc, doc_id))
        inserted_id = res.get("inserted_id")
        return Mutation("INDEX DOC", f"{index} _id={inserted_id}", {"inserted_id": inserted_id})

    def update_row(self, target, body):
        index = es_index(body)
        rep = body.get("id")
        if not isinstance(rep, str) or not rep:
            raise EngineError("Missing document _id")
        set_fields = body.get("set") or {}
        unset_fields = body.get("unset") or []
        if not isinstance(set_fields, dict) or not isinstance(unset_fields, list):
            raise EngineError("Invalid update payload")
        res = self._unwrap(esn.update_document(self.cfg, self.user, self.pwd,
                                               index, rep, set_fields, unset_fields))
        return Mutation("UPDATE DOC", f"{index} _id={rep}",
                        {"modified": 1 if (res or {}).get("result") == "updated" else 0})

    def delete_row(self, target, body):
        index = es_index(body)
        rep = body.get("id")
        if not isinstance(rep, str) or not rep:
            raise EngineError("Missing document _id")
        res = self._unwrap(esn.delete_document(self.cfg, self.user, self.pwd, index, rep))
        return Mutation("DELETE DOC", f"{index} _id={rep}",
                        {"deleted": 1 if (res or {}).get("result") == "deleted" else 0})

    # ── test a user's own login ─────────────────────────────────────────────
    def login_probe(self, test_user, test_pass, test_db=""):
        raise NotSupported("Login testing for this engine is coming in the next pass.")
