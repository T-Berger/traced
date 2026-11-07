"""Import the exercise into a new workspace and prepare its empty media assessment."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from evidence_explorer.config import Settings
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService

HERE = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True, help="A new disposable directory")
    args = parser.parse_args()
    data_dir = args.data_dir.expanduser().resolve()
    if data_dir.exists():
        parser.error("Choose a new directory. An existing workspace is never reused.")

    manifest = json.loads((HERE / "fixture-manifest.json").read_bytes())
    package = (HERE.parent / "fixtures/case_packages/fictional-core-demo.zip").read_bytes()
    if hashlib.sha256(package).hexdigest() != manifest["package_sha256"]:
        parser.error("The fixture package does not match its manifest.")
    data_dir.mkdir()
    settings = Settings(data_dir=data_dir, ollama_url="http://127.0.0.1:1")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, data_dir / "trail-artifacts")
    case_id = service.import_package(package)
    if (
        case_id != manifest["case_id"]
        or service.case_fingerprint(case_id) != manifest["native_bundle_fingerprint"]
    ):
        raise ValueError("The imported case identity differs from the fixture manifest.")
    runs = repository.list_runs(case_id)
    expected_runs = [run["id"] for run in manifest["ordered_runs"]]
    if [run.id for run in runs] != expected_runs:
        raise ValueError("The imported run order differs from the fixture manifest.")
    media = service.start_scenario_state(case_id, manifest["media_inference"])
    if media.base_run_id != expected_runs[1] or media.excluded_record_ids or media.version != 1:
        raise ValueError("The prepared assessment must be empty and pinned to the media run.")

    # Record real setup state before any browser is opened.
    with sqlite3.connect(settings.db_path.as_uri() + "?mode=ro", uri=True) as connection:
        counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "review_cases",
                "review_scenarios",
                "review_events",
                "review_decisions",
                "review_jobs",
                "collection_sources",
                "collection_jobs",
            )
        }
    expected_counts = {
        "review_cases": 1,
        "review_scenarios": 1,
        "review_events": 1,
        "review_decisions": 0,
        "review_jobs": 0,
        "collection_sources": 0,
        "collection_jobs": 0,
    }
    if counts != expected_counts:
        raise ValueError("The disposable workspace does not have the expected clean setup state.")
    print(
        json.dumps(
            {
                "workspace": str(data_dir),
                "case_id": case_id,
                "native_bundle_fingerprint": service.case_fingerprint(case_id),
                "ordered_run_ids": expected_runs,
                "media_assessment": media.model_dump(mode="json"),
                "counts": counts,
                "setup_event": "Real local scenario creation only",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
