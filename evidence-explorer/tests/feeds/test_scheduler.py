"""Durable catch-up, bounded scheduling, outcome reconciliation, and backoff."""

from datetime import UTC, datetime, timedelta

from evidence_explorer.db import Repository
from evidence_explorer.feeds import FeedSubscriptionInput, tick_feeds

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)


def add_feed(repo, index=0, **changes):
    return repo.feeds.save(
        FeedSubscriptionInput.model_validate(
            {
                "name": f"Source {index}",
                "url": f"https://news.example/feed/{index}",
                "analyze": False,
                **changes,
            }
        ),
        now=NOW,
    )


def finish(repo, job_id, stamp, *, failed=False, result=None):
    repo.update_job(
        job_id,
        status="failed" if failed else "succeeded",
        error="HTTP 503" if failed else None,
        result=result or {},
    )
    with repo._connection(write=True) as connection:
        connection.execute(
            "UPDATE jobs SET finished_at=?,updated_at=? WHERE id=?",
            (stamp.isoformat(), stamp.isoformat(), job_id),
        )


def test_restart_catches_up_once_without_backlog_or_overlap(repo):
    feed = add_feed(repo)
    restarted = Repository(repo.db_path)
    restarted.initialize()
    late = NOW + timedelta(days=3)
    result = tick_feeds(restarted, now=late)
    assert len(result.queued_job_ids) == 1
    assert restarted.feeds.get(feed.id).next_refresh_at == (late + timedelta(hours=1)).isoformat()
    assert not tick_feeds(restarted, now=late + timedelta(days=2)).queued_job_ids
    assert restarted.stats()["jobs"] == 1


def test_failure_backoff_reconciles_once_and_caps_at_one_day(repo):
    feed = add_feed(repo)
    now = NOW
    job_id = tick_feeds(repo, now=now).queued_job_ids[0]
    for failures in range(1, 8):
        finish(repo, job_id, now, failed=True)
        result = tick_feeds(repo, now=now)
        record = repo.feeds.get(feed.id)
        delay = min(60 * 2 ** (failures - 1), 1440)
        assert result.reconciled == 1
        assert record.consecutive_failures == failures
        assert record.last_error == "HTTP 503"
        assert record.next_refresh_at == (now + timedelta(minutes=delay)).isoformat()
        assert tick_feeds(repo, now=now).reconciled == 0
        assert not tick_feeds(repo, now=now + timedelta(minutes=delay, seconds=-1)).queued_job_ids
        now += timedelta(minutes=delay)
        job_id = tick_feeds(repo, now=now).queued_job_ids[0]
    finish(repo, job_id, now)
    tick_feeds(repo, now=now)
    assert repo.feeds.get(feed.id).consecutive_failures == 0
    assert repo.feeds.get(feed.id).last_error is None


def test_terminal_results_and_analysis_link_survive_worker_restart(repo):
    feed = add_feed(repo)
    job_id = tick_feeds(repo, now=NOW).queued_job_ids[0]
    analysis_id = repo.enqueue_job("analyze", {"mode": "demo"})
    result = {"imported": 1, "analysis_job_id": analysis_id, "errors": []}
    finish(repo, job_id, NOW, result=result)
    reopened = Repository(repo.db_path)
    reopened.initialize()
    assert tick_feeds(reopened, now=NOW).reconciled == 1
    record = reopened.feeds.get(feed.id)
    assert record.last_result == result
    assert record.last_analysis_job_id == analysis_id
    assert record.last_refreshed_at == NOW.isoformat()
    assert record.last_analysis_status == "queued"
    reopened.update_job(analysis_id, status="failed", error="Local model is unavailable")
    assert reopened.feeds.get(feed.id).last_analysis_status == "failed"
    assert reopened.feeds.list_page()[0].last_analysis_error == "Local model is unavailable"


def test_failure_backoff_never_accelerates_a_weekly_feed(repo):
    feed = add_feed(repo, interval_minutes=10080)
    job_id = tick_feeds(repo, now=NOW).queued_job_ids[0]
    finish(repo, job_id, NOW, failed=True)
    tick_feeds(repo, now=NOW)
    record = repo.feeds.get(feed.id)
    assert record.next_refresh_at == (NOW + timedelta(days=7)).isoformat()
    assert not tick_feeds(repo, now=NOW + timedelta(days=1)).queued_job_ids


def test_partial_success_keeps_error_summary_without_failure_backoff(repo):
    feed = add_feed(repo)
    job_id = tick_feeds(repo, now=NOW).queued_job_ids[0]
    finish(repo, job_id, NOW, result={"imported": 1, "errors": [{"error": "Paywall"}]})
    tick_feeds(repo, now=NOW)
    record = repo.feeds.get(feed.id)
    assert record.consecutive_failures == 0
    assert "Paywall" in record.last_error
    assert record.next_refresh_at == (NOW + timedelta(hours=1)).isoformat()


def test_tick_queue_cap_and_eventual_reconciliation_fairness(repo):
    feeds = [add_feed(repo, index) for index in range(125)]
    jobs = [repo.feeds.queue_one(feed.id, now=NOW) for feed in feeds]
    for feed, job_id in zip(feeds, jobs, strict=True):
        repo.feeds.set_enabled(feed.id, False, now=NOW)
        finish(repo, job_id, NOW)
    assert tick_feeds(repo, now=NOW, limit=0).reconciled == 100
    assert tick_feeds(repo, now=NOW, limit=0).reconciled == 25
    assert tick_feeds(repo, now=NOW, limit=0).reconciled == 0
    for feed in feeds[:25]:
        repo.feeds.set_enabled(feed.id, True, now=NOW)
    assert len(tick_feeds(repo, now=NOW, limit=1000).queued_job_ids) == 20
    assert len(tick_feeds(repo, now=NOW).queued_job_ids) == 5


def test_interrupted_job_recovery_needs_no_worker_completion_hook(repo):
    feed = add_feed(repo)
    tick_feeds(repo, now=NOW)
    assert repo.jobs.claim_record() is not None
    assert repo.recover_interrupted_jobs() == 1
    tick_feeds(repo, now=NOW)
    assert repo.feeds.get(feed.id).consecutive_failures == 1


def test_paused_scheduled_skip_does_not_fake_a_successful_refresh(repo):
    feed = add_feed(repo)
    job_id = tick_feeds(repo, now=NOW).queued_job_ids[0]
    repo.feeds.set_enabled(feed.id, False, now=NOW)
    finish(repo, job_id, NOW, result={"skipped": True, "skip_reason": "paused", "errors": []})
    tick_feeds(repo, now=NOW)
    record = repo.feeds.get(feed.id)
    assert record.last_finished_at == NOW.isoformat()
    assert record.last_refreshed_at is None
    assert record.consecutive_failures == 0
    assert record.enabled is False
