# Dockerfile for Traced Evidence Workbench - Railway Deployment
# This builds and deploys the complete integrated workspace with all dashboards

# Use official Python image
FROM python:3.12-slim-bookworm as builder

# Install uv for dependency management
RUN pip install uv==0.11.3

WORKDIR /app

# Copy lock file and project configuration
COPY pyproject.toml uv.lock .python-version ./
COPY evidence-explorer/pyproject.toml evidence-explorer/pyproject.toml
COPY swarm-detector/pyproject.toml swarm-detector/pyproject.toml
COPY operation-silent-storm/pyproject.toml operation-silent-storm/pyproject.toml

# Install all dependencies from the root lock file
RUN uv sync --frozen --all-extras --all-packages --no-install-workspace

# Copy application source code
COPY evidence-explorer/src evidence-explorer/src
COPY evidence-explorer/README.md evidence-explorer/README.md
COPY swarm-detector/src swarm-detector/src
COPY swarm-detector/README.md swarm-detector/README.md
COPY operation-silent-storm/src operation-silent-storm/src
COPY operation-silent-storm/README.md operation-silent-storm/README.md
COPY tools tools
COPY traced traced
COPY traced-investigate traced-investigate

# Install with locked dependencies
RUN uv sync --locked --all-extras --all-packages --no-editable

# Final runtime stage
FROM python:3.12-slim-bookworm as runtime

WORKDIR /app

# Set up environment
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EVIDENCE_HOST=0.0.0.0 \
    EVIDENCE_DATA_DIR=/app/data \
    EVIDENCE_READ_ONLY=false

# Create non-root user for security
RUN groupadd --gid 10001 traced \
    && useradd --uid 10001 --gid traced --create-home --shell /bin/sh traced \
    && mkdir -p /app/data /app/.venv \
    && chown traced:traced /app/data

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application files
COPY --chown=traced:traced evidence-explorer/src evidence-explorer/src
COPY --chown=traced:traced swarm-detector/src swarm-detector/src
COPY --chown=traced:traced operation-silent-storm/src operation-silent-storm/src
COPY --chown=traced:traced tools tools
COPY --chown=traced:traced traced traced
COPY --chown=traced:traced traced-investigate traced-investigate
COPY --chown=traced:traced traced_wsgi.py traced_wsgi.py

# Switch to non-root user
USER 10001:10001

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${PORT:-8000}/health', timeout=4).read()" || exit 1

# Expose port
EXPOSE 8000

# Start the application
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "traced_wsgi:server", "--bind", "0.0.0.0:${PORT:-8000}", "--workers", "2", "--threads", "4", "--timeout", "120"]
