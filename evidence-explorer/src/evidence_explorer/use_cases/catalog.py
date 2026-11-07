"""A small purpose catalog for the same evidence and human-review workflow."""

from .contracts import UseCaseDefinition

_CATALOG = (
    UseCaseDefinition(
        id="public_information_monitoring",
        title="Public-interest information monitoring",
        target_roles=("Communications lead", "CISO", "OSINT analyst"),
        problem="Public claims and changing discussion are scattered across sources and time.",
        user_story=(
            "As a responsible analyst, I want to monitor a declared public topic and compare "
            "observed activity with a documented baseline so I can choose what needs review."
        ),
        outcome="A source-linked monitoring brief with reviewed signals and coverage limits.",
        scope_notes=(
            "Define the public topic, sources, languages, time windows, and reviewer first.",
            "Topic or narrative labels must be supplied or reviewed; the rule infers no labels.",
        ),
        requires_activity_baseline=True,
    ),
    UseCaseDefinition(
        id="election_information_integrity",
        title="Election-related information integrity",
        target_roles=("Election information analyst", "Fact-checker", "Research editor"),
        problem="Repeated public election claims can obscure their origins and factual support.",
        user_story=(
            "As an information-integrity reviewer, I want to compare public election claims "
            "and their exact sources so I can publish a neutral, qualified account of the evidence."
        ),
        outcome="A reviewed evidence timeline and briefing about public factual claims.",
        scope_notes=(
            "Use public-interest topics and public statements, including public-role statements.",
            "Review factual assertions and source provenance; do not profile private beliefs.",
            "This process does not select voters, persuasive messages, or political targeting.",
        ),
        requires_activity_baseline=True,
    ),
    UseCaseDefinition(
        id="behavioural_coordination_review",
        title="Behavioural coordination review",
        target_roles=("Disinformation analyst", "OSINT investigator", "Research reviewer"),
        problem="Activity bursts can reflect coordination, a shared event, or active individuals.",
        user_story=(
            "As an investigator, I want to inspect repeated joint behaviour, surrounding activity, "
            "and counterevidence so I can describe the observed pattern without guessing intent."
        ),
        outcome="A qualified account of behaviour with complete-context sensitivity checks.",
        scope_notes=(
            "Reuse the Swarm method and its complete declared background context.",
            "Coordination establishes no bots, hostile intent, foreign control, or operator.",
            "Genuine criticism and openly organized participation remain legitimate alternatives.",
        ),
    ),
    UseCaseDefinition(
        id="cited_claim_investigation",
        title="Cited claim investigation",
        target_roles=("Fact-checker", "Newsroom editor", "OSINT analyst"),
        problem="A confident statement may rely on weak, repeated, or irrelevant citations.",
        user_story=(
            "As a reviewer, I want to inspect one factual claim against saved passages and "
            "alternative accounts so I can decide which wording the evidence supports."
        ),
        outcome="A cited proposed assessment followed by a separate human briefing decision.",
        scope_notes=(
            "Retain search attempts, exact quotations, source versions, and missing evidence.",
            "A check result is a model proposal; exact quotations do not establish factual truth.",
            "Unknown dates, identities, and source independence remain explicit.",
        ),
    ),
    UseCaseDefinition(
        id="platform_evidence_dossier",
        title="Human-reviewed platform evidence dossier",
        target_roles=("Trust and safety reviewer", "Communications lead", "CISO", "OSINT analyst"),
        problem="Escalation needs reproducible evidence, source context, and review history.",
        user_story=(
            "As a responsible reviewer, I want to assemble saved evidence, context, and review "
            "decisions so a human can judge whether and how to raise a platform concern."
        ),
        outcome="A portable evidence dossier with policy references and unresolved questions.",
        scope_notes=(
            "Policy links are proposed references; applicability and reporting routes need review.",
            "The process prepares evidence without submitting reports or deciding legal liability.",
            "Retain source identifiers, timestamps, content hashes, and a human review trail.",
        ),
    ),
)


def list_use_cases() -> tuple[UseCaseDefinition, ...]:
    return _CATALOG


def get_use_case(use_case_id: str) -> UseCaseDefinition:
    for use_case in _CATALOG:
        if use_case.id == use_case_id:
            return use_case
    raise ValueError(f"Unknown use case: {use_case_id}")
