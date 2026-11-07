"""Source meaning survives generation, old caches, storage, graphs and portable review."""

import json
from collections import Counter

import httpx
import pytest

from evidence_explorer import analysis
from evidence_explorer.chunking import chunk_text
from evidence_explorer.claim_selection import CLAIMS_POLICY_VERSION
from evidence_explorer.graphs import source_graph
from evidence_explorer.models import ArticleInput, ClaimInput
from evidence_explorer.models_client import OllamaClient
from evidence_explorer.review.integrations.explorer import bundle_from_repository
from evidence_explorer.review.integrations.packages import export_case_package, import_case_package
from evidence_explorer.service import ExplorerService
from evidence_explorer.worker import run_worker

INTERVIEW = (
    "Bayerns Landtagspräsidentin Ilse Aigner wird Unionskandidatin für die Wahl "
    "zur Bundespräsidentin.\n"
    "tagesthemen: Wo Markus Söder, eigentlich Ihr langjähriger politischer Rivale, "
    "eine zentrale Rolle gespielt hat. Hat er sie wirklich gewollt oder hat er "
    "einfach nur gepokert?\n"
    "Münch: Also sie ist jetzt sicherlich nicht die Kandidatin seines Herzens gewesen."
)
BAD_PARAPHRASE = "Markus Söder ist langjähriger politischer Rivale von Ursula Münch."


def wire_selections(monkeypatch, settings, *, select_containing, invalid_then_partial=False):
    calls = Counter()

    def respond(request):
        calls[request.url.path] += 1
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": settings.chat_model, "digest": "chat-revision"},
                        {"name": settings.embedding_model, "digest": "embedding-revision"},
                    ]
                },
            )
        payload = json.loads(request.content)
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[1.0, 0.5] for _ in payload["input"]]})
        assert request.url.path == "/api/chat"
        candidates = json.loads(payload["messages"][1]["content"])["candidates"]
        selected = next(
            candidate for candidate in candidates if select_containing in candidate["text"]
        )
        choices = [{"candidate_id": selected["candidate_id"]}]
        if invalid_then_partial:
            choices = (
                [{"candidate_id": "not-supplied"}]
                if calls["/api/chat"] == 1
                else [*choices, {"candidate_id": "not-supplied"}]
            )
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(
                        {
                            "claims": choices,
                        }
                    )
                }
            },
        )

    monkeypatch.setattr(
        analysis,
        "OllamaClient",
        lambda configured: OllamaClient(configured, transport=httpx.MockTransport(respond)),
    )
    return calls


def save_source(repo, text):
    article = ArticleInput(
        title="Attribution regression fixture",
        publisher="Fictional test publisher",
        url="https://source.example/attribution",
        published_at="",
        text=text,
        is_sample=True,
    )
    article_id = repo.upsert_article(article)
    chunks = repo.replace_chunks(article_id, chunk_text(text))
    return article_id, chunks


@pytest.mark.parametrize(
    "text,selected,required",
    [
        (
            INTERVIEW,
            "Münch:",
            ["Ihr langjähriger politischer Rivale", "Hat er sie", "Münch:", "nicht"],
        ),
        (
            INTERVIEW.replace("Ilse Aigner", "Alex Weber")
            .replace("Markus Söder", "Sam Roth")
            .replace("Münch:", "Keller:"),
            "Keller:",
            ["Sam Roth", "Ihr langjähriger", "Keller:", "nicht"],
        ),
        (
            "Interviewer: Has Morgan approved the plan?\nDana: In my view, it might fail. "
            "The board has not approved it.",
            "Dana:",
            ["Has Morgan", "Dana:", "In my view", "might", "not"],
        ),
        (
            "The minister said Morgan had not approved the plan.",
            "minister said",
            ["minister said", "Morgan", "not"],
        ),
        (
            "Morgan met Lee after the hearing. She said the plan might fail.",
            "She said",
            ["Morgan met Lee", "She said", "might"],
        ),
    ],
)
def test_meaning_is_unchanged_through_worker_graph_and_portable_case(
    repo, settings, monkeypatch, tmp_path, text, selected, required
):
    article_id, _ = save_source(repo, text)
    calls = wire_selections(monkeypatch, settings, select_containing=selected)
    service = ExplorerService(settings)
    job_id = service.enqueue_analysis("ollama", [article_id])
    assert run_worker(settings, once=True) == 1
    job = repo.get_job(job_id)
    assert job["status"] == "succeeded", job["error"]
    assert job["result"]["claims_created"] == 1
    assert calls["/api/chat"] == 1
    claim = repo.list_active_claims(article_id)[0]
    assert claim["claim_text"] == claim["quote"]
    assert claim["quote"] in text
    assert all(word in claim["claim_text"] for word in required)
    assert BAD_PARAPHRASE not in claim["claim_text"]
    assert claim["subject"] == claim["attribution"] == ""
    assert claim["event_time"] is claim["location"] is None
    graph = source_graph(
        [repo.get_article(article_id)], repo.list_active_claims(article_id), article_id
    )
    assert graph.claims[0]["claim_text"] == claim["quote"]
    bundle = bundle_from_repository(repo)
    assert bundle.findings[0].statement == claim["quote"]
    assert bundle.findings[0].details["subject"] == ""
    assert bundle.findings[0].details["attribution"] is None
    reopened = import_case_package(export_case_package(bundle), tmp_path / "reviewer")
    assert reopened.fingerprint() == bundle.fingerprint()
    assert reopened.findings[0].statement == claim["quote"]
    assert reopened.findings[0].details["review_state"] == "unreviewed"


def test_old_misattribution_cache_is_replaced_as_current_without_reembedding_or_losing_history(
    repo, settings, monkeypatch
):
    article_id, chunks = save_source(repo, INTERVIEW)
    calls = wire_selections(monkeypatch, settings, select_containing="Münch:")
    old_key = f"ollama:{settings.chat_model}@chat-revision:v1"
    embedding_key = f"ollama:{settings.embedding_model}@embedding-revision:v1"
    for chunk in chunks:
        repo.save_embedding(chunk["id"], embedding_key, [1.0, 0.5])
    source_hash = repo.get_article(article_id)["content_hash"]
    old_marker = f"claims:{article_id}:{source_hash}:{old_key}"
    repo.replace_claims(
        article_id,
        [
            ClaimInput(
                claim_text=BAD_PARAPHRASE,
                quote="Wo Markus Söder, eigentlich Ihr langjähriger politischer Rivale",
                chunk_id=chunks[0]["id"],
            )
        ],
        old_key,
        cache_key=old_marker,
    )
    assert repo.get_meta(old_marker) == "done"
    assert repo.list_active_claims(article_id) == []
    service = ExplorerService(settings)
    first = service.enqueue_analysis("ollama", [article_id])
    assert run_worker(settings, once=True) == 1
    result = repo.get_job(first)["result"]
    assert result["embeddings_created"] == result["cached"] == 0
    assert result["claims_created"] == 1
    assert result["model_key"].endswith(":" + CLAIMS_POLICY_VERSION)
    assert calls["/api/embed"] == 0
    assert calls["/api/chat"] == 1
    active = repo.list_active_claims(article_id)
    assert len(active) == 1
    assert active[0]["claim_text"] == active[0]["quote"]
    assert active[0]["claim_text"] != BAD_PARAPHRASE
    assert {row["model_key"] for row in repo.list_claims(article_id)} == {
        old_key,
        result["model_key"],
    }
    assert [f.statement for f in bundle_from_repository(repo).findings] == [active[0]["quote"]]
    assert bundle_from_repository(repo, model_key=old_key).findings[0].statement == BAD_PARAPHRASE
    second = service.enqueue_analysis("ollama", [article_id])
    assert run_worker(settings, once=True) == 1
    assert repo.get_job(second)["result"]["cached"] == 1
    assert calls["/api/chat"] == 1
    assert calls["/api/embed"] == 0
    assert repo.list_active_claims(article_id) == active


def test_rejected_selection_warning_survives_current_policy_cache_and_reopen(
    repo, settings, monkeypatch
):
    article_id, _ = save_source(repo, "The minister said Morgan had not approved the plan.")
    calls = wire_selections(
        monkeypatch, settings, select_containing="minister said", invalid_then_partial=True
    )
    service = ExplorerService(settings)
    first = service.enqueue_analysis("ollama", [article_id])
    assert run_worker(settings, once=True) == 1
    first_job = repo.get_job(first)
    assert first_job["status"] == "succeeded", first_job["error"]
    assert first_job["result"]["claims_created"] == 1
    assert first_job["result"]["claims_rejected"] == 1
    assert calls["/api/chat"] == 2
    saved = repo.list_active_claims(article_id)
    second_service = ExplorerService(settings)
    second = second_service.enqueue_analysis("ollama", [article_id])
    assert run_worker(settings, once=True) == 1
    result = second_service.repo.get_job(second)["result"]
    assert result["cached"] == result["claims_rejected"] == 1
    assert result["claims_created"] == result["embeddings_created"] == 0
    assert calls["/api/chat"] == 2
    assert calls["/api/embed"] == 1
    assert second_service.repo.list_active_claims(article_id) == saved
