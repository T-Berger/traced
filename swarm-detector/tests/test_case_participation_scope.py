"""Participation counts and account timelines have deliberately different scopes."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from swarm_detector.sample_data import build_sample
from swarm_detector.service import Workbench


def test_off_case_activity_does_not_inflate_thread_share_or_break_export(tmp_path):
    dataset = build_sample()
    channel_key, post_key = "syn-channel-z-off-case", "syn-post-off-case"
    dataset["channels"].append(
        {
            "channel_key": channel_key,
            "name": "Synthetic unrelated discussion channel",
            "category": "other",
            "rationale": "Explicitly documented synthetic category for this regression.",
        }
    )
    origin = datetime(2026, 10, 20, 15, tzinfo=UTC)
    dataset["posts"].append(
        {
            "post_key": post_key,
            "post_family_key": post_key,
            "channel_key": channel_key,
            "timestamp_utc": origin.isoformat(),
            "title": "[Synthetic] Unrelated off-case discussion",
            "text_original": "Synthetic activity outside the repeated-arrival case threads.",
            "source_ref": "synthetic://posts/" + post_key,
        }
    )
    off_case_keys = set()
    for index in range(60):
        message_key = f"syn-off-case-message-{index:03d}"
        off_case_keys.add(message_key)
        dataset["messages"].append(
            {
                "message_key": message_key,
                "post_key": post_key,
                "account_key": "syn-repeat-01",
                "channel_key": channel_key,
                "timestamp_utc": (origin + timedelta(seconds=index + 1)).isoformat(),
                "text_original": "[Synthetic] Independent off-case activity.",
                "urls": [],
                "source_ref": "synthetic://messages/" + message_key,
                "sender_type": "user",
            }
        )
    workbench = Workbench(tmp_path / "participation-scope.db")
    workbench.store.put_dataset(dataset)
    run = workbench.run_analysis(dataset=dataset)
    case = next(group for group in run["groups"] if "syn-repeat-01" in group["account_keys"])

    detail = workbench.case_detail(case["case_id"], run_id=run["run_id"], limit=20)
    assert detail["concentration"]["group_comments"] == 72
    assert detail["concentration"]["thread_comments"] == 117
    assert detail["concentration"]["share"] == pytest.approx(72 / 117)
    assert detail["pagination"]["messages"]["total"] == 132
    assert detail["pagination"]["messages"]["has_more"] is True
    assert len(detail["messages"]) == 20
    assert len(detail["context_messages"]) == 20
    assert detail["pagination"]["context_messages"]["total"] == 177
    assert (
        next(
            channel
            for channel in detail["dataset"]["channels"]
            if channel["channel_key"] == channel_key
        )["category"]
        == "other"
    )

    complete = workbench.materialize_case(case["case_id"], run_id=run["run_id"])
    assert complete["complete"] is True
    assert off_case_keys <= {message["message_key"] for message in complete["messages"]}
    assert len(complete["messages"]) == 132
    assert len(complete["context_messages"]) == 177
    assert complete["concentration"] == detail["concentration"]

    exported = json.loads(workbench.export_case(case["case_id"], "json", run_id=run["run_id"]))
    assert off_case_keys <= {message["message_key"] for message in exported["messages"]}
    assert exported["concentration"] == detail["concentration"]
    assert "72 of 117 observed comments" in workbench.export_case(
        case["case_id"], run_id=run["run_id"]
    )
