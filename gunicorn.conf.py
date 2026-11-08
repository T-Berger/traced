"""Gunicorn configuration for Railpack."""

bind = "0.0.0.0:$PORT"
workers = 2
threads = 4
timeout = 120
worker_class = "uvicorn.workers.UvicornWorker"
