# A GitHub Actions runner that behaves like `ubuntu-latest`, on your own machine.
#
# Plain Ubuntu, not the Playwright base gravity uses: warden has no browser
# tests, so the ~2 GB of preinstalled browsers would sit unused. What warden
# actually needs is Python (via uv), Node (via setup-node), the `mysql` client
# it shells out to, and a Docker CLI so the integration job's service containers
# can be driven through the host's daemon.
FROM ubuntu:24.04

ARG RUNNER_VERSION=2.328.0
ARG RUNNER_ARCH=arm64
ARG DOCKER_CLI_VERSION=27.5.1

# git/curl/jq/sudo/unzip are the runner's own needs; mariadb-client is warden's
# (the MySQL adapter shells out to `mysql`); the rest are what the actions
# runner links against on a bare image. ca-certificates so HTTPS works at all.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl ca-certificates git jq sudo unzip python3 \
      mariadb-client \
      libicu74 liblttng-ust1 libkrb5-3 zlib1g libssl3 \
 && rm -rf /var/lib/apt/lists/*

# The Docker CLI only, talking to the host daemon over the socket scripts/runner-up
# mounts. Static binary, so nothing here tries to start a second daemon.
RUN case "${RUNNER_ARCH}" in \
      arm64) DOCKER_ARCH=aarch64 ;; \
      *)     DOCKER_ARCH=x86_64  ;; \
    esac \
 && curl -fsSL "https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-${DOCKER_CLI_VERSION}.tgz" \
    | tar xz -C /usr/local/bin --strip-components=1 docker/docker \
 && docker --version

# The runner refuses to run as root, and the integration job apt-installs at job
# time, so: an unprivileged user with passwordless sudo, same shape as hosted.
RUN useradd -m -s /bin/bash runner \
 && echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner \
 && chmod 0440 /etc/sudoers.d/runner

USER runner
WORKDIR /home/runner

RUN curl -fsSL \
      "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz" \
    | tar xz

COPY --chown=runner:runner entrypoint.sh /home/runner/entrypoint.sh
RUN chmod +x /home/runner/entrypoint.sh

ENTRYPOINT ["/home/runner/entrypoint.sh"]
