"""Native investigator intake and source-bound manual drafting for the review desk."""

from __future__ import annotations

import json
import secrets
from collections import OrderedDict
from threading import Lock

from dash import Dash, Input, Output, State, dcc, html, no_update
from dash.exceptions import PreventUpdate

from ..review.drafts import draft_key
from ..review.integrations.snapshots import workspace_snapshot
from ..review.presentation import badge, empty
from . import workspace


def _notice(message, *, error=False):
    return html.Div(
        message,
        className="rv-notice rv-notice--error" if error else "rv-notice",
        role="alert" if error else "status",
    )


def saved_investigation_tools():
    return html.Details(
        [
            html.Summary("Open saved investigator draft"),
            html.P(
                "Bring an investigator's saved conclusions, reasoning steps and assumptions "
                "into this review desk. Opening a draft does not confirm scope or approve it.",
                className="rv-small rv-muted",
            ),
            html.Button(
                "Refresh saved drafts",
                id="investigation-saved-refresh",
                n_clicks=0,
                className="rv-button rv-button--outline",
            ),
            html.Div(id="investigation-saved-inventory", role="status"),
            html.Label("Saved investigator draft", htmlFor="investigation-saved-picker"),
            dcc.Dropdown(
                id="investigation-saved-picker",
                options=[],
                value=None,
                placeholder="Choose a saved draft",
                clearable=True,
            ),
            html.Div(id="investigation-saved-preview"),
            html.Button(
                "Open draft for review",
                id="investigation-saved-open",
                n_clicks=0,
                disabled=True,
                className="rv-button rv-button--primary",
            ),
            html.Div(id="investigation-saved-message", role="status", **{"aria-live": "polite"}),
            html.P(
                "Drafts from another workspace can be opened with Import case package above.",
                className="rv-small rv-muted",
            ),
            dcc.Store(id="investigation-saved-token", storage_type="memory"),
        ],
        id="investigation-saved-panel",
        className="rv-details rv-investigation-intake",
    )


def _draft_field(label, ident, *, rows=3, optional=False):
    return html.Div(
        [
            html.Label(label, htmlFor=ident),
            dcc.Textarea(
                id=ident,
                value="",
                rows=rows,
                className="rv-textarea",
                persistence=True,
                persistence_type="session",
                placeholder="Optional" if optional else None,
            ),
        ]
    )


def manual_draft_panel():
    return html.Details(
        [
            html.Summary("Write a checkable claim"),
            html.P(
                "Turn saved evidence into a proposed answer to your case question. State a "
                "claim, explain one reasoning step, and cite an exact passage. The draft "
                "stays separate from your Use, Qualify or Withhold decision.",
                className="rv-small rv-muted",
            ),
            html.Button(
                "Start a draft for this case",
                id="investigation-draft-start",
                n_clicks=0,
                className="rv-button rv-button--outline",
            ),
            html.P(
                "Starting again clears the fields below. Switching cases keeps your text "
                "bound to the original case until you start again.",
                className="rv-small rv-muted",
            ),
            html.Div(id="investigation-draft-context", role="status"),
            html.Div(id="investigation-draft-availability", role="status"),
            html.Div(
                [
                    html.Label("Saved evidence record", htmlFor="investigation-draft-record"),
                    dcc.Dropdown(
                        id="investigation-draft-record",
                        options=[],
                        value=None,
                        placeholder="Choose the source for this reasoning step",
                    ),
                    html.Details(
                        [
                            html.Summary("Read the saved source"),
                            html.Div(id="investigation-draft-source"),
                        ],
                        className="rv-details",
                        open=True,
                    ),
                    _draft_field("Proposed claim", "investigation-draft-claim"),
                    _draft_field("Reasoning step", "investigation-draft-step"),
                    _draft_field(
                        "Exact quote from this source", "investigation-draft-quote", rows=4
                    ),
                    html.P(
                        "Copy a passage exactly as saved. Choose enough text to identify one "
                        "occurrence. Up to 12,000 characters; the other fields allow 4,000 each.",
                        className="rv-small rv-muted",
                    ),
                    _draft_field(
                        "Assumption beyond the quoted evidence",
                        "investigation-draft-assumption",
                        optional=True,
                    ),
                    html.Button(
                        "Save draft finding",
                        id="investigation-draft-save",
                        n_clicks=0,
                        disabled=True,
                        className="rv-button rv-button--primary",
                    ),
                ],
                className="rv-investigation-draft-fields",
            ),
            html.Div(id="investigation-draft-message", role="status", **{"aria-live": "polite"}),
            dcc.Store(id="investigation-draft-token", storage_type="session"),
        ],
        id="investigation-draft-panel",
        className="rv-details rv-investigation-step",
    )


def _preview(summary):
    source = str(summary.get("draft_source") or "Not recorded")
    gaps = summary.get("gaps") or []
    return html.Section(
        [
            html.H3(summary["title"]),
            html.P(summary["goal"]),
            html.Div(
                [badge(f"Draft author: {source}"), badge("Human review required", "amber")],
                className="rv-badges",
            ),
            html.P(
                f"{summary['records']} saved records · {summary['claims']} claims · "
                f"{summary['steps']} reasoning steps · {summary['assumptions']} assumptions",
                className="rv-small",
            ),
            html.Details(
                [html.Summary("Stated gaps"), html.Ul([html.Li(str(gap)) for gap in gaps])],
                className="rv-details",
            )
            if gaps
            else None,
            html.P(
                "The original draft and its evidence will be preserved. Machine checks "
                "and human decisions remain separate.",
                className="rv-small rv-muted",
            ),
        ]
    )


def preserve_review_draft(service, state, stored, note):
    """Keep an unsaved decision note on its original finding and evidence snapshot."""
    data = dict(stored or {})
    notes = dict(data.get("notes") or {})
    if isinstance(state, dict) and state.get("case_id") and state.get("finding_id"):
        view = service.view(state["case_id"], state.get("scenario_id"))
        if note is not None:
            notes[draft_key(state, view)] = str(note)
    return {**data, "notes": notes}


def _note_assessment(view, bundle, finding, assessment, version):
    """Resolve a draft's own assessment without borrowing the active selection."""
    run_id = finding["run_id"]
    assessments = view.get("assessments", [])
    if assessment == "baseline":
        baselines = [
            item
            for item in assessments
            if item.get("is_baseline") and item.get("base_run_id") == run_id
        ]
        if not baselines:
            # Before a durable baseline exists, only the first run has an implicit one.
            first_run = next(iter(bundle.get("runs", [])), {})
            if version == 1 and first_run.get("id") == run_id:
                return {"base_run_id": run_id, "implicit_baseline": True, "version": 1}
            return None
        # Draft keys normalize baseline IDs. Compare every possible original
        # baseline instead of guessing which local/imported baseline owned it.
        if any(item.get("version") == version for item in baselines):
            return {"baselines": sorted(baselines, key=lambda item: item["id"])}
        return None
    else:
        selected = next(
            (
                item
                for item in assessments
                if item.get("id") == assessment
                and not item.get("is_baseline")
                and item.get("base_run_id") == run_id
            ),
            None,
        )
    return selected if selected and selected.get("version") == version else None


def rekey_exact_review_draft(service, state, before_bundle, before_view, stored):
    """Keep each unchanged finding's note usable after a case gains another run.

    Original keys remain intact. Only browser note text is copied; durable reviews
    are never read or written here, and a newer draft at the destination wins.
    """
    try:
        with workspace_snapshot(service) as current:
            after = current.get_case(state["case_id"])
            view = current.view(state["case_id"])
        if before_bundle["case"] != after["case"]:
            return stored
        for collection in ("records", "fragments", "runs", "findings"):
            ident = "snapshot_id" if collection == "findings" else "id"
            current = {item[ident]: item for item in after.get(collection, [])}
            if any(current.get(item[ident]) != item for item in before_bundle.get(collection, [])):
                return stored
        old_findings = {item["snapshot_id"]: item for item in before_bundle.get("findings", [])}

        def prior_links(bundle):
            return {
                item["id"]: item
                for item in bundle.get("links", [])
                if item.get("target", {}).get("id") in old_findings
                and item.get("target", {}).get("type") == "finding"
            }

        if prior_links(after) != prior_links(before_bundle):
            return stored
        notes = dict(stored.get("notes", {}))
        for key, note in list(notes.items()):
            try:
                parts = json.loads(key)
                if not isinstance(parts, list) or len(parts) != 5:
                    continue
                case_id, finding_id, assessment, version, fingerprint = parts
            except (ValueError, TypeError):
                continue
            if (
                case_id != before_bundle["case"]["id"]
                or fingerprint != before_view["case_fingerprint"]
                or not isinstance(finding_id, str)
                or not isinstance(assessment, str)
                or type(version) is not int
                or finding_id not in old_findings
            ):
                continue
            finding = old_findings[finding_id]
            original = _note_assessment(before_view, before_bundle, finding, assessment, version)
            current = _note_assessment(view, after, finding, assessment, version)
            if original is None or original != current:
                continue
            new_key = json.dumps(
                [case_id, finding_id, assessment, version, view["case_fingerprint"]],
                separators=(",", ":"),
            )
            notes.setdefault(new_key, note)
        return {**stored, "notes": notes}
    except (KeyError, ValueError, OSError):
        return stored


class _Tokens:
    """Browser data selects a bounded server-side context; it cannot edit that context."""

    def __init__(self):
        self.values = OrderedDict()
        self.lock = Lock()

    def put(self, value):
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.values[token] = value
            while len(self.values) > 32:
                self.values.popitem(last=False)
        return token

    def get(self, token):
        if not isinstance(token, str):
            return None
        with self.lock:
            return self.values.get(token)


def register_intake_callbacks(app: Dash, service) -> None:
    previews, drafts = _Tokens(), _Tokens()

    def current_draft(token, state):
        saved = drafts.get(token)
        if saved is None:
            raise ValueError("Start a draft for the current case. Your written text is kept.")
        if workspace.check_context(service, state or {}) != saved["context"]:
            raise ValueError(
                "This draft belongs to a different case or saved assessment. Return to its "
                "original context, or start a new draft for this case. Your text is kept."
            )
        return saved

    @app.callback(
        Output("investigation-saved-picker", "options"),
        Output("investigation-saved-inventory", "children"),
        Input("investigation-saved-refresh", "n_clicks"),
    )
    def inventory(_clicks):
        result = workspace.list_saved_investigations(service)
        options = [
            {"label": f"{item['title']} · {item['claims']} claims", "value": item["id"]}
            for item in result["items"]
        ]
        notes = [result.get("message") or f"{len(options)} saved drafts available."]
        if result["unavailable"]:
            notes.append(
                f"{result['unavailable']} incomplete or unavailable entries were not opened."
            )
        if result["truncated"]:
            notes.append("The list is bounded; use Import case package for an older draft.")
        return options, html.P(" ".join(notes), className="rv-small rv-muted")

    @app.callback(
        Output("investigation-saved-preview", "children"),
        Output("investigation-saved-token", "data"),
        Output("investigation-saved-open", "disabled"),
        Input("investigation-saved-picker", "value"),
        Input("investigation-saved-refresh", "n_clicks"),
    )
    def preview(investigation_id, _clicks):
        if not investigation_id:
            return "", None, True
        try:
            summary = workspace.preview_saved_investigation(service, investigation_id)
            disabled = service.settings.read_only or not summary["claims"]
            return _preview(summary), previews.put(summary), bool(disabled)
        except (ValueError, OSError) as exc:
            return _notice(str(exc), error=True), None, True

    @app.callback(
        Output("rv-case", "options", allow_duplicate=True),
        Output("rv-case", "value", allow_duplicate=True),
        Output("rv-session", "data", allow_duplicate=True),
        Output("rv-draft-store", "data", allow_duplicate=True),
        Output("investigation-saved-message", "children"),
        Input("investigation-saved-open", "n_clicks"),
        State("investigation-saved-picker", "value"),
        State("investigation-saved-token", "data"),
        State("rv-session", "data"),
        State("rv-draft-store", "data"),
        State("rv-review-reason", "value"),
        prevent_initial_call=True,
    )
    def open_saved(clicks, investigation_id, token, state, stored, note):
        if not clicks:
            raise PreventUpdate
        case_id = None
        try:
            summary = previews.get(token)
            if not summary or summary["id"] != investigation_id:
                raise ValueError("Choose and preview the saved draft again before opening it.")
            if not summary["claims"]:
                raise ValueError(
                    "This draft has no claim to review. "
                    "Import its case package to inspect its sources."
                )
            before_bundle = before_view = None
            with workspace_snapshot(service) as captured:
                kept = preserve_review_draft(captured, state, stored, note)
                if state and state.get("case_id") == summary["case_id"]:
                    before_bundle = captured.get_case(state["case_id"])
                    before_view = captured.view(state["case_id"], state.get("scenario_id"))
            case_id = workspace.import_saved_investigation(
                service, investigation_id, expected_fingerprint=summary["fingerprint"]
            )
            session = workspace.open_investigation_session(
                service, case_id, run_id=summary["run_id"]
            )
            if before_bundle is not None:
                kept = rekey_exact_review_draft(service, state, before_bundle, before_view, kept)
            options = [
                {"label": item["title"], "value": item["id"]} for item in service.list_cases()
            ]
            return (
                options,
                case_id,
                session,
                kept,
                _notice("Draft opened. Check its reasoning, then record your own review decision."),
            )
        except (ValueError, PermissionError, OSError) as exc:
            message = str(exc)
        except Exception:
            message = "The draft could not be opened. Refresh the saved drafts and try again."
        if case_id is not None:
            message = (
                "The draft was imported, but its review context could not be opened. "
                "Reload the saved case. " + message
            )
        return (no_update,) * 4 + (_notice(message, error=True),)

    @app.callback(
        Output("investigation-draft-token", "data"),
        Output("investigation-draft-record", "options"),
        Output("investigation-draft-record", "value"),
        Output("investigation-draft-claim", "value"),
        Output("investigation-draft-step", "value"),
        Output("investigation-draft-quote", "value"),
        Output("investigation-draft-assumption", "value"),
        Output("investigation-draft-context", "children"),
        Input("investigation-draft-start", "n_clicks"),
        State("rv-session", "data"),
        prevent_initial_call=True,
    )
    def start_draft(clicks, state):
        if not clicks:
            raise PreventUpdate
        try:
            if service.settings.read_only:
                raise PermissionError("This workspace is read-only. Drafts cannot be saved here.")
            result = workspace.draft_context(service, state or {})
            if not result["records"]:
                raise ValueError("This case has no saved text records to cite.")
            token = drafts.put({**result, "state": dict(state)})
            options = [
                {"label": record["label"], "value": record["id"]} for record in result["records"]
            ]
            return (
                token,
                options,
                None,
                "",
                "",
                "",
                "",
                html.Div([badge("Manual draft"), html.P(result["question"])]),
            )
        except (ValueError, PermissionError, OSError) as exc:
            return (no_update,) * 7 + (_notice(str(exc), error=True),)

    @app.callback(
        Output("investigation-draft-save", "disabled"),
        Output("investigation-draft-availability", "children"),
        Input("rv-session", "data"),
        Input("investigation-draft-token", "data"),
        Input("investigation-draft-record", "value"),
    )
    def available(state, token, record_id):
        if not token:
            return True, "Start a draft after selecting a saved case."
        try:
            saved = current_draft(token, state)
            if record_id not in {record["id"] for record in saved["records"]}:
                return True, "Choose the saved record you will quote."
            return bool(service.settings.read_only), "No model call or review decision is made."
        except (ValueError, PermissionError, OSError) as exc:
            return True, _notice(str(exc), error=True)

    @app.callback(
        Output("investigation-draft-source", "children"),
        Input("investigation-draft-record", "value"),
        Input("investigation-draft-token", "data"),
        Input("rv-session", "data"),
    )
    def source(record_id, token, state):
        if not record_id or not token:
            return empty("Choose a source", "Its exact saved text will appear here.")
        try:
            saved = current_draft(token, state)
            if record_id not in {record["id"] for record in saved["records"]}:
                raise ValueError("Choose a record from this draft's saved evidence.")
            bundle = service.get_case(saved["context"]["case_id"])
            record = next(item for item in bundle["records"] if item["id"] == record_id)
            current_draft(token, state)
            return html.Pre(
                record["text"],
                className="rv-source-text",
                style={
                    "whiteSpace": "pre-wrap",
                    "overflowWrap": "anywhere",
                    "maxHeight": "22rem",
                    "overflowY": "auto",
                },
            )
        except (ValueError, OSError, KeyError, StopIteration):
            return _notice("This source context changed. Your draft text is kept.", error=True)

    @app.callback(
        Output("rv-session", "data", allow_duplicate=True),
        Output("rv-draft-store", "data", allow_duplicate=True),
        Output("investigation-draft-message", "children"),
        Output("investigation-draft-token", "data", allow_duplicate=True),
        Input("investigation-draft-save", "n_clicks"),
        State("investigation-draft-token", "data"),
        State("rv-session", "data"),
        State("investigation-draft-record", "value"),
        State("investigation-draft-claim", "value"),
        State("investigation-draft-step", "value"),
        State("investigation-draft-quote", "value"),
        State("investigation-draft-assumption", "value"),
        State("rv-draft-store", "data"),
        State("rv-review-reason", "value"),
        prevent_initial_call=True,
    )
    def save(clicks, token, state, record_id, claim, step, quote, assumption, stored, note):
        if not clicks:
            raise PreventUpdate
        try:
            saved = current_draft(token, state)
            if record_id not in {record["id"] for record in saved["records"]}:
                raise ValueError("Choose a record from this draft's saved evidence.")
            with workspace_snapshot(service) as captured:
                kept = preserve_review_draft(captured, state, stored, note)
                before_bundle = captured.get_case(state["case_id"])
                before_view = captured.view(state["case_id"], state.get("scenario_id"))
            result = workspace.save_manual_draft(
                service,
                state,
                claim=claim,
                step=step,
                record_id=record_id,
                quote=quote,
                assumption=assumption or "",
            )
            kept = rekey_exact_review_draft(service, state, before_bundle, before_view, kept)
            return (
                result["session"],
                kept,
                _notice("Manual draft saved. Check the reasoning before making a review decision."),
                None,
            )
        except (ValueError, PermissionError, OSError) as exc:
            message = str(exc)
        except Exception:
            message = "The draft could not be saved. Your text is kept; check the source and retry."
        return no_update, no_update, _notice(message, error=True), no_update
