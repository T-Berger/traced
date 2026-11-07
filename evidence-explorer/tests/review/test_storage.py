"""Persistence, isolation, and worker recovery checks for analyst review storage."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.storage import ReviewRepository, VersionConflictError


def make_bundle(case_id: str = "case-a") -> EvidenceBundle:
    text = "The accounts published the same link at 09:00."
    return EvidenceBundle.model_validate(
        {
            "case": {
                "id": case_id,
                "title": "Synthetic investigation",
                "question": "Coordinated?",
                "synthetic": True,
            },
            "records": [
                {
                    "id": f"{case_id}-record",
                    "kind": "post",
                    "source_label": "Saved post",
                    "text": text,
                    "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                    "producer_note": "Preserve this extension",
                }
            ],
            "fragments": [
                {
                    "id": f"{case_id}-fragment",
                    "record_id": f"{case_id}-record",
                    "selector": {"kind": "text", "start": 0, "end": len(text)},
                    "quote": text,
                }
            ],
            "runs": [
                {
                    "id": f"{case_id}-run",
                    "input_fingerprint": "fixture-v1",
                    "input_manifest": [f"{case_id}-record"],
                    "method": "manual",
                    "method_version": "1",
                    "created_at": "2026-11-06T18:00:00Z",
                }
            ],
            "findings": [
                {
                    "snapshot_id": f"{case_id}-finding",
                    "lineage_id": "coordination",
                    "version": 1,
                    "run_id": f"{case_id}-run",
                    "kind": "inference",
                    "statement": "The accounts may have coordinated their distribution.",
                }
            ],
            "links": [
                {
                    "id": f"{case_id}-link",
                    "source": {"type": "record", "id": f"{case_id}-record"},
                    "target": {"type": "finding", "id": f"{case_id}-finding"},
                    "relation": "supports",
                    "basis": "Same link and publication time.",
                    "fragment_ids": [f"{case_id}-fragment"],
                    "run_id": f"{case_id}-run",
                }
            ],
        }
    )


@pytest.fixture
def repo(tmp_path):
    repository = ReviewRepository(tmp_path / "review.sqlite3")
    repository.initialize()
    repository.import_bundle(make_bundle())
    return repository


def scenario(repo):
    return repo.create_scenario("case-a", "case-a-run", reviewer="Mina")


def enqueue(repo, current_scenario=None):
    current_scenario = current_scenario or scenario(repo)
    return repo.enqueue_job("case-a", "case-a-finding", current_scenario["id"], "demo")


def test_idempotent_import_and_json_roundtrip(repo):
    before = repo.list_cases()
    assert repo.import_bundle(make_bundle()) == "case-a"
    assert repo.list_cases() == before
    stored = repo.get_bundle("case-a").model_dump(mode="json")
    assert stored == make_bundle().model_dump(mode="json")
    assert repo.list_cases()[0]["fingerprint"] == make_bundle().fingerprint()
    json.dumps(repo.list_cases())


def test_additive_import_preserves_missing_objects_and_updates_fingerprint(repo):
    updated = make_bundle().model_dump(mode="json")
    updated["runs"][0]["id"] = "new-run"
    updated["findings"][0].update(snapshot_id="new-finding", version=2, run_id="new-run")
    updated["links"] = []
    old_fingerprint = repo.list_cases()[0]["fingerprint"]
    repo.import_bundle(EvidenceBundle.model_validate(updated))
    bundle = repo.get_bundle("case-a")
    assert {item.id for item in bundle.runs} == {"case-a-run", "new-run"}
    assert {item.snapshot_id for item in bundle.findings} == {"case-a-finding", "new-finding"}
    assert len(bundle.links) == 1
    assert repo.list_cases()[0]["fingerprint"] != old_fingerprint
    assert repo.list_cases()[0]["fingerprint"] == bundle.fingerprint()
    repo.import_bundle(EvidenceBundle.model_validate(updated))
    assert len(repo.get_bundle("case-a").findings) == 2


def test_changed_immutable_record_is_rejected_atomically(repo):
    updated = make_bundle().model_dump(mode="json")
    updated["records"][0]["source_label"] = "Changed source attribution"
    with pytest.raises(ValueError, match="Immutable records ID"):
        repo.import_bundle(EvidenceBundle.model_validate(updated))
    assert repo.get_bundle("case-a").fingerprint() == make_bundle().fingerprint()


def test_mutated_model_cannot_bypass_import_validation(repo):
    bundle = make_bundle("case-b")
    bundle.records[0].text += " Mutated after model construction."
    with pytest.raises(ValueError, match="Content hash mismatch"):
        repo.import_bundle(bundle)
    assert [item["id"] for item in repo.list_cases()] == ["case-a"]


def test_changed_case_metadata_is_rejected(repo):
    updated = make_bundle().model_dump(mode="json")
    updated["case"]["question"] = "An entirely different investigation?"
    with pytest.raises(ValueError, match="metadata changed: case"):
        repo.import_bundle(EvidenceBundle.model_validate(updated))


def test_exclusion_is_reversible_persistent_and_leaves_baseline_unchanged(repo):
    current = scenario(repo)
    baseline = repo.get_bundle("case-a").fingerprint()
    changed = repo.set_exclusion(current["id"], "case-a-record", True, "Unreliable", 1)
    assert changed["version"] == 2
    assert changed["excluded_record_ids"] == ["case-a-record"]
    reopened = ReviewRepository(repo.db_path)
    assert reopened.get_scenario(current["id"]) == changed
    assert repo.get_bundle("case-a").fingerprint() == baseline
    assert repo.set_exclusion(current["id"], "case-a-record", True, "Again", 2) == changed
    restored = repo.set_exclusion(current["id"], "case-a-record", False, "Restored", 2)
    assert restored["version"] == 3
    assert restored["excluded_record_ids"] == []
    events = repo.list_events("case-a")
    assert [event["event_type"] for event in events] == [
        "scenario_created",
        "exclusion_changed",
        "exclusion_changed",
    ]
    assert events[1]["payload"]["reason"] == "Unreliable"
    assert events[1]["reviewer"] == "Mina"
    json.dumps(events)


def test_scenario_rejects_cross_case_references(repo):
    repo.import_bundle(make_bundle("case-b"))
    current = scenario(repo)
    with pytest.raises(ValueError, match="does not belong"):
        repo.create_scenario("case-a", "case-b-run")
    with pytest.raises(ValueError, match="does not belong"):
        repo.set_exclusion(current["id"], "case-b-record", True, "Wrong case", 1)
    with pytest.raises(ValueError, match="does not belong"):
        repo.save_review("case-b", "case-b-finding", current["id"], "Mina", "use", "Checked")
    assert repo.get_scenario(current["id"])["version"] == 1


def test_concurrent_edits_require_current_scenario_version(repo):
    current = scenario(repo)

    def edit(_):
        try:
            return repo.set_exclusion(current["id"], "case-a-record", True, "Checked", 1)
        except VersionConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, range(2)))
    assert results.count("conflict") == 1
    assert repo.get_scenario(current["id"])["version"] == 2


def test_review_has_valid_disposition_references_and_version(repo):
    current = scenario(repo)
    with pytest.raises(ValueError):
        repo.save_review("case-a", "missing", current["id"], "Mina", "use", "Checked")
    with pytest.raises(ValueError):
        repo.save_review("case-a", "case-a-finding", current["id"], "Mina", "verified", "Checked")
    review = repo.save_review(
        "case-a", "case-a-finding", current["id"], "Mina", "qualify", "Sponsor unestablished"
    )
    assert review["scenario_version"] == 1
    assert review["case_fingerprint"] == make_bundle().fingerprint()
    assert repo.list_events("case-a")[-1]["payload"]["id"] == review["id"]
    assert repo.get_bundle("case-a").reviews == []


def test_review_rejects_stale_scenario_without_recording_a_decision(repo):
    current = scenario(repo)
    fingerprint = repo.get_bundle("case-a").fingerprint()
    repo.set_exclusion(current["id"], "case-a-record", True, "Exclude for review", 1)
    events_before = repo.list_events("case-a")
    with pytest.raises(VersionConflictError, match="Scenario changed"):
        repo.save_review(
            "case-a",
            "case-a-finding",
            current["id"],
            "Mina",
            "use",
            "Checked",
            expected_version=1,
            expected_fingerprint=fingerprint,
        )
    assert repo.list_events("case-a") == events_before
    with sqlite3.connect(repo.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0] == 0


def test_review_rejects_stale_case_and_persists_the_current_fingerprint(repo):
    current = scenario(repo)
    fingerprint = repo.get_bundle("case-a").fingerprint()
    updated = make_bundle().model_dump(mode="json")
    updated["runs"][0]["id"] = "new-run"
    updated["findings"][0].update(snapshot_id="new-finding", version=2, run_id="new-run")
    updated["links"] = []
    repo.import_bundle(EvidenceBundle.model_validate(updated))
    events_before = repo.list_events("case-a")
    with pytest.raises(VersionConflictError, match="Investigation changed"):
        repo.save_review(
            "case-a",
            "case-a-finding",
            current["id"],
            "Mina",
            "use",
            "Checked",
            expected_version=1,
            expected_fingerprint=fingerprint,
        )
    assert repo.list_events("case-a") == events_before
    current_fingerprint = repo.get_bundle("case-a").fingerprint()
    saved = repo.save_review(
        "case-a",
        "case-a-finding",
        current["id"],
        "Mina",
        "qualify",
        "Updated corpus checked",
        expected_version=1,
        expected_fingerprint=current_fingerprint,
    )
    assert saved["case_fingerprint"] == current_fingerprint
    assert repo.list_events("case-a")[-1]["payload"]["case_fingerprint"] == current_fingerprint
    with sqlite3.connect(repo.db_path) as connection:
        stored = connection.execute(
            "SELECT case_fingerprint FROM review_decisions WHERE id = ?", (saved["id"],)
        ).fetchone()
    assert stored[0] == current_fingerprint


def test_initialize_migrates_legacy_decisions_without_inventing_their_fingerprint(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE review_decisions ("
            "id TEXT PRIMARY KEY, case_id TEXT NOT NULL, finding_id TEXT NOT NULL, "
            "scenario_id TEXT NOT NULL, scenario_version INTEGER NOT NULL, "
            "reviewer TEXT NOT NULL, disposition TEXT NOT NULL, reason TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO review_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy",
                "old-case",
                "old-finding",
                "old-scenario",
                1,
                "Prior analyst",
                "use",
                "Checked",
                "2026-11-05T12:00:00+00:00",
            ),
        )
    migrated = ReviewRepository(db_path)
    migrated.initialize()
    migrated.initialize()
    with sqlite3.connect(db_path) as connection:
        old_review = connection.execute(
            "SELECT reason, case_fingerprint FROM review_decisions WHERE id = 'legacy'"
        ).fetchone()
    assert old_review == ("Checked", None)
    migrated.import_bundle(make_bundle())
    current = scenario(migrated)
    saved = migrated.save_review(
        "case-a", "case-a-finding", current["id"], "Mina", "use", "Checked current corpus"
    )
    assert saved["case_fingerprint"] == make_bundle().fingerprint()


def test_job_payload_is_version_pinned_and_active_enqueue_is_idempotent(repo):
    current = scenario(repo)
    job = enqueue(repo, current)
    assert enqueue(repo, current)["id"] == job["id"]
    payload = job["payload"]
    assert payload["case_fingerprint"] == make_bundle().fingerprint()
    assert payload["finding_snapshot"]["snapshot_id"] == "case-a-finding"
    assert payload["scenario_version"] == 1
    assert payload["excluded_record_ids"] == []
    repo.set_exclusion(current["id"], "case-a-record", True, "Temporarily exclude", 1)
    second_job = enqueue(repo, current)
    assert second_job["id"] != job["id"]
    assert second_job["payload"]["scenario_version"] == 2
    assert repo.get_job(job["id"])["payload"] == payload
    assert repo.get_job("missing") is None
    json.dumps(job)


def test_concurrent_workers_cannot_claim_the_same_job(repo):
    job = enqueue(repo)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(lambda _: repo.claim_job(), range(4)))
    claimed = [item for item in claimed if item is not None]
    assert len(claimed) == 1
    assert claimed[0]["id"] == job["id"]
    assert claimed[0]["attempts"] == 1
    assert claimed[0]["lease_expires_at"]
    repo.finish_job(job["id"], {"findings": [], "scope": "fixture only"})
    completed = repo.get_job(job["id"])
    assert completed["status"] == "completed"
    assert completed["result"]["scope"] == "fixture only"
    with pytest.raises(ValueError, match="running job"):
        repo.finish_job(job["id"], {})


def test_expired_job_is_recovered_with_original_payload(repo):
    job = enqueue(repo)
    repo.claim_job()
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            "UPDATE review_jobs SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", job["id"]),
        )
    assert repo.recover_jobs() == 1
    assert repo.recover_jobs() == 0
    recovered = repo.claim_job()
    assert recovered["id"] == job["id"]
    assert recovered["attempts"] == 2
    assert recovered["payload"] == job["payload"]
    repo.fail_job(job["id"], "Model request timed out")
    assert repo.get_job(job["id"])["status"] == "failed"
    assert repo.get_job(job["id"])["error"] == "Model request timed out"
    assert repo.claim_job() is None


def test_stale_worker_cannot_finish_or_fail_a_reclaimed_attempt(repo):
    job = enqueue(repo)
    original = repo.claim_job()
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            "UPDATE review_jobs SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", job["id"]),
        )
    newer = repo.claim_job()
    assert newer["attempts"] == original["attempts"] + 1
    with pytest.raises(ValueError, match="current attempt"):
        repo.finish_job(job["id"], {"obsolete": True}, expected_attempt=original["attempts"])
    with pytest.raises(ValueError, match="current attempt"):
        repo.fail_job(job["id"], "Old failure", expected_attempt=original["attempts"])
    repo.finish_job(job["id"], {"current": True}, expected_attempt=newer["attempts"])
    assert repo.get_job(job["id"])["result"] == {"current": True}


def test_heartbeat_renews_only_the_current_running_attempt(repo):
    job = enqueue(repo)
    assert repo.heartbeat_job(job["id"], expected_attempt=0) is False
    original = repo.claim_job()
    assert repo.heartbeat_job(job["id"], expected_attempt=original["attempts"]) is True
    renewed = repo.get_job(job["id"])
    assert renewed["lease_expires_at"] > original["lease_expires_at"]
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            "UPDATE review_jobs SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", job["id"]),
        )
    current = repo.claim_job()
    assert repo.heartbeat_job(job["id"], expected_attempt=original["attempts"]) is False
    assert repo.get_job(job["id"])["lease_expires_at"] == current["lease_expires_at"]
    assert repo.heartbeat_job(job["id"], expected_attempt=current["attempts"]) is True
    repo.finish_job(job["id"], {}, expected_attempt=current["attempts"])
    assert repo.heartbeat_job(job["id"], expected_attempt=current["attempts"]) is False
    assert repo.heartbeat_job("missing", expected_attempt=1) is False


def test_initialization_preserves_unrelated_tables(repo):
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('keep')")
    repo.initialize()
    with sqlite3.connect(repo.db_path) as connection:
        assert connection.execute("SELECT value FROM unrelated").fetchone()[0] == "keep"
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
