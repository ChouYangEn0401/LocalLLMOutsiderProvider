# syntax=docker/dockerfile:1
#
# llmop container image. Build from the REPOSITORY ROOT (the compose files in this
# folder do it for you):
#
#   docker build -f docker/Dockerfile -t llmop:local .
#
# Runtime layout:
#   /config/config.yaml   your config, mounted read-only (see docker/README.md)
#   /data                 SQLite database (API keys, sessions, usage) - a volume
# The container always listens on port 8780 (publish it with "ports:" in compose).

# ---- stage 1: build the wheel ------------------------------------------------
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --disable-pip-version-check --upgrade pip \
 && pip wheel --no-cache-dir --disable-pip-version-check --no-deps --wheel-dir /dist .

# ---- stage 2: runtime --------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LLMOP_IN_CONTAINER=1 \
    LLMOP_CONFIG=/config/config.yaml \
    LLMOP_DATA_DIR=/data \
    TZ=UTC

# tzdata: daily / monthly quotas reset at midnight of TZ (set TZ in compose).
# The service runs as an unprivileged user (uid 10001), never as root.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 llmop \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /data \
            --shell /usr/sbin/nologin llmop \
 && mkdir -p /data /config \
 && chown 10001:10001 /data

COPY --from=build /dist/*.whl /tmp/
RUN for w in /tmp/*.whl; do pip install "${w}[server]"; done && rm -f /tmp/*.whl

USER 10001:10001
WORKDIR /data
EXPOSE 8780
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8780/health', timeout=4).status == 200 else 1)"]

# "docker compose run --rm llmop <command>" runs other llmop commands, e.g. hash-password.
ENTRYPOINT ["llmop", "--config", "/config/config.yaml"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8780"]
