# syntax=docker/dockerfile:1
# Dockerfile for Operation Silent Storm - Historical Media Evidence Workbench

FROM ghcr.io/astral-sh/uv:0.11.3 AS uv

FROM python:3.12-slim-bookworm AS builder
COPY --from=uv /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=600 \
    UV_PYTHON_DOWNLOADS=never

# Copy root configuration
COPY pyproject.toml uv.lock .python-version README.md ./
COPY evidence-explorer/pyproject.toml evidence-explorer/pyproject.toml
COPY swarm-detector/pyproject.toml swarm-detector/pyproject.toml
COPY operation-silent-storm/pyproject.toml operation-silent-storm/pyproject.toml

# Install dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-default-groups --package silent-storm --no-install-workspace

# Copy source code
COPY operation-silent-storm/src operation-silent-storm/src
COPY operation-silent-storm/README.md operation-silent-storm/README.md

# Install with locked dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --package silent-storm --no-editable

# Runtime stage
FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8052 \
    SILENT_STORM_DB_PATH=/app/silent-storm.db \
    SILENT_STORM_DATASET_ROOT=/app/dataset

WORKDIR /app

# Create non-root user
RUN groupadd --gid 10002 silentstorm \
    && useradd --uid 10002 --gid silentstorm --create-home --shell /bin/sh silentstorm \
    && mkdir -p /app/dataset /app/media \
    && chown silentstorm:silentstorm /app/dataset /app/media

COPY --from=builder /app/.venv /app/.venv
COPY --chown=silentstorm:silentstorm operation-silent-storm/src operation-silent-storm/src

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${PORT:-8052}/health', timeout=4).read()" || exit 1

USER 10002:10002

EXPOSE 8052

# Use the app module which has a Flask server
CMD ["sh", "-c", "exec gunicorn -k uvicorn.workers.UvicornWorker -b 0.0.0.0:${PORT:-8052} --workers 1 --threads 4 --timeout 120 silent_storm.app:create_app"]
