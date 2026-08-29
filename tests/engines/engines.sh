#!/usr/bin/env bash
# Spin the test databases up or down, by friendly name.
#
#   ./engines.sh up                 # all of them, then wait until they're ready
#   ./engines.sh up pg mongo        # just those two
#   ./engines.sh down               # stop + remove everything
#   ./engines.sh status             # what's running
#
# Names accepted: pg/postgres, mysql/mariadb, mongo, es/elasticsearch, redis.
# The creds are baked into docker-compose.yml and match what the tests default
# to, so once these are up, `uv run pytest -m integration` just works.
set -euo pipefail
cd "$(dirname "$0")"

# friendly name -> compose service name
resolve() {
  case "$1" in
    pg|postgres|postgresql) echo postgres ;;
    mysql|mariadb|maria)    echo mysql ;;
    mongo|mongodb)          echo mongo ;;
    es|elastic|elasticsearch) echo elasticsearch ;;
    redis)                  echo redis ;;
    *) echo "unknown engine: $1" >&2; exit 2 ;;
  esac
}

cmd="${1:-help}"; shift || true

case "$cmd" in
  up)
    services=(); for n in "$@"; do services+=("$(resolve "$n")"); done
    echo "starting ${services[*]:-all engines}..."
    # ${arr[@]+"${arr[@]}"} so an empty array expands to nothing instead of
    # tripping "unbound variable" under set -u on macOS's stock bash 3.2.
    docker compose up -d --wait ${services[@]+"${services[@]}"}    # --wait blocks on the healthchecks
    echo "ready. point the tests at them: uv run pytest -m integration"
    ;;
  down)
    docker compose down
    ;;
  status|ps)
    docker compose ps
    ;;
  *)
    grep '^#' "$0" | grep -v '^#!' | sed 's/^# \{0,1\}//'   # header block as help, minus the shebang
    ;;
esac
