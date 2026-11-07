"""A real worker and SQLite queue exercise the provider through a fake HTTP transport."""

import json
import sqlite3

import httpx
import pytest

from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.service import ReviewService
from evidence_explorer.review.storage import ReviewRepository
from evidence_explorer.review.verifier import MAX_PROMPT_BYTES, OpenRouterVerifier
from evidence_explorer.review.worker import ReviewWorker

TEST_KEY = "test-only-api-secret-never-persist"
PRIVATE_ERROR = "provider error body contains private source text"
MODEL = "test/requested-model"
ACTUAL_MODEL = "test/actual-model-version"


@pytest.fixture
def live_desk(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_KEY)
    monkeypatch.setenv("EVIDENCE_REVIEW_MODEL", MODEL)
    # Configuration probes must remain offline even when the local model is the default.
    from evidence_explorer.models_client import OllamaClient

    original_local = OllamaClient.__init__

    def local_unavailable(self, settings, transport=None):
        original_local(
            self,
            settings,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"models": []})),
        )

    monkeypatch.setattr(OllamaClient, "__init__", local_unavailable)
    repository = ReviewRepository(tmp_path / "review.sqlite3")
    repository.initialize()
    service = ReviewService(repository)
    data = load_demo_bundle().model_dump(mode="json")
    data["case"].update(id="live-test:case", synthetic=False)
    case_id = service.import_bundle(data)
    scenario = service.start_scenario(case_id, "harbor:c3-v1")
    return repository, service, case_id, scenario


def intercept_provider(monkeypatch, respond):
    original = OpenRouterVerifier.__init__

    def initialize(self, api_key, model, **kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        original(self, api_key, model, **kwargs)

    monkeypatch.setattr(OpenRouterVerifier, "__init__", initialize)


def result_payload(fragment="harbor:f02", quote=None):
    quote = quote or load_demo_bundle().fragments[1].quote
    return {
        "findings": [
            {
                "finding_type": "unsupported_inference",
                "explanation": "A shared correspondence address does not establish control.",
                "fragment_ids": [fragment],
                "quotes": [quote],
            }
        ]
    }


def response_for(result):
    return httpx.Response(
        200,
        json={
            "id": "test-completion-id",
            "model": ACTUAL_MODEL,
            "choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        },
    )


def stored_text(repository):
    with sqlite3.connect(repository.db_path) as connection:
        return "\n".join(connection.iterdump())


def test_imported_case_worker_pins_model_and_evidence_and_returns_exact_citation(
    live_desk, monkeypatch
):
    repository, service, case_id, scenario = live_desk
    scenario = service.exclude(
        scenario["id"], "harbor:r01", True, "Test exclusion", scenario["version"]
    )
    captured = {}

    def respond(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {TEST_KEY}"
        captured.update(json.loads(request.content))
        return response_for(result_payload())

    intercept_provider(monkeypatch, respond)
    assert service.challenge_config()["providers"]["openrouter"]["ready"] is True
    job = service.challenge(case_id, "harbor:c3-v1", scenario["id"], mode="openrouter")
    assert job["payload"]["model"] == MODEL
    assert job["payload"]["excluded_record_ids"] == ["harbor:r01"]
    monkeypatch.setenv("EVIDENCE_REVIEW_MODEL", "test/changed-after-queue")
    assert ReviewWorker(repository).run_once() is True
    completed = service.job(job["id"])
    assert completed["status"] == "completed", completed["error"]
    assert completed["stale"] is False
    assert captured["model"] == MODEL
    assert captured["response_format"]["type"] == "json_schema"
    assert captured["provider"]["require_parameters"] is True
    assert "tools" not in captured
    prompt = captured["messages"][1]["content"]
    assert len(prompt.encode()) <= MAX_PROMPT_BYTES
    passages = json.loads(prompt)["passages"]
    assert "harbor:r01" not in {passage["record_id"] for passage in passages}
    finding = completed["result"]["findings"][0]
    assert finding["fragment_ids"] == ["harbor:f02"]
    assert finding["quotes"] == [load_demo_bundle().fragments[1].quote]
    assert completed["result"]["search_scope"]["actual_model"] == ACTUAL_MODEL
    assert TEST_KEY not in stored_text(repository)
    # A fresh service/connection can retrieve the completed cited result.
    restarted = ReviewService(ReviewRepository(repository.db_path))
    assert restarted.job(job["id"])["result"] == completed["result"]


@pytest.mark.parametrize("missing", ["OPENROUTER_API_KEY", "EVIDENCE_REVIEW_MODEL"])
def test_missing_provider_configuration_is_actionable_before_queue(live_desk, monkeypatch, missing):
    repository, service, case_id, scenario = live_desk
    monkeypatch.setenv(missing, "")
    config = service.challenge_config()["providers"]["openrouter"]
    assert config["ready"] is False
    assert missing in config["missing"]
    assert missing in config["message"]
    assert TEST_KEY not in json.dumps(config)
    with pytest.raises(ValueError, match=missing):
        service.challenge(case_id, "harbor:c3-v1", scenario["id"], mode="openrouter")
    with sqlite3.connect(repository.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_jobs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "failure", ["auth", "rate_limit", "timeout", "malformed", "unknown_citation", "wrong_quote"]
)
def test_provider_failure_cannot_become_a_clean_result_or_leak_response_body(
    live_desk, monkeypatch, caplog, failure
):
    repository, service, case_id, scenario = live_desk

    def respond(request):
        if failure in {"auth", "rate_limit"}:
            return httpx.Response(
                401 if failure == "auth" else 429,
                json={"error": {"message": f"{PRIVATE_ERROR} {TEST_KEY}"}},
            )
        if failure == "timeout":
            raise httpx.ReadTimeout(f"{PRIVATE_ERROR} {TEST_KEY}", request=request)
        if failure == "malformed":
            return httpx.Response(200, json={"choices": [{"message": {"content": PRIVATE_ERROR}}]})
        if failure == "unknown_citation":
            return response_for(result_payload(fragment="not-a-saved-fragment"))
        return response_for(result_payload(quote=PRIVATE_ERROR))

    intercept_provider(monkeypatch, respond)
    job = service.challenge(case_id, "harbor:c3-v1", scenario["id"], mode="openrouter")
    assert ReviewWorker(repository).run_once()
    failed = service.job(job["id"])
    assert failed["status"] == "failed"
    assert not failed.get("result")
    assert failed["error"]
    for text in (failed["error"], stored_text(repository), caplog.text):
        assert TEST_KEY not in text
        assert PRIVATE_ERROR not in text
    # A retry must create an actionable new attempt, not reuse the failed response.
    retry = service.challenge(case_id, "harbor:c3-v1", scenario["id"], mode="openrouter")
    assert retry["status"] == "queued"
    assert retry["id"] != job["id"]


def test_real_investigation_never_falls_back_to_deterministic_demo(live_desk):
    repository, service, case_id, scenario = live_desk
    with pytest.raises(ValueError, match="synthetic"):
        service.challenge(case_id, "harbor:c3-v1", scenario["id"], mode="demo")
    assert ReviewWorker(repository).run_once() is False
