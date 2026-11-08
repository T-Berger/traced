"""Read-only availability for the merged, currently gated theory pipeline.

The standalone theory engine does not yet export a validated EvidenceBundle.
This module intentionally does not import the engine, inspect old reports, run
models, or infer an account identity from names or source text.
"""

THEORY_REVIEWED_COMMIT = "355e55eed893346805461a07f49096b45b7789e4"


def theory_review_availability() -> dict:
    """Describe the evidence handoff currently available to this workspace.

    This status describes the merged implementation reviewed at the commit
    above. It is not a live source check, an analysis run, or a finding.
    """
    return {
        "method": "Swarm theory pipeline",
        "status": "unavailable",
        "reviewed_commit": THEORY_REVIEWED_COMMIT,
        "reason": (
            "Account analysis and embeddings are gated because the supplied export lacks "
            "a supported verified source-native sender identity contract. The standalone "
            "pipeline also has no validated evidence-bundle handoff to this review."
        ),
        "next_steps": (
            "Establish and review source-scoped identities and complete input coverage; "
            "then design a versioned evidence handoff with exact source references."
        ),
        "limitations": [
            "Previously generated name-based account results are not validated evidence.",
            "Timing, similarity and model outputs do not establish automation or intent.",
            "This status does not determine a category, violation, or platform action.",
            "The existing Swarm detector is a separate method with its own evidence and run.",
        ],
    }
