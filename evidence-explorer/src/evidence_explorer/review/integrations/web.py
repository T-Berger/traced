"""Compose the existing review desk with specialist media, replay, and export controls."""

from __future__ import annotations

import base64
import json
import re
import zlib
from binascii import Error as Base64Error
from io import BytesIO

from dash import Input, Output, State, ctx, dcc, html, no_update
from dash.development.base_component import Component
from flask import abort, request, send_file
from pydantic import ValidationError

from ..contracts import EvidenceBundle
from ..importing import _validation_message
from ..selectors import initial_selection
from ..web import create_app as create_review_app
from .packages import MAX_PACKAGE_BYTES
from .workspace import WorkspaceService

MAX_UPLOAD_CONTENT_CHARS = 4 * ((MAX_PACKAGE_BYTES + 2) // 3) + 256
# Dash carries the file as a base64 data URI inside a small JSON callback envelope.
MAX_UPLOAD_HTTP_BYTES = MAX_UPLOAD_CONTENT_CHARS + 64 * 1024
_UPLOAD_SIZE_MESSAGE = "Choose a small selected-case package, up to 64 MB."
_IMPORT_ERRORS = (ValueError, KeyError, OSError, IndexError)


def _walk(node):
    if isinstance(node, Component):
        yield node
        children = getattr(node, "children", None)
        for child in children if isinstance(children, (tuple, list)) else [children]:
            yield from _walk(child)


def _notice(message, error=False):
    return html.Div(
        message, className="rv-notice rv-notice--error" if error else "rv-notice", role="status"
    )


def _case_upload_message(exc: Exception) -> str:
    """Report why an upload failed, allowing only a bounded fragment ID from validation."""
    if exc.args == (_UPLOAD_SIZE_MESSAGE,):
        return _UPLOAD_SIZE_MESSAGE
    if isinstance(exc, OSError):
        return "Could not read or save the case. Check workspace access and try again."
    if isinstance(exc, (Base64Error, IndexError)):
        return "The uploaded file could not be read. Select the case file again."

    reason = "Invalid evidence package."
    if isinstance(exc, ValidationError) and exc.title == EvidenceBundle.__name__:
        # EvidenceBundle accepts extra fields and only validates declared field paths.
        # Other models can report producer keys in their error locations.
        reason = _validation_message(exc)
        if exc.error_count() == 1:
            issue = exc.errors(include_input=False, include_context=False, include_url=False)[0]
            prefix = "Value error, Unresolved record for fragment "
            if (
                not issue["loc"]
                and issue["type"] == "value_error"
                and issue["msg"].startswith(prefix)
            ):
                fragment_id = issue["msg"].removeprefix(prefix)
                reason = "A saved evidence fragment refers to a missing record."
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/%~-]{0,199}", fragment_id):
                    reason = f"Evidence fragment “{fragment_id}” refers to a missing record."
            elif issue["type"] == "json_invalid":
                reason = "The evidence JSON could not be read."
    elif isinstance(exc, (json.JSONDecodeError, UnicodeError)):
        reason = "The evidence JSON could not be read."
    elif isinstance(exc.__cause__, zlib.error):
        return (
            "Invalid evidence package. The compressed data is damaged. "
            "Choose a valid case package and try again."
        )
    elif type(exc) is ValueError and str(exc).startswith("Package file verification failed:"):
        reason = "Package file verification failed."
    return reason + " Ask the sender for a corrected case file, then upload it again."


def create_app(service: WorkspaceService, *, server=None, workbench_links=None):
    """Reuse review layout/callbacks; keep domain additions outside that shared module."""
    app = create_review_app(service, server=server, workbench_links=workbench_links)
    app.server.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_HTTP_BYTES
    original_layout = app.layout

    def layout():
        root = original_layout()
        from ...claim_check.ui import claim_check_panel
        from ...compliance_review.model_ui import model_proposal_panel
        from ...investigation.ui import check_panel
        from .collection_scope_ui import saved_scope_panel
        from .components import case_tools, comparison_tools, export_tools
        from .media_ui import media_panel

        slots = {
            "rv-case-tools": case_tools(),
            "rv-next-action-slot": html.Div(id="trail-next-action", **{"aria-live": "polite"}),
            "rv-export-tools": export_tools(),
            "rv-compare-tools": comparison_tools(),
            "rv-media-slot": media_panel(),
            "rv-domain-slot": [
                check_panel(),
                html.Div(id="trail-domain-context"),
                saved_scope_panel(),
                claim_check_panel(),
                model_proposal_panel(read_only=service.settings.read_only),
            ],
        }
        for node in _walk(root):
            node_id = getattr(node, "id", None)
            if isinstance(node_id, str) and node_id in slots:
                node.children = slots[node_id]
        _add_alerts_page(root)
        return root

    def _add_alerts_page(root):
        """Integrated desk only: Alerts and Policies tabs and pages beside the shared pages."""
        from dash import dcc

        from ...alerts.settings import AlertSettingsStore
        from ...alerts.ui import alerts_page
        from ...compliance_review.alerts_ui import alerts_bridge_panel
        from ...compliance_review.local_alerts_ui import local_alerts_panel
        from ...policies.ui import policies_page

        def tab(label, value):
            return dcc.Tab(
                label=label,
                value=value,
                className=f"rv-navigation-tab rv-navigation-tab--{value}",
                selected_className=(
                    f"rv-navigation-tab--selected rv-navigation-tab--selected-{value}"
                ),
            )

        for node in _walk(root):
            if getattr(node, "id", None) == "rv-page":
                node.children = [
                    *node.children,
                    tab("Alerts", "alerts"),
                    tab("Policies", "policies"),
                ]
            elif getattr(node, "className", None) == "rv-main":
                children = list(node.children)
                page = alerts_page(
                    AlertSettingsStore(service.settings.data_dir),
                    read_only=service.settings.read_only,
                )
                content = list(page.children.children)
                header = next(
                    i
                    for i, item in enumerate(content)
                    if getattr(item, "className", None) == "al-top"
                )
                content.insert(header + 1, html.Div(local_alerts_panel(), className="rv-case-card"))
                anchor = next(
                    i for i, item in enumerate(content) if getattr(item, "id", None) == "al-ready"
                )
                content.insert(anchor + 1, alerts_bridge_panel())
                page.children.children = content
                # Keep the shared footer last.
                children[-1:-1] = [
                    page,
                    policies_page(),
                ]
                node.children = children

    app.layout = layout

    @app.server.get("/trail-artifact")
    def artifact():
        try:
            captured = service.capture_artifact(
                request.args.get("case_id", ""),
                request.args.get("record_id", ""),
                expected_hash=request.args.get("expected_hash"),
            )
            if captured.path.suffix.lower() not in {
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
                ".gif",
                ".svg",
            }:
                abort(415)
            response = send_file(
                BytesIO(captured.content),
                download_name=captured.filename,
                conditional=True,
                etag=captured.content_hash,
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
            return response
        except (KeyError, ValueError, OSError):
            abort(404)

    @app.callback(Output("trail-domain-context", "children"), Input("rv-session", "data"))
    def domain_context(state):
        if not state or not state.get("case_id"):
            return ""
        bundle = service.get_case(state["case_id"])
        finding = next(
            (f for f in bundle["findings"] if f["snapshot_id"] == state.get("finding_id")), {}
        )
        details = finding.get("details", {})
        if details.get("domain") != "swarm":
            from .specialist_context import specialist_history_context

            return specialist_history_context(bundle, finding)
        pairs = details.get("swarm_pairs", [])
        return html.Details(
            [
                html.Summary("Behavioral evidence and alternative explanations"),
                html.Ul([html.Li(note) for note in details.get("counterevidence", [])]),
                html.Table(
                    [
                        html.Thead(
                            html.Tr(
                                [
                                    html.Th(label)
                                    for label in [
                                        "Account pair",
                                        "Observed families",
                                        "Control mean",
                                        "Control 95th percentile",
                                    ]
                                ]
                            )
                        ),
                        html.Tbody(
                            [
                                html.Tr(
                                    [
                                        html.Td(f"{pair['account_a']} / {pair['account_b']}"),
                                        html.Td(str(pair["observed"])),
                                        html.Td(f"{pair['null_mean']:.2f}"),
                                        html.Td(f"{pair['null_p95']:.2f}"),
                                    ]
                                )
                                for pair in pairs[:6]
                            ]
                        ),
                    ],
                    style={"width": "100%"},
                ),
                html.P(
                    "Descriptive randomized controls; "
                    "these values do not establish bots or intent.",
                    className="rv-small rv-muted",
                ),
            ],
            open=False,
            className="rv-details",
        )

    @app.callback(
        Output("trail-download", "data"),
        Output("trail-export-attempt", "data"),
        Input("trail-package", "n_clicks"),
        Input("trail-brief", "n_clicks"),
        Input("trail-json", "n_clicks"),
        State("rv-session", "data"),
        State("rv-draft-store", "data"),
        State("rv-review-reason", "value"),
        State("trail-handoff-context", "data"),
        State("trail-planning-selected", "data"),
        prevent_initial_call=True,
    )
    def download(
        _package,
        _brief,
        _json,
        state,
        drafts=None,
        note=None,
        expected_context=None,
        planning_draft=None,
    ):
        if not state or not state.get("case_id"):
            return no_update, no_update
        from .handoff_service import load_handoff_context, prepare_export

        binding = None
        download_context = None
        try:
            context = load_handoff_context(
                service, state, drafts, note, planning_draft=planning_draft
            )
            binding = context[2]
            download_context = context[1]["download_context"]
            kind = {"trail-package": "package", "trail-brief": "brief", "trail-json": "json"}[
                ctx.triggered_id
            ]
            prepared = prepare_export(
                service,
                kind,
                state,
                drafts,
                note,
                context=context,
                expected_context=expected_context,
                planning_draft=planning_draft,
            )
            send = dcc.send_bytes if isinstance(prepared.content, bytes) else dcc.send_string
            return send(prepared.content, prepared.filename), {
                "binding": binding,
                "download_context": download_context,
                "status": "prepared",
                "message": prepared.message,
            }
        except (ValueError, KeyError, OSError) as exc:
            return no_update, {
                "binding": binding,
                "download_context": expected_context,
                "status": "failed",
                "message": str(exc),
            }

    @app.callback(
        Output("trail-package", "disabled"),
        Output("trail-brief", "disabled"),
        Output("trail-json", "disabled"),
        Output("trail-export-message", "children"),
        Input("rv-session", "data"),
        Input("rv-draft-store", "data"),
        Input("rv-review-reason", "value"),
        Input("trail-export-attempt", "data"),
        Input("trail-handoff-context", "data"),
        Input("trail-planning-selected", "data"),
    )
    def export_ready(state, drafts, note, attempt=None, expected_context=None, planning_draft=None):
        from .handoff_service import load_handoff_context, validate_download_context

        if not state or not state.get("case_id"):
            return True, True, True, "Choose a case before exporting."
        try:
            _, handoff, binding = load_handoff_context(
                service, state, drafts, note, planning_draft=planning_draft
            )
        except (ValueError, KeyError, OSError) as exc:
            return True, True, True, _notice(str(exc), error=True)
        if handoff["dirty"]:
            return (
                True,
                True,
                True,
                "Unsaved planning changes. Save or discard the planning draft before downloading."
                if handoff["planning_dirty"]
                else "Unsaved changes. Save your decision in Review before downloading.",
            )
        try:
            validate_download_context(handoff["download_context"], expected_context)
        except ValueError as exc:
            return True, True, True, _notice(str(exc), error=True)
        unavailable_media = bool(
            handoff["counts"]["unavailable_media"] or handoff["counts"]["unknown_media"]
        )
        message = (
            "The case package needs all referenced media. Inspect the unavailable records "
            "above. The saved brief and draft dossier can still explain these gaps."
            if unavailable_media
            else "Saved assessment files are available to download. This does not establish "
            "platform-report readiness or recipient acceptance."
        )
        status = [html.P(message, className="rv-small rv-muted")]
        if (
            isinstance(attempt, dict)
            and attempt.get("binding") == binding
            and attempt.get("download_context") == handoff["download_context"]
        ):
            status.append(
                _notice(attempt.get("message", ""), error=attempt.get("status") == "failed")
            )
        return unavailable_media, False, False, html.Div(status)

    @app.callback(
        Output("rv-case", "options"),
        Output("rv-case", "value"),
        Output("trail-import-message", "children"),
        Input("trail-upload", "contents"),
        Input("trail-explorer", "n_clicks"),
        prevent_initial_call=True,
    )
    def import_case(contents, _explorer):
        from_explorer = ctx.triggered_id == "trail-explorer"
        try:
            if from_explorer:
                case_id = service.import_explorer()
            elif contents:
                if len(contents) > MAX_UPLOAD_CONTENT_CHARS:
                    raise ValueError(_UPLOAD_SIZE_MESSAGE)
                payload = base64.b64decode(contents.split(",", 1)[1], validate=True)
                if len(payload) > MAX_PACKAGE_BYTES:
                    raise ValueError(_UPLOAD_SIZE_MESSAGE)
                case_id = (
                    service.import_package(payload)
                    if payload.startswith(b"PK")
                    else (service.import_bundle(json.loads(payload)))
                )
            else:
                return no_update, no_update, no_update
        except _IMPORT_ERRORS as exc:
            message = str(exc) if from_explorer else _case_upload_message(exc)
            return no_update, no_update, _notice(message, error=True)

        try:
            options = [
                {"label": case["title"], "value": case["id"]} for case in service.list_cases()
            ]
            from .handoff_service import import_receipt_for
            from .handoff_ui import render_import_receipt

            return (
                options,
                case_id,
                render_import_receipt(import_receipt_for(service, case_id)),
            )
        except _IMPORT_ERRORS:
            return (
                no_update,
                no_update,
                _notice(
                    "The case was saved, but its import receipt could not be loaded. "
                    "Reload the workspace to open the saved case.",
                    error=True,
                ),
            )

    @app.callback(
        Output("rv-session", "data", allow_duplicate=True),
        Output("trail-operation-message", "children"),
        Input("trail-recompute", "n_clicks"),
        Input("trail-original", "n_clicks"),
        Input("trail-result", "n_clicks"),
        Input("trail-episode", "n_clicks"),
        State("rv-session", "data"),
        prevent_initial_call=True,
    )
    def scenario_action(_recompute, _original, _result, _episode, state):
        state = dict(state or {})
        try:
            case_id = state["case_id"]
            if ctx.triggered_id == "trail-episode":
                from .episodes import strongest_episode_record

                record = strongest_episode_record(
                    service.repository.get_bundle(case_id), state["finding_id"]
                )
                state["record_id"] = record.id
                return state, _notice(
                    "Strongest observed episode selected. Inspect its context, then exclude "
                    "this source to test the full documented episode family."
                )
            if ctx.triggered_id == "trail-original":
                finding_id, record_id = initial_selection(service.get_case(case_id))
                state.update(
                    scenario_id=None,
                    scenario_version=None,
                    job_id=None,
                    finding_id=finding_id,
                    record_id=record_id,
                    case_fingerprint=service.case_fingerprint(case_id),
                )
                return state, _notice("Original assessment restored. Scenario history is retained.")
            if not state.get("scenario_id"):
                raise ValueError("Exclude a source first to create an assessment scenario.")
            if ctx.triggered_id == "trail-recompute":
                job = service.recompute(case_id, state["finding_id"], state["scenario_id"])
                state["job_id"] = job["id"]
                # The comparison callback renders the current persisted job status.
                return state, ""
            view = service.view(case_id, state["scenario_id"])
            selected = view.get("scenario") or {}
            if selected.get("imported"):
                from ..navigation import navigation_context

                navigation_context(view, state)
                saved = view.get("saved_comparison") or {}
                if not saved.get("result_available"):
                    raise ValueError(
                        "This saved assessment has no completed result available to inspect."
                    )
                state.update(navigate_to="compare", navigate_target="rv-compare-summary")
                result_description = {True: "locally computed ", False: "imported ", None: ""}[
                    selected.get("locally_computed", False)
                ]
                return state, _notice(
                    f"Inspecting the saved {result_description}result. "
                    "Missing relationships remain explicit. Inspection is read-only."
                )
            result = (view.get("computation") or {}).get("result") or {}
            if not result.get("result_run_id"):
                raise ValueError("Wait for a completed Swarm recomputation first.")
            bundle = service.get_case(case_id)
            bundle["findings"] = [
                f for f in bundle["findings"] if f["run_id"] == result["result_run_id"]
            ]
            scenario = service.repository.create_scenario(case_id, result["result_run_id"])
            finding_id, record_id = initial_selection(bundle)
            state.update(
                scenario_id=scenario["id"],
                scenario_version=scenario["version"],
                finding_id=finding_id,
                record_id=record_id,
                job_id=None,
                case_fingerprint=service.case_fingerprint(case_id),
            )
            return state, _notice("Reviewing the saved result. Earlier decisions remain separate.")
        except (KeyError, ValueError) as exc:
            return no_update, _notice(str(exc), error=True)

    @app.callback(Output("trail-episode", "disabled"), Input("rv-session", "data"))
    def episode_available(state):
        if not state or not state.get("case_id"):
            return True
        bundle = service.get_case(state["case_id"])
        context = bundle.get("swarm_context") or {}
        return not any(
            finding["snapshot_id"] == state.get("finding_id")
            and finding["run_id"] == context.get("baseline_run_id")
            for finding in bundle["findings"]
        )

    @app.callback(Output("trail-result", "children"), Input("rv-session", "data"))
    def result_label(state):
        if state and state.get("case_id"):
            view = service.view(state["case_id"], state.get("scenario_id"))
            if (view.get("scenario") or {}).get("imported"):
                return "Inspect saved result"
        return "Review result"

    @app.callback(
        Output("rv-scenario", "value", allow_duplicate=True),
        Input("trail-operation-message", "children"),
        Input("rv-session", "data"),
        State("rv-scenario", "value"),
        prevent_initial_call="initial_duplicate",
    )
    def selected_assessment(_message, state, current_value):
        if not state or not state.get("case_id"):
            return no_update
        view = service.view(state["case_id"], state.get("scenario_id"))
        selected_value = "baseline" if view.get("is_baseline") else state.get("scenario_id")
        # Ordinary review actions already synchronize the picker with the session.
        # Echoing that value can overwrite a new selection while this request runs.
        return no_update if selected_value == current_value else selected_value

    @app.callback(
        Output("trail-comparison", "children"),
        Output("trail-recompute", "disabled"),
        Output("trail-result", "disabled"),
        Output("trail-tick", "disabled"),
        Input("rv-session", "data"),
        Input("trail-tick", "n_intervals"),
    )
    def comparison(state, _tick):
        if not state or not state.get("case_id"):
            return "", True, True, True
        view = service.view(state["case_id"], state.get("scenario_id"))
        bundle = view["bundle"]
        scenario = view.get("scenario") or {}
        if not bundle["runs"]:
            return _notice("This case has no saved analysis run to compare."), True, True, True
        run_id = scenario.get("base_run_id") or bundle["runs"][0]["id"]
        run = next((run for run in bundle["runs"] if run["id"] == run_id), {})
        can_recompute = bool(
            not scenario.get("imported")
            and run.get("full_inputs_available")
            and bundle.get("swarm_context")
            and run_id == bundle["swarm_context"].get("baseline_run_id")
            and scenario.get("excluded_record_ids")
        )
        job = view.get("computation")
        if scenario.get("imported"):
            from ..workspace_views import saved_comparison_summary

            saved = view.get("saved_comparison") or {}
            return (
                saved_comparison_summary(saved, view, location="specialist-saved", compact=True),
                True,
                not saved.get("result_available", False),
                True,
            )
        if not job:
            return (
                html.P(
                    "Source exclusions show review impact. Detector results change only "
                    "after a completed recomputation.",
                    className="rv-small rv-muted",
                ),
                (not can_recompute),
                True,
                True,
            )
        if job["status"] != "completed":
            status = job["status"]
            status_messages = {
                "queued": "Swarm recomputation queued with the saved background activity.",
                "running": "Swarm recomputation is running.",
                "failed": "Swarm recomputation failed.",
            }
            return (
                _notice(job.get("error") or status_messages[status], error=status == "failed"),
                True,
                True,
                status == "failed",
            )
        result = job["result"]
        rows = result.get("correspondences", [])
        cached = result.get("cached")
        if cached is False:
            summary = "Completed Swarm recomputation · recorded settings and seed retained."
        elif cached is True:
            summary = "Reused the matching saved Swarm result."
        else:
            summary = "Saved Swarm result available. Local computation status is unknown."
        return (
            html.Div(
                [
                    html.P(summary),
                    html.Ul(
                        [
                            html.Li(
                                f"{row.get('relation', 'uncertain')}: "
                                f"{len(row.get('baseline_snapshot_ids', []))} "
                                "baseline finding(s) → "
                                f"{len(row.get('result_snapshot_ids', []))} result finding(s)."
                            )
                            for row in rows
                        ]
                    ),
                    html.P(
                        "Review decisions stay with their exact findings and assessments.",
                        className="rv-small rv-muted",
                    ),
                ]
            ),
            not can_recompute,
            False,
            True,
        )

    from ...alerts.ui import register_alert_callbacks
    from ...claim_check.ui import register_claim_check_callbacks
    from ...compliance_review.alerts_ui import register_alert_bridge_callbacks
    from ...compliance_review.category_ui import register_category_review_callbacks
    from ...compliance_review.local_alerts_ui import register_local_alerts_callbacks
    from ...compliance_review.model_ui import register_model_proposal_callbacks
    from ...compliance_review.saved_ui import register_saved_context_callbacks
    from ...compliance_review.velocity_ui import register_velocity_callbacks
    from ...investigation.intake_ui import register_intake_callbacks
    from ...investigation.ui import register_check_callbacks
    from ...policies.ui import register_policy_callbacks
    from ...social_dashboard.events_ui import register_event_callbacks
    from ...social_dashboard.ui import register_callbacks as register_social_dashboard_callbacks
    from .activity_ui import register_activity_callbacks
    from .brief_editor_ui import register_planning_brief_callbacks
    from .collection_ui import register_collection_callbacks
    from .handoff_ui import register_handoff_callbacks
    from .media_ui import register_media_callbacks
    from .navigation_ui import register_navigation_callbacks
    from .sources_ui import register_source_callbacks
    from .use_cases_ui import register_use_case_callbacks

    register_source_callbacks(app, service)
    register_intake_callbacks(app, service)
    register_check_callbacks(app, service)
    register_collection_callbacks(app, service)
    register_planning_brief_callbacks(app, service)
    register_activity_callbacks(app, service)
    register_claim_check_callbacks(app, service)
    register_use_case_callbacks(app, service)
    register_navigation_callbacks(app, service)
    register_media_callbacks(app, service)
    register_handoff_callbacks(app, service)
    register_social_dashboard_callbacks(app, service)
    register_saved_context_callbacks(app, service)
    register_category_review_callbacks(app, service)
    register_model_proposal_callbacks(app, service)
    register_velocity_callbacks(app, service)
    register_event_callbacks(app, service)
    register_alert_callbacks(app, service)
    register_alert_bridge_callbacks(app, service)
    register_local_alerts_callbacks(app, service)
    register_policy_callbacks(app)
    return app
