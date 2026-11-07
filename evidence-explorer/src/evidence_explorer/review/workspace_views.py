"""Small, source-neutral renderers for Cases, Review, Compare, and Brief."""

from __future__ import annotations

from dash import html

from .evidence_roles import evidence_source_cards
from .navigation import NextAction, navigation_context
from .presentation import badge, empty, record_button, record_origin_label
from .selectors import reference
from .workflow import assessment_context, assessment_decisions, brief_projection, case_type

TYPE_LABELS = {
    "swarm": "Behavioral analysis",
    "media": "Historical media",
    "investigation": "Agent investigation",
    "sources": "Multi-platform sources",
}


def case_cards(bundles: list[dict], selected: str | None):
    return [
        html.Section(
            [
                html.Div(
                    [
                        badge(TYPE_LABELS[bundle.get("case_type") or case_type(bundle)]),
                        *(
                            [badge("Synthetic example", "amber")]
                            if bundle["case"].get("synthetic")
                            else []
                        ),
                    ],
                    className="rv-badges",
                ),
                html.H2(bundle["case"]["title"]),
                html.P(bundle["case"].get("question", ""), className="rv-muted"),
                html.P(
                    f"{bundle.get('record_count', len(bundle.get('records', [])))} saved records · "
                    f"{bundle.get('finding_count', len(bundle.get('findings', [])))} findings",
                    className="rv-small rv-muted",
                ),
                html.Button(
                    "Continue review" if bundle["case"]["id"] == selected else "Open case",
                    id={"type": "rv-open-case", "cid": bundle["case"]["id"]},
                    n_clicks=0,
                    className="rv-button rv-button--outline",
                ),
            ],
            className="rv-case-card",
        )
        for bundle in bundles
    ] or empty("No cases yet", "Import a case to begin reviewing its saved evidence.")


def _run_label(run: dict | None) -> str:
    """Describe only the resolved active run, independent of case-wide content."""
    if run is None:
        return "Analysis run unavailable"
    method = run.get("method")
    if method is None or (isinstance(method, str) and not method.strip()):
        return "Method not supplied"
    if not isinstance(method, str):
        return "Saved analysis"
    return {
        "swarm_detector.detector.analyze": TYPE_LABELS["swarm"],
        "supplied-media-metadata-and-byte-hashes": TYPE_LABELS["media"],
        "saved_specialist_review_snapshot": TYPE_LABELS["media"],
        "synthetic-specialist-review-handoff": TYPE_LABELS["media"],
        "saved_source_collection": TYPE_LABELS["sources"],
        "evidence_explorer_repository_snapshot": TYPE_LABELS["investigation"],
        "external_claim_check": TYPE_LABELS["investigation"],
    }.get(method, "Saved analysis")


def context_header(view: dict):
    context = assessment_context(view)
    bundle = view["bundle"]
    run = next(
        (
            r
            for r in bundle.get("runs", [])
            if context["base_run_id"] and r.get("id") == context["base_run_id"]
        ),
        None,
    )
    details = run or {}
    return [
        html.Strong(bundle.get("case", {}).get("title", "Choose a case")),
        html.Span(f"{_run_label(run)} · {context['label']}", className="rv-small rv-muted"),
        html.Details(
            [
                html.Summary("Dataset and analysis version"),
                html.P(
                    str(
                        bundle.get("case", {}).get("release_id")
                        or bundle.get("case", {}).get("dataset_version")
                        or "Supplied case snapshot"
                    )
                ),
                html.P(
                    f"Method: {details.get('method') or 'Not supplied'} · "
                    f"version {details.get('method_version') or 'Not supplied'}"
                ),
                html.P(f"Run: {context['base_run_id'] or 'Not supplied'}", className="rv-mono"),
            ],
            className="rv-audit-details",
        ),
    ]


def finding_summary(bundle: dict, finding: dict):
    selected = finding.get("snapshot_id")
    bases = []
    premises = []
    findings = {f["snapshot_id"]: f for f in bundle.get("findings", [])}
    for link in bundle.get("links", []):
        kind, ident = reference(link["target"])
        if (
            kind != "finding"
            or ident != selected
            or link["relation"] not in {"supports", "depends_on"}
        ):
            continue
        source_kind, source_id = reference(link["source"])
        if source_kind == "finding" and source_id in findings:
            premises.append(findings[source_id]["statement"])
        if link.get("basis") and link["basis"] not in bases:
            bases.append(link["basis"])
    assumptions = finding.get("details", {}).get("assumptions", [])
    return [
        html.H3("Evidence basis"),
        html.P(
            bases[0]
            if bases
            else "Inspect the linked records and the evidence behind this statement."
        ),
        *(
            [
                html.Details(
                    [
                        html.Summary("Linked findings"),
                        html.Ul([html.Li(p) for p in dict.fromkeys(premises)]),
                    ],
                    className="rv-details",
                )
            ]
            if premises
            else []
        ),
        *(
            [
                html.Div(
                    [
                        html.H3("Assumption to check"),
                        html.Ul([html.Li(str(a)) for a in assumptions]),
                    ],
                    className="rv-assumptions",
                )
            ]
            if assumptions
            else []
        ),
    ]


def linked_sources(bundle: dict, selected: str | None, record_id: str | None, excluded: set):
    cards = evidence_source_cards(bundle, selected)
    direct_count = sum(card["direct"] for card in cards)
    visible = min(direct_count, 3) if direct_count else 2
    if not cards:
        return empty(
            "No evidence records linked",
            "Review the stated assumption and any linked findings.",
        )

    def passage_view(passage):
        selector = passage["selector"]
        description = (
            f"Exact saved quotation · characters {selector['start']}–{selector['end']} "
            "(end exclusive)"
            if passage["exact"]
            else "Saved reference; an exact text match is not available."
        )
        return html.Div(
            [
                html.P(description, className="rv-small rv-muted"),
                html.Blockquote(passage["quote"], className="rv-evidence-card-quote")
                if passage["quote"] is not None
                else None,
                html.P(passage["id"], className="rv-mono rv-small"),
            ],
            className="rv-evidence-passage",
        )

    def card_view(card, index):
        record = card["record"]
        b = record_button(record, f"linked-{index}")
        b.className = "rv-source-link rv-evidence-card-action" + (
            " is-selected" if record["id"] == record_id else ""
        )
        b.children = [
            html.Strong(record.get("source_label") or "Saved record"),
            html.Span("Inspect saved record →", className="rv-small"),
        ]
        b.__setattr__("aria-pressed", str(record["id"] == record_id).lower())
        attribution = (
            " · ".join(
                dict.fromkeys(str(value) for value in (card["publisher"], card["domain"]) if value)
            )
            or "Publisher and domain not supplied"
        )
        passage_count = len(
            {passage["id"] for role in card["roles"] for passage in role["passages"]}
        )
        return html.Article(
            [
                b,
                html.P(attribution, className="rv-small rv-muted"),
                html.P(
                    f"Published: {record.get('published_at') or 'Unknown publication time'}",
                    className="rv-small rv-muted",
                ),
                html.P(
                    record_origin_label(record.get("metadata", {}).get("synthetic")),
                    className="rv-small rv-muted",
                ),
                html.Div(
                    [
                        *[badge(role["label"]) for role in card["roles"]],
                        *(
                            [badge("Excluded in scenario", "amber")]
                            if record["id"] in excluded
                            else []
                        ),
                    ],
                    className="rv-badges",
                ),
                html.Details(
                    [
                        html.Summary(f"Saved passages and relationship details ({passage_count})"),
                        html.P(
                            "These are saved evidence roles, not a reviewer decision or proof of "
                            "independent corroboration. Input dependency is not support "
                            "for the claim.",
                            className="rv-small rv-muted",
                        ),
                        *[
                            html.Section(
                                [
                                    html.H4(role["label"]),
                                    *[
                                        html.P(basis, className="rv-small")
                                        for basis in role["bases"]
                                    ],
                                    *[passage_view(passage) for passage in role["passages"]],
                                    html.P(
                                        "Record-level or linked-finding reference; no direct exact "
                                        "quotation supplied for this role.",
                                        className="rv-small rv-muted",
                                    )
                                    if not role["passages"]
                                    else None,
                                    html.P(" · ".join(role["links"]), className="rv-mono rv-small")
                                    if role["links"]
                                    else None,
                                ],
                                className="rv-evidence-role",
                            )
                            for role in card["roles"]
                        ],
                        html.P(record["id"], className="rv-mono rv-small"),
                    ],
                    className="rv-details rv-evidence-card-details",
                ),
                html.Details(
                    [
                        html.Summary(
                            f"Connections between saved sources ({len(card['relationships'])})"
                        ),
                        html.P(
                            "Matching text, supplied URLs and declared links describe source "
                            "relationships; they do not establish independent corroboration. "
                            "Overlap groups are bounded to 200 groups and 200 member records.",
                            className="rv-small rv-muted",
                        ),
                        *[
                            html.Section(
                                [
                                    html.H4(relation["label"]),
                                    html.P(relation["basis"], className="rv-small"),
                                    html.P(
                                        relation.get("direction") or relation.get("url"),
                                        className="rv-small",
                                    )
                                    if relation.get("direction") or relation.get("url")
                                    else None,
                                    *[
                                        record_button(
                                            peer, f"linked-relation-{index}-{number}-{peer_index}"
                                        )
                                        for peer_index, peer in enumerate(relation["peers"])
                                    ],
                                    html.P(
                                        f"{len(relation['peers'])} of {relation['peer_count']} "
                                        "related records shown.",
                                        className="rv-small rv-muted",
                                    ),
                                    html.P(relation["link_id"], className="rv-mono rv-small")
                                    if relation.get("link_id")
                                    else None,
                                ],
                                className="rv-evidence-role",
                            )
                            for number, relation in enumerate(card["relationships"])
                        ],
                    ],
                    className="rv-details rv-evidence-card-details",
                )
                if card["relationships"]
                else None,
            ],
            className="rv-evidence-card" + (" is-selected" if record["id"] == record_id else ""),
        )

    return [
        *[card_view(card, i) for i, card in enumerate(cards[:visible])],
        *(
            [
                html.Details(
                    [
                        html.Summary(f"All linked records ({len(cards)})"),
                        *[card_view(card, i + visible) for i, card in enumerate(cards[visible:])],
                    ],
                    className="rv-details rv-evidence-more",
                )
            ]
            if len(cards) > visible
            else []
        ),
    ]


def _audit_details(label, *children):
    return html.Details([html.Summary(label), *children], className="rv-audit-details")


def _comparison_citation(citation, context, records, location):
    record_id = citation["record_id"]
    selector = citation.get("selector") or {}
    selector_text = (
        f"Characters {selector['start']}–{selector['end']} (end exclusive)"
        if selector.get("kind") == "text" and "start" in selector and "end" in selector
        else "Saved selector: " + str(selector.get("kind") or "record reference")
    )
    action = NextAction(
        "source",
        "Open saved source",
        "Inspect this exact saved record as source context.",
        context or "",
        record_id=record_id,
        location=location,
    )
    return html.Li(
        [
            html.Strong(citation.get("source_label") or record_id),
            html.Blockquote(citation["quote"], className="rv-source-quote")
            if citation.get("quote") is not None
            else None,
            html.P("Record-level reference; no exact fragment supplied.", className="rv-small")
            if not citation.get("fragment_id")
            else None,
            html.Button(
                action.label,
                id=action.button_id(),
                n_clicks=0,
                className="rv-text-button",
            )
            if context and record_id in records
            else None,
            _audit_details(
                "Citation reference",
                html.P(f"Record: {record_id}", className="rv-mono"),
                html.P(f"Fragment: {citation['fragment_id']}", className="rv-mono")
                if citation.get("fragment_id")
                else None,
                html.P(selector_text),
            ),
        ],
        className="rv-brief-item",
    )


def _comparison_findings(findings, title, context, records, location):
    return html.Section(
        [
            html.H3(title),
            *[
                html.Article(
                    [
                        badge(str(finding.get("kind") or "Finding").capitalize()),
                        html.P(finding["statement"], style={"whiteSpace": "pre-wrap"}),
                        html.Ul([html.Li(issue) for issue in finding["issues"]])
                        if finding.get("issues")
                        else None,
                        html.Details(
                            [
                                html.Summary(
                                    f"Exact saved citations ({len(finding.get('evidence', []))})"
                                ),
                                html.Ul(
                                    [
                                        _comparison_citation(
                                            finding["evidence"][0],
                                            context,
                                            records,
                                            f"{location}-{index}-0",
                                        )
                                    ]
                                ),
                                html.Details(
                                    [
                                        html.Summary(
                                            f"More exact citations ({len(finding['evidence']) - 1})"
                                        ),
                                        html.Ul(
                                            [
                                                _comparison_citation(
                                                    citation,
                                                    context,
                                                    records,
                                                    f"{location}-{index}-{number}",
                                                )
                                                for number, citation in enumerate(
                                                    finding["evidence"][1:], start=1
                                                )
                                            ]
                                        ),
                                    ],
                                    className="rv-details",
                                )
                                if len(finding["evidence"]) > 1
                                else None,
                            ],
                            className="rv-details",
                        )
                        if finding.get("evidence")
                        else html.P(
                            "No exact evidence reference is supplied for this finding.",
                            className="rv-small rv-muted",
                        ),
                        _audit_details(
                            "Finding reference",
                            html.P(f"Snapshot: {finding['snapshot_id']}", className="rv-mono"),
                            html.P(f"Run: {finding['run_id']}", className="rv-mono"),
                        ),
                    ],
                    className="rv-brief-item",
                )
                for index, finding in enumerate(findings)
            ],
            html.P("No saved finding is supplied on this side.", className="rv-small rv-muted")
            if not findings
            else None,
        ],
        className="rv-comparison-column",
    )


def saved_comparison_summary(
    projection: dict, current_view: dict, location: str = "saved-comparison", *, compact=False
):
    """Inspect supplied run relationships without selecting or creating an assessment."""
    assessment = assessment_context(current_view)
    try:
        context = navigation_context(
            current_view,
            {
                "case_id": current_view.get("bundle", {}).get("case", {}).get("id"),
                "case_fingerprint": current_view.get("case_fingerprint"),
                "scenario_id": (current_view.get("scenario") or {}).get("id"),
                "scenario_version": assessment["version"],
            },
        )
    except ValueError:
        context = None
    records = {record["id"] for record in current_view.get("bundle", {}).get("records", [])}
    status = str(projection.get("scenario_status") or "Not supplied").replace("_", " ")
    content = [
        html.H3(projection.get("label") or "Saved comparison unavailable"),
        html.P(
            projection.get("summary") or "No saved comparison details are available for this view.",
            className="rv-notice",
            role="status",
        ),
        html.P(["Saved status · ", html.Strong(status.capitalize())], className="rv-small"),
        _audit_details(
            "Scenario and run references",
            html.P(
                f"Saved scenario: {projection.get('scenario_id') or 'Not selected'}",
                className="rv-mono",
            ),
            html.P(
                f"Baseline run: {projection.get('baseline_run_id') or 'Not supplied'}",
                className="rv-mono",
            ),
            html.P(
                f"Result run: {projection.get('result_run_id') or 'Not supplied'}",
                className="rv-mono",
            ),
        ),
        html.Ul([html.Li(issue) for issue in projection["issues"]])
        if projection.get("issues")
        else None,
    ]
    if not compact:
        seen = {"baseline": set(), "result": set()}
        for index, row in enumerate(projection.get("correspondences", [])):
            for side in seen:
                seen[side].update(f["snapshot_id"] for f in row[f"{side}_findings"])
            content.append(
                html.Section(
                    [
                        html.H3(
                            f"Supplied relationship {index + 1}: {row.get('relation') or 'Unknown'}"
                        ),
                        badge("Invalid supplied relationship", "amber")
                        if row.get("status") == "invalid"
                        else None,
                        _audit_details(
                            "Supplied finding references",
                            *[
                                html.P(
                                    f"Supplied {side} snapshot IDs: "
                                    + ", ".join(row.get(f"{side}_snapshot_ids", [])),
                                    className="rv-mono",
                                )
                                for side in ("baseline", "result")
                            ],
                        )
                        if row.get("status") == "invalid"
                        else None,
                        html.P(
                            row.get("basis") or "No relationship basis supplied.",
                            className="rv-small",
                        ),
                        html.P(row["uncertainty"], className="rv-small rv-muted")
                        if row.get("uncertainty")
                        else None,
                        html.Ul([html.Li(issue) for issue in row["issues"]])
                        if row.get("issues")
                        else None,
                        html.Div(
                            [
                                _comparison_findings(
                                    row[f"{side}_findings"],
                                    title,
                                    context,
                                    records,
                                    f"{location}-relationship-{index}-{side}",
                                )
                                for side, title in [
                                    ("baseline", "Baseline finding"),
                                    ("result", "Result finding"),
                                ]
                            ],
                            className="rv-comparison-grid",
                        ),
                    ],
                    className="rv-details",
                )
            )
        unpaired = {
            side: [
                finding
                for finding in projection.get(f"{side}_findings", [])
                if finding["snapshot_id"] not in seen[side]
            ]
            for side in seen
        }
        if any(unpaired.values()):
            content.append(
                html.Section(
                    [
                        html.H3("Saved findings without a supplied relationship"),
                        html.P(
                            "These are exact saved findings. "
                            "Their placement does not imply a match.",
                            className="rv-small rv-muted",
                        ),
                        html.Div(
                            [
                                _comparison_findings(
                                    unpaired[side],
                                    title,
                                    context,
                                    records,
                                    f"{location}-unpaired-{side}",
                                )
                                for side, title in [
                                    ("baseline", "Baseline findings"),
                                    ("result", "Result findings"),
                                ]
                            ],
                            className="rv-comparison-grid",
                        ),
                    ],
                    className="rv-details",
                )
            )
    if limitations := projection.get("limitations"):
        content.extend(
            [
                html.P(
                    "Reviewer decisions stay with the assessment that was reviewed.",
                    className="rv-small rv-muted",
                ),
                _audit_details(
                    "How to interpret this comparison",
                    html.Ul([html.Li(limitation) for limitation in limitations]),
                ),
            ]
        )
    return html.Section(content, **{"aria-label": "Saved baseline and result comparison"})


def compare_summary(baseline: dict, scenario: dict | None, *, current_view: dict | None = None):
    if scenario is None:
        return empty(
            "No exclusion scenario yet",
            "In Review, open a record and expand “Test without this record.” "
            "Add a reason to create a reversible scenario.",
        )
    context = assessment_context(scenario)
    records = {r["id"]: r for r in scenario["bundle"].get("records", [])}
    excluded = scenario.get("impact", {}).get("excluded_record_ids", [])
    affected = set(scenario.get("impact", {}).get("affected_finding_ids", []))
    current = assessment_decisions(scenario)
    needs_review = len(affected.difference(current))
    names = {f["snapshot_id"]: f["statement"] for f in scenario["bundle"].get("findings", [])}
    impact = html.Div(
        [
            html.Section(
                [
                    html.H2("Baseline"),
                    html.P("All saved records included."),
                    html.P(
                        f"{len(assessment_decisions(baseline))} current decisions",
                        className="rv-small rv-muted",
                    ),
                    html.P(
                        "Preserved independently of this scenario.", className="rv-small rv-muted"
                    ),
                ],
                className="rv-comparison-column",
            ),
            html.Section(
                [
                    html.H2(context["label"]),
                    html.P(f"Assessment version {context['version']}", className="rv-small"),
                    html.Div(
                        [
                            badge(f"{len(excluded)} excluded"),
                            badge(f"{len(affected)} affected"),
                            badge(f"{needs_review} to review", "amber") if needs_review else None,
                        ],
                        className="rv-badges",
                    ),
                    html.Details(
                        [
                            html.Summary(f"Excluded records ({len(excluded)})"),
                            html.Ul(
                                [
                                    html.Li(records.get(rid, {}).get("source_label", rid))
                                    for rid in excluded
                                ]
                            ),
                        ],
                        className="rv-details",
                    )
                    if excluded
                    else html.P(
                        "All records restored. Review decisions for this version separately."
                    ),
                    html.Details(
                        [
                            html.Summary(f"Affected findings ({len(affected)})"),
                            html.Ul(
                                [
                                    html.Li(
                                        [
                                            names.get(fid, fid),
                                            " · ",
                                            badge(
                                                "Reviewed" if fid in current else "Needs review",
                                                "green" if fid in current else "amber",
                                            ),
                                        ]
                                    )
                                    for fid in sorted(affected)
                                ]
                            ),
                        ],
                        className="rv-details",
                    )
                    if affected
                    else html.P("No currently excluded evidence affects these findings."),
                    html.P(
                        "Exclusions identify dependencies. The saved result keeps a separate "
                        "review context."
                        if scenario.get("saved_comparison", {}).get("result_available")
                        else "Dependency impact only. Detector results change after recomputation.",
                        className="rv-small rv-muted",
                    ),
                ],
                className="rv-comparison-column",
            ),
        ],
        className="rv-comparison-grid",
    )
    saved = scenario.get("saved_comparison")
    if (
        not saved
        or saved.get("status") == "not_selected"
        or (saved.get("status") == "not_saved" and saved.get("origin") != "imported_saved")
    ):
        return impact
    return html.Div(
        [
            html.Details(
                [
                    html.Summary(
                        f"Excluded sources and review impact · {len(excluded)} excluded · "
                        f"{len(affected)} affected"
                    ),
                    impact,
                ],
                className="rv-details",
            ),
            saved_comparison_summary(saved, current_view or scenario, location="assessment-saved"),
        ]
    )


def brief_preview(view: dict):
    projection = brief_projection(view)
    included = []
    for item in projection["included"]:
        citations = []
        seen = set()
        for citation in item["evidence"]:
            if citation["record_id"] in seen:
                continue
            seen.add(citation["record_id"])
            b = html.Button(
                citation["source_label"],
                id={"type": "rv-citation", "fid": item["finding_id"], "rid": citation["record_id"]},
                n_clicks=0,
                className="rv-text-button",
            )
            citations.append(
                html.Li(
                    [
                        b,
                        html.Span(
                            " · " + record_origin_label(citation.get("record_synthetic")),
                            className="rv-small rv-muted",
                        ),
                    ]
                )
            )
        included.append(
            html.Section(
                [
                    badge(
                        "Use with qualification"
                        if item["disposition"] == "qualify"
                        else "Use as written",
                        "green",
                    ),
                    html.H3(item["statement"]),
                    *(
                        [html.P(item["claim_origin"], className="rv-small rv-muted")]
                        if item.get("claim_origin")
                        else []
                    ),
                    html.P(item["reason"]),
                    html.Details(
                        [html.Summary(f"Exact evidence ({len(seen)} records)"), html.Ul(citations)],
                        className="rv-details",
                    ),
                ],
                className="rv-brief-item",
            )
        )
    omission_status = {
        "withheld": ("Withheld", "neutral"),
        "needs_review": ("Needs review", "amber"),
        "not_reviewed": ("Awaiting decision", "neutral"),
    }
    omissions = [
        html.Li(
            [
                html.Div(badge(*omission_status[item["status"]]), className="rv-badges"),
                html.Strong(item["statement"]),
                *(
                    [html.P(item["claim_origin"], className="rv-small rv-muted")]
                    if item.get("claim_origin")
                    else []
                ),
                html.P(item["reason"], className="rv-small rv-muted"),
            ]
        )
        for item in projection["omitted"]
    ]
    return [
        html.H2("Included in this brief"),
        html.Div(
            [
                badge(projection["assessment"]["label"]),
                badge(f"{len(included)} included", "green" if included else "neutral"),
                badge(f"{len(omissions)} omitted"),
            ],
            className="rv-badges",
        ),
        *(
            included
            or [
                empty(
                    "No statements selected yet",
                    "Save a Use or Qualify decision in Review. "
                    "Expand Omitted statements to see what is missing and why.",
                )
            ]
        ),
        html.Details(
            [html.Summary(f"Omitted statements ({len(omissions)})"), html.Ul(omissions)],
            className="rv-details",
        ),
        html.Details(
            [
                html.Summary("Saved coverage and limitations"),
                html.Dl(
                    [
                        node
                        for label, value in projection["coverage"].items()
                        for node in [
                            html.Dt(label.replace("_", " ").capitalize()),
                            html.Dd(str(value) if value is not None else "Unknown"),
                        ]
                    ],
                    className="rv-metadata",
                )
                if projection["coverage"]
                else html.P("Coverage is not recorded."),
                html.Ul([html.Li(x) for x in projection["limitations"]]),
            ],
            className="rv-details",
        ),
    ]
