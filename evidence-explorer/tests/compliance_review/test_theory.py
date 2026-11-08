from evidence_explorer.compliance_review.theory import (
    THEORY_REVIEWED_COMMIT,
    theory_review_availability,
)


def test_theory_handoff_is_explicitly_unavailable():
    status = theory_review_availability()
    assert status["status"] == "unavailable"
    assert status["reviewed_commit"] == THEORY_REVIEWED_COMMIT
    assert "source-native sender identity" in status["reason"]
    assert "no validated evidence-bundle handoff" in status["reason"]
    assert "finding" not in status
