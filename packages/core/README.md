# core (`warden_core`)

The muscle. Everything that actually talks to a database, or has to be careful about it, lives here. No HTTP, no UI, no opinions about how you call it.

Code is in [`src/warden_core/`](src/warden_core/). The cast:

- **`adapters/`** the good stuff, one class per engine. Start [there](src/warden_core/adapters/) if you're adding or fixing an engine.
- **drivers** `pg_native` / `mongo_native` / `es_native` / `redis_native` are the pooled native clients (psycopg, pymongo, urllib3, redis-py). `pg` / `mysql` / `docdb` / `sqlitedb` shell out to the CLI tools as a fallback and for the query console.
- **`validation.py`** the bouncer. Identifier checks, privilege whitelists, MySQL account quoting, the MariaDB-vs-MySQL sniff test. If it stops a bad string reaching a database, it's here.
- **`util.py`** quoting + `engine_family()`, the loose matcher that maps `aurora-postgresql`, `mariadb`, `elasticache` and friends onto the six real families.
- **`config.py`** environments + engine catalog. **`audit.py`** the append-only logbook.

**Golden rule:** user input never gets f-stringed into a query without passing through `validation` / `util` first. Values go as parameters, identifiers get quoted. Break this and warden stops being warden.
