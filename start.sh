#!/bin/bash
# Railway start script for Traced integrated workspace

exec gunicorn -k uvicorn.workers.UvicornWorker traced_wsgi:server --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 4 --timeout 120
