"""Render saved review evidence without storage access or application mutations."""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from dash import html

from .models import ReviewJob, SourceGroup
from .selectors import Bundle, EvidenceIndex, evidence_ids, finding_id


def badge(text: object, tone: str = "neutral") -> html.Span:
    return html.Span(str(text), className=f"rv-badge rv-badge--{tone}")


def empty(title: str, detail: str) -> html.Div:
    return html.Div([html.Strong(title), html.P(detail)], className="rv-empty")


def record_origin_label(synthetic: object) -> str:
    """Describe only an explicit record flag; a case label never supplies it."""
    if synthetic is True:
        return "Synthetic record"
    if synthetic is False:
        return "Collected source; factual accuracy requires review"
    return "Record origin not specified"


def record_button(record: Bundle, location: str, label: str | None = None) -> html.Button:
    rid = str(record.get("id", ""))
    return html.Button(
        label or record.get("source_label") or rid,
        id={"type": "rv-record", "rid": rid, "location": location},
        n_clicks=0,
        className="rv-text-button",
    )


def finding_queue(
    bundle: Bundle, selected: str | None, affected: Collection[str], decisions: Bundle | None = None
) -> html.Div | list[html.Button]:
    findings = bundle.get("findings", [])
    if not findings:
        return empty("No findings yet", "Import an assessment with source-linked findings.")
    result = []
    for finding in findings:
        fid = finding_id(finding)
        kind = str(finding.get("kind", "inference")).replace("_", " ")
        badges = [badge(kind, "blue" if kind == "observation" else "amber")]
        decision = (decisions or {}).get(fid)
        if decision:
            badges.append(
                badge("Reviewed · " + str(decision.get("disposition", "")).title(), "green")
            )
        elif fid in affected:
            badges.append(badge("Needs review", "amber"))
        else:
            badges.append(badge("Draft"))
        result.append(
            html.Button(
                [
                    html.Span(badges, className="rv-badges"),
                    html.Strong(finding.get("statement", "Untitled finding")),
                ],
                id={"type": "rv-finding", "fid": fid},
                n_clicks=0,
                className="rv-finding" + (" is-selected" if fid == selected else ""),
                **{"aria-pressed": str(fid == selected).lower()},
            )
        )
    return result


def evidence_graph(
    bundle: Bundle,
    selected: str | None,
    affected: Collection[str],
    excluded: Collection[str],
    *,
    active_record_id: str | None = None,
) -> list[dict[str, Any]]:
    """Compatibility adapter for the bounded, provenance-preserving graph model."""
    from .graph import build_evidence_graph

    return build_evidence_graph(
        bundle, selected, affected, excluded, active_record_id=active_record_id
    ).elements


def highlight_text(text: str, fragments: Sequence[Bundle]) -> tuple[list[str | html.Mark], bool]:
    """Only highlight a quote when the stored character selector matches exactly."""
    ranges = []
    for fragment in fragments:
        selector = fragment.get("selector") or {}
        start, end = selector.get("start"), selector.get("end")
        quote = fragment.get("quote", "")
        if selector.get("kind", "text") == "text" and type(start) is int and type(end) is int:
            if 0 <= start < end <= len(text) and text[start:end] == quote:
                ranges.append((start, end))
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    children = []
    position = 0
    for start, end in merged:
        children.extend([text[position:start], html.Mark(text[start:end])])
        position = end
    children.append(text[position:])
    return children, bool(merged)


def _publication_label(value: str | None) -> str:
    if not value:
        return "Publication date unknown"
    try:
        date = datetime.fromisoformat(value)
    except ValueError:
        return "Publication date format unavailable"
    return f"Published {date.day} {date:%b %Y}"


def _source_passages(text: str, highlighted: list[str | html.Mark], exact: bool) -> list:
    full_record = html.Div(highlighted, className="rv-source-text")
    if len(text) <= 600:
        return [
            html.Div(
                badge("Exact saved passage", "green") if exact else badge("Saved record context"),
                className="rv-source-caption",
            ),
            full_record,
        ]
    passages = [html.P(mark) for mark in highlighted if isinstance(mark, html.Mark)]
    return [
        html.Div(
            badge("Exact saved passages", "green") if exact else badge("Opening of saved record"),
            className="rv-source-caption",
        ),
        html.Div(passages if exact else text[:400].rstrip() + "…", className="rv-source-text"),
        html.Details(
            [html.Summary("Read full saved record"), full_record],
            className="rv-details",
        ),
    ]


def source_inspector(
    bundle: Bundle,
    record_id: str | None,
    finding_id: str | None,
    excluded: Collection[str],
    *,
    fragment_ids: Collection[str] | None = None,
) -> html.Div:
    record = next((r for r in bundle.get("records", []) if str(r["id"]) == record_id), None)
    if not record:
        return empty("Choose a source", "Select a source in the evidence path to inspect its text.")
    _, active_ids = evidence_ids(bundle, finding_id)
    fragments = [
        f
        for f in bundle.get("fragments", [])
        if str(f.get("record_id")) == record_id and str(f.get("id")) in active_ids
    ]
    if fragment_ids is not None:
        fragments = [fragment for fragment in fragments if fragment["id"] in fragment_ids]
    text = str(record.get("text") or "Saved text is unavailable for this record.")
    highlighted, exact = highlight_text(text, fragments)
    published_at = record.get("published_at")
    file_only = record.get("artifact_ref") and not record.get("text")
    source_url = str(record.get("source_url") or "")
    link = (
        html.A(
            "Open original ↗",
            href=source_url,
            target="_blank",
            rel="noopener noreferrer",
            className="rv-text-button",
        )
        if urlparse(source_url).scheme in {"http", "https"}
        else html.Span("Original link unavailable", className="rv-muted")
    )
    return html.Div(
        [
            html.Div(
                [
                    badge(str(record.get("kind", "record")).replace("_", " ")),
                    *([badge("Excluded in scenario", "amber")] if record_id in excluded else []),
                ],
                className="rv-badges",
            ),
            html.H3(record.get("source_label") or "Saved record"),
            html.P(
                "Record provenance: "
                + record_origin_label(record.get("metadata", {}).get("synthetic"))
                + ".",
                className="rv-small rv-muted",
            ),
            *(
                [html.P(_publication_label(published_at), className="rv-small rv-muted")]
                if published_at or not file_only
                else []
            ),
            link,
            *(
                [html.P("Supplied local media · exact saved file", className="rv-source-caption")]
                if file_only
                else _source_passages(text, highlighted, exact)
            ),
            html.Details(
                [
                    html.Summary("Source dates and references"),
                    html.Dl(
                        [
                            html.Dt("Publication timestamp"),
                            html.Dd(published_at or "Not supplied"),
                            html.Dt("Acquired"),
                            html.Dd(record.get("acquired_at") or "Not supplied"),
                            html.Dt("Record ID"),
                            html.Dd(html.Code(record_id)),
                            *(
                                [
                                    html.Dt("Content hash"),
                                    html.Dd(html.Code(record["content_hash"])),
                                ]
                                if record.get("content_hash")
                                else []
                            ),
                            *(
                                [
                                    html.Dt("Fragment IDs"),
                                    html.Dd([html.Div(html.Code(f["id"])) for f in fragments]),
                                ]
                                if fragments
                                else []
                            ),
                        ],
                        className="rv-metadata",
                    ),
                ],
                className="rv-details",
            ),
        ]
    )


def source_relations(bundle: Bundle, groups: Sequence[Bundle]) -> html.Div | list[html.Details]:
    records = {str(r["id"]): r for r in bundle.get("records", [])}
    if not groups:
        return empty(
            "No source groups found", "Similar wording alone does not establish an origin."
        )
    cards = []
    for index, group in enumerate(groups):
        members = group.get("record_ids", group.get("members", group.get("member_ids", [])))
        ids = [
            str(m.get("id", m.get("record_id", ""))) if isinstance(m, dict) else str(m)
            for m in members
        ]
        kind = str(group.get("kind", group.get("relation", group.get("type", "source group"))))
        label = group.get("label") or group.get("title") or kind.replace("_", " ")
        cards.append(
            html.Details(
                [
                    html.Summary([html.Strong(label), badge(f"{len(ids)} records")]),
                    html.P(
                        group.get("explanation")
                        or group.get("reason")
                        or group.get("basis")
                        or "Inspect individual records and the relationship that connects them.",
                        className="rv-muted",
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    record_button(
                                        records.get(rid, {"id": rid}), f"group-{index}-{j}"
                                    ),
                                    html.Span(
                                        str(
                                            records.get(rid, {}).get("published_at")
                                            or "Time unknown"
                                        ),
                                        className="rv-muted rv-small",
                                    ),
                                ],
                                className="rv-group-member",
                            )
                            for j, rid in enumerate(ids)
                        ]
                    ),
                ],
                className="rv-source-group",
                open=index == 0,
            )
        )
    return cards


def job_panel(job: Bundle | None, bundle: Bundle | None = None):
    if not job:
        return empty("Ready to challenge", "Check this finding against records in the saved case.")
    status = str(job.get("status", job.get("state", "queued")))
    result = job.get("result") or job.get("result_json") or {}
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = {"message": result}
    if not isinstance(result, dict):
        result = {"checks": result if isinstance(result, list) else []}
    checks = result.get("checks", result.get("findings", result.get("issues", [])))
    if isinstance(checks, dict):
        checks = [checks]
    mode = str(job.get("mode", (job.get("payload") or {}).get("mode", "demo")))
    parts = [
        html.Div(
            [
                badge(
                    status.replace("_", " "),
                    "amber" if status in {"queued", "running", "pending"} else "neutral",
                ),
                badge(
                    {
                        "demo": "Deterministic demo",
                        "ollama": "Local model",
                        "openrouter": "OpenRouter",
                    }.get(mode, "Model review"),
                    "blue",
                ),
            ],
            className="rv-badges",
        )
    ]
    if mode != "demo":
        scope = result.get("search_scope") or {}
        payload = job.get("payload") or {}
        actual_model = result.get("actual_model") or scope.get("actual_model") or scope.get("model")
        requested_model = (
            result.get("requested_model") or scope.get("requested_model") or payload.get("model")
        )
        identity = actual_model or requested_model
        if identity:
            parts.append(
                html.P(
                    f"{'Model used' if actual_model else 'Requested model'}: {identity}",
                    className="rv-small rv-mono rv-model-identity",
                )
            )
        if status in {"queued", "running"}:
            parts.append(
                html.P(
                    "The check runs in the background. "
                    "You can inspect the evidence while it works.",
                    className="rv-small rv-muted",
                )
            )
    progress = job.get("progress")
    if progress is not None:
        parts.append(html.P(str(progress), className="rv-muted"))
    if job.get("error"):
        parts.append(
            html.Div(str(job["error"]), className="rv-notice rv-notice--error", role="alert")
        )
    fragments = {f["id"]: f for f in (bundle or {}).get("fragments", [])}
    for index, check in enumerate(checks or []):
        if not isinstance(check, dict):
            continue
        kind = check.get("finding_type", check.get("type", check.get("verdict", "Review finding")))
        explanation = check.get(
            "explanation", check.get("short_explanation", check.get("reason", ""))
        )
        references = list(check.get("record_ids", []))
        for fragment_id in check.get("fragment_ids", []):
            if fragment_id in fragments:
                references.append(fragments[fragment_id]["record_id"])
        if check.get("record_id"):
            references = [check["record_id"], *references]
        parts.append(
            html.Div(
                [
                    badge(str(kind).replace("_", " "), "amber"),
                    html.P(explanation or check.get("message", "Inspect the linked evidence.")),
                    *(
                        [html.Blockquote(check["quote"])]
                        if check.get("quote")
                        else [html.Blockquote(quote) for quote in check.get("quotes", [])]
                    ),
                    html.Div(
                        [
                            record_button(
                                {"id": str(rid)}, f"check-{index}-{j}", f"Inspect {rid} →"
                            )
                            for j, rid in enumerate(dict.fromkeys(references))
                        ]
                    ),
                ],
                className="rv-check",
            )
        )
    scope = result.get("search_scope", result.get("scope"))
    if scope:
        if isinstance(scope, dict):
            searched = scope.get("record_count", len(scope.get("record_ids", [])))
            total = scope.get("total_record_count", "available")
            excluded = len(scope.get("excluded_record_ids", []))
            method = "deterministic fixture" if mode == "demo" else "model check"
            summary = (
                f"Reviewed {searched} of {total} saved records · {excluded} excluded · {method}"
            )
            parts.append(html.P(summary, className="rv-small rv-muted"))
            for limitation in scope.get("limitations", []):
                parts.append(html.P(str(limitation), className="rv-small rv-muted"))
            parts.append(
                html.Details(
                    [
                        html.Summary("Technical audit details"),
                        html.Pre(json.dumps(scope, indent=2, ensure_ascii=False)),
                    ],
                    className="rv-audit-details",
                )
            )
        else:
            parts.append(html.P("Search scope: " + str(scope), className="rv-small rv-muted"))
    if not checks and status in {"completed", "complete", "succeeded", "done"}:
        parts.append(
            html.P(
                result.get("message")
                or "No issue was returned by this search. Review its coverage before acting."
            )
        )
    if result.get("stale") or job.get("stale"):
        parts.append(
            html.Div(
                "This result belongs to an earlier version. Run a new check.",
                className="rv-notice rv-notice--error",
                role="alert",
            )
        )
    return parts


def review_history(events, selected_finding, scenario_id, scenario_version, case_fingerprint=None):
    """Approval belongs to one case fingerprint and scenario version."""
    reviews = [
        event.get("payload", {})
        for event in events
        if event.get("event_type") == "finding_reviewed"
        and event.get("payload", {}).get("finding_id") == selected_finding
    ]
    current = [
        review
        for review in reviews
        if scenario_id is not None
        and review.get("scenario_id") == scenario_id
        and review.get("scenario_version") == scenario_version
        and case_fingerprint is not None
        and review.get("case_fingerprint") == case_fingerprint
    ]
    previous = [review for review in reviews if review not in current]
    labels = {"use": "Use in briefing", "qualify": "Use with qualification", "withhold": "Withhold"}

    def decision(review, previous_version=False):
        return html.Div(
            [
                html.Div(
                    [
                        badge(
                            labels.get(review.get("disposition"), "Reviewed"),
                            "neutral" if previous_version else "green",
                        ),
                        badge(
                            "Previous scenario / version"
                            if previous_version
                            else "Current version",
                            "amber" if previous_version else "green",
                        ),
                    ],
                    className="rv-badges",
                ),
                html.P(review.get("reason", "")),
                html.Span(review.get("created_at", ""), className="rv-small rv-muted"),
            ],
            className="rv-history-item",
        )

    children = [html.H3("Saved review")]
    children.append(
        decision(current[-1])
        if current
        else html.P("No decision recorded for this scenario version.", className="rv-muted")
    )
    if previous:
        children.append(
            html.Details(
                [
                    html.Summary(f"Previous reviews ({len(previous)})"),
                    *[decision(review, True) for review in reversed(previous[-5:])],
                ]
            )
        )
    return html.Div(children, className="rv-saved-review")


def render_finding_queue(index: EvidenceIndex, selected: str | None, affected: Collection[str]):
    """Serialize finding display fields only at the component boundary."""
    return finding_queue(
        {"findings": [finding.model_dump(mode="json") for finding in index.findings.values()]},
        selected,
        affected,
    )


def render_evidence_graph(
    index: EvidenceIndex, selected: str | None, affected: Collection[str], excluded: Collection[str]
):
    # The graph needs labels and references, never all saved source text.
    return evidence_graph(
        {
            "findings": [
                {"snapshot_id": finding.snapshot_id, "statement": finding.statement}
                for finding in index.findings.values()
            ],
            "records": [
                {"id": record.id, "source_label": record.source_label}
                for record in index.records.values()
            ],
            "fragments": [
                {"id": fragment.id, "record_id": fragment.record_id}
                for fragment in index.fragments.values()
            ],
            "links": [link.model_dump(mode="json") for link in index.links_to(selected)],
        },
        selected,
        affected,
        excluded,
    )


def render_source_inspector(
    index: EvidenceIndex,
    record_id: str | None,
    finding_id: str | None,
    excluded: Collection[str],
):
    record = index.records.get(record_id)
    _, fragment_ids = index.evidence_for(finding_id)
    return source_inspector(
        {
            "records": [record.model_dump(mode="json")] if record else [],
            "fragments": [
                index.fragments[fid].model_dump(mode="json")
                for fid in fragment_ids
                if index.fragments[fid].record_id == record_id
            ],
            "links": [link.model_dump(mode="json") for link in index.links_to(finding_id)],
        },
        record_id,
        finding_id,
        excluded,
    )


def render_source_relations(index: EvidenceIndex, groups: Sequence[SourceGroup]):
    relevant = {rid for group in groups for rid in group.record_ids}
    return source_relations(
        {
            "records": [
                {
                    "id": record.id,
                    "source_label": record.source_label,
                    "published_at": record.published_at,
                }
                for rid, record in index.records.items()
                if rid in relevant
            ]
        },
        [group.model_dump(mode="json") for group in groups],
    )


def render_job(job: ReviewJob | None, fragment_records: dict[str, str] | None = None):
    """A polling response needs job state and cited references, not its source bundle."""
    return job_panel(
        job.model_dump(mode="json", exclude_unset=True) if job else None,
        {
            "fragments": [
                {"id": fid, "record_id": rid} for fid, rid in (fragment_records or {}).items()
            ]
        },
    )
