"""Small, isolated fixtures; tests never require a running model server."""

import pytest

from evidence_explorer.config import Settings
from evidence_explorer.db import Repository
from evidence_explorer.models import ArticleInput


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, ollama_url="http://127.0.0.1:1")


@pytest.fixture
def repo(settings):
    repository = Repository(settings.db_path)
    repository.initialize()
    return repository


@pytest.fixture
def article():
    return ArticleInput(
        title="Fictional bridge inspection announced",
        publisher="Example City Bulletin",
        url="https://bulletin.example/bridge",
        published_at="2026-11-01T09:00:00+00:00",
        text=(
            "The fictional city announced a bridge inspection on Monday. "
            "The transport office said the bridge would remain open to pedestrians."
        ),
        topic="Bridge inspection",
        is_sample=True,
    )
