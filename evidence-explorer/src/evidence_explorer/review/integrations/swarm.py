"""Adapt Swarm's existing detector into source-neutral Evidence Trail bundles.

The detector remains the authority for behavioral results. The bridge preserves
its complete declared input, including background activity, so a scenario never
accidentally substitutes selected candidate comments for an activity baseline.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from copy import deepcopy
from typing import Any

from ..contracts import EvidenceBundle


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _swarm_modules():
    try:
        from swarm_detector.detector import analyze
        from swarm_detector.sample_data import build_sample
    except ImportError as exc:
        raise RuntimeError(
            "The optional Swarm integration is unavailable. Install the local "
            "swarm-detector package in the Evidence Explorer environment, "
            "including its networkx dependency."
        ) from exc
    return analyze, build_sample


def _record_id(namespace: str, kind: str, key: str, raw: dict) -> str:
    # Source identity and source content both participate in immutable identity.
    return f"{namespace}:{kind}:{_digest([key, raw])[:24]}"


def build_swarm_bundle(dataset: dict, run: dict | None = None) -> EvidenceBundle:
    """Return a validated bundle from a full normalized dataset and detector run.

    If ``run`` is omitted the installed Swarm detector runs with its own defaults.
    Passing a run from a different dataset fails rather than exporting mismatched
    results. Original data is copied; the adapter never edits producer state.
    """
    dataset = deepcopy(dataset)
    if run is None:
        analyze, _ = _swarm_modules()
        run = analyze(dataset)
    run = deepcopy(run)
    source_fingerprint = _digest(dataset)
    if run.get("source_fingerprint") != source_fingerprint:
        raise ValueError("Swarm result does not match the complete supplied dataset")
    if run.get("dataset_id") != str(dataset.get("dataset_id", "unnamed-dataset")):
        raise ValueError("Swarm result belongs to a different dataset")

    namespace = f"swarm:{_digest(str(dataset.get('dataset_id', 'unnamed-dataset')))[:12]}"
    run_id = f"{namespace}:run:{_digest(run)[:24]}"
    records: list[dict] = []
    fragments: list[dict] = []
    links: list[dict] = []
    post_ids: dict[str, str] = {}
    message_ids: dict[str, str] = {}
    fragment_ids: dict[str, str] = {}
    channel_ids: dict[str, str] = {}

    def add_record(kind: str, key: str, raw: dict, text: str, label: str) -> str:
        rid = _record_id(namespace, kind, key, raw)
        source = raw.get("source_ref")
        record = {
            "id": rid,
            "kind": kind,
            "source_label": label,
            "text": text,
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "source_url": source
            if isinstance(source, str) and source.startswith(("https://", "http://"))
            else None,
            "acquired_at": None,
            "published_at": raw.get("timestamp_utc"),
            "metadata": {
                "synthetic": bool(dataset.get("synthetic", False)),
                "source_ref": source,
                "swarm": deepcopy(raw),
            },
        }
        records.append(record)
        fid = f"{rid}:fragment"
        fragments.append(
            {
                "id": fid,
                "record_id": rid,
                "selector": {"kind": "text", "start": 0, "end": len(text)}
                if text
                else {"kind": "whole_record"},
                "quote": text or None,
            }
        )
        fragment_ids[rid] = fid
        return rid

    def add_link(
        source_type: str,
        source: str,
        target_type: str,
        target: str,
        relation: str,
        basis: str,
        evidence: list[str] | None = None,
        producer_run: str | None = None,
    ):
        links.append(
            {
                "id": f"{namespace}:link:{
                    _digest([source_type, source, target_type, target, relation, producer_run])[:24]
                }",
                "source": {"type": source_type, "id": source},
                "target": {"type": target_type, "id": target},
                "relation": relation,
                "fragment_ids": evidence or [],
                "basis": basis,
                "run_id": producer_run,
            }
        )

    for channel in dataset.get("channels", []):
        key = channel["channel_key"]
        channel_ids[key] = add_record(
            "telegram_channel",
            key,
            channel,
            json.dumps(channel, ensure_ascii=False, sort_keys=True),
            f"Supplied channel metadata: {channel.get('name', key)}",
        )
    for post in dataset.get("posts", []):
        key = post["post_key"]
        post_ids[key] = add_record(
            "telegram_post",
            key,
            post,
            post.get("text_original", ""),
            f"Supplied parent post: {post.get('title', key)}",
        )
        if post.get("channel_key") in channel_ids:
            add_link(
                "record",
                post_ids[key],
                "record",
                channel_ids[post["channel_key"]],
                "part_of",
                "Supplied export identifies the parent channel.",
            )
    for message in dataset.get("messages", []):
        key = message["message_key"]
        message_ids[key] = add_record(
            "telegram_message",
            key,
            message,
            message.get("text_original", ""),
            f"Supplied Telegram comment: {key}",
        )
        if message.get("post_key") in post_ids:
            add_link(
                "record",
                message_ids[key],
                "record",
                post_ids[message["post_key"]],
                "part_of",
                "Supplied export identifies the parent discussion.",
            )

    # Exact saved text is a content relationship, never independent provenance.
    originals: dict[tuple[str, str], str] = {}
    for record in records:
        if record["kind"] not in {"telegram_message", "telegram_post"} or not record["text"]:
            continue
        key = (record["kind"], record["text"])
        original = originals.setdefault(key, record["id"])
        if original != record["id"]:
            add_link(
                "record",
                original,
                "record",
                record["id"],
                "same_content",
                "Saved text is exactly identical; common origin and independence are unknown.",
            )

    pairs = {pair["pair_key"]: pair for pair in run["pairs"]}
    post_index = {p["post_key"]: p for p in dataset.get("posts", [])}
    findings = []
    for group in run["groups"]:
        snapshot = f"{run_id}:finding:{group['case_id']}"
        group_pairs = [pairs[key] for key in group["pair_keys"]]
        evidence_keys = sorted(
            {
                event[field]
                for pair in group_pairs
                for event in pair["events"]
                for field in ("message_a", "message_b")
            }
        )
        if any(key not in message_ids for key in evidence_keys):
            raise ValueError("Swarm group contains unresolved supporting messages")
        counterevidence = [
            "Observed coordination does not establish automation, hostile intent, foreign control, "
            "or an operator.",
            "Shared breaking news and public calls to participate require contextual review.",
        ]
        public_calls = [
            post_ids[key]
            for key in group["post_keys"]
            if key in post_index
            and "PUBLIC CALL" in post_index[key].get("text_original", "")
            and dataset.get("synthetic")
        ]
        if public_calls:
            counterevidence.append(
                "This synthetic fixture contains a public volunteer call. "
                "Its observed coordination can have a transparent human explanation."
            )
        findings.append(
            {
                "snapshot_id": snapshot,
                "lineage_id": f"{namespace}:group:{_digest(sorted(group['account_keys']))[:24]}",
                "version": 1,
                "run_id": run_id,
                "kind": "observation",
                "statement": (
                    f"{'Synthetic example: ' if dataset.get('synthetic') else ''}"
                    f"{len(group['account_keys'])} accounts repeatedly entered discussions close "
                    f"together across {group['family_count']} observed post families."
                ),
                "details": {
                    "domain": "swarm",
                    "swarm_group": deepcopy(group),
                    "swarm_pairs": deepcopy(group_pairs),
                    "counterevidence": counterevidence,
                    "context_record_ids": public_calls,
                    "behavior_uses_text": False,
                    "review_needed": True,
                },
            }
        )
        for key in evidence_keys:
            fid = fragment_ids[message_ids[key]]
            add_link(
                "fragment",
                fid,
                "finding",
                snapshot,
                "supports",
                "Saved comment metadata identifies account, first-arrival time, and parent "
                "discussion; text is shown for context and does not determine behavior rank.",
                [fid],
                run_id,
            )

    coverage = {
        "channels": len(dataset.get("channels", [])),
        "posts": len(dataset.get("posts", [])),
        "messages": len(dataset.get("messages", [])),
        "candidate_groups": len(run["groups"]),
        "declared_input": "Complete normalized subset, including background traffic",
        "comparisons": deepcopy(dataset.get("comparisons", [])),
    }
    return EvidenceBundle.model_validate(
        {
            "schema_version": "1.0",
            "case": {
                "id": f"{namespace}:case",
                "title": dataset.get("label", "Swarm behavioral investigation"),
                "question": "Which groups show repeated joint arrivals, and what alternative "
                "explanations remain?",
                "synthetic": bool(dataset.get("synthetic", False)),
                "coverage": coverage,
                "limitations": [
                    "Behavioral observations do not establish bots, intent, attribution, or truth.",
                    "Randomized controls are descriptive references, not truth probabilities.",
                    *dataset.get("manifest", {}).get("limitations", []),
                ],
            },
            "records": records,
            "fragments": fragments,
            "runs": [
                {
                    "id": run_id,
                    "input_fingerprint": source_fingerprint,
                    "input_manifest": [r["id"] for r in records],
                    "method": "swarm_detector.detector.analyze",
                    "method_version": str(run.get("manifest", {}).get("detector_version", 1)),
                    "created_at": run["created_at"],
                    "full_inputs_available": True,
                    "recomputation_capability": "swarm_full_subset",
                    "configuration": deepcopy(run["config"]),
                    "coverage": coverage,
                    "swarm_result": run,
                }
            ],
            "findings": findings,
            "links": links,
            "swarm_context": {
                "version": 1,
                "baseline_run_id": run_id,
                "dataset_fingerprint": source_fingerprint,
                "dataset": dataset,
            },
        }
    )


def build_swarm_demo_bundle() -> EvidenceBundle:
    """Analyze Swarm's existing fictional fixture and adapt its actual results."""
    analyze, build_sample = _swarm_modules()
    dataset = build_sample()
    return build_swarm_bundle(dataset, analyze(dataset))


def _correspondences(before: list[dict], after: list[dict]) -> list[dict]:
    """Describe member overlaps without assuming changed groups share identity."""
    members_before = [set(f["details"]["swarm_group"]["account_keys"]) for f in before]
    members_after = [set(f["details"]["swarm_group"]["account_keys"]) for f in after]
    adjacent_before = defaultdict(set)
    adjacent_after = defaultdict(set)
    for i, left in enumerate(members_before):
        for j, right in enumerate(members_after):
            if left & right:
                adjacent_before[i].add(j)
                adjacent_after[j].add(i)
    seen_before: set[int] = set()
    seen_after: set[int] = set()
    result = []
    for i in range(len(before)):
        if i in seen_before:
            continue
        left, right = {i}, set()
        while True:
            updated_right = right | {j for x in left for j in adjacent_before[x]}
            updated_left = left | {x for j in updated_right for x in adjacent_after[j]}
            if (updated_left, updated_right) == (left, right):
                break
            left, right = updated_left, updated_right
        seen_before.update(left)
        seen_after.update(right)
        if not right:
            relation = "removed"
        elif len(left) == len(right) == 1:
            relation = (
                "continuation"
                if members_before[i] == members_after[next(iter(right))]
                else "uncertain"
            )
        elif len(left) == 1:
            relation = "split"
        elif len(right) == 1:
            relation = "merge"
        else:
            relation = "uncertain"
        result.append(
            {
                "baseline_snapshot_ids": sorted(before[x]["snapshot_id"] for x in left),
                "result_snapshot_ids": sorted(after[j]["snapshot_id"] for j in right),
                "relation": relation,
                "basis": "Compared supplied account identifiers. Changed membership does not "
                "establish persistent group identity; approvals are not transferred.",
            }
        )
    for j in range(len(after)):
        if j not in seen_after:
            result.append(
                {
                    "baseline_snapshot_ids": [],
                    "result_snapshot_ids": [after[j]["snapshot_id"]],
                    "relation": "added",
                    "basis": "No baseline candidate shares member identifiers.",
                }
            )
    return result


def recompute_swarm_scenario(
    bundle: EvidenceBundle | dict,
    excluded_record_ids: list[str],
    reason: str,
) -> EvidenceBundle:
    """Append a same-subset exclusion run and scenario, preserving baseline history.

    A selected parent post excludes its complete documented post family. Selecting
    an individual message excludes that message only. Every scenario starts from
    the retained original baseline; it never silently chains earlier exclusions.
    """
    bundle = EvidenceBundle.model_validate(
        bundle.model_dump(mode="json") if isinstance(bundle, EvidenceBundle) else bundle
    )
    payload = bundle.model_dump(mode="json")
    context = payload.get("swarm_context")
    if not isinstance(context, dict) or not isinstance(context.get("dataset"), dict):
        raise ValueError("Swarm recomputation unavailable: complete normalized inputs are missing")
    original = context["dataset"]
    if _digest(original) != context.get("dataset_fingerprint"):
        raise ValueError("Swarm recomputation unavailable: retained inputs changed")
    baseline = next((r for r in payload["runs"] if r["id"] == context.get("baseline_run_id")), None)
    if (
        not baseline
        or not baseline["full_inputs_available"]
        or baseline["input_fingerprint"] != context["dataset_fingerprint"]
    ):
        raise ValueError("Swarm recomputation unavailable: baseline context is incomplete")
    raw_baseline = baseline.get("swarm_result", {})
    if raw_baseline.get("source_fingerprint") != context["dataset_fingerprint"] or (
        raw_baseline.get("config") != baseline["configuration"]
    ):
        raise ValueError("Swarm recomputation unavailable: baseline provenance is inconsistent")
    if not reason.strip():
        raise ValueError("An exclusion scenario requires a reason")
    records = {r["id"]: r for r in payload["records"]}
    selected = set(excluded_record_ids)
    if not selected or not selected <= records.keys():
        raise ValueError("Choose existing supplied records to exclude")
    if any(records[rid]["kind"] not in {"telegram_message", "telegram_post"} for rid in selected):
        raise ValueError("Only supplied messages or parent post families can be excluded")

    original_messages = {m["message_key"]: m for m in original["messages"]}
    original_posts = {p["post_key"]: p for p in original["posts"]}
    for rid in selected:
        record = records[rid]
        supplied = record["metadata"].get("swarm", {})
        key_field = "message_key" if record["kind"] == "telegram_message" else "post_key"
        source = original_messages if key_field == "message_key" else original_posts
        if supplied != source.get(supplied.get(key_field)):
            raise ValueError("Selected exclusion record does not match retained source context")

    post_index = {post["post_key"]: post for post in original["posts"]}
    excluded_posts = {
        records[rid]["metadata"]["swarm"]["post_key"]
        for rid in selected
        if records[rid]["kind"] == "telegram_post"
    }
    excluded_families = {post_index[key].get("post_family_key") or key for key in excluded_posts}
    excluded_posts = {
        key
        for key, post in post_index.items()
        if (post.get("post_family_key") or key) in excluded_families
    }
    excluded_messages = {
        records[rid]["metadata"]["swarm"]["message_key"]
        for rid in selected
        if records[rid]["kind"] == "telegram_message"
    }
    filtered = deepcopy(original)
    filtered["posts"] = [p for p in filtered["posts"] if p["post_key"] not in excluded_posts]
    filtered["messages"] = [
        m
        for m in filtered["messages"]
        if m["post_key"] not in excluded_posts and m["message_key"] not in excluded_messages
    ]
    expanded = sorted(
        rid
        for rid, record in records.items()
        if record["kind"] == "telegram_post"
        and record["metadata"]["swarm"]["post_key"] in excluded_posts
        or record["kind"] == "telegram_message"
        and (
            record["metadata"]["swarm"]["post_key"] in excluded_posts
            or record["metadata"]["swarm"]["message_key"] in excluded_messages
        )
    )
    scenario_digest = _digest([baseline["id"], expanded, reason.strip()])[:24]
    scenario_id = f"{payload['case']['id']}:scenario:{scenario_digest}"
    # An identical request reuses its immutable scenario instead of appending a
    # fresh timestamped detector run and duplicating the audit trail.
    if any(s["id"] == scenario_id for s in payload["scenarios"]):
        return bundle
    analyze, _ = _swarm_modules()
    result_run = analyze(filtered, deepcopy(baseline["configuration"]))
    result_bundle = build_swarm_bundle(filtered, result_run).model_dump(mode="json")
    for finding in result_bundle["findings"]:
        previous_versions = [
            f["version"] for f in payload["findings"] if f["lineage_id"] == finding["lineage_id"]
        ]
        finding["version"] = max(previous_versions, default=0) + 1
    result_id = result_bundle["runs"][0]["id"]
    before = [f for f in payload["findings"] if f["run_id"] == baseline["id"]]
    scenario = {
        "id": scenario_id,
        "base_run_id": baseline["id"],
        "excluded_record_ids": expanded,
        "requested_excluded_record_ids": sorted(selected),
        "reason": reason.strip(),
        "status": "completed",
        "result_run_id": result_id,
        "correspondences": _correspondences(before, result_bundle["findings"]),
        "full_input_fingerprint": baseline["input_fingerprint"],
        "result_input_fingerprint": result_bundle["runs"][0]["input_fingerprint"],
        "configuration_preserved": True,
        "reviews_need_reconsideration": [f["snapshot_id"] for f in before],
    }
    for name, key in (
        ("records", "id"),
        ("fragments", "id"),
        ("runs", "id"),
        ("findings", "snapshot_id"),
        ("links", "id"),
    ):
        seen = {item[key] for item in payload[name]}
        payload[name].extend(item for item in result_bundle[name] if item[key] not in seen)
    payload["scenarios"].append(scenario)
    return EvidenceBundle.model_validate(payload)
