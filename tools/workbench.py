"""Serve the shared review desk and both specialist dashboards from one server.

The review desk keeps the root path; Swarm Detector and Silent Storm mount
under their own prefixes on the same Flask server. Each surface shows the
same Traced workbench switcher, styled by the shared identity assets.
"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Event, Thread

from flask import Flask

SWARM_PREFIX = "/swarm/"
SILENT_STORM_PREFIX = "/silent-storm/"
WORKBENCH_LINKS = [
    ("Review desk", "/"),
    ("Swarm Detector", SWARM_PREFIX),
    ("Silent Storm", SILENT_STORM_PREFIX),
]


def create_workbench(with_worker: bool = True) -> tuple[Flask, Event, Thread | None]:
    """Compose the three Dash apps on one Flask server and start the worker."""
    from evidence_explorer.config import Settings
    from evidence_explorer.review.integrations.web import create_app as create_desk
    from evidence_explorer.review.integrations.worker import WorkspaceWorker
    from evidence_explorer.review.integrations.workspace import (
        WorkspaceRepository,
        WorkspaceService,
    )

    settings = Settings.from_env()
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, settings.data_dir / "trail-artifacts")
    server = Flask(__name__)
    stop = Event()
    thread = None
    if with_worker:
        thread = Thread(target=WorkspaceWorker(repository, settings).run, args=(stop,), daemon=True)
        thread.start()

    create_desk(service, server=server, workbench_links=WORKBENCH_LINKS)

    from silent_storm.app import create_app as create_silent_storm
    from swarm_detector.web import create_app as create_swarm

    create_swarm(url_base_pathname=SWARM_PREFIX, server=server, workbench_links=WORKBENCH_LINKS)
    create_silent_storm(
        flask_server=server,
        url_base_pathname=SILENT_STORM_PREFIX,
        workbench_links=WORKBENCH_LINKS,
    )
    return server, stop, thread


def run_workbench(host: str, port: int, with_worker: bool = True, root: Path | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    server, stop, thread = create_workbench(with_worker=with_worker)
    from werkzeug.serving import run_simple

    print(f"Traced workbench: review desk on http://{host}:{port}/")
    print(f"Swarm Detector on http://{host}:{port}{SWARM_PREFIX}")
    print(f"Silent Storm on http://{host}:{port}{SILENT_STORM_PREFIX}")
    try:
        run_simple(host, port, server, threaded=True, use_reloader=False)
    finally:
        stop.set()
        if thread:
            thread.join(timeout=2)


if __name__ == "__main__":
    run_workbench("127.0.0.1", 8053)
