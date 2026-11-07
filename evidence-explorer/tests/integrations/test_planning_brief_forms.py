"""Activity-rule editing keeps raw drafts separate from validated planning rules."""

from types import SimpleNamespace

import pytest
from dash import dcc
from dash.development.base_component import Component
from pydantic import ValidationError

from evidence_explorer.planning_briefs.models import BriefAlertRule, DraftFields
from evidence_explorer.review.integrations.brief_editor_forms import (
    FIELD_NAMES,
    PREFIX,
    blank_fields,
    editor_fields,
    form_values,
    parsed_fields,
)
from evidence_explorer.review.integrations.brief_editor_rule_forms import RULE_FIELD_NAMES
from evidence_explorer.review.integrations.brief_editor_state import (
    EditorDraft,
    EditorStore,
    apply_editor_action,
    selected_revision_context,
)


def saved_rule(**changes):
    return BriefAlertRule.model_validate(
        {
            "id": "origin-illustration",
            "title": "Imported illustrative threshold",
            "metric": "Public posts",
            "threshold_percent": 35,
            "threshold_basis": "illustrative_unvalidated",
            "direction": "increase",
            "comparison_basis": "exposure_normalized",
            "min_baseline_count": 5,
            "min_current_count": 0,
            "min_coverage_fraction": 0.8,
            **changes,
        }
    )


def rule_values(**changes):
    return {
        **blank_fields(),
        "rule-enabled": "enabled",
        "rule-metric": "Public posts",
        "rule-threshold-percent": "35",
        "rule-direction": "increase",
        "rule-comparison": "exposure_normalized",
        "rule-min-baseline": "5",
        "rule-min-current": "0",
        "rule-min-coverage-percent": "80",
        **changes,
    }


def descendants(node):
    if isinstance(node, Component):
        yield node
        yield from descendants(getattr(node, "children", None))
    elif isinstance(node, (list, tuple)):
        for child in node:
            yield from descendants(child)


def test_optional_rule_starts_disabled_and_blank_without_an_illustrative_default():
    values = blank_fields()
    assert values["rule-enabled"] == "disabled"
    assert all(values[name] in ("", None) for name in RULE_FIELD_NAMES if name != "rule-enabled")
    assert parsed_fields(values).alert_rule is None
    nodes = {node.id: node for node in descendants(editor_fields()) if hasattr(node, "id")}
    assert set(PREFIX + name for name in FIELD_NAMES) == set(nodes)
    for name in RULE_FIELD_NAMES:
        assert nodes[PREFIX + name].value == values[name]
    assert all(
        isinstance(nodes[PREFIX + name], dcc.Input) and nodes[PREFIX + name].type == "text"
        for name in RULE_FIELD_NAMES
        if name not in {"rule-enabled", "rule-direction", "rule-comparison"}
    )


def test_planning_inputs_pair_shared_text_surface_and_boundary_roles():
    inputs = {
        node.id.removeprefix(PREFIX): node
        for node in descendants(editor_fields())
        if isinstance(node, dcc.Input)
    }
    for control in inputs.values():
        assert control.type == "text" and control.value == ""
        assert control.style == inputs["rule-metric"].style
        assert control.style["color"] == "var(--rv-ink)"
        assert control.style["backgroundColor"] == "var(--rv-surface)"
        assert "var(--rv-input-border)" in control.style["border"]
        assert control.style["--Dash-Fill-Interactive-Strong"] == "var(--rv-focus)"
        assert control.style["--Dash-Fill-Inverse-Strong"] == "var(--rv-surface)"
        assert "outline" not in control.style  # Keep the shared keyboard-focus ring.
    assert inputs["role"].maxLength == 500
    for window in ("baseline", "observation"):
        assert inputs[f"{window}-start"].disabled is True
        assert inputs[f"{window}-start"].placeholder == "2026-11-07T09:00:00+01:00"
        assert inputs[f"{window}-end"].disabled is True
        assert inputs[f"{window}-end"].placeholder == "2026-11-08T09:00:00+01:00"


def test_explicit_rule_roundtrip_is_analyst_configured_and_keeps_zero_current_count():
    values = rule_values()
    fields = parsed_fields(values)
    rule = fields.alert_rule
    assert rule.threshold_basis == "analyst_configured"
    assert rule.min_baseline_count == 5 and rule.min_current_count == 0
    assert rule.min_coverage_fraction == 0.8
    assert rule.threshold_percent == 35
    assert parsed_fields(values).alert_rule.id == rule.id
    assert parsed_fields(form_values(fields), fields.model_dump(mode="json")) == fields


def test_editor_rule_identity_is_local_to_each_draft_and_stable_after_failed_save():
    class FailedWrites:
        def __init__(self):
            self.rules = []

        def create_draft(self, *, fields, author):
            self.rules.append(fields.alert_rule)
            raise ValueError("Simulated persistence conflict")

    planner = FailedWrites()
    service = SimpleNamespace(planning_briefs=planner)
    first, _, error = apply_editor_action(service, "save", {}, {}, rule_values())
    assert error
    retried, _, error = apply_editor_action(
        service, "save", {}, first.model_dump(mode="json"), rule_values()
    )
    assert error and retried.active == first.active
    second, _, error = apply_editor_action(service, "save", {}, {}, rule_values())
    assert error and second.active != first.active
    assert planner.rules[0].id == planner.rules[1].id != planner.rules[2].id
    assert planner.rules[0].threshold_basis == "analyst_configured"


def test_existing_rule_id_ignores_new_plan_seed_even_after_intentional_edit():
    fields = DraftFields(alert_rule=saved_rule())
    values = {**form_values(fields), "rule-threshold-percent": "36"}
    edited = parsed_fields(values, fields.model_dump(mode="json"), rule_seed="different-plan")
    assert edited.alert_rule.id == fields.alert_rule.id
    assert edited.alert_rule.threshold_basis == "analyst_configured"


def test_large_change_percent_and_whole_counts_keep_contract_precision():
    fields = parsed_fields(
        rule_values(
            **{
                "rule-threshold-percent": "350",
                "rule-min-baseline": "9007199254740993",
                "rule-min-coverage-percent": "100",
            }
        )
    )
    assert fields.alert_rule.threshold_percent == 350
    assert fields.alert_rule.min_baseline_count == 9007199254740993
    assert fields.alert_rule.min_coverage_fraction == 1


def test_unchanged_imported_rule_preserves_all_origin_metadata():
    fields = DraftFields(alert_rule=saved_rule(), purpose="Saved question")
    values = form_values(fields)
    values["question"] = "Changed question, same activity rule"
    updated = parsed_fields(values, fields.model_dump(mode="json"))
    assert updated.alert_rule.model_dump(mode="json") == fields.alert_rule.model_dump(mode="json")
    values["rule-threshold-percent"] = "35.00"
    assert parsed_fields(values, fields.model_dump(mode="json")).alert_rule == fields.alert_rule


@pytest.mark.parametrize("coverage", [0.007, 0.013, 0.014, 0.12345678901234568])
def test_editing_another_rule_field_preserves_exact_saved_coverage(coverage):
    fields = DraftFields(alert_rule=saved_rule(min_coverage_fraction=coverage))
    values = form_values(fields)
    assert parsed_fields(values, fields.model_dump(mode="json")).alert_rule == fields.alert_rule
    values["rule-threshold-percent"] = "36"
    edited = parsed_fields(values, fields.model_dump(mode="json")).alert_rule
    assert edited.min_coverage_fraction == coverage
    assert edited.threshold_basis == "analyst_configured"
    assert edited.id == fields.alert_rule.id
    assert edited.title != fields.alert_rule.title


def test_rule_can_be_explicitly_removed_without_parsing_disabled_invalid_text():
    fields = DraftFields(alert_rule=saved_rule())
    values = {**form_values(fields), "rule-enabled": "disabled", "rule-threshold-percent": "bad"}
    assert parsed_fields(values, fields.model_dump(mode="json")).alert_rule is None
    legacy = {
        name: value for name, value in form_values(fields).items() if name not in RULE_FIELD_NAMES
    }
    assert parsed_fields(legacy, fields.model_dump(mode="json")).alert_rule == fields.alert_rule


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("rule-metric", " "),
        ("rule-threshold-percent", ""),
        ("rule-threshold-percent", "0"),
        ("rule-threshold-percent", "NaN"),
        ("rule-threshold-percent", "Infinity"),
        ("rule-threshold-percent", True),
        ("rule-direction", None),
        ("rule-comparison", "unknown"),
        ("rule-min-baseline", "0"),
        ("rule-min-baseline", "5.5"),
        ("rule-min-baseline", "5.0"),
        ("rule-min-baseline", True),
        ("rule-min-current", "-1"),
        ("rule-min-current", "1e2"),
        ("rule-min-coverage-percent", "0"),
        ("rule-min-coverage-percent", "101"),
        ("rule-min-coverage-percent", "NaN"),
        ("rule-min-coverage-percent", "1e1000000"),
        ("rule-min-coverage-percent", True),
    ],
)
def test_enabled_rules_reject_missing_invalid_and_nonfinite_values(name, value):
    with pytest.raises(ValueError):
        parsed_fields(rule_values(**{name: value}))


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("rule-direction", None, "Choose a change direction."),
        ("rule-comparison", None, "Choose how to compare activity."),
        ("rule-metric", " ", "Name the activity measure."),
        ("rule-threshold-percent", "0", "Enter a positive, finite change threshold (%)."),
        ("rule-threshold-percent", "NaN", "Enter a positive, finite change threshold (%)."),
        ("rule-threshold-percent", "bad", "Enter a positive, finite change threshold (%)."),
        ("rule-threshold-percent", True, "Enter a positive, finite change threshold (%)."),
        ("rule-min-baseline", "0", "Enter a whole minimum baseline count of at least one."),
        ("rule-min-baseline", "5.0", "Enter a whole minimum baseline count of at least one."),
        ("rule-min-current", "-1", "Enter a whole minimum observed count of zero or more."),
        (
            "rule-min-coverage-percent",
            "101",
            "Enter minimum source coverage greater than zero and no more than 100%.",
        ),
        (
            "rule-min-coverage-percent",
            "bad",
            "Enter minimum source coverage greater than zero and no more than 100%.",
        ),
    ],
)
def test_rule_validation_messages_name_the_field_and_keep_original_cause(name, value, message):
    values = rule_values(**{name: value})
    original = dict(values)
    with pytest.raises(ValueError) as failed:
        parsed_fields(values)
    assert str(failed.value) == message
    assert isinstance(failed.value.__cause__, ValidationError)
    assert values == original
    assert not any(
        fragment in str(failed.value)
        for fragment in ("literal_error", "NoneType", "input_type", "pydantic", "http", "type=")
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"direction": None}, "Choose a change direction."),
        ({"min_current_count": "0"}, "Enter a whole minimum observed count of zero or more."),
        ({"id": ""}, "Open a valid saved revision or start a new planning draft."),
    ],
)
def test_invalid_saved_rule_has_readable_message_with_chained_validation(changes, message):
    previous = {**saved_rule().model_dump(mode="json"), **changes}
    with pytest.raises(ValueError) as failed:
        parsed_fields(blank_fields(), {"alert_rule": previous})
    assert str(failed.value) == "The saved activity rule needs correction. " + message
    assert isinstance(failed.value.__cause__, ValidationError)


def test_nonobject_saved_rule_has_readable_message_instead_of_validation_internals():
    with pytest.raises(ValueError) as failed:
        parsed_fields(blank_fields(), {"alert_rule": "malformed saved rule"})
    assert str(failed.value) == (
        "The saved activity rule needs correction. "
        "Open a valid saved revision or start a new planning draft."
    )
    assert isinstance(failed.value.__cause__, ValidationError)


def test_cleared_direction_save_keeps_raw_draft_and_returns_only_human_instruction():
    values = rule_values(**{"rule-direction": None})
    store, message, error = apply_editor_action(
        SimpleNamespace(planning_briefs=object()), "save", {}, {}, values
    )
    assert error and message.startswith("Choose a change direction.")
    assert store.for_case(None).fields == values
    assert "literal_error" not in message and "http" not in message and "NoneType" not in message


def test_enabled_rule_requires_every_field_without_count_or_percentage_defaults():
    for name in RULE_FIELD_NAMES:
        if name != "rule-enabled":
            with pytest.raises(ValueError):
                parsed_fields(rule_values(**{name: None}))


def test_equal_count_rule_uses_existing_window_validation():
    values = rule_values(
        **{
            "rule-comparison": "equal_duration_counts",
            "baseline-known": "known",
            "baseline-start": "2026-11-01T00:00:00Z",
            "baseline-end": "2026-11-02T00:00:00Z",
            "observation-known": "known",
            "observation-start": "2026-11-03T00:00:00Z",
            "observation-end": "2026-11-05T00:00:00Z",
        }
    )
    with pytest.raises(ValueError, match="equal UTC window durations"):
        parsed_fields(values)


@pytest.mark.parametrize(
    "edit", [{}, {"question": "Unsaved legacy question"}, {"baseline-start": "invalid raw time"}]
)
def test_legacy_browser_fields_upgrade_without_losing_raw_edits_or_saved_rule(edit):
    fields = DraftFields(purpose="Saved question", alert_rule=saved_rule())
    legacy = {
        name: value for name, value in form_values(fields).items() if name not in RULE_FIELD_NAMES
    }
    draft = EditorDraft.model_validate(
        {"fields": {**legacy, **edit}, "saved_fields": fields.model_dump(mode="json")}
    )
    assert set(FIELD_NAMES) <= draft.fields.keys()
    assert draft.dirty == bool(edit)
    assert draft.saved_fields.alert_rule == fields.alert_rule
    assert {name: draft.fields[name] for name in edit} == edit
    assert (
        parsed_fields(form_values(fields), fields.model_dump(mode="json")).alert_rule
        == fields.alert_rule
    )


def test_invalid_raw_rule_survives_failed_save_without_calling_persistence():
    class NoWrites:
        def create_draft(self, **kwargs):
            raise AssertionError("Invalid rule must never reach persistence")

    values = rule_values(**{"rule-threshold-percent": "thirty five"})
    store, message, error = apply_editor_action(
        SimpleNamespace(planning_briefs=NoWrites()), "save", {}, {}, values
    )
    draft = store.for_case(None)
    assert error and "unsaved fields are kept" in message
    assert draft.fields["rule-threshold-percent"] == "thirty five"
    assert draft.dirty and draft.brief_id is None
    restored = EditorStore.model_validate(store.model_dump(mode="json")).for_case(None)
    assert restored.fields == draft.fields


def test_rule_edit_is_dirty_and_blocks_confirmation_before_explicit_save():
    class NoWrites:
        def confirm(self, *args, **kwargs):
            raise AssertionError("Unsaved rule must never reach confirmation")

        def load(self, *args):
            raise KeyError("No saved refresh needed for this test")

    fields = DraftFields(alert_rule=saved_rule())
    values = {**form_values(fields), "rule-threshold-percent": "36"}
    store = EditorStore()
    store.for_case("case-a")
    store.drafts[store.active] = EditorDraft(
        case_id="case-a",
        brief_id="brief-a",
        selected_revision_id="revision-a",
        expected_head_revision_id="revision-a",
        saved_fields=fields,
        fields=values,
    )
    failed, _, error = apply_editor_action(
        SimpleNamespace(planning_briefs=NoWrites()),
        "confirm",
        {"case_id": "case-a"},
        store.model_dump(mode="json"),
        values,
    )
    assert error
    assert selected_revision_context(failed.for_case("case-a"))["dirty"]
