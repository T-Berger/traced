"""HTTP smoke checks for the packaged Dash application and its seed data."""

import importlib
import os
import subprocess
import sys


def test_importing_web_does_not_initialize_or_seed_a_database(settings, monkeypatch):
    monkeypatch.setenv("EVIDENCE_DATA_DIR", str(settings.data_dir / "unused"))
    web = importlib.import_module("evidence_explorer.web")
    importlib.reload(web)
    assert not (settings.data_dir / "unused").exists()


def test_wsgi_entrypoint_initializes_a_ready_offline_application(tmp_path):
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from evidence_explorer.wsgi import server; "
            "assert server.test_client().get('/health').status_code == 200",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "EVIDENCE_DATA_DIR": str(tmp_path / "wsgi"),
            "EVIDENCE_READ_ONLY": "false",
            "OLLAMA_BASE_URL": "http://127.0.0.1:1",
        },
        check=True,
        capture_output=True,
        text=True,
    )
    assert (tmp_path / "wsgi" / "evidence.sqlite3").is_file()


def test_dashboard_health_and_layout_work_without_ollama(settings, monkeypatch):
    monkeypatch.setenv("EVIDENCE_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("OLLAMA_BASE_URL", settings.ollama_url)
    web = importlib.import_module("evidence_explorer.web")
    dashboard = web.create_app(settings)
    client = dashboard.server.test_client()
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200
    response = client.get("/_dash-layout")
    assert response.status_code == 200
    assert response.is_json
    layout = response.get_data(as_text=True).lower()
    assert "fictional" in layout or "synthetic" in layout
    assert "bridge" in layout
