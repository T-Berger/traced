import gc
import json
import sqlite3
import tracemalloc
from contextlib import contextmanager
from copy import deepcopy

import pytest

from swarm_detector.ingest import dataset_readiness
from swarm_detector.models import Case, Message, Page
from swarm_detector.service import Workbench


@pytest.fixture
def workbench(tmp_path):
    wb = Workbench(tmp_path / "paged.db")
    wb.ensure_sample()
    return wb


@contextmanager
def forbid_legacy_payload_reads(workbench):
    """Use SQLite's authorizer so even new accidental call paths are caught."""
    original = workbench.store.database.connection

    @contextmanager
    def guarded_connection():
        with original() as conn:

            def authorize(action, table, column, _database, _trigger):
                if (
                    action == sqlite3.SQLITE_READ
                    and table in {"datasets", "runs"}
                    and column == "payload"
                ):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            conn.set_authorizer(authorize)
            yield conn

    workbench.store.database.connection = guarded_connection
    try:
        yield
    finally:
        workbench.store.database.connection = original


def test_interactive_reads_and_review_validation_cannot_read_full_legacy_payloads(workbench):
    case_id = workbench.list_cases()[0]["case_id"]
    expected = workbench.snapshot()
    with forbid_legacy_payload_reads(workbench):
        assert workbench.overview()["message_count"] == len(expected["dataset"]["messages"])
        assert workbench.readiness() == dataset_readiness(expected["dataset"], expected["run"])
        assert workbench.ensure_sample()["run_id"] == expected["run"]["run_id"]
        assert workbench.case_page(limit=2)["returned"] == 2
        detail = workbench.case_detail(case_id, limit=7)
        source_ref = detail["messages"][0]["message_key"]
        saved = workbench.save_review(
            case_id,
            "Reviewer",
            "Insufficient evidence",
            "Need to inspect the original context.",
            claim="An observed claim",
            narrative_notes="Source context was inspected.",
            evidence_refs=[source_ref],
            expected_revision=0,
        )
        assert workbench.get_review(case_id)["revision"] == saved["revision"]
        assert workbench.search("fictional")
        assert "messages" not in detail["dataset"]
        assert "posts" not in detail["dataset"]
        assert "pairs" not in detail["run"]
        # Complete case export also reads normalized case records, not the corpus.
        assert json.loads(workbench.export_case(case_id, "json"))["case"]["case_id"] == case_id
        with pytest.raises(sqlite3.DatabaseError, match="prohibited"):
            workbench.snapshot()


def test_case_pages_have_stable_order_exact_totals_and_independent_offsets(workbench):
    case_id = workbench.list_cases()[0]["case_id"]
    first = workbench.case_detail(case_id, limit=7)
    second = workbench.case_detail(
        case_id,
        limit=7,
        message_offset=7,
        context_offset=7,
        post_offset=7,
        pair_offset=7,
        event_offset=7,
    )
    assert len(first["messages"]) == 7
    assert len(first["pairs"]) == 7
    assert sum(len(pair["events"]) for pair in first["pairs"]) <= 7
    assert first["pagination"]["messages"]["has_more"]
    assert first["pagination"]["messages"]["total"] == second["pagination"]["messages"]["total"]
    assert {m["message_key"] for m in first["messages"]}.isdisjoint(
        m["message_key"] for m in second["messages"]
    )
    assert first["concentration"] == second["concentration"]
    assert first["sensitivity"] == second["sensitivity"]
    assert first["stress_test"] == second["stress_test"]
    complete = workbench.materialize_case(case_id)
    assert complete["complete"]
    assert first["messages"] == complete["messages"][:7]
    assert second["messages"] == complete["messages"][7:14]
    assert complete["concentration"]["group_comments"] == len(complete["messages"])


def test_pair_event_budget_is_global_and_complete_export_keeps_all_references(workbench):
    case_id = workbench.list_cases()[0]["case_id"]
    detail = workbench.case_detail(case_id)
    assert sum(len(pair["events"]) for pair in detail["pairs"]) <= 200
    assert detail["pagination"]["events"]["total"] > 200
    assert detail["pagination"]["events"]["has_more"]
    complete = workbench.materialize_case(case_id)
    assert (
        sum(len(pair["events"]) for pair in complete["pairs"])
        == detail["pagination"]["events"]["total"]
    )
    refs = {message["message_key"] for message in complete["messages"]}
    assert all(
        event[key] in refs
        for pair in complete["pairs"]
        for event in pair["events"]
        for key in ("message_a", "message_b")
    )
    pair_key = detail["pairs"][0]["pair_key"]
    event_page = workbench.pair_events(case_id, pair_key, limit=2, offset=1)
    assert event_page["returned"] == 2
    with pytest.raises(ValueError, match="selected case"):
        workbench.pair_events(case_id, "unrelated-pair")


@pytest.mark.parametrize("limit,offset", [(501, 0), (0, 0), (True, 0), (2, -1), (2, True)])
def test_invalid_page_bounds_are_rejected(workbench, limit, offset):
    with pytest.raises(ValueError):
        workbench.case_page(limit=limit, offset=offset)
    with pytest.raises(ValueError):
        workbench.case_detail(
            workbench.list_cases()[0]["case_id"], limit=limit, message_offset=offset
        )


def test_repositories_return_validated_domain_records(workbench):
    run = workbench.store.runs.info()
    page = workbench.store.cases.page(run.run_id, limit=2)
    assert isinstance(page, Page)
    assert isinstance(page.items[0], Case)
    sources = workbench.store.cases.messages(
        run.run_id, page.items[0].case_id, run.dataset_id, limit=2
    )
    assert isinstance(sources.items[0], Message)


def _peak_read_memory(workbench, case_id):
    workbench.case_detail(case_id)  # Warm imports and Pydantic's model machinery.
    gc.collect()
    tracemalloc.start()
    try:
        detail = workbench.case_detail(case_id)
        _, peak = tracemalloc.get_traced_memory()
        assert len(detail["messages"]) <= 200
        assert len(detail["context_messages"]) <= 200
        return peak
    finally:
        tracemalloc.stop()


def test_case_read_memory_does_not_scale_with_unrelated_corpus(workbench):
    case_id = workbench.list_cases()[0]["case_id"]
    small_peak = _peak_read_memory(workbench, case_id)
    dataset = workbench.store.get_dataset()
    dataset["dataset_id"] = "large-synthetic-corpus"
    extra_post = deepcopy(dataset["posts"][0])
    extra_post.update(post_key="unrelated-post", post_family_key="unrelated-family")
    dataset["posts"].append(extra_post)
    original = dataset["messages"][0]
    for index in range(20_000):
        dataset["messages"].append(
            {
                **original,
                "message_key": f"unrelated-{index}",
                "post_key": "unrelated-post",
                "account_key": "unrelated-account",
                "text_original": "Unrelated source text. " * 12,
                "urls": [],
            }
        )
    workbench.store.put_dataset(dataset)
    workbench.run_analysis({"randomizations": 2})
    assert workbench.overview()["message_count"] == 21_137
    large_peak = _peak_read_memory(workbench, case_id)
    print(
        f"case_detail traced Python peak: 1,137 rows={small_peak:,} bytes; "
        f"21,137 rows={large_peak:,} bytes"
    )
    assert large_peak < small_peak * 2 + 128_000
    with forbid_legacy_payload_reads(workbench):
        # Guard the full source size before materializing a sensitivity rerun.
        result = workbench.stress_test(case_id)
        assert result["available"] is False
        assert "10,000" in result["note"]
