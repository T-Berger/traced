"""Repository boundaries, indexed reads, and shared-transaction behavior."""

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.models import ReviewJob, ScenarioState
from evidence_explorer.review.storage import ReviewRepository


@pytest.fixture
def repository(tmp_path):
    repo = ReviewRepository(tmp_path / "review.sqlite3")
    repo.initialize()
    repo.import_bundle(load_demo_bundle())
    return repo


def test_components_return_validated_records_and_facade_preserves_wire_shapes(repository):
    bundle = load_demo_bundle()
    state = repository.scenarios.create(bundle.case.id, bundle.runs[-1].id)
    assert isinstance(state, ScenarioState)
    assert repository.get_scenario(state.id) == state.model_dump(mode="json")
    job = repository.jobs.enqueue(bundle.case.id, bundle.findings[-1].snapshot_id, state.id, "demo")
    assert isinstance(job, ReviewJob)
    assert repository.get_job(job.id) == job.model_dump(mode="json", exclude_unset=True)


def test_metadata_and_mutations_do_not_load_the_full_saved_bundle(tmp_path):
    queries = []

    class TracedRepository(ReviewRepository):
        @contextmanager
        def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
            with super()._connection(write=write) as connection:
                connection.set_trace_callback(queries.append)
                yield connection

    repo = TracedRepository(tmp_path / "review.sqlite3")
    repo.initialize()
    bundle = load_demo_bundle()
    repo.import_bundle(bundle)
    queries.clear()
    case_id = bundle.case.id
    finding = bundle.findings[-1]
    record = bundle.records[0]
    fragment = bundle.fragments[0]
    assert repo.get_case_metadata(case_id).case == bundle.case
    assert repo.get_case_fingerprint(case_id) == bundle.fingerprint()
    assert repo.get_finding(case_id, finding.snapshot_id) == finding
    assert repo.list_runs(case_id) == bundle.runs
    assert repo.has_record(case_id, record.id)
    assert repo.get_fragment_records(case_id, [fragment.id, "missing"]) == {
        fragment.id: fragment.record_id
    }
    state = repo.create_scenario(case_id, finding.run_id)
    repo.set_exclusion(state["id"], record.id, True, "Review this source separately", 1)
    repo.save_review(case_id, finding.snapshot_id, state["id"], "Mina", "qualify", "Checked")
    repo.enqueue_job(case_id, finding.snapshot_id, state["id"], "demo")
    assert repo.list_events(case_id)
    assert queries
    assert all("bundle_json" not in query.lower() for query in queries)


def test_snapshot_returns_the_fingerprint_from_the_same_stored_row(repository):
    bundle, fingerprint = repository.get_bundle_snapshot(load_demo_bundle().case.id)
    assert fingerprint == bundle.fingerprint()


def test_job_configuration_is_pinned_and_distinguishes_active_requests(repository):
    bundle = load_demo_bundle()
    state = repository.scenarios.create(bundle.case.id, bundle.runs[-1].id)
    configuration = {
        "model": "local-review-model",
        "model_identity": "local-review-model@revision-a",
        "prompt_version": "review-v2",
        "retrieval_version": "passages-v1",
        "reason": "Check the source interpretation",
    }
    first = repository.enqueue_job_state(
        bundle.case.id, bundle.findings[-1].snapshot_id, state.id, "ollama", **configuration
    )
    assert first.payload.model_identity == configuration["model_identity"]
    assert first.payload.reason == configuration["reason"]
    assert (
        repository.enqueue_job_state(
            bundle.case.id, bundle.findings[-1].snapshot_id, state.id, "ollama", **configuration
        ).id
        == first.id
    )
    configuration["model_identity"] = "local-review-model@revision-b"
    assert (
        repository.enqueue_job_state(
            bundle.case.id, bundle.findings[-1].snapshot_id, state.id, "ollama", **configuration
        ).id
        != first.id
    )


def test_additive_import_updates_object_projections_without_removing_existing_ids(repository):
    bundle = load_demo_bundle()
    incoming = bundle.model_dump(mode="json")
    new_run = {**incoming["runs"][0], "id": "additional-run"}
    new_finding = {
        **incoming["findings"][0],
        "snapshot_id": "additional-finding",
        "run_id": new_run["id"],
    }
    incoming["runs"] = [new_run]
    incoming["findings"] = [new_finding]
    incoming["links"] = []
    incoming["reviews"] = []
    incoming["scenarios"] = []
    repository.import_bundle(EvidenceBundle.model_validate(incoming))
    assert repository.get_finding(bundle.case.id, "additional-finding").run_id == new_run["id"]
    assert (
        repository.get_finding(bundle.case.id, bundle.findings[0].snapshot_id) == bundle.findings[0]
    )
    assert [run.id for run in repository.list_runs(bundle.case.id)] == [
        *(run.id for run in bundle.runs),
        "additional-run",
    ]
    assert repository.get_case_metadata(bundle.case.id).finding_count == len(bundle.findings) + 1


def test_bound_components_do_not_commit_the_callers_transaction(repository):
    class BoundRepository(ReviewRepository):
        def __init__(self, connection):
            super().__init__(repository.db_path)
            self.connection = connection

        @contextmanager
        def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
            yield self.connection

    payload = load_demo_bundle().model_dump(mode="json")
    payload["case"]["id"] = "another-case"
    bundle = EvidenceBundle.model_validate(payload)
    with pytest.raises(RuntimeError, match="abort the operation"):
        with repository._connection(write=True) as connection:
            bound = BoundRepository(connection)
            bound.import_bundle(bundle)
            state = bound.scenarios.create(bundle.case.id, bundle.runs[-1].id)
            bound.history.save_review(
                bundle.case.id,
                bundle.findings[-1].snapshot_id,
                state.id,
                "Mina",
                "qualify",
                "Transaction is not committed yet",
            )
            assert connection.in_transaction
            raise RuntimeError("abort the operation")
    with pytest.raises(KeyError, match="Unknown case"):
        repository.get_case_metadata(bundle.case.id)
    with sqlite3.connect(repository.db_path) as connection:
        for table in (
            "review_record_ids",
            "review_fragment_records",
            "review_runs",
            "review_findings",
        ):
            assert (
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE case_id = ?", (bundle.case.id,)
                ).fetchone()[0]
                == 0
            )


def test_concurrent_baseline_creation_reuses_one_context_and_event(repository):
    bundle = load_demo_bundle()

    def create(_):
        return repository.scenarios.create(
            bundle.case.id, bundle.runs[-1].id, assessment_kind="baseline"
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        states = list(pool.map(create, range(4)))
    assert len({state.id for state in states}) == 1
    assert repository.get_scenario_kind(states[0].id) == "baseline"
    assert repository.list_scenario_states(bundle.case.id) == [(states[0], "baseline")]
    assert len(repository.list_events(bundle.case.id)) == 1


def test_case_and_audit_reads_can_be_paginated(repository):
    bundle = load_demo_bundle()
    payload = bundle.model_dump(mode="json")
    payload["case"]["id"] = "another-case"
    repository.import_bundle(EvidenceBundle.model_validate(payload))
    assert len(repository.list_case_page(limit=1)) == 1
    assert repository.list_case_page(limit=1, offset=1)[0].id == "another-case"
    for _ in range(3):
        repository.create_scenario(bundle.case.id, bundle.runs[-1].id)
    all_events = repository.list_audit_events(bundle.case.id)
    assert repository.list_audit_events(bundle.case.id, limit=1, offset=1) == all_events[1:2]
