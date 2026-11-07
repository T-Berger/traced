import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest

_script = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_sample.py"
_spec = importlib.util.spec_from_file_location("sample_evaluation_harness", _script)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Unable to load the standalone sample evaluation harness.")
_harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)


def test_harness_uses_temporary_storage_and_does_not_claim_human_review(monkeypatch, tmp_path):
    working_db = tmp_path / "do-not-open.db"
    working_db.write_bytes(b"This sentinel is not a SQLite database.")
    monkeypatch.setenv("SWARM_DB_PATH", str(working_db))
    report, packet = _harness.evaluate_sample(controls=4)
    assert report["all_checks_passed"], report["checks"]
    assert working_db.read_bytes() == b"This sentinel is not a SQLite database."
    assert report["working_database_opened"] is False
    assert report["real_archive_accessed"] is False
    assert report["human_review"]["completed_reviews"] == 0
    assert report["human_review"]["independent_reviewers"] == []
    assert report["real_world_accuracy"]["measured"] is False
    assert "NOT HUMAN-REVIEWED" in packet
    assert all(case["review_status"] == "not_human_reviewed" for case in report["cases"])


def test_evidence_checker_rejects_a_dangling_message_reference():
    dataset = {
        "channels": [{"channel_key": "c"}],
        "posts": [
            {
                "post_key": "p",
                "channel_key": "c",
                "post_family_key": "f",
                "source_ref": "synthetic://p",
            }
        ],
        "messages": [],
    }
    run = {
        "config": {"window_seconds": 120},
        "pairs": [
            {
                "account_a": "a",
                "account_b": "b",
                "events": [
                    {
                        "post_key": "p",
                        "family_key": "f",
                        "message_a": "missing",
                        "message_b": "also-missing",
                    }
                ],
            }
        ],
    }
    before = deepcopy(dataset)
    with pytest.raises(AssertionError, match="does not resolve"):
        _harness.verify_evidence_references(dataset, run)
    assert dataset == before


def test_thread_share_checks_selected_threads_and_preserves_broader_activity():
    inside = {"message_key": "inside", "post_key": "selected", "account_key": "core"}
    outside = {"message_key": "outside", "post_key": "elsewhere", "account_key": "core"}
    ordinary = {"message_key": "ordinary", "post_key": "selected", "account_key": "other"}
    detail = {
        "case": {"account_keys": ["core"], "post_keys": ["selected"]},
        "posts": [{"post_key": "selected"}, {"post_key": "elsewhere"}],
        "messages": [inside, outside],
        "context_messages": [inside, ordinary, outside],
        "concentration": {"group_comments": 1, "thread_comments": 2, "share": 0.5},
    }
    result = _harness.verify_thread_share(detail)
    assert result["broader_core_activity_comments"] == 2
    assert result["core_comments_in_selected_threads"] == 1
    detail["concentration"] = {"group_comments": 2, "thread_comments": 2, "share": 1}
    with pytest.raises(AssertionError, match="selected-post scope"):
        _harness.verify_thread_share(detail)
