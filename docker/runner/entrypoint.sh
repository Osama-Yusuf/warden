#!/usr/bin/env bash
# Register (once), run, and deregister only on a real stop.
#
# The token this receives is a *registration* token: repo-scoped, minted on
# demand, valid for about an hour. It arrives as an environment variable and is
# never written to disk here, so nothing durable in this image or repository is
# a credential.
set -euo pipefail

: "${RUNNER_TOKEN:?RUNNER_TOKEN is required (scripts/runner-up mints one)}"
: "${REPO_URL:?REPO_URL is required}"
RUNNER_NAME="${RUNNER_NAME:-warden-docker}"
RUNNER_LABELS="${RUNNER_LABELS:-self-hosted,linux,docker}"

# Deregister on an explicit stop only (SIGTERM from `docker stop`, SIGINT from a
# foreground Ctrl-C). Deliberately NOT an EXIT trap: with `--restart
# unless-stopped`, an EXIT trap deregisters on any exit, Docker restarts the
# container, and config.sh then refuses "already configured" in a crash loop.
cleanup() {
  echo "-> deregistering ${RUNNER_NAME}"
  ./config.sh remove --token "${RUNNER_TOKEN}" >/dev/null 2>&1 || true
  exit 0
}
trap cleanup INT TERM

# Make the mounted Docker socket usable without root. Its group id comes from the
# host and isn't knowable at build time, so match it at start-up and join it.
DOCKER_GROUP=""
if [ -S /var/run/docker.sock ]; then
  sock_gid="$(stat -c '%g' /var/run/docker.sock)"
  if ! getent group "$sock_gid" >/dev/null; then
    sudo groupadd -g "$sock_gid" dockerhost
  fi
  DOCKER_GROUP="$(getent group "$sock_gid" | cut -d: -f1)"
  sudo usermod -aG "$DOCKER_GROUP" runner
  echo "-> docker socket available (group ${DOCKER_GROUP})"
fi

# Configure only when not already configured, so a restart reuses the stored
# credentials instead of needing a fresh registration token it can't get.
if [ ! -f .runner ]; then
  ./config.sh \
    --unattended \
    --replace \
    --url "${REPO_URL}" \
    --token "${RUNNER_TOKEN}" \
    --name "${RUNNER_NAME}" \
    --labels "${RUNNER_LABELS}" \
    --work "${RUNNER_WORK_DIR:-_work}"
else
  echo "-> already configured, reusing registration"
fi

echo "-> ${RUNNER_NAME} listening for jobs [${RUNNER_LABELS}], nice ${RUNNER_NICE:-0}"
# `sg` rather than a re-exec: group membership is only read at process start, so
# run.sh has to launch already holding the docker group.
if [ -n "$DOCKER_GROUP" ]; then
  exec sg "$DOCKER_GROUP" -c "nice -n ${RUNNER_NICE:-0} ./run.sh"
fi
exec nice -n "${RUNNER_NICE:-0}" ./run.sh
