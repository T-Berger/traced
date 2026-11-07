"""Service status and presentation mode must reflect actual worker state."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from evidence_explorer.service import ExplorerService


def test_status_does_not_expose_the_mutable_model_cache(settings, monkeypatch):
    status = {"available": True, "models": ["original"], "missing": [], "error": None}
    monkeypatch.setattr("evidence_explorer.service.OllamaClient.status", lambda _: status)
    service = ExplorerService(settings)
    response = service.status()
    response["ollama"]["models"].append("injected")
    response["ollama"]["available"] = False
    assert service.status()["ollama"]["models"] == ["original"]
    assert service.status()["ollama"]["available"] is True


def test_stopped_worker_is_offline_even_with_a_recent_heartbeat(settings):
    service = ExplorerService(settings)
    service.repo.set_meta("worker_heartbeat", datetime.now(UTC).isoformat())
    service.repo.set_meta("worker_state", "stopped")
    assert service.status()["worker"]["online"] is False
    service.repo.set_meta("worker_state", "running")
    assert service.status()["worker"]["online"] is True


def test_read_only_service_cannot_enqueue_jobs(repo, settings, tmp_path):
    destination = tmp_path / "snapshot" / "evidence.sqlite3"
    repo.backup(destination)
    snapshot_settings = replace(settings, data_dir=destination.parent, read_only=True)
    service = ExplorerService(snapshot_settings)
    with pytest.raises(PermissionError):
        service.enqueue_analysis("demo")
    with pytest.raises(PermissionError):
        service.enqueue_search("bridge", "demo")
    assert service.status()["worker"]["online"] is False
