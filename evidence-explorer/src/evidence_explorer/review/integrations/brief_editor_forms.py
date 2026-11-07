"""Planning form controls and lossless browser-to-contract field conversion."""

from __future__ import annotations

from typing import Any

from dash import dcc, html

from ...planning_briefs.models import BriefWindow, DraftFields
from .brief_editor_rule_forms import (
    RULE_FIELD_NAMES,
    activity_rule_fields,
    blank_rule_values,
    parsed_rule,
    rule_form_values,
)
from .brief_editor_styles import PLANNING_TEXT_INPUT_STYLE
from .use_cases_ui import purpose_options

PREFIX = "trail-planning-"
FIELD_NAMES = (
    "purpose",
    "question",
    "topics",
    "sources",
    "role",
    "output",
    "languages",
    "baseline-known",
    "baseline-start",
    "baseline-end",
    "observation-known",
    "observation-start",
    "observation-end",
    *RULE_FIELD_NAMES,
)


def blank_fields() -> dict[str, Any]:
    values = dict.fromkeys(FIELD_NAMES, "")
    values.update(purpose=None, output=None)
    values["baseline-known"] = values["observation-known"] = "unknown"
    values.update(blank_rule_values())
    return values


def form_values(fields: DraftFields) -> dict[str, Any]:
    values = {
        **blank_fields(),
        "purpose": fields.use_case_id,
        "question": fields.purpose or "",
        "topics": "\n".join(fields.public_subjects_or_topics),
        "sources": "\n".join(fields.source_selection),
        "role": fields.reviewer_role or "",
        "output": fields.expected_output,
        "languages": "\n".join(fields.languages),
        **rule_form_values(fields.alert_rule),
    }
    for name in ("baseline", "observation"):
        window = getattr(fields, f"{name}_window")
        if window is not None:
            values[f"{name}-known"] = "known"
            values[f"{name}-start"] = window.start.isoformat() if window.start else ""
            values[f"{name}-end"] = window.end.isoformat() if window.end else ""
    return values


def parsed_fields(
    values: dict[str, Any],
    previous: dict[str, Any] | None = None,
    *,
    rule_seed: str | None = None,
) -> DraftFields:
    """Keep invalid edit text in the browser; only explicit saves parse the contract."""

    def lines(name):
        return tuple(
            line.strip() for line in str(values.get(name) or "").splitlines() if line.strip()
        )

    data = dict(previous or {})
    data.update(
        use_case_id=values.get("purpose") or None,
        purpose=values.get("question") or None,
        public_subjects_or_topics=lines("topics"),
        source_selection=lines("sources"),
        reviewer_role=values.get("role") or None,
        expected_output=values.get("output") or None,
        languages=lines("languages"),
        alert_rule=parsed_rule(values, data.get("alert_rule"), rule_seed=rule_seed),
    )
    for name in ("baseline", "observation"):
        key = f"{name}_window"
        if values.get(f"{name}-known") != "known":
            data[key] = None
        else:
            previous_window = data.get(key) or {}
            data[key] = BriefWindow.model_validate(
                {
                    "id": previous_window.get("id", name),
                    "start": str(values.get(f"{name}-start") or "").strip() or None,
                    "end": str(values.get(f"{name}-end") or "").strip() or None,
                }
            )
    # The public-information scope survives an edit; rules change only through
    # explicit fields and are validated by the shared planning contract.
    return DraftFields.model_validate(data)


def _field(label, component, help_text=None):
    return html.Div(
        [
            html.Label(label, htmlFor=component.id),
            component,
            html.P(help_text, className="rv-small rv-muted") if help_text else None,
        ],
        style={"minWidth": 0},
    )


def _window(name, title):
    return html.Fieldset(
        [
            html.Legend(title),
            _field(
                "Window availability",
                dcc.Dropdown(
                    id=PREFIX + name + "-known",
                    options=[
                        {"label": "Unknown", "value": "unknown"},
                        {"label": "Enter an exact time window", "value": "known"},
                    ],
                    value="unknown",
                    clearable=False,
                    searchable=False,
                ),
            ),
            _field(
                "Start, including UTC offset",
                dcc.Input(
                    id=PREFIX + name + "-start",
                    type="text",
                    value="",
                    disabled=True,
                    placeholder="2026-11-07T09:00:00+01:00",
                    style=PLANNING_TEXT_INPUT_STYLE.copy(),
                ),
            ),
            _field(
                "End, including UTC offset",
                dcc.Input(
                    id=PREFIX + name + "-end",
                    type="text",
                    value="",
                    disabled=True,
                    placeholder="2026-11-08T09:00:00+01:00",
                    style=PLANNING_TEXT_INPUT_STYLE.copy(),
                ),
            ),
        ],
        style={"minWidth": 0, "display": "grid", "gap": "10px"},
    )


def editor_fields():
    """Blank fields describe intention; catalog choices never fill the user's scope."""
    return html.Div(
        [
            _field(
                "Question you want to investigate",
                dcc.Textarea(
                    id=PREFIX + "question",
                    value="",
                    className="rv-textarea",
                    maxLength=4000,
                ),
            ),
            _field(
                "Investigation purpose",
                dcc.Dropdown(
                    id=PREFIX + "purpose",
                    options=purpose_options(),
                    value=None,
                    placeholder="Choose a purpose",
                    clearable=True,
                ),
            ),
            _field(
                "Public subjects or topics, one per line",
                dcc.Textarea(
                    id=PREFIX + "topics",
                    value="",
                    className="rv-textarea",
                ),
            ),
            _field(
                "Reviewer role",
                dcc.Input(
                    id=PREFIX + "role",
                    type="text",
                    value="",
                    maxLength=500,
                    style=PLANNING_TEXT_INPUT_STYLE.copy(),
                ),
            ),
            _field(
                "Intended output",
                dcc.Dropdown(
                    id=PREFIX + "output",
                    value=None,
                    clearable=True,
                    options=[
                        {"label": "Reviewed brief", "value": "reviewed_brief"},
                        {"label": "Evidence dossier", "value": "evidence_dossier"},
                    ],
                    placeholder="Choose an output",
                ),
            ),
            html.Details(
                [
                    html.Summary("Source references and languages"),
                    _field(
                        "Exact source references, one per line",
                        dcc.Textarea(
                            id=PREFIX + "sources",
                            value="",
                            className="rv-textarea",
                        ),
                        "After gathering evidence, use this case's sources in the plan. "
                        "Attaching evidence checks these references against the saved case.",
                    ),
                    _field(
                        "Languages, one per line (optional)",
                        dcc.Textarea(
                            id=PREFIX + "languages",
                            value="",
                            className="rv-textarea",
                        ),
                    ),
                ],
                className="rv-details",
            ),
            html.Details(
                [
                    html.Summary("Time windows and activity comparison"),
                    html.P(
                        "Leave time windows unknown while planning. For activity comparison, "
                        "set both windows and an explicit comparison rule before confirming "
                        "scope. Purposes requiring a baseline need both windows with UTC offsets.",
                        className="rv-small rv-muted",
                    ),
                    html.Div(
                        [
                            _window("baseline", "Baseline window"),
                            _window("observation", "Observation window"),
                        ],
                        style={"display": "grid", "gap": "12px"},
                    ),
                    activity_rule_fields(PREFIX),
                ],
                className="rv-details",
            ),
        ],
        style={"display": "grid", "gap": "14px", "minWidth": 0},
    )
