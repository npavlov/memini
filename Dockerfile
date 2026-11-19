# The Memini service image. Three stages:
#
# - build: uv installs the locked dependencies, then the project, into a virtualenv (no dev group,
#   bytecode compiled), with the optional extra local-embeddings: onnxruntime, tokenizers and
#   numpy, the runtime of the reranker. Dependencies come first, so a change to src/ reuses their
#   layer.
# - reranker: the model files of the reranker (Qwen3-Reranker 0.6B as ONNX, about 610 MB), fetched
#   from Hugging Face at a pinned commit and checked against pinned SHA-256 values; the build fails
#   if they differ. Its layer depends on the pins below only, so a change to the code does not
#   fetch them again.
# - runtime: the same Python base with that virtualenv and those files only, plus git and ssh from
#   Debian for the git connector; no uv, no compiler, no source tree. It runs as a non-root user
#   (UID/GID 10001) and holds no secret: the database URL comes from the environment
#   (MEMINI_DATABASE_URL) or a mounted file (MEMINI_DATABASE_URL_FILE) at run time.
#
#   docker build -t memini:dev .          # or: docker compose build
#
# The reranker is on in the image (docs/requirements.md, section 9, Reranker): its files are part
# of the image, so the service never fetches them and works offline. RERANKER_FILES=none builds the
# image without them (CI's smoke test does, so that a pull request fetches no 610 MB): the reranker
# then says in the log that it is off, and search keeps the fused order.
#
# Base images are pinned by version and digest; update both together. The Python version is the
# one in .python-version (uv refuses to sync with another), uv the one CI pins.

ARG PYTHON_IMAGE=python:3.12.16-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d

# The reranker's files: baked (the default) or none. The model, its commit, the model file and the
# digest are the defaults of the search_reranker_* settings (src/memini/config.py); the SHA-256 of
# each file folds into that digest. tests/test_rerank.py checks that they agree: change them
# together.
ARG RERANKER_FILES=baked
ARG RERANKER_MODEL=n24q02m/Qwen3-Reranker-0.6B-ONNX
ARG RERANKER_REVISION=342a8a54de23bba03eb8cce5f5bef503edf18315
ARG RERANKER_MODEL_FILE=onnx/model_yesno_quantized.onnx
ARG RERANKER_DIGEST=sha256:eaa6edfadd2440e4f80851b3ff7701f26e8ec6aacaa2fedcee8ce86afd09afea
ARG RERANKER_MODEL_SHA256=2bc0a77812a15ff6ead003370f36149139ae2c1bb1faf779b00c00886cd919ef
ARG RERANKER_TOKENIZER_SHA256=8579e1ca7cc5d82a9e0202eed555529996f4ffe7f563c2979a0290cf3db452d3

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
    uv sync --locked --no-dev --extra local-embeddings --no-install-project

# The project itself, as a regular (non-editable) install: the runtime needs no source tree.
COPY pyproject.toml uv.lock .python-version README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra local-embeddings --no-editable


# The reranker's files where the service looks for them (MEMINI_SEARCH_RERANKER_MODEL_DIR below):
# <dir>/<org>--<name>/<the digest's first 16 hex digits>/, as memini.embeddings.model_files lays
# them out. Downloaded with the image's Python (the slim image has no curl), each request bounded
# by a timeout, then checked by sha256sum: a file that differs fails the build.
FROM ${PYTHON_IMAGE} AS reranker-baked
ARG RERANKER_MODEL RERANKER_REVISION RERANKER_MODEL_FILE RERANKER_DIGEST \
    RERANKER_MODEL_SHA256 RERANKER_TOKENIZER_SHA256
RUN set -eu; \
    files="/models/$(printf '%s' "$RERANKER_MODEL" | sed 's|/|--|')/$(printf '%s' "$RERANKER_DIGEST" | tr ':' '-' | cut -c1-23)"; \
    base="https://huggingface.co/$RERANKER_MODEL/resolve/$RERANKER_REVISION"; \
    for name in "$RERANKER_MODEL_FILE" tokenizer.json; do \
      mkdir -p "$(dirname "$files/$name")"; \
      python -c 'import shutil, sys, urllib.request; response = urllib.request.urlopen(sys.argv[1], timeout=60); target = open(sys.argv[2], "wb"); shutil.copyfileobj(response, target, 1 << 20); target.close()' \
        "$base/$name" "$files/$name"; \
    done; \
    printf '%s  %s\n' "$RERANKER_MODEL_SHA256" "$files/$RERANKER_MODEL_FILE" \
        "$RERANKER_TOKENIZER_SHA256" "$files/tokenizer.json" | sha256sum --check --strict -

# No files: an empty directory in their place.
FROM ${PYTHON_IMAGE} AS reranker-none
RUN mkdir /models

FROM reranker-${RERANKER_FILES} AS reranker


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

# Owned by root and not writable by the service user: the service cannot change its own code, nor
# its model. The model files come first: they change less often than the code.
COPY --from=reranker /models /opt/memini/models
COPY --from=build /opt/memini/venv /opt/memini/venv

# Inside a container the service listens on every interface of the container's own network: on
# 127.0.0.1 nothing outside the container could reach it. Who can reach the container is decided
# where its port is published; compose.yaml publishes it on the host's 127.0.0.1 only.
# The reranker is on, with the files above; MEMINI_SEARCH_RERANKER=none in the settings file
# (.env.memini) turns it off. onnxruntime sends usage events to Microsoft unless
# ORT_DISABLE_TELEMETRY is set: the service sets it before it imports the runtime, and the image
# sets it for any other process too (memini.embeddings.runtime).
ENV PATH=/opt/memini/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ORT_DISABLE_TELEMETRY=1 \
    MEMINI_HTTP_HOST=0.0.0.0 \
    MEMINI_HTTP_PORT=6364 \
    MEMINI_GIT_MIRROR_DIR=/var/lib/memini/mirrors \
    MEMINI_SEARCH_RERANKER=local \
    MEMINI_SEARCH_RERANKER_MODEL_DIR=/opt/memini/models

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
