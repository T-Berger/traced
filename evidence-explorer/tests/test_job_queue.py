"""Concurrent consumers must never claim the same persistent job twice."""

from concurrent.futures import ThreadPoolExecutor

import pytest


def test_concurrent_claims_are_unique_and_share_the_repository_queue(repo):
    expected = {repo.enqueue_job("analyze", {"article_ids": [index]}) for index in range(12)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = list(pool.map(lambda _: repo.jobs.claim_job(), range(16)))
    claimed = [job["id"] for job in jobs if job is not None]
    assert len(claimed) == len(set(claimed)) == len(expected)
    assert set(claimed) == expected
    assert all(repo.get_job(job_id)["attempts"] == 1 for job_id in expected)


def test_unserializable_result_leaves_running_job_intact(repo):
    job_id = repo.jobs.enqueue_job("analyze", {"mode": "demo"})
    repo.claim_job()
    with pytest.raises(ValueError):
        repo.jobs.update_job(job_id, status="succeeded", result={"value": float("nan")})
    assert repo.get_job(job_id)["status"] == "running"
    assert repo.get_job(job_id)["finished_at"] is None


def test_queue_objects_are_validated_before_any_write(repo):
    with pytest.raises(ValueError, match="JSON object"):
        repo.enqueue_job("analyze", [])
    assert repo.stats()["jobs"] == 0
    job_id = repo.enqueue_job("analyze", {"mode": "demo"})
    repo.claim_job()
    with pytest.raises(ValueError, match="JSON object"):
        repo.update_job(job_id, status="succeeded", result=[])
    assert repo.get_job(job_id)["status"] == "running"


@pytest.mark.parametrize("encoded", ["[]", "null", '"string"', "{invalid"])
def test_malformed_legacy_job_is_failed_preserving_input_and_next_job_is_claimed(repo, encoded):
    bad = repo.enqueue_job("analyze", {"mode": "demo"})
    good = repo.enqueue_job("analyze", {"mode": "demo"})
    with repo._connection(write=True) as connection:
        connection.execute("UPDATE jobs SET payload=? WHERE id=?", (encoded, bad))
    claimed = repo.jobs.claim_record()
    assert claimed.id == good
    failed = repo.get_job(bad)
    assert failed["status"] == "failed"
    assert failed["error"]
    assert failed["payload"]["invalid_persisted_payload_json"] == encoded
    assert len(repo.list_jobs()) == 2
