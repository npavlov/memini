# The Memini service image. Two stages:
#
# - build: uv installs the locked dependencies, then the project, into a virtualenv (no dev group,
#   bytecode compiled). Dependencies come first, so a change to src/ reuses their layer.
# - runtime: the same Python base with that virtualenv only, plus git and ssh from Debian for the
#   git connector; no uv, no compiler, no source tree. It runs as a non-root user (UID/GID 10001)
#   and holds no secret: the database URL comes from the environment (MEMINI_DATABASE_URL) or a
#   mounted file (MEMINI_DATABASE_URL_FILE) at run time.
#
#   docker build -t memini:dev .          # or: docker compose build
#
# Base images are pinned by version and digest; update both together. The Python version is the
# one in .python-version (uv refuses to sync with another), uv the one CI pins.

ARG PYTHON_IMAGE=python:3.12.16-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build

COPY --from=uv /uv /usr/local/bin/uv

# Compile bytecode at install, so the read-only runtime never writes .pyc files. Copy from the
# cache mount instead of hard-linking across filesystems. Use the image's Python, never a download.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/memini/venv

WORKDIR /src

# Dependencies only, from uv.lock: this layer changes when the lock does.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=.python-version,target=.python-version \
    uv sync --locked --no-dev --no-install-project

# The project itself, as a regular (non-editable) install: the runtime needs no source tree.
COPY pyproject.toml uv.lock .python-version README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="Memini" \
      org.opencontainers.image.description="Self-hosted knowledge service: code, messages and meeting notes in; MCP and HTTP out." \
      org.opencontainers.image.source="https://github.com/npavlov/memini" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN groupadd --system --gid 10001 memini \
 && useradd --system --uid 10001 --gid memini --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin memini

# git runs the git connector: the service keeps a bare mirror of every repository source and
# reads code from it (memini.git); openssh-client is its ssh transport. Both come from the Debian
# release of the base image, which the digest pins; no recommended packages, no apt lists kept.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client \
 && rm -rf /var/lib/apt/lists/*

# The mirrors live on a volume mounted here (compose.yaml), writable while the root filesystem is
# read-only. The directory belongs to the service user with mode 0700, and a new named volume
# takes its owner and mode from it.
RUN install -d -o 10001 -g 10001 -m 0700 /var/lib/memini/mirrors

# Owned by root and not writable by the service user: the service cannot change its own code.
COPY --from=build /opt/memini/venv /opt/memini/venv

# Inside a container the service listens on every interface of the container's own network: on
# 127.0.0.1 nothing outside the container could reach it. Who can reach the container is decided
# where its port is published; compose.yaml publishes it on the host's 127.0.0.1 only.
ENV PATH=/opt/memini/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MEMINI_HTTP_HOST=0.0.0.0 \
    MEMINI_HTTP_PORT=6364 \
    MEMINI_GIT_MIRROR_DIR=/var/lib/memini/mirrors

USER 10001:10001
WORKDIR /opt/memini
EXPOSE 6364

# Healthy means ready to serve: /readyz answers 200 only when the database is reachable too, so
# `docker compose up --wait` returns when MCP calls can succeed. /healthz (liveness) would say
# healthy while every request fails. Plain Docker does not restart an unhealthy container, so a
# database outage does not cause a restart loop. Python, not curl: the slim image has no curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=1s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + (os.environ.get('MEMINI_HTTP_PORT') or '6364') + '/readyz', timeout=4)"]

ENTRYPOINT ["memini"]
CMD ["serve"]
