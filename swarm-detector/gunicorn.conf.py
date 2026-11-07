"""One SQLite-backed application worker, managed through container signals.

The optional Gunicorn control socket is disabled so startup also works with a
read-only root filesystem; persistent application data remains in SWARM_DATA_DIR.
"""

import os

bind = f"0.0.0.0:{int(os.environ.get('PORT', '8050'))}"
workers = 1
worker_class = "gthread"
threads = 4
preload_app = False
timeout = 60
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
capture_output = True
loglevel = "info"
control_socket_disable = True
# Exclude query strings, cookies, and authorization headers from access logs.
access_log_format = "%(h)s %(m)s %(U)s %(s)s %(L)s"
