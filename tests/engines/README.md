# test engines

The databases the integration + smoke tests run against, as one compose file so
you don't have to remember five `docker run` incantations. The creds here match
exactly what the tests default to, so once these are up, the tests just connect.

### Fastest path
```bash
./engines.sh up            # start all five, wait until they're actually ready
uv run pytest -m integration
./engines.sh down          # done
```

Want just a couple? `./engines.sh up pg mongo`. Names: `pg`, `mysql`, `mongo`,
`es`, `redis` (SQLite isn't here, it's just a file). `./engines.sh status` shows
what's running.

### Or plain compose
```bash
docker compose up -d                 # all
docker compose up -d postgres redis  # a subset
docker compose down
```

### What you get
| service | image | port | creds |
|---|---|---|---|
| postgres | postgres:16 | 5432 | admin / adminpass |
| mysql | mariadb:11 | 3306 | root / adminpass |
| mongo | mongo:6 | 27017 | admin / adminpass |
| elasticsearch | elasticsearch:8.13.4 | 9200 | elastic / espass |
| redis | redis:7 | 6379 | (no user) / testpass |

Yes, the "mysql" service is really MariaDB. That's on purpose: it exercises the
MySQL-vs-MariaDB branch in warden's account handling.

### Ports already taken?
Override any of them without editing the file:
```bash
WARDEN_PG_PORT=15432 docker compose up -d postgres
```
Then point the test at it: `WARDEN_TEST_PG_PORT=15432 uv run pytest -m integration`.
