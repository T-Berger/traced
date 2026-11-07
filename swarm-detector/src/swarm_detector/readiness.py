"""Readiness facts and shared report formatting for in-memory and SQL inputs."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from itertools import chain

from pydantic import BaseModel, ConfigDict


class ReadinessFacts(BaseModel):
    model_config = ConfigDict(frozen=True)

    post_count: int
    message_count: int
    channel_count: int
    account_count: int
    unknown_accounts: int
    valid_timestamps: int
    invalid_timestamps: int
    observation_start: str | None
    observation_end: str | None
    invalid_posts: int
    invalid_links: int
    predating: int
    duplicate_keys: bool
    missing_source_refs: int
    checked_event_refs: int
    unresolved_event_refs: int


def normalized_time(value) -> str:
    if not isinstance(value, str):
        raise ValueError("An observation timestamp must be text.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("A source timezone is required.")
    return parsed.astimezone(UTC).isoformat()


def facts_from_records(dataset: dict, run: dict | None) -> ReadinessFacts:
    """Compatibility path for callers already holding complete wire records."""
    posts = dataset.get("posts", [])
    messages = dataset.get("messages", [])
    channel_ids = {channel.get("channel_key") for channel in dataset.get("channels", [])}
    post_index = {post.get("post_key"): post for post in posts}
    message_index = {message.get("message_key"): message for message in messages}
    accounts = {
        message["account_key"]
        for message in messages
        if isinstance(message.get("account_key"), str)
        and message["account_key"].strip()
        and message.get("sender_type", "user") == "user"
    }
    unknown = sum(
        not isinstance(message.get("account_key"), str)
        or not message["account_key"].strip()
        or message.get("sender_type", "user") != "user"
        for message in messages
    )
    times = {}
    start = end = None
    valid = invalid = 0
    for kind, records in (("post", posts), ("message", messages)):
        for record in records:
            try:
                timestamp = normalized_time(record.get("timestamp_utc"))
            except (ValueError, TypeError, OverflowError):
                invalid += 1
                continue
            valid += 1
            start = timestamp if start is None else min(start, timestamp)
            end = timestamp if end is None else max(end, timestamp)
            times[(kind, record.get(f"{kind}_key"))] = timestamp
    invalid_links = predating = 0
    for message in messages:
        post = post_index.get(message.get("post_key"))
        if post is None or message.get("channel_key") != post.get("channel_key"):
            invalid_links += 1
            continue
        message_time = times.get(("message", message.get("message_key")))
        post_time = times.get(("post", post.get("post_key")))
        predating += int(
            message_time is not None and post_time is not None and message_time < post_time
        )
    checked = unresolved = 0
    for pair in (run or {}).get("pairs", []):
        for event in pair.get("events", []):
            for key in ("message_a", "message_b"):
                checked += 1
                message = message_index.get(event.get(key))
                unresolved += int(not message or message.get("post_key") != event.get("post_key"))
    missing_refs = sum(
        not isinstance(record.get("source_ref"), str) or not record["source_ref"].strip()
        for record in chain(posts, messages)
    )
    return ReadinessFacts(
        post_count=len(posts),
        message_count=len(messages),
        channel_count=len(channel_ids),
        account_count=len(accounts),
        unknown_accounts=unknown,
        valid_timestamps=valid,
        invalid_timestamps=invalid,
        observation_start=start,
        observation_end=end,
        invalid_posts=sum(post.get("channel_key") not in channel_ids for post in posts),
        invalid_links=invalid_links,
        predating=predating,
        duplicate_keys=len(post_index) != len(posts) or len(message_index) != len(messages),
        missing_source_refs=missing_refs,
        checked_event_refs=checked,
        unresolved_event_refs=unresolved,
    )


def readiness_from_facts(dataset: dict, run: dict | None, facts: ReadinessFacts) -> dict:
    """Summarize imported evidence without inferring unobserved archive coverage.

    Checks marked verified apply to the normalized records, not to an unseen raw
    Telegram export. This pure function does not open source locators or files.
    """
    run = run or {}
    manifest = dataset.get("manifest", {})
    manifest = manifest if isinstance(manifest, dict) else {}
    diagnostics = run.get("diagnostics", {})
    synthetic = dataset.get("synthetic") is True
    checks = []
    limitations = [note for note in manifest.get("limitations", []) if isinstance(note, str)]

    def check(key: str, label: str, status: str, detail: str) -> None:
        checks.append({"key": key, "label": label, "status": status, "detail": detail})

    def positive(value) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value > 0
        )

    account_count = facts.account_count
    unknown_accounts = facts.unknown_accounts
    cross_channel = manifest.get("identity_stable_across_channels") is True and bool(account_count)
    if not account_count:
        identity_status = "unknown"
        identity_detail = "No identifiable user accounts are available for account-level detection."
    elif cross_channel and synthetic and not unknown_accounts:
        identity_status = "verified"
        identity_detail = (
            f"{account_count} controlled fictional account IDs are stable across the synthetic channels. "
            "This does not verify identities in the real challenge archive."
        )
    elif cross_channel:
        identity_status = "limited"
        identity_detail = (
            f"{account_count} identifiable user IDs are present; cross-channel stability is declared "
            "in metadata, rather than independently established from the raw archive."
        )
    else:
        identity_status = "limited"
        identity_detail = (
            f"{account_count} identifiable user IDs are available for within-channel analysis. "
            "Cross-channel identity is unverified and that capability is disabled."
        )
    if unknown_accounts:
        identity_status = "limited" if account_count else "unknown"
        identity_detail += (
            f" {unknown_accounts} comments have missing identities or non-user senders."
        )
    check("identities", "Account identity coverage", identity_status, identity_detail)
    if identity_status != "verified":
        limitations.append(identity_detail)

    invalid_times = facts.invalid_timestamps
    precision = manifest.get("timestamp_precision_seconds")
    precision_declared = (
        manifest.get("timestamp_precision_declared", "timestamp_precision_seconds" in manifest)
        is True
    )
    if not positive(precision):
        precision = diagnostics.get("timestamp_precision_seconds", 60)
        precision_declared = False
    if not positive(precision):
        precision = 60
    if not facts.valid_timestamps:
        time_status = "unknown"
        time_detail = "No valid timezone-aware observation timestamps are available."
    elif invalid_times:
        time_status = "limited"
        time_detail = (
            f"{facts.valid_timestamps} timestamps parse with a timezone; {invalid_times} do not. "
            f"The configured precision is {precision:g} seconds."
        )
    elif not precision_declared:
        time_status = "unknown"
        time_detail = (
            f"All {facts.valid_timestamps} imported timestamps parse with a timezone, but source precision "
            f"was not declared. {precision:g} seconds is an analysis assumption, not a verified source resolution."
        )
    else:
        time_status = "verified"
        time_detail = (
            f"All {facts.valid_timestamps} imported timestamps parse with a timezone; metadata declares "
            f"{precision:g}-second precision. Formatting alone does not independently establish source resolution."
        )
    check("time_precision", "Observation times and precision", time_status, time_detail)
    if time_status != "verified":
        limitations.append(time_detail)

    invalid_posts = facts.invalid_posts
    invalid_links = facts.invalid_links
    predating = facts.predating
    duplicate_keys = facts.duplicate_keys
    if not facts.message_count or not facts.post_count:
        mapping_status = "unknown"
        mapping_detail = (
            "Post and comment records are both required to check imported thread links."
        )
    elif invalid_posts or invalid_links or predating or duplicate_keys:
        mapping_status = "limited"
        mapping_detail = (
            f"Imported links contain {invalid_posts} unknown post channels, {invalid_links} unresolved "
            f"or inconsistent parents, and {predating} comments predating parents. "
            f"Duplicate post/message keys: {'yes' if duplicate_keys else 'no'}."
        )
    else:
        mapping_status = "verified"
        mapping_detail = (
            f"All {facts.message_count} comments resolve to an imported parent with a consistent channel; "
            "none predates its parent. Original Telegram discussion-group mapping still requires source inspection."
        )
    check("thread_mapping", "Imported thread mapping", mapping_status, mapping_detail)
    if mapping_status != "verified":
        limitations.append(mapping_detail)

    missing_source_refs = facts.missing_source_refs
    checked_event_refs = facts.checked_event_refs
    unresolved_event_refs = facts.unresolved_event_refs
    if not facts.post_count and not facts.message_count:
        source_status = "unknown"
        source_detail = "No imported records are available for provenance checks."
    elif missing_source_refs or unresolved_event_refs or duplicate_keys:
        source_status = "limited"
        source_detail = (
            f"{missing_source_refs} imported records lack source locators; {unresolved_event_refs} of "
            f"{checked_event_refs} event references do not resolve to the expected imported post. "
            f"Duplicate post/message keys: {'yes' if duplicate_keys else 'no'}."
        )
    else:
        source_status = "verified"
        source_detail = (
            f"All {facts.post_count + facts.message_count} imported records have source locators; "
            f"{checked_event_refs} event references resolve internally. "
            "This check does not reopen locators in the original archive."
        )
    check("source_refs", "Imported evidence provenance", source_status, source_detail)
    if source_status != "verified":
        limitations.append(source_detail)

    # Import totals and generator counts are not full-source denominators. Require
    # an explicitly scoped and evidenced source declaration before displaying one.
    source_coverage = manifest.get("source_coverage", {})
    available = None
    denominator_scope = None
    if isinstance(source_coverage, dict):
        supplied = source_coverage.get("available_messages")
        scope = source_coverage.get("scope")
        evidence = source_coverage.get("evidence")
        if (
            isinstance(supplied, int)
            and not isinstance(supplied, bool)
            and supplied >= facts.message_count
            and isinstance(scope, str)
            and scope.strip()
            and isinstance(evidence, str)
            and evidence.strip()
        ):
            available, denominator_scope = supplied, scope
    omitted = diagnostics.get("discovery_omitted_post_count", 0)
    if not isinstance(omitted, int) or isinstance(omitted, bool) or omitted < 0:
        omitted = 0
    if available is None:
        coverage_status = "unknown"
        coverage_detail = (
            f"{facts.message_count} comments are imported. The available-source denominator is unknown; "
            "record_counts describes this import, not the full archive."
        )
    else:
        coverage_status = "limited"
        coverage_detail = (
            f"{facts.message_count} comments are imported against a declared denominator of {available} "
            f"for '{denominator_scope}'. Declaration evidence: {source_coverage['evidence']}. "
            "The original source total has not been independently recounted here."
        )
    if omitted:
        coverage_detail += (
            f" {omitted} posts were omitted from candidate discovery by the resource guard."
        )
    check("coverage", "Imported and discovery coverage", coverage_status, coverage_detail)
    limitations.append(coverage_detail)

    controls = diagnostics.get("controls_completed", 0)
    controls = controls if isinstance(controls, int) and not isinstance(controls, bool) else 0
    mixing = diagnostics.get("mixing", [])
    limited_rows = [row for row in mixing if row.get("limited_controls", 0)]
    run_matches = run.get("dataset_id") == dataset.get("dataset_id")
    if not run or controls <= 0:
        baseline_status = "unknown"
        baseline_detail = "No completed randomized controls are available for this dataset."
    elif not run_matches:
        baseline_status = "limited"
        baseline_detail = (
            "The supplied run belongs to a different dataset; its baseline is not applicable."
        )
    elif not mixing:
        baseline_status = "limited"
        baseline_detail = (
            f"{controls} controls are reported, but stratum mixing diagnostics are unavailable."
        )
    else:
        baseline_status = "limited" if limited_rows else "verified"
        baseline_detail = (
            f"{controls} controls completed; {len(limited_rows)} of {len(mixing)} channel-hour strata "
            "had limited randomization in at least one control. The reference is descriptive, "
            "not a bot probability, significance finding, or proof of hostile intent."
        )
    check("baseline", "Randomized reference and mixing", baseline_status, baseline_detail)
    if baseline_status != "verified":
        limitations.append(baseline_detail)

    if synthetic:
        limitations.insert(
            0,
            "All observations are synthetic. Successful recovery does not establish accuracy on real Telegram data.",
        )
    limitations.append(
        "The completed challenge archive and its source-specific adapter have not been verified by this imported-record report."
    )
    label = dataset.get("label") or dataset.get("dataset_id") or "Unnamed dataset"
    if synthetic and "synthetic" not in label.lower():
        label = "Synthetic demonstration — " + label
    return {
        "dataset_id": dataset.get("dataset_id"),
        "run_id": run.get("run_id"),
        "label": label,
        "synthetic": synthetic,
        "observation_start": facts.observation_start,
        "observation_end": facts.observation_end,
        "post_count": facts.post_count,
        "message_count": facts.message_count,
        "account_count": account_count,
        "channel_count": facts.channel_count,
        "checks": checks,
        "limitations": list(dict.fromkeys(limitations)),
        "coverage": {
            "imported_messages": facts.message_count,
            "available_messages": available,
            "omitted_discovery_posts": omitted,
            "denominator_scope": denominator_scope,
        },
        "capabilities": {
            "cross_channel_identity": cross_channel,
            "timestamp_precision_seconds": precision,
        },
    }
