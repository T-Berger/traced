from collections import Counter
from copy import deepcopy

import pytest

from swarm_detector.detector import _build_groups, _prepare, _shuffle_assignments, analyze
from swarm_detector.sample_data import build_sample


def small_dataset(post_count=4, accounts=("a", "b", "c"), one_family=False):
    posts, messages = [], []
    for post in range(post_count):
        key = f"post-{post}"
        posts.append(
            {
                "post_key": key,
                "channel_key": "channel-a",
                "post_family_key": "same-origin" if one_family else key,
            }
        )
        for index, account in enumerate(accounts):
            messages.append(
                {
                    "message_key": f"message-{post}-{index}",
                    "post_key": key,
                    "account_key": account,
                    "sender_type": "user",
                    "timestamp_utc": f"2026-10-14T08:{post * 5:02d}:{index * 10:02d}Z",
                    "text_original": "A deliberately irrelevant opinion.",
                }
            )
    return {
        "dataset_id": "small-synthetic-test",
        "synthetic": True,
        "posts": posts,
        "messages": messages,
    }


@pytest.fixture(scope="module")
def sample_result():
    dataset = build_sample()
    result = analyze(
        dataset, {"randomizations": 8, "swaps_per_slot": 4, "max_attempts_per_slot": 40}
    )
    return dataset, result


def test_repeated_arrivals_and_open_activism_are_candidates(sample_result):
    dataset, result = sample_result
    groups = [set(group["account_keys"]) for group in result["groups"]]
    repeat = {f"syn-repeat-{index:02d}" for index in range(1, 9)}
    volunteers = {f"syn-volunteer-{index:02d}" for index in range(1, 6)}
    assert any(repeat <= members for members in groups)
    assert any(volunteers <= members for members in groups)
    assert result["synthetic"] is True
    assert dataset["manifest"]["synthetic"] is True
    assert all(group["family_count"] >= 3 for group in result["groups"])
    assert result["diagnostics"]["baseline_is_descriptive"] is True


def test_single_post_and_prolific_individual_do_not_create_groups():
    assert analyze(small_dataset(post_count=1), {"randomizations": 2})["groups"] == []
    dataset = small_dataset(accounts=("critic",))
    for original in list(dataset["messages"]):
        for number in range(40):
            message = {**original, "message_key": original["message_key"] + f"-followup-{number}"}
            dataset["messages"].append(message)
    result = analyze(dataset, {"randomizations": 2})
    assert result["groups"] == []
    assert result["diagnostics"]["first_arrivals"] == 4


def test_synthetic_organic_and_critic_controls_remain_outside_candidates(sample_result):
    _, result = sample_result
    members = {account for group in result["groups"] for account in group["account_keys"]}
    assert not any(account.startswith("syn-organic-") for account in members)
    assert "syn-prolific-critic-01" not in members


def test_rewording_and_narrative_labels_cannot_change_detection(sample_result):
    dataset, original = sample_result
    reworded = deepcopy(dataset)
    for message in reworded["messages"]:
        message["text_original"] = "The opposite political opinion, or a quotation of it."
        message["urls"] = ["https://example.invalid/a-new-link"]
        message["narrative"] = "A label that must never affect detection"
    for post in reworded["posts"]:
        post["text_original"] = (
            "Completely reworded parent post with an unchanged supplied family ID."
        )
        post["title"] = "Different opinion"
    for channel in reworded["channels"]:
        channel["category"] = "A different channel label"
    result = analyze(reworded, original["config"])
    assert result["input_fingerprint"] == original["input_fingerprint"]
    assert result["run_id"] != original["run_id"]
    assert result["source_fingerprint"] != original["source_fingerprint"]
    assert result["pairs"] == original["pairs"]
    assert result["groups"] == original["groups"]


def test_duplicate_post_family_cannot_manufacture_recurrence():
    result = analyze(small_dataset(post_count=4, one_family=True), {"randomizations": 2})
    assert result["pairs"] == []
    assert result["groups"] == []


def test_swaps_preserve_counts_and_global_account_post_uniqueness():
    slots = []
    for post, early, late in (
        ("a", "u1", "u2"),
        ("b", "u2", "u3"),
        ("c", "u3", "u4"),
        ("d", "u4", "u1"),
    ):
        for hour, account in (("08", early), ("09", late)):
            slots.append(
                {
                    "post_key": post,
                    "account_key": account,
                    "stratum": ("channel", "2026-10-14T" + hour),
                    "timestamp": f"2026-10-14T{hour}:00:00Z",
                }
            )
    untouched = deepcopy(slots)
    expected_counts = Counter((slot["stratum"], slot["account_key"]) for slot in slots)
    for seed in range(6):
        labels, diagnostics = _shuffle_assignments(slots, seed, swaps_per_slot=5)
        actual_counts = Counter(
            (slot["stratum"], label) for slot, label in zip(slots, labels, strict=True)
        )
        assignments = [(slot["post_key"], label) for slot, label in zip(slots, labels, strict=True)]
        assert actual_counts == expected_counts
        assert len(assignments) == len(set(assignments))
        assert sum(row["accepted_swaps"] for row in diagnostics) > 0
        assert slots == untouched


def test_first_arrival_and_invalid_records_are_explicit():
    dataset = small_dataset(post_count=1, accounts=("a",))
    original = dataset["messages"][0]
    dataset["messages"].extend(
        [
            {**original, "message_key": "later", "timestamp_utc": "2026-10-14T08:10:00Z"},
            {
                **original,
                "message_key": "ambiguous",
                "account_key": "b",
                "timestamp_utc": "2026-10-14T08:00:00",
            },
            {**original, "message_key": "unknown", "account_key": None},
            {**original, "message_key": "missing-post", "post_key": "unresolved"},
        ]
    )
    slots, diagnostics = _prepare(dataset)
    assert len(slots) == 1
    assert slots[0].message_key == original["message_key"]
    assert diagnostics["invalid_timestamp_messages"] == 1
    assert diagnostics["unidentifiable_or_nonuser_messages"] == 1
    assert diagnostics["unresolved_post_messages"] == 1


def test_two_core_does_not_count_a_leaf_as_core_membership():
    pairs = []
    for index, (a, b, support) in enumerate(
        (("a", "b", 20), ("a", "c", 20), ("b", "c", 20), ("c", "leaf", 3))
    ):
        pairs.append(
            {
                "pair_key": f"pair-{index}",
                "account_a": a,
                "account_b": b,
                "retained": True,
                "observed": support,
                "score": 3.0,
                "events": [{"post_key": "post", "family_key": "family"}],
            }
        )
    groups = _build_groups(pairs, synthetic=True)
    assert any(set(group["account_keys"]) == {"a", "b", "c"} for group in groups)
    assert all("leaf" not in group["account_keys"] for group in groups)


def test_dense_post_discovery_omissions_are_reported():
    result = analyze(small_dataset(), {"randomizations": 2, "max_pairs_per_post": 1})
    assert result["groups"] == []
    assert result["diagnostics"]["discovery_omitted_post_count"] == 4
    assert result["diagnostics"]["discovery_omitted_arrivals"] == 12


def test_no_controls_means_no_retained_evidence():
    result = analyze(small_dataset(), {"randomizations": 0})
    assert result["pairs"]
    assert result["groups"] == []
    assert not any(pair["retained"] for pair in result["pairs"])


def test_timestamp_precision_disables_unsupported_windows():
    dataset = small_dataset()
    dataset["manifest"] = {"timestamp_precision_seconds": 60}
    result = analyze(dataset, {"randomizations": 2})
    assert result["diagnostics"]["disabled_sensitivity_windows"] == [30]
    assert all("30" not in pair["sensitivity"] for pair in result["pairs"])
    with pytest.raises(ValueError, match="timestamp precision"):
        analyze(dataset, {"window_seconds": 30})
