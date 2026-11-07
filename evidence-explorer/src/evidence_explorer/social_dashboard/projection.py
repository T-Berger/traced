"""Pure saved-case aggregation with explicit time, identity and annotation gaps."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..review.contracts import EvidenceBundle, Record
from ..source_contracts import PLATFORM_LABELS, safe_source_url
from .contracts import (
    DashboardCoverage,
    DashboardEdge,
    DashboardFilters,
    DashboardOption,
    DashboardOptions,
    DashboardProjection,
    DashboardRow,
    DashboardSentimentTarget,
    DashboardSummary,
    DashboardTimeBucket,
    SentimentCounts,
)

MAX_EDGES = 200
_LABELS = {"positive", "negative", "neutral", "mixed", "unclear"}
_LIMITATIONS = (
    "Counts describe saved record snapshots, not reach, unique people or public opinion.",
    "Topic/narrative search is a literal text filter, not an inferred topic classification.",
    "Missing, invalid or timezone-free timestamps remain unknown; time bases are not substituted.",
    (
        "Date-only values retain their supplied precision but are unsupported in timezone-based "
        "bins; no hour or timezone is invented."
    ),
    "Unknown timestamps stay outside the time chart and remain visible in the record table.",
    "Timestamp or bucket boundaries beyond the supported calendar remain visible as unsupported.",
    "Multiple saved versions and reposts are not independent corroboration.",
    (
        "Sentiment describes explicitly saved annotations for one selected run and its targets; "
        "it is not a truth assessment, human approval or population estimate."
    ),
    (
        "Different sentiment targets are reported separately; a combined trend is unavailable "
        "when the selection contains multiple targets."
    ),
    "Relationships show declared saved links and shared URLs, not inferred coordination.",
    "Shared-URL groups use a bounded star display; edges do not imply communication or copying.",
    "This view uses saved evidence only and creates no live monitor, collection or alert.",
)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _key(kind: str, *parts: str) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
    return kind + ":" + hashlib.sha256(encoded).hexdigest()


def _time(value: Any) -> tuple[datetime | None, str]:
    if value is None or isinstance(value, str) and not value.strip():
        return None, "missing"
    if not isinstance(value, str):
        return None, "invalid"
    try:
        if date.fromisoformat(value).isoformat() == value:
            return None, "date_only"
    except ValueError:
        pass
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            return None, "naive"
        return result.astimezone(UTC), "known"
    except OverflowError:
        return None, "unsupported"
    except ValueError:
        return None, "invalid"


def _identity(record: Record) -> tuple[str, str | None, str | None, str, str]:
    metadata = record.metadata
    supplied = metadata.get("platform") or metadata.get("source_platform")
    platform = supplied if isinstance(supplied, str) and supplied in PLATFORM_LABELS else "unknown"
    swarm = metadata.get("swarm")
    swarm = swarm if isinstance(swarm, dict) else {}
    channel = _text(metadata.get("channel_id")) or _text(swarm.get("channel_key"))
    author = _text(metadata.get("author_id")) or _text(swarm.get("account_key"))
    channel_key = _key("channel", platform, channel) if channel and platform != "unknown" else None
    author_key = _key("author", platform, channel, author) if channel_key and author else None
    channel_name = _text(metadata.get("channel_name")) or _text(swarm.get("name"))
    channel_label = "Unknown channel"
    if channel_key:
        name = (
            f"{channel_name} ({channel})" if channel_name and channel_name != channel else channel
        )
        channel_label = f"{PLATFORM_LABELS[platform]} · {name}"
    provenance = metadata.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    author_name = _text(provenance.get("author_label"))
    author_label = "Unknown author"
    if author_key:
        name = f"{author_name} ({author})" if author_name and author_name != author else author
        author_label = f"{channel_label} · {name}"
    return platform, channel_key, author_key, channel_label, author_label


def _annotations(bundle: EvidenceBundle, run_id: str | None):
    runs = {run.id: run for run in bundle.runs}
    records = {record.id: record for record in bundle.records}
    fragments = {fragment.id: fragment for fragment in bundle.fragments}
    available = bool(
        run_id in runs
        and runs[run_id].status == "completed"
        and any(
            finding.run_id == run_id and finding.details.get("domain") == "social_sentiment"
            for finding in bundle.findings
        )
    )
    candidates = defaultdict(list)
    invalid = set()
    if not available:
        return {}, invalid, False
    for finding in bundle.findings:
        if finding.run_id != run_id or finding.details.get("domain") != "social_sentiment":
            continue
        annotation = finding.details.get("annotation")
        if not isinstance(annotation, dict):
            continue
        rid = annotation.get("record_id")
        if not isinstance(rid, str) or rid not in records:
            continue
        record = records[rid]
        refs = annotation.get("fragment_ids")
        valid = (
            rid in runs[run_id].input_manifest
            and annotation.get("content_hash") == record.content_hash
            and isinstance(annotation.get("label"), str)
            and annotation["label"] in _LABELS
            and all(_text(annotation.get(key)) for key in ("target", "model", "prompt_version"))
            and isinstance(refs, list)
            and bool(refs)
            and len(refs) == len({ref for ref in refs if isinstance(ref, str)})
            and all(
                isinstance(ref, str)
                and ref in fragments
                and fragments[ref].record_id == rid
                and fragments[ref].selector.kind == "text"
                and bool(fragments[ref].quote)
                for ref in refs
            )
        )
        if valid:
            candidates[rid].append((finding.snapshot_id, annotation))
        else:
            invalid.add(rid)
    accepted = {}
    for rid, values in candidates.items():
        if len(values) == 1 and rid not in invalid:
            accepted[rid] = values[0]
        else:
            invalid.add(rid)
    return accepted, invalid, available


def _row(record: Record, annotation, filters: DashboardFilters) -> DashboardRow:
    platform, channel, author, channel_label, author_label = _identity(record)
    finding_id, saved = annotation if annotation else (None, {})
    origin = record.metadata.get("synthetic")
    return DashboardRow(
        id=record.id,
        kind=record.kind,
        title=_text(record.metadata.get("title")) or record.source_label,
        source_url=record.source_url,
        preview=(record.text or "")[:240],
        published_at=record.published_at,
        acquired_at=record.acquired_at,
        time_status=_time(
            record.published_at if filters.time_basis == "publication" else record.acquired_at
        )[1],
        platform=platform,
        channel_key=channel,
        channel_label=channel_label,
        author_key=author,
        author_label=author_label,
        synthetic=origin if type(origin) is bool else None,
        sentiment=saved.get("label", "unknown"),
        sentiment_target=saved.get("target"),
        sentiment_finding_id=finding_id,
        sentiment_fragment_ids=tuple(saved.get("fragment_ids", ())),
        sentiment_model=saved.get("model"),
        sentiment_prompt_version=saved.get("prompt_version"),
        sentiment_run_id=filters.sentiment_run_id if annotation else None,
    )


def _summaries(rows: list[DashboardRow], field: str) -> tuple[DashboardSummary, ...]:
    groups = defaultdict(list)
    for row in rows:
        groups[getattr(row, field)].append(row)
    summaries = []
    for key, members in groups.items():
        label = (
            PLATFORM_LABELS.get(key, "Unknown platform")
            if field == "platform"
            else min(getattr(row, field.replace("_key", "_label")) for row in members)
        )
        summaries.append(
            DashboardSummary(
                key=key,
                label=label,
                count=len(members),
                record_ids=tuple(r.id for r in members),
            )
        )
    return tuple(sorted(summaries, key=lambda item: (-item.count, item.label, item.key or "")))


def _options(summaries):
    return tuple(
        DashboardOption(value=item.key, label=item.label, count=item.count)
        for item in summaries
        if item.key is not None
    )


def _sentiments(rows: list[DashboardRow]) -> SentimentCounts:
    counts = defaultdict(int)
    for row in rows:
        counts[row.sentiment] += 1
    return SentimentCounts(**counts)


def _sentiment_targets(
    rows: list[DashboardRow],
) -> tuple[DashboardSentimentTarget, ...]:
    grouped = defaultdict(list)
    for row in rows:
        if row.sentiment_target is not None:
            grouped[row.sentiment_target].append(row)
    return tuple(
        DashboardSentimentTarget(
            target=target,
            count=len(members),
            record_ids=tuple(row.id for row in members),
            sentiment_counts=_sentiments(members),
        )
        for target, members in sorted(grouped.items())
    )


def _bucket(moment: datetime, unit: str) -> tuple[date, date]:
    start = moment.date()
    if unit == "week":
        start -= timedelta(days=start.weekday())
        end = start + timedelta(days=7)
    elif unit == "month":
        start = start.replace(day=1)
        end = date(start.year + int(start.month == 12), start.month % 12 + 1, 1)
    else:
        end = start + timedelta(days=1)
    return start, end


def _edges(bundle: EvidenceBundle, rows: list[DashboardRow]):
    ids = {row.id for row in rows}
    records = {record.id: record for record in bundle.records if record.id in ids}
    fragment_records = {fragment.id: fragment.record_id for fragment in bundle.fragments}
    edges = {}

    def add(source, target, relation, basis, link_id=None):
        if source not in ids or target not in ids or source == target:
            return
        if relation != "documented_repost":
            source, target = sorted((source, target))
        key = (source, target, relation)
        edges.setdefault(key, {"bases": set(), "links": set()})
        edges[key]["bases"].add(basis)
        if link_id:
            edges[key]["links"].add(link_id)

    for link in bundle.links:
        if link.relation not in {"documented_repost", "same_content"}:
            continue
        source = (
            link.source.id
            if link.source.type == "record"
            else fragment_records.get(link.source.id)
            if link.source.type == "fragment"
            else None
        )
        target = (
            link.target.id
            if link.target.type == "record"
            else fragment_records.get(link.target.id)
            if link.target.type == "fragment"
            else None
        )
        add(source, target, link.relation, link.basis, link.id)
    native = defaultdict(list)
    urls = defaultdict(list)
    for record in records.values():
        metadata = record.metadata
        identity = tuple(metadata.get(key) for key in ("platform", "channel_id", "native_id"))
        if all(_text(part) for part in identity):
            native[identity].append(record.id)
        shared = metadata.get("shared_urls")
        for url in (
            {value for value in shared if isinstance(value, str)}
            if isinstance(shared, list)
            else ()
        ):
            try:
                safe_source_url(url)
            except ValueError:
                continue
            urls[url].append(record.id)
    for record in records.values():
        repost = record.metadata.get("repost_of")
        if isinstance(repost, dict):
            identity = tuple(repost.get(key) for key in ("platform", "channel_id", "native_id"))
            if all(_text(part) for part in identity):
                for original in native.get(identity, ()):
                    add(
                        original,
                        record.id,
                        "documented_repost",
                        "Saved repost identity.",
                    )
    for url, members in urls.items():
        ordered = sorted(members)
        for member in ordered[1:]:
            add(ordered[0], member, "shared_url", f"Same saved shared URL: {url}")
    result = tuple(
        DashboardEdge(
            id=_key("relationship", *key),
            source_record_id=key[0],
            target_record_id=key[1],
            relation=key[2],
            basis="; ".join(sorted(saved["bases"])),
            link_ids=tuple(sorted(saved["links"])),
        )
        for key, saved in sorted(edges.items())[:MAX_EDGES]
    )
    return result, len(edges)


def project_dashboard(
    bundle: EvidenceBundle,
    *,
    excluded_record_ids=(),
    filters: DashboardFilters | dict | None = None,
) -> DashboardProjection:
    """Aggregate complete saved input; unknown time remains outside dated buckets."""
    bundle = EvidenceBundle.model_validate(bundle)
    selected = DashboardFilters.model_validate(filters or {})
    excluded = set(excluded_record_ids)
    annotations, invalid, run_available = _annotations(bundle, selected.sentiment_run_id)
    records = sorted(bundle.records, key=lambda record: record.id)
    available = [
        _row(record, annotations.get(record.id), selected)
        for record in records
        if record.id not in excluded
    ]
    by_id = {record.id: record for record in records}
    matching = []
    unknown_time = 0
    outside = 0
    buckets = defaultdict(list)
    query = selected.text_query.casefold()
    for row in available:
        if (
            selected.platform is not None
            and row.platform != selected.platform
            or selected.channel_key is not None
            and row.channel_key != selected.channel_key
            or selected.author_key is not None
            and row.author_key != selected.author_key
            or query
            and query not in (by_id[row.id].text or "").casefold()
        ):
            continue
        moment, _ = _time(
            row.published_at if selected.time_basis == "publication" else row.acquired_at
        )
        if moment is None:
            unknown_time += 1
        elif (
            selected.start
            and moment.date().isoformat() < selected.start
            or selected.end
            and moment.date().isoformat() > selected.end
        ):
            outside += 1
            continue
        bucket_key = None
        if moment is not None:
            try:
                bucket_key = _bucket(moment, selected.bucket)
            except (OverflowError, ValueError):
                row = row.model_copy(update={"time_status": "unsupported"})
                unknown_time += 1
        matching.append(row)
        if bucket_key is not None:
            buckets[bucket_key].append(row)
    time_buckets = tuple(
        DashboardTimeBucket(
            start=start.isoformat(),
            end=end.isoformat(),
            label=start.isoformat(),
            count=len(members),
            record_ids=tuple(row.id for row in members),
            sentiment_counts=_sentiments(members),
        )
        for (start, end), members in sorted(buckets.items())
    )
    edges, total_edges = _edges(bundle, matching)
    sentiment_runs = []
    for run in bundle.runs:
        count = sum(
            finding.run_id == run.id and finding.details.get("domain") == "social_sentiment"
            for finding in bundle.findings
        )
        if count and run.status == "completed":
            sentiment_runs.append(
                DashboardOption(value=run.id, label=f"{run.method} · {run.created_at}", count=count)
            )
    target_summaries = _sentiment_targets(matching)
    return DashboardProjection(
        case_id=bundle.case.id,
        case_fingerprint=bundle.fingerprint(),
        filters=selected,
        rows=tuple(matching),
        time_buckets=time_buckets,
        platforms=_summaries(matching, "platform"),
        channels=_summaries(matching, "channel_key"),
        authors=_summaries(matching, "author_key"),
        sentiment_counts=_sentiments(matching),
        sentiment_targets=target_summaries,
        sentiment_comparable=len(target_summaries) <= 1,
        edges=edges,
        options=DashboardOptions(
            platforms=_options(_summaries(available, "platform")),
            channels=_options(_summaries(available, "channel_key")),
            authors=_options(_summaries(available, "author_key")),
            sentiment_runs=tuple(sorted(sentiment_runs, key=lambda option: option.value)),
        ),
        coverage=DashboardCoverage(
            total_records=len(records),
            excluded_records=sum(record.id in excluded for record in records),
            eligible_records=len(available),
            matching_records=len(matching),
            unknown_timestamp_records=unknown_time,
            date_only_timestamp_records=sum(row.time_status == "date_only" for row in matching),
            missing_timestamp_records=sum(row.time_status == "missing" for row in matching),
            invalid_timestamp_records=sum(row.time_status == "invalid" for row in matching),
            naive_timestamp_records=sum(row.time_status == "naive" for row in matching),
            unsupported_timestamp_records=sum(row.time_status == "unsupported" for row in matching),
            outside_date_records=outside,
            unknown_channel_records=sum(row.channel_key is None for row in matching),
            unknown_author_records=sum(row.author_key is None for row in matching),
            unknown_sentiment_records=sum(row.sentiment == "unknown" for row in matching),
            invalid_annotation_records=sum(row.id in invalid for row in matching),
            relationship_total=total_edges,
            relationship_shown=len(edges),
            sentiment_run_available=run_available,
        ),
        limitations=_LIMITATIONS,
    )
