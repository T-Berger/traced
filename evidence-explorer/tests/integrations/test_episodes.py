"""Episode selection counts distinct pairs per family, without rerunning detection."""

from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.integrations.episodes import strongest_episode_record
from evidence_explorer.review.integrations.swarm import _digest


def _bundle(*, tie=False):
    posts = [
        {"post_key": "post-a", "post_family_key": "family-a", "text_original": "Original A"},
        {"post_key": "post-a-copy", "post_family_key": "family-a", "text_original": "Copy A"},
        {"post_key": "post-b", "post_family_key": "family-b", "text_original": "Original B"},
    ]
    dataset = {"dataset_id": "episode-fixture", "posts": posts, "messages": [], "channels": []}
    pairs = [
        {
            "pair_key": "pair-1",
            "account_a": "a",
            "account_b": "b",
            "events": [
                {"post_key": "post-a", "family_key": "family-a"},
                {"post_key": "post-a-copy", "family_key": "family-a"},
                {"post_key": "post-a-copy", "family_key": "family-a"},
            ],
        },
        {
            "pair_key": "pair-2",
            "account_a": "c",
            "account_b": "d",
            "events": [
                {"post_key": "post-b", "family_key": "family-b"},
            ],
        },
    ]
    if not tie:
        pairs.append(
            {
                "pair_key": "pair-3",
                "account_a": "e",
                "account_b": "f",
                "events": [
                    {"post_key": "post-b", "family_key": "family-b"},
                ],
            }
        )
    records = [
        {
            "id": f"record:{post['post_key']}",
            "kind": "telegram_post",
            "source_label": post["post_key"],
            "text": post["text_original"],
            "content_hash": hashlib.sha256(post["text_original"].encode()).hexdigest(),
            "metadata": {"swarm": post},
        }
        for post in posts
    ]
    fingerprint = _digest(dataset)
    return EvidenceBundle.model_validate(
        {
            "case": {
                "id": "case",
                "title": "Synthetic episode ranking",
                "question": "Which family?",
                "synthetic": True,
            },
            "records": records,
            "fragments": [],
            "runs": [
                {
                    "id": "baseline",
                    "input_fingerprint": fingerprint,
                    "input_manifest": [record["id"] for record in records],
                    "method": "fixture",
                    "method_version": "1",
                    "created_at": "2026-11-06T00:00:00Z",
                    "full_inputs_available": True,
                    "configuration": {"seed": 42},
                    "swarm_result": {"source_fingerprint": fingerprint, "config": {"seed": 42}},
                }
            ],
            "findings": [
                {
                    "snapshot_id": "finding",
                    "lineage_id": "group",
                    "version": 1,
                    "run_id": "baseline",
                    "kind": "observation",
                    "statement": "Fixture finding",
                    "details": {"domain": "swarm", "swarm_pairs": pairs},
                }
            ],
            "swarm_context": {
                "dataset": dataset,
                "dataset_fingerprint": fingerprint,
                "baseline_run_id": "baseline",
            },
        }
    )


def test_selects_family_with_more_distinct_pair_support_instead_of_more_raw_events():
    bundle = _bundle()
    before = bundle.fingerprint()
    selected = strongest_episode_record(bundle, "finding")
    assert selected.id == "record:post-b"
    assert selected is next(record for record in bundle.records if record.id == selected.id)
    assert selected.metadata["swarm"]["post_family_key"] == "family-b"
    assert bundle.fingerprint() == before


def test_family_tie_and_representative_post_are_deterministic_under_input_reordering():
    bundle = _bundle(tie=True)
    selected = strongest_episode_record(bundle, "finding")
    assert selected.id == "record:post-a"
    data = bundle.model_dump(mode="json")
    data["records"].reverse()
    data["findings"][0]["details"]["swarm_pairs"].reverse()
    for pair in data["findings"][0]["details"]["swarm_pairs"]:
        pair["events"].reverse()
    assert (
        strongest_episode_record(EvidenceBundle.model_validate(data), "finding").id == selected.id
    )


def test_same_accounts_with_reversed_order_and_different_pair_ids_do_not_add_support():
    data = _bundle().model_dump(mode="json")
    pairs = data["findings"][0]["details"]["swarm_pairs"]
    extra = deepcopy(pairs[0])
    extra.update({"pair_key": "another-id", "account_a": "b", "account_b": "a"})
    pairs.append(extra)
    assert (
        strongest_episode_record(EvidenceBundle.model_validate(data), "finding").id
        == "record:post-b"
    )


def test_unknown_or_non_swarm_finding_is_rejected():
    bundle = _bundle()
    with pytest.raises(ValueError, match="Unknown finding"):
        strongest_episode_record(bundle, "missing")
    data = bundle.model_dump(mode="json")
    data["findings"][0]["details"]["domain"] = "historical_media"
    with pytest.raises(ValueError, match="Swarm finding"):
        strongest_episode_record(EvidenceBundle.model_validate(data), "finding")


@pytest.mark.parametrize(
    "change", ["missing_context", "missing_dataset", "not_full", "changed_inputs"]
)
def test_complete_unchanged_baseline_inputs_are_required(change):
    data = _bundle().model_dump(mode="json")
    if change == "missing_context":
        data.pop("swarm_context")
    elif change == "missing_dataset":
        data["swarm_context"].pop("dataset")
    elif change == "not_full":
        data["runs"][0]["full_inputs_available"] = False
    else:
        data["swarm_context"]["dataset"]["posts"][0]["text_original"] = "Changed input"
    with pytest.raises(ValueError, match="context|inputs"):
        strongest_episode_record(EvidenceBundle.model_validate(data), "finding")


def test_result_run_finding_cannot_be_mistaken_for_the_original_baseline():
    data = _bundle().model_dump(mode="json")
    result = deepcopy(data["runs"][0])
    result["id"] = "result-run"
    data["runs"].append(result)
    data["findings"][0]["run_id"] = "result-run"
    with pytest.raises(ValueError, match="original baseline"):
        strongest_episode_record(EvidenceBundle.model_validate(data), "finding")


def test_unresolved_or_conflicting_pair_event_post_context_is_rejected():
    data = _bundle().model_dump(mode="json")
    event = data["findings"][0]["details"]["swarm_pairs"][0]["events"][0]
    event["post_key"] = "missing-post"
    with pytest.raises(ValueError, match="unavailable baseline parent"):
        strongest_episode_record(EvidenceBundle.model_validate(data), "finding")
    event["post_key"] = "post-a"
    event["family_key"] = "incorrect-family"
    with pytest.raises(ValueError, match="does not match"):
        strongest_episode_record(EvidenceBundle.model_validate(data), "finding")


def test_missing_parent_record_or_event_support_cannot_choose_an_unrelated_source():
    data = _bundle().model_dump(mode="json")
    data["records"] = [record for record in data["records"] if record["id"] != "record:post-b"]
    data["runs"][0]["input_manifest"] = [record["id"] for record in data["records"]]
    with pytest.raises(ValueError, match="original supplied parent-post"):
        strongest_episode_record(EvidenceBundle.model_validate(data), "finding")
    data["findings"][0]["details"]["swarm_pairs"] = []
    with pytest.raises(ValueError, match="No documented pair-event"):
        strongest_episode_record(EvidenceBundle.model_validate(data), "finding")
