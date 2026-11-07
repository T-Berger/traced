"""Explicit, snapshot-bound reasoning checks beside the existing evidence inspector."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import OrderedDict
from threading import Lock

from dash import Dash, Input, Output, State, ctx, dcc, html
from dash.exceptions import PreventUpdate

from ..review.integrations.snapshots import workspace_snapshot
from ..review.navigation import NextAction, navigation_context
from ..review.presentation import badge, empty
from ..review.verifier import CheckError, openrouter_configuration
from .workspace import check_case, check_context

LABELS = {
    "supported": ("Supported by saved text", "blue"),
    "unchecked": ("Not established", "amber"),
    "assumption_not_backed": ("Assumption not backed", "amber"),
    "unsupported": ("Unsupported step", "amber"),
    "contradicted": ("Conflicting evidence", "red"),
}
CONCLUSIONS = {
    "holds": ("No flagged dependencies", "neutral"),
    "weak": ("Needs closer review", "amber"),
    "undermined": ("Unsupported or conflicting dependency", "red"),
}
MODEL_LABELS = {
    "supported": ("Model raised no objection", "neutral"),
    "contradicted": ("Model-proposed conflict", "amber"),
    "unsupported": ("Model-proposed concern", "amber"),
    "assumption_not_backed": ("Model questions this assumption", "amber"),
}
MODEL_STATUS = {
    "ran": ("Model check completed", "blue"),
    "partial": ("Model check incomplete", "amber"),
    "failed": ("Model check failed", "red"),
    "not_run": ("Model check not run", "neutral"),
}


class ReportCache:
    """Keep report content on the server; browser tokens never supply verdicts."""

    def __init__(self, limit: int = 16):
        self.limit = limit
        self.items = OrderedDict()
        self.lock = Lock()

    def put(self, result: dict) -> dict:
        token = uuid.uuid4().hex
        with self.lock:
            self.items[token] = result
            while len(self.items) > self.limit:
                self.items.popitem(last=False)
        return {"id": token}

    def get(self, token: object) -> dict | None:
        if not isinstance(token, dict) or set(token) != {"id"}:
            return None
        key = token.get("id")
        if not isinstance(key, str):
            return None
        with self.lock:
            return self.items.get(key)


def check_panel():
    return html.Section(
        [
            html.H3("Check the reasoning"),
            html.P(
                "Follow conclusions through their steps and assumptions to the saved evidence. "
                "Check quotations and look for unsupported or conflicting dependencies before "
                "recording your decision.",
                className="rv-small rv-muted",
            ),
            html.Fieldset(
                [
                    html.Button(
                        "Check saved reasoning",
                        id="investigation-check-run",
                        n_clicks=0,
                        className="rv-button rv-button--outline",
                    ),
                    html.Button(
                        "Check with OpenRouter",
                        id="investigation-check-openrouter",
                        n_clicks=0,
                        disabled=True,
                        className="rv-button rv-button--outline",
                    ),
                    html.Button(
                        "Download check report",
                        id="investigation-check-download",
                        n_clicks=0,
                        disabled=True,
                        className="rv-text-button",
                    ),
                ],
                id="investigation-check-actions",
                className="rv-toolbar",
                style={"border": "none", "padding": 0, "margin": 0, "minWidth": 0},
            ),
            html.P(
                "Check saved reasoning runs offline. Check with OpenRouter sends statements "
                "from the selected analysis run and relevant saved excerpts to OpenRouter "
                "and its model provider. Excerpts may include nearby related text from the "
                "complete saved case. Up to 4 requests per action. Results are proposals "
                "for human review.",
                className="rv-small rv-muted",
            ),
            html.Div(id="investigation-openrouter-status", role="status"),
            html.Div(id="investigation-check-message", role="status"),
            dcc.Loading(
                html.Div(id="investigation-check-result", **{"aria-live": "polite"}),
                type="circle",
            ),
            dcc.Store(id="investigation-check-token", storage_type="memory"),
            dcc.Download(id="investigation-check-file"),
        ],
        className="rv-details investigation-check",
        id="investigation-check-panel",
        **{"aria-label": "Saved reasoning checks"},
    )


def _label(value, basis=None):
    labels = MODEL_LABELS if basis == "model" else LABELS
    text, tone = labels.get(value, ("Not established", "amber"))
    return badge(text, tone)


def _model_provenance(model):
    children = []
    if model.get("provider") or model.get("model"):
        children.extend(
            [
                html.P(
                    f"Provider: {model.get('provider') or 'Not recorded'} · "
                    f"Requested model: {model.get('model') or 'Not recorded'} · "
                    f"Returned model: {model.get('actual_model') or 'Not reported'}",
                    className="rv-small rv-mono",
                ),
                html.P(
                    f"Requests: {model.get('calls', 0)} · "
                    f"Cached checks: {model.get('cached', 0)} · "
                    f"Errors: {model.get('errors', 0)} · "
                    f"Skipped checks: {model.get('skipped', 0)} · "
                    f"Discarded objections: {model.get('discarded_objections', 0)}",
                    className="rv-small",
                ),
                html.P(
                    "Model observations are proposals for review. A lack of objections "
                    "does not establish support or approve a conclusion.",
                    className="rv-small rv-muted",
                ),
            ]
        )
    if model.get("reason"):
        children.append(
            html.P(
                model["reason"],
                className="rv-notice"
                if model.get("status") in {"partial", "failed"}
                else "rv-small rv-muted",
                role="status",
            )
        )
    return html.Div(children, id="investigation-model-result")


def _finding_action(finding, context, location):
    action = NextAction(
        "finding",
        "Inspect this finding",
        "",
        context,
        finding_id=finding["id"],
        location=location,
    )
    return html.Button(action.label, id=action.button_id(), n_clicks=0, className="rv-text-button")


def _passages(items, context, records, location):
    passages = []
    seen = set()
    for item in items:
        rid, quote = item.get("record_id"), item.get("quote")
        key = (rid, item.get("start"), item.get("end"), quote)
        if rid not in records or not isinstance(quote, str) or key in seen:
            continue
        seen.add(key)
        text = records[rid].get("text")
        start, end = item.get("start"), item.get("end")
        exact = (
            isinstance(text, str)
            and type(start) is int
            and type(end) is int
            and 0 <= start < end <= len(text)
        )
        if exact:
            full_quote = text[start:end]
            exact = quote == full_quote or (
                len(full_quote) > 300 and quote == full_quote[:297] + "…"
            )
        # Only current immutable source text may be presented as a checked quotation.
        if not exact:
            continue
        quote = full_quote
        action = NextAction(
            "source",
            "Open saved record",
            "",
            context,
            record_id=rid,
            location=f"{location}-{len(passages)}",
        )
        passages.append(
            html.Div(
                [
                    html.Blockquote(quote, className="rv-evidence-card-quote"),
                    html.P(
                        f"Exact saved text · characters {start}–{end}",
                        className="rv-small rv-muted",
                    ),
                    html.Button(
                        action.label,
                        id=action.button_id(),
                        n_clicks=0,
                        className="rv-text-button",
                    ),
                ],
                className="investigation-passage",
            )
        )
        if len(passages) == 8:
            break
    return passages


def render_report(report: dict, state: dict, view: dict):
    """Render a verified server report, reusing context-bound review navigation."""
    context = navigation_context(view, state)
    records = {record["id"]: record for record in view["bundle"]["records"]}
    findings = {item["id"]: item for item in report["findings"]}
    selected = findings.get(state.get("finding_id"))
    conclusions = sorted(
        report["conclusions"],
        key=lambda item: ({"undermined": 0, "weak": 1, "holds": 2}[item["status"]], item["label"]),
    )
    summary = report["summary"]
    model = summary.get("model_checks", {})
    links_by_id = {link["id"]: link for link in report["links"]}
    content = [
        html.Div(
            [
                badge("Saved evidence checked", "blue"),
                badge(*MODEL_STATUS.get(model.get("status"), MODEL_STATUS["not_run"])),
                badge("Human decision still required", "amber"),
            ],
            className="rv-badges",
        ),
        _model_provenance(model),
        html.P(
            f"{summary['records_failed']} record integrity problem(s) · "
            f"{summary['broken_fragments']} broken quotation(s). "
            "These checks describe consistency with the saved sample, not factual truth "
            "or complete historical coverage.",
            className="rv-small rv-muted",
        ),
    ]
    unavailable = sum(
        item["frozen"] in {"unavailable", "not_checked"} for item in report["records"]
    )
    if unavailable:
        content.append(
            html.P(
                f"{unavailable} original Maltego record(s) could not be rechecked in this "
                "workspace. Their saved text and quotations were checked; original-record "
                "verification remains unavailable.",
                className="rv-notice",
            )
        )
    if selected:
        concerns = [item["message"] for item in selected["problems"]]
        selected_conclusion = next(
            (item for item in conclusions if item["id"] == selected["id"]), None
        )
        selected_status = (
            badge(*CONCLUSIONS[selected_conclusion["status"]])
            if selected_conclusion
            else _label(selected["verdict"], selected.get("basis"))
        )
        content.append(
            html.Section(
                [
                    html.H4(f"This {selected['role']} · {selected['label']}"),
                    selected_status,
                    *[html.P(reason) for reason in dict.fromkeys(concerns)],
                    *[html.P(caveat, className="rv-small") for caveat in selected["caveats"]],
                    html.Details(
                        [
                            html.Summary("Model notes for this finding"),
                            html.Ul([html.Li(note) for note in selected["model_notes"][:40]]),
                            html.P("Further notes are included in the downloaded report.")
                            if len(selected["model_notes"]) > 40
                            else None,
                        ],
                        className="rv-details",
                        open=True,
                    )
                    if selected.get("model_notes")
                    else None,
                ],
                className="investigation-selected",
            )
        )
    if conclusions:
        cards = []
        for index, conclusion in enumerate(conclusions):
            label, tone = CONCLUSIONS[conclusion["status"]]
            flagged = conclusion["flagged"]
            cards.append(
                html.Details(
                    [
                        html.Summary(
                            [
                                badge(label, tone),
                                html.Span(f" {conclusion['label']} · {conclusion['statement']}"),
                            ]
                        ),
                        _finding_action(conclusion, context, f"reasoning-claim-{index}"),
                        *[
                            html.Div(
                                [
                                    _label(
                                        flag["label"],
                                        links_by_id.get(flag["id"], {}).get("basis")
                                        if flag.get("item") == "link"
                                        else findings.get(flag["id"], {}).get("basis"),
                                    ),
                                    html.P(flag["reason"]),
                                    html.P(
                                        "Reasoning path: " + " → ".join(flag["via"]),
                                        className="rv-small rv-muted",
                                    ),
                                    *_passages(
                                        flag.get("evidence", []),
                                        context,
                                        records,
                                        f"reasoning-flag-{index}-{j}",
                                    ),
                                ],
                                className="investigation-concern",
                            )
                            for j, flag in enumerate(flagged[:16])
                        ],
                        *(
                            [html.P("No dependencies were flagged by the checks that ran.")]
                            if not flagged
                            else []
                        ),
                        *(
                            [
                                html.P(
                                    f"{len(flagged) - 16} further concerns are included "
                                    "in the downloaded report."
                                )
                            ]
                            if len(flagged) > 16
                            else []
                        ),
                        *[
                            html.P(item["caveat"], className="rv-small")
                            for item in conclusion["caveats"]
                        ],
                    ],
                    open=conclusion["id"] == state.get("finding_id") or index == 0,
                    className="rv-details investigation-conclusion",
                )
            )
        content.extend([html.H4("Conclusions · concerns first"), *cards])
    else:
        content.append(
            empty(
                "No substantive conclusions in this assessment",
                "Collection observations describe what was saved. Open an investigator draft "
                "or write a checkable claim on Cases to address the investigation question.",
            )
        )
    if selected:
        links = [
            link
            for link in report["links"]
            if link["target"].get("id") == selected["id"] and link["scope"] == "chain"
        ]
        content.append(
            html.Details(
                [
                    html.Summary(
                        f"Steps, assumptions and evidence for this finding ({len(links)})"
                    ),
                    *[
                        html.Div(
                            [
                                _label(link["label"], link.get("basis")),
                                html.P(
                                    findings.get(link["source"].get("id"), {}).get("statement")
                                    or link["relation"]
                                ),
                                *[
                                    html.P(reason, className="rv-small")
                                    for reason in link["reasons"]
                                ],
                                *(
                                    [
                                        _finding_action(
                                            findings[link["source"]["id"]],
                                            context,
                                            f"reasoning-step-{j}",
                                        )
                                    ]
                                    if link["source"].get("id") in findings
                                    else []
                                ),
                                *_passages(
                                    link["evidence"], context, records, f"reasoning-link-{j}"
                                ),
                            ],
                            className="investigation-concern",
                        )
                        for j, link in enumerate(links[:40])
                    ],
                    *(
                        [html.P("Further dependencies are included in the downloaded report.")]
                        if len(links) > 40
                        else []
                    ),
                ],
                className="rv-details",
            )
        )
    content.append(
        html.Details(
            [
                html.Summary("Check scope and limitations"),
                html.P(
                    "Mechanical and rule-based checks ran on the complete saved case; "
                    "conclusions shown belong to the selected analysis run."
                ),
                *[html.P(note, className="rv-small") for note in report.get("notes", [])],
                html.P(
                    f"Checker version {report['checker']['version']} · {report['checked_at']}",
                    className="rv-small",
                ),
                html.P(
                    f"Evidence snapshot: {report['bundle']['fingerprint']}",
                    className="rv-mono rv-small",
                ),
            ],
            className="rv-audit-details",
        )
    )
    return html.Div(content)


def register_check_callbacks(app: Dash, service):
    cache = ReportCache()

    @app.callback(
        Output("investigation-openrouter-status", "children"),
        Output("investigation-check-openrouter", "disabled"),
        Input("rv-session", "data"),
    )
    def model_configuration(state):
        configuration = openrouter_configuration()
        ready = configuration["ready"]
        return html.Div(
            [
                badge(
                    "OpenRouter configured" if ready else "OpenRouter not configured",
                    "blue" if ready else "amber",
                ),
                html.P(
                    configuration.get("model") or "No model configured",
                    className="rv-small rv-mono",
                ),
                html.P(
                    "Configuration is present; availability is checked when you run the analysis."
                    if ready
                    else configuration["message"]
                    + " Restart the app after changing its configuration.",
                    className="rv-small rv-muted",
                ),
            ]
        ), not ready or not bool((state or {}).get("case_id"))

    @app.callback(
        Output("investigation-check-token", "data"),
        Output("investigation-check-message", "children"),
        Input("investigation-check-run", "n_clicks"),
        Input("investigation-check-openrouter", "n_clicks"),
        State("rv-session", "data"),
        prevent_initial_call=True,
        running=[(Output("investigation-check-actions", "disabled"), True, False)],
    )
    def run_check(clicks, openrouter_clicks, state):
        use_openrouter = ctx.triggered_id == "investigation-check-openrouter"
        if ctx.triggered_id not in {
            "investigation-check-run",
            "investigation-check-openrouter",
        } or not (openrouter_clicks if use_openrouter else clicks):
            raise PreventUpdate
        try:
            return cache.put(check_case(service, state or {}, use_openrouter=use_openrouter)), ""
        except (CheckError, ValueError, OSError, sqlite3.Error) as exc:
            return None, html.P(str(exc), className="rv-notice", role="status")

    def current(token, state, owner=service):
        result = cache.get(token)
        if result is None or result["context"] != check_context(owner, state or {}):
            return None
        return result

    @app.callback(
        Output("investigation-check-result", "children"),
        Output("investigation-check-download", "disabled"),
        Input("investigation-check-token", "data"),
        Input("rv-session", "data"),
    )
    def show_check(token, state):
        if not (state or {}).get("case_id"):
            return html.P("Choose a saved case to check its reasoning.", className="rv-small"), True
        try:
            with workspace_snapshot(service) as snapshot:
                result = current(token, state, snapshot)
                view = snapshot.view(state["case_id"], state.get("scenario_id")) if result else None
            if result is None:
                return html.P(
                    "Run a check for the current saved case and assessment. Earlier reports "
                    "are not applied to changed evidence.",
                    className="rv-small rv-muted",
                ), True
            return render_report(result["report"], state, view), False
        except (ValueError, OSError, sqlite3.Error) as exc:
            return html.P(str(exc), className="rv-notice", role="status"), True

    @app.callback(
        Output("investigation-check-file", "data"),
        Input("investigation-check-download", "n_clicks"),
        State("investigation-check-token", "data"),
        State("rv-session", "data"),
        prevent_initial_call=True,
    )
    def download_check(clicks, token, state):
        if not clicks:
            raise PreventUpdate
        try:
            result = current(token, state)
        except (ValueError, OSError, sqlite3.Error):
            raise PreventUpdate from None
        if result is None:
            raise PreventUpdate
        return dcc.send_string(
            json.dumps(result, ensure_ascii=False, indent=2), "reasoning-check.json"
        )
