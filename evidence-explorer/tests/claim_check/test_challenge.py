"""The second pass can select evidence but cannot manufacture quotation text."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from evidence_explorer.claim_check.assessor import select_fragments
from evidence_explorer.claim_check.challenge import challenge_saved_passages
from evidence_explorer.claim_check.contracts import EvidenceSnapshot, SavedEvidence


def sample():
    import hashlib

    text = "This is a candidate planet. Confirmation requires further observations."
    record = SavedEvidence(
        id="record-a",
        url="https://example.org/science",
        requested_url="https://example.org/science",
        title="Observatory record",
        publisher="Observatory",
        text=text,
        retrieved_at=datetime.now(UTC).isoformat(),
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    return EvidenceSnapshot(
        records=[record],
        fragments=select_fragments("confirmed planet", [record]),
        created_at=datetime.now(UTC).isoformat(),
    )


@pytest.mark.parametrize("ids", [["P1"], ["P999"], ["P1", "P1"]])
def test_objections_resolve_only_unique_supplied_ids_to_exact_source_text(ids):
    snapshot = sample()

    def generate(messages, schema, *, max_tokens):
        assert "untrusted data" in messages[0]["content"]
        return schema(
            findings=[
                dict(
                    finding_type="unsupported_inference",
                    explanation="Confirmation is not established.",
                    fragment_ids=ids,
                )
            ]
        )

    client = SimpleNamespace(generate_structured=generate)
    if ids != ["P1"]:
        with pytest.raises(ValueError):
            challenge_saved_passages(client, "The planet is confirmed.", snapshot)
    else:
        result = challenge_saved_passages(client, "The planet is confirmed.", snapshot)
        assert result["findings"][0]["fragment_ids"] == [snapshot.fragments[0].id]
        assert result["findings"][0]["quotes"] == [snapshot.records[0].text]
