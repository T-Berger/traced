"""WSGI entry for hosted runs of the Traced workbench (Docker, Railway).

Selects the workspace exactly like ``./traced`` (``EVIDENCE_DATA_DIR``, absolute
or relative to the repository root) and builds the same Flask server that
``./traced start`` serves: the review desk at ``/``, Swarm Detector at
``/swarm/``, Silent Storm at ``/silent-storm/``, and the workspace worker in this
process. Serve it with one gunicorn worker process (``deploy/gunicorn.conf.py``)
so the workspace keeps one worker lifecycle. ``TRACED_NO_WORKER=1`` serves the
dashboards without the worker.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.traced import configure_environment  # noqa: E402
from tools.workbench import create_workbench  # noqa: E402

configuration = configure_environment(ROOT, os.environ.get("EVIDENCE_DATA_DIR") or None)
Path(configuration["data_dir"]).mkdir(parents=True, exist_ok=True)
# The review desk already serves /health (Swarm Detector and Silent Storm add
# /swarm/health and /silent-storm/health); Railway's health check uses /health.
application, _stop, _worker = create_workbench(
    with_worker=os.environ.get("TRACED_NO_WORKER", "").lower() not in {"1", "true", "yes"}
)
