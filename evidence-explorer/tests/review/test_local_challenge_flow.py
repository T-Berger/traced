"""Local model integration uses the real worker, storage and HTTP client with fake HTTP."""

import json

import httpx
import pytest

from evidence_explorer.config import Settings
from evidence_explorer.models_client import ModelError, OllamaClient
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.service import ReviewService
from evidence_explorer.review.storage import ReviewRepository
from evidence_explorer.review.verifier import CheckOutput
from evidence_explorer.review.worker import ReviewWorker

LOCAL_MODEL = "test-local:9b"
DIGEST = "sha256:test-model-version"
PRIVATE_ERROR = "private source text in model failure"


def provider_output(passages, *, invalid_quote=False):
    quote = load_demo_bundle().fragments[1].quote
    passage = next(passage for passage in passages if passage["text"] == quote)
    return {
        "findings": [
            {
                "finding_type": "unsupported_inference",
                "explanation": "A shared address does not establish direction of a campaign.",
                "fragment_ids": [passage["fragment_id"]],
                "quotes": [quote.replace(". This", "; this") if invalid_quote else quote],
            }
        ]
    }


@pytest.fixture
def local_desk(tmp_path, monkeypatch):
    monkeypatch.setenv("EVIDENCE_CHAT_MODEL", LOCAL_MODEL)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("EVIDENCE_REVIEW_MODEL", "")
    remote = {"digest": DIGEST, "models": [LOCAL_MODEL], "requests": [], "failure": None}

    def respond(request):
        assert request.url.host == "127.0.0.1", "Local checks must not switch providers"
        remote["requests"].append(request)
        if remote["failure"] == "connection":
            raise httpx.ConnectError(PRIVATE_ERROR, request=request)
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": name, "digest": remote["digest"]} for name in remote["models"]
                    ]
                },
            )
        assert request.url.path == "/api/chat"
        if remote["failure"] == "timeout":
            raise httpx.ReadTimeout(PRIVATE_ERROR, request=request)
        if remote["failure"] == "http":
            return httpx.Response(500, json={"error": PRIVATE_ERROR})
        if remote["failure"] == "invalid_json":
            return httpx.Response(200, json={"message": {"content": PRIVATE_ERROR}})
        prompt = json.loads(json.loads(request.content)["messages"][1]["content"])
        return httpx.Response(
            200,
            json={
                "model": LOCAL_MODEL,
                "done": True,
                "done_reason": "stop",
                "message": {
                    "content": json.dumps(
                        provider_output(
                            prompt["passages"], invalid_quote=remote["failure"] == "wrong_quote"
                        )
                    )
                },
            },
        )

    original = OllamaClient.__init__

    def initialize(self, settings, transport=None):
        original(self, settings, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(OllamaClient, "__init__", initialize)
    repository = ReviewRepository(tmp_path / "review.sqlite3")
    repository.initialize()
    service = ReviewService(repository)
    bundle = load_demo_bundle().model_dump(mode="json")
    bundle["case"].update(id="local-test:case", synthetic=False)
    case_id = service.import_bundle(bundle)
    scenario = service.start_scenario(case_id, "harbor:c3-v1")
    return repository, service, case_id, scenario, remote


def test_local_challenge_pins_installed_model_and_returns_saved_citations(local_desk, monkeypatch):
    repository, service, case_id, scenario, remote = local_desk
    scenario = service.exclude(
        scenario["id"], "harbor:r01", True, "Address record excluded", scenario["version"]
    )
    config = service.challenge_config()
    assert config["default_mode"] == "ollama"
    assert config["providers"]["ollama"]["ready"] is True
    assert config["providers"]["openrouter"]["ready"] is False
    job = service.challenge(case_id, "harbor:c3-v1", scenario["id"], mode="ollama")
    assert job["payload"]["model"] == LOCAL_MODEL
    assert DIGEST in job["payload"]["model_identity"]
    monkeypatch.setenv("EVIDENCE_CHAT_MODEL", "changed-after-queue:1b")
    assert ReviewWorker(repository).run_once()
    result = service.job(job["id"])
    assert result["status"] == "completed", result.get("error")
    assert result["result"]["mode"] == "ollama"
    assert result["result"]["findings"][0]["fragment_ids"] == ["harbor:f02"]
    scope = result["result"]["search_scope"]
    assert scope["provider"] == "ollama"
    assert scope["requested_model"] == LOCAL_MODEL
    assert scope["actual_model"] == LOCAL_MODEL
    assert DIGEST in scope["model"]
    assert result["result"]["search_scope"]["excluded_record_ids"] == ["harbor:r01"]
    posts = [request for request in remote["requests"] if request.method == "POST"]
    assert len(posts) == 1
    body = json.loads(posts[0].content)
    assert body["model"] == LOCAL_MODEL
    assert body["stream"] is False
    assert body["think"] is False
    assert isinstance(body["format"], dict)
    assert "tools" not in body
    prompt = json.loads(body["messages"][1]["content"])
    assert load_demo_bundle().fragments[0].quote not in {
        item["text"] for item in prompt["passages"]
    }
    assert "harbor:f02" in scope["fragment_ids"]
    assert result["result"]["findings"][0]["quotes"] == [load_demo_bundle().fragments[1].quote]
    restarted = ReviewService(ReviewRepository(repository.db_path))
    assert restarted.job(job["id"])["result"] == result["result"]


@pytest.mark.parametrize("problem", ["connection", "missing_model"])
def test_local_readiness_reports_problem_without_silent_cloud_fallback(local_desk, problem):
    repository, service, case_id, scenario, remote = local_desk
    if problem == "connection":
        remote["failure"] = problem
    else:
        remote["models"] = []
    config = service.challenge_config()
    assert config["providers"]["ollama"]["ready"] is False
    assert "Ollama" in config["providers"]["ollama"]["message"]
    with pytest.raises(ValueError):
        service.challenge(case_id, "harbor:c3-v1", scenario["id"], mode="ollama")
    assert ReviewWorker(repository).run_once() is False
    assert not any(request.method == "POST" for request in remote["requests"])


def test_replaced_installed_model_is_rejected_after_queue(local_desk):
    repository, service, case_id, scenario, remote = local_desk
    job = service.challenge(case_id, "harbor:c3-v1", scenario["id"], mode="ollama")
    remote["digest"] = "sha256:replaced-model"
    assert ReviewWorker(repository).run_once()
    result = service.job(job["id"])
    assert result["status"] == "failed"
    assert "changed" in result["error"].lower()
    assert not any(request.method == "POST" for request in remote["requests"])


def test_alias_restoration_does_not_accept_a_rewritten_quote(local_desk):
    repository, service, case_id, scenario, remote = local_desk
    job = service.challenge(case_id, "harbor:c3-v1", scenario["id"], mode="ollama")
    remote["failure"] = "wrong_quote"
    assert ReviewWorker(repository).run_once()
    failed = service.job(job["id"])
    assert failed["status"] == "failed"
    assert not failed.get("result")
    assert "quotation does not match saved fragment: harbor:f02" in failed["error"]
    # A subsequent correct response must persist the original IDs and exact saved text.
    remote["failure"] = None
    retry = service.challenge(case_id, "harbor:c3-v1", scenario["id"], mode="ollama")
    assert retry["id"] != job["id"]
    assert ReviewWorker(repository).run_once()
    completed = service.job(retry["id"])
    assert completed["status"] == "completed", completed.get("error")
    finding = completed["result"]["findings"][0]
    assert finding["fragment_ids"] == ["harbor:f02"]
    assert finding["quotes"] == [load_demo_bundle().fragments[1].quote]


@pytest.mark.parametrize("failure", ["timeout", "http", "invalid_json"])
def test_local_job_failure_is_visible_and_does_not_echo_private_provider_errors(
    local_desk, caplog, failure
):
    repository, service, case_id, scenario, remote = local_desk
    job = service.challenge(case_id, "harbor:c3-v1", scenario["id"], mode="ollama")
    remote["failure"] = failure
    assert ReviewWorker(repository).run_once()
    result = service.job(job["id"])
    assert result["status"] == "failed"
    assert not result.get("result")
    assert "Local model" in result["error"]
    assert PRIVATE_ERROR not in result["error"]
    assert PRIVATE_ERROR not in caplog.text
    remote["failure"] = None
    retry = service.challenge(case_id, "harbor:c3-v1", scenario["id"], mode="ollama")
    assert retry["id"] != job["id"] and retry["status"] == "queued"


@pytest.mark.parametrize(
    "response_fields",
    [{"done": False}, {"done_reason": "length"}, {"model": "unexpected-model:latest"}],
)
def test_structured_local_generation_rejects_incomplete_or_wrong_model_results(response_fields):
    def respond(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": LOCAL_MODEL, "digest": DIGEST}]})
        return httpx.Response(
            200,
            json={
                "model": LOCAL_MODEL,
                "done": True,
                "done_reason": "stop",
                "message": {"content": '{"findings": []}'},
                **response_fields,
            },
        )

    with OllamaClient(Settings(chat_model=LOCAL_MODEL), httpx.MockTransport(respond)) as client:
        with pytest.raises(ModelError, match="no result was accepted"):
            client.generate_structured([], CheckOutput, max_tokens=100)
        assert client.last_generation_metadata == {}


def test_local_model_configuration_rejects_nonlocal_endpoint_before_request(
    local_desk, monkeypatch
):
    _, service, case_id, scenario, remote = local_desk
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://untrusted.example.invalid")
    with pytest.raises(ValueError):
        service.challenge(case_id, "harbor:c3-v1", scenario["id"], mode="ollama")
    assert remote["requests"] == []
