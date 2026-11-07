"""Inspectable first-arrival coordination candidates, with descriptive controls.

This module never reads message text, URLs, narrative labels, or channel categories.
It detects recurring behaviour; its results do not establish bots or intent.
It accepts an in-memory dataset and returns a specialist run without persisting
it. Callers own input collection, complete background context, storage, and
conversion into the shared evidence contract.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import platform
import random
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import networkx as nx

from .config import DetectionConfig, positive_number


@dataclass(frozen=True, slots=True)
class _Assignment:
    """Account-to-thread membership used by the randomization algorithm."""

    post_key: str
    account_key: str
    stratum: tuple[str, str]

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for value in (self.post_key, self.account_key)):
            raise ValueError("Assignments require nonempty post and account identifiers.")
        if (
            not isinstance(self.stratum, tuple)
            or len(self.stratum) != 2
            or not all(isinstance(value, str) and value for value in self.stratum)
        ):
            raise ValueError("An assignment stratum requires a channel and UTC hour.")


@dataclass(frozen=True, slots=True)
class _Arrival(_Assignment):
    """One validated first arrival; source wording is deliberately absent."""

    family_key: str
    channel_key: str
    message_key: str
    timestamp: float
    timestamp_utc: str

    def __post_init__(self) -> None:
        # Explicit base call also works with slots dataclasses on Python 3.12.
        _Assignment.__post_init__(self)
        if not all(
            isinstance(value, str) and value
            for value in (self.family_key, self.channel_key, self.message_key, self.timestamp_utc)
        ):
            raise ValueError("Arrivals require source, family and channel identifiers.")
        if (
            isinstance(self.timestamp, bool)
            or not isinstance(self.timestamp, (int, float))
            or not math.isfinite(self.timestamp)
        ):
            raise ValueError("Arrival timestamps must be finite.")
        if self.stratum != (self.channel_key, self.timestamp_utc[:13]):
            raise ValueError("Arrival stratum must match its channel and UTC hour.")


DEFAULT_CONFIG = DetectionConfig().as_dict()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _timestamp(value: str) -> tuple[float, str]:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("A source timezone is required")
    parsed = parsed.astimezone(UTC)
    return parsed.timestamp(), parsed.isoformat().replace("+00:00", "Z")


def _prepare(dataset: dict) -> tuple[list[_Arrival], dict]:
    posts = {str(p["post_key"]): p for p in dataset.get("posts", [])}
    counts = Counter()
    arrivals: dict[tuple[str, str], _Arrival] = {}
    seen_messages = set()
    # Sorting uses only behavioural identifiers and timestamps, never text.
    messages = sorted(
        dataset.get("messages", []),
        key=lambda m: (
            str(m.get("timestamp_utc", "")),
            str(m.get("message_key", "")),
            str(m.get("account_key", "")),
        ),
    )
    for message in messages:
        message_key = str(message.get("message_key", ""))
        if not message_key:
            counts["missing_message_keys"] += 1
            continue
        if message_key in seen_messages:
            counts["duplicate_messages"] += 1
            continue
        seen_messages.add(message_key)
        account = message.get("account_key")
        if not account or message.get("sender_type", "user") != "user":
            counts["unidentifiable_or_nonuser_messages"] += 1
            continue
        post_key = str(message.get("post_key", ""))
        post = posts.get(post_key)
        if post is None:
            counts["unresolved_post_messages"] += 1
            continue
        channel = str(post.get("channel_key", ""))
        if not channel:
            counts["unresolved_channel_messages"] += 1
            continue
        try:
            timestamp, iso = _timestamp(message["timestamp_utc"])
        except (ValueError, TypeError, KeyError, OverflowError):
            counts["invalid_timestamp_messages"] += 1
            continue
        counts["eligible_messages"] += 1
        account = str(account)
        slot = _Arrival(
            post_key=post_key,
            family_key=str(post.get("post_family_key") or post_key),
            channel_key=channel,
            account_key=account,
            message_key=message_key,
            timestamp=timestamp,
            timestamp_utc=iso,
            stratum=(channel, iso[:13]),
        )
        key = (post_key, account)
        previous = arrivals.get(key)
        if previous is None or (timestamp, message_key) < (
            previous.timestamp,
            previous.message_key,
        ):
            arrivals[key] = slot
    slots = sorted(
        arrivals.values(),
        key=lambda s: (s.post_key, s.timestamp, s.account_key),
    )
    diagnostics = {
        "posts": len(posts),
        "messages": len(dataset.get("messages", [])),
        "first_arrivals": len(slots),
        "identifiable_accounts": len({s.account_key for s in slots}),
        "channels": len({s.channel_key for s in slots}),
        "post_families": len({s.family_key for s in slots}),
        **dict(counts),
    }
    return slots, diagnostics


def _post_indices(slots: list[_Arrival]) -> dict[str, list[int]]:
    result = defaultdict(list)
    for index, slot in enumerate(slots):
        result[slot.post_key].append(index)
    return dict(result)


def _discover(
    slots: list[_Arrival], by_post: dict, window: float, limit: int, minimum: int
) -> tuple[list[tuple[str, str]], list[str]]:
    families = defaultdict(set)
    omitted = []
    for post_key, indices in by_post.items():
        local_pairs = []
        left = 0
        exceeded = False
        for right, index in enumerate(indices):
            while slots[index].timestamp - slots[indices[left]].timestamp > window:
                left += 1
            for earlier in indices[left:right]:
                local_pairs.append(
                    tuple(sorted((slots[earlier].account_key, slots[index].account_key)))
                )
                if len(local_pairs) > limit:
                    exceeded = True
                    break
            if exceeded:
                break
        if exceeded:
            omitted.append(post_key)
            continue
        family = slots[indices[0]].family_key
        for pair in local_pairs:
            families[pair].add(family)
    return sorted(pair for pair, support in families.items() if len(support) >= minimum), omitted


def _candidate_adjacency(candidates: list[tuple[str, str]]) -> dict[str, set]:
    result = defaultdict(set)
    for account_a, account_b in candidates:
        result[account_a].add(account_b)
    return dict(result)


def _measure(
    slots: list[_Arrival],
    by_post: dict,
    labels: list[str],
    adjacency: dict,
    windows: list[float],
    include_events: bool = False,
    main_window: float = 120,
) -> tuple[dict, dict, dict]:
    support = {window: defaultdict(set) for window in windows}
    opportunities = defaultdict(set)
    events = defaultdict(list)
    for indices in by_post.values():
        lookup = {labels[index]: index for index in indices}
        family = slots[indices[0]].family_key
        for account_a, index_a in lookup.items():
            for account_b in adjacency.get(account_a, ()):
                index_b = lookup.get(account_b)
                if index_b is None:
                    continue
                pair = (account_a, account_b)
                opportunities[pair].add(family)
                slot_a, slot_b = slots[index_a], slots[index_b]
                delta = abs(slot_a.timestamp - slot_b.timestamp)
                for window in windows:
                    if delta <= window:
                        support[window][pair].add(family)
                if include_events and delta <= main_window:
                    events[pair].append(
                        {
                            "post_key": slot_a.post_key,
                            "family_key": family,
                            "message_a": slot_a.message_key,
                            "message_b": slot_b.message_key,
                            "delta_seconds": delta,
                            "timestamp_a": slot_a.timestamp_utc,
                            "timestamp_b": slot_b.timestamp_utc,
                        }
                    )
    return support, opportunities, events


def _shuffle_assignments(
    slots: Sequence[_Assignment | Mapping],
    seed: int,
    swaps_per_slot: int = 10,
    max_attempts_per_slot: int = 100,
) -> tuple[list[str], list[dict]]:
    """Swap sender labels without changing counts, times, or account/post uniqueness."""
    # Preserve the private helper's mapping input compatibility for existing callers.
    # Analyze itself supplies validated arrival records throughout.
    slots = [
        (
            _Assignment(slot["post_key"], slot["account_key"], tuple(slot["stratum"]))
            if isinstance(slot, Mapping)
            else slot
        )
        for slot in slots
    ]
    rng = random.Random(seed)
    original = [slot.account_key for slot in slots]
    labels = original.copy()
    strata = defaultdict(list)
    members = defaultdict(set)
    for index, slot in enumerate(slots):
        strata[slot.stratum].append(index)
        members[slot.post_key].add(labels[index])
    diagnostics = []
    for stratum, indices in sorted(strata.items()):
        target = len(indices) * swaps_per_slot
        attempt_limit = len(indices) * max_attempts_per_slot
        attempts = accepted = 0
        # An immovable stratum should not burn its full rejection budget.
        movable = len(indices) >= 2 and len({slots[i].post_key for i in indices}) >= 2
        while movable and accepted < target and attempts < attempt_limit:
            attempts += 1
            index_a, index_b = rng.sample(indices, 2)
            account_a, account_b = labels[index_a], labels[index_b]
            post_a, post_b = slots[index_a].post_key, slots[index_b].post_key
            if account_a == account_b or post_a == post_b:
                continue
            # Membership covers the entire post, including other hour strata.
            if account_b in members[post_a] or account_a in members[post_b]:
                continue
            members[post_a].remove(account_a)
            members[post_a].add(account_b)
            members[post_b].remove(account_b)
            members[post_b].add(account_a)
            labels[index_a], labels[index_b] = account_b, account_a
            accepted += 1
        changed = sum(labels[index] != original[index] for index in indices)
        diagnostics.append(
            {
                "channel_key": stratum[0],
                "utc_hour": stratum[1],
                "slots": len(indices),
                "attempts": attempts,
                "accepted_swaps": accepted,
                "target_swaps": target,
                "changed_slots": changed,
                "changed_fraction": changed / len(indices),
                "limited": accepted < target or changed / len(indices) < 0.2,
            }
        )
    return labels, diagnostics


def _percentile(values: list[int], quantile: float = 0.95) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _build_groups(pairs: list[dict], synthetic: bool) -> list[dict]:
    graph = nx.Graph()
    pair_by_accounts = {}
    for pair in pairs:
        if not pair["retained"]:
            continue
        accounts = (pair["account_a"], pair["account_b"])
        # Positive family support is the graph weight; excess is only a rank.
        graph.add_edge(*accounts, weight=pair["observed"])
        pair_by_accounts[accounts] = pair
    if graph.number_of_edges() == 0:
        return []
    communities = nx.community.greedy_modularity_communities(graph, weight="weight", resolution=1.0)
    groups = []
    for community in sorted(communities, key=lambda members: tuple(sorted(members))):
        core = nx.k_core(graph.subgraph(community), k=2)
        for component in sorted(
            nx.connected_components(core), key=lambda members: tuple(sorted(members))
        ):
            if len(component) < 3:
                continue
            accounts = sorted(component)
            evidence = [
                pair
                for key, pair in pair_by_accounts.items()
                if key[0] in component and key[1] in component
            ]
            events = [event for pair in evidence for event in pair["events"]]
            peripheral = sorted(
                {
                    neighbour
                    for account in component
                    for neighbour in graph.neighbors(account)
                    if neighbour in community and neighbour not in core
                }
            )
            groups.append(
                {
                    "case_id": "case-" + _digest(accounts)[:12],
                    "name": "Candidate coordinated group",
                    "kind": "candidate",
                    "account_keys": accounts,
                    "peripheral_keys": peripheral,
                    "pair_keys": sorted(pair["pair_key"] for pair in evidence),
                    "post_keys": sorted({event["post_key"] for event in events}),
                    "family_count": len({event["family_key"] for event in events}),
                    "rank_score": statistics.median(pair["score"] for pair in evidence),
                    "description": (
                        ("Synthetic demonstration. " if synthetic else "")
                        + f"{len(accounts)} accounts repeatedly entered discussions close together. "
                        "The evidence does not establish automation, intent, or an operator."
                    ),
                }
            )
    groups.sort(
        key=lambda group: (-group["rank_score"], -group["family_count"], group["account_keys"])
    )
    for number, group in enumerate(groups, start=1):
        group["name"] = f"Candidate group {number:02d}" + (" · synthetic" if synthetic else "")
    return groups


def analyze(dataset: dict, config: dict | None = None) -> dict:
    """Compute candidate pairs/groups and baseline diagnostics from supplied inputs.

    Invalid or unresolved arrivals are counted in preparation diagnostics; this
    function does not fetch missing records or establish archive completeness.
    Callers must supply the intended comparison context before treating a run as
    a recomputation of an existing assessment.
    """
    settings = DetectionConfig.from_mapping(config).as_dict()
    precision = positive_number(
        dataset.get("manifest", {}).get("timestamp_precision_seconds", 1),
        "timestamp_precision_seconds",
    )
    if settings["window_seconds"] < precision:
        raise ValueError("The main timing window cannot be smaller than source timestamp precision")
    slots, diagnostics = _prepare(dataset)
    by_post = _post_indices(slots)
    fingerprint = _digest(
        [
            [
                slot.post_key,
                slot.family_key,
                slot.channel_key,
                slot.account_key,
                slot.message_key,
                slot.timestamp_utc,
            ]
            for slot in slots
        ]
    )
    candidates, omitted = _discover(
        slots,
        by_post,
        settings["window_seconds"],
        settings["max_pairs_per_post"],
        settings["min_families"],
    )
    adjacency = _candidate_adjacency(candidates)
    original_labels = [slot.account_key for slot in slots]
    sensitivity_windows = [window for window in (30, 120, 300) if window >= precision]
    windows = sorted({*sensitivity_windows, settings["window_seconds"]})
    observed, opportunities, events = _measure(
        slots,
        by_post,
        original_labels,
        adjacency,
        windows,
        include_events=True,
        main_window=settings["window_seconds"],
    )
    null_counts = {pair: [] for pair in candidates}
    mixing = defaultdict(
        lambda: {
            "attempts": 0,
            "accepted_swaps": 0,
            "changed_slots": 0,
            "limited_controls": 0,
            "slots": 0,
        }
    )
    for control in range(settings["randomizations"]):
        randomized, control_diagnostics = _shuffle_assignments(
            slots,
            int(settings["seed"]) + control * 1_000_003,
            settings["swaps_per_slot"],
            settings["max_attempts_per_slot"],
        )
        measured, _, _ = _measure(
            slots, by_post, randomized, adjacency, [settings["window_seconds"]]
        )
        for pair in candidates:
            null_counts[pair].append(len(measured[settings["window_seconds"]].get(pair, ())))
        for row in control_diagnostics:
            summary = mixing[(row["channel_key"], row["utc_hour"])]
            summary["slots"] = row["slots"]
            for key in ("attempts", "accepted_swaps", "changed_slots"):
                summary[key] += row[key]
            summary["limited_controls"] += int(row["limited"])
    pairs = []
    for pair in candidates:
        values = null_counts[pair]
        mean = statistics.fmean(values) if values else 0.0
        variance = statistics.pvariance(values) if values else 0.0
        p95 = _percentile(values) if values else 0.0
        support = len(observed[settings["window_seconds"]].get(pair, ()))
        pair_events = sorted(
            events[pair], key=lambda event: (event["timestamp_a"], event["post_key"])
        )
        pairs.append(
            {
                "pair_key": "pair-" + _digest(pair)[:12],
                "account_a": pair[0],
                "account_b": pair[1],
                "observed": support,
                "opportunities": len(opportunities.get(pair, ())),
                "null_mean": mean,
                "null_p95": p95,
                "null_variance": variance,
                "score": (support - mean) / math.sqrt(variance + 1),
                "retained": bool(values)
                and support >= settings["min_families"]
                and support > mean
                and support > p95,
                "events": pair_events,
                "sensitivity": {
                    str(window): len(observed[window].get(pair, ()))
                    for window in sensitivity_windows
                },
            }
        )
    pairs.sort(key=lambda pair: (-pair["score"], pair["account_a"], pair["account_b"]))
    synthetic = bool(dataset.get("synthetic", False))
    groups = _build_groups(pairs, synthetic)
    stratum_rows = []
    for (channel, hour), summary in sorted(mixing.items()):
        denominator = summary["slots"] * settings["randomizations"]
        stratum_rows.append(
            {
                "channel_key": channel,
                "utc_hour": hour,
                **summary,
                "mean_changed_fraction": summary["changed_slots"] / denominator
                if denominator
                else 0,
            }
        )
    diagnostics.update(
        {
            "candidate_pairs": len(pairs),
            "retained_pairs": sum(pair["retained"] for pair in pairs),
            "candidate_groups": len(groups),
            "controls_completed": settings["randomizations"],
            "baseline_is_descriptive": True,
            "timestamp_precision_seconds": precision,
            "disabled_sensitivity_windows": [
                window for window in (30, 120, 300) if window < precision
            ],
            "discovery_omitted_posts": omitted,
            "discovery_omitted_post_count": len(omitted),
            "discovery_omitted_arrivals": sum(len(by_post[key]) for key in omitted),
            "mixing": stratum_rows,
            "limited_baseline_strata": [
                {"channel_key": row["channel_key"], "utc_hour": row["utc_hour"]}
                for row in stratum_rows
                if row["limited_controls"]
            ],
            "notes": [
                "Randomized controls are descriptive references, not probabilities or significance claims.",
                "The detector uses first observed arrivals and can miss later coordinated activity.",
                "Post families do not eliminate dependence between different posts about one event.",
                "The null preserves hourly activity and thread bursts, but not every topic or reply preference.",
            ],
        }
    )
    if not settings["randomizations"]:
        diagnostics["notes"].append("No randomized controls were run; no edges are retained.")
    source_fingerprint = _digest(dataset)
    software_versions = {
        "python": platform.python_version(),
        "networkx": importlib.metadata.version("networkx"),
    }
    return {
        "run_id": "run-"
        + _digest(
            {
                "input": fingerprint,
                "source": source_fingerprint,
                "dataset_id": dataset.get("dataset_id"),
                "config": settings,
                "version": 1,
            }
        )[:16],
        "config": settings,
        "input_fingerprint": fingerprint,
        "source_fingerprint": source_fingerprint,
        "software_versions": software_versions,
        "manifest": {
            "detector_version": 1,
            "software_versions": software_versions,
            "source_fingerprint": source_fingerprint,
            "input_fingerprint": fingerprint,
        },
        "dataset_id": str(dataset.get("dataset_id", "unnamed-dataset")),
        "synthetic": synthetic,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "pairs": pairs,
        "groups": groups,
        "diagnostics": diagnostics,
    }
