"""Recoverable planning working copies and explicit revision command transitions.

This module owns optimistic-write expectations. Refreshing a saved state may update
labels, but never advances the revision against which the user started editing.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...planning_briefs.models import BriefState, DraftFields
from .brief_editor_forms import FIELD_NAMES, blank_fields, form_values, parsed_fields


class EditorDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str | None = None
    brief_id: str | None = None
    selected_revision_id: str | None = None
    base_revision_id: str | None = None
    expected_head_revision_id: str | None = None
    current_head_revision_id: str | None = None
    number: int | None = None
    fields: dict[str, str | None] = Field(default_factory=blank_fields)
    saved_fields: DraftFields = Field(default_factory=DraftFields)
    status: str | None = None
    reasons: tuple[str, ...] = ()
    is_head: bool = False
    imported: bool = False
    is_active_for_case: bool = False
    binding_case_id: str | None = None
    scope_fingerprint: str | None = None
    content_hash: str | None = None
    confirmation_id: str | None = None
    evidence_requested: bool = False

    @model_validator(mode="after")
    def complete_form_fields(self):
        """Upgrade browser working copies without dropping raw edits or saved rule metadata."""
        self.fields = {**form_values(self.saved_fields), **self.fields}
        return self

    @property
    def dirty(self) -> bool:
        return self.fields != form_values(self.saved_fields)


class PlanContinuation(BaseModel):
    """A browser suggestion for an explicitly saved plan, never a case binding."""

    model_config = ConfigDict(extra="forbid")
    brief_id: str
    revision_id: str
    origin_case_id: str | None = None


class EditorStore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: str | None = None
    cases: dict[str, str] = Field(default_factory=dict)
    drafts: dict[str, EditorDraft] = Field(default_factory=dict)
    continuation: PlanContinuation | None = None

    def remember_plan(self, case_id: str | None, state: BriefState) -> None:
        if state.is_head and not state.imported and state.revision.binding is None:
            self.continuation = PlanContinuation(
                brief_id=state.revision.brief_id,
                revision_id=state.revision.revision_id,
                origin_case_id=case_id,
            )
        elif self.continuation and self.continuation.brief_id == state.revision.brief_id:
            self.continuation = None

    def for_case(self, case_id: str | None) -> EditorDraft:
        key = json.dumps([case_id], separators=(",", ":"))
        active = self.cases.get(key)
        if active not in self.drafts:
            active = str(uuid4())
            self.drafts[active] = EditorDraft(case_id=case_id)
            self.cases[key] = active
        self.active = active
        return self.drafts[active]

    def open_state(self, case_id: str | None, state: BriefState) -> EditorDraft:
        key = json.dumps([case_id, state.revision.brief_id, state.revision.revision_id])
        draft = self.drafts.get(key)
        if draft is None:
            draft = EditorDraft(
                case_id=case_id,
                fields=form_values(state.revision.fields),
                saved_fields=state.revision.fields,
                base_revision_id=state.revision.revision_id,
                expected_head_revision_id=state.head_revision_id,
            )
        _remember_state(draft, state)
        self.drafts[key] = draft
        self.cases[json.dumps([case_id], separators=(",", ":"))] = key
        self.active = key
        return draft


def _remember_state(draft: EditorDraft, state: BriefState) -> None:
    """Refresh server-derived labels without advancing a captured write expectation."""
    revision = state.revision
    draft.brief_id = revision.brief_id
    draft.selected_revision_id = revision.revision_id
    draft.current_head_revision_id = state.head_revision_id
    draft.number = revision.number
    draft.status = state.status
    draft.reasons = state.reasons
    draft.is_head = state.is_head
    draft.imported = state.imported
    draft.is_active_for_case = state.is_active_for_case
    draft.binding_case_id = revision.binding.case_id if revision.binding else None
    draft.scope_fingerprint = revision.binding.scope.scope_fingerprint if revision.binding else None
    draft.content_hash = revision.content_hash
    draft.confirmation_id = state.confirmation.confirmation_id if state.confirmation else None


def case_scope_context(service, session) -> dict[str, Any]:
    """Read frozen case inputs, never a live-source picker or another case's plan."""
    state = session if isinstance(session, dict) else {}
    case_id = state.get("case_id")
    if not case_id:
        return {"case_id": None, "source_refs": []}
    binding = service.planning_briefs.case_binding(case_id)
    fingerprint = binding.source_case_fingerprint
    if fingerprint != state.get("case_fingerprint"):
        raise ValueError("The case changed. Choose the current case again before binding a plan.")
    scope = binding.scope
    title = service.repository.get_case_metadata(case_id).case.title
    return {
        "case_id": case_id,
        "title": title,
        "case_fingerprint": fingerprint,
        "scope_fingerprint": scope.scope_fingerprint,
        "basis": scope.basis,
        "source_refs": list(scope.source_refs),
    }


def _required_scope(case_id, scope):
    if not case_id or scope.get("case_id") != case_id or scope.get("error"):
        raise ValueError("Select a current saved case before binding or confirming this plan.")
    return scope["case_fingerprint"]


def _saved_action(draft):
    if not draft.brief_id or not draft.selected_revision_id:
        raise ValueError("Save a planning revision first.")
    if draft.dirty:
        raise ValueError(
            "Save your edits, or copy them to a new unbound draft, before using a saved revision."
        )


def apply_editor_action(
    service,
    action,
    session,
    stored,
    values,
    picker=None,
    revision=None,
    scope=None,
    visible_key=None,
):
    """Run one explicit UI action; failed writes leave all working copies intact."""
    case_id = (session or {}).get("case_id")
    store = EditorStore.model_validate(stored or {})
    previous = store.drafts.get(store.active)
    changed_case = previous is not None and previous.case_id != case_id
    if changed_case and action == "context" and visible_key == store.active:
        # The old form is still visible when the case selection arrives. Capture
        # its last keystroke before displaying the target case's separate copy.
        previous.fields = {**blank_fields(), **values}
    draft = store.for_case(case_id)
    if changed_case and action != "context":
        return (
            store,
            "The selected case changed. Its own planning fields are now shown; "
            "repeat the action intentionally.",
            True,
        )
    if action in FIELD_NAMES:
        draft.fields = {name: values.get(name) for name in FIELD_NAMES}
        return store, "", False
    if action == "context":
        try:
            if draft.brief_id and draft.selected_revision_id:
                _remember_state(
                    draft, service.planning_briefs.load(draft.brief_id, draft.selected_revision_id)
                )
            return store, "", False
        except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
            return store, str(exc) + " Your planning edits are kept.", True
    # The last keystroke may precede the browser's edit-store round trip. Keep
    # those visible fields even when opening another revision fails validation.
    if action in {"load", "new"}:
        draft.fields = {**blank_fields(), **values}
    if action == "new":
        store.continuation = None
        key = str(uuid4())
        store.active = key
        store.drafts[key] = EditorDraft(case_id=case_id)
        store.cases[json.dumps([case_id], separators=(",", ":"))] = key
        return (
            store,
            "New unsaved planning draft. Earlier edits are kept in this browser session.",
            False,
        )
    planner = service.planning_briefs
    if action in {"save", "copy", "bind", "confirm", "adopt", "use-scope", "resume"}:
        draft.fields = {**blank_fields(), **values}
    try:
        if action == "load":
            if not picker or not revision:
                raise ValueError("Choose a saved draft and an exact revision to open.")
            loaded = planner.load(picker, revision)
            if loaded.revision.binding and loaded.revision.binding.case_id != case_id:
                raise ValueError(
                    "Open the case associated with this bound planning revision first."
                )
            draft = store.open_state(case_id, loaded)
            draft.evidence_requested = bool(case_id)
            store.remember_plan(case_id, loaded)
            return (
                store,
                (
                    "Opened the saved revision; your unsaved edits for it are retained."
                    if draft.dirty
                    else "Opened the exact saved planning revision."
                ),
                False,
            )
        if action == "resume":
            _required_scope(case_id, scope or {})
            actual_scope = case_scope_context(service, session)
            if any(
                (scope or {}).get(key) != actual_scope.get(key)
                for key in ("case_fingerprint", "scope_fingerprint", "source_refs")
            ):
                raise ValueError("The selected evidence changed. Refresh its scope and try again.")
            pending = store.continuation
            if pending is None:
                raise ValueError("Choose a saved plan to continue.")
            loaded = planner.load(pending.brief_id, pending.revision_id)
            if loaded.imported or not loaded.is_head or loaded.revision.binding is not None:
                raise ValueError(
                    "The saved plan changed. Open its current version before choosing evidence."
                )
            key = json.dumps([case_id, pending.brief_id, pending.revision_id])
            existing = store.drafts.get(key)
            if existing and existing.dirty:
                raise ValueError(
                    "This case already has unsaved edits for that plan. "
                    "Open the saved plan to recover them before replacing its sources."
                )
            # Opening another working copy retains the previous one. Only the
            # subsequent Save, Attach and Confirm commands can persist changes.
            draft = store.open_state(case_id, loaded)
            draft.fields["sources"] = "\n".join(scope["source_refs"])
            draft.evidence_requested = True
            return (
                store,
                "Saved plan opened with this case's sources in the working draft. "
                "Review them and Save plan; evidence is not attached yet. Earlier edits are kept.",
                False,
            )
        if action == "use-scope":
            _required_scope(case_id, scope or {})
            draft.fields["sources"] = "\n".join(scope["source_refs"])
            draft.evidence_requested = True
            return (
                store,
                "Sources copied into the plan. Next: Save plan, then Attach collected evidence.",
                False,
            )
        if action == "copy":
            saved = planner.create_draft(
                author="Local analyst",
                fields=parsed_fields(
                    draft.fields,
                    draft.saved_fields.model_dump(mode="json"),
                    rule_seed=draft.brief_id or store.active,
                ),
            )
            message = (
                "Copied fields into a new unbound draft. No approval or binding was transferred."
            )
        elif action == "save":
            fields = parsed_fields(
                draft.fields,
                draft.saved_fields.model_dump(mode="json"),
                rule_seed=draft.brief_id or store.active,
            )
            if draft.brief_id is None:
                saved = planner.create_draft(author="Local analyst", fields=fields)
            else:
                if draft.imported or draft.base_revision_id != draft.expected_head_revision_id:
                    raise ValueError(
                        "A historical or imported revision cannot silently replace the current "
                        "draft. Adopt it explicitly or copy its fields."
                    )
                saved = planner.save_draft(
                    draft.brief_id,
                    fields,
                    base_revision_id=draft.base_revision_id,
                    expected_head_revision_id=draft.expected_head_revision_id,
                    author="Local analyst",
                )
            if saved.binding:
                message = "Updated plan saved. Next: check the changes and confirm scope again."
            elif case_id and set(fields.source_selection) == set(
                (scope or {}).get("source_refs", ())
            ):
                message = "Plan and source selection saved. Next: Attach collected evidence."
            elif case_id and (draft.evidence_requested or fields.source_selection):
                message = (
                    "Plan saved. Next: use this case's sources, save any changes, "
                    "and attach the evidence."
                )
            else:
                message = (
                    "Plan saved. Next: gather evidence, then reopen this plan to attach the case."
                )
        elif action == "adopt":
            _saved_action(draft)
            saved = planner.adopt_revision(
                draft.selected_revision_id,
                case_id,
                expected_head_revision_id=draft.expected_head_revision_id,
                expected_case_fingerprint=_required_scope(case_id, scope or {}),
                author="Local analyst",
            )
            message = (
                "Adopted as a new local revision for this case. "
                "Confirm its planning scope explicitly."
            )
        elif action in {"bind", "confirm"}:
            _saved_action(draft)
            if draft.imported or draft.selected_revision_id != draft.expected_head_revision_id:
                raise ValueError(
                    "Adopt this exact revision locally before binding or confirming it."
                )
            fingerprint = _required_scope(case_id, scope or {})
            if action == "bind":
                saved = planner.bind(
                    draft.brief_id,
                    case_id,
                    expected_head_revision_id=draft.expected_head_revision_id,
                    expected_case_fingerprint=fingerprint,
                    author="Local analyst",
                )
                message = (
                    "Evidence attached to the saved plan. "
                    "Next: check the scope, then Confirm scope."
                )
            else:
                planner.confirm(
                    draft.brief_id,
                    expected_head_revision_id=draft.expected_head_revision_id,
                    expected_case_fingerprint=fingerprint,
                    reviewer="Local analyst",
                )
                _remember_state(draft, planner.load(draft.brief_id, draft.selected_revision_id))
                return (
                    store,
                    "Scope confirmed. Next: inspect the findings and exact evidence, "
                    "then save your briefing decisions.",
                    False,
                )
        else:
            raise ValueError("This planning action is unavailable.")
        old_key = store.active
        loaded = planner.load(saved.brief_id, saved.revision_id)
        # A successful save consumes only its submitted working copy. Historical
        # revisions and other contexts remain in durable storage/browser recovery.
        if old_key:
            store.drafts.pop(old_key, None)
        opened = store.open_state(case_id, loaded)
        opened.evidence_requested = draft.evidence_requested
        store.remember_plan(case_id, loaded)
        return store, message, False
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        if draft.brief_id and draft.selected_revision_id:
            try:
                _remember_state(draft, planner.load(draft.brief_id, draft.selected_revision_id))
            except (ValueError, KeyError, OSError, sqlite3.Error):
                pass
        return (
            store,
            str(exc) + " Your unsaved fields are kept. Open a saved revision separately, "
            "or copy your fields into a new unbound draft.",
            True,
        )


def selected_revision_context(draft: EditorDraft) -> dict[str, Any] | None:
    """Expose case-scoped unsaved edits without asserting a saved planning revision."""
    if not draft.brief_id and not draft.dirty:
        return None
    return {
        "case_id": draft.case_id,
        "brief_id": draft.brief_id,
        "revision_id": draft.selected_revision_id,
        "base_revision_id": draft.base_revision_id,
        "expected_head_revision_id": draft.expected_head_revision_id,
        "head_revision_id": draft.current_head_revision_id,
        "status": draft.status,
        "dirty": draft.dirty,
        "is_active_for_case": draft.is_active_for_case,
        "imported": draft.imported,
        "bound_case_id": draft.binding_case_id,
        "scope_fingerprint": draft.scope_fingerprint,
        "content_hash": draft.content_hash,
        "confirmation_id": draft.confirmation_id,
    }
