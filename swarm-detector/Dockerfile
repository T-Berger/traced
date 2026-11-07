# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.11.3 AS uv

FROM python:3.12-slim-bookworm AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Build from the EDTH root; install only this member's production dependencies.
COPY pyproject.toml uv.lock .python-version README.md ./
COPY evidence-explorer/pyproject.toml evidence-explorer/pyproject.toml
COPY swarm-detector/pyproject.toml swarm-detector/pyproject.toml
COPY operation-silent-storm/pyproject.toml operation-silent-storm/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-default-groups --package swarm-detector --no-install-workspace
COPY swarm-detector/README.md swarm-detector/README.md
COPY swarm-detector/src/ swarm-detector/src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --package swarm-detector --no-editable

FROM python:3.12-slim-bookworm AS runtime
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SWARM_DATA_DIR=/var/data \
    PORT=8050
WORKDIR /app
RUN groupadd --gid 10001 swarm \
    && useradd --uid 10001 --gid swarm --create-home --shell /bin/sh swarm \
    && mkdir -p /var/data /home/swarm/.ssh \
    && chmod 0700 /home/swarm/.ssh \
    && chown swarm:swarm /var/data /home/swarm/.ssh
COPY --from=builder /app/.venv /app/.venv
COPY swarm-detector/gunicorn.conf.py ./gunicorn.conf.py
COPY --chmod=755 swarm-detector/scripts/entrypoint.sh ./scripts/entrypoint.sh
USER 10001:10001
EXPOSE 8050
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8050') + '/health', timeout=4).read()"
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
