# Traced workbench in one image: the review desk at /, Swarm Detector at /swarm/,
# and Silent Storm at /silent-storm/, with the workspace worker, the same server
# that `./traced start` runs locally. Railway builds it through railway.toml.
#
#   docker build -t traced-workbench .
#   docker run --rm -p 8053:8053 traced-workbench      # http://127.0.0.1:8053/
#
# No BuildKit cache mounts: Railway rejects cache mounts without its service ID.
# uv comes from PyPI, which the build needs anyway, pinned to the workspace's version.
FROM python:3.12-slim-bookworm
RUN pip install --no-cache-dir --disable-pip-version-check uv==0.11.3
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=600 \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Dependencies first, so code changes reuse this layer. No extras: Swarm Detector's
# optional embeddings extra would add torch and about 1.9 GB of CUDA wheels that
# the dashboards never import.
COPY pyproject.toml uv.lock .python-version README.md ./
COPY evidence-explorer/pyproject.toml evidence-explorer/pyproject.toml
COPY swarm-detector/pyproject.toml swarm-detector/pyproject.toml
COPY operation-silent-storm/pyproject.toml operation-silent-storm/pyproject.toml
RUN uv sync --frozen --no-default-groups --all-packages --no-install-workspace

COPY evidence-explorer/README.md evidence-explorer/README.md
COPY evidence-explorer/src evidence-explorer/src
COPY swarm-detector/README.md swarm-detector/README.md
COPY swarm-detector/src swarm-detector/src
COPY operation-silent-storm/README.md operation-silent-storm/README.md
COPY operation-silent-storm/src operation-silent-storm/src
RUN uv sync --locked --no-default-groups --all-packages --no-editable

COPY tools/traced.py tools/workbench.py tools/
COPY traced_wsgi.py ./
COPY deploy/ deploy/
RUN chmod 0755 deploy/start.sh \
    && groupadd --gid 10001 traced \
    && useradd --uid 10001 --gid traced --create-home --shell /bin/sh traced \
    && mkdir -p /app/data \
    && chown traced:traced /app/data

# One data directory for all three surfaces, so one Railway volume at /app/data
# keeps every case, Swarm Detector run, and Silent Storm review across deploys.
ENV EVIDENCE_HOST=0.0.0.0 \
    EVIDENCE_DATA_DIR=/app/data \
    SWARM_DATA_DIR=/app/data/swarm \
    SILENT_STORM_PROJECT=/app/operation-silent-storm \
    SILENT_STORM_DB=/app/data/silent-storm/silent-storm.sqlite3 \
    PORT=8053
EXPOSE 8053
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8053') + '/health', timeout=4).read()"

# Starts as root only to take ownership of a mounted volume, then runs as traced.
CMD ["/app/deploy/start.sh"]
