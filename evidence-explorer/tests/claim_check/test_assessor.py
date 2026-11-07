"""Assessment validity, local-only calls, and bounded passage selection."""

import hashlib
from datetime import UTC, datetime

import pytest

from evidence_explorer.claim_check.assessor import (
    MAX_PASSAGE_BYTES,
    OllamaClaimAssessor,
    select_fragments,
    validate_assessment,
)
from evidence_explorer.claim_check.contracts import (
    AssessmentCitation,
    EvidenceSnapshot,
    ProposedAssessment,
    SavedEvidence,
)
from evidence_explorer.config import Settings


def evidence(
    text="The observatory reported a candidate planet, not a confirmed detection.", key="a"
):
    return SavedEvidence(
        id=key,
        url=f"https://example.org/{key}",
        requested_url=f"https://example.org/{key}",
        title=f"Source {key}",
        publisher="Example",
        text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        retrieved_at=datetime.now(UTC).isoformat(),
    )


def snapshot(*records):
    return EvidenceSnapshot(
        records=list(records),
        fragments=select_fragments("confirmed planet", list(records)),
        created_at=datetime.now(UTC).isoformat(),
    )


def test_empty_evidence_abstains_without_connecting_to_model(tmp_path):
    result = OllamaClaimAssessor(
        Settings(data_dir=tmp_path, ollama_url="http://127.0.0.1:1")
    ).assess("The planet is confirmed.", snapshot())
    assert result.status == "insufficient_evidence"
    assert result.provider == "rules"
    assert result.citations == []


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.org",
        "http://user:pass@localhost:11434",
        "http://localhost:11434?token=x",
    ],
)
def test_local_assessment_rejects_remote_or_credentialed_endpoint(url):
    with pytest.raises(ValueError, match="loopback"):
        OllamaClaimAssessor(Settings(ollama_url=url))


@pytest.mark.parametrize(
    "citation",
    [
        AssessmentCitation(fragment_id="missing", quote="candidate planet", stance="contradicts"),
        AssessmentCitation(
            fragment_id="actual", quote="A confirmed detection.", stance="contradicts"
        ),
    ],
)
def test_hallucinated_citations_are_rejected(citation):
    saved = snapshot(evidence())
    if citation.fragment_id == "actual":
        citation = citation.model_copy(update={"fragment_id": saved.fragments[0].id})
    result = ProposedAssessment(
        status="contradicted", explanation="Not established", citations=[citation]
    )
    with pytest.raises(ValueError, match="citation"):
        validate_assessment(result, saved)


def test_substantive_verdict_requires_correct_evidence_stance():
    saved = snapshot(evidence())
    result = ProposedAssessment(
        status="supported",
        explanation="The original report says candidate.",
        citations=[
            AssessmentCitation(
                fragment_id=saved.fragments[0].id, quote="candidate planet", stance="contradicts"
            )
        ],
    )
    with pytest.raises(ValueError, match="supporting"):
        validate_assessment(result, saved)


def test_selection_preserves_exact_spans_and_does_not_amplify_copies():
    a = evidence(("The planet is still a candidate. " * 200), "a")
    copy = evidence(a.text, "copy")
    b = evidence("Independent observation describes the planet. " * 200, "b")
    saved = snapshot(a, copy, b)
    assert {f.record_id for f in saved.fragments} == {"a", "b"}
    assert sum(len(f.text.encode()) for f in saved.fragments) <= MAX_PASSAGE_BYTES
    assert len(saved.records) == 3
    assert saved.fingerprint() == saved.fingerprint()
    for fragment in saved.fragments:
        record = next(r for r in saved.records if r.id == fragment.record_id)
        assert record.text[fragment.start : fragment.end] == fragment.text


def test_model_receives_sources_as_untrusted_data_and_provenance_is_recorded(monkeypatch):
    from evidence_explorer.claim_check import assessor as module

    saved = snapshot(evidence())

    class Client:
        last_generation_metadata = {"actual_model": "fixture-model"}

        def __init__(self, settings):
            self.settings = settings

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def model_key(self, kind):
            return "fixture-model@digest"

        def generate_structured(self, messages, schema, *, max_tokens):
            assert "untrusted data" in messages[0]["content"]
            if "findings" in schema.model_fields:
                return schema(findings=[])
            assert "not guaranteed truth" in messages[0]["content"]
            return schema(
                status="contradicted",
                explanation="The source describes a candidate.",
                citations=[
                    dict(
                        fragment_id="P1",
                        stance="contradicts",
                    )
                ],
                unresolved_questions=["Has later confirmation been published?"],
            )

    monkeypatch.setattr(module, "OllamaClient", Client)
    result = OllamaClaimAssessor(Settings()).assess("Confirmed planet", saved)
    assert result.model == "fixture-model@digest"
    assert result.metadata["evidence_fingerprint"] == saved.fingerprint()
    assert result.metadata["semantic_validation"] == "analyst_review_required"
    assert result.citations[0].quote == saved.fragments[0].text
    assert result.metadata["challenge"]["status"] == "completed"
    assert result.metadata["challenge"]["findings"] == []
