"""Local citation aliases are a reversible HTTP encoding, never evidence recovery."""

import json
from copy import deepcopy

import httpx
import pytest

from evidence_explorer.config import Settings
from evidence_explorer.models_client import OllamaClient
from evidence_explorer.review.verifier import CheckError, OllamaVerifier, OpenRouterVerifier

MODEL = "test-local:9b"
PASSAGES = [
    {
        "fragment_id": "article:fragment:d5a0735d6cb86050949e",
        "record_id": "article:record:82d5001ee53595955a08",
        "text": "The notice says ‘unverified’—not confirmed.\nKeep this exact punctuation.",
        "truncated": False,
    },
    {
        "fragment_id": "article:fragment:ee2bbd2bf65a06484337",
        "record_id": "article:record:82d5001ee53595955a08",
        "text": "A second passage belongs to the same saved article.",
        "truncated": True,
    },
    {
        "fragment_id": "article:fragment:bd598a7dbaf597de5399",
        "record_id": "article:record:a0fb5587fa31adbb0033",
        "text": "Another source says: “No link has been established.”",
        "truncated": False,
    },
]


def objection(ids, quotes):
    return {
        "findings": [
            {
                "finding_type": "unsupported_inference",
                "explanation": "The passages do not substantiate attribution.",
                "fragment_ids": ids,
                "quotes": quotes,
            }
        ]
    }


def local_transport(output, captured):
    def respond(request):
        assert request.url.host == "127.0.0.1"
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": MODEL, "digest": "sha256:fixture"}]}
            )
        assert request.url.path == "/api/chat"
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": MODEL,
                "done": True,
                "done_reason": "stop",
                "message": {"content": json.dumps(output)},
            },
        )

    return httpx.MockTransport(respond)


def test_local_aliases_preserve_grouping_exact_quotes_and_canonical_output():
    passages = deepcopy(PASSAGES)
    before = deepcopy(passages)
    captured = []
    quotes = [passages[2]["text"], passages[0]["text"]]
    output = objection(["P3", "P1"], quotes)
    with OllamaClient(
        Settings(chat_model=MODEL), transport=local_transport(output, captured)
    ) as client:
        verifier = OllamaVerifier(client)
        first = verifier.check("The sources prove attribution.", passages)
        second = verifier.check("The sources prove attribution.", passages)

    assert first == second
    assert first["findings"][0]["fragment_ids"] == [
        passages[2]["fragment_id"],
        passages[0]["fragment_id"],
    ]
    assert first["findings"][0]["quotes"] == quotes
    assert passages == before, "Encoding an HTTP request must not mutate stored evidence"
    prompts = [json.loads(request["messages"][1]["content"]) for request in captured]
    assert prompts[0] == prompts[1], "The same ordered evidence must receive stable aliases"
    sent = prompts[0]["passages"]
    assert [passage["fragment_id"] for passage in sent] == ["P1", "P2", "P3"]
    assert [passage["record_id"] for passage in sent] == ["R1", "R1", "R2"]
    assert [passage["text"] for passage in sent] == [passage["text"] for passage in before]
    assert [passage["truncated"] for passage in sent] == [False, True, False]
    assert all(passage["fragment_id"] not in json.dumps(prompts[0]) for passage in before)
    assert all(passage["record_id"] not in json.dumps(prompts[0]) for passage in before)


@pytest.mark.parametrize(
    "invented_id",
    ["P0", "P4", "p1", "P01", "P1 ", "R1", PASSAGES[0]["fragment_id"]],
)
def test_local_model_must_return_exact_supplied_aliases(invented_id):
    output = objection(["P1", invented_id], [PASSAGES[0]["text"], PASSAGES[0]["text"]])
    with OllamaClient(Settings(chat_model=MODEL), transport=local_transport(output, [])) as client:
        with pytest.raises(CheckError, match="unknown passage alias"):
            OllamaVerifier(client).check("Claim", deepcopy(PASSAGES))


def test_local_aliases_do_not_carry_over_between_requests():
    captured = []
    output = objection(["P1"], ["Shared wording in two different saved records."])
    first_passage = {**PASSAGES[0], "text": output["findings"][0]["quotes"][0]}
    second_passage = {**PASSAGES[2], "text": first_passage["text"]}
    with OllamaClient(
        Settings(chat_model=MODEL), transport=local_transport(output, captured)
    ) as client:
        verifier = OllamaVerifier(client)
        first = verifier.check("Claim", [first_passage])
        second = verifier.check("Claim", [second_passage])
    assert first["findings"][0]["fragment_ids"] == [first_passage["fragment_id"]]
    assert second["findings"][0]["fragment_ids"] == [second_passage["fragment_id"]]
    assert len(captured) == 2


def test_cloud_verifier_continues_to_send_and_return_canonical_ids():
    captured = {}
    passages = deepcopy(PASSAGES)
    output = objection([passages[0]["fragment_id"]], [passages[0]["text"]])

    def respond(request):
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(output)}}]},
        )

    verifier = OpenRouterVerifier(
        "test-only-key", "test/model", transport=httpx.MockTransport(respond)
    )
    result = verifier.check("Claim", passages)
    assert json.loads(captured["messages"][1]["content"])["passages"] == PASSAGES
    assert result == output
    assert passages == PASSAGES
