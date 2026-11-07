"""The convenience launcher selects the same configured store as Explorer.

The launcher is copied into a temporary project and invokes a fake uv executable.
That executable reads an unchanged copy of the real Settings implementation, so
environment/dotenv resolution is exercised without starting a service or touching
the user's project configuration and data.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import evidence_explorer.config as configuration
from evidence_explorer.config import Settings
from evidence_explorer.db import Repository
from evidence_explorer.demo import seed_demo
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def isolated_launcher(tmp_path):
    project = tmp_path / "isolated project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    launcher = scripts / "start-trail.sh"
    launcher.write_bytes((PROJECT_ROOT / "scripts" / "start-trail.sh").read_bytes())
    package = project / "src" / "evidence_explorer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "config.py").write_bytes(Path(configuration.__file__).read_bytes())
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from evidence_explorer.config import Settings\n"
        "settings = Settings.from_env()\n"
        "print(json.dumps({'cwd': os.getcwd(), 'arguments': sys.argv[1:], "
        "'venv': os.environ.get('UV_PROJECT_ENVIRONMENT'), "
        "'data_dir': str(settings.data_dir), 'db_path': str(settings.db_path)}))\n"
    )
    fake_uv.chmod(0o755)
    outside = tmp_path / "caller"
    outside.mkdir()
    (outside / ".env").write_text("EVIDENCE_DATA_DIR=./wrong-caller-data\n")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("EVIDENCE_") and key != "PYTHON_DOTENV_DISABLED"
    }
    env["PATH"] = str(fake_bin) + os.pathsep + os.environ.get("PATH", os.defpath)
    env["PYTHONPATH"] = str(project / "src")

    def run(*, explicit_data_dir=None, dotenv_data_dir=None, arguments=()):
        current_env = dict(env)
        if explicit_data_dir is not None:
            current_env["EVIDENCE_DATA_DIR"] = explicit_data_dir
        if dotenv_data_dir is not None:
            (project / ".env").write_text(f'EVIDENCE_DATA_DIR="{dotenv_data_dir}"\n')
        result = subprocess.run(
            ["/bin/bash", str(launcher), *arguments],
            cwd=outside,
            env=current_env,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        return json.loads(result.stdout)

    return project, run


@pytest.mark.parametrize(
    ("explicit_data_dir", "dotenv_data_dir", "expected_relative"),
    [
        (None, None, "data"),
        ("./chosen-by-environment", None, "chosen-by-environment"),
        (None, "./chosen from dotenv", "chosen from dotenv"),
        ("./environment-wins", "./dotenv-loses", "environment-wins"),
        ("./data/evidence-trail", None, "data/evidence-trail"),
    ],
    ids=["default", "environment", "dotenv", "environment-precedence", "legacy-store"],
)
def test_launcher_uses_shared_settings_without_forcing_a_different_database(
    isolated_launcher, explicit_data_dir, dotenv_data_dir, expected_relative
):
    project, launch = isolated_launcher
    captured = launch(
        explicit_data_dir=explicit_data_dir,
        dotenv_data_dir=dotenv_data_dir,
        arguments=("--port", "8097"),
    )
    assert Path(captured["cwd"]) == project
    assert Path(captured["data_dir"]) == project / expected_relative
    assert Path(captured["db_path"]) == project / expected_relative / "evidence.sqlite3"
    assert Path(captured["venv"]) == project / ".venv-trail"
    assert captured["arguments"][:4] == ["run", "--locked", "--extra", "swarm"]
    assert "--data-dir" not in captured["arguments"]
    assert "evidence_explorer.review.integrations" in captured["arguments"]
    assert "--with-worker" in captured["arguments"]
    assert captured["arguments"][-2:] == ["--port", "8097"]
    assert not Path(captured["data_dir"]).exists()


def test_launcher_preserves_an_explicit_absolute_data_directory(isolated_launcher, tmp_path):
    project, launch = isolated_launcher
    destination = tmp_path / "separate saved cases"
    captured = launch(explicit_data_dir=str(destination))
    assert Path(captured["data_dir"]) == destination
    assert Path(captured["data_dir"]) != project / "data"
    assert not destination.exists()


@pytest.mark.parametrize("selection", ["default", "environment", "dotenv"])
def test_saved_sample_articles_reach_review_in_the_launchers_selected_temporary_store(
    isolated_launcher, selection, monkeypatch
):
    project, launch = isolated_launcher
    captured = launch(
        explicit_data_dir="./shared-environment" if selection == "environment" else None,
        dotenv_data_dir="./shared-dotenv" if selection == "dotenv" else None,
    )
    settings = Settings(data_dir=Path(captured["data_dir"]), ollama_url="http://127.0.0.1:1")
    explorer = Repository(settings.db_path)
    explorer.initialize()
    seed_demo(explorer)  # Saved deterministic fixtures; no model or worker is invoked.
    saved_articles = explorer.articles.list_articles()
    assert saved_articles
    source_urls = {article.url for article in saved_articles}

    def unexpected_seed(_repo):
        pytest.fail("Review must read the existing source store rather than seed another one")

    monkeypatch.setattr("evidence_explorer.demo.seed_demo", unexpected_seed)
    reviews = WorkspaceRepository(settings.db_path)
    reviews.initialize()
    workspace = WorkspaceService(reviews, settings, settings.data_dir / "trail-artifacts")
    case_id = workspace.import_explorer()
    snapshot = reviews.get_bundle(case_id)
    assert {
        record.source_url for record in snapshot.records if record.kind == "article"
    } == source_urls
    assert snapshot.findings
    assert explorer.articles.list_articles() == saved_articles
    assert reviews.db_path == explorer.db_path == Path(captured["db_path"])
    assert not (project / "data" / "evidence-trail").exists()
    assert explorer.jobs.list_jobs() == []
