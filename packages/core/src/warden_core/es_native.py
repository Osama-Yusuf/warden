"""Native Elasticsearch / OpenSearch access over the REST API.

ES and OpenSearch expose the same REST surface (`/`, `_cat`, `_cluster`,
`_search`, `_doc`), so one small pooled HTTP client talks to both — without the
version incompatibilities the official SDKs hit across the two products.
urllib3 gives connection pooling + TLS, so repeated calls reuse sockets.

An index maps to a "collection/table", a document to a "row". The free-form
query console still speaks the native Query DSL.
"""

import json as _json
import threading

try:
    import urllib3
    urllib3.disable_warnings()
    HAVE_URLLIB3 = True
except ImportError:  # pragma: no cover
    HAVE_URLLIB3 = False


_http = None
_http_lock = threading.Lock()


def available():
    return HAVE_URLLIB3


def _pool():
    global _http
    if _http is None:
        with _http_lock:
            if _http is None:
                _http = urllib3.PoolManager(
                    cert_reqs="CERT_NONE",  # admin tool: tolerate self-signed like the mongo path
                    retries=urllib3.Retry(2, redirect=False),
                    timeout=urllib3.Timeout(connect=6, read=30),
                    maxsize=8,
                )
    return _http


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
        r = _pool().request(
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
    """[{name, size_bytes, docs}] — includes system (dot) indices too."""
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
