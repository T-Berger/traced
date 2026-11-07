"""Bounded source selection cannot rewrite the meaning of a genuine quotation."""

import json

import httpx
import pytest

from evidence_explorer.analysis import validate_claims as legacy_validate_claims
from evidence_explorer.claim_excerpts import build_claim_excerpts
from evidence_explorer.claim_selection import CLAIMS_POLICY_VERSION
from evidence_explorer.grounding import validate_claims, validate_extractive_claims
from evidence_explorer.models import ClaimInput
from evidence_explorer.models_client import ModelError, OllamaClient

QUOTE = "The office said: ‘The bridge remains closed.’ Review resumes on Monday."
ARTICLE = {"title": "Bridge inspection update", "text": QUOTE}
CHUNK = {"id": 7, "text": QUOTE, "article_id": 1, "start_char": 0, "end_char": len(QUOTE)}


def selection(candidate_id=None):
    if candidate_id is None:
        candidate_id = build_claim_excerpts(ARTICLE, [CHUNK])[0].candidate_id
    return {"candidate_id": candidate_id}


def response(*claims):
    return httpx.Response(200, json={"message": {"content": json.dumps({"claims": list(claims)})}})


def transport_for(settings, results, requests, *, missing=False):
    def respond(request):
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": []
                    if missing
                    else [
                        {"name": settings.chat_model, "digest": "revision"},
                        {"name": settings.embedding_model, "digest": "embedding-revision"},
                    ]
                },
            )
        assert request.url.path == "/api/chat"
        requests.append(json.loads(request.content))
        result = results[len(requests) - 1]
        if isinstance(result, Exception):
            raise result
        return result

    return httpx.MockTransport(respond)


def test_valid_selection_copies_source_and_versions_claims_without_invalidating_embeddings(
    settings,
):
    requests = []
    with OllamaClient(
        settings, transport=transport_for(settings, [response(selection())], requests)
    ) as client:
        result = client.extract_claims(ARTICLE, [CHUNK])
        assert client.model_key("claims").endswith(f"@revision:{CLAIMS_POLICY_VERSION}")
        assert client.model_key("chat").endswith("@revision:v1")
        assert client.model_key("embedding").endswith("@embedding-revision:v1")
        assert client.last_claim_extraction_metadata == {"attempts": 1, "rejected_claims": 0}
    assert result == [ClaimInput(claim_text=QUOTE, quote=QUOTE, chunk_id=7)]
    assert len(requests) == 1
    assert len(requests[0]["messages"]) == 2
    assert legacy_validate_claims is validate_claims
    assert requests[0]["format"]["$defs"]["ExcerptSelection"]["additionalProperties"] is False


def test_unknown_id_gets_one_correction_with_original_context(settings):
    requests = []
    invalid = selection("invented")
    with OllamaClient(
        settings,
        transport=transport_for(settings, [response(invalid), response(selection())], requests),
    ) as client:
        result = client.extract_claims(ARTICLE, [CHUNK])
        assert client.last_claim_extraction_metadata == {"attempts": 2, "rejected_claims": 0}
    assert result[0].claim_text == result[0].quote == QUOTE
    assert len(requests) == 2
    original, correction = (request["messages"] for request in requests)
    assert correction[:2] == original
    assert correction[2]["role"] == "assistant"
    assert json.loads(correction[2]["content"])["claims"] == [invalid]
    assert correction[3]["role"] == "user"
    assert "Unknown source excerpt" in correction[3]["content"]
    assert "Do not write claim text" in correction[3]["content"]


def test_second_unknown_selection_fails_without_a_third_generation(settings):
    requests = []
    invalid = response(selection("invented"))
    with OllamaClient(
        settings, transport=transport_for(settings, [invalid, invalid], requests)
    ) as client:
        with pytest.raises(ModelError, match="source-selection checks.*no claims saved"):
            client.extract_claims(ARTICLE, [CHUNK])
        assert client.last_claim_extraction_metadata == {"attempts": 2, "rejected_claims": 1}
    assert len(requests) == 2


def test_corrected_mixed_ids_keep_only_supplied_source_excerpts_and_deduplicate(settings):
    requests = []
    with OllamaClient(
        settings,
        transport=transport_for(
            settings,
            [
                response(selection("unknown")),
                response(selection(), selection(), selection("unknown")),
            ],
            requests,
        ),
    ) as client:
        result = client.extract_claims(ARTICLE, [CHUNK])
        assert client.last_claim_extraction_metadata == {"attempts": 2, "rejected_claims": 1}
    assert result == [ClaimInput(claim_text=QUOTE, quote=QUOTE, chunk_id=7)]
    assert len(requests) == 2


def test_empty_valid_selection_is_an_explicit_abstention(settings):
    requests = []
    with OllamaClient(
        settings, transport=transport_for(settings, [response()], requests)
    ) as client:
        assert client.extract_claims(ARTICLE, [CHUNK]) == []
        assert client.last_claim_extraction_metadata == {"attempts": 1, "rejected_claims": 0}
    assert len(requests) == 1


def test_empty_correction_after_invalid_id_fails_visibly(settings):
    requests = []
    with OllamaClient(
        settings,
        transport=transport_for(settings, [response(selection("unknown")), response()], requests),
    ) as client:
        with pytest.raises(ModelError, match="source-selection checks.*no claims saved"):
            client.extract_claims(ARTICLE, [CHUNK])
    assert len(requests) == 2


def test_no_safe_statement_does_not_call_a_model(settings):
    text = "Interviewer: Did the bridge reopen yesterday?"
    article = {"title": "An unanswered question", "text": text}
    chunks = [{"id": 1, "text": text, "start_char": 0, "end_char": len(text)}]
    requests = []
    with OllamaClient(settings, transport=transport_for(settings, [], requests)) as client:
        assert client.extract_claims(article, chunks) == []
        assert client.last_claim_extraction_metadata == {"attempts": 0, "rejected_claims": 0}
    assert requests == []


def test_inconsistent_source_offsets_fail_before_generation(settings):
    requests = []
    with OllamaClient(settings, transport=transport_for(settings, [], requests)) as client:
        with pytest.raises(ValueError, match="match the saved article"):
            client.extract_claims(ARTICLE, [CHUNK | {"start_char": 1}])
    assert requests == []


def test_metadata_resets_between_calls(settings):
    requests = []
    results = [
        response(selection("unknown")),
        response(selection(), selection("unknown")),
        response(selection()),
    ]
    with OllamaClient(settings, transport=transport_for(settings, results, requests)) as client:
        client.extract_claims(ARTICLE, [CHUNK])
        assert client.last_claim_extraction_metadata == {"attempts": 2, "rejected_claims": 1}
        client.extract_claims(ARTICLE, [CHUNK])
        assert client.last_claim_extraction_metadata == {"attempts": 1, "rejected_claims": 0}


@pytest.mark.parametrize(
    "extra",
    [
        {"claim_text": "Markus Söder ist langjähriger politischer Rivale von Ursula Münch."},
        {"subject": "Ursula Münch"},
        {"attribution": "The ministry"},
        {"quote": QUOTE.replace("‘", "'").replace("’", "'")},
    ],
)
def test_model_cannot_attach_a_paraphrase_or_inferred_person_to_a_valid_id(settings, extra):
    requests = []
    proposal = selection() | extra
    with OllamaClient(
        settings, transport=transport_for(settings, [response(proposal)], requests)
    ) as client:
        with pytest.raises(ModelError, match="invalid structured JSON"):
            client.extract_claims(ARTICLE, [CHUNK])
    assert len(requests) == 1


@pytest.mark.parametrize(
    "failure",
    [httpx.Response(503), httpx.ConnectError("Disconnected"), httpx.ReadTimeout("Timed out")],
)
def test_transport_failures_are_not_retried(settings, failure):
    requests = []
    with OllamaClient(settings, transport=transport_for(settings, [failure], requests)) as client:
        with pytest.raises(ModelError):
            client.extract_claims(ARTICLE, [CHUNK])
    assert len(requests) == 1


@pytest.mark.parametrize(
    "content", ["not JSON", "{}", '{"claims": [{}]}', '{"claims": [{"candidate_id": 7}]}']
)
def test_schema_failures_are_not_retried(settings, content):
    requests = []
    malformed = httpx.Response(200, json={"message": {"content": content}})
    with OllamaClient(settings, transport=transport_for(settings, [malformed], requests)) as client:
        with pytest.raises(ModelError, match="invalid structured JSON"):
            client.extract_claims(ARTICLE, [CHUNK])
    assert len(requests) == 1


def test_missing_model_does_not_attempt_generation(settings):
    requests = []
    with OllamaClient(
        settings, transport=transport_for(settings, [], requests, missing=True)
    ) as client:
        with pytest.raises(ModelError, match="missing"):
            client.extract_claims(ARTICLE, [CHUNK])
    assert requests == []


def test_failed_http_correction_does_not_trigger_further_attempts(settings):
    requests = []
    with OllamaClient(
        settings,
        transport=transport_for(
            settings, [response(selection("unknown")), httpx.Response(500)], requests
        ),
    ) as client:
        with pytest.raises(ModelError, match="HTTP 500"):
            client.extract_claims(ARTICLE, [CHUNK])
    assert len(requests) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"claim_text": "The bridge is open."},
        {"subject": "an invented person"},
        {"attribution": "an invented speaker"},
        {"event_time": "2026-11-07"},
        {"location": "Berlin"},
    ],
)
def test_exact_quote_does_not_allow_semantic_changes_even_on_a_duplicate(changes):
    faithful = ClaimInput(claim_text=QUOTE, quote=QUOTE, chunk_id=7)
    distorted = faithful.model_copy(update=changes)
    with pytest.raises(ValueError, match="preserve source wording"):
        validate_extractive_claims([faithful, distorted], [CHUNK])
