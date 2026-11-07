"""Provider behavior is checked at its HTTP boundary, without global monkeypatches."""

import json

import httpx
import pytest

from evidence_explorer.review.verifier import MAX_PROMPT_BYTES, CheckError, OpenRouterVerifier


def test_openrouter_request_is_structured_bounded_and_has_no_tools():
    captured = {}

    def respond(request):
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps({"findings": []})}}],
            },
        )

    verifier = OpenRouterVerifier(
        "test-only-secret", "test/model", transport=httpx.MockTransport(respond)
    )
    result = verifier.check("Does the evidence support this claim?", [])
    assert result["findings"] == []
    assert captured["model"] == "test/model"
    assert captured["response_format"]["type"] == "json_schema"
    assert captured["provider"]["require_parameters"] is True
    assert captured["max_tokens"] == 1800
    assert "tools" not in captured
    assert len(captured["messages"][1]["content"].encode()) <= MAX_PROMPT_BYTES


def test_provider_error_never_appears_as_a_clean_review():
    verifier = OpenRouterVerifier(
        "test-only-secret",
        "test/model",
        transport=httpx.MockTransport(lambda request: httpx.Response(429)),
    )
    with pytest.raises(CheckError, match="HTTP 429"):
        verifier.check("Claim", [])


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"choices": []},
        {
            "choices": [{"message": {"content": "not JSON"}}],
        },
    ],
)
def test_malformed_provider_response_is_a_check_error(payload):
    verifier = OpenRouterVerifier(
        "test-only-secret",
        "test/model",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    with pytest.raises(CheckError, match="invalid review JSON"):
        verifier.check("Claim", [])
