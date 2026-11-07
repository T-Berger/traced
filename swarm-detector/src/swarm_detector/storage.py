"""Compatibility facade over focused, typed SQLite repositories.

New application code uses ``datasets``, ``runs``, ``cases``, ``evidence`` and
``reviews``. Legacy dictionary methods remain explicit serialization boundaries.
The database and migration history are local to Swarm; selecting an integrated
Evidence Trail workspace does not merge this store into Explorer's database.
"""

from __future__ import annotations

from pathlib import Path

from .models import AnalysisRun, Dataset, Review
from .persistence.cases import CaseRepository
from .persistence.database import Database, default_db_path
from .persistence.datasets import DatasetRepository
from .persistence.evidence import EvidenceRepository
from .persistence.readiness import ReadinessRepository
from .persistence.reviews import ReviewRepository
from .persistence.runs import RunRepository
from .serialization import canonical_json, fingerprint, utc_now

__all__ = ["Store", "canonical_json", "default_db_path", "fingerprint", "utc_now"]


class Store:
    """Initialize the specialist database and expose its repository operations.

    Construction creates the parent directory and applies schema migrations via
    ``Database``. Methods use operation-local connections; the facade does not
    own an integrated worker, model transport, or shared evidence-case storage.
    """

    def __init__(self, path: str | Path | None = None):
        self.database = Database(path)
        self.path = self.database.path
        self.datasets = DatasetRepository(self.database)
        self.runs = RunRepository(self.database)
        self.cases = CaseRepository(self.database)
        self.evidence = EvidenceRepository(self.database)
        self.reviews = ReviewRepository(self.database)
        self.readiness = ReadinessRepository(self.database)

    def connection(self):
        return self.database.connection()

    def put_dataset(self, dataset: dict | Dataset) -> str:
        model = dataset if isinstance(dataset, Dataset) else Dataset.model_validate(dataset)
        return self.datasets.write(model, dataset if isinstance(dataset, dict) else None)

    def get_dataset(self, dataset_id: str | None = None) -> dict | None:
        """Explicit full-corpus compatibility read; interactive views do not call it."""
        dataset = self.datasets.materialize(dataset_id)
        return dataset.to_record() if dataset else None

    def put_run(self, run: dict | AnalysisRun) -> None:
        model = run if isinstance(run, AnalysisRun) else AnalysisRun.model_validate(run)
        self.runs.write(model, run if isinstance(run, dict) else None)

    def get_run(self, run_id: str | None = None) -> dict | None:
        """Explicit full-analysis compatibility read, including every pair/event."""
        run = self.runs.materialize(run_id)
        return run.to_record() if run else None

    def active_run_id(self) -> str | None:
        return self.runs.active_id()

    def save_review(self, review: dict | Review, expected_revision: int | None = None) -> dict:
        model = review if isinstance(review, Review) else Review.model_validate(review)
        return self.reviews.save(model, expected_revision=expected_revision).to_record()

    def get_review(self, run_id: str, case_id: str, revision: int | None = None) -> dict | None:
        review = (
            self.reviews.latest(run_id, case_id)
            if revision is None
            else self.reviews.revision(run_id, case_id, revision)
        )
        return review.to_record() if review else None

    def review_history(self, run_id: str, case_id: str) -> list[dict]:
        return [review.to_record() for review in self.reviews.history(run_id, case_id)]

    def search(self, dataset_id: str, query: str, limit: int = 30) -> list[dict]:
        return [message.to_record() for message in self.evidence.search(dataset_id, query, limit)]

    def backup(self, destination: str | Path) -> Path:
        return self.database.backup(destination)
