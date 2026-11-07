import json

import pytest
from plotly.utils import PlotlyJSONEncoder

from swarm_detector.presentation import case_view, evidence_view


@pytest.fixture
def detail():
    """A complete, small view input that needs no workbench or database."""
    return {
        "case": {
            "case_id": "case-a",
            "name": "Candidate A",
            "kind": "candidate",
            "account_keys": ["account-a", "account-b"],
            "family_count": 1,
        },
        "accounts": [{"account_key": "account-a"}, {"account_key": "account-b"}],
        "posts": [
            {
                "post_key": "post-a",
                "timestamp_utc": "2026-10-14T08:00:00Z",
                "text_original": "PARENT_SOURCE_TEXT",
                "source_ref": "input:post-a",
            }
        ],
        "messages": [
            {
                "message_key": f"message-{account}",
                "account_key": f"account-{account}",
                "post_key": "post-a",
                "channel_key": "channel-a",
                "timestamp_utc": timestamp,
                "text_original": f"COMMENT_SOURCE_TEXT_{account}",
                "source_ref": f"input:message-{account}",
            }
            for account, timestamp in (
                ("a", "2026-10-14T08:00:30Z"),
                ("b", "2026-10-14T08:01:00Z"),
            )
        ],
        "dataset": {"channels": [{"channel_key": "channel-a", "name": "Channel A"}]},
        "run": {"run_id": "run-a", "config": {"window_seconds": 120}, "diagnostics": {}},
        "pairs": [
            {
                "pair_key": "pair-a",
                "account_a": "account-a",
                "account_b": "account-b",
                "retained": True,
                "observed": 1,
                "null_mean": 0,
                "null_p95": 0,
                "events": [],
            }
        ],
        "content_matches": {
            "links": [{"url": "https://example.invalid/SHARED_SOURCE_URL", "accounts": 2}],
            "texts": [{"text": "REPEATED_SOURCE_TEXT", "accounts": 2}],
        },
    }


def test_populated_case_view_renders_first_arrivals_in_minutes(detail):
    outputs = case_view(detail, {})

    assert len(outputs) == 13
    timing_matrix = outputs[2]
    assert list(timing_matrix.data[0].z[0]) == [0.5]
    assert list(timing_matrix.data[0].z[1]) == [1.0]
    assert outputs[7] == "post-a"
    assert outputs[4][0]["data"]["id"] == "account-a"
    assert len(outputs[10].data) == 1


@pytest.mark.parametrize("reveal", [[], ["reveal"]])
def test_source_language_and_shared_content_obey_explicit_reveal(detail, reveal):
    outputs = evidence_view(detail, "post-a", reveal, {}, 100)
    encoded = json.dumps(outputs, cls=PlotlyJSONEncoder)

    assert len(outputs) == 9
    for source_text in (
        "PARENT_SOURCE_TEXT",
        "COMMENT_SOURCE_TEXT_a",
        "COMMENT_SOURCE_TEXT_b",
        "REPEATED_SOURCE_TEXT",
        "SHARED_SOURCE_URL",
    ):
        assert (source_text in encoded) == bool(reveal)
    assert "input:message-a" in encoded
    assert "account-a" in encoded


def test_case_load_error_remains_visible_without_case_data():
    outputs = case_view({"error": "Selected evidence could not be loaded"}, {})

    assert len(outputs) == 13
    assert outputs[0].children == "Selected evidence could not be loaded"
    assert outputs[7] is None


@pytest.mark.parametrize("loaded_count", [20, 25])
def test_pair_event_table_discloses_its_display_limit(detail, loaded_count):
    detail["pairs"][0]["events"] = [
        {
            "post_key": f"limited-post-{index:03d}",
            "family_key": f"family-{index}",
            "delta_seconds": 12,
            "timestamp_a": "2026-10-14T08:00:30Z",
        }
        for index in range(loaded_count)
    ]
    outputs = evidence_view(detail, "post-a", [], {"pair_key": "pair-a"}, 100)
    pair_evidence = json.dumps(outputs[6], cls=PlotlyJSONEncoder)

    assert "limited-post-019" in pair_evidence
    assert "limited-post-020" not in pair_evidence
    assert (f"Showing first 20 of {loaded_count} loaded events." in pair_evidence) == (
        loaded_count > 20
    )
