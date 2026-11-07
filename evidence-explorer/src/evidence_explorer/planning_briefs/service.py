"""Explicit planning commands and read-only state, without scheduling or case creation."""

from __future__ import annotations

from .models import (
    BriefBinding,
    BriefConfirmation,
    BriefRevision,
    BriefState,
    DraftFields,
    PlanningHistory,
)
from .persistence import PlanningBriefRepository, StoredState


def confirmation_issues(revision: BriefRevision) -> tuple[str, ...]:
    # Keep shared contract imports local so progress may use the pure scope helper.
    from ..use_cases.catalog import get_use_case
    from ..use_cases.contracts import MonitoringBrief

    fields = revision.fields
    issues = []
    if revision.binding is None:
        issues.append("Bind the draft to a saved case.")
    elif set(fields.source_selection) != set(revision.binding.scope.source_refs):
        issues.append("The source selection must equal the saved case scope.")
    if not fields.use_case_id:
        issues.append("Choose the purpose of this brief.")
    else:
        try:
            definition = get_use_case(fields.use_case_id)
        except (KeyError, ValueError):
            issues.append("Choose a supported brief purpose.")
        else:
            if definition.requires_activity_baseline and not all(
                window and window.start is not None and window.end is not None
                for window in (fields.baseline_window, fields.observation_window)
            ):
                issues.append("Supply complete baseline and observation windows.")
    if (
        fields.alert_rule
        and fields.alert_rule.comparison_basis == "equal_duration_counts"
        and not all(
            window and window.start is not None and window.end is not None
            for window in (fields.baseline_window, fields.observation_window)
        )
    ):
        issues.append("An equal-count alert rule needs two complete comparison windows.")
    try:
        MonitoringBrief.model_validate(
            {"id": revision.brief_id, **fields.model_dump(mode="json"), "confirmed": False}
        )
    except ValueError:
        issues.append("Supply purpose, public topics, source selection, reviewer role, and output.")
    return tuple(issues)


def _require_complete(revision: BriefRevision) -> None:
    issues = confirmation_issues(revision)
    if issues:
        raise ValueError(" ".join(issues))


def _state(stored: StoredState) -> BriefState:
    revision = stored.revision
    issues = list(confirmation_issues(revision))
    is_head = stored.head_revision_id == revision.revision_id
    if not stored.local:
        issues.append("Imported history requires explicit local adoption and confirmation.")
    if not is_head:
        issues.append("This is a historical revision, not the current local draft.")
    if stored.binding_problem:
        issues.append(stored.binding_problem)
    if revision.binding and not stored.is_active_for_case:
        issues.append("This brief is not the active local plan for its case.")
    if not stored.confirmation_is_local:
        issues.append("This revision has no local confirmation.")
    confirmed = (
        is_head
        and stored.local
        and stored.confirmation_is_local
        and stored.binding_current
        and stored.is_active_for_case
        and not confirmation_issues(revision)
    )
    if confirmed:
        status = "confirmed"
        issues = []
    elif revision.binding is not None:
        status = "needs_review"
    elif revision.number == 1 and revision.fields == DraftFields():
        status = "draft"
    else:
        status = "unbound"
    return BriefState(
        revision=revision,
        confirmation=stored.confirmation,
        status=status,
        reasons=tuple(issues),
        head_revision_id=stored.head_revision_id,
        is_head=is_head,
        imported=not stored.local,
        confirmation_is_local=stored.confirmation_is_local,
        is_active_for_case=stored.is_active_for_case,
    )


class PlanningBriefService:
    def __init__(self, repository: PlanningBriefRepository):
        self.repository = repository

    def create_draft(
        self, *, author: str, fields: DraftFields | None = None, brief_id: str | None = None
    ) -> BriefRevision:
        return self.repository.create(fields or DraftFields(), author=author, brief_id=brief_id)

    def save_draft(
        self,
        brief_id: str,
        fields: DraftFields,
        *,
        base_revision_id: str,
        expected_head_revision_id: str,
        author: str,
    ) -> BriefRevision:
        return self.repository.save(
            brief_id,
            fields,
            base_revision_id=base_revision_id,
            expected_head_revision_id=expected_head_revision_id,
            author=author,
        )

    def load(self, brief_id: str, revision_id: str | None = None) -> BriefState:
        return _state(self.repository.load(brief_id, revision_id))

    def list(self, *, limit: int = 50, offset: int = 0) -> tuple[BriefState, ...]:
        return tuple(_state(row) for row in self.repository.list(limit=limit, offset=offset))

    def list_for_case(
        self, case_id: str | None, *, limit: int = 50, offset: int = 0
    ) -> tuple[BriefState, ...]:
        return tuple(
            _state(row)
            for row in self.repository.list_for_case(case_id, limit=limit, offset=offset)
        )

    def history(self, brief_id: str, *, limit: int = 100, offset: int = 0) -> PlanningHistory:
        return self.repository.history(brief_id, limit=limit, offset=offset)

    def bind(
        self,
        brief_id: str,
        case_id: str,
        *,
        expected_head_revision_id: str,
        expected_case_fingerprint: str,
        author: str,
    ) -> BriefRevision:
        return self.repository.bind(
            brief_id,
            case_id,
            expected_head_revision_id=expected_head_revision_id,
            expected_case_fingerprint=expected_case_fingerprint,
            author=author,
        )

    def confirm(
        self,
        brief_id: str,
        *,
        expected_head_revision_id: str,
        expected_case_fingerprint: str,
        reviewer: str,
    ) -> BriefConfirmation:
        return self.repository.confirm(
            brief_id,
            expected_head_revision_id=expected_head_revision_id,
            expected_case_fingerprint=expected_case_fingerprint,
            reviewer=reviewer,
            validate=_require_complete,
        )

    def case_binding(self, case_id: str) -> BriefBinding:
        return self.repository.case_binding(case_id)

    def state_for_case(self, case_id: str) -> BriefState | None:
        stored = self.repository.state_for_case(case_id)
        return _state(stored) if stored else None

    def history_for_case(
        self, case_id: str, *, limit: int = 1000, offset: int = 0
    ) -> PlanningHistory:
        return self.repository.history_for_case(case_id, limit=limit, offset=offset)

    def context_token_for_case(self, case_id: str) -> str:
        return self.repository.context_token_for_case(case_id)

    def import_history(self, history: PlanningHistory) -> None:
        self.repository.import_history(history)

    def adopt_revision(
        self,
        revision_id: str,
        case_id: str,
        *,
        expected_head_revision_id: str | None,
        expected_case_fingerprint: str,
        author: str,
    ) -> BriefRevision:
        return self.repository.adopt(
            revision_id,
            case_id,
            expected_head_revision_id=expected_head_revision_id,
            expected_case_fingerprint=expected_case_fingerprint,
            author=author,
        )
