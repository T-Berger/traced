# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.11.3 AS uv

FROM python:3.12-slim-bookworm
COPY --from=uv /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=600 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH" \
    EVIDENCE_HOST=0.0.0.0 \
    EVIDENCE_PORT=8050 \
    EVIDENCE_DATA_DIR=/app/data

# Build from the EDTH root so the shared lock and workspace members are available.
COPY pyproject.toml uv.lock .python-version README.md ./
COPY evidence-explorer/pyproject.toml evidence-explorer/pyproject.toml
COPY swarm-detector/pyproject.toml swarm-detector/pyproject.toml
COPY operation-silent-storm/pyproject.toml operation-silent-storm/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-default-groups --package evidence-explorer \
    --extra hosting --no-install-workspace
COPY evidence-explorer/README.md evidence-explorer/README.md
COPY evidence-explorer/src evidence-explorer/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --package evidence-explorer \
    --extra hosting --no-editable

# A standalone hosted viewer starts with the fictional offline corpus. Mount an
# exported investigation at /app/data to replace it with your own snapshot.
RUN evidence-explorer seed
ENV EVIDENCE_READ_ONLY=true

EXPOSE 8050
CMD ["sh", "-c", "exec gunicorn evidence_explorer.wsgi:server --bind 0.0.0.0:${PORT:-8050} --workers 1 --threads 4 --timeout 120"]
