"""Corpus growth must not turn ordinary views or searches into full materialization."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from evidence_explorer.records import AnalysisRequest, ArticleRecord, EmbeddingRecord, SearchRequest
from evidence_explorer.retrieval import hybrid_search_records
from evidence_explorer.worker import run_worker


@pytest.fixture
def indexed_corpus(repo, article):
    count = 600
    text = article.text
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    with repo._connection(write=True) as connection:
        connection.executemany(
            "INSERT INTO articles(id,title,publisher,url,published_at,text,language,topic,"
            "is_sample,content_hash,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    index,
                    f"Article {index}",
                    f"Publisher {index % 3}",
                    f"https://test.example/{index}",
                    article.published_at,
                    text,
                    "en" if index % 2 else "de",
                    "General",
                    0,
                    content_hash,
                    "now",
                    "now",
                )
                for index in range(1, count + 1)
            ],
        )
        connection.executemany(
            "INSERT INTO chunks(id,article_id,ordinal,text,start_char,end_char) "
            "VALUES (?,?,0,?,0,?)",
            [(index, index, text, len(text)) for index in range(1, count + 1)],
        )
        connection.executemany(
            "INSERT INTO embeddings(chunk_id,model_key,vector_json) VALUES (?,'test',?)",
            [(index, json.dumps([index / count, 1.0])) for index in range(1, count + 1)],
        )
    return repo


def test_search_streams_all_vector_batches_but_hydrates_only_candidates(
    indexed_corpus, monkeypatch
):
    repo = indexed_corpus
    monkeypatch.setattr(repo, "list_chunks", lambda *_: pytest.fail("Full passage load"))
    monkeypatch.setattr(repo, "list_embeddings", lambda *_: pytest.fail("Full vector load"))
    batches = []
    selected = []
    stream = repo.embeddings.iter_batches
    hydrate = repo.passages.by_ids

    def observed_stream(*args, **kwargs):
        for batch in stream(*args, **kwargs):
            batches.append(len(batch))
            yield batch

    def observed_hydrate(chunk_ids):
        selected.append(len(chunk_ids))
        return hydrate(chunk_ids)

    monkeypatch.setattr(repo.embeddings, "iter_batches", observed_stream)
    monkeypatch.setattr(repo.passages, "by_ids", observed_hydrate)
    matches = hybrid_search_records(repo, "no_lexical_match", [1.0, 0.0], "test")
    assert [match.article_id for match in matches] == list(range(600, 590, -1))
    assert sum(batches) == 600
    assert max(batches) <= 256
    assert max(selected) <= 100


def test_metadata_page_has_exact_totals_without_source_bodies(indexed_corpus, monkeypatch):
    repo = indexed_corpus
    monkeypatch.setattr(repo.articles, "list_articles", lambda: pytest.fail("Full article load"))
    view = repo.collection(article_limit=17)
    assert view.totals.articles == 600
    assert view.totals.sources == 3
    assert view.totals.languages == 2
    assert len(view.articles) == 17
    assert all("text" not in record.model_dump() for record in view.articles)
    second_page = repo.articles.page(limit=17, offset=17)
    assert not {record.id for record in view.articles} & {record.id for record in second_page}


def test_worker_uses_id_pages_instead_of_reading_all_article_bodies(
    repo, settings, article, monkeypatch
):
    article_id = repo.upsert_article(article)
    monkeypatch.setattr(repo, "list_articles", lambda: pytest.fail("Full article load"))
    monkeypatch.setattr(
        repo.articles, "list_articles", lambda: pytest.fail("Full typed article load")
    )
    job_id = repo.enqueue_job("analyze", {"mode": "demo"})
    run_worker(settings, once=True)
    assert repo.get_job(job_id)["status"] == "succeeded"
    assert repo.get_job(job_id)["result"]["articles"] == 1
    assert repo.passages.list_chunks(article_id)


def test_concurrent_import_cannot_expand_a_captured_analysis_scope(
    repo, settings, article, monkeypatch
):
    original_id = repo.upsert_article(article)
    capture = repo.articles.scope

    def import_after_capture():
        scope = capture()
        repo.upsert_article(article.model_copy(update={"url": "https://later.example/new"}))
        return scope

    monkeypatch.setattr(repo.articles, "scope", import_after_capture)
    job_id = repo.enqueue_job("analyze", {"mode": "demo"})
    run_worker(settings, once=True)
    job = repo.get_job(job_id)
    assert job["status"] == "succeeded", job["error"]
    assert job["result"]["articles"] == 1
    assert repo.passages.list_chunks(original_id)
    assert repo.passages.list_chunks(original_id + 1) == []


def test_typed_storage_records_are_used_and_wire_methods_still_serialize(repo, article):
    article_id = repo.upsert_article(article)
    record = repo.articles.get_article(article_id)
    assert isinstance(record, ArticleRecord)
    assert repo.get_article(article_id) == record.model_dump(mode="json")
    with pytest.raises(ValidationError):
        record.title = "Changed without persistence"


@pytest.mark.parametrize(
    "payload", [{"mdoe": "demo"}, {"article_ids": [True]}, {"article_ids": ["1"]}]
)
def test_analysis_requests_reject_ambiguous_or_misspelled_input(payload):
    with pytest.raises(ValidationError):
        AnalysisRequest.model_validate(payload)


def test_query_validation_and_vector_dimensions_fail_explicitly(indexed_corpus):
    with pytest.raises(ValidationError):
        SearchRequest(query="   ")
    with pytest.raises(ValidationError):
        EmbeddingRecord(chunk_id=1, vector=[float("nan")])
    with pytest.raises(ValueError, match="dimensions"):
        hybrid_search_records(indexed_corpus, "unmatched", [1, 0, 0], "test")
    assert hybrid_search_records(indexed_corpus, "unmatched", [1, 0], "test", limit=0) == []


@pytest.mark.parametrize("limit", [True, 1.5, 0, 501])
def test_article_pages_have_validated_hard_limits(repo, limit):
    with pytest.raises(ValidationError):
        repo.articles.page(limit=limit)
