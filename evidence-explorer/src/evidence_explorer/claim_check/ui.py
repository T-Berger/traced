"""External-evidence controls and read-only result projections for the review desk.

Callbacks enqueue work and read saved jobs; search, fetching, and inference stay in
the worker. Browser state contains job identifiers, never collected source text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dash import Dash, Input, Output, State, ctx, dcc, html, no_update

from ..review.controller import session_for_case
from ..review.origins import claim_origin_label, claim_origin_sentence
from .contracts import CheckPolicy, ClaimCheckJob
from .service import ClaimCheckService

if TYPE_CHECKING:
    from ..review.integrations.workspace import WorkspaceService

_ACTIVE = {"queued", "running"}
_LABELS = {
    "supported": "Model proposal: Supported by saved evidence",
    "contradicted": "Model proposal: Contradicted by saved evidence",
    "misleading_context": "Model proposal: Misleading context",
    "insufficient_evidence": "Model proposal: Insufficient evidence",
    "not_factual": "Model proposal: Not a factual claim",
}


def claim_check_panel() -> html.Section:
    return html.Section(
        [
            html.Div(id="cc-assessment"),
            html.Details(
                [
                    html.Summary("Check claim with external evidence"),
                    html.P(
                        "Search for evidence about the selected statement, then review a "
                        "local model's proposed assessment. Search queries go to the public "
                        "search provider; fetched evidence is assessed locally.",
                        className="rv-small rv-muted",
                    ),
                    html.Div(id="cc-selected-claim", className="rv-small"),
                    html.Details(
                        [
                            html.Summary("Search scope and additional sources"),
                            html.Label("Search queries (optional, up to 3)", htmlFor="cc-queries"),
                            dcc.Textarea(
                                id="cc-queries",
                                value="",
                                placeholder="One query per line. Leave blank to use the claim.",
                                className="rv-textarea",
                            ),
                            html.Label("Additional article URLs (up to 6)", htmlFor="cc-urls"),
                            dcc.Textarea(
                                id="cc-urls",
                                value="",
                                placeholder="https://… (one per line)",
                                className="rv-textarea",
                            ),
                            html.Label("Maximum sources to collect", htmlFor="cc-source-limit"),
                            dcc.Input(
                                id="cc-source-limit", type="number", value=6, min=1, max=8, step=1
                            ),
                        ],
                        className="rv-details",
                    ),
                    html.Button(
                        "Check claim",
                        id="cc-submit",
                        n_clicks=0,
                        disabled=True,
                        className="rv-button rv-button--outline",
                    ),
                    html.Div(id="cc-message", role="status", **{"aria-live": "polite"}),
                    html.Label("Saved checks for this finding", htmlFor="cc-run"),
                    dcc.Dropdown(
                        id="cc-run", options=[], value=None, clearable=False, searchable=False
                    ),
                    html.Div(id="cc-progress", role="status", **{"aria-live": "polite"}),
                    html.Div(
                        [
                            html.Button(
                                "Open result for review",
                                id="cc-open",
                                n_clicks=0,
                                disabled=True,
                                className="rv-button rv-button--primary",
                            ),
                            html.Button(
                                "Retry failed check",
                                id="cc-retry",
                                n_clicks=0,
                                disabled=True,
                                className="rv-button rv-button--outline",
                            ),
                        ],
                        className="rv-toolbar",
                    ),
                ],
                className="rv-details",
            ),
            dcc.Store(id="cc-operation", storage_type="memory"),
            dcc.Interval(id="cc-tick", interval=1800, disabled=True),
        ],
        **{"aria-label": "External claim checking"},
    )


def _notice(message: str, *, error: bool = False) -> html.Div:
    return html.Div(
        message,
        className="rv-notice rv-notice--error" if error else "rv-notice",
        role="status",
    )


def parse_policy(queries: str | None, urls: str | None, limit: int | None) -> CheckPolicy:
    return CheckPolicy(
        queries=[line.strip() for line in (queries or "").splitlines() if line.strip()],
        seed_urls=[line.strip() for line in (urls or "").splitlines() if line.strip()],
        max_sources=6 if limit is None else limit,
    )


def _belongs(job: ClaimCheckJob | None, state: dict) -> bool:
    return bool(
        job
        and job.source_case_id == state.get("case_id")
        and job.source_finding_id == state.get("finding_id")
    )


def _challenge_panel(
    assessment: dict,
    fragment_records: dict[str, str],
    excluded_record_ids: set[str],
    needs_review: bool,
):
    challenge = (assessment.get("metadata") or {}).get("challenge")
    if not challenge:
        return None
    findings = []
    for index, finding in enumerate(challenge.get("findings", [])):
        quotes = []
        for quote_index, (fragment_id, quote) in enumerate(
            zip(finding.get("fragment_ids", []), finding.get("quotes", []), strict=False)
        ):
            record_id = fragment_records.get(fragment_id)
            quotes.append(
                html.Li(
                    [
                        html.Q(quote),
                        html.Span(
                            " Excluded from this scenario · saved quotation.",
                            className="rv-small rv-muted",
                        )
                        if record_id in excluded_record_ids
                        else None,
                        html.Button(
                            "Inspect source",
                            id={
                                "type": "rv-record",
                                "rid": record_id,
                                "location": f"cc-objection-{index}-{quote_index}",
                            },
                            n_clicks=0,
                            className="rv-button rv-button--quiet",
                        )
                        if record_id
                        else None,
                    ]
                )
            )
        findings.append(
            html.Li(
                [
                    html.Strong(finding.get("finding_type", "objection").replace("_", " ")),
                    html.P(finding.get("explanation", "")),
                    html.Ul(quotes),
                ]
            )
        )
    return html.Details(
        [
            html.Summary(
                "Saved automated challenge · not recomputed"
                if needs_review
                else "Automated challenge of the claim"
            ),
            html.P(challenge.get("scope", "Same saved passages."), className="rv-small rv-muted"),
            html.Ul(findings)
            if findings
            else html.P(
                "No objection found in the checked passages. This does not verify the claim.",
                className="rv-small rv-muted",
            ),
        ],
        className="rv-details",
    )


def _claim_check_provenance(details: dict) -> str:
    """Keep explicit finding origin separate from current and legacy case labels."""
    check = details.get("claim_check") or {}
    origin = claim_origin_label({"details": details}) or "Claim origin not specified"
    claim = claim_origin_sentence(origin)

    case_origin = check.get("source_case_synthetic")
    if "source_case_synthetic" not in check and "source_synthetic_basis" not in check:
        case_origin = check.get("source_synthetic")
        if type(case_origin) is bool and origin == "Claim origin not specified":
            claim += " This older result's source flag labels only the originating case."
    if case_origin is True:
        case = "Originating case: Synthetic example."
    elif case_origin is False:
        case = "Originating case: Not marked synthetic."
    else:
        case = "Originating case: Not specified."
    return f"{claim} {case} Saved records retain their own provenance."


def assessment_panel(
    details: dict,
    fragments: list[dict] | None = None,
    case_limitations: list[str] | None = None,
    *,
    excluded_record_ids: set[str] | None = None,
    needs_review: bool = False,
) -> html.Section | str:
    """Render portable assessment metadata, also after importing a case package."""
    if details.get("domain") != "claim_check" or not details.get("assessment"):
        return ""
    assessment = details["assessment"]
    excluded_record_ids = excluded_record_ids or set()
    check = details.get("claim_check") or {}
    questions = assessment.get("unresolved_questions") or []
    issues = check.get("issues") or []
    limitations = list(
        dict.fromkeys([*(case_limitations or []), *check.get("origin_limitations", [])])
    )
    fragment_records = {fragment["id"]: fragment["record_id"] for fragment in fragments or []}
    citations = []
    for index, citation in enumerate(assessment.get("citations", [])):
        record_id = fragment_records.get(citation.get("fragment_id"))
        citations.append(
            html.Li(
                [
                    html.Span(f"{citation.get('stance', 'context').title()}: "),
                    html.Q(citation.get("quote", "")),
                    html.Span(
                        " Excluded from this scenario · saved quotation.",
                        className="rv-small rv-muted",
                    )
                    if record_id in excluded_record_ids
                    else None,
                    html.Button(
                        "Inspect source",
                        id={"type": "rv-record", "rid": record_id, "location": f"cc-{index}"},
                        n_clicks=0,
                        className="rv-button rv-button--quiet",
                    )
                    if record_id
                    else None,
                ]
            )
        )
    return html.Section(
        [
            html.H3(
                "Saved evidence assessment · review needed"
                if needs_review
                else "Proposed evidence assessment"
            ),
            _notice(
                "Evidence is excluded in this scenario. This saved proposal and its automated "
                "challenge have not been recomputed. Review the remaining evidence before "
                "recording a decision."
            )
            if needs_review
            else None,
            html.Strong(
                _LABELS.get(assessment.get("status"), "Model proposal: Assessment requires review")
                + (" · original result" if needs_review else "")
            ),
            html.P(assessment.get("explanation", "")),
            html.P(
                "Model proposal · your briefing decision is recorded separately. "
                "Coordination and deceptive intent are not established by this check.",
                className="rv-small rv-muted",
            ),
            html.P(_claim_check_provenance(details), className="rv-small rv-muted"),
            html.Ul(citations) if citations else None,
            html.Ul([html.Li(question) for question in questions]) if questions else None,
            _challenge_panel(assessment, fragment_records, excluded_record_ids, needs_review),
            html.Details(
                [
                    html.Summary("Search coverage and limitations"),
                    html.P(f"Collection: {check.get('collection_coverage', 'unknown')}"),
                    html.Ul([html.Li(limitation) for limitation in limitations])
                    if limitations
                    else None,
                    html.Ul(
                        [html.Li(issue.get("message", "Collection issue")) for issue in issues]
                    ),
                    html.Ul(
                        [
                            html.Li(
                                f"{batch.get('provider', 'Search')}: {batch.get('query', '')} "
                                f"({len(batch.get('hits', []))} discovery leads)"
                            )
                            for batch in check.get("search_batches", [])
                        ]
                    ),
                    html.P(
                        "Unknown source independence remains unresolved. Search leads and "
                        "matching wording do not establish independent corroboration.",
                        className="rv-small rv-muted",
                    ),
                    html.P(
                        f"Local model: {assessment.get('model') or 'not run'} · "
                        f"Prompt: {assessment.get('prompt_version') or 'not applicable'}",
                        className="rv-small rv-muted",
                    ),
                ],
                className="rv-details",
            ),
        ],
        className="rv-evidence-section",
    )


def job_panel(job: ClaimCheckJob) -> html.Div:
    notices = []
    if job.stale or job.status == "stale":
        notices.append(
            _notice("The source investigation changed. Submit a fresh check.", error=True)
        )
    if job.error:
        notices.append(_notice(job.error, error=True))
    if job.result:
        result = job.result
        notices.extend(
            [
                html.P(_LABELS[result.assessment.status]),
                html.P(result.assessment.explanation),
                html.P(
                    f"{result.sources_count} saved source(s) · {result.coverage} collection · "
                    "ready for analyst review",
                    className="rv-small rv-muted",
                ),
                html.Ul([html.Li(issue.message) for issue in result.issues]),
            ]
        )
    elif job.snapshot:
        notices.append(
            html.P(
                f"{len(job.snapshot.records)} source(s) saved; "
                f"{len(job.snapshot.issues)} collection issue(s).",
                className="rv-small rv-muted",
            )
        )
    return html.Div([html.P(f"{job.status.title()} · {job.stage.replace('_', ' ')}"), *notices])


def register_claim_check_callbacks(
    app: Dash,
    service: WorkspaceService,
    checks: ClaimCheckService | None = None,
) -> None:
    checks = checks or ClaimCheckService(service.repository, service.settings)

    @app.callback(
        Output("cc-operation", "data"),
        Output("cc-message", "children"),
        Input("cc-submit", "n_clicks"),
        Input("cc-retry", "n_clicks"),
        State("rv-session", "data"),
        State("cc-queries", "value"),
        State("cc-urls", "value"),
        State("cc-source-limit", "value"),
        State("cc-run", "value"),
        prevent_initial_call=True,
    )
    def submit(_submit, _retry, state, queries, urls, limit, selected):
        state = state or {}
        try:
            if not state.get("case_id") or not state.get("finding_id"):
                raise ValueError("Choose a case and one finding to check.")
            if ctx.triggered_id == "cc-retry":
                job = checks.get(selected) if selected else None
                if not _belongs(job, state):
                    raise ValueError("Select a saved check for this finding.")
                job = checks.retry(selected)
            else:
                job = checks.submit(
                    state["case_id"], state["finding_id"], parse_policy(queries, urls, limit)
                )
            return {"job_id": job.id}, _notice(
                "Queued. The worker will collect evidence and run the local model."
            )
        except (ValueError, KeyError, OSError) as exc:
            return no_update, _notice(str(exc), error=True)

    @app.callback(
        Output("cc-run", "options"),
        Output("cc-run", "value"),
        Output("cc-tick", "disabled"),
        Output("cc-submit", "disabled"),
        Input("rv-session", "data"),
        Input("cc-tick", "n_intervals"),
        Input("cc-operation", "data"),
        State("cc-run", "value"),
    )
    def refresh(state, _tick, operation, selected):
        state = state or {}
        if not state.get("case_id") or not state.get("finding_id"):
            return [], None, True, True
        jobs = checks.list(state["case_id"], finding_id=state["finding_id"], limit=20)
        options = [{"label": f"{job.created_at} · {job.status}", "value": job.id} for job in jobs]
        ids = {job.id for job in jobs}
        requested = (operation or {}).get("job_id")
        if ctx.triggered_id == "cc-operation" and requested in ids:
            selected = requested
        elif selected not in ids:
            selected = jobs[0].id if jobs else None
        active = any(job.status in _ACTIVE for job in jobs)
        return options, selected, not active, active

    @app.callback(
        Output("cc-progress", "children"),
        Output("cc-open", "disabled"),
        Output("cc-retry", "disabled"),
        Input("cc-run", "value"),
        Input("cc-tick", "n_intervals"),
        Input("rv-session", "data"),
    )
    def progress(selected, _tick, state):
        job = checks.get(selected) if selected else None
        if not _belongs(job, state or {}):
            return html.P("No saved check selected.", className="rv-small rv-muted"), True, True
        return (
            job_panel(job),
            not bool(job.status == "completed" and job.result),
            job.status != "failed" or job.stale,
        )

    @app.callback(
        Output("cc-assessment", "children"),
        Output("cc-selected-claim", "children"),
        Input("rv-session", "data"),
    )
    def current_assessment(state):
        state = state or {}
        if not state.get("case_id"):
            return "", "Choose a factual claim to check."
        view = service.view(state["case_id"], state.get("scenario_id"))
        bundle = view["bundle"]
        finding = next(
            (f for f in bundle["findings"] if f["snapshot_id"] == state.get("finding_id")), {}
        )
        details = finding.get("details", {})
        assessment = details.get("assessment") or {}
        metadata = assessment.get("metadata") or {}
        fragment_records = {
            fragment["id"]: fragment["record_id"] for fragment in bundle.get("fragments", [])
        }
        # Older exported cases may predate the explicit assessment-input edges.
        # Preserve correct scenario labeling for their cited/challenged passages.
        input_fragment_ids = set(metadata.get("reviewed_fragment_ids", []))
        input_fragment_ids.update(
            citation["fragment_id"] for citation in assessment.get("citations", [])
        )
        for objection in (metadata.get("challenge") or {}).get("findings", []):
            input_fragment_ids.update(objection.get("fragment_ids", []))
        input_record_ids = {fragment_records.get(identifier) for identifier in input_fragment_ids}
        impact = view.get("impact") or {}
        excluded = set(impact.get("excluded_record_ids", []))
        needs_review = bool(
            state.get("finding_id") in impact.get("affected_finding_ids", [])
            or input_record_ids & excluded
        )
        selected_claim = finding.get("statement", "")
        if excluded:
            selected_claim = html.Div(
                [
                    html.P(selected_claim),
                    html.P(
                        "A new external check creates a separate assessment. "
                        "This scenario's exclusions are not applied to the new search.",
                        className="rv-small rv-muted",
                    ),
                ]
            )
        return (
            assessment_panel(
                details,
                bundle.get("fragments", []),
                bundle.get("case", {}).get("limitations", []),
                excluded_record_ids=excluded,
                needs_review=needs_review,
            ),
            selected_claim,
        )

    @app.callback(
        Output("rv-case", "options", allow_duplicate=True),
        Output("rv-case", "value", allow_duplicate=True),
        Output("rv-session", "data", allow_duplicate=True),
        Output("cc-message", "children", allow_duplicate=True),
        Input("cc-open", "n_clicks"),
        State("cc-run", "value"),
        State("rv-session", "data"),
        prevent_initial_call=True,
    )
    def open_result(_click, selected, state):
        try:
            job = checks.get(selected) if selected else None
            if not _belongs(job, state or {}) or job.status != "completed" or not job.result:
                raise ValueError("Select a completed check for this finding.")
            case_id = job.result.result_case_id
            next_state = session_for_case(service, case_id)
            next_state["navigate_to"] = "review"
            options = [
                {"label": case["title"], "value": case["id"]} for case in service.list_cases()
            ]
            return (
                options,
                case_id,
                next_state,
                _notice("Saved assessment opened. Inspect its evidence and record your decision."),
            )
        except (ValueError, KeyError, OSError) as exc:
            return no_update, no_update, no_update, _notice(str(exc), error=True)
