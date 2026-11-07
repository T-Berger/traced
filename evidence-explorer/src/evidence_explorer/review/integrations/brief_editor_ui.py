"""Explicit planning revisions on Cases; evidence and review sessions stay read-only.

Browser edits are recoverable working copies, separate from durable revisions and
from the review-note draft store. Only button actions call planning commands.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

from dash import Dash, Input, Output, State, ctx, dcc, html, no_update

from ..navigation import contextual_next_action
from ..presentation import badge
from .brief_editor_forms import (
    FIELD_NAMES,
    PREFIX,
    editor_fields,
)
from .brief_editor_state import (
    EditorDraft as EditorDraft,
)
from .brief_editor_state import (
    EditorStore as EditorStore,
)
from .brief_editor_state import (
    apply_editor_action as apply_editor_action,
)
from .brief_editor_state import (
    case_scope_context as case_scope_context,
)
from .brief_editor_state import (
    selected_revision_context as selected_revision_context,
)
from .planning_journey import continuation_state, current_draft, draft_stage


def planning_brief_panel() -> html.Section:
    """Mount once on Cases; rendering and reloads never create planning records."""

    def button(label, suffix, *, primary=False):
        return html.Button(
            label,
            id=PREFIX + suffix,
            n_clicks=0,
            className="rv-button rv-button--primary" if primary else "rv-button rv-button--outline",
        )

    return html.Section(
        [
            html.H2("Investigation plan"),
            html.P(
                "Save your question, gather the evidence, then attach it and confirm the scope. "
                "Confirming a plan does not approve findings or start collection or analysis.",
                className="rv-small rv-muted",
            ),
            dcc.Store(id=PREFIX + "edit", storage_type="session", data={}),
            dcc.Store(id=PREFIX + "selected", storage_type="session"),
            dcc.Store(id=PREFIX + "refresh", storage_type="memory"),
            dcc.Store(id=PREFIX + "case", storage_type="memory"),
            dcc.Store(id=PREFIX + "visible", storage_type="memory"),
            html.Div(
                [
                    html.P(id=PREFIX + "continuation-help", className="rv-small"),
                    button("Use this case in saved plan", "resume", primary=True),
                ],
                id=PREFIX + "continuation",
                className="rv-notice",
                style={"display": "none"},
            ),
            html.Label("Continue a saved plan", htmlFor=PREFIX + "picker"),
            dcc.Dropdown(
                id=PREFIX + "picker",
                options=[],
                value=None,
                clearable=True,
                placeholder="Choose a saved question",
            ),
            html.P(id=PREFIX + "selection-help", className="rv-small rv-muted"),
            html.Div(
                [button("Open saved plan", "load"), button("Start another plan", "new")],
                className="rv-toolbar",
            ),
            html.Div(id=PREFIX + "status", role="status", **{"aria-live": "polite"}),
            html.Div(id=PREFIX + "scope"),
            button("Use this case's sources in the plan", "use-scope"),
            editor_fields(),
            html.Div(
                [
                    button("Save plan", "save", primary=True),
                    button("Attach collected evidence", "bind"),
                    button("Confirm scope", "confirm"),
                ],
                className="rv-toolbar",
            ),
            html.Details(
                [
                    html.Summary("Plan versions and imported history"),
                    html.Label("Exact saved revision", htmlFor=PREFIX + "revision"),
                    dcc.Dropdown(id=PREFIX + "revision", options=[], value=None, clearable=False),
                    html.P(
                        "The latest version is selected when you choose a plan. To inspect an "
                        "earlier version, select it here, then choose Open saved plan above. "
                        "Changing the selection keeps your current working fields intact.",
                        className="rv-small rv-muted",
                    ),
                    html.P(
                        "Earlier versions remain unchanged. Imported confirmation is history "
                        "from its original workspace; adopt a local version and confirm it here.",
                        className="rv-small rv-muted",
                    ),
                    button("Adopt selected version for this case", "adopt"),
                    button("Copy fields into a separate plan", "copy"),
                ],
                id=PREFIX + "history",
                className="rv-details",
            ),
            html.Div(id=PREFIX + "message", role="status", **{"aria-live": "polite"}),
        ],
        id=PREFIX + "panel",
        className="rv-case-card",
        style={"display": "grid", "gap": "12px", "minWidth": 0},
        **{"aria-label": "Saved investigation planning drafts"},
    )


def _scope_summary(scope):
    if scope.get("error"):
        return html.Div(scope["error"], className="rv-notice rv-notice--error", role="alert")
    if not scope.get("case_id"):
        return html.P(
            "No evidence case selected. You can save the plan first and attach evidence later.",
            className="rv-small",
        )
    basis = (
        "Includes the selected sources, including any that failed or returned no records."
        if scope["basis"] == "collection_scope"
        else "Uses all saved records in this case, with their supplied source identities."
    )
    return html.Div(
        [
            html.Strong("Selected case: " + scope["title"]),
            html.P(basis, className="rv-small"),
            html.Details(
                [
                    html.Summary(f"Exact saved source references ({len(scope['source_refs'])})"),
                    html.Pre(
                        "\n".join(scope["source_refs"]),
                        style={"whiteSpace": "pre-wrap", "overflowWrap": "anywhere"},
                    ),
                ]
            ),
        ]
    )


def _next_step(draft, scope, *, current_case: bool, navigation=None):
    """Explain the next explicit action without changing or approving saved state."""
    stage = draft_stage(draft, scope.get("source_refs", ()), current_case=current_case)
    if stage == "history":
        text = "Open plan versions to adopt this saved version locally or copy it into a new plan."
        label, target = "Open plan versions", PREFIX + "history"
    elif stage in {"draft", "start"}:
        text = "Save the plan to keep your question and any source changes."
        label, target = "Go to Save plan", PREFIX + "save"
    elif stage == "saved":
        text = (
            "Your plan is saved. Gather evidence, or use the selected case's sources below. "
            "After collection or import, continue this plan with the saved case."
        )
        label, target = "Gather evidence", "trail-gather-evidence"
    elif stage == "scope":
        text = "Use this case's sources in the plan, then save before attaching evidence."
        label, target = "Use this case's sources", PREFIX + "use-scope"
    elif stage == "attach_ready":
        text = "Attach the collected evidence to this saved plan, then confirm the scope."
        label, target = "Go to Attach collected evidence", PREFIX + "bind"
    elif stage == "confirm":
        text = "Check the attached sources and any missing planning details, then confirm scope."
        label, target = "Go to Confirm scope", PREFIX + "confirm"
    else:
        text = "The scope is confirmed. Inspect the findings and evidence before saving decisions."
        label, target = "Continue to the next review action", "rv-next-action-slot"
    destination = (
        html.Button(
            navigation.label,
            id=navigation.button_id(),
            n_clicks=0,
            className="rv-button rv-button--primary",
        )
        if navigation is not None and stage in {"saved", "confirmed"}
        else html.A(label, href="#" + target)
    )
    return html.Div(
        [html.Strong("Next: "), text, " ", destination],
        className="rv-notice",
    )


def register_planning_brief_callbacks(app: Dash, service) -> None:
    """Register an isolated editor. No callback writes rv-session or page navigation."""
    actions = ("load", "new", "save", "bind", "confirm", "adopt", "copy", "use-scope", "resume")

    @app.callback(
        Output(PREFIX + "continuation", "style"),
        Output(PREFIX + "continuation-help", "children"),
        Output(PREFIX + "resume", "disabled"),
        Input(PREFIX + "edit", "data"),
        Input(PREFIX + "case", "data"),
    )
    def continuation(stored, scope):
        store = EditorStore.model_validate(stored or {})
        scope = scope or {}
        try:
            saved = continuation_state(service, store, scope.get("case_id"))
        except (ValueError, KeyError, OSError, sqlite3.Error):
            saved = None
        if saved is None or scope.get("error"):
            return {"display": "none"}, "", True
        active = store.drafts.get(store.active)
        if active and (active.brief_id or active.dirty):
            # A different case's working copy takes priority. The usual saved
            # picker remains available for an intentional switch.
            return {"display": "none"}, "", True
        return (
            {},
            [
                html.Strong("Continue your saved plan: "),
                saved.revision.fields.purpose,
                html.Br(),
                "Selected evidence: ",
                scope.get("title", "Saved case"),
                html.Br(),
                "This copies its sources into your working plan. Review and save "
                "before attaching evidence and confirming scope.",
            ],
            False,
        )

    @app.callback(
        Output(PREFIX + "case", "data"),
        Output(PREFIX + "scope", "children"),
        Input("rv-session", "data"),
    )
    def case_scope(session):
        try:
            scope = case_scope_context(service, session)
        except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
            scope = {
                "case_id": (session or {}).get("case_id"),
                "source_refs": [],
                "error": str(exc),
            }
        return scope, _scope_summary(scope)

    @app.callback(
        Output(PREFIX + "picker", "options"),
        Output(PREFIX + "picker", "value"),
        Output(PREFIX + "revision", "options"),
        Output(PREFIX + "revision", "value"),
        Output(PREFIX + "load", "disabled"),
        Output(PREFIX + "selection-help", "children"),
        Input("rv-session", "data"),
        Input(PREFIX + "refresh", "data"),
        Input(PREFIX + "picker", "value"),
        Input(PREFIX + "revision", "value"),
    )
    def saved_selection(session, refresh, picker, revision):
        """Own selector values independently of the open, possibly unsaved working copy."""
        case_id = (session or {}).get("case_id")
        try:
            states = {
                state.revision.brief_id: state
                for state in service.planning_briefs.list_for_case(case_id)
            }
            if (
                ctx.triggered_id == PREFIX + "refresh"
                and refresh
                and refresh.get("case_id") == case_id
            ):
                picker, revision = refresh.get("brief_id"), refresh.get("revision_id")
            # A valid pending selection may be outside the first catalog page.
            # Preserve it across case changes and unrelated saved refreshes.
            if picker and picker not in states:
                try:
                    selected = service.planning_briefs.load(picker)
                except KeyError:
                    selected = None
                if selected and (
                    selected.revision.binding is None
                    or selected.revision.binding.case_id == case_id
                ):
                    states[picker] = selected
            options = [
                {
                    "label": (
                        f"{state.revision.fields.purpose or 'Untitled investigation plan'} "
                        f"· revision {state.revision.number}"
                    ),
                    "value": state.revision.brief_id,
                }
                for state in states.values()
            ]
            if picker not in states:
                return (
                    options,
                    None,
                    [],
                    None,
                    True,
                    "Continue a saved question here, including a plan saved "
                    "before collecting evidence.",
                )
            history = service.planning_briefs.history(picker, limit=1000)
            by_id = {row.revision_id: row for row in history.revisions}
            # The bounded history page starts at the oldest revision. Its head
            # metadata is already available, even when revision 1001 is absent.
            latest = states[picker].revision
            by_id[latest.revision_id] = latest
            if revision and revision not in by_id:
                try:
                    pending = service.planning_briefs.load(picker, revision).revision
                except (KeyError, ValueError):
                    pass  # A stale revision from another brief is not a valid choice.
                else:
                    by_id[pending.revision_id] = pending
            revisions = sorted(
                (
                    row
                    for row in by_id.values()
                    if row.binding is None or row.binding.case_id == case_id
                ),
                key=lambda row: row.number,
                reverse=True,
            )
            if revision not in {row.revision_id for row in revisions}:
                # This is a visible pending choice only. Opening the revision is
                # the explicit action that captures the editor's base and head.
                revision = latest.revision_id
            choices = [
                {
                    "label": f"Revision {row.number} · {row.created_at.isoformat()}",
                    "value": row.revision_id,
                }
                for row in revisions
            ]
            return (
                options,
                picker,
                choices,
                revision,
                revision is None,
                "Choose Open saved plan to continue. "
                "Your working fields stay unchanged until then. "
                "Earlier versions are available under Plan versions and imported history.",
            )
        except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
            return (
                no_update,
                no_update,
                no_update,
                no_update,
                True,
                str(exc) + " Your selection and planning edits are kept.",
            )

    @app.callback(
        Output(PREFIX + "edit", "data"),
        Output(PREFIX + "message", "children"),
        Output(PREFIX + "refresh", "data"),
        *[Output(PREFIX + name, "value") for name in FIELD_NAMES],
        Output(PREFIX + "visible", "data"),
        Input("rv-session", "data"),
        *[Input(PREFIX + name, "value") for name in FIELD_NAMES],
        *[Input(PREFIX + name, "n_clicks") for name in actions],
        State(PREFIX + "edit", "data"),
        State(PREFIX + "picker", "value"),
        State(PREFIX + "revision", "value"),
        State(PREFIX + "case", "data"),
        State(PREFIX + "visible", "data"),
    )
    def edit(session, *arguments):
        count = len(FIELD_NAMES)
        values = dict(zip(FIELD_NAMES, arguments[:count], strict=True))
        stored, picker, revision, scope, visible_key = arguments[count + len(actions) :]
        trigger = ctx.triggered_id
        action = (
            trigger.removeprefix(PREFIX)
            if isinstance(trigger, str) and trigger.startswith(PREFIX)
            else "context"
        )
        store, message, error = apply_editor_action(
            service, action, session, stored, values, picker, revision, scope, visible_key
        )
        draft = store.for_case((session or {}).get("case_id"))
        notice = (
            html.Div(
                message,
                className="rv-notice rv-notice--error" if error else "rv-notice",
                role="alert" if error else "status",
            )
            if message
            else ""
        )
        fields = (
            [no_update] * count
            if action in FIELD_NAMES and not error
            else [draft.fields[name] for name in FIELD_NAMES]
        )
        refresh = no_update
        if action in {"save", "copy", "bind", "confirm", "adopt", "resume"} and not error:
            refresh = {
                "case_id": draft.case_id,
                "brief_id": draft.brief_id,
                "revision_id": draft.selected_revision_id,
                "head_revision_id": draft.current_head_revision_id,
                "confirmation_id": draft.confirmation_id,
                "scope_fingerprint": draft.scope_fingerprint,
            }
            refresh["token"] = json.dumps(refresh, sort_keys=True, separators=(",", ":"))
        return (
            store.model_dump(mode="json"),
            notice,
            refresh,
            *fields,
            no_update if action in FIELD_NAMES and not error else store.active,
        )

    @app.callback(
        Output(PREFIX + "status", "children"),
        Output(PREFIX + "selected", "data"),
        Output(PREFIX + "save", "disabled"),
        Output(PREFIX + "bind", "disabled"),
        Output(PREFIX + "confirm", "disabled"),
        Output(PREFIX + "adopt", "disabled"),
        Output(PREFIX + "copy", "disabled"),
        Output(PREFIX + "use-scope", "disabled"),
        Input(PREFIX + "edit", "data"),
        Input(PREFIX + "case", "data"),
        State("rv-session", "data"),
    )
    def status(stored, scope, session=None):
        store = EditorStore.model_validate(stored or {})
        draft = store.drafts.get(store.active)
        if draft is None:
            return (
                "New plan · save your question to begin.",
                None,
                False,
                True,
                True,
                True,
                False,
                True,
            )
        try:
            draft = current_draft(service, draft)
        except (ValueError, KeyError, OSError, sqlite3.Error):
            # Preserve the working fields, but never display stale confirmation
            # as an authority if the saved revision cannot be read.
            draft = draft.model_copy(deep=True)
            draft.status = "needs_review"
            draft.is_head = False
            draft.reasons = ("Reload the saved plan to check its current revision.",)
        bound = draft.binding_case_id is not None
        label = {
            "draft": "Draft",
            "unbound": "Plan saved",
            "needs_review": "Needs review",
            "confirmed": "Scope confirmed",
        }.get(draft.status, "New unsaved plan")
        current_case = bool(
            scope
            and scope.get("case_id") == draft.case_id
            and not scope.get("error")
            and draft.case_id
            and (session is None or session.get("case_id") == draft.case_id)
            and draft.binding_case_id in {None, draft.case_id}
        )
        saved = bool(draft.brief_id and draft.selected_revision_id)
        current = saved and draft.is_head and not draft.imported
        clean = not draft.dirty
        notices = [badge(label), badge("Evidence attached" if bound else "Evidence not attached")]
        if draft.dirty:
            notices.append(html.Strong("Unsaved changes are kept in this browser session."))
        if draft.number:
            notices.append(html.Span(f"Selected revision {draft.number}", className="rv-small"))
        if draft.imported:
            notices.append(
                html.P(
                    "Imported history: adopt locally, then confirm explicitly.",
                    className="rv-small",
                )
            )
        if draft.reasons:
            notices.append(
                html.Details(
                    [
                        html.Summary("Details to complete before confirmation"),
                        html.Ul(
                            [
                                html.Li(
                                    "Attach the plan to a saved evidence case."
                                    if reason == "Bind the draft to a saved case."
                                    else reason
                                )
                                for reason in draft.reasons
                            ],
                            className="rv-small",
                        ),
                    ],
                    className="rv-details",
                )
            )
        navigation = None
        stage = draft_stage(draft, (scope or {}).get("source_refs", ()), current_case=current_case)
        if stage in {"saved", "confirmed"} and session is not None and hasattr(service, "view"):
            try:
                view = (
                    service.view(session["case_id"], session.get("scenario_id"))
                    if session.get("case_id")
                    else {}
                )
                navigation = replace(
                    contextual_next_action(
                        view,
                        session,
                        page="cases",
                        planning_stage=stage,
                        purpose_id=draft.saved_fields.use_case_id,
                    ),
                    location="planning-journey",
                )
            except (ValueError, KeyError, OSError, sqlite3.Error):
                pass  # The shared guide supplies stale-context recovery.
        notices.append(
            _next_step(draft, scope or {}, current_case=current_case, navigation=navigation)
        )
        return (
            html.Div(notices),
            selected_revision_context(draft),
            bool(saved and not current),
            not (
                current_case
                and current
                and clean
                and set(draft.saved_fields.source_selection)
                == set((scope or {}).get("source_refs", ()))
            ),
            not (
                current_case
                and current
                and clean
                and bound
                and draft.binding_case_id == draft.case_id
                and draft.status != "confirmed"
            ),
            not (current_case and saved and clean and (draft.imported or not draft.is_head)),
            False,
            not current_case,
        )

    @app.callback(
        Output(PREFIX + "baseline-start", "disabled"),
        Output(PREFIX + "baseline-end", "disabled"),
        Output(PREFIX + "observation-start", "disabled"),
        Output(PREFIX + "observation-end", "disabled"),
        Input(PREFIX + "baseline-known", "value"),
        Input(PREFIX + "observation-known", "value"),
    )
    def window_controls(baseline, observation):
        return (
            baseline != "known",
            baseline != "known",
            observation != "known",
            observation != "known",
        )
