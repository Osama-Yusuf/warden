# warden in a box. Runs the web UI so you can manage your databases from a
# browser with no Python install. The native desktop window can't live in a
# container, so this is the web face of warden.
#
# It ships the database client tools (psql, mysql, mongosh, sqlite3) so the
# query console works for every engine, not just the Python-driver features.
#
#   docker build -t warden .
#   docker run --rm -p 8642:8642 warden      # then open http://localhost:8642
#
# Pinned to bookworm on purpose: trixie's apt rejects MongoDB's SHA1-signed
# repo, so mongosh won't install there.
FROM python:3.12-slim-bookworm

# Client tools the query console + subprocess fallbacks shell out to.
# Elasticsearch and Redis go through pure-Python drivers, so no CLI for those.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        postgresql-client default-mysql-client sqlite3 \
        curl gnupg ca-certificates; \
    curl -fsSL https://pgp.mongodb.com/server-7.0.asc \
        | gpg --dearmor -o /usr/share/keyrings/mongodb.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/mongodb.gpg] http://repo.mongodb.org/apt/debian bookworm/mongodb-org/7.0 main" \
        > /etc/apt/sources.list.d/mongodb.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends mongodb-mongosh; \
    rm -rf /var/lib/apt/lists/*

# uv: fast, reproducible install straight from the lockfile.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev

# 0.0.0.0 so the port is reachable from outside the container. warden drops its
# same-origin check on a wildcard bind, which is the operator saying "yes, I'm
# exposing this on purpose."
ENV WARDEN_WEB_HOST=0.0.0.0 \
    WARDEN_WEB_PORT=8642 \
    PATH="/app/.venv/bin:$PATH"
EXPOSE 8642

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD curl -fsS http://localhost:8642/api/config || exit 1

CMD ["warden-web"]
