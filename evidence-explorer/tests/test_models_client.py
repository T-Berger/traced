"""Exercise the actual local HTTP adapter without downloading model weights."""

import json

import httpx
import pytest

from evidence_explorer.analysis import validate_claims
from evidence_explorer.models import ClaimBatch, ClaimInput
from evidence_explorer.models_client import ModelError, OllamaClient


def tags(settings):
    return {
        "models": [
            {"name": settings.embedding_model, "digest": "embedding-revision"},
            {"name": settings.chat_model, "digest": "chat-revision"},
        ]
    }


def test_embedding_requests_are_batched_and_keyed_by_revision(settings):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=tags(settings))
        if request.url.path == "/api/embed":
            body = json.loads(request.content)
            assert body["model"] == settings.embedding_model
            assert body["input"] == ["The bridge is open.", "Die Brücke ist offen."]
            return httpx.Response(200, json={"embeddings": [[1.0, 0.0], [0.0, 1.0]]})
        return httpx.Response(404)

    with OllamaClient(settings, transport=httpx.MockTransport(respond)) as client:
        assert client.embed(["The bridge is open.", "Die Brücke ist offen."]) == [
            [1.0, 0.0],
            [0.0, 1.0],
        ]
        assert "embedding-revision" in client.model_key("embedding")
    assert sum(request.url.path == "/api/embed" for request in requests) == 1


def test_connection_failure_is_explicit_and_does_not_create_demo_output(settings):
    def unavailable(request):
        raise httpx.ConnectError("Ollama is not running", request=request)

    with OllamaClient(settings, transport=httpx.MockTransport(unavailable)) as client:
        assert client.status()["available"] is False
        with pytest.raises(ModelError):
            client.embed(["An article that must use the real configured model."])


def test_missing_model_is_reported(settings):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"models": []}))
    with OllamaClient(settings, transport=transport) as client:
        status = client.status()
        assert status["available"] is True
        assert settings.embedding_model in status["missing"]
        assert settings.chat_model in status["missing"]


@pytest.mark.parametrize(
    "body", [{}, {"models": {}}, {"models": [None]}, {"models": [{"name": 4}]}]
)
def test_malformed_model_list_is_reported_as_unavailable(settings, body):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    with OllamaClient(settings, transport=transport) as client:
        assert client.status()["available"] is False
        with pytest.raises(ModelError):
            client.model_key("embedding")


@pytest.mark.parametrize("content", ["not valid JSON", "{}", '{"claims": [{}]}'])
def test_invalid_model_output_fails_validation(settings, article, content):
    def respond(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=tags(settings))
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"message": {"content": content}})
        return httpx.Response(404)

    chunk = {
        "id": 7,
        "text": article.text,
        "article_id": 1,
        "start_char": 0,
        "end_char": len(article.text),
    }
    with OllamaClient(settings, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ModelError):
            client.extract_claims(article.model_dump(), [chunk])


def test_non_object_http_error_remains_a_model_error(settings):
    def respond(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=tags(settings))
        return httpx.Response(500, json=["unexpected upstream error"])

    with OllamaClient(settings, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ModelError, match="500"):
            client.embed(["a source passage"])


def test_claim_extraction_uses_a_selection_schema_and_preserves_source_wording(settings, article):
    def respond(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=tags(settings))
        if request.url.path == "/api/chat":
            body = json.loads(request.content)
            assert body["model"] == settings.chat_model
            assert body["stream"] is False
            assert body["think"] is False
            assert body["options"] == {"temperature": 0, "num_ctx": 8192, "num_predict": 1600}
            assert "claims" in body["format"]["properties"]
            supplied = json.loads(body["messages"][1]["content"])
            candidate = supplied["candidates"][0]
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": json.dumps(
                            {"claims": [{"candidate_id": candidate["candidate_id"]}]}
                        )
                    }
                },
            )
        return httpx.Response(404)

    chunk = {
        "id": 7,
        "text": article.text,
        "article_id": 1,
        "start_char": 0,
        "end_char": len(article.text),
    }
    with OllamaClient(settings, transport=httpx.MockTransport(respond)) as client:
        extracted = client.extract_claims(article.model_dump(), [chunk])
    assert extracted[0].chunk_id == 7
    assert extracted[0].claim_text == extracted[0].quote == article.text
    assert extracted[0].subject == extracted[0].attribution == ""
    assert validate_claims(extracted, [chunk]) == extracted


def test_structured_generation_uses_the_supplied_schema_and_token_budget(settings):
    messages = [{"role": "user", "content": "Extract assertions from the saved passages."}]

    def respond(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=tags(settings))
        body = json.loads(request.content)
        assert body["messages"] == messages
        assert body["format"] == ClaimBatch.model_json_schema()
        assert body["options"] == {"temperature": 0, "num_ctx": 8192, "num_predict": 123}
        assert body["stream"] is False
        assert body["think"] is False
        return httpx.Response(200, json={"message": {"content": '{"claims": []}'}})

    with OllamaClient(settings, transport=httpx.MockTransport(respond)) as client:
        result = client.generate_structured(messages, ClaimBatch, max_tokens=123)
    assert isinstance(result, ClaimBatch)
    assert result.claims == []


@pytest.mark.parametrize(
    "response",
    [
        [],
        {},
        {"message": None},
        {"message": []},
        {"message": {}},
        {"message": {"content": None}},
        {"message": {"content": {"claims": []}}},
        {"message": {"content": "not JSON"}},
        {"message": {"content": "{}"}},
    ],
)
def test_structured_generation_rejects_malformed_envelopes_and_content(settings, response):
    def respond(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=tags(settings))
        return httpx.Response(200, json=response)

    with OllamaClient(settings, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ModelError):
            client.generate_structured([], ClaimBatch, max_tokens=1600)


@pytest.mark.parametrize(
    "embeddings",
    [[], [[1.0]], [[1.0], [1.0, 0.0]], [[0.0, 0.0], [1.0, 0.0]]],
)
def test_malformed_embedding_batches_are_rejected(settings, embeddings):
    def respond(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=tags(settings))
        return httpx.Response(200, json={"embeddings": embeddings})

    with OllamaClient(settings, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ModelError):
            client.embed(["first passage", "second passage"])


def test_claim_quotes_must_exist_in_the_named_passage():
    chunk = {"id": 7, "text": "The city announced the bridge would reopen on Friday."}
    supported = ClaimInput(
        claim_text="The bridge will reopen on Friday.",
        quote="the bridge would reopen on Friday",
        chunk_id=7,
    )
    assert validate_claims([supported], [chunk]) == [supported]

    fabricated = supported.model_copy(update={"quote": "The bridge collapsed on Friday."})
    with pytest.raises(ValueError):
        validate_claims([fabricated], [chunk])
    wrong_passage = supported.model_copy(update={"chunk_id": 999})
    with pytest.raises(ValueError):
        validate_claims([wrong_passage], [chunk])
