"""Article policy works without an Ollama transport and keeps its client facade."""

import json

import httpx
import pytest

from evidence_explorer.article_extraction import ArticleClaimExtractor, ClaimExtractionError
from evidence_explorer.claim_selection import ClaimSelectionBatch
from evidence_explorer.models import ClaimInput
from evidence_explorer.models_client import ModelError, OllamaClient

QUOTE = "The office said: ‘The bridge remains closed.’ Review resumes on Monday."
ARTICLE = {"title": "Bridge inspection update", "text": QUOTE}
CHUNK = {"id": 7, "text": QUOTE, "article_id": 1, "start_char": 0, "end_char": len(QUOTE)}


def batch(candidate_id):
    return ClaimSelectionBatch.model_validate({"claims": [{"candidate_id": candidate_id}]})


def first_selection(messages, schema, *, max_tokens):
    assert schema is ClaimSelectionBatch
    assert max_tokens == 1600
    supplied = json.loads(messages[1]["content"])
    return batch(supplied["candidates"][0]["candidate_id"])


def no_http(request):
    pytest.fail(f"The injected generator must not make an HTTP request: {request.url}")


def test_article_policy_accepts_a_generator_without_model_settings_or_storage():
    extractor = ArticleClaimExtractor(first_selection)

    assert extractor.extract_claims(ARTICLE, [CHUNK]) == [
        ClaimInput(claim_text=QUOTE, quote=QUOTE, chunk_id=7)
    ]
    assert extractor.last_metadata == {"attempts": 1, "rejected_claims": 0}


def test_article_policy_limits_the_supplied_context_to_eight_chunks():
    statements = [f"The office confirmed that route {index} remains closed." for index in range(9)]
    article = {"title": "Route closures", "text": "\n".join(statements)}
    chunks = []
    offset = 0
    for index, text in enumerate(statements, start=1):
        chunks.append(
            {"id": index, "text": text, "start_char": offset, "end_char": offset + len(text)}
        )
        offset += len(text) + 1

    def generate(messages, schema, *, max_tokens):
        candidates = json.loads(messages[1]["content"])["candidates"]
        assert {candidate["chunk_id"] for candidate in candidates} == set(range(1, 9))
        assert statements[8] not in json.dumps(candidates)
        return first_selection(messages, schema, max_tokens=max_tokens)

    assert ArticleClaimExtractor(generate).extract_claims(article, chunks)[0].quote == statements[0]


def test_domain_failure_stops_after_one_correction_and_metadata_resets_on_reuse():
    calls = []

    def generate(messages, schema, *, max_tokens):
        calls.append(messages)
        return batch("unknown")

    extractor = ArticleClaimExtractor(generate)
    with pytest.raises(ClaimExtractionError, match="source-selection checks.*no claims saved"):
        extractor.extract_claims(ARTICLE, [CHUNK])
    assert len(calls) == 2
    assert calls[1][:2] == calls[0]
    assert extractor.last_metadata == {"attempts": 2, "rejected_claims": 1}

    text = "Interviewer: Did the bridge reopen yesterday?"
    chunks = [{"id": 1, "text": text, "start_char": 0, "end_char": len(text)}]
    assert extractor.extract_claims({"title": "Question", "text": text}, chunks) == []
    assert len(calls) == 2
    assert extractor.last_metadata == {"attempts": 0, "rejected_claims": 0}


def test_client_facade_uses_subclass_generator_and_exposes_live_attempt_metadata(settings):
    class InjectedClient(OllamaClient):
        def generate_structured(self, messages, schema, *, max_tokens):
            assert self.last_claim_extraction_metadata == {"attempts": 1, "rejected_claims": 0}
            return first_selection(messages, schema, max_tokens=max_tokens)

    with InjectedClient(settings, transport=httpx.MockTransport(no_http)) as client:
        assert client.extract_claims(ARTICLE, [CHUNK])[0].quote == QUOTE


def test_client_facade_resolves_monkeypatched_generator_each_call(settings, monkeypatch):
    with OllamaClient(settings, transport=httpx.MockTransport(no_http)) as client:
        monkeypatch.setattr(client, "generate_structured", first_selection)
        assert client.extract_claims(ARTICLE, [CHUNK])[0].quote == QUOTE

        failure = ModelError("Injected transport failure")
        calls = []

        def unavailable(messages, schema, *, max_tokens):
            calls.append(messages)
            raise failure

        monkeypatch.setattr(client, "generate_structured", unavailable)
        with pytest.raises(ModelError) as raised:
            client.extract_claims(ARTICLE, [CHUNK])
        assert raised.value is failure
        assert len(calls) == 1
        assert client.last_claim_extraction_metadata == {"attempts": 1, "rejected_claims": 0}


def test_client_facade_keeps_the_existing_model_error_after_failed_selection(settings, monkeypatch):
    with OllamaClient(settings, transport=httpx.MockTransport(no_http)) as client:
        monkeypatch.setattr(client, "generate_structured", lambda *args, **kwargs: batch("unknown"))
        with pytest.raises(ModelError, match="source-selection checks.*no claims saved"):
            client.extract_claims(ARTICLE, [CHUNK])
        assert client.last_claim_extraction_metadata == {"attempts": 2, "rejected_claims": 1}
