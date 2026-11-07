"""Both model passes see the same bounded saved evidence and source context."""

import hashlib
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from evidence_explorer.claim_check import assessor, prompt_context
from evidence_explorer.claim_check.challenge import challenge_saved_passages
from evidence_explorer.claim_check.contracts import (
    EvidenceFragment,
    EvidenceSnapshot,
    SavedEvidence,
)
from evidence_explorer.config import Settings

CLAIM = "The observatory reportedly found a candidate planet."


@pytest.fixture
def saved_snapshot():
    records = []
    for key, text, title in (
        ("original", "A candidate was reported. The interpretation remains uncertain.", "Report"),
        ("reply", "The authors dispute the criticism and request more observations.", "Reply"),
    ):
        records.append(
            SavedEvidence(
                id=key,
                url=f"https://example.invalid/{key}",
                requested_url=f"https://example.invalid/{key}",
                title=title,
                publisher="Example Observatory",
                text=text,
                published_at=None,
                retrieved_at="2026-11-07T00:00:00+00:00",
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                metadata={"discovery_only": {"snippet": "Search text is not saved evidence."}},
            )
        )
    fragments = []
    for fragment_id, record, start, end in (
        ("original-later", records[0], 26, len(records[0].text)),
        ("reply-full", records[1], 0, len(records[1].text)),
        ("original-first", records[0], 0, 25),
    ):
        fragments.append(
            EvidenceFragment(
                id=fragment_id,
                record_id=record.id,
                text=record.text[start:end],
                start=start,
                end=end,
            )
        )
    return EvidenceSnapshot(
        records=records, fragments=fragments, created_at="2026-11-07T00:00:00+00:00"
    )


def test_payload_preserves_alias_order_exact_claim_spans_and_saved_source_identity(saved_snapshot):
    before = saved_snapshot.model_dump_json()
    aliases, data = prompt_context.saved_passage_context(CLAIM, saved_snapshot)
    payload = json.loads(data)

    assert payload["claim"] == CLAIM
    assert list(aliases) == ["P1", "P2", "P3"]
    assert list(aliases.values()) == saved_snapshot.fragments
    assert [passage["source_id"] for passage in payload["passages"]] == ["S1", "S2", "S1"]
    for passage, fragment in zip(payload["passages"], saved_snapshot.fragments, strict=True):
        assert (passage["text"], passage["start"], passage["end"]) == (
            fragment.text,
            fragment.start,
            fragment.end,
        )
    for source, record in zip(payload["sources"], saved_snapshot.records, strict=True):
        assert source == {
            "source_id": f"S{saved_snapshot.records.index(record) + 1}",
            "title": record.title,
            "url": record.url,
            "publisher": record.publisher,
            "published_at": None,
            "retrieved_at": record.retrieved_at,
            "saved_text_chars": len(record.text),
        }
    assert "Search text is not saved evidence" not in data
    assert "independent" not in data
    assert saved_snapshot.model_dump_json() == before


def test_assessment_and_challenge_share_payload_and_resolve_exact_citations(
    saved_snapshot, monkeypatch
):
    messages_seen = []

    class Client:
        last_generation_metadata = {"actual_model": "fixture-model"}

        def __init__(self, settings):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def model_key(self, kind):
            return "fixture-model@digest"

        def generate_structured(self, messages, schema, *, max_tokens):
            messages_seen.append(messages)
            assert "untrusted data" in messages[0]["content"]
            assert prompt_context.CLAIM_AND_SOURCE_RULES in messages[0]["content"]
            if "findings" in schema.model_fields:
                assert max_tokens == 1400
                return schema(
                    findings=[
                        {
                            "finding_type": "insufficient_evidence",
                            "explanation": "The supplied reply leaves the interpretation open.",
                            "fragment_ids": ["P2", "P1"],
                        }
                    ]
                )
            assert max_tokens == 1600
            return schema(
                status="insufficient_evidence",
                explanation="The reported candidate remains qualified.",
                citations=[{"fragment_id": "P3", "stance": "context"}],
                unresolved_questions=["Will further observations resolve the interpretation?"],
            )

    before = saved_snapshot.fingerprint()
    monkeypatch.setattr(assessor, "OllamaClient", Client)
    result = assessor.OllamaClaimAssessor(Settings()).assess(CLAIM, saved_snapshot)

    assert len(messages_seen) == 2
    assert messages_seen[0][1] == messages_seen[1][1]
    assert result.prompt_version == "external-claim-assessment-v4"
    assert result.metadata["challenge"]["prompt_version"] == "external-claim-challenge-v3"
    assert result.citations[0].fragment_id == saved_snapshot.fragments[2].id
    assert result.citations[0].quote == saved_snapshot.fragments[2].text
    objection = result.metadata["challenge"]["findings"][0]
    assert objection["fragment_ids"] == [saved_snapshot.fragments[i].id for i in (1, 0)]
    assert objection["quotes"] == [saved_snapshot.fragments[i].text for i in (1, 0)]
    assert result.metadata["semantic_validation"] == "analyst_review_required"
    assert result.metadata["evidence_fingerprint"] == saved_snapshot.fingerprint() == before


def test_input_limit_counts_utf8_bytes_and_accepts_exact_boundary(saved_snapshot, monkeypatch):
    claim = "候補惑星という報告"
    _, data = prompt_context.saved_passage_context(claim, saved_snapshot)
    byte_count = len(data.encode("utf-8"))
    assert byte_count > len(data)
    monkeypatch.setattr(prompt_context, "MAX_PROMPT_BYTES", byte_count)
    assert prompt_context.saved_passage_context(claim, saved_snapshot)[1] == data
    monkeypatch.setattr(prompt_context, "MAX_PROMPT_BYTES", byte_count - 1)
    with pytest.raises(ValueError, match="bounded input size"):
        prompt_context.saved_passage_context(claim, saved_snapshot)


@pytest.mark.parametrize("pass_name", ["assessment", "challenge"])
def test_oversized_source_metadata_fails_before_model_access(
    saved_snapshot, monkeypatch, pass_name
):
    record = saved_snapshot.records[0].model_copy(update={"title": "ä" * 14_000})
    oversized = EvidenceSnapshot.model_validate(
        {**saved_snapshot.model_dump(), "records": [record, saved_snapshot.records[1]]}
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Oversized input must be rejected before model access")

    monkeypatch.setattr(assessor, "OllamaClient", forbidden)
    with pytest.raises(ValueError, match="bounded input size"):
        if pass_name == "assessment":
            assessor.OllamaClaimAssessor(Settings()).assess(CLAIM, oversized)
        else:
            challenge_saved_passages(
                SimpleNamespace(generate_structured=forbidden), CLAIM, oversized
            )


@pytest.mark.parametrize(
    ("status", "stance"),
    [
        ("supported", "supports"),
        ("contradicted", "contradicts"),
        ("misleading_context", "context"),
        ("insufficient_evidence", "context"),
        ("not_factual", "context"),
    ],
)
def test_status_guidance_reaches_model_without_forcing_a_verdict(
    saved_snapshot, monkeypatch, status, stance
):
    def generate(messages, schema, *, max_tokens):
        if "findings" in schema.model_fields:
            return schema(findings=[])
        assert schema.model_fields["status"].description == assessor.STATUS_GUIDANCE
        assert assessor.STATUS_GUIDANCE in messages[0]["content"]
        return schema(
            status=status,
            explanation="An explicitly supplied model proposal for this contract test.",
            citations=[{"fragment_id": "P1", "stance": stance}],
            unresolved_questions=[],
        )

    client = SimpleNamespace(
        last_generation_metadata={},
        model_key=lambda kind: "fixture-model@digest",
        generate_structured=generate,
    )
    monkeypatch.setattr(assessor, "OllamaClient", lambda settings: nullcontext(client))

    result = assessor.OllamaClaimAssessor(Settings()).assess(CLAIM, saved_snapshot)

    # Wording guidance does not silently convert the model's chosen taxonomy.
    # Real semantic quality remains a separate observed-model evaluation.
    assert result.status == status
