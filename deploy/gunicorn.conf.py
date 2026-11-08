"""Gunicorn settings for the hosted Traced workbench.

One worker process keeps one workspace worker lifecycle: the worker thread
starts inside the application, so more processes would mean more workers.
Threads serve concurrent requests.
"""

import os

bind = f"0.0.0.0:{int(os.environ.get('PORT', '8053'))}"
workers = 1
worker_class = "gthread"
threads = int(os.environ.get("TRACED_THREADS", "8"))
preload_app = False
timeout = 120
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
capture_output = True
loglevel = "info"
control_socket_disable = True
# Exclude query strings, cookies, and authorization headers from access logs.
access_log_format = "%(h)s %(m)s %(U)s %(s)s %(L)s"
