"""Server-side planning facts; browser selections and imported confirmations grant no progress."""

from __future__ import annotations

from ..use_cases.contracts import (
    BriefBindingFact,
    BriefScopeContext,
    MonitoringBrief,
    ProgressFacts,
)
from .models import BriefState, ScopeIdentity, digest


def scope_context(scope: ScopeIdentity) -> BriefScopeContext:
    """Keep the complete saved identity without copying evidence bodies into UI metadata."""
    return BriefScopeContext(
        policy=scope.policy,
        case_id=scope.case_id,
        evidence_fingerprint=scope.evidence_fingerprint,
        scope_fingerprint=scope.scope_fingerprint,
        source_refs=scope.source_refs,
    )


def confirmed_progress_facts(
    state: BriefState | None, context: BriefScopeContext | None
) -> ProgressFacts:
    """Project only the active, locally confirmed revision onto its exact saved scope."""
    if state is None or context is None:
        return ProgressFacts()
    revision, confirmation = state.revision, state.confirmation
    binding = revision.binding
    if (
        state.status != "confirmed"
        or not state.is_head
        or not state.is_active_for_case
        or state.head_revision_id != revision.revision_id
        or state.imported
        or not state.confirmation_is_local
        or binding is None
        or confirmation is None
        or confirmation.brief_id != revision.brief_id
        or confirmation.revision_id != revision.revision_id
        or confirmation.binding_digest != digest(binding.model_dump(mode="json"))
        or scope_context(binding.scope) != context
    ):
        return ProgressFacts()
    try:
        plan = MonitoringBrief.model_validate(
            {"id": revision.brief_id, **revision.fields.model_dump(mode="json"), "confirmed": True}
        )
    except ValueError:
        return ProgressFacts()
    return ProgressFacts(
        brief=plan,
        brief_binding=BriefBindingFact(
            brief_id=revision.brief_id,
            revision_id=revision.revision_id,
            case_id=binding.case_id,
            policy=context.policy,
            plan_fingerprint=digest(plan.model_dump(mode="json")),
            evidence_fingerprint=context.evidence_fingerprint,
            scope_fingerprint=context.scope_fingerprint,
        ),
    )
