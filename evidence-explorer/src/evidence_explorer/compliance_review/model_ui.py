"""Explicit local model action for the currently selected source and finding."""

from dash import Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from ..review.integrations.snapshots import workspace_snapshot

PREFIX = "compliance-model-"


def model_proposal_panel(*, read_only=False):
    return html.Details(
        [
            html.Summary("Propose categories for saved records"),
            html.P(
                "First open a finding and its saved text in Review. Enter the target, then "
                "select up to 20 saved records and explicitly queue local Ollama analysis. "
                "A baseline assessment may be "
                "created to bind the job. Model proposals need human review and are not "
                "violations or platform decisions.",
                className="rv-small rv-muted",
            ),
            html.Label("Target of analysis", htmlFor=PREFIX + "target"),
            dcc.Input(
                id=PREFIX + "target", type="text", maxLength=500, debounce=True, disabled=read_only
            ),
            html.Label("Saved records to analyze (1–20)", htmlFor=PREFIX + "records"),
            dcc.Dropdown(
                id=PREFIX + "records",
                options=[],
                value=[],
                multi=True,
                disabled=read_only,
                placeholder="Choose saved records",
            ),
            html.Button(
                "Queue local proposal",
                id=PREFIX + "run",
                n_clicks=0,
                disabled=read_only,
                className="rv-button rv-button--outline",
            ),
            html.Div(id=PREFIX + "status", role="status", **{"aria-live": "polite"}),
            html.P(
                "Local analysis is disabled in this read-only workspace.",
                className="rv-small rv-muted",
            )
            if read_only
            else None,
            dcc.Store(id=PREFIX + "job"),
            dcc.Interval(id=PREFIX + "poll", interval=2000, disabled=True),
        ],
        className="rv-details",
    )


def queue_selected(service, state, target, selected_record_ids=None):
    """Resolve the current selection on one snapshot and recheck it when queued."""
    if service.settings.read_only:
        raise PermissionError("A read-only workspace cannot queue model proposals.")
    if not isinstance(state, dict) or not all(
        state.get(key) for key in ("case_id", "case_fingerprint", "finding_id", "record_id")
    ):
        raise ValueError("Open a saved case, finding and text record in Review first.")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("Enter the target of analysis.")
    case_id, finding_id = state["case_id"], state["finding_id"]
    selected = selected_record_ids if selected_record_ids is not None else [state["record_id"]]
    if (
        not isinstance(selected, list)
        or not 1 <= len(selected) <= 20
        or any(not isinstance(item, str) or not item for item in selected)
        or len(selected) != len(set(selected))
    ):
        raise ValueError("Choose 1 to 20 distinct saved records.")
    with workspace_snapshot(service) as snapshot:
        view = snapshot.load_view(case_id, state.get("scenario_id"))
        if view.case_fingerprint != state["case_fingerprint"]:
            raise ValueError("The saved case changed. Open the evidence again before analysis.")
        if finding_id not in {finding.snapshot_id for finding in view.findings}:
            raise ValueError("The selected finding is outside this assessment.")
        records = {record.id: record for record in view.bundle.records}
        manifest = []
        for record_id in selected:
            record = records.get(record_id)
            if record is None or not record.text or len(record.text) > 12000:
                raise ValueError("A selected saved record has no text or exceeds the size limit.")
            if record.id in view.impact.excluded_record_ids:
                raise ValueError("A selected record is excluded from this assessment.")
            manifest.append({"id": record.id, "content_hash": record.content_hash})
        fingerprint = view.case_fingerprint
        scenario = view.scenario
        if scenario and getattr(scenario, "imported", False):
            raise ValueError("Open a local assessment before running a proposal.")
        if scenario and state.get("scenario_version") != scenario.version:
            raise ValueError("The selected assessment changed. Reopen it before analysis.")
        scenario_id = scenario.id if scenario else None
        version = scenario.version if scenario else None
    if scenario_id is None:
        baseline = service.baseline_scenario(case_id, finding_id)
        scenario_id, version = baseline["id"], baseline["version"]
    job = service.queue_category_proposal(
        case_id,
        finding_id,
        scenario_id,
        manifest[0]["id"],
        manifest[0]["content_hash"],
        target,
        fingerprint,
        version,
        record_manifest=manifest,
    )
    return job


def register_model_proposal_callbacks(app, service):
    @app.callback(
        Output(PREFIX + "records", "options"),
        Output(PREFIX + "records", "value"),
        Input("rv-session", "data"),
    )
    def record_options(state):
        if not isinstance(state, dict) or not state.get("case_id"):
            return [], []
        try:
            with workspace_snapshot(service) as snapshot:
                view = snapshot.load_view(state["case_id"], state.get("scenario_id"))
                if view.case_fingerprint != state.get("case_fingerprint"):
                    return [], []
                options = [
                    {"label": f"{record.source_label} · {record.id}", "value": record.id}
                    for record in view.bundle.records
                    if record.text
                    and len(record.text) <= 12000
                    and record.id not in view.impact.excluded_record_ids
                ]
        except (ValueError, KeyError):
            return [], []
        selected = (
            [state["record_id"]] if state.get("record_id") in {o["value"] for o in options} else []
        )
        return options, selected

    @app.callback(
        Output(PREFIX + "status", "children"),
        Output(PREFIX + "job", "data"),
        Output(PREFIX + "poll", "disabled"),
        Input(PREFIX + "run", "n_clicks"),
        Input(PREFIX + "poll", "n_intervals"),
        Input("rv-session", "data"),
        State(PREFIX + "target", "value"),
        State(PREFIX + "records", "value"),
        State(PREFIX + "job", "data"),
        prevent_initial_call=True,
    )
    def act(_clicks, _interval, state, target, selected, stored):
        trigger = ctx.triggered_id
        if trigger == PREFIX + "run":
            try:
                job = queue_selected(service, state, target, selected)
            except Exception as exc:
                return html.P(str(exc) or "Could not queue the local proposal."), None, True
            binding = {
                key: state.get(key) for key in ("case_id", "scenario_id", "finding_id", "record_id")
            }
            return (
                html.P("Queued for the shared worker."),
                {"id": job.id, "binding": binding},
                False,
            )
        if not stored or not isinstance(state, dict):
            raise PreventUpdate
        if stored.get("binding") != {
            key: state.get(key) for key in ("case_id", "scenario_id", "finding_id", "record_id")
        }:
            return (
                html.P("Selection changed. Open the original case to inspect that job."),
                None,
                True,
            )
        job = service.load_job(stored["id"])
        if not job or job.case_id != state.get("case_id"):
            return html.P("The queued job is unavailable."), None, True
        if job.status in {"queued", "running"}:
            return html.P(f"Local proposal: {job.status}."), no_update, False
        if job.status == "failed":
            return html.P(f"Local proposal failed: {job.error}"), no_update, True
        if job.stale:
            return (
                html.P("The case or assessment changed; this result is stale. Reopen the case."),
                no_update,
                True,
            )
        result = job.result
        count = getattr(result, "proposal_count", 0) if result else 0
        return (
            html.P(
                f"Saved {count} unreviewed proposal(s) in a separate assessment. "
                "Refresh the case and select the new assessment to review its exact evidence. "
                "No candidates does not mean the content is clear."
            ),
            no_update,
            True,
        )
