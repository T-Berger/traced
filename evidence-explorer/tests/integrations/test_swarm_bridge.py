"""Behavioral integration checks against the unchanged Swarm detector."""

from __future__ import annotations

from copy import deepcopy

import pytest

from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.integrations.swarm import (
    _correspondences,
    build_swarm_bundle,
    recompute_swarm_scenario,
)

swarm_detector = pytest.importorskip("swarm_detector.detector")
sample_data = pytest.importorskip("swarm_detector.sample_data")


@pytest.fixture(scope="module")
def source():
    dataset = sample_data.build_sample()
    run = swarm_detector.analyze(dataset, {"randomizations": 5, "seed": 42})
    return dataset, run


@pytest.fixture(scope="module")
def baseline(source):
    return build_swarm_bundle(*source)


def test_bundle_preserves_actual_detector_results_and_complete_context(source, baseline):
    dataset, run = source
    payload = baseline.model_dump(mode="json")
    assert payload["swarm_context"]["dataset"] == dataset
    assert payload["runs"][0]["swarm_result"] == run
    assert payload["runs"][0]["configuration"] == run["config"]
    assert payload["case"]["synthetic"] is True
    assert payload["runs"][0]["full_inputs_available"] is True
    assert len(payload["records"]) == sum(
        len(dataset[key]) for key in ("channels", "posts", "messages")
    )
    assert len(payload["runs"][0]["input_manifest"]) == len(payload["records"])
    assert all(f["details"]["behavior_uses_text"] is False for f in payload["findings"])
    assert sorted(
        f["details"]["swarm_group"]["account_keys"] for f in payload["findings"]
    ) == sorted(g["account_keys"] for g in run["groups"])


def test_exact_evidence_links_and_control_context_survive(baseline):
    payload = baseline.model_dump(mode="json")
    records = {r["id"]: r for r in payload["records"]}
    fragments = {f["id"]: f for f in payload["fragments"]}
    for link in payload["links"]:
        if link["relation"] == "supports":
            fragment = fragments[link["source"]["id"]]
            record = records[fragment["record_id"]]
            assert fragment["quote"] == record["text"]
            assert record["metadata"]["swarm"]["timestamp_utc"]
    members = {a for f in payload["findings"] for a in f["details"]["swarm_group"]["account_keys"]}
    assert not any(a.startswith("syn-organic-") for a in members)
    assert "syn-prolific-critic-01" not in members
    volunteers = [
        f
        for f in payload["findings"]
        if "syn-volunteer-01" in f["details"]["swarm_group"]["account_keys"]
    ]
    assert volunteers, "Transparent public volunteers can be behaviorally coordinated"
    assert volunteers[0]["details"]["context_record_ids"]
    assert "transparent human explanation" in " ".join(volunteers[0]["details"]["counterevidence"])
    assert all(link["relation"] != "documented_repost" for link in payload["links"])
    assert EvidenceBundle.model_validate_json(baseline.model_dump_json()) == baseline


def test_mismatched_source_run_is_rejected(source):
    dataset, run = source
    altered = deepcopy(dataset)
    altered["messages"][0]["text_original"] += " Different source content."
    with pytest.raises(ValueError, match="does not match"):
        build_swarm_bundle(altered, run)


def test_scenario_retains_background_and_settings_and_preserves_history(baseline, source):
    dataset, _ = source
    original = baseline.model_dump(mode="json")
    base_finding = original["findings"][0]
    payload = deepcopy(original)
    payload["reviews"] = [
        {
            "id": "test:review-baseline",
            "finding_snapshot_id": base_finding["snapshot_id"],
            "run_id": base_finding["run_id"],
            "reviewer": "Fixture reviewer",
            "disposition": "qualify",
            "reason": "Observed timing alone does not establish intent.",
            "timestamp": "2026-11-06T08:00:00Z",
        }
    ]
    reviewed = EvidenceBundle.model_validate(payload)
    reviewed_before = reviewed.model_dump(mode="json")
    target = next(
        r
        for r in reviewed.records
        if r.kind == "telegram_post" and r.metadata["swarm"]["post_key"] == "syn-episode-01"
    )
    result = recompute_swarm_scenario(reviewed, [target.id], "Check one episode's influence")
    result_payload = result.model_dump(mode="json")
    scenario = result_payload["scenarios"][-1]
    result_run = next(r for r in result_payload["runs"] if r["id"] == scenario["result_run_id"])
    source_run = next(r for r in result_payload["runs"] if r["id"] == scenario["base_run_id"])
    assert result_run["configuration"] == source_run["configuration"]
    assert result_payload["case"] == original["case"]
    assert result_payload["swarm_context"] == original["swarm_context"]
    assert len(result_run["input_manifest"]) < len(source_run["input_manifest"])
    filtered = deepcopy(dataset)
    filtered["posts"] = [p for p in dataset["posts"] if p["post_key"] != "syn-episode-01"]
    filtered["messages"] = [m for m in dataset["messages"] if m["post_key"] != "syn-episode-01"]
    actual = result_run["swarm_result"]
    expected = swarm_detector.analyze(filtered, source_run["configuration"])
    assert actual["input_fingerprint"] == expected["input_fingerprint"]
    assert actual["source_fingerprint"] == expected["source_fingerprint"]
    assert actual["pairs"] == expected["pairs"]
    assert any(
        r["metadata"]["swarm"].get("post_key", "").startswith("syn-background-post-")
        for r in result_payload["records"]
        if r["id"] in result_run["input_manifest"]
    )
    assert result_payload["reviews"] == reviewed.model_dump(mode="json")["reviews"]
    assert len(result_payload["reviews"]) == 1
    assert result_payload["reviews"][0]["run_id"] != result_run["id"]
    assert reviewed.model_dump(mode="json") == reviewed_before
    assert baseline.model_dump(mode="json") == original
    assert scenario["correspondences"]
    assert EvidenceBundle.model_validate_json(result.model_dump_json()) == result
    assert recompute_swarm_scenario(result, [target.id], "Check one episode's influence") == result


def test_post_family_exclusion_expands_all_documented_family_members(source):
    dataset, _ = source
    dataset = deepcopy(dataset)
    # A documented family can contain multiple parent posts. This metadata does
    # not assert a common source for their text; it scopes the sensitivity test.
    for post in dataset["posts"]:
        if post["post_key"] in {"syn-episode-01", "syn-episode-02"}:
            post["post_family_key"] = "test-shared-post-family"
    run = swarm_detector.analyze(dataset, {"randomizations": 3})
    bundle = build_swarm_bundle(dataset, run)
    target = next(
        r
        for r in bundle.records
        if r.kind == "telegram_post" and r.metadata["swarm"]["post_key"] == "syn-episode-01"
    )
    result = recompute_swarm_scenario(bundle, [target.id], "Remove a complete documented family")
    scenario = result.scenarios[-1]
    excluded = {
        r.metadata["swarm"]["post_key"]
        for r in result.records
        if r.id in scenario.excluded_record_ids
    }
    assert excluded == {"syn-episode-01", "syn-episode-02"}


def test_incomplete_or_changed_retained_inputs_cannot_recompute(baseline):
    target = next(r.id for r in baseline.records if r.kind == "telegram_post")
    payload = baseline.model_dump(mode="json")
    payload.pop("swarm_context")
    with pytest.raises(ValueError, match="complete normalized inputs"):
        recompute_swarm_scenario(payload, [target], "Inspect")
    payload = baseline.model_dump(mode="json")
    payload["swarm_context"]["dataset"]["messages"].pop()
    with pytest.raises(ValueError, match="retained inputs changed"):
        recompute_swarm_scenario(payload, [target], "Inspect")


def test_group_correspondence_is_explicit_for_split_merge_and_uncertain():
    def finding(name, members):
        return {"snapshot_id": name, "details": {"swarm_group": {"account_keys": members}}}

    split = _correspondences(
        [finding("before", ["a", "b", "c", "d"])],
        [finding("one", ["a", "b"]), finding("two", ["c", "d"])],
    )
    assert split[0]["relation"] == "split"
    merge = _correspondences(
        [finding("one", ["a", "b"]), finding("two", ["c", "d"])],
        [finding("after", ["a", "b", "c", "d"])],
    )
    assert merge[0]["relation"] == "merge"
    changed = _correspondences(
        [finding("before", ["a", "b", "c"])], [finding("after", ["a", "b", "d"])]
    )
    assert changed[0]["relation"] == "uncertain"
    assert (
        _correspondences([finding("old", ["a"])], [finding("new", ["z"])])[0]["relation"]
        == "removed"
    )
