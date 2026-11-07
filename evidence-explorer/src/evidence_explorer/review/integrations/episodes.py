"""Select a documented Swarm episode for a descriptive sensitivity check."""

from __future__ import annotations

from collections import defaultdict

from ..contracts import EvidenceBundle, Record
from .swarm import _digest


def strongest_episode_record(bundle: EvidenceBundle, finding_id: str) -> Record:
    """Return a baseline parent post from the family supporting most distinct pairs.

    A pair contributes at most once per documented post family, even when copies
    of a post or repeated events occur. Ties use the family key, then contributing
    post key and record ID. The selected family is a sensitivity-test input; its
    rank does not establish automation, intent, or independent corroboration.
    """
    finding = next((item for item in bundle.findings if item.snapshot_id == finding_id), None)
    if finding is None:
        raise ValueError(f"Unknown finding: {finding_id}")
    context = getattr(bundle, "swarm_context", None)
    if finding.details.get("domain") != "swarm" or not isinstance(context, dict):
        raise ValueError(
            "Episode selection requires a Swarm finding with complete baseline context."
        )
    dataset = context.get("dataset")
    baseline_id = context.get("baseline_run_id")
    if not isinstance(dataset, dict) or not isinstance(dataset.get("posts"), list):
        raise ValueError(
            "Episode selection unavailable: complete normalized Swarm inputs are missing."
        )
    baseline = next((run for run in bundle.runs if run.id == baseline_id), None)
    if baseline is None or not baseline.full_inputs_available:
        raise ValueError("Episode selection unavailable: complete baseline inputs are required.")
    if finding.run_id != baseline_id:
        raise ValueError("Select an original baseline Swarm finding for this sensitivity check.")
    fingerprint = _digest(dataset)
    if (
        fingerprint != context.get("dataset_fingerprint")
        or fingerprint != baseline.input_fingerprint
    ):
        raise ValueError("Episode selection unavailable: retained baseline inputs changed.")
    raw_baseline = baseline.model_dump().get("swarm_result", {})
    if (
        raw_baseline.get("source_fingerprint") != fingerprint
        or raw_baseline.get("config") != baseline.configuration
    ):
        raise ValueError("Episode selection unavailable: baseline provenance is inconsistent.")

    post_index = {post["post_key"]: post for post in dataset["posts"]}
    pairs_by_family: dict[str, set[tuple[str, str]]] = defaultdict(set)
    posts_by_family: dict[str, set[str]] = defaultdict(set)
    pairs = finding.details.get("swarm_pairs")
    if not isinstance(pairs, list):
        raise ValueError("No documented pair-event support is available for this finding.")
    for pair in pairs:
        account_a, account_b = pair.get("account_a"), pair.get("account_b")
        if (
            not isinstance(account_a, str)
            or not isinstance(account_b, str)
            or account_a == account_b
        ):
            raise ValueError(
                "Pair-event support requires two distinct supplied account identifiers."
            )
        pair_id = tuple(sorted((account_a, account_b)))
        for event in pair.get("events", []):
            post_key = event.get("post_key")
            if post_key not in post_index:
                raise ValueError(
                    "Pair-event support references an unavailable baseline parent post."
                )
            post = post_index[post_key]
            family = post.get("post_family_key") or post_key
            if not isinstance(family, str) or not family:
                raise ValueError("Baseline parent posts require a documented family identifier.")
            if event.get("family_key", family) != family:
                raise ValueError(
                    "Pair-event family does not match its supplied baseline parent post."
                )
            pairs_by_family[family].add(pair_id)
            posts_by_family[family].add(post_key)
    if not pairs_by_family:
        raise ValueError("No documented pair-event support is available for this finding.")

    family = min(pairs_by_family, key=lambda key: (-len(pairs_by_family[key]), key))
    input_ids = set(baseline.input_manifest)
    candidates = []
    for record in bundle.records:
        if record.kind != "telegram_post" or record.id not in input_ids:
            continue
        raw = record.metadata.get("swarm", {})
        post_key = raw.get("post_key")
        if post_key in posts_by_family[family] and raw == post_index.get(post_key):
            candidates.append(record)
    if not candidates:
        raise ValueError(
            "The strongest family's original supplied parent-post record is unavailable."
        )
    return min(candidates, key=lambda record: (record.metadata["swarm"]["post_key"], record.id))
