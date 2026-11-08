import hashlib

import pytest

from evidence_explorer.compliance_review.matrix import build_monthly_matrix
from evidence_explorer.compliance_review.proposals import (
    propose_for_record,
    propose_with_local_model,
)
from evidence_explorer.config import Settings


class FakeModel:
    last_generation_metadata = {"requested_temperature": 0}

    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = 0

    def model_key(self, kind):
        assert kind == "chat"
        return "ollama:test@" + "a" * 64 + ":v1"

    def generate_structured(self, messages, schema, *, max_tokens):
        self.calls += 1
        assert "untrusted" in messages[0]["content"]
        assert max_tokens == 1600
        return schema.model_validate(
            {"candidates": self.candidates, "uncertainty": "Review context."}
        )


def record(text):
    return {
        "id": "saved:1",
        "kind": "post",
        "text": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
    }


def candidate(category, quote):
    return {"category": category, "quote": quote, "rationale": "Requires human review."}


def test_proposals_require_exact_unique_quotes_and_remain_unreviewed():
    saved = record("One fictional statement. Repeated. Repeated.")
    client = FakeModel(
        [
            candidate("harassment", "fictional statement"),
            candidate("hate_speech", "invented"),
            candidate("racism", "Repeated."),
        ]
    )
    result = propose_for_record(saved, "Fictional subject", client)
    assert len(result["annotations"]) == 1
    proposal = result["annotations"][0]
    assert proposal["status"] == "proposed" and proposal["origin"] == "model"
    assert saved["text"][proposal["start"] : proposal["end"]] == proposal["quote"]
    assert proposal["record_content_hash"] == saved["content_hash"]
    assert proposal["run_id"] == result["run_id"]
    assert len(result["rejected_candidates"]) == 2
    assert result["model"] == "ollama:test@" + "a" * 64 + ":v1"


def test_stale_record_rejected_before_any_generation():
    saved = record("Fictional record")
    saved["text"] = "Changed text"
    client = FakeModel([])
    with pytest.raises(ValueError, match="content hash"):
        propose_for_record(saved, "Fictional subject", client)
    assert client.calls == 0


def test_model_output_enters_matrix_as_proposal_only():
    saved = record("A fictional statement for examination.")
    saved["published_at"] = "2026-01-04T10:00:00Z"
    result = propose_for_record(
        saved, "Fictional subject", FakeModel([candidate("racism", "fictional statement")])
    )
    matrix = build_monthly_matrix([saved], result["annotations"], {"2026-01": "complete"})
    counts = matrix["months"][0]["categories"]["racism"]
    assert matrix["months"][0]["coverage"] == "unknown"
    assert counts["proposed"] is None
    assert counts["confirmed"] is None
    assert counts["observed_proposed"] == 1
    assert counts["observed_confirmed"] == 0
    assert matrix["invalid_annotations"] == []


def test_no_candidates_is_unresolved_and_remote_ollama_is_rejected():
    result = propose_for_record(
        record("An ordinary fictional statement."), "Subject", FakeModel([])
    )
    assert result["annotations"] == []
    assert result["uncertainty"] == "Review context."
    with pytest.raises(ValueError, match="loopback"):
        propose_with_local_model(
            record("Text"), "Subject", Settings(ollama_url="https://example.com")
        )
