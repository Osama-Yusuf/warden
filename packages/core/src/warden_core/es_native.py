"""Native Elasticsearch / OpenSearch access over the REST API.

ES and OpenSearch expose the same REST surface (`/`, `_cat`, `_cluster`,
`_search`, `_doc`), so one small pooled HTTP client talks to both, without the
version incompatibilities the official SDKs hit across the two products.
urllib3 gives connection pooling + TLS, so repeated calls reuse sockets.

An index maps to a "collection/table", a document to a "row". The free-form
query console still speaks the native Query DSL.
"""

import json as _json
import re as _re
import ssl as _ssl
import threading

try:
    import urllib3
    urllib3.disable_warnings()  # the opt-in insecure pool would otherwise warn on every call
    HAVE_URLLIB3 = True
except ImportError:  # pragma: no cover
    HAVE_URLLIB3 = False

_ES_FIELD = _re.compile(r"^[A-Za-z0-9_.@\-]+$")


# One pool per verification mode. TLS verifies the certificate by default;
# tls_insecure opts out (for self-signed certs or private CAs like AWS).
_pools = {}
_http_lock = threading.Lock()


def available():
    return HAVE_URLLIB3


def _pool(cfg):
    insecure = bool(cfg.get("tls_insecure"))
    key = "insecure" if insecure else "verify"
    p = _pools.get(key)
    if p is None:
        with _http_lock:
            p = _pools.get(key)
            if p is None:
                common = dict(retries=urllib3.Retry(2, redirect=False),
                              timeout=urllib3.Timeout(connect=6, read=30), maxsize=8)
                if insecure:
                    p = urllib3.PoolManager(cert_reqs="CERT_NONE", **common)
                else:
                    # default context: system trust store + hostname verification
                    p = urllib3.PoolManager(ssl_context=_ssl.create_default_context(), **common)
                _pools[key] = p
    return p


def _base(cfg):
    scheme = "https" if cfg.get("tls") else "http"
    return f"{scheme}://{cfg['host']}:{int(cfg['port'])}"


def _headers(user, pwd):
    h = {"Content-Type": "application/json"}
    if user:
        h.update(urllib3.make_headers(basic_auth=f"{user}:{pwd}"))
    return h


def _req(cfg, user, pwd, method, path, body=None):
    """One REST call. Returns (data, error) with data parsed from JSON."""
    try:
        r = _pool(cfg).request(
            method, _base(cfg) + path, headers=_headers(user, pwd),
            body=_json.dumps(body).encode() if body is not None else None)
        text = r.data.decode("utf-8", "replace")
        data = _json.loads(text) if text[:1] in ("{", "[") else text
        if r.status >= 400:
            err = data.get("error", data) if isinstance(data, dict) else data
            msg = err if isinstance(err, str) else _json.dumps(err)[:400]
            return None, f"HTTP {r.status}: {msg}"
        return data, None
    except Exception as e:
        return None, str(e)


def ping(cfg, user, pwd):
    data, err = _req(cfg, user, pwd, "GET", "/")
    if err:
        return None, err
    d = data or {}
    name = d.get("cluster_name") or d.get("name") or cfg["host"]
    ver = (d.get("version") or {}).get("number", "")
    return f"{name} (v{ver})" if ver else str(name), None


def cluster_name(cfg, user, pwd):
    data, err = _req(cfg, user, pwd, "GET", "/")
    if err:
        return None, err
    return (data or {}).get("cluster_name") or cfg["host"], None


def list_indices(cfg, user, pwd):
    """[{name, size_bytes, docs}]. Includes system (dot) indices too."""
    data, err = _req(cfg, user, pwd, "GET",
                     "/_cat/indices?format=json&bytes=b&h=index,docs.count,store.size")
    if err:
        return None, err
    out = []
    for row in (data or []):
        out.append({
            "name": row.get("index"),
            "size_bytes": int(row.get("store.size") or 0),
            "docs": int(row.get("docs.count") or 0),
        })
    return out, None


def search_docs(cfg, user, pwd, index, limit=50, offset=0, search=None):
    """A page of documents shaped for the grid: columns = _id + union of
    top-level _source keys, rows aligned, ids parallel, total = hit count."""
    term = (search or "").strip()
    body = {"from": max(0, int(offset)), "size": max(1, int(limit)), "track_total_hits": True}
    body["query"] = {"query_string": {"query": term}} if term else {"match_all": {}}
    data, err = _req(cfg, user, pwd, "POST", f"/{index}/_search", body)
    if err:
        return None, err
    hits = (data or {}).get("hits") or {}
    docs = hits.get("hits") or []
    total = hits.get("total")
    total_n = total.get("value") if isinstance(total, dict) else total
    columns, seen = ["_id"], {"_id"}
    for d in docs:
        for k in (d.get("_source") or {}).keys():
            if k not in seen:
                seen.add(k)
                columns.append(k)
    rows, ids = [], []
    for d in docs:
        src = d.get("_source") or {}
        rows.append([d.get("_id") if c == "_id" else src.get(c) for c in columns])
        ids.append(d.get("_id"))
    return {"columns": columns, "rows": rows, "ids": ids,
            "total": total_n, "filtered": bool(term)}, None


def index_stats(cfg, user, pwd, index):
    data, err = _req(cfg, user, pwd, "GET",
                     f"/{index}/_stats/docs,store,segments")
    if err:
        return None, err
    indices = (data or {}).get("indices") or {}
    for _name, st in indices.items():
        total = st.get("total") or {}
        return {"rows": (total.get("docs") or {}).get("count"),
                "size_bytes": (total.get("store") or {}).get("size_in_bytes"),
                "segments": (total.get("segments") or {}).get("count")}, None
    return {}, None


def cluster_health(cfg, user, pwd):
    h, err = _req(cfg, user, pwd, "GET", "/_cluster/health")
    if err:
        return None, err
    out = {"status": h.get("status"), "cluster_name": h.get("cluster_name"),
           "nodes": h.get("number_of_nodes"), "data_nodes": h.get("number_of_data_nodes"),
           "active_shards": h.get("active_shards"),
           "relocating_shards": h.get("relocating_shards"),
           "unassigned_shards": h.get("unassigned_shards")}
    s, serr = _req(cfg, user, pwd, "GET", "/_cluster/stats")
    if not serr and isinstance(s, dict):
        idx = s.get("indices") or {}
        nodes = s.get("nodes") or {}
        out["indices"] = (idx.get("count"))
        out["docs"] = ((idx.get("docs") or {}).get("count"))
        out["size_bytes"] = ((idx.get("store") or {}).get("size_in_bytes"))
        out["heap_used_bytes"] = (((nodes.get("jvm") or {}).get("mem") or {}).get("heap_used_in_bytes"))
    return out, None


def raw_search(cfg, user, pwd, index, body):
    """Query console: run a Query DSL body via _search against index (or _all)."""
    return _req(cfg, user, pwd, "POST", f"/{index or '_all'}/_search", body)


# ---------------------------------------------------------------------------
# Document CRUD (writes refresh so the browser reflects them immediately)
# ---------------------------------------------------------------------------

def insert_document(cfg, user, pwd, index, doc, doc_id=None):
    if doc_id:
        method, path = "PUT", f"/{index}/_doc/{doc_id}?refresh=true"
    else:
        method, path = "POST", f"/{index}/_doc?refresh=true"
    data, err = _req(cfg, user, pwd, method, path, doc)
    if err:
        return None, err
    return {"inserted_id": (data or {}).get("_id")}, None


def update_document(cfg, user, pwd, index, doc_id, set_fields, unset_fields):
    """Partial update by _id via a painless script. Field names are validated to
    a safe charset (they're interpolated into the script); values ride in params
    so they can't inject."""
    for k in list((set_fields or {}).keys()) + list(unset_fields or []):
        if not _ES_FIELD.match(str(k)):
            return None, f"Field name can't be edited from the grid: {k}"
    parts, params = [], {}
    for k, v in (set_fields or {}).items():
        parts.append(f"ctx._source['{k}'] = params['{k}']")
        params[k] = v
    for k in (unset_fields or []):
        parts.append(f"ctx._source.remove('{k}')")
    if not parts:
        return {"result": "noop"}, None
    body = {"script": {"source": "; ".join(parts), "lang": "painless", "params": params}}
    data, err = _req(cfg, user, pwd, "POST", f"/{index}/_update/{doc_id}?refresh=true", body)
    if err:
        return None, err
    return {"result": (data or {}).get("result")}, None


def delete_document(cfg, user, pwd, index, doc_id):
    data, err = _req(cfg, user, pwd, "DELETE", f"/{index}/_doc/{doc_id}?refresh=true")
    if err:
        return None, err
    return {"result": (data or {}).get("result")}, None


# ---------------------------------------------------------------------------
# User & role management. Elasticsearch uses the X-Pack _security API;
# OpenSearch uses the security-plugin _plugins/_security API (different shapes),
# so we detect the distribution and route accordingly.
# ---------------------------------------------------------------------------

def _is_opensearch(cfg, user, pwd):
    data, _ = _req(cfg, user, pwd, "GET", "/")
    return ((data or {}).get("version") or {}).get("distribution", "").lower() == "opensearch"


def list_users(cfg, user, pwd):
    if _is_opensearch(cfg, user, pwd):
        data, err = _req(cfg, user, pwd, "GET", "/_plugins/_security/api/internalusers")
        if err:
            return None, err
        return [{"user": n, "roles": (i.get("opendistro_security_roles") or []) + (i.get("backend_roles") or []),
                 "enabled": True, "reserved": bool(i.get("reserved"))}
                for n, i in (data or {}).items()], None
    data, err = _req(cfg, user, pwd, "GET", "/_security/user")
    if err:
        return None, err
    out = []
    for name, info in (data or {}).items():
        out.append({"user": name, "roles": info.get("roles", []),
                    "enabled": info.get("enabled", True),
                    "full_name": info.get("full_name"), "email": info.get("email"),
                    "reserved": bool((info.get("metadata") or {}).get("_reserved"))})
    return out, None


def user_info(cfg, user, pwd, name):
    if _is_opensearch(cfg, user, pwd):
        data, err = _req(cfg, user, pwd, "GET", f"/_plugins/_security/api/internalusers/{name}")
        if err:
            return None, ("User not found" if "404" in err else err)
        info = (data or {}).get(name) or {}
        return {"user": name, "roles": (info.get("opendistro_security_roles") or []) + (info.get("backend_roles") or []),
                "enabled": True, "reserved": bool(info.get("reserved"))}, None
    data, err = _req(cfg, user, pwd, "GET", f"/_security/user/{name}")
    if err:
        return None, ("User not found" if "404" in err else err)
    info = (data or {}).get(name)
    if not info:
        return None, "User not found"
    return {"user": name, "roles": info.get("roles", []), "enabled": info.get("enabled", True),
            "full_name": info.get("full_name"), "email": info.get("email"),
            "reserved": bool((info.get("metadata") or {}).get("_reserved"))}, None


def list_roles(cfg, user, pwd):
    if _is_opensearch(cfg, user, pwd):
        data, err = _req(cfg, user, pwd, "GET", "/_plugins/_security/api/roles")
        return (sorted((data or {}).keys()) if not err else None), err
    data, err = _req(cfg, user, pwd, "GET", "/_security/role")
    return (sorted((data or {}).keys()) if not err else None), err


def create_index(cfg, user, pwd, index):
    """Create an empty index. Returns (ok, error)."""
    _d, err = _req(cfg, user, pwd, "PUT", f"/{index}")
    return (bool(not err), err)


def create_user(cfg, user, pwd, name, password, roles):
    if _is_opensearch(cfg, user, pwd):
        body = {"password": password, "backend_roles": roles or []}
        data, err = _req(cfg, user, pwd, "PUT", f"/_plugins/_security/api/internalusers/{name}", body)
        return (bool(not err), err)
    body = {"password": password, "roles": roles or []}
    data, err = _req(cfg, user, pwd, "POST", f"/_security/user/{name}", body)
    return (bool(not err), err)


def set_password(cfg, user, pwd, name, password):
    if _is_opensearch(cfg, user, pwd):
        body = [{"op": "replace", "path": "/password", "value": password}]
        _d, err = _req(cfg, user, pwd, "PATCH", f"/_plugins/_security/api/internalusers/{name}", body)
        return (bool(not err), err)
    _d, err = _req(cfg, user, pwd, "POST", f"/_security/user/{name}/_password", {"password": password})
    return (bool(not err), err)


def set_roles(cfg, user, pwd, name, roles):
    """Replace a user's role set (used to grant/revoke by computing the new set)."""
    if _is_opensearch(cfg, user, pwd):
        body = [{"op": "replace", "path": "/backend_roles", "value": roles}]
        _d, err = _req(cfg, user, pwd, "PATCH", f"/_plugins/_security/api/internalusers/{name}", body)
        return (bool(not err), err)
    # X-Pack has no partial user update; re-PUT the user preserving other fields.
    cur, err = user_info(cfg, user, pwd, name)
    if err:
        return False, err
    body = {"roles": roles}
    if cur.get("full_name"):
        body["full_name"] = cur["full_name"]
    if cur.get("email"):
        body["email"] = cur["email"]
    _d, err = _req(cfg, user, pwd, "PUT", f"/_security/user/{name}", body)
    return (bool(not err), err)


def delete_user(cfg, user, pwd, name):
    if _is_opensearch(cfg, user, pwd):
        _d, err = _req(cfg, user, pwd, "DELETE", f"/_plugins/_security/api/internalusers/{name}")
        return (bool(not err), err)
    _d, err = _req(cfg, user, pwd, "DELETE", f"/_security/user/{name}")
    return (bool(not err), err)
