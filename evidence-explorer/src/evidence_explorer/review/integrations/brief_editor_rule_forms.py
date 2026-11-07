"""Optional activity-rule controls and lossless conversion to the shared contract."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, DecimalException
from typing import Any

from dash import dcc, html
from pydantic import ValidationError

from ...planning_briefs.models import BriefAlertRule
from .brief_editor_styles import PLANNING_TEXT_INPUT_STYLE

RULE_FIELD_NAMES = (
    "rule-enabled",
    "rule-metric",
    "rule-threshold-percent",
    "rule-direction",
    "rule-comparison",
    "rule-min-baseline",
    "rule-min-current",
    "rule-min-coverage-percent",
)

_RULE_ERRORS = {
    "metric": "Name the activity measure.",
    "threshold_percent": "Enter a positive, finite change threshold (%).",
    "direction": "Choose a change direction.",
    "comparison_basis": "Choose how to compare activity.",
    "min_baseline_count": "Enter a whole minimum baseline count of at least one.",
    "min_current_count": "Enter a whole minimum observed count of zero or more.",
    "min_coverage_fraction": (
        "Enter minimum source coverage greater than zero and no more than 100%."
    ),
}


def _validated_rule(data: dict[str, Any], *, saved: bool = False) -> BriefAlertRule:
    """Translate contract details at the form boundary while retaining the original cause."""
    try:
        return BriefAlertRule.model_validate(data)
    except ValidationError as exc:
        fallback = "Open a valid saved revision or start a new planning draft."
        messages = dict.fromkeys(
            _RULE_ERRORS.get((error["loc"] or (None,))[0], fallback)
            for error in exc.errors(include_url=False, include_input=False)
        )
        prefix = "The saved activity rule needs correction. " if saved else ""
        raise ValueError(prefix + " ".join(messages)) from exc


def blank_rule_values() -> dict[str, str | None]:
    return {
        **dict.fromkeys(RULE_FIELD_NAMES, ""),
        "rule-enabled": "disabled",
        "rule-direction": None,
        "rule-comparison": None,
    }


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def rule_form_values(rule: BriefAlertRule | None) -> dict[str, str | None]:
    if rule is None:
        return blank_rule_values()
    return {
        "rule-enabled": "enabled",
        "rule-metric": rule.metric,
        "rule-threshold-percent": _decimal_text(Decimal(str(rule.threshold_percent))),
        "rule-direction": rule.direction,
        "rule-comparison": rule.comparison_basis,
        "rule-min-baseline": str(rule.min_baseline_count),
        "rule-min-current": str(rule.min_current_count),
        "rule-min-coverage-percent": _decimal_text(Decimal(str(rule.min_coverage_fraction)) * 100),
    }


def parsed_rule(
    values: dict[str, Any], previous: dict[str, Any] | None, *, rule_seed: str | None = None
) -> BriefAlertRule | None:
    """Parse on explicit saves; field-only callers fall back to a configuration-derived ID."""
    existing = _validated_rule(previous, saved=True) if previous is not None else None
    if "rule-enabled" not in values:
        return existing  # Older browser sessions and field-only API callers have no rule controls.
    if values["rule-enabled"] == "disabled":
        return None
    if values["rule-enabled"] != "enabled":
        raise ValueError("Choose whether to include an activity rule.")
    if existing and all(
        values.get(name) == value for name, value in rule_form_values(existing).items()
    ):
        return existing

    def number(name: str, *, integer: bool = False):
        raw = values.get(name)
        if isinstance(raw, bool) or raw is None:
            return raw  # Let the shared contract reject flags and missing values.
        try:
            text = str(raw).strip()
            return int(text) if integer else float(text)
        except ValueError:
            return raw  # Preserve invalid text for field-specific contract validation.

    metric = str(values.get("rule-metric") or "").strip()
    coverage_fraction = values.get("rule-min-coverage-percent")
    if not isinstance(coverage_fraction, bool) and coverage_fraction is not None:
        try:
            coverage_fraction = float(Decimal(str(coverage_fraction).strip()) / 100)
        except (DecimalException, OverflowError, ValueError):
            pass  # The shared contract rejects the original invalid value below.
    visible = {
        "metric": metric,
        "threshold_percent": number("rule-threshold-percent"),
        "direction": values.get("rule-direction"),
        "comparison_basis": values.get("rule-comparison"),
        "min_baseline_count": number("rule-min-baseline", integer=True),
        "min_current_count": number("rule-min-current", integer=True),
        "min_coverage_fraction": coverage_fraction,
    }
    identity_seed = rule_seed if rule_seed is not None else json.dumps(visible, sort_keys=True)
    identity = hashlib.sha256(identity_seed.encode()).hexdigest()[:20]
    parsed = _validated_rule(
        {
            **visible,
            "id": existing.id if existing else "activity-rule-" + identity,
            "title": "Activity rule: " + metric,
            "threshold_basis": "analyst_configured",
        }
    )
    # Formatting a saved number differently does not change its provenance.
    if existing and all(getattr(existing, name) == getattr(parsed, name) for name in visible):
        return existing
    return parsed


def activity_rule_fields(prefix: str):
    """Keep advanced inputs behind a disclosure, without a preselected threshold."""

    def field(label, suffix, *, choices=None, help_text=None):
        identifier = prefix + suffix
        component = (
            dcc.Dropdown(
                id=identifier,
                options=[{"label": label, "value": value} for label, value in choices],
                value=None,
                clearable=True,
                placeholder="Choose an option",
            )
            if choices
            else dcc.Input(
                id=identifier,
                value="",
                type="text",
                style=PLANNING_TEXT_INPUT_STYLE.copy(),
            )
        )
        return html.Div(
            [
                html.Label(label, htmlFor=identifier),
                component,
                html.P(help_text, className="rv-small rv-muted") if help_text else None,
            ],
            style={"minWidth": 0},
        )

    return html.Details(
        [
            html.Summary("Optional activity rule"),
            html.P(
                "A rule asks for review when activity changes. It does not approve evidence "
                "or confirm this planning scope. Disabled rule fields are ignored when saving.",
                className="rv-small rv-muted",
            ),
            html.Label("Include an activity rule", htmlFor=prefix + "rule-enabled"),
            dcc.Dropdown(
                id=prefix + "rule-enabled",
                options=[
                    {"label": "No activity rule", "value": "disabled"},
                    {"label": "Use the rule entered below", "value": "enabled"},
                ],
                value="disabled",
                clearable=False,
                searchable=False,
            ),
            field("Activity measure label", "rule-metric"),
            field("Change threshold (%)", "rule-threshold-percent", help_text="Greater than zero."),
            field(
                "Change direction",
                "rule-direction",
                choices=[("Increase", "increase"), ("Decrease", "decrease"), ("Either", "either")],
            ),
            field(
                "How to compare activity",
                "rule-comparison",
                choices=[
                    ("Counts in equal-length time windows", "equal_duration_counts"),
                    ("Counts adjusted for observation opportunity", "exposure_normalized"),
                ],
            ),
            field(
                "Minimum baseline items",
                "rule-min-baseline",
                help_text="Whole number, at least one.",
            ),
            field(
                "Minimum observed items",
                "rule-min-current",
                help_text="Whole number, zero or more.",
            ),
            field(
                "Minimum source coverage (%)",
                "rule-min-coverage-percent",
                help_text="Greater than zero and no more than 100.",
            ),
        ],
        className="rv-details",
        style={"minWidth": 0},
    )
